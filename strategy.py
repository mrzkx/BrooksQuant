"""
Al Brooks 价格行为策略 - 核心入口

整合四大阿布价格行为策略（异步版本）：
1. Strong Spike - 强突破直接入场
2. H2/L2 Pullback - 通道回调策略
3. Failed Breakout - 失败突破反转策略
4. Wedge Reversal - 楔形反转策略

模块化架构：
- logic/market_analyzer.py: 市场状态识别
- logic/patterns.py: 模式检测
- logic/state_machines.py: H2/L2 状态机
"""

import json
import logging
import pandas as pd
from typing import List, Optional, Tuple

import redis.asyncio as aioredis

# 导入模块化组件
from logic.market_analyzer import MarketState, MarketAnalyzer
from logic.patterns import PatternDetector
from logic.state_machines import HState, LState, H2StateMachine, L2StateMachine


class AlBrooksStrategy:
    """
    Al Brooks 价格行为策略（异步版本）
    
    通过组合各模块实现完整的交易信号生成
    """

    def __init__(self, ema_period: int = 20, lookback_period: int = 20, redis_url: Optional[str] = None):
        self.ema_period = ema_period
        self.lookback_period = lookback_period
        
        # 初始化模块化组件
        self.market_analyzer = MarketAnalyzer(ema_period=ema_period)
        self.pattern_detector = PatternDetector(lookback_period=lookback_period)
        
        # Redis 客户端（用于 OBI 过滤）
        self.redis_client: Optional[aioredis.Redis] = None
        self.redis_url = redis_url
        self._redis_connected = False
    
    async def connect_redis(self) -> bool:
        """异步连接 Redis"""
        if not self.redis_url:
            return False
        
        try:
            self.redis_client = await aioredis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=2,
            )
            await self.redis_client.ping()
            self._redis_connected = True
            logging.info("✅ 策略已连接 Redis（用于 OBI 过滤）")
            return True
        except Exception as e:
            logging.warning(f"⚠️ 策略无法连接 Redis: {e}，OBI 过滤将被禁用")
            self.redis_client = None
            self._redis_connected = False
            return False
    
    async def close_redis(self):
        """关闭 Redis 连接"""
        if self.redis_client:
            try:
                await self.redis_client.aclose()
            except:
                pass
            self.redis_client = None
            self._redis_connected = False

    def _compute_ema(self, df: pd.DataFrame) -> pd.Series:
        """计算 EMA"""
        return df["close"].ewm(span=self.ema_period, adjust=False).mean()

    def _compute_atr(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        """计算 ATR"""
        high = df["high"]
        low = df["low"]
        close = df["close"]

        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))

        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.ewm(span=period, adjust=False).mean()
    
    async def _get_obi(self, symbol: str = "BTCUSDT") -> Optional[float]:
        """从 Redis 异步获取 OBI"""
        if self.redis_client is None or not self._redis_connected:
            return None
        
        try:
            data = await self.redis_client.get(f"cache:obi:{symbol}")
            if data is None:
                return None
            return json.loads(data).get("obi")
        except Exception as e:
            logging.debug(f"获取 OBI 失败: {e}")
            return None
    
    def _calculate_tp1_tp2(
        self, entry_price: float, stop_loss: float, side: str, 
        base_height: float, atr: Optional[float] = None
    ) -> Tuple[float, float]:
        """
        计算分批止盈目标位
        
        TP1: 1R 距离（50% 仓位）
        TP2: Measured Move 或 2R（剩余 50%）
        """
        risk = abs(entry_price - stop_loss)
        
        if side == "buy":
            tp1 = entry_price + risk
            measured_move = entry_price + base_height if base_height > 0 else entry_price + (risk * 2)
            tp2 = max(measured_move, entry_price + (risk * 2))
            if base_height < risk * 1.5:
                tp2 = max(tp2, entry_price + (risk * 3))
        else:
            tp1 = entry_price - risk
            measured_move = entry_price - base_height if base_height > 0 else entry_price - (risk * 2)
            tp2 = min(measured_move, entry_price - (risk * 2))
            if base_height < risk * 1.5:
                tp2 = min(tp2, entry_price - (risk * 3))
        
        return (tp1, tp2)

    async def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        异步生成交易信号
        
        返回包含信号的 DataFrame:
        - ema, atr: 技术指标
        - market_state: 市场状态
        - signal, side: 交易信号
        - stop_loss, risk_reward_ratio: 风险管理
        - base_height, tp1_price, tp2_price: 止盈目标
        - tight_channel_score: 紧凑通道评分
        """
        data = df.copy()
        data["ema"] = self._compute_ema(data)
        
        if len(data) >= 20:
            data["atr"] = self._compute_atr(data, period=20)
        else:
            data["atr"] = None

        # 初始化结果列表
        signals: List[Optional[str]] = [None] * len(data)
        sides: List[Optional[str]] = [None] * len(data)
        stops: List[Optional[float]] = [None] * len(data)
        market_states: List[Optional[str]] = [None] * len(data)
        risk_reward_ratios: List[Optional[float]] = [None] * len(data)
        base_heights: List[Optional[float]] = [None] * len(data)
        tp1_prices: List[Optional[float]] = [None] * len(data)
        tp2_prices: List[Optional[float]] = [None] * len(data)
        tight_channel_scores: List[Optional[float]] = [None] * len(data)

        # Spike 回撤入场状态
        pending_spike: Optional[Tuple[str, str, float, float, float, int]] = None

        # H2/L2 状态机
        h2_machine = H2StateMachine()
        l2_machine = L2StateMachine()

        for i in range(1, len(data)):
            row = data.iloc[i]
            close, high, low = row["close"], row["high"], row["low"]
            ema = row["ema"]
            atr = row["atr"] if "atr" in data.columns else None

            # 检测市场状态
            market_state = self.market_analyzer.detect_market_state(data, i, ema)
            market_states[i] = market_state.value
            
            # 计算紧凑通道评分
            tight_channel_scores[i] = self.market_analyzer.calculate_tight_channel_score(data, i, ema)
            
            # 紧凑通道方向
            tight_channel_direction = None
            if market_state == MarketState.TIGHT_CHANNEL:
                tight_channel_direction = self.market_analyzer.get_tight_channel_direction(data, i)

            # 处理待处理的 Spike 回撤入场
            if pending_spike is not None:
                signal_type, side, stop_loss, limit_price, base_height, spike_idx = pending_spike

                if side == "buy" and low <= limit_price:
                    signals[i] = signal_type
                    sides[i] = side
                    stops[i] = stop_loss
                    base_heights[i] = base_height
                    risk_reward_ratios[i] = 2.0
                    tp1, tp2 = self._calculate_tp1_tp2(limit_price, stop_loss, side, base_height, atr)
                    tp1_prices[i], tp2_prices[i] = tp1, tp2
                    pending_spike = None
                    h2_machine.set_strong_trend()
                    continue
                elif side == "sell" and high >= limit_price:
                    signals[i] = signal_type
                    sides[i] = side
                    stops[i] = stop_loss
                    base_heights[i] = base_height
                    risk_reward_ratios[i] = 2.0
                    tp1, tp2 = self._calculate_tp1_tp2(limit_price, stop_loss, side, base_height, atr)
                    tp1_prices[i], tp2_prices[i] = tp1, tp2
                    pending_spike = None
                    l2_machine.set_strong_trend()
                    continue
                elif (side == "buy" and close > limit_price * 1.05) or (side == "sell" and close < limit_price * 0.95):
                    pending_spike = None
                elif i - spike_idx > 5:
                    pending_spike = None

            # 优先级1: Failed Breakout（区间策略最高优先级）
            if market_state == MarketState.TRADING_RANGE:
                result = self.pattern_detector.detect_failed_breakout(data, i, ema, atr, market_state)
                if result:
                    signal_type, side, stop_loss, base_height = result
                    signals[i] = signal_type
                    sides[i] = side
                    stops[i] = stop_loss
                    base_heights[i] = base_height
                    risk_reward_ratios[i] = 1.0
                    tp1, tp2 = self._calculate_tp1_tp2(close, stop_loss, side, base_height, atr)
                    tp1_prices[i], tp2_prices[i] = tp1, tp2
                    continue

            # 优先级2: Strong Spike
            spike_result = self.pattern_detector.detect_strong_spike(data, i, ema, atr, market_state)
            if spike_result:
                signal_type, side, stop_loss, limit_price, base_height = spike_result

                if limit_price is not None:
                    pending_spike = (signal_type, side, stop_loss, limit_price, base_height, i)
                else:
                    # OBI 过滤
                    obi_pass = True
                    if market_state == MarketState.BREAKOUT:
                        obi = await self._get_obi("BTCUSDT")
                        if obi is not None:
                            if side == "buy" and obi < -0.3:
                                logging.info(f"🚫 OBI过滤: {signal_type} 被阻止 (OBI={obi:.4f} < -0.3)")
                                obi_pass = False
                            elif side == "sell" and obi > 0.3:
                                logging.info(f"🚫 OBI过滤: {signal_type} 被阻止 (OBI={obi:.4f} > 0.3)")
                                obi_pass = False
                    
                    if obi_pass:
                        signals[i] = signal_type
                        sides[i] = side
                        stops[i] = stop_loss
                        base_heights[i] = base_height
                        risk_reward_ratios[i] = 2.0
                        tp1, tp2 = self._calculate_tp1_tp2(close, stop_loss, side, base_height, atr)
                        tp1_prices[i], tp2_prices[i] = tp1, tp2
                        if side == "buy":
                            h2_machine.set_strong_trend()
                        else:
                            l2_machine.set_strong_trend()
                continue

            # 优先级3: Climax 反转
            climax_result = self.pattern_detector.detect_climax_reversal(data, i, ema, atr)
            if climax_result:
                signal_type, side, stop_loss, base_height = climax_result
                
                # TIGHT_CHANNEL 保护
                if market_state == MarketState.TIGHT_CHANNEL:
                    if (tight_channel_direction == "up" and side == "sell") or \
                       (tight_channel_direction == "down" and side == "buy"):
                        continue
                
                signals[i] = signal_type
                sides[i] = side
                stops[i] = stop_loss
                base_heights[i] = base_height
                risk_reward_ratios[i] = 2.0
                tp1, tp2 = self._calculate_tp1_tp2(close, stop_loss, side, base_height, atr)
                tp1_prices[i], tp2_prices[i] = tp1, tp2
                continue

            # 优先级4: Wedge 反转
            wedge_result = self.pattern_detector.detect_wedge_reversal(data, i, ema, atr)
            if wedge_result:
                signal_type, side, stop_loss, base_height = wedge_result
                
                # TIGHT_CHANNEL 保护
                if market_state == MarketState.TIGHT_CHANNEL:
                    if (tight_channel_direction == "up" and side == "sell") or \
                       (tight_channel_direction == "down" and side == "buy"):
                        continue
                
                signals[i] = signal_type
                sides[i] = side
                stops[i] = stop_loss
                base_heights[i] = base_height
                risk_reward_ratios[i] = 2.0
                tp1, tp2 = self._calculate_tp1_tp2(close, stop_loss, side, base_height, atr)
                tp1_prices[i], tp2_prices[i] = tp1, tp2
                continue

            # H2/L2 状态机更新
            h2_signal = h2_machine.update(
                close, high, low, ema, atr, data, i,
                self.pattern_detector.calculate_unified_stop_loss
            )
            if h2_signal:
                signals[i] = h2_signal.signal_type
                sides[i] = h2_signal.side
                stops[i] = h2_signal.stop_loss
                base_heights[i] = h2_signal.base_height
                risk_reward_ratios[i] = 2.0
                tp1, tp2 = self._calculate_tp1_tp2(close, h2_signal.stop_loss, h2_signal.side, h2_signal.base_height, atr)
                tp1_prices[i], tp2_prices[i] = tp1, tp2

            l2_signal = l2_machine.update(
                close, high, low, ema, atr, data, i,
                self.pattern_detector.calculate_unified_stop_loss
            )
            if l2_signal:
                signals[i] = l2_signal.signal_type
                sides[i] = l2_signal.side
                stops[i] = l2_signal.stop_loss
                base_heights[i] = l2_signal.base_height
                risk_reward_ratios[i] = 2.0
                tp1, tp2 = self._calculate_tp1_tp2(close, l2_signal.stop_loss, l2_signal.side, l2_signal.base_height, atr)
                tp1_prices[i], tp2_prices[i] = tp1, tp2

        # 写入结果
        data["market_state"] = market_states
        data["signal"] = signals
        data["side"] = sides
        data["stop_loss"] = stops
        data["risk_reward_ratio"] = risk_reward_ratios
        data["base_height"] = base_heights
        data["tp1_price"] = tp1_prices
        data["tp2_price"] = tp2_prices
        data["tight_channel_score"] = tight_channel_scores
        
        return data
