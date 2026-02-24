"""
K线生产者 — 订阅 K 线、驱动 BrooksStrategy、分发信号
"""
import asyncio
import logging
from typing import Dict, List, Optional

import pandas as pd
from binance import BinanceSocketManager, AsyncClient
from binance.exceptions import ReadLoopClosed

from config import SYMBOL as CONFIG_SYMBOL, OBSERVE_MODE
from strategy import BrooksStrategy
from trade_logger import TradeLogger
from workers.helpers import load_historical_klines, fill_missing_klines
from logic.constants import signal_side, is_spike_signal

try:
    from websockets.exceptions import ConnectionClosed
except ImportError:
    ConnectionClosed = Exception  # type: ignore

SYMBOL = CONFIG_SYMBOL
INTERVAL = AsyncClient.KLINE_INTERVAL_5MINUTE


async def kline_producer(
    user_queues: List[asyncio.Queue],
    close_queues: Dict[str, asyncio.Queue],
    strategy: BrooksStrategy,
    trade_logger: TradeLogger,
) -> None:
    history: List[Dict] = []
    kline_count = 0
    reconnect_attempt = 0
    max_reconnect_attempts = 10
    base_delay = 1
    client: Optional[AsyncClient] = None
    last_kline_timestamp: Optional[int] = None

    while reconnect_attempt < max_reconnect_attempts:
        try:
            logging.info(
                f"正在连接 Binance API，订阅 {SYMBOL} {INTERVAL} K线数据..."
                + (f" (重连尝试 {reconnect_attempt + 1}/{max_reconnect_attempts})"
                   if reconnect_attempt > 0 else "")
            )

            try:
                if client is not None:
                    try:
                        await client.close_connection()
                    except Exception:
                        pass
                client = await AsyncClient.create()
                logging.info("Binance 客户端已创建")
            except Exception as e:
                logging.error(f"创建 Binance 客户端失败: {e}", exc_info=True)
                raise

            if reconnect_attempt == 0:
                last_kline_timestamp = await load_historical_klines(client, history)
            else:
                last_kline_timestamp = await fill_missing_klines(client, history, last_kline_timestamp)

            if len(history) >= 50:
                df = pd.DataFrame(history)
                result = strategy.on_new_bar(df)
                logging.info(
                    f"市场状态扫描完成: state={strategy.mstate.state.value} "
                    f"AI={strategy.mstate.always_in.name}"
                )

            bm = BinanceSocketManager(client, max_queue_size=10000)
            kline_stream = bm.kline_futures_socket(symbol=SYMBOL, interval=INTERVAL)
            logging.info(f"合约K线 WebSocket 流已创建: {SYMBOL} {INTERVAL}")

            reconnect_attempt = 0
            kline_count = len(history)

            try:
                async with kline_stream as stream:
                    logging.info("WebSocket 连接已建立，开始接收实时 K 线数据...")
                    while True:
                        try:
                            msg = await stream.recv()
                            if not msg:
                                continue

                            k = msg.get("k", {})
                            if not k:
                                continue

                            current_price = float(k.get("c", 0))
                            if current_price <= 0:
                                current_price = float(k.get("l", 0))

                            await _check_stop_loss_take_profit(
                                trade_logger, close_queues, current_price, check_stop_loss=False
                            )

                            if not k.get("x"):
                                continue

                            close_price = float(k.get("c", 0))
                            await _check_stop_loss_take_profit(
                                trade_logger, close_queues, close_price, check_stop_loss=True
                            )

                            kline_count += 1
                            kline_open_time = int(k.get("t", 0))
                            logging.info(
                                f"K线收盘 #{kline_count}: O={float(k['o']):.2f} "
                                f"H={float(k['h']):.2f} L={float(k['l']):.2f} C={float(k['c']):.2f}"
                            )

                            kline_data = {
                                "timestamp": kline_open_time,
                                "open": float(k["o"]),
                                "high": float(k["h"]),
                                "low": float(k["l"]),
                                "close": float(k["c"]),
                            }
                            last_kline_timestamp = kline_open_time
                            history.append(kline_data)
                            while len(history) > 500:
                                history.pop(0)

                            if len(history) < 50:
                                continue

                            df = pd.DataFrame(history)
                            result = strategy.on_new_bar(df)

                            trade_logger.increment_kline()

                            for u in list(trade_logger.positions.keys()):
                                if trade_logger.needs_tp1_fill_sync(u) and u in close_queues:
                                    await close_queues[u].put({"action": "sync_tp1"})

                            if result is not None:
                                signal = _build_signal(result)
                                _log_signal(signal)
                                for q in user_queues:
                                    await q.put(signal)

                        except asyncio.CancelledError:
                            logging.info("K线生产者任务已取消")
                            raise
                        except ReadLoopClosed:
                            logging.warning("WebSocket 读取循环已关闭，准备重连...")
                            raise
                        except (ConnectionClosed, ConnectionError, OSError) as e:
                            logging.warning(f"WebSocket 连接断开: {e}")
                            raise
                        except Exception as e:
                            logging.error(f"处理 K 线消息时出错: {e}", exc_info=True)
                            await asyncio.sleep(1)

            except asyncio.CancelledError:
                raise
            except (ReadLoopClosed, ConnectionClosed, ConnectionError, OSError) as e:
                logging.warning(f"WebSocket 连接错误: {e}")
                reconnect_attempt += 1
                if client is not None:
                    try:
                        await client.close_connection()
                    except Exception:
                        pass
                delay = min(base_delay * (2 ** reconnect_attempt), 60)
                logging.info(f"等待 {delay} 秒后尝试重连...")
                await asyncio.sleep(delay)
                continue

        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.error(f"K线生产者发生未预期的错误: {e}", exc_info=True)
            reconnect_attempt += 1
            if client is not None:
                try:
                    await client.close_connection()
                except Exception:
                    pass
            if reconnect_attempt >= max_reconnect_attempts:
                logging.error(f"达到最大重连次数 ({max_reconnect_attempts})，停止重连")
                break
            delay = min(base_delay * (2 ** reconnect_attempt), 60)
            await asyncio.sleep(delay)

    try:
        await client.close_connection()
    except Exception:
        pass


