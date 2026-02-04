"""
策略形态信号检测（Failed Breakout / Spike / Climax / Wedge / MTR / Final Flag）

供 strategy 通过 SignalChecker 统一调用，冷却期与成交量由回调注入。

优化 v2.0：打破 StrongTrend 对反转信号的绝对封锁
- 高优先级放行：P1 Climax / P3 Wedge 在 StrongTrend 中允许反转
- 动能衰减检测：过去 5 根 K 线实体递减时解除反向信号屏蔽
- MTR 准入：EMA 触碰或穿越即可触发，不再依赖 Channel 状态
"""

import logging
from typing import Optional, Callable, Dict, Any

import pandas as pd

from .market_analyzer import MarketState, MarketCycle
from .patterns import PatternDetector
from .signal_models import BarContext, SignalResult


# 冷却期检查: (signal_type, side, current_bar, is_latest_bar) -> bool（True=应跳过）
# 成交量确认: (ctx) -> bool（True=通过）
def _noop_cooldown(_st: str, _side: str, _bar: int, _latest: bool) -> bool:
    return False


def _noop_volume(_ctx: BarContext) -> bool:
    return True


# ============================================================================
# 动能衰减检测（Momentum Decay Detection）
# ============================================================================

def detect_momentum_decay(
    data: pd.DataFrame,
    i: int,
    lookback: int = 5,
    decay_threshold: float = 0.7,
) -> bool:
    """
    检测动能衰减：过去 N 根 K 线的实体大小是否呈递减趋势
    
    Al Brooks: "趋势末端的特征是动能递减 - K 线实体越来越小"
    
    Args:
        data: K线数据
        i: 当前 K 线索引
        lookback: 回看周期（默认 5 根）
        decay_threshold: 衰减阈值（后半段平均实体 < 前半段 * threshold）
    
    Returns:
        True 表示检测到动能衰减
    """
    if i < lookback:
        return False
    
    # 获取最近 N 根 K 线的实体大小
    bodies = []
    for j in range(i - lookback + 1, i + 1):
        if j < 0 or j >= len(data):
            continue
        bar = data.iloc[j]
        body = abs(float(bar["close"]) - float(bar["open"]))
        bodies.append(body)
    
    if len(bodies) < lookback:
        return False
    
    # 前半段 vs 后半段
    mid = len(bodies) // 2
    first_half = bodies[:mid]
    second_half = bodies[mid:]
    
    avg_first = sum(first_half) / len(first_half) if first_half else 0
    avg_second = sum(second_half) / len(second_half) if second_half else 0
    
    # 后半段平均实体 < 前半段 * 阈值 = 动能衰减
    if avg_first > 0 and avg_second < avg_first * decay_threshold:
        logging.debug(
            f"📉 检测到动能衰减: 前半段均值={avg_first:.2f}, "
            f"后半段均值={avg_second:.2f} < {avg_first * decay_threshold:.2f}"
        )
        return True
    
    return False


def check_ema_touched_or_broken(
    data: pd.DataFrame,
    i: int,
    ema: float,
    lookback: int = 5,
    tolerance_pct: float = 0.001,
) -> bool:
    """
    检测价格是否触碰或穿越 EMA
    
    Al Brooks: "价格回测 EMA 是趋势可能反转的早期信号"
    
    Args:
        data: K线数据
        i: 当前 K 线索引
        ema: EMA 值
        lookback: 回看周期（默认 5 根）
        tolerance_pct: 触碰容差（默认 0.1%）
    
    Returns:
        True 表示价格触碰或穿越 EMA
    """
    if i < 1 or ema <= 0:
        return False
    
    tolerance = ema * tolerance_pct
    
    for j in range(max(0, i - lookback + 1), i + 1):
        bar = data.iloc[j]
        bar_high = float(bar["high"])
        bar_low = float(bar["low"])
        
        # 触碰：K 线范围覆盖 EMA
        if bar_low <= ema + tolerance and bar_high >= ema - tolerance:
            logging.debug(f"📍 EMA触碰检测: K线#{j} 范围[{bar_low:.2f}, {bar_high:.2f}] 覆盖 EMA={ema:.2f}")
            return True
    
    return False


