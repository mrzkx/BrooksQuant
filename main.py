import asyncio
import logging
import os
from typing import Dict, List, Optional

import pandas as pd
from binance import AsyncClient, BinanceSocketManager
from binance.exceptions import ReadLoopClosed

from config import (
    load_user_credentials, 
    REDIS_URL,
    OBSERVE_BALANCE,
    POSITION_SIZE_PERCENT,
    LEVERAGE,
    SYMBOL as CONFIG_SYMBOL,
    KLINE_INTERVAL,
)
from strategy import AlBrooksStrategy
from trade_logger import TradeLogger
from user_manager import TradingUser

# 动态订单流模块
from delta_flow import aggtrade_worker

# 尝试导入 websockets 异常（如果可用）
try:
    from websockets.exceptions import ConnectionClosed
except ImportError:
    # 如果 websockets 未安装，创建一个占位类
    ConnectionClosed = Exception  # type: ignore


# 交易参数（从 config.py 读取）
SYMBOL = CONFIG_SYMBOL
INTERVAL = AsyncClient.KLINE_INTERVAL_5MINUTE

# 观察模式：设置为 True 时只模拟交易，不实际下单
OBSERVE_MODE = os.getenv("OBSERVE_MODE", "true").lower() == "true"


def calculate_order_quantity(current_price: float) -> float:
    """
    计算下单数量（仅用于观察模式）
    
    ⚠️ 注意：此函数仅用于观察模式，使用配置文件中的 OBSERVE_BALANCE
    实盘模式下使用 TradingUser.calculate_order_quantity()，它会：
    1. 从 Binance API 获取真实余额
    2. 根据余额动态计算仓位比例：
       - 余额 <= 1000 USDT: 100% 仓位（全仓）
       - 余额 > 1000 USDT: 20% 仓位
    
    公式: 下单数量 = (总资金 × 仓位百分比 × 杠杆) / 当前价格
    
    示例（默认参数）:
    - 总资金: 10000 USDT
    - 仓位: 20%
    - 杠杆: 20x
    - 价格: 90000 USDT
    - 数量: (10000 × 0.2 × 20) / 90000 = 0.444 BTC
    
    返回: BTC 数量（保留3位小数）
    """
    if current_price <= 0:
        return 0.001  # 默认最小值
    
    # 开仓金额 = 总资金 × 仓位百分比
    position_value = OBSERVE_BALANCE * (POSITION_SIZE_PERCENT / 100)
    
    # 实际购买力 = 开仓金额 × 杠杆
    buying_power = position_value * LEVERAGE
    
    # 下单数量 = 购买力 / 当前价格
    quantity = buying_power / current_price
    
    # 保留3位小数（Binance BTC 最小精度）
    quantity = round(quantity, 3)
    
    # 确保不低于最小交易量
    return max(quantity, 0.001)


# K线周期对应的毫秒数
KLINE_INTERVAL_MS = {
    "1m": 60 * 1000,
    "3m": 3 * 60 * 1000,
    "5m": 5 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "30m": 30 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "2h": 2 * 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}


async def _load_historical_klines(
    client: AsyncClient, history: List[Dict], limit: int = 200
) -> Optional[int]:
    """
    加载历史K线数据到history列表
    
    返回: 最后一根K线的开盘时间戳（毫秒），用于后续补全
    """
    last_timestamp = None
    try:
        logging.info(f"正在下载历史K线数据（{SYMBOL} {INTERVAL}，{limit}根）...")
        historical_klines = await client.get_historical_klines(
            symbol=SYMBOL,
            interval=INTERVAL,
            limit=limit,
        )
        logging.info(f"成功下载 {len(historical_klines)} 根历史K线数据")

        # 清空并重新填充历史数据
        history.clear()
        for kline in historical_klines:
            history.append(
                {
                    "timestamp": int(kline[0]),  # K线开盘时间戳（毫秒）
                    "open": float(kline[1]),
                    "high": float(kline[2]),
                    "low": float(kline[3]),
                    "close": float(kline[4]),
                }
            )
        
        if history:
            last_timestamp = history[-1]["timestamp"]
            
        logging.info(f"历史数据已加载到内存，共 {len(history)} 根K线")
    except Exception as e:
        logging.error(f"下载历史K线数据失败: {e}", exc_info=True)
        if len(history) == 0:
            logging.warning("历史数据为空，需要等待K线数据积累")
    
    return last_timestamp