async def _check_stop_loss_take_profit(
    trade_logger: TradeLogger,
    close_queues: Dict[str, asyncio.Queue],
    current_price: float,
    check_stop_loss: bool = True,
) -> None:
    if current_price <= 0:
        return
    for user_name in list(trade_logger.positions.keys()):
        trade = trade_logger.positions.get(user_name)
        if trade is None:
            continue
        result = trade_logger.check_stop_loss_take_profit(
            user_name, current_price, check_stop_loss=check_stop_loss
        )
        if not result:
            continue

        if isinstance(result, dict) and result.get("action") == "tp1":
            tp1_info = result
            logging.info(
                f"[{user_name}] TP1触发: 平仓50% @ {tp1_info['close_price']:.2f}"
            )
            if not OBSERVE_MODE and user_name in close_queues:
                tp1_request = {
                    "action": "tp1",
                    "side": "SELL" if tp1_info["trade"].side.lower() == "buy" else "BUY",
                    "close_quantity": tp1_info["close_quantity"],
                    "close_price": tp1_info["close_price"],
                    "new_stop_loss": tp1_info["new_stop_loss"],
                    "tp2_price": tp1_info["tp2_price"],
                    "remaining_quantity": tp1_info["trade"].remaining_quantity,
                    "entry_price": float(tp1_info["trade"].entry_price),
                    "position_side": tp1_info["trade"].side,
                }
                await close_queues[user_name].put(tp1_request)
        else:
            closed_trade = result
            logging.info(
                f"[{user_name}] {closed_trade.exit_reason}: "
                f"价格={current_price:.2f}, 盈亏={closed_trade.pnl:.4f} USDT"
            )
            if not OBSERVE_MODE and user_name in close_queues:
                close_request = {
                    "action": "close",
                    "side": closed_trade.side,
                    "quantity": float(closed_trade.remaining_quantity or closed_trade.quantity),
                    "exit_price": float(closed_trade.exit_price),
                    "exit_reason": closed_trade.exit_reason,
                }
                await close_queues[user_name].put(close_request)


def _build_signal(result) -> Dict:
    """从 SignalResult 构建信号字典"""
    side = signal_side(result.signal_type)
    is_spike = is_spike_signal(result.signal_type)
    return {
        "signal": result.signal_type.name,
        "side": side,
        "price": result.entry_price,
        "stop_loss": result.stop_loss,
        "take_profit": result.tp2,
        "tp1_price": result.tp1,
        "tp2_price": result.tp2,
        "market_state": result.reason,
        "is_spike": is_spike,
        "signal_strength": 1.0,
        "tp1_close_ratio": 0.5,
    }


def _log_signal(signal: Dict) -> None:
    tp1 = signal.get("tp1_price", 0)
    tp2 = signal.get("tp2_price", 0)
    entry_type = "市价" if signal.get("is_spike") else "限价"
    logging.info(
        f"信号: {signal['signal']} {signal['side']} @ {signal['price']:.2f} ({entry_type}), "
        f"SL={signal['stop_loss']:.2f}, TP1={tp1:.2f}, TP2={tp2:.2f}"
    )
