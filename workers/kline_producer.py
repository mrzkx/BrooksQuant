"""
K线生产者模块

负责订阅K线数据、生成策略信号并分发给用户队列
"""

import asyncio
import logging
import os
from typing import Dict, List, Optional

import pandas as pd

from binance import BinanceSocketManager, AsyncClient
from binance.exceptions import ReadLoopClosed

from config import SYMBOL as CONFIG_SYMBOL, KLINE_INTERVAL
from strategy import AlBrooksStrategy
from trade_logger import TradeLogger
from workers.helpers import load_historical_klines, fill_missing_klines

# 尝试导入 websockets 异常
try:
    from websockets.exceptions import ConnectionClosed
except ImportError:
    ConnectionClosed = Exception  # type: ignore

SYMBOL = CONFIG_SYMBOL
INTERVAL = AsyncClient.KLINE_INTERVAL_5MINUTE
OBSERVE_MODE = os.getenv("OBSERVE_MODE", "true").lower() == "true"


async def kline_producer(
    user_queues: List[asyncio.Queue],
    close_queues: Dict[str, asyncio.Queue],
    strategy: AlBrooksStrategy,
    trade_logger: TradeLogger,
) -> None:
    """
    订阅 K 线，生成策略信号并分发给所有用户队列
    
    支持自动重连和指数退避机制，基于时间戳精确补全缺失的 K 线
    """
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

            # 创建客户端
            try:
                if client is not None:
                    try:
                        await client.close_connection()
                    except:
                        pass
                client = await AsyncClient.create()
                logging.info("Binance 客户端已创建")
            except Exception as e:
                logging.error(f"创建 Binance 客户端失败: {e}", exc_info=True)
                raise

            # 加载或补全历史K线数据
            if reconnect_attempt == 0:
                last_kline_timestamp = await load_historical_klines(client, history)
            else:
                logging.info(f"重连后补全数据，上次最后K线时间戳: {last_kline_timestamp}")
                last_kline_timestamp = await fill_missing_klines(client, history, last_kline_timestamp)

            # 进行一次信号扫描
            if len(history) >= 50:
                df = pd.DataFrame(history)
                signals_df = await strategy.generate_signals(df)
                last = signals_df.iloc[-1]
                market_state = last.get("market_state", "Unknown")
                logging.info(f"市场状态扫描完成，当前市场模式: {market_state}")

            # 创建WebSocket流（必须传入 max_queue_size 防止队列溢出）
            bm = BinanceSocketManager(client, max_queue_size=10000)
            kline_stream = bm.kline_socket(symbol=SYMBOL, interval=INTERVAL)
            logging.info(f"K线 WebSocket 流已创建: {SYMBOL} {INTERVAL}")

            # 重置重连计数
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

                            # 获取实时价格
                            current_price = float(k.get("c", 0))
                            if current_price <= 0:
                                current_price = float(k.get("l", 0))

                            # 实时检查止损止盈
                            await _check_stop_loss_take_profit(
                                trade_logger, close_queues, current_price
                            )

                            if not k.get("x"):  # 只处理已收盘的K线
                                continue

                            # 处理已收盘的K线
                            kline_count += 1
                            kline_open_time = int(k.get("t", 0))
                            logging.info(
                                f"📊 K线收盘 #{kline_count}: O={float(k['o']):.2f} "
                                f"H={float(k['h']):.2f} L={float(k['l']):.2f} C={float(k['c']):.2f}"
                            )

                            # 更新历史数据
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

                            # 生成信号
                            df = pd.DataFrame(history)
                            signals_df = await strategy.generate_signals(df)
                            last = signals_df.iloc[-1]

                            trade_logger.increment_kline()

                            if last["signal"]:
                                signal = _build_signal(last, k, df)
                                _log_signal(signal, last)
                                
                                # 广播给所有用户
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
                logging.info("K线生产者任务已取消")
                raise
            except (ReadLoopClosed, ConnectionClosed, ConnectionError, OSError) as e:
                logging.warning(f"WebSocket 连接错误: {e}")
                reconnect_attempt += 1
                if client is not None:
                    try:
                        await client.close_connection()
                    except:
                        pass
                delay = min(base_delay * (2**reconnect_attempt), 60)
                logging.info(f"等待 {delay} 秒后尝试重连...")
                await asyncio.sleep(delay)
                continue

        except asyncio.CancelledError:
            logging.info("K线生产者任务已取消")
            break
        except Exception as e:
            logging.error(f"K线生产者发生未预期的错误: {e}", exc_info=True)
            reconnect_attempt += 1
            if client is not None:
                try:
                    await client.close_connection()
                except:
                    pass
            if reconnect_attempt >= max_reconnect_attempts:
                logging.error(f"达到最大重连次数 ({max_reconnect_attempts})，停止重连")
                break
            delay = min(base_delay * (2**reconnect_attempt), 60)
            logging.info(f"等待 {delay} 秒后尝试重连...")
            await asyncio.sleep(delay)

    # 最终清理
    try:
        await client.close_connection()
        logging.info("Binance 客户端连接已关闭")
    except:
        pass