async def _fill_missing_klines(
    client: AsyncClient, history: List[Dict], last_timestamp: Optional[int] = None
) -> Optional[int]:
    """
    补全缺失的K线数据（重连后使用）
    
    基于时间戳精确补全，避免重复或遗漏：
    1. 根据 last_timestamp 和当前时间计算缺失的 K 线数量
    2. 使用 start_time 参数精确获取缺失的 K 线
    3. 按时间戳去重合并
    
    返回: 补全后最后一根K线的时间戳
    """
    import time
    
    try:
        if len(history) == 0:
            # 如果没有历史数据，直接加载
            return await _load_historical_klines(client, history)

        # 获取 K 线周期的毫秒数
        interval_ms = KLINE_INTERVAL_MS.get(KLINE_INTERVAL, 5 * 60 * 1000)  # 默认 5 分钟
        
        # 获取历史数据中最后一根 K 线的时间戳
        if last_timestamp is None:
            last_timestamp = history[-1].get("timestamp")
        
        if last_timestamp is None:
            # 没有时间戳信息，回退到简单补全
            logging.warning("历史数据无时间戳，使用简单补全模式")
            limit = min(100, 500 - len(history))
            missing_klines = await client.get_historical_klines(
                symbol=SYMBOL,
                interval=INTERVAL,
                limit=limit,
            )
        else:
            # 基于时间戳精确计算缺失的 K 线数量
            current_time_ms = int(time.time() * 1000)
            time_gap_ms = current_time_ms - last_timestamp
            missing_count = time_gap_ms // interval_ms
            
            if missing_count <= 0:
                logging.info("没有缺失的K线数据")
                return last_timestamp
            
            # 限制最大补全数量（避免一次请求过多）
            missing_count = min(missing_count + 1, 200)  # +1 确保包含边界
            
            logging.info(
                f"正在补全缺失的K线数据（从 {last_timestamp} 开始，预计 {missing_count} 根）..."
            )
            
            # 使用 start_time 参数精确获取缺失的 K 线
            missing_klines = await client.get_historical_klines(
                symbol=SYMBOL,
                interval=INTERVAL,
                start_str=str(last_timestamp),  # 从断开时的最后一根开始
                limit=missing_count,
            )

        if not missing_klines:
            logging.info("没有新的K线数据需要补全")
            return last_timestamp

        # 构建时间戳到K线的映射（用于去重）
        existing_timestamps = {kline.get("timestamp") for kline in history if kline.get("timestamp")}
        
        # 将新数据转换为统一格式，并按时间戳去重
        new_klines = []
        for kline in missing_klines:
            kline_timestamp = int(kline[0])
            
            # 跳过已存在的 K 线
            if kline_timestamp in existing_timestamps:
                continue
            
            kline_data = {
                "timestamp": kline_timestamp,
                "open": float(kline[1]),
                "high": float(kline[2]),
                "low": float(kline[3]),
                "close": float(kline[4]),
            }
            new_klines.append(kline_data)
            existing_timestamps.add(kline_timestamp)

        # 合并并按时间戳排序
        if new_klines:
            history.extend(new_klines)
            # 按时间戳排序
            history.sort(key=lambda x: x.get("timestamp", 0))
            # 保留最近 500 根
            while len(history) > 500:
                history.pop(0)
            
            new_last_timestamp = history[-1].get("timestamp") if history else None
            logging.info(
                f"✅ 已补全 {len(new_klines)} 根K线，当前历史数据: {len(history)} 根"
            )
            return new_last_timestamp
        else:
            logging.info("所有K线数据已是最新")
            return history[-1].get("timestamp") if history else None
            
    except Exception as e:
        logging.error(f"补全K线数据失败: {e}", exc_info=True)
        return last_timestamp


