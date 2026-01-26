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

订单流过滤：
- delta_flow.py: 动态订单流 Delta 分析（替代静态 OBI）
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

# 导入动态订单流模块
from delta_flow import (
    DeltaAnalyzer, 
    DeltaSnapshot, 
    DeltaTrend,
    DeltaSignalModifier, 
    get_delta_analyzer
)


class AlBrooksStrategy:
    """
    Al Brooks 价格行为策略（异步版本）
    
    通过组合各模块实现完整的交易信号生成
    
    订单流过滤：
    - 使用动态订单流 Delta 分析（基于 aggTrade）替代静态 OBI
    - Delta 分析能够区分：主动买入、主动卖出、流动性撤离、吸收
    - Delta 窗口与 K 线周期对齐，确保信号同步
    """

    def __init__(
        self, 
        ema_period: int = 20, 
        lookback_period: int = 20, 
        redis_url: Optional[str] = None,
        kline_interval: str = "5m"
    ):
        self.ema_period = ema_period
        self.lookback_period = lookback_period
        self.kline_interval = kline_interval
        
        # 初始化模块化组件
        self.market_analyzer = MarketAnalyzer(ema_period=ema_period)
        self.pattern_detector = PatternDetector(lookback_period=lookback_period)
        
        # Redis 客户端（用于 Delta 数据缓存，可选）
        self.redis_client: Optional[aioredis.Redis] = None
        self.redis_url = redis_url
        self._redis_connected = False
        
        # Delta 分析器（从全局获取，与 aggtrade_worker 共享，窗口与 K 线周期对齐）
        self.delta_analyzer: DeltaAnalyzer = get_delta_analyzer(kline_interval=kline_interval)
    
    async def connect_redis(self) -> bool:
        """异步连接 Redis（可选，用于 Delta 数据缓存）"""
        if not self.redis_url:
            logging.info("✅ 策略已初始化（Delta 分析使用内存模式）")
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
            logging.info("✅ 策略已连接 Redis（用于 Delta 缓存）")
            return True
        except Exception as e:
            logging.warning(f"⚠️ 策略无法连接 Redis: {e}，Delta 数据将使用内存模式")
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
    
    async def _get_delta_snapshot(self, symbol: str = "BTCUSDT") -> Optional[DeltaSnapshot]:
        """
        获取动态订单流 Delta 快照
        
        优先从内存获取（与 aggtrade_worker 共享），
        如果 Redis 可用也可以从 Redis 获取备用数据。
        
        Returns:
            DeltaSnapshot: 包含 Delta 分析结果的快照
        """
        # 优先从全局 Delta 分析器获取（实时数据）
        try:
            snapshot = await self.delta_analyzer.get_snapshot(symbol)
            if snapshot.trade_count > 0:
                return snapshot
        except Exception as e:
            logging.debug(f"从 Delta 分析器获取快照失败: {e}")
        
        # 备用：从 Redis 获取缓存数据
        if self.redis_client and self._redis_connected:
            try:
                data = await self.redis_client.get(f"cache:delta:{symbol}")
                if data:
                    parsed = json.loads(data)
                    return DeltaSnapshot(
                        cumulative_delta=parsed.get("cumulative_delta", 0.0),
                        buy_volume=parsed.get("buy_volume", 0.0),
                        sell_volume=parsed.get("sell_volume", 0.0),
                        delta_ratio=parsed.get("delta_ratio", 0.0),
                        delta_avg=parsed.get("delta_avg", 0.0),
                        delta_acceleration=parsed.get("delta_acceleration", 0.0),
                        delta_trend=DeltaTrend(parsed.get("delta_trend", "neutral")),
                        is_absorption=parsed.get("is_absorption", False),
                        is_climax_buy=parsed.get("is_climax_buy", False),
                        is_climax_sell=parsed.get("is_climax_sell", False),
                        trade_count=parsed.get("trade_count", 0),
                        timestamp=parsed.get("timestamp", 0),
                    )
            except Exception as e:
                logging.debug(f"从 Redis 获取 Delta 缓存失败: {e}")
        
        return None
    
    def _calculate_delta_signal_modifier(
        self, snapshot: DeltaSnapshot, side: str, price_change_pct: float = 0.0
    ) -> Tuple[float, str]:
        """
        计算动态订单流 Delta 对信号的调节作用
        
        核心逻辑（基于 Al Brooks 价格行为）：
        
        1. 买单吃进 (Aggressive Buying)：
           - Delta 为正且趋势看涨 → 增强买入信号
           - 这是真正的"Spike"，有机构资金支撑
        
        2. 卖单撤离 (Liquidity Withdrawal)：
           - 价格上涨但 Delta 不匹配 → 减弱买入信号
           - 这是"假突破"的典型特征
        
        3. 吸收 (Absorption)：
           - Delta 很大但价格不动 → 强烈减弱信号
           - 隐藏的大单在悄悄出货/吸筹
        
        Returns:
            (modifier, reason)
            - modifier > 1.0: 增强信号（订单流确认）
            - modifier = 1.0: 不调整
            - modifier < 1.0: 减弱信号（订单流不支持）
            - modifier = 0.0: 阻止信号（强烈反向订单流）
        """
        return DeltaSignalModifier.calculate_modifier(snapshot, side, price_change_pct)
    
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
        - delta_modifier: Delta调节因子 (>1增强, <1减弱, None未启用)
          基于动态订单流分析（aggTrade），可识别：
          - 主动买入/卖出（真实突破）
          - 流动性撤离（假突破）
          - 吸收（隐藏大单出货/吸筹）
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
        delta_modifiers: List[Optional[float]] = [None] * len(data)  # Delta调节因子

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
            
            # 只在处理最新 K 线时打印日志（避免历史数据重复打印）
            is_latest_bar = (i == len(data) - 1)

            # 检测市场状态
            market_state = self.market_analyzer.detect_market_state(data, i, ema)
            market_states[i] = market_state.value
            
            # 获取趋势方向和强度（用于逆势交易过滤）
            trend_direction = self.market_analyzer.get_trend_direction()
            trend_strength = self.market_analyzer.get_trend_strength()
            
            # 计算紧凑通道评分
            tight_channel_scores[i] = self.market_analyzer.calculate_tight_channel_score(data, i, ema)
            
            # 紧凑通道方向
            tight_channel_direction = None
            if market_state == MarketState.TIGHT_CHANNEL:
                tight_channel_direction = self.market_analyzer.get_tight_channel_direction(data, i)
            
            # ========== Al Brooks 核心：强趋势模式判断 ==========
            # 在 TIGHT_CHANNEL 或 STRONG_TREND 中，完全禁止反转，只允许顺势
            is_strong_trend_mode = (
                market_state == MarketState.TIGHT_CHANNEL or 
                market_state == MarketState.STRONG_TREND or
                trend_strength >= 0.7
            )
            
            # 确定允许的交易方向（None = 任意方向，"buy" = 只做多，"sell" = 只做空）
            allowed_side: Optional[str] = None
            if is_strong_trend_mode:
                if tight_channel_direction == "up" or trend_direction == "up":
                    allowed_side = "buy"  # 上升趋势只允许做多
                elif tight_channel_direction == "down" or trend_direction == "down":
                    allowed_side = "sell"  # 下降趋势只允许做空

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
            # ⭐ Al Brooks: FailedBreakout 是反转信号，在强趋势中完全禁止
            if market_state == MarketState.TRADING_RANGE and not is_strong_trend_mode:
                result = self.pattern_detector.detect_failed_breakout(data, i, ema, atr, market_state)
                if result:
                    signal_type, side, stop_loss, base_height = result
                    
                    # 检查是否符合允许的方向
                    if allowed_side is not None and side != allowed_side:
                        if is_latest_bar:
                            logging.info(
                                f"🚫 强趋势禁止反转: {signal_type} {side} - "
                                f"趋势={trend_direction}(强度={trend_strength:.2f})，只允许{allowed_side}"
                            )
                        continue
                    
                    signals[i] = signal_type
                    sides[i] = side
                    stops[i] = stop_loss
                    base_heights[i] = base_height
                    risk_reward_ratios[i] = 1.0
                    tp1, tp2 = self._calculate_tp1_tp2(close, stop_loss, side, base_height, atr)
                    tp1_prices[i], tp2_prices[i] = tp1, tp2
                    continue

            # 优先级2: Strong Spike
            # ⭐ Spike 是顺势信号，在强趋势中只允许顺势方向
            spike_result = self.pattern_detector.detect_strong_spike(data, i, ema, atr, market_state)
            if spike_result:
                signal_type, side, stop_loss, limit_price, base_height = spike_result
                
                # 检查是否符合允许的方向
                if allowed_side is not None and side != allowed_side:
                    if is_latest_bar:
                        logging.info(
                            f"🚫 强趋势只顺势: {signal_type} {side} 被禁止 - "
                            f"趋势={trend_direction}，只允许{allowed_side}"
                        )
                    continue

                if limit_price is not None:
                    pending_spike = (signal_type, side, stop_loss, limit_price, base_height, i)
                else:
                    # 动态订单流 Delta 过滤（替代静态 OBI）
                    delta_modifier = 1.0
                    delta_reason = "Delta未启用"
                    
                    # 计算 K 线价格变化百分比
                    kline_open = data.iloc[i]["open"]
                    price_change_pct = ((close - kline_open) / kline_open * 100) if kline_open > 0 else 0.0
                    
                    if market_state == MarketState.BREAKOUT:
                        delta_snapshot = await self._get_delta_snapshot("BTCUSDT")
                        if delta_snapshot is not None and delta_snapshot.trade_count > 0:
                            delta_modifier, delta_reason = self._calculate_delta_signal_modifier(
                                delta_snapshot, side, price_change_pct
                            )
                            
                            # 只在最新K线打印Delta日志
                            if is_latest_bar:
                                if delta_modifier == 0.0:
                                    logging.info(f"🚫 Delta阻止: {signal_type} {side} - {delta_reason}")
                                elif delta_modifier < 1.0:
                                    logging.info(f"⚠️ Delta减弱: {signal_type} {side} (调节={delta_modifier:.2f}) - {delta_reason}")
                                elif delta_modifier > 1.0:
                                    logging.info(f"✅ Delta增强: {signal_type} {side} (调节={delta_modifier:.2f}) - {delta_reason}")
                    
                    if delta_modifier > 0:
                        signals[i] = signal_type
                        sides[i] = side
                        stops[i] = stop_loss
                        base_heights[i] = base_height
                        risk_reward_ratios[i] = 2.0
                        delta_modifiers[i] = delta_modifier  # 记录Delta调节因子
                        tp1, tp2 = self._calculate_tp1_tp2(close, stop_loss, side, base_height, atr)
                        tp1_prices[i], tp2_prices[i] = tp1, tp2
                        if side == "buy":
                            h2_machine.set_strong_trend()
                        else:
                            l2_machine.set_strong_trend()
                continue

            # 优先级3: Climax 反转
            # ⭐ Al Brooks: Climax 是反转信号，在强趋势中完全禁止
            # "在紧凑通道中做反转是自杀行为" - Al Brooks
            if is_strong_trend_mode:
                # 强趋势模式：完全跳过 Climax 反转检测
                pass
            else:
                climax_result = self.pattern_detector.detect_climax_reversal(data, i, ema, atr)
                if climax_result:
                    signal_type, side, stop_loss, base_height = climax_result
                    
                    # 检查是否符合允许的方向
                    if allowed_side is not None and side != allowed_side:
                        if is_latest_bar:
                            logging.info(
                                f"🚫 强趋势禁止反转: {signal_type} {side} - "
                                f"趋势={trend_direction}，Climax反转在强趋势中胜率<20%"
                            )
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
            # ⭐ Al Brooks: Wedge 是反转信号，在强趋势中完全禁止
            if is_strong_trend_mode:
                # 强趋势模式：完全跳过 Wedge 反转检测
                pass
            else:
                wedge_result = self.pattern_detector.detect_wedge_reversal(data, i, ema, atr)
                if wedge_result:
                    signal_type, side, stop_loss, base_height = wedge_result
                    
                    # 检查是否符合允许的方向
                    if allowed_side is not None and side != allowed_side:
                        if is_latest_bar:
                            logging.info(
                                f"🚫 强趋势禁止反转: {signal_type} {side} - "
                                f"趋势={trend_direction}，Wedge反转在强趋势中胜率<15%"
                            )
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
            # ⭐ H2 是顺势做多信号，在强趋势中只在上升趋势允许
            if allowed_side is None or allowed_side == "buy":
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

            # ⭐ L2 是顺势做空信号，在强趋势中只在下降趋势允许
            if allowed_side is None or allowed_side == "sell":
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
        data["delta_modifier"] = delta_modifiers  # Delta调节因子
        
        return data
