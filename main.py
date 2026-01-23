import asyncio
import logging
import os
from typing import Dict, List, Optional
import json

import pandas as pd
from binance import AsyncClient, BinanceSocketManager
from binance.exceptions import ReadLoopClosed
import redis.asyncio as aioredis

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
    计算下单数量
    
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


async def _load_historical_klines(
    client: AsyncClient, history: List[Dict], limit: int = 200
) -> None:
    """加载历史K线数据到history列表"""
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
                    "open": float(kline[1]),
                    "high": float(kline[2]),
                    "low": float(kline[3]),
                    "close": float(kline[4]),
                }
            )
        logging.info(f"历史数据已加载到内存，共 {len(history)} 根K线")
    except Exception as e:
        logging.error(f"下载历史K线数据失败: {e}", exc_info=True)
        if len(history) == 0:
            logging.warning("历史数据为空，需要等待K线数据积累")


async def _fill_missing_klines(
    client: AsyncClient, history: List[Dict], last_timestamp: Optional[int] = None
) -> None:
    """补全缺失的K线数据（重连后使用）"""
    try:
        if len(history) == 0:
            # 如果没有历史数据，直接加载
            await _load_historical_klines(client, history)
            return

        # 获取最后一根K线的时间戳（如果提供）
        if last_timestamp is None:
            # 如果没有提供，尝试从历史数据估算
            # 5分钟K线，估算缺失的数量（最多补100根）
            limit = min(100, 500 - len(history))
        else:
            # 根据时间戳计算需要补多少根
            # 简化处理：补最近100根
            limit = 100

        logging.info(f"正在补全缺失的K线数据（最多{limit}根）...")
        missing_klines = await client.get_historical_klines(
            symbol=SYMBOL,
            interval=INTERVAL,
            limit=limit,
        )

        if not missing_klines:
            return

        # 获取现有历史数据的最后一根K线时间戳
        existing_last_close = history[-1]["close"] if history else None

        # 将新数据转换为统一格式
        new_klines = []
        for kline in missing_klines:
            kline_data = {
                "open": float(kline[1]),
                "high": float(kline[2]),
                "low": float(kline[3]),
                "close": float(kline[4]),
            }
            new_klines.append(kline_data)

        # 去重：如果新数据的最后一根与现有数据的最后一根相同，跳过
        if existing_last_close is not None and new_klines:
            if abs(new_klines[-1]["close"] - existing_last_close) < 0.01:
                # 最后一根相同，移除它
                new_klines.pop()

        # 合并数据，按时间顺序
        if new_klines:
            # 简单合并（实际应该按时间戳排序去重）
            history.extend(new_klines)
            history = history[-500:]  # 保留最近500根
            logging.info(
                f"已补全 {len(new_klines)} 根K线，当前历史数据: {len(history)} 根"
            )
    except Exception as e:
        logging.error(f"补全K线数据失败: {e}", exc_info=True)