async def kline_producer(
    user_queues: List[asyncio.Queue],
    close_queues: Dict[str, asyncio.Queue],  # 平仓队列: {user_name: queue}
    strategy: AlBrooksStrategy,
    trade_logger: TradeLogger,
) -> None:
    """订阅 K 线，生成策略信号并分发给所有用户队列，同时检查止损止盈。
    支持自动重连和指数退避机制，基于时间戳精确补全缺失的 K 线。
    """
    history: List[Dict] = []
    kline_count = 0
    reconnect_attempt = 0
    max_reconnect_attempts = 10  # 最大重连次数
    base_delay = 1  # 基础延迟（秒）
    client: Optional[AsyncClient] = None  # 在外部定义，避免未绑定错误
    last_kline_timestamp: Optional[int] = None  # 跟踪最后一根 K 线的时间戳

    while reconnect_attempt < max_reconnect_attempts:
        try:
            logging.info(
                f"正在连接 Binance API，订阅 {SYMBOL} {INTERVAL} K线数据..."
                + (
                    f" (重连尝试 {reconnect_attempt + 1}/{max_reconnect_attempts})"
                    if reconnect_attempt > 0
                    else ""
                )
            )

            # 创建或重新创建客户端
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
                # 首次连接，加载历史数据
                last_kline_timestamp = await _load_historical_klines(client, history)
            else:
                # 重连后，基于时间戳精确补全缺失的数据
                logging.info(f"重连后补全数据，上次最后K线时间戳: {last_kline_timestamp}")
                last_kline_timestamp = await _fill_missing_klines(client, history, last_kline_timestamp)

            # 如果有足够的历史数据，进行一次信号扫描
            if len(history) >= 50:
                df = pd.DataFrame(history)
                signals_df = await strategy.generate_signals(df)
                last = signals_df.iloc[-1]
                market_state = last.get("market_state", "Unknown")
                logging.info(f"市场状态扫描完成，当前市场模式: {market_state}")
                if last["signal"]:
                    logging.info(
                        f"⚠️ 历史数据中发现信号: {last['signal']} {last['side']} @ {last['close']:.2f}"
                    )

            # 创建WebSocket流
            bm = BinanceSocketManager(client)
            kline_stream = bm.kline_socket(symbol=SYMBOL, interval=INTERVAL)
            logging.info(f"K线 WebSocket 流已创建: {SYMBOL} {INTERVAL}")

            # 重置重连计数（连接成功后）
            reconnect_attempt = 0
            kline_count = len(history)  # 从历史数据数量开始计数

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

                            # 获取实时价格（使用最新价格，不等待收盘）
                            current_price = float(k.get("c", 0))
                            if current_price <= 0:
                                current_price = float(
                                    k.get("l", 0)
                                )  # 如果没有收盘价，使用最新价

                            # 实时检查止损止盈（使用当前价格）
                            if current_price > 0:
                                for user_name in list(trade_logger.positions.keys()):
                                    trade = trade_logger.positions.get(user_name)
                                    if trade is not None:
                                        closed_trade = (
                                            trade_logger.check_stop_loss_take_profit(
                                                user_name, current_price
                                            )
                                        )
                                        if closed_trade:
                                            logging.info(
                                                f"[{user_name}] {closed_trade.exit_reason}: "
                                                f"价格={current_price:.2f}, 盈亏={closed_trade.pnl:.4f} USDT ({closed_trade.pnl_percent:.2f}%)"
                                            )
                                            print(
                                                f"[{user_name}] {closed_trade.exit_reason}: "
                                                f"价格={current_price:.2f}, 盈亏={closed_trade.pnl:.4f} USDT ({closed_trade.pnl_percent:.2f}%)"
                                            )
                                            
                                            # 实盘模式：发送平仓请求到队列
                                            if not OBSERVE_MODE and user_name in close_queues:
                                                close_request = {
                                                    "action": "close",
                                                    "side": closed_trade.side,
                                                    "quantity": float(closed_trade.remaining_quantity or closed_trade.quantity),
                                                    "exit_price": float(closed_trade.exit_price),
                                                    "exit_reason": closed_trade.exit_reason,
                                                }
                                                await close_queues[user_name].put(close_request)
                                                logging.info(f"[{user_name}] 已发送平仓请求到队列: {close_request}")

                            if not k.get("x"):  # 只处理已收盘的 K 线
                                continue

                            # 已收盘的 K 线
                            kline_count += 1
                            kline_open_time = int(k.get("t", 0))  # K线开盘时间戳
                            logging.info(
                                f"📊 K线收盘 #{kline_count}: O={float(k['o']):.2f} H={float(k['h']):.2f} L={float(k['l']):.2f} C={float(k['c']):.2f}"
                            )

                            # 提取 OHLC（包含时间戳，用于重连后补全）
                            kline_data = {
                                "timestamp": kline_open_time,
                                "open": float(k["o"]),
                                "high": float(k["h"]),
                                "low": float(k["l"]),
                                "close": float(k["c"]),
                            }
                            
                            # 更新最后 K 线时间戳（用于重连后精确补全）
                            last_kline_timestamp = kline_open_time
                            
                            history.append(kline_data)
                            # 保留最近 500 根
                            while len(history) > 500:
                                history.pop(0)

                            # 只有当有足够的历史数据时才生成信号
                            if len(history) < 50:
                                continue

                            df = pd.DataFrame(history)
                            signals_df = await strategy.generate_signals(df)
                            last = signals_df.iloc[-1]

                            # 获取当前市场状态和技术指标
                            market_state = last.get("market_state", "Unknown")
                            atr_value = last.get("atr", None)
                            
                            # 计算K线实体比例（用于调试）
                            kline_range = float(k["h"]) - float(k["l"])
                            kline_body = abs(float(k["c"]) - float(k["o"]))
                            body_ratio = kline_body / kline_range if kline_range > 0 else 0

                            # 每根K线都记录市场状态和关键指标（用于调试）
                            if kline_count % 10 == 0:
                                atr_str = f"{atr_value:.2f}" if atr_value else "N/A"
                                climax_threshold = atr_value * 2.5 if atr_value else 0
                                is_potential_climax = kline_range > climax_threshold if atr_value else False
                                logging.info(
                                    f"📈 状态: {market_state}, ATR={atr_str}, "
                                    f"K线范围={kline_range:.2f}, 实体比={body_ratio:.1%}, "
                                    f"潜在Climax={'是' if is_potential_climax else '否'}"
                                )

                            # 每根K线递增计数器（用于冷却期管理）
                            trade_logger.increment_kline()
                            
                            # 调试日志：详细记录信号检测条件
                            if kline_count % 5 == 0 or last["signal"]:  # 每5根K线或有信号时输出
                                # 计算最近10根K线的平均实体
                                if len(history) >= 10:
                                    recent_bodies = [abs(bar["close"] - bar["open"]) for bar in history[-10:]]
                                    avg_body = sum(recent_bodies) / len(recent_bodies)
                                    body_multiple = kline_body / avg_body if avg_body > 0 else 0
                                    
                                    logging.debug(
                                        f"🔍 信号检测条件: 实体={kline_body:.2f}, 平均实体={avg_body:.2f}, "
                                        f"倍数={body_multiple:.2f}x (需要>1.8x), 实体比={body_ratio:.1%} (需要>80%)"
                                    )
                            
                            if last["signal"]:
                                entry_price = last["close"]
                                stop_loss = last["stop_loss"]
                                risk_reward_ratio = last.get(
                                    "risk_reward_ratio", 1.0
                                )  # 默认1:1
                                base_height = last.get("base_height", None)  # Measured Move基准高度
                                
                                # 获取分批止盈目标位
                                tp1_price = last.get("tp1_price", None)  # 第一目标位（1R，50%仓位）
                                tp2_price = last.get("tp2_price", None)  # 第二目标位（2R+，剩余50%仓位）
                                
                                # 获取市场上下文
                                tight_channel_score = last.get("tight_channel_score", 0.0)  # 紧凑通道评分

                                # 计算信号强度（当前K线的实体大小）
                                current_bar = df.iloc[-1]
                                signal_strength = abs(current_bar["close"] - current_bar["open"])

                                # 如果有TP1/TP2，使用分批止盈；否则使用传统方式
                                if tp1_price and tp2_price:
                                    # 分批止盈模式
                                    take_profit = tp2_price  # 主要显示TP2作为最终目标
                                else:
                                    # 传统止盈模式（向后兼容）
                                    # 计算止损距离
                                    if last["side"] == "buy":
                                        stop_distance = entry_price - stop_loss
                                    else:  # sell
                                        stop_distance = stop_loss - entry_price
                                    
                                    # 确保止盈至少是止损的2倍（最小盈亏比 2:1）
                                    min_tp_distance = stop_distance * 2.0
                                    
                                    # 传统方式的止盈距离
                                    traditional_tp_distance = stop_distance * risk_reward_ratio
                                    
                                    # Measured Move 方式
                                    if base_height and base_height > 0:
                                        # 混合模式：取 Measured Move、传统方式、最小止盈距离的最大值
                                        actual_tp_distance = max(base_height, traditional_tp_distance, min_tp_distance)
                                        
                                        if last["side"] == "buy":
                                            take_profit = entry_price + actual_tp_distance
                                        else:  # sell
                                            take_profit = entry_price - actual_tp_distance
                                    else:
                                        # 回退方案：确保至少2倍止损距离
                                        actual_tp_distance = max(traditional_tp_distance, min_tp_distance)
                                        
                                        if last["side"] == "buy":
                                            take_profit = entry_price + actual_tp_distance
                                        else:  # sell
                                            take_profit = entry_price - actual_tp_distance

                                signal = {
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
                                }

                                # 市场状态映射到中文显示
                                state_map = {
                                    "Breakout": "突破模式(Spike)",
                                    "Channel": "通道模式(Channel)",
                                    "TradingRange": "区间模式(Range)",
                                    "Unknown": "未知状态",
                                }
                                state_display = state_map.get(
                                    market_state, market_state
                                )

                                # 根据是否有TP1/TP2选择不同的日志格式
                                if tp1_price and tp2_price:
                                    logging.info(
                                        "🎯 触发交易信号: %s %s @ %.2f, 止损=%.2f, TP1=%.2f(50%%), TP2=%.2f(50%%), 市场模式=%s",
                                        signal["signal"],
                                        signal["side"],
                                        signal["price"],
                                        signal["stop_loss"],
                                        tp1_price,
                                        tp2_price,
                                        state_display,
                                    )
                                    print(
                                        f"🎯 触发信号: {signal['signal']} {signal['side']} @ {signal['price']:.2f}, "
                                        f"止损={signal['stop_loss']:.2f}, TP1={tp1_price:.2f}(50%), TP2={tp2_price:.2f}(50%), "
                                        f"市场模式={state_display}"
                                    )
                                else:
                                    logging.info(
                                        "🎯 触发交易信号: %s %s @ %.2f, 止损=%.2f, 止盈=%.2f, 盈亏比=1:%.1f, 市场模式=%s",
                                        signal["signal"],
                                        signal["side"],
                                        signal["price"],
                                        signal["stop_loss"],
                                        signal["take_profit"],
                                        risk_reward_ratio,
                                        state_display,
                                    )
                                    print(
                                        f"🎯 触发信号: {signal['signal']} {signal['side']} @ {signal['price']:.2f}, "
                                        f"止损={signal['stop_loss']:.2f}, 止盈={signal['take_profit']:.2f}, "
                                        f"盈亏比=1:{risk_reward_ratio:.1f}, 市场模式={state_display}"
                                    )
                                # 广播给所有用户
                                for q in user_queues:
                                    await q.put(signal)
                        except asyncio.CancelledError:
                            logging.info("K线生产者任务已取消")
                            raise  # 重新抛出，让外层处理
                        except ReadLoopClosed:
                            # WebSocket 读取循环已关闭，需要重连
                            logging.warning("WebSocket 读取循环已关闭，准备重连...")
                            raise  # 重新抛出，触发重连
                        except (ConnectionClosed, ConnectionError, OSError) as e:
                            # WebSocket连接断开
                            logging.warning(f"WebSocket 连接断开: {e}")
                            raise  # 重新抛出，触发重连
                        except Exception as e:  # type: ignore
                            # 处理消息时的其他错误，记录但继续（不触发重连）
                            # 注意：连接错误会被重新抛出，由外层处理
                            logging.error(f"处理 K 线消息时出错: {e}", exc_info=True)
                            await asyncio.sleep(1)  # 出错后等待1秒再继续

            except asyncio.CancelledError:
                logging.info("K线生产者任务已取消")
                raise  # 重新抛出，让外层处理
            except ReadLoopClosed:
                # WebSocket 读取循环已关闭，需要重连
                logging.warning("WebSocket 读取循环已关闭，准备重连...")
                reconnect_attempt += 1

                # 关闭旧客户端
                if client is not None:
                    try:
                        await client.close_connection()
                    except:
                        pass

                # 指数退避：延迟时间 = base_delay * (2 ^ reconnect_attempt)
                delay = min(base_delay * (2**reconnect_attempt), 60)  # 最多60秒
                logging.info(f"等待 {delay} 秒后尝试重连...")
                await asyncio.sleep(delay)
                continue  # 继续重连循环
            except (ConnectionClosed, ConnectionError, OSError) as e:
                # WebSocket连接错误，准备重连
                logging.warning(f"WebSocket 连接错误: {e}")
                reconnect_attempt += 1

                # 关闭旧客户端
                if client is not None:
                    try:
                        await client.close_connection()
                    except:
                        pass

                # 指数退避：延迟时间 = base_delay * (2 ^ reconnect_attempt)
                delay = min(base_delay * (2**reconnect_attempt), 60)  # 最多60秒
                logging.info(f"等待 {delay} 秒后尝试重连...")
                await asyncio.sleep(delay)
                continue  # 继续重连循环

        except asyncio.CancelledError:
            logging.info("K线生产者任务已取消")
            break
        except Exception as e:
            logging.error(f"K线生产者发生未预期的错误: {e}", exc_info=True)
            reconnect_attempt += 1

            # 关闭客户端
            if client is not None:
                try:
                    await client.close_connection()
                except:
                    pass

            if reconnect_attempt >= max_reconnect_attempts:
                logging.error(f"达到最大重连次数 ({max_reconnect_attempts})，停止重连")
                break

            # 指数退避
            delay = min(base_delay * (2**reconnect_attempt), 60)
            logging.info(f"等待 {delay} 秒后尝试重连...")
            await asyncio.sleep(delay)

    # 最终清理
    try:
        await client.close_connection()
        logging.info("Binance 客户端连接已关闭")
    except:
        pass