async def _check_stop_loss_take_profit(
    trade_logger: TradeLogger,
    close_queues: Dict[str, asyncio.Queue],
    current_price: float
) -> None:
    """检查止损止盈"""
    if current_price <= 0:
        return
    
    for user_name in list(trade_logger.positions.keys()):
        trade = trade_logger.positions.get(user_name)
        if trade is None:
            continue
        
        result = trade_logger.check_stop_loss_take_profit(user_name, current_price)
        
        if not result:
            continue
        
        # 处理TP1操作（返回字典）
        if isinstance(result, dict) and result.get("action") == "tp1":
            tp1_info = result
            logging.info(
                f"[{user_name}] TP1触发: 平仓50% @ {tp1_info['close_price']:.2f}, "
                f"新止损={tp1_info['new_stop_loss']:.2f}"
            )
            print(f"[{user_name}] 🎯 TP1触发: 平仓50% @ {tp1_info['close_price']:.2f}")
            
            if not OBSERVE_MODE and user_name in close_queues:
                tp1_request = {
                    "action": "tp1",
                    "side": tp1_info["trade"].side,
                    "close_quantity": tp1_info["close_quantity"],
                    "close_price": tp1_info["close_price"],
                    "new_stop_loss": tp1_info["new_stop_loss"],
                    "tp2_price": tp1_info["tp2_price"],
                    "remaining_quantity": tp1_info["trade"].remaining_quantity,
                }
                await close_queues[user_name].put(tp1_request)
                logging.info(f"[{user_name}] 已发送TP1请求到队列")
        
        else:
            # 完全平仓（Trade对象）
            closed_trade = result
            logging.info(
                f"[{user_name}] {closed_trade.exit_reason}: "
                f"价格={current_price:.2f}, 盈亏={closed_trade.pnl:.4f} USDT ({closed_trade.pnl_percent:.2f}%)"
            )
            print(
                f"[{user_name}] {closed_trade.exit_reason}: "
                f"价格={current_price:.2f}, 盈亏={closed_trade.pnl:.4f} USDT ({closed_trade.pnl_percent:.2f}%)"
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
                logging.info(f"[{user_name}] 已发送平仓请求到队列")


def _build_signal(last, k, df) -> Dict:
    """构建信号字典"""
    entry_price = last["close"]
    stop_loss = last["stop_loss"]
    risk_reward_ratio = last.get("risk_reward_ratio", 1.0)
    base_height = last.get("base_height", None)
    tp1_price = last.get("tp1_price", None)
    tp2_price = last.get("tp2_price", None)
    tight_channel_score = last.get("tight_channel_score", 0.0)
    market_state = last.get("market_state", "Unknown")
    atr_value = last.get("atr", None)
    
    # 计算信号强度
    current_bar = df.iloc[-1]
    signal_strength = abs(current_bar["close"] - current_bar["open"])
    
    # 计算止盈
    if tp1_price and tp2_price:
        take_profit = tp2_price
    else:
        if last["side"] == "buy":
            stop_distance = entry_price - stop_loss
        else:
            stop_distance = stop_loss - entry_price
        
        min_tp_distance = stop_distance * 2.0
        traditional_tp_distance = stop_distance * risk_reward_ratio
        
        if base_height and base_height > 0:
            actual_tp_distance = max(base_height, traditional_tp_distance, min_tp_distance)
        else:
            actual_tp_distance = max(traditional_tp_distance, min_tp_distance)
        
        if last["side"] == "buy":
            take_profit = entry_price + actual_tp_distance
        else:
            take_profit = entry_price - actual_tp_distance

    return {
        "signal": last["signal"],
        "side": last["side"],
        "price": entry_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_reward_ratio": risk_reward_ratio,
        "market_state": market_state,
        "signal_strength": signal_strength,
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
        "tight_channel_score": tight_channel_score,
        "atr": atr_value,
    }


def _log_signal(signal: Dict, last) -> None:
    """记录信号日志"""
    state_map = {
        "Breakout": "突破模式(Spike)",
        "Channel": "通道模式(Channel)",
        "TradingRange": "区间模式(Range)",
        "Unknown": "未知状态",
    }
    state_display = state_map.get(signal["market_state"], signal["market_state"])
    
    tp1_price = signal.get("tp1_price")
    tp2_price = signal.get("tp2_price")
    
    if tp1_price and tp2_price:
        logging.info(
            f"🎯 触发交易信号: {signal['signal']} {signal['side']} @ {signal['price']:.2f}, "
            f"止损={signal['stop_loss']:.2f}, TP1={tp1_price:.2f}(50%), TP2={tp2_price:.2f}(50%), "
            f"市场模式={state_display}"
        )
        print(
            f"🎯 触发信号: {signal['signal']} {signal['side']} @ {signal['price']:.2f}, "
            f"止损={signal['stop_loss']:.2f}, TP1={tp1_price:.2f}(50%), TP2={tp2_price:.2f}(50%)"
        )
    else:
        logging.info(
            f"🎯 触发交易信号: {signal['signal']} {signal['side']} @ {signal['price']:.2f}, "
            f"止损={signal['stop_loss']:.2f}, 止盈={signal['take_profit']:.2f}, "
            f"盈亏比=1:{signal['risk_reward_ratio']:.1f}, 市场模式={state_display}"
        )
        print(
            f"🎯 触发信号: {signal['signal']} {signal['side']} @ {signal['price']:.2f}, "
            f"止损={signal['stop_loss']:.2f}, 止盈={signal['take_profit']:.2f}"
        )