async def orderbook_worker(symbol: str = SYMBOL) -> None:
    """
    订单簿深度监控工作线程
    
    功能：
    1. 订阅 Binance WebSocket depth20 数据流（20档深度，更难被操纵）
    2. 实时计算 OBI（Order Book Imbalance）
    3. 将结果存入 Redis，10秒过期
    
    OBI 计算公式：
    OBI = (sum(bids_qty) - sum(asks_qty)) / (sum(bids_qty) + sum(asks_qty))
    
    OBI 解读：
    - OBI > 0.3: 买盘占优，强势
    - OBI < -0.3: 卖盘占优，弱势
    - -0.3 <= OBI <= 0.3: 均衡
    """
    redis_client: Optional[aioredis.Redis] = None
    client: Optional[AsyncClient] = None
    reconnect_attempt = 0
    max_reconnect_attempts = 10
    base_delay = 1
    
    while reconnect_attempt < max_reconnect_attempts:
        try:
            logging.info(
                f"正在连接 Redis 和 Binance WebSocket (订单簿深度)..."
                + (
                    f" (重连尝试 {reconnect_attempt + 1}/{max_reconnect_attempts})"
                    if reconnect_attempt > 0
                    else ""
                )
            )
            
            # 连接 Redis
            try:
                redis_client = await aioredis.from_url(
                    REDIS_URL,
                    encoding="utf-8",
                    decode_responses=True,
                    socket_connect_timeout=5,
                )
                # 测试连接
                await redis_client.ping()
                logging.info(f"✅ Redis 连接成功: {REDIS_URL.split('@')[-1] if '@' in REDIS_URL else 'localhost'}")
            except Exception as e:
                logging.error(f"❌ Redis 连接失败: {e}")
                logging.warning("订单簿深度监控将被禁用（不影响主策略）")
                # Redis 连接失败不影响主系统，直接返回
                return
            
            # 创建 Binance 客户端
            try:
                if client is not None:
                    try:
                        await client.close_connection()
                    except:
                        pass
                client = await AsyncClient.create()
                logging.info("✅ Binance WebSocket 客户端创建成功")
            except Exception as e:
                logging.error(f"❌ Binance 客户端创建失败: {e}")
                raise
            
            # 创建 WebSocket 管理器
            bsm = BinanceSocketManager(client)
            
            # 订阅 depth20 数据流（20档深度，更难被操纵）
            depth_socket = bsm.depth_socket(symbol, depth=BinanceSocketManager.WEBSOCKET_DEPTH_20)
            
            # OBI 历史记录（用于计算滑动平均和变化率）
            obi_history: List[float] = []
            OBI_HISTORY_SIZE = 30  # 保留最近30个OBI值（约30秒）
            
            async with depth_socket as stream:
                logging.info(f"🔄 订单簿深度监控已启动: {symbol} (depth20, 增强OBI分析)")
                reconnect_attempt = 0  # 重置重连计数
                
                while True:
                    try:
                        msg = await asyncio.wait_for(stream.recv(), timeout=30.0)
                        
                        if msg is None:
                            logging.warning("订单簿数据流返回 None，可能连接断开")
                            break
                        
                        # 解析订单簿数据
                        if "bids" not in msg or "asks" not in msg:
                            continue
                        
                        # 计算买卖盘总量
                        bids = msg["bids"]  # [[price, qty], ...]
                        asks = msg["asks"]  # [[price, qty], ...]
                        
                        total_bid_qty = sum(float(bid[1]) for bid in bids)
                        total_ask_qty = sum(float(ask[1]) for ask in asks)
                        
                        # 计算 OBI
                        total_qty = total_bid_qty + total_ask_qty
                        if total_qty > 0:
                            obi = (total_bid_qty - total_ask_qty) / total_qty
                        else:
                            obi = 0.0
                        
                        # 更新 OBI 历史记录
                        obi_history.append(obi)
                        if len(obi_history) > OBI_HISTORY_SIZE:
                            obi_history.pop(0)
                        
                        # 计算增强 OBI 指标
                        obi_avg = sum(obi_history) / len(obi_history) if obi_history else obi
                        
                        # 计算 OBI 变化率（Delta OBI）：最近10个 vs 前10个
                        obi_delta = 0.0
                        if len(obi_history) >= 20:
                            recent_avg = sum(obi_history[-10:]) / 10
                            older_avg = sum(obi_history[-20:-10]) / 10
                            obi_delta = recent_avg - older_avg
                        elif len(obi_history) >= 5:
                            # 数据不足时用简化计算
                            obi_delta = obi - obi_history[0]
                        
                        # 计算 OBI 趋势方向
                        obi_trend = "neutral"
                        if obi_delta > 0.05:
                            obi_trend = "bullish"  # 买盘增强
                        elif obi_delta < -0.05:
                            obi_trend = "bearish"  # 卖盘增强
                        
                        # 存入 Redis，10秒过期（增强版数据）
                        redis_key = f"cache:obi:{symbol}"
                        await redis_client.setex(
                            redis_key,
                            10,  # 10秒过期
                            json.dumps({
                                "obi": round(obi, 4),           # 瞬时OBI
                                "obi_avg": round(obi_avg, 4),   # 滑动平均OBI
                                "obi_delta": round(obi_delta, 4),  # OBI变化率
                                "obi_trend": obi_trend,         # OBI趋势方向
                                "bid_qty": round(total_bid_qty, 4),
                                "ask_qty": round(total_ask_qty, 4),
                                "timestamp": msg.get("E", 0),
                            })
                        )
                        
                        # 定期日志（每50次更新记录一次）
                        if int(msg.get("E", 0)) % 50000 < 1000:  # 约每50秒
                            status = "买盘占优" if obi_avg > 0.3 else "卖盘占优" if obi_avg < -0.3 else "均衡"
                            logging.debug(
                                f"📊 OBI更新: 瞬时={obi:.4f}, 平均={obi_avg:.4f}, Delta={obi_delta:.4f} ({obi_trend}), "
                                f"买盘={total_bid_qty:.2f}, 卖盘={total_ask_qty:.2f}"
                            )
                    
                    except ReadLoopClosed:
                        # WebSocket 读取循环已关闭，需要重连
                        logging.warning("WebSocket 读取循环已关闭，准备重连...")
                        break  # 退出内层循环，触发外层重连逻辑
                    except asyncio.TimeoutError:
                        logging.warning("订单簿数据流超时，尝试重连...")
                        break
                    except Exception as e:
                        # 其他异常，记录但继续尝试（可能是临时错误）
                        logging.error(f"处理订单簿数据失败: {e}", exc_info=True)
                        await asyncio.sleep(1)
        
        except ReadLoopClosed:
            reconnect_attempt += 1
            delay = min(base_delay * (2 ** reconnect_attempt), 60)
            logging.warning(
                f"订单簿 WebSocket 读取循环已关闭，"
                f"{delay}秒后重连 ({reconnect_attempt}/{max_reconnect_attempts})"
            )
            await asyncio.sleep(delay)
        except ConnectionClosed as e:
            reconnect_attempt += 1
            delay = min(base_delay * (2 ** reconnect_attempt), 60)
            logging.warning(
                f"订单簿 WebSocket 连接关闭: {e}，"
                f"{delay}秒后重连 ({reconnect_attempt}/{max_reconnect_attempts})"
            )
            await asyncio.sleep(delay)
        
        except Exception as e:
            reconnect_attempt += 1
            delay = min(base_delay * (2 ** reconnect_attempt), 60)
            logging.error(
                f"订单簿监控异常: {e}，"
                f"{delay}秒后重连 ({reconnect_attempt}/{max_reconnect_attempts})",
                exc_info=True
            )
            await asyncio.sleep(delay)
        
        finally:
            # 清理资源
            if client is not None:
                try:
                    await client.close_connection()
                except:
                    pass
            if redis_client is not None:
                try:
                    await redis_client.aclose()
                except:
                    pass
    
    logging.error(f"订单簿监控达到最大重连次数 ({max_reconnect_attempts})，已停止")