async def user_worker(
    user: TradingUser, 
    signal_queue: asyncio.Queue, 
    close_queue: asyncio.Queue,  # 平仓队列
    trade_logger: TradeLogger
) -> None:
    """消费信号并为该用户下单（观察模式或实际下单）。"""
    logging.info(f"用户工作线程 [{user.name}] 已启动")

    if not OBSERVE_MODE:
        logging.info(f"正在为用户 [{user.name}] 连接 Binance API...")
        await user.connect()
        logging.info(f"用户 [{user.name}] 已连接 Binance API")
        
        # 获取交易规则（stepSize, tickSize）
        try:
            filters = await user.get_symbol_filters(SYMBOL)
            logging.info(
                f"[{user.name}] 获取交易规则: stepSize={filters['stepSize']}, "
                f"minQty={filters['minQty']}, tickSize={filters['tickSize']}"
            )
        except Exception as e:
            logging.warning(f"[{user.name}] 获取交易规则失败: {e}，将使用默认值")
        
        # 设置杠杆（实盘模式下首次设置）
        leverage_ok = await user.set_leverage(SYMBOL, leverage=LEVERAGE)
        if not leverage_ok:
            logging.error(f"[{user.name}] 设置杠杆失败，交易可能使用错误的杠杆倍数！")
        
        # 获取并显示初始余额
        try:
            initial_balance = await user.get_futures_balance()
            position_pct = user.calculate_position_size_percent(initial_balance)
            logging.info(
                f"[{user.name}] 实盘模式: 余额={initial_balance:.2f} USDT, "
                f"仓位比例={position_pct:.0f}%, 杠杆={LEVERAGE}x"
            )
            print(
                f"[{user.name}] 实盘模式: 余额={initial_balance:.2f} USDT, "
                f"仓位比例={position_pct:.0f}% ({'全仓' if position_pct == 100 else '20%仓位'}), "
                f"杠杆={LEVERAGE}x"
            )
        except Exception as e:
            logging.error(f"[{user.name}] 获取初始余额失败: {e}")

    signal_count = 0
    while True:
        try:
            # 检查是否需要挂 TP2 订单（TP1 已触发但 TP2 未挂单）
            if not OBSERVE_MODE and trade_logger.needs_tp2_order(user.name):
                trade = trade_logger.positions.get(user.name)
                if trade:
                    try:
                        tp2_qty = trade.remaining_quantity or (trade.quantity * 0.5)
                        tp2_qty = max(round(float(tp2_qty), 3), 0.001)
                        
                        stop_side = "SELL" if trade.side == "buy" else "BUY"
                        
                        tp2_response = await user.create_take_profit_market_order(
                            symbol=SYMBOL,
                            side=stop_side,
                            quantity=tp2_qty,
                            stop_price=round(float(trade.tp2_price), 2),
                            reduce_only=True,
                        )
                        tp2_order_id = tp2_response.get("orderId")
                        trade_logger.mark_tp2_order_placed(user.name)  # 标记已挂单
                        
                        logging.info(
                            f"[{user.name}] ✅ TP2止盈单已设置: ID={tp2_order_id}, "
                            f"触发价={trade.tp2_price:.2f}, 数量={tp2_qty:.4f} BTC (剩余50%)"
                        )
                        print(
                            f"[{user.name}] ✅ TP2止盈单已设置: 触发价={trade.tp2_price:.2f}, "
                            f"数量={tp2_qty:.4f} BTC"
                        )
                    except Exception as tp2_err:
                        logging.error(f"[{user.name}] ⚠️ TP2止盈单设置失败: {tp2_err}")
            
            # 使用 wait 同时监听两个队列（优先处理平仓请求）
            signal_task = asyncio.create_task(signal_queue.get())
            close_task = asyncio.create_task(close_queue.get())
            
            done, pending = await asyncio.wait(
                [signal_task, close_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            
            # 取消未完成的任务
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            
            # 获取完成的任务结果
            completed_task = done.pop()
            result = completed_task.result()
            
            # 处理平仓请求（优先级高）
            if completed_task == close_task or (isinstance(result, dict) and result.get("action") == "close"):
                if not OBSERVE_MODE:
                    close_request = result
                    try:
                        logging.info(f"[{user.name}] 🔴 执行平仓: {close_request}")
                        
                        close_response = await user.close_position_market(
                            symbol=SYMBOL,
                            side=close_request["side"],
                            quantity=close_request["quantity"],
                        )
                        
                        logging.info(
                            f"[{user.name}] ✅ 平仓成功: {close_request['exit_reason']}, "
                            f"数量={close_request['quantity']:.4f} BTC"
                        )
                        print(
                            f"[{user.name}] ✅ 平仓成功: {close_request['exit_reason']}, "
                            f"数量={close_request['quantity']:.4f} BTC"
                        )
                        
                        # 取消该用户的所有挂单（止损单等）
                        await user.cancel_all_orders(SYMBOL)
                        
                    except Exception as close_err:
                        logging.error(f"[{user.name}] ❌ 平仓失败: {close_err}")
                        print(f"[{user.name}] ❌ 平仓失败: {close_err}")
                continue  # 处理完平仓后继续循环
            
            # 处理信号
            signal: Dict = result
            signal_count += 1
            logging.info(
                f"[{user.name}] 收到信号 #{signal_count}: {signal['signal']} {signal['side']} @ {signal['price']:.2f}"
            )

            # 检查1: 是否在冷却期
            if trade_logger.is_in_cooldown(user.name):
                logging.info(
                    f"⏳ [{user.name}] 在冷却期内，跳过信号: {signal['signal']} {signal['side']}"
                )
                continue
            
            # 检查2: 如果有持仓，检查反手强度
            signal_strength = signal.get("signal_strength", 0.0)
            
            # ⭐ 动态反手阈值：根据市场状态调整
            market_state_str = signal.get("market_state", "")
            if market_state_str in ["Breakout", "StrongTrend"]:
                reversal_threshold = 1.5  # 强趋势中提高门槛，减少反手
            elif market_state_str == "TradingRange":
                reversal_threshold = 1.0  # 震荡市放宽门槛，允许更多反转
            else:
                reversal_threshold = 1.2  # 默认值（Channel 等状态）
            
            if not trade_logger.should_allow_reversal(
                user.name, 
                signal_strength, 
                reversal_threshold=reversal_threshold
            ):
                logging.info(
                    f"❌ [{user.name}] 反手信号强度不足，跳过: {signal['signal']} {signal['side']} "
                    f"(强度={signal_strength:.2f}, 阈值={reversal_threshold:.1f}x, 市场={market_state_str})"
                )
                continue

            # 根据当前价格动态计算下单数量
            if OBSERVE_MODE:
                # 观察模式：使用配置的模拟资金
                order_qty = calculate_order_quantity(signal["price"])
            else:
                # 实盘模式：获取真实余额，动态计算仓位（使用 stepSize 规则）
                try:
                    real_balance = await user.get_futures_balance(force_refresh=True)
                    order_qty = user.calculate_order_quantity(
                        balance=real_balance,
                        current_price=signal["price"],
                        leverage=LEVERAGE,
                        symbol=SYMBOL
                    )
                except Exception as e:
                    logging.error(f"[{user.name}] 获取余额失败，跳过信号: {e}")
                    continue
            
            if OBSERVE_MODE:
                # 观察模式：只记录模拟交易（支持分批止盈）
                tp1_price = signal.get("tp1_price")
                tp2_price = signal.get("tp2_price")
                market_state_val = signal.get("market_state")
                tight_channel_score_val = signal.get("tight_channel_score", 0.0)
                
                trade = trade_logger.open_position(
                    user=user.name,
                    signal=signal["signal"],
                    side=signal["side"],
                    entry_price=signal["price"],
                    quantity=order_qty,
                    stop_loss=signal["stop_loss"],
                    take_profit=signal["take_profit"],
                    signal_strength=signal_strength,
                    tp1_price=tp1_price,
                    tp2_price=tp2_price,
                    market_state=market_state_val,
                    tight_channel_score=tight_channel_score_val,
                )
                
                # 计算持仓价值
                position_value = order_qty * signal["price"]
                
                # 根据是否有TP1/TP2选择不同的日志格式
                if tp1_price and tp2_price:
                    logging.info(
                        f"[{user.name}] ✅ 模拟开仓: {signal['signal']} {signal['side']} @ {signal['price']:.2f}, "
                        f"数量={order_qty:.4f} BTC (≈{position_value:.2f} USDT), "
                        f"止损={signal['stop_loss']:.2f}, TP1={tp1_price:.2f}(50%), TP2={tp2_price:.2f}(50%)"
                    )
                    print(
                        f"[{user.name}] ✅ 模拟开仓: {signal['signal']} {signal['side']} @ {signal['price']:.2f}, "
                        f"数量={order_qty:.4f} BTC (≈{position_value:.2f} USDT), "
                        f"止损={signal['stop_loss']:.2f}, TP1={tp1_price:.2f}(50%), TP2={tp2_price:.2f}(50%)"
                    )
                else:
                    logging.info(
                        f"[{user.name}] ✅ 模拟开仓: {signal['signal']} {signal['side']} @ {signal['price']:.2f}, "
                        f"数量={order_qty:.4f} BTC (≈{position_value:.2f} USDT), "
                        f"止损={signal['stop_loss']:.2f}, 止盈={signal['take_profit']:.2f}"
                    )
                    print(
                        f"[{user.name}] ✅ 模拟开仓: {signal['signal']} {signal['side']} @ {signal['price']:.2f}, "
                        f"数量={order_qty:.4f} BTC (≈{position_value:.2f} USDT), "
                        f"止损={signal['stop_loss']:.2f}, 止盈={signal['take_profit']:.2f}"
                    )
            else:
                # ========== 实盘下单模式（Al Brooks 理念）==========
                # 策略：
                # 1. 突破型信号（Spike/Failed Breakout/Climax）→ 市价入场（快速成交）
                # 2. 回撤型信号（H2/L2/Wedge/Spike_Entry）→ 限价入场（等待更优价位）
                # 3. 止损使用市价单（确保触发时能成交）
                # 4. 止盈不预挂，通过 K 线监控动态退出（Al Brooks 核心理念）
                
                tp1_price = signal.get("tp1_price")
                tp2_price = signal.get("tp2_price")
                market_state_val = signal.get("market_state")
                tight_channel_score_val = signal.get("tight_channel_score", 0.0)
                
                # 计算持仓价值
                position_value = order_qty * signal["price"]
                
                # 确定止损方向（与开仓相反）
                stop_side = "SELL" if signal["side"].lower() == "buy" else "BUY"
                
                # 根据信号类型决定入场方式（Al Brooks 理念）
                signal_type = signal["signal"]
                
                # 突破型信号：需要快速入场，使用市价单
                BREAKOUT_SIGNALS = ["Spike_Buy", "Spike_Sell", 
                                    "Failed_Breakout_Buy", "Failed_Breakout_Sell",
                                    "Climax_Buy", "Climax_Sell"]
                
                # 回撤型信号：可以等待更好价位，使用限价单
                PULLBACK_SIGNALS = ["H2_Buy", "H2_Sell", "L2_Buy", "L2_Sell",
                                    "Wedge_Buy", "Wedge_Sell",
                                    "Spike_Entry_Buy", "Spike_Entry_Sell"]
                
                is_breakout_signal = signal_type in BREAKOUT_SIGNALS
                
                try:
                    if is_breakout_signal:
                        # ===== 突破型信号：市价入场 =====
                        logging.info(
                            f"[{user.name}] 🚀 执行市价入场（突破型）: "
                            f"{signal_type} {signal['side'].upper()} @ 市价, 数量={order_qty:.4f} BTC, "
                            f"持仓价值≈{position_value:.2f} USDT"
                        )
                        
                        entry_response = await user.create_market_order(
                            symbol=SYMBOL,
                            side=signal["side"].upper(),
                            quantity=order_qty,
                            reduce_only=False,
                        )
                        
                        order_id = entry_response.get("orderId")
                        order_status = entry_response.get("status", "FILLED")
                        
                        logging.info(f"[{user.name}] 市价开仓单已成交: ID={order_id}, 状态={order_status}")
                    else:
                        # ===== 回撤型信号：限价入场 =====
                        limit_price = user.calculate_limit_price(
                            current_price=signal["price"],
                            side=signal["side"],
                            slippage_pct=0.05,  # 0.05% 滑点
                            symbol=SYMBOL
                        )
                        
                        logging.info(
                            f"[{user.name}] 🎯 执行限价入场（回撤型）: "
                            f"{signal_type} {signal['side'].upper()} @ {limit_price:.2f}, 数量={order_qty:.4f} BTC, "
                            f"持仓价值≈{position_value:.2f} USDT"
                        )
                        
                        entry_response = await user.create_limit_order(
                            symbol=SYMBOL,
                            side=signal["side"].upper(),
                            quantity=order_qty,
                            price=limit_price,
                            time_in_force="GTC",  # 撤销前有效
                        )
                        
                        order_id = entry_response.get("orderId")
                        order_status = entry_response.get("status", "NEW")
                        
                        logging.info(f"[{user.name}] 限价开仓单已提交: ID={order_id}, 状态={order_status}")
                    
                    # Step 2: 创建止损市价单（Al Brooks：止损必须确定性执行）
                    stop_order_id = None
                    try:
                        stop_response = await user.create_stop_market_order(
                            symbol=SYMBOL,
                            side=stop_side,
                            quantity=order_qty,
                            stop_price=round(signal["stop_loss"], 2),
                            reduce_only=True,
                        )
                        stop_order_id = stop_response.get("orderId")
                        logging.info(f"[{user.name}] ✅ 止损市价单已设置: ID={stop_order_id}, 触发价={signal['stop_loss']:.2f}")
                    except Exception as stop_err:
                        logging.error(f"[{user.name}] ⚠️ 止损单设置失败: {stop_err}")
                        print(f"[{user.name}] ⚠️ 止损单设置失败，请手动设置止损！")
                    
                    # ===== Al Brooks 理念：不预挂止盈单 =====
                    # 止盈通过 K 线监控动态退出：
                    # 1. 检测反转信号 / Climax / 通道触及时退出
                    # 2. 使用追踪止损保护利润
                    # 3. 由 trade_logger.check_stop_loss_take_profit() 实时检测
                    logging.info(
                        f"[{user.name}] 📊 Al Brooks 动态退出模式: "
                        f"TP1={tp1_price:.2f if tp1_price else 0:.2f}, TP2={tp2_price:.2f if tp2_price else 0:.2f}, "
                        f"将通过 K 线监控触发平仓"
                    )
                    
                    # 获取实际成交信息
                    if is_breakout_signal:
                        # 市价单立即成交，取平均成交价
                        actual_price = float(entry_response.get("avgPrice", signal["price"]))
                    else:
                        # 限价单可能未立即成交，使用限价单价格
                        actual_price = float(entry_response.get("price", limit_price))
                    actual_qty = float(entry_response.get("origQty", order_qty))
                    executed_qty = float(entry_response.get("executedQty", 0))
                    
                    # 同时记录到交易日志（包含分批止盈参数）
                    trade = trade_logger.open_position(
                        user=user.name,
                        signal=signal["signal"],
                        side=signal["side"],
                        entry_price=actual_price,
                        quantity=actual_qty,
                        stop_loss=signal["stop_loss"],
                        take_profit=signal["take_profit"],
                        signal_strength=signal_strength,
                        tp1_price=tp1_price,
                        tp2_price=tp2_price,
                        market_state=market_state_val,
                        tight_channel_score=tight_channel_score_val,
                    )
                    
                    # 日志输出
                    status_emoji = "✅" if order_status == "FILLED" else "📝"
                    order_type_text = "市价单" if is_breakout_signal else "限价单"
                    status_text = "已成交" if order_status == "FILLED" else f"挂单中({order_status})"
                    
                    if tp1_price and tp2_price:
                        logging.info(
                            f"[{user.name}] {status_emoji} 实盘{order_type_text}{status_text}: {signal['signal']} {signal['side']} @ {actual_price:.2f}, "
                            f"数量={actual_qty:.4f} BTC, 止损={signal['stop_loss']:.2f}, "
                            f"TP1={tp1_price:.2f}(50%), TP2={tp2_price:.2f}(50%) [K线动态退出]"
                        )
                        print(
                            f"[{user.name}] {status_emoji} 实盘{order_type_text}{status_text}: {signal['signal']} {signal['side']} @ {actual_price:.2f}, "
                            f"数量={actual_qty:.4f} BTC, 止损={signal['stop_loss']:.2f}"
                        )
                    else:
                        logging.info(
                            f"[{user.name}] {status_emoji} 实盘{order_type_text}{status_text}: {signal['signal']} {signal['side']} @ {actual_price:.2f}, "
                            f"数量={actual_qty:.4f} BTC, 止损={signal['stop_loss']:.2f}, 止盈={signal['take_profit']:.2f} [K线动态退出]"
                        )
                        print(
                            f"[{user.name}] {status_emoji} 实盘{order_type_text}{status_text}: {signal['signal']} {signal['side']} @ {actual_price:.2f}, "
                            f"数量={actual_qty:.4f} BTC, 止损={signal['stop_loss']:.2f}, 止盈={signal['take_profit']:.2f}"
                        )
                        
                except Exception as exc:
                    logging.exception(f"[{user.name}] ❌ 实盘下单失败: {exc}")
                    print(f"[{user.name}] ❌ 实盘下单失败: {exc}")

            signal_queue.task_done()
        except asyncio.CancelledError:
            logging.info(f"用户工作线程 [{user.name}] 已取消")
            break
        except Exception as e:
            logging.error(f"用户工作线程 [{user.name}] 出错: {e}", exc_info=True)
            signal_queue.task_done()


async def print_stats_periodically(trade_logger: TradeLogger, users: List[TradingUser]):
    """定期打印交易统计"""
    await asyncio.sleep(60)  # 启动后等待1分钟再开始统计
    while True:
        await asyncio.sleep(300)  # 每5分钟打印一次
        logging.info("=" * 60)
        logging.info("定期交易统计:")
        print("\n" + "=" * 60)
        print("📊 定期交易统计:")
        for user in users:
            stats = trade_logger.get_user_stats(user.name)
            # 检查是否有持仓
            has_position = (
                user.name in trade_logger.positions
                and trade_logger.positions[user.name] is not None
            )
            position_info = ""
            if has_position:
                pos = trade_logger.positions[user.name]
                position_info = (
                    f", 当前持仓: {pos.signal} {pos.side} @ {pos.entry_price:.2f}"
                )

            stats_msg = (
                f"[{user.name}] 总交易: {stats['total_trades']}, "
                f"盈利: {stats['winning_trades']}, 亏损: {stats['losing_trades']}, "
                f"胜率: {stats['win_rate']:.2f}%, 总盈亏: {stats['total_pnl']:.4f} USDT{position_info}"
            )
            logging.info(stats_msg)
            print(stats_msg)
        logging.info("=" * 60)
        print("=" * 60 + "\n")


async def main() -> None:
    logging.info("=" * 60)
    logging.info("BrooksQuant 交易系统启动")
    logging.info("=" * 60)

    credentials = load_user_credentials()
    logging.info(f"已加载 {len(credentials)} 组用户凭据")

    # 观察模式下，如果没有配置凭据，创建一个默认用户
    if OBSERVE_MODE and len(credentials) == 0:
        from config import UserCredentials

        credentials = [UserCredentials(api_key="", api_secret="")]
        logging.info("观察模式：使用默认用户（无需 API 密钥）")

    # 支持单个或多个用户
    if len(credentials) == 0:
        raise RuntimeError(
            "需要在环境变量中配置至少一组用户凭据：USER1_API_KEY/USER1_API_SECRET"
        )

    # 创建用户（支持1个或多个）
    users = [TradingUser(f"User{i+1}", cred) for i, cred in enumerate(credentials)]
    logging.info(f"已创建 {len(users)} 个交易用户: {[u.name for u in users]}")

    if OBSERVE_MODE:
        logging.info("=" * 60)
        logging.info("观察模式已启用 - 将进行模拟交易，不会实际下单")
        logging.info(f"模拟资金: {OBSERVE_BALANCE} USDT, 仓位: {POSITION_SIZE_PERCENT}%, 杠杆: {LEVERAGE}x")
        logging.info("=" * 60)
        print("=" * 60)
        print("观察模式已启用 - 将进行模拟交易，不会实际下单")
        print(f"模拟资金: {OBSERVE_BALANCE} USDT, 仓位: {POSITION_SIZE_PERCENT}%, 杠杆: {LEVERAGE}x")
        print("=" * 60)
    else:
        logging.info("=" * 60)
        logging.info("实际交易模式 - 将进行真实下单")
        logging.info(f"仓位: {POSITION_SIZE_PERCENT}%, 杠杆: {LEVERAGE}x")
        logging.info("=" * 60)
        print("=" * 60)
        print("实际交易模式 - 将进行真实下单")
        print(f"仓位: {POSITION_SIZE_PERCENT}%, 杠杆: {LEVERAGE}x")
        print("=" * 60)

    logging.info(f"交易对: {SYMBOL}, K线周期: {INTERVAL}")

    # 信号队列（每个用户一个）
    signal_queues = [asyncio.Queue() for _ in users]
    
    # 平仓队列（每个用户一个，用于实盘模式下的止盈止损平仓）
    close_queues = {user.name: asyncio.Queue() for user in users}
    
    # 初始化策略（异步版本，Delta 窗口与 K 线周期对齐）
    strategy = AlBrooksStrategy(redis_url=REDIS_URL, kline_interval=KLINE_INTERVAL)
    # 异步连接 Redis（可选，用于 Delta 缓存）
    redis_connected = await strategy.connect_redis()
    logging.info(
        f"策略已初始化: EMA周期={strategy.ema_period}, "
        f"K线周期={KLINE_INTERVAL}, Delta窗口={strategy.delta_analyzer.WINDOW_SECONDS}秒"
    )

    trade_logger = TradeLogger()
    logging.info(f"交易日志器已初始化")

    logging.info("正在启动所有任务...")
    tasks = [
        kline_producer(signal_queues, close_queues, strategy, trade_logger),
        aggtrade_worker(SYMBOL, REDIS_URL, KLINE_INTERVAL),  # 动态订单流监控（Delta窗口与K线周期对齐）
        *[user_worker(user, sq, close_queues[user.name], trade_logger) 
          for user, sq in zip(users, signal_queues)],
        print_stats_periodically(trade_logger, users),
    ]
    logging.info(f"已创建 {len(tasks)} 个任务（含动态订单流监控，Delta窗口={KLINE_INTERVAL}）")

    try:
        logging.info("所有任务已启动，程序运行中...")
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logging.info("任务已被取消")
        pass
    except KeyboardInterrupt:
        logging.info("收到键盘中断信号，正在关闭...")
        print("\n正在关闭...")
        # 打印最终统计
        logging.info("=" * 60)
        logging.info("最终交易统计:")
        print("\n" + "=" * 60)
        print("最终交易统计:")
        for user in users:
            stats = trade_logger.get_user_stats(user.name)
            stats_msg = (
                f"[{user.name}] 总交易: {stats['total_trades']}, "
                f"盈利: {stats['winning_trades']}, 亏损: {stats['losing_trades']}, "
                f"胜率: {stats['win_rate']:.2f}%, 总盈亏: {stats['total_pnl']:.4f} USDT"
            )
            logging.info(stats_msg)
            print(stats_msg)
        print("=" * 60)
    finally:
        # 收尾关闭客户端和 Redis 连接
        logging.info("正在清理资源...")
        await strategy.close_redis()
        if not OBSERVE_MODE:
            await asyncio.gather(
                *(user.close() for user in users), return_exceptions=True
            )
        logging.info("程序已正常退出")


if __name__ == "__main__":
    asyncio.run(main())