class SignalChecker:
    """
    形态信号检测器：Failed Breakout、Spike、Climax、Wedge、MTR、Final Flag。
    冷却期与成交量确认通过回调注入，便于与 strategy 解耦。
    """

    def __init__(
        self,
        pattern_detector: PatternDetector,
        check_signal_cooldown: Optional[Callable[[str, str, int, bool], bool]] = None,
        volume_confirms_breakout: Optional[Callable[[BarContext], bool]] = None,
        mtr_overrides: Optional[Dict[str, Any]] = None,
    ):
        self.pattern_detector = pattern_detector
        self._check_cooldown = check_signal_cooldown or _noop_cooldown
        self._volume_confirms = volume_confirms_breakout or _noop_volume
        self.mtr_overrides = mtr_overrides  # 仅回测使用，如 retest_tolerance=0.001

    def check_failed_breakout(
        self, data: pd.DataFrame, ctx: BarContext
    ) -> Optional[SignalResult]:
        """检测 Failed Breakout 信号。"""
        if ctx.market_state != MarketState.TRADING_RANGE or ctx.is_strong_trend_mode:
            return None
        relaxed_signal_bar = ctx.market_cycle == MarketCycle.TRADING_RANGE
        result = self.pattern_detector.detect_wedge_failed_breakout(
            data, ctx.i, ctx.ema, ctx.atr, ctx.market_state
        )
        if not result:
            result = self.pattern_detector.detect_failed_breakout(
                data, ctx.i, ctx.ema, ctx.atr, ctx.market_state,
                relaxed_signal_bar=relaxed_signal_bar,
            )
        if not result:
            return None
        signal_type, side, stop_loss, base_height = result
        if self._check_cooldown(signal_type, side, ctx.i, ctx.is_latest_bar):
            return None
        if ctx.allowed_side is not None and side != ctx.allowed_side:
            if ctx.is_latest_bar:
                logging.info(
                    f"🚫 强趋势禁止反转: {signal_type} {side} - "
                    f"趋势={ctx.trend_direction}(强度={ctx.trend_strength:.2f})，只允许{ctx.allowed_side}"
                )
            return None
        return SignalResult(
            signal_type=signal_type,
            side=side,
            stop_loss=stop_loss,
            base_height=base_height,
            risk_reward=1.0,
        )

    def check_spike(
        self, data: pd.DataFrame, ctx: BarContext
    ) -> Optional[SignalResult]:
        """检测 Strong Spike 信号。"""
        result = self.pattern_detector.detect_strong_spike(
            data, ctx.i, ctx.ema, ctx.atr, ctx.market_state
        )
        if not result:
            return None
        signal_type, side, stop_loss, limit_price, base_height, entry_mode, is_high_risk = result
        if self._check_cooldown(signal_type, side, ctx.i, ctx.is_latest_bar):
            return None
        if ctx.market_state == MarketState.STRONG_TREND:
            if ctx.trend_direction == "up" and side == "sell":
                if ctx.is_latest_bar:
                    logging.info(f"🚫 StrongTrend禁止做空: {signal_type} - 上涨趋势中禁止卖出")
                return None
            if ctx.trend_direction == "down" and side == "buy":
                if ctx.is_latest_bar:
                    logging.info(f"🚫 StrongTrend禁止做多: {signal_type} - 下跌趋势中禁止买入")
                return None
        if ctx.allowed_side is not None and side != ctx.allowed_side:
            if ctx.is_latest_bar:
                logging.info(
                    f"🚫 强趋势只顺势: {signal_type} {side} 被禁止 - "
                    f"趋势={ctx.trend_direction}，只允许{ctx.allowed_side}"
                )
            return None
        if not self._volume_confirms(ctx):
            if ctx.is_latest_bar:
                logging.debug(f"⏭ 成交量未确认突破跳过: {signal_type} {side}（未达均量倍数）")
            return None
        if ctx.is_latest_bar and is_high_risk:
            logging.info(
                f"⚠️ Spike 高风险: {signal_type} 止损距离>2.5*ATR，建议仓位 50%"
            )
        return SignalResult(
            signal_type=signal_type,
            side=side,
            stop_loss=stop_loss,
            base_height=base_height,
            limit_price=limit_price,
            risk_reward=2.0,
            entry_mode=entry_mode,
            is_high_risk=is_high_risk,
        )

    def check_ma_gap_bar(
        self, data: pd.DataFrame, ctx: BarContext
    ) -> Optional[SignalResult]:
        """
        检测 Moving Average Gap Bar 信号
        
        Al Brooks 加密货币市场修正版：
        - 上涨 MA Gap：连续 3 根 K 线的 Low 始终高于 20 EMA = 强动能
        - 下跌 MA Gap：连续 3 根 K 线的 High 始终低于 20 EMA = 强动能
        
        当检测到 MA Gap 时：
        - 解除 "必须触碰 EMA" 的回调限制
        - 只要当前棒是顺势趋势棒且突破前一棒极值，允许直接入场
        - 入场方式：限价单（订单簿最优价）
        """
        # MA Gap Bar 只在趋势状态下触发
        if ctx.market_state not in [MarketState.STRONG_TREND, MarketState.TIGHT_CHANNEL,
                                     MarketState.CHANNEL, MarketState.BREAKOUT]:
            return None
        
        result = self.pattern_detector.detect_ma_gap_bar(
            data, ctx.i, ctx.ema, ctx.atr, ctx.market_state
        )
        if not result:
            return None
        
        signal_type, side, stop_loss, limit_price, base_height, entry_mode = result
        
        if self._check_cooldown(signal_type, side, ctx.i, ctx.is_latest_bar):
            return None
        
        # MA Gap Bar 必须顺势，逆势不允许
        if ctx.allowed_side is not None and side != ctx.allowed_side:
            if ctx.is_latest_bar:
                logging.debug(
                    f"🚫 MA Gap Bar 必须顺势: {signal_type} {side} 被禁止 - "
                    f"趋势={ctx.trend_direction}，只允许{ctx.allowed_side}"
                )
            return None
        
        # 成交量确认（可选）
        if not self._volume_confirms(ctx):
            if ctx.is_latest_bar:
                logging.debug(f"⏭ 成交量未确认跳过: {signal_type} {side}")
            return None
        
        if ctx.is_latest_bar:
            logging.debug(
                f"✅ MA Gap Bar 信号: {signal_type} {side} - "
                f"限价入场={limit_price:.2f}, 止损={stop_loss:.2f}"
            )
        
        return SignalResult(
            signal_type=signal_type,
            side=side,
            stop_loss=stop_loss,
            base_height=base_height,
            limit_price=limit_price,
            risk_reward=2.0,
            entry_mode=entry_mode,
            strength=0.7,  # MA Gap Bar 是高置信度的顺势信号
        )

    def check_climax(
        self, data: pd.DataFrame, ctx: BarContext
    ) -> Optional[SignalResult]:
        """
        检测 Climax 反转信号（优化版：高优先级放行）
        
        优化：
        1. Climax 是 P1 高优先级信号，即便在 StrongTrend 中也允许反转
        2. 动能衰减时解除方向屏蔽
        """
        # Al Brooks: Spike 周期内禁止反转信号，这是 "Always In" 阶段
        if ctx.market_cycle == MarketCycle.SPIKE:
            return None
        
        # ⭐ 优化：Climax 是 P1 高优先级信号，不再被 StrongTrend 完全封锁
        # 但仍需检测是否有真正的 Climax 形态
        result = self.pattern_detector.detect_climax_reversal(
            data, ctx.i, ctx.ema, ctx.atr
        )
        if not result:
            return None
        signal_type, side, stop_loss, base_height = result
        if self._check_cooldown(signal_type, side, ctx.i, ctx.is_latest_bar):
            return None
        
        # ⭐ 优化：高优先级放行逻辑
        # Climax 是趋势极端情况，即便在 StrongTrend 中也应该允许反转
        if ctx.allowed_side is not None and side != ctx.allowed_side:
            # 检查是否有动能衰减
            has_momentum_decay = detect_momentum_decay(data, ctx.i, lookback=5, decay_threshold=0.7)
            
            if has_momentum_decay:
                # 动能衰减，允许反转
                if ctx.is_latest_bar:
                    logging.info(
                        f"⚡ Climax P1放行(动能衰减): {signal_type} {side} - "
                        f"趋势={ctx.trend_direction}(强度={ctx.trend_strength:.2f})，动能递减允许反转"
                    )
            else:
                # 即便没有动能衰减，Climax 作为 P1 信号也应该被允许（高胜率形态）
                # 但给予警告，让用户知道这是逆势交易
                if ctx.is_latest_bar:
                    logging.info(
                        f"⚡ Climax P1放行(高优先级): {signal_type} {side} - "
                        f"趋势={ctx.trend_direction}(强度={ctx.trend_strength:.2f})，Climax是趋势耗尽信号"
                    )
        
        return SignalResult(
            signal_type=signal_type,
            side=side,
            stop_loss=stop_loss,
            base_height=base_height,
            risk_reward=2.0,
            # 标记为高优先级反转信号
            strength=0.8 if ctx.is_strong_trend_mode else 0.6,
        )

    def check_wedge(
        self, data: pd.DataFrame, ctx: BarContext
    ) -> Optional[SignalResult]:
        """
        检测 Wedge 反转信号（优化版：P3 高优先级放行）
        
        优化：
        1. Wedge（三推）是 P3 高优先级信号，即便在 StrongTrend 中也允许反转
        2. 动能衰减时解除方向屏蔽
        3. 三推形态本身就是趋势耗尽的典型结构
        """
        # Al Brooks: Spike 周期内禁止反转信号，这是 "Always In" 阶段
        if ctx.market_cycle == MarketCycle.SPIKE:
            return None
        
        # ⭐ 优化：Wedge 是 P3 高优先级信号，不再被 StrongTrend 完全封锁
        relaxed_signal_bar = ctx.market_cycle == MarketCycle.TRADING_RANGE
        result = self.pattern_detector.detect_wedge_reversal(
            data, ctx.i, ctx.ema, ctx.atr, ctx.market_state,
            relaxed_signal_bar=relaxed_signal_bar,
        )
        if not result:
            return None
        signal_type, side, stop_loss, base_height, wedge_tp1, wedge_tp2, is_strong_reversal_bar = result
        if self._check_cooldown(signal_type, side, ctx.i, ctx.is_latest_bar):
            return None
        
        # ⭐ 优化：高优先级放行逻辑
        # Wedge（三推）本身就是趋势耗尽的经典形态
        if ctx.allowed_side is not None and side != ctx.allowed_side:
            # 检查是否有动能衰减
            has_momentum_decay = detect_momentum_decay(data, ctx.i, lookback=5, decay_threshold=0.7)
            
            if has_momentum_decay:
                # 动能衰减 + 三推形态，允许反转
                if ctx.is_latest_bar:
                    logging.info(
                        f"⚡ Wedge P3放行(动能衰减): {signal_type} {side} - "
                        f"趋势={ctx.trend_direction}(强度={ctx.trend_strength:.2f})，三推+动能递减允许反转"
                    )
            elif is_strong_reversal_bar:
                # 强反转棒 + 三推形态，允许反转
                if ctx.is_latest_bar:
                    logging.info(
                        f"⚡ Wedge P3放行(强反转棒): {signal_type} {side} - "
                        f"趋势={ctx.trend_direction}(强度={ctx.trend_strength:.2f})，三推形态是趋势耗尽信号"
                    )
            else:
                # 普通三推，在 StrongTrend 中仍需谨慎
                if ctx.is_latest_bar:
                    logging.info(
                        f"🚫 Wedge 高风险: {signal_type} {side} - "
                        f"趋势={ctx.trend_direction}(强度={ctx.trend_strength:.2f})，"
                        f"无动能衰减且无强反转棒，胜率较低"
                    )
                return None
        
        # 基础强度 + 强反转棒加成 + StrongTrend 逆势加成
        strength = 0.5 + (0.2 if is_strong_reversal_bar else 0.0)
        if ctx.is_strong_trend_mode and side != ctx.allowed_side:
            # 逆势 Wedge 信号需要更高置信度
            strength += 0.1
        
        return SignalResult(
            signal_type=signal_type,
            side=side,
            stop_loss=stop_loss,
            base_height=base_height,
            risk_reward=2.0,
            wedge_tp1_price=wedge_tp1,
            wedge_tp2_price=wedge_tp2,
            wedge_strong_reversal_bar=is_strong_reversal_bar,
            strength=strength,
        )

    def check_mtr(
        self, data: pd.DataFrame, ctx: BarContext
    ) -> Optional[SignalResult]:
        """
        检测 MTR（Major Trend Reversal）主要趋势反转（优化版：EMA 触碰准入）
        
        优化：
        1. MTR 准入条件从 NOT StrongTrend 修改为 EMA_Touched_Or_Broken
        2. 只要价格回测触碰或穿过 EMA，即便系统还未切换至 Channel 状态，也允许 MTR 逻辑运行
        3. 动能衰减时解除方向屏蔽
        """
        # Al Brooks: Spike 周期内禁止反转信号，这是 "Always In" 阶段
        if ctx.market_cycle == MarketCycle.SPIKE:
            return None
        
        # ⭐ 优化：MTR 准入条件改为 EMA 触碰或穿越
        # 原逻辑: if ctx.is_strong_trend_mode: return None
        # 新逻辑: 检查 EMA 是否被触碰或穿越
        ema_touched = check_ema_touched_or_broken(
            data, ctx.i, ctx.ema, lookback=5, tolerance_pct=0.001
        )
        
        if ctx.is_strong_trend_mode and not ema_touched:
            # 仍在 StrongTrend 且 EMA 未被触碰，不允许 MTR
            return None
        
        kwargs = self.mtr_overrides or {}
        result = self.pattern_detector.detect_mtr_reversal(
            data, ctx.i, ctx.ema, ctx.atr, ctx.market_state, **kwargs
        )
        if not result:
            return None
        signal_type, side, stop_loss, base_height = result
        if self._check_cooldown(signal_type, side, ctx.i, ctx.is_latest_bar):
            return None
        
        # ⭐ 优化：EMA 触碰 + 动能衰减时允许逆势 MTR
        if ctx.allowed_side is not None and side != ctx.allowed_side:
            has_momentum_decay = detect_momentum_decay(data, ctx.i, lookback=5, decay_threshold=0.7)
            
            if ema_touched and has_momentum_decay:
                # EMA 触碰 + 动能衰减，允许 MTR
                if ctx.is_latest_bar:
                    logging.info(
                        f"⚡ MTR放行(EMA触碰+动能衰减): {signal_type} {side} - "
                        f"趋势={ctx.trend_direction}(强度={ctx.trend_strength:.2f})，"
                        f"EMA回测+动能递减允许反转"
                    )
            elif ema_touched:
                # 仅 EMA 触碰，给予警告但允许
                if ctx.is_latest_bar:
                    logging.info(
                        f"⚡ MTR放行(EMA触碰): {signal_type} {side} - "
                        f"趋势={ctx.trend_direction}(强度={ctx.trend_strength:.2f})，"
                        f"EMA已回测，趋势可能反转"
                    )
            else:
                # EMA 未触碰，拒绝 MTR
                if ctx.is_latest_bar:
                    logging.info(
                        f"🚫 MTR 未满足条件: {signal_type} {side} - "
                        f"趋势={ctx.trend_direction}，EMA 未被触碰，MTR 不触发"
                    )
                return None
        
        # 计算强度：EMA 触碰 + 动能衰减给予更高强度
        strength = 0.6
        if ema_touched:
            strength += 0.1
        if detect_momentum_decay(data, ctx.i, lookback=5, decay_threshold=0.7):
            strength += 0.1
        
        return SignalResult(
            signal_type=signal_type,
            side=side,
            stop_loss=stop_loss,
            base_height=base_height,
            risk_reward=2.0,
            strength=strength,
        )

    def check_final_flag(
        self, data: pd.DataFrame, ctx: BarContext, final_flag_info: dict
    ) -> Optional[SignalResult]:
        """
        检测 Final Flag Reversal（终极旗形反转）- 高胜率反转点。
        
        Al Brooks: "Final Flag 是趋势耗尽的最后挣扎，突破失败后是高胜率反转入场点。"
        
        Args:
            data: K线数据
            ctx: 当前 K 线的市场上下文
            final_flag_info: Final Flag 信息（来自 MarketAnalyzer.get_final_flag_info()）
        """
        # Final Flag 反转不受强趋势模式限制（因为本身就是趋势耗尽的信号）
        if ctx.market_state != MarketState.FINAL_FLAG:
            return None
        
        result = self.pattern_detector.detect_final_flag_reversal(
            data, ctx.i, ctx.ema, ctx.atr, ctx.market_state, final_flag_info
        )
        if not result:
            return None
        
        signal_type, side, stop_loss, base_height = result
        if self._check_cooldown(signal_type, side, ctx.i, ctx.is_latest_bar):
            return None
        
        # Final Flag 反转是高胜率信号，给予更高的风险回报比
        return SignalResult(
            signal_type=signal_type,
            side=side,
            stop_loss=stop_loss,
            base_height=base_height,
            risk_reward=2.5,  # 高胜率信号，风险回报比 2.5
            strength=0.8,  # 高置信度
        )

    def check_spike_market_entry(
        self, data: pd.DataFrame, ctx: BarContext
    ) -> Optional[SignalResult]:
        """
        检测 Spike_Market_Entry 信号 - 突破阶段直接入场
        
        Al Brooks: "在突破阶段（Breakout Phase），收盘价就是买入信号"
        
        触发场景：MarketCycle.SPIKE 期间
        
        触发条件：
        1. 当前棒是强趋势棒（实体占比 > 60%，收盘在极端 25%）
        2. 价格处于 EMA 上方（买入）或下方（卖出）
        
        入场点：当前棒收盘价直接市价入场
        
        Returns:
            SignalResult 或 None
        """
        # 只在 Spike 周期触发
        if ctx.market_cycle != MarketCycle.SPIKE:
            return None
        
        i = ctx.i
        if i < 3:
            return None
        
        ema = ctx.ema
        atr = ctx.atr
        
        # 获取当前 K 线数据
        current_bar = data.iloc[i]
        curr_close = float(current_bar["close"])
        curr_open = float(current_bar["open"])
        curr_high = float(current_bar["high"])
        curr_low = float(current_bar["low"])
        curr_body = abs(curr_close - curr_open)
        curr_range = curr_high - curr_low
        
        if curr_range <= 0:
            return None
        
        # ========== 条件1: 强趋势棒验证（实体占比 > 60%，收盘在极端 25%）==========
        MIN_BODY_RATIO = 0.60
        CLOSE_POSITION_PCT = 0.25  # 收盘在顶部/底部 25% 区域
        
        body_ratio = curr_body / curr_range
        if body_ratio < MIN_BODY_RATIO:
            return None
        
        # 判断方向
        is_bullish = curr_close > curr_open
        is_bearish = curr_close < curr_open
        
        if not is_bullish and not is_bearish:
            return None  # 十字星，跳过
        
        # ========== 条件2: 价格相对 EMA 位置 ==========
        signal_side = None
        stop_loss = 0.0
        
        if is_bullish and curr_close > ema:
            # 看涨：收盘价必须在 K 线顶部 25% 区域
            close_from_high = (curr_high - curr_close) / curr_range
            if close_from_high > CLOSE_POSITION_PCT:
                return None
            
            signal_side = "buy"
            # 止损：当前棒低点外 0.1%
            stop_loss = curr_low * (1.0 - 0.001)
        
        elif is_bearish and curr_close < ema:
            # 看跌：收盘价必须在 K 线底部 25% 区域
            close_from_low = (curr_close - curr_low) / curr_range
            if close_from_low > CLOSE_POSITION_PCT:
                return None
            
            signal_side = "sell"
            # 止损：当前棒高点外 0.1%
            stop_loss = curr_high * (1.0 + 0.001)
        
        if signal_side is None:
            return None
        
        # ========== 冷却期检查 ==========
        signal_type = f"Spike_Market_{signal_side.capitalize()}"
        if self._check_cooldown(signal_type, signal_side, i, ctx.is_latest_bar):
            return None
        
        # ========== 方向过滤（Spike 周期不严格限制，但仍检查 allowed_side）==========
        # Spike 周期已经是强动能，allowed_side 检查可以放宽
        if ctx.allowed_side is not None and signal_side != ctx.allowed_side:
            # Spike 周期内仍允许顺势交易，但记录日志
            if ctx.is_latest_bar:
                logging.debug(
                    f"⚠️ Spike_Market_Entry 方向检查: {signal_type} - "
                    f"allowed_side={ctx.allowed_side}，但 Spike 周期放行"
                )
        
        # ========== 计算 base_height ==========
        base_height = (atr * 2.0) if atr and atr > 0 else curr_range
        
        # 入场模式：市价入场
        entry_mode = "Market_Entry"
        
        # 日志输出
        if ctx.is_latest_bar:
            logging.info(
                f"🚀 检测到 Spike 突破阶段，激活应急入场逻辑（跳过 H2 等待） | "
                f"信号: {signal_type} | 实体比: {body_ratio:.0%} | "
                f"入场: {curr_close:.2f} | 止损: {stop_loss:.2f}"
            )
        
        return SignalResult(
            signal_type=signal_type,
            side=signal_side,
            stop_loss=stop_loss,
            base_height=base_height,
            entry_mode=entry_mode,
            risk_reward=2.0,
            strength=0.8,  # Spike 阶段的信号具有高置信度
        )

    def check_micro_channel_h1(
        self, data: pd.DataFrame, ctx: BarContext
    ) -> Optional[SignalResult]:
        """
        检测 Micro_Channel_H1 信号 - 微型通道顺势补位
        
        Al Brooks: "在微型通道（Micro Channel）中，不会出现标准的回调（阴线），
        此时 High 1 (H1) 或 Breakout Bar Close 即可作为入场信号。"
        
        触发场景：MarketState.STRONG_TREND 或 TIGHT_CHANNEL
        
        触发条件：
        1. Gap 检测：连续至少 3 根 K 线完全脱离 EMA（Low > EMA 或 High < EMA）
        2. H1 触发：当前 K 线最高点突破了前一根 K 线的最高点（买入）
                    或当前 K 线最低点跌破了前一根 K 线的最低点（卖出）
        3. 豁免条件：跳过 _has_counting_bars（阴线计数）的检查
        
        Returns:
            SignalResult 或 None
        """
        # 只在 StrongTrend 或 TightChannel 状态下触发
        if ctx.market_state not in [MarketState.STRONG_TREND, MarketState.TIGHT_CHANNEL]:
            return None
        
        i = ctx.i
        if i < 5:
            return None
        
        ema = ctx.ema
        atr = ctx.atr
        
        # 获取当前 K 线数据
        current_bar = data.iloc[i]
        curr_close = float(current_bar["close"])
        curr_open = float(current_bar["open"])
        curr_high = float(current_bar["high"])
        curr_low = float(current_bar["low"])
        
        # 前一根 K 线
        prev_bar = data.iloc[i - 1]
        prev_high = float(prev_bar["high"])
        prev_low = float(prev_bar["low"])
        
        # ========== Step 1: 计算 GapCount（连续脱离 EMA 的 K 线数）==========
        MIN_GAP_COUNT = 3   # 最少需要 3 根
        STRONG_GAP_COUNT = 5  # 5 根以上忽略 HTF 反向限制
        
        # 检查向上 Gap（Low > EMA）
        up_gap_count = 0
        for j in range(1, min(20, i)):
            bar = data.iloc[i - j]
            bar_low = float(bar["low"])
            bar_ema = float(bar["ema"]) if "ema" in bar else ema
            if bar_low > bar_ema:
                up_gap_count += 1
            else:
                break
        
        # 检查向下 Gap（High < EMA）
        down_gap_count = 0
        for j in range(1, min(20, i)):
            bar = data.iloc[i - j]
            bar_high = float(bar["high"])
            bar_ema = float(bar["ema"]) if "ema" in bar else ema
            if bar_high < bar_ema:
                down_gap_count += 1
            else:
                break
        
        # 如果没有足够的 Gap，返回 None
        if up_gap_count < MIN_GAP_COUNT and down_gap_count < MIN_GAP_COUNT:
            return None
        
        # ========== Step 2: H1 触发检测（突破前一棒极值）==========
        signal_side = None
        stop_loss = 0.0
        gap_count = 0
        
        # 上涨微型通道 H1
        if up_gap_count >= MIN_GAP_COUNT:
            # 当前棒必须是阳线
            if curr_close <= curr_open:
                return None
            
            # H1 触发：当前棒最高点突破前一棒最高点
            if curr_high <= prev_high:
                return None
            
            # 当前棒 Low 也必须高于 EMA（保持 Gap 状态）
            if curr_low <= ema:
                return None
            
            signal_side = "buy"
            gap_count = up_gap_count
            
            # 止损：前一棒低点外 0.1%
            stop_loss = prev_low * (1.0 - 0.001)
        
        # 下跌微型通道 L1
        elif down_gap_count >= MIN_GAP_COUNT:
            # 当前棒必须是阴线
            if curr_close >= curr_open:
                return None
            
            # L1 触发：当前棒最低点跌破前一棒最低点
            if curr_low >= prev_low:
                return None
            
            # 当前棒 High 也必须低于 EMA（保持 Gap 状态）
            if curr_high >= ema:
                return None
            
            signal_side = "sell"
            gap_count = down_gap_count
            
            # 止损：前一棒高点外 0.1%
            stop_loss = prev_high * (1.0 + 0.001)
        
        if signal_side is None:
            return None
        
        # ========== Step 3: 冷却期检查 ==========
        # 使用 H1 信号类型
        signal_type = f"MicroChannel_H1_{signal_side.capitalize()}"
        if self._check_cooldown(signal_type, signal_side, i, ctx.is_latest_bar):
            return None
        
        # ========== Step 4: 方向过滤 ==========
        # Gap >= 5 根时，忽略反向限制（短线动能压倒长线趋势）
        ignore_htf_filter = gap_count >= STRONG_GAP_COUNT
        
        if not ignore_htf_filter:
            if ctx.allowed_side is not None and signal_side != ctx.allowed_side:
                if ctx.is_latest_bar:
                    logging.debug(
                        f"🚫 MicroChannel_H1 方向过滤: {signal_type} - "
                        f"趋势={ctx.trend_direction}，只允许{ctx.allowed_side}"
                    )
                return None
        else:
            # Gap >= 5 根，忽略 HTF 反向限制
            if ctx.is_latest_bar:
                logging.info(
                    f"⚡ 微型通道强动能放行: {signal_type} - "
                    f"GapCount={gap_count} >= 5，短线动能压倒长线趋势"
                )
        
        # ========== Step 5: 计算 base_height ==========
        base_height = (atr * 2.0) if atr and atr > 0 else (curr_high - curr_low)
        
        # 入场模式：限价单（使用突破价位）
        entry_mode = "Limit_Entry"
        limit_price = prev_high if signal_side == "buy" else prev_low
        
        # 日志输出
        if ctx.is_latest_bar:
            logging.info(
                f"🚀 检测到微型通道，激活应急入场逻辑（跳过 H2 等待） | "
                f"信号: H1_{signal_side.capitalize()} 触发 (GapCount: {gap_count}) | "
                f"限价入场: {limit_price:.2f} | 止损: {stop_loss:.2f}"
            )
        
        return SignalResult(
            signal_type=signal_type,
            side=signal_side,
            stop_loss=stop_loss,
            base_height=base_height,
            limit_price=limit_price,
            entry_mode=entry_mode,
            risk_reward=2.0,
            strength=0.75 + (0.1 if gap_count >= STRONG_GAP_COUNT else 0.0),  # Gap 越多强度越高
        )

    def check_gapbar_entry(
        self, data: pd.DataFrame, ctx: BarContext
    ) -> Optional[SignalResult]:
        """
        检测 GapBar_Entry 信号 - 强单边行情专用顺势入场
        
        Al Brooks 修正版：当市场处于强单边行情时，H2/L2 的"等待回调"逻辑可能
        导致长时间无法入场。此时使用 GapBar_Entry 作为补位手段。
        
        触发条件：
        1. 市场状态为 StrongTrend 或 TightChannel
        2. 连续 N 根 K 线完全脱离 EMA（Gap Count >= 3）
           - 上涨：Low > EMA
           - 下跌：High < EMA
        3. 当前 K 线是顺势趋势棒（实体占比 > 50%）
        4. 价格突破前一根棒的最高点（Buy）或最低点（Sell）
        
        特性：
        - 优先级低于标准 H2/L2，但在 StrongTrend 期间作为主要补位手段
        - Gap >= 5 根时，忽略 HTF(1h) 反向限制（短线动能压倒长线趋势）
        
        Returns:
            SignalResult 或 None
        """
        # 只在 StrongTrend 或 TightChannel 状态下触发
        if ctx.market_state not in [MarketState.STRONG_TREND, MarketState.TIGHT_CHANNEL]:
            return None
        
        i = ctx.i
        if i < 5:
            return None
        
        ema = ctx.ema
        atr = ctx.atr
        
        # 获取当前 K 线数据
        current_bar = data.iloc[i]
        curr_close = float(current_bar["close"])
        curr_open = float(current_bar["open"])
        curr_high = float(current_bar["high"])
        curr_low = float(current_bar["low"])
        curr_body = abs(curr_close - curr_open)
        curr_range = curr_high - curr_low
        
        # 前一根 K 线
        prev_bar = data.iloc[i - 1]
        prev_high = float(prev_bar["high"])
        prev_low = float(prev_bar["low"])
        
        # ========== Step 1: 计算 GapCount（连续脱离 EMA 的 K 线数）==========
        MIN_GAP_COUNT = 3  # 最少需要 3 根
        STRONG_GAP_COUNT = 5  # 5 根以上忽略 HTF 反向限制
        
        # 检查向上 Gap（Low > EMA）
        up_gap_count = 0
        for j in range(1, min(20, i)):
            bar = data.iloc[i - j]
            bar_low = float(bar["low"])
            bar_ema = float(bar["ema"]) if "ema" in bar else ema
            if bar_low > bar_ema:
                up_gap_count += 1
            else:
                break
        
        # 检查向下 Gap（High < EMA）
        down_gap_count = 0
        for j in range(1, min(20, i)):
            bar = data.iloc[i - j]
            bar_high = float(bar["high"])
            bar_ema = float(bar["ema"]) if "ema" in bar else ema
            if bar_high < bar_ema:
                down_gap_count += 1
            else:
                break
        
        # 如果没有足够的 Gap，返回 None
        if up_gap_count < MIN_GAP_COUNT and down_gap_count < MIN_GAP_COUNT:
            return None
        
        # ========== Step 2: 检测当前棒是否为顺势趋势棒 ==========
        MIN_BODY_RATIO = 0.50  # 实体占比 > 50%
        
        if curr_range <= 0:
            return None
        
        body_ratio = curr_body / curr_range
        if body_ratio < MIN_BODY_RATIO:
            return None
        
        # ========== Step 3: 生成信号 ==========
        signal_side = None
        stop_loss = 0.0
        gap_count = 0
        
        # 上涨 Gap Bar Entry
        if up_gap_count >= MIN_GAP_COUNT:
            # 当前棒必须是阳线
            if curr_close <= curr_open:
                return None
            
            # 当前棒必须突破前一棒最高点
            if curr_high <= prev_high:
                return None
            
            # 当前棒 Low 也必须高于 EMA（保持 Gap 状态）
            if curr_low <= ema:
                return None
            
            signal_side = "buy"
            gap_count = up_gap_count
            
            # 止损：前一棒低点外 0.1%
            stop_loss = prev_low * (1.0 - 0.001)
        
        # 下跌 Gap Bar Entry
        elif down_gap_count >= MIN_GAP_COUNT:
            # 当前棒必须是阴线
            if curr_close >= curr_open:
                return None
            
            # 当前棒必须突破前一棒最低点
            if curr_low >= prev_low:
                return None
            
            # 当前棒 High 也必须低于 EMA（保持 Gap 状态）
            if curr_high >= ema:
                return None
            
            signal_side = "sell"
            gap_count = down_gap_count
            
            # 止损：前一棒高点外 0.1%
            stop_loss = prev_high * (1.0 + 0.001)
        
        if signal_side is None:
            return None
        
        # ========== Step 4: 冷却期检查 ==========
        signal_type = f"GapBar_{signal_side.capitalize()}"
        if self._check_cooldown(signal_type, signal_side, i, ctx.is_latest_bar):
            return None
        
        # ========== Step 5: 方向过滤 ==========
        # 注意：GapBar_Entry 是顺势信号，但需要检查 allowed_side
        # Gap >= 5 根时，忽略反向限制（短线动能压倒长线趋势）
        ignore_htf_filter = gap_count >= STRONG_GAP_COUNT
        
        if not ignore_htf_filter:
            if ctx.allowed_side is not None and signal_side != ctx.allowed_side:
                if ctx.is_latest_bar:
                    logging.debug(
                        f"🚫 GapBar 方向过滤: {signal_type} - "
                        f"趋势={ctx.trend_direction}，只允许{ctx.allowed_side}"
                    )
                return None
        else:
            # Gap >= 5 根，忽略 HTF 反向限制
            if ctx.is_latest_bar:
                logging.info(
                    f"⚡ GapBar 强动能放行: {signal_type} - "
                    f"Gap={gap_count}根 >= 5，短线动能压倒长线趋势"
                )
        
        # ========== Step 6: 计算 base_height ==========
        # 使用 ATR 的 2 倍作为目标
        base_height = (atr * 2.0) if atr and atr > 0 else (curr_high - curr_low)
        
        # 入场模式：限价单（使用突破价位）
        entry_mode = "Limit_Entry"
        limit_price = prev_high if signal_side == "buy" else prev_low
        
        # 日志输出
        if ctx.is_latest_bar:
            logging.info(
                f"🚀 强趋势 GapBar 触发入场，跳过 H2 等待 | "
                f"信号: {signal_type} | Gap: {gap_count}根 | "
                f"限价入场: {limit_price:.2f} | 止损: {stop_loss:.2f}"
            )
        
        return SignalResult(
            signal_type=signal_type,
            side=signal_side,
            stop_loss=stop_loss,
            base_height=base_height,
            limit_price=limit_price,
            entry_mode=entry_mode,
            risk_reward=2.0,
            strength=0.7 + (0.1 if gap_count >= STRONG_GAP_COUNT else 0.0),  # Gap 越多强度越高
        )