async def kline_producer(
    user_queues: List[asyncio.Queue],
    strategy: AlBrooksStrategy,
    trade_logger: TradeLogger,
) -> None:
    """订阅 K 线，生成策略信号并分发给所有用户队列，同时检查止损止盈。
    支持自动重连和指数退避机制。
    """
    history: List[Dict] = []
    kline_count = 0
    reconnect_attempt = 0
    max_reconnect_attempts = 10  # 最大重连次数
    base_delay = 1  # 基础延迟（秒）
    client: Optional[AsyncClient] = None  # 在外部定义，避免未绑定错误

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
                await _load_historical_klines(client, history)
            else:
                # 重连后，补全缺失的数据
                await _fill_missing_klines(client, history)

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
                                    if trade_logger.positions[user_name] is not None:
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

                            if not k.get("x"):  # 只处理已收盘的 K 线
                                continue

                            # 已收盘的 K 线
                            kline_count += 1
                            logging.info(
                                f"📊 K线收盘 #{kline_count}: O={float(k['o']):.2f} H={float(k['h']):.2f} L={float(k['l']):.2f} C={float(k['c']):.2f}"
                            )

                            # 提取 OHLC
                            kline_data = {
                                "open": float(k["o"]),
                                "high": float(k["h"]),
                                "low": float(k["l"]),
                                "close": float(k["c"]),
                            }
                            history.append(kline_data)
                            history = history[-500:]  # 保留最近 500 根

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
    user: TradingUser, queue: asyncio.Queue, trade_logger: TradeLogger
) -> None:
    """消费信号并为该用户下单（观察模式或实际下单）。"""
    logging.info(f"用户工作线程 [{user.name}] 已启动")

    if not OBSERVE_MODE:
        logging.info(f"正在为用户 [{user.name}] 连接 Binance API...")
        await user.connect()
        logging.info(f"用户 [{user.name}] 已连接 Binance API")

    signal_count = 0
    while True:
        try:
            signal: Dict = await queue.get()
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
            order_qty = calculate_order_quantity(signal["price"])
            
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
                # 实际下单模式
                order_params = {
                    "symbol": SYMBOL,
                    "side": signal["side"].upper(),
                    "type": "MARKET",
                    "quantity": order_qty,
                }
                try:
                    logging.info(f"[{user.name}] 正在执行订单: {order_params}")
                    await user.create_order(**order_params)
                    # 同时记录到交易日志（包含信号强度）
                    trade = trade_logger.open_position(
                        user=user.name,
                        signal=signal["signal"],
                        side=signal["side"],
                        entry_price=signal["price"],
                        quantity=order_qty,
                        stop_loss=signal["stop_loss"],
                        take_profit=signal["take_profit"],
                        signal_strength=signal_strength,
                    )
                    logging.info(
                        f"[{user.name}] ✅ 订单执行成功: {signal['signal']} {signal['side']} @ {signal['price']:.2f}, "
                        f"数量={order_qty:.4f} BTC"
                    )
                    print(
                        f"[{user.name}] ✅ 已执行 {signal['signal']} 信号，方向={signal['side']}, "
                        f"价格={signal['price']:.2f}, 数量={order_qty:.4f} BTC, "
                        f"止损={signal['stop_loss']:.2f}, 止盈={signal['take_profit']:.2f}"
                    )
                except Exception as exc:
                    logging.exception(f"[{user.name}] ❌ 下单失败: {exc}")

            queue.task_done()
        except asyncio.CancelledError:
            logging.info(f"用户工作线程 [{user.name}] 已取消")
            break
        except Exception as e:
            logging.error(f"用户工作线程 [{user.name}] 出错: {e}", exc_info=True)
            queue.task_done()


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

    queues = [asyncio.Queue() for _ in users]
    
    # 初始化策略（异步版本）
    strategy = AlBrooksStrategy(redis_url=REDIS_URL)
    # 异步连接 Redis
    redis_connected = await strategy.connect_redis()
    logging.info(f"策略已初始化: EMA周期={strategy.ema_period}, Redis OBI过滤={'启用' if redis_connected else '禁用'}")

    trade_logger = TradeLogger()
    logging.info(f"交易日志器已初始化")

    logging.info("正在启动所有任务...")
    tasks = [
        kline_producer(queues, strategy, trade_logger),
        orderbook_worker(SYMBOL),  # 订单簿深度监控（OBI）
        *[user_worker(user, q, trade_logger) for user, q in zip(users, queues)],
        print_stats_periodically(trade_logger, users),
    ]
    logging.info(f"已创建 {len(tasks)} 个任务（含订单簿深度监控）")

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
