"""
策略形态信号检测（Failed Breakout / Spike / Climax / Wedge / MTR / Final Flag）

供 strategy 通过 SignalChecker 统一调用，冷却期与成交量由回调注入。
"""

import logging
from typing import Optional, Callable

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
    ):
        self.pattern_detector = pattern_detector
        self._check_cooldown = check_signal_cooldown or _noop_cooldown
        self._volume_confirms = volume_confirms_breakout or _noop_volume

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

    def check_climax(
        self, data: pd.DataFrame, ctx: BarContext
    ) -> Optional[SignalResult]:
        """检测 Climax 反转信号。"""
        if ctx.is_strong_trend_mode:
            return None
        result = self.pattern_detector.detect_climax_reversal(
            data, ctx.i, ctx.ema, ctx.atr
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
                    f"趋势={ctx.trend_direction}，Climax反转在强趋势中胜率<20%"
                )
            return None
        return SignalResult(
            signal_type=signal_type,
            side=side,
            stop_loss=stop_loss,
            base_height=base_height,
            risk_reward=2.0,
        )

    def check_wedge(
        self, data: pd.DataFrame, ctx: BarContext
    ) -> Optional[SignalResult]:
        """检测 Wedge 反转信号。"""
        if ctx.is_strong_trend_mode:
            return None
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
        if ctx.allowed_side is not None and side != ctx.allowed_side:
            if ctx.is_latest_bar:
                logging.info(
                    f"🚫 强趋势禁止反转: {signal_type} {side} - "
                    f"趋势={ctx.trend_direction}，Wedge反转在强趋势中胜率<15%"
                )
            return None
        strength = 0.5 + (0.2 if is_strong_reversal_bar else 0.0)
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
        """检测 MTR（Major Trend Reversal）主要趋势反转，利用 BarContext 市场状态。"""
        if ctx.is_strong_trend_mode:
            return None
        result = self.pattern_detector.detect_mtr_reversal(
            data, ctx.i, ctx.ema, ctx.atr, ctx.market_state
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
                    f"趋势={ctx.trend_direction}，MTR 反转在强趋势中不触发"
                )
            return None
        return SignalResult(
            signal_type=signal_type,
            side=side,
            stop_loss=stop_loss,
            base_height=base_height,
            risk_reward=2.0,
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
