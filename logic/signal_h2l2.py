"""
策略 H2/L2 状态机信号处理

供 strategy 通过 H2L2Processor 统一调用，冷却期与 Delta 调节由回调注入。
"""

import logging
from typing import Optional, Tuple, Callable

import pandas as pd

from .market_analyzer import MarketCycle
from .patterns import PatternDetector
from .htf_filter import HTFFilter, HTFTrend
from .signal_models import BarContext, SignalResult
from .state_machines import H2StateMachine, L2StateMachine


# DeltaSnapshot 类型由调用方传入，此处用 type hint 字符串避免循环依赖 delta_flow
def _noop_cooldown(_st: str, _side: str, _bar: int, _latest: bool) -> bool:
    return False


def _default_delta_modifier(_snapshot, _side: str, _pct: float) -> Tuple[float, str]:
    return (1.0, "")


class H2L2Processor:
    """
    H2/L2 状态机信号处理器：冷却期与 Delta 调节通过回调注入。
    """

    def __init__(
        self,
        htf_filter: HTFFilter,
        pattern_detector: PatternDetector,
        check_signal_cooldown: Optional[Callable[[str, str, int, bool], bool]] = None,
        calculate_delta_modifier: Optional[Callable[..., Tuple[float, str]]] = None,
    ):
        self.htf_filter = htf_filter
        self.pattern_detector = pattern_detector
        self._check_cooldown = check_signal_cooldown or _noop_cooldown
        self._calc_delta = calculate_delta_modifier or _default_delta_modifier

    def validate_h2l2_signal_bar(
        self, ctx: BarContext, data: pd.DataFrame, signal_side: str, row_index: int
    ) -> Tuple[bool, str]:
        """H2/L2 信号棒质量校验：TradingRange 时放宽参数。返回 (bar_valid, bar_reason)。"""
        if ctx.market_cycle == MarketCycle.TRADING_RANGE:
            return self.pattern_detector.validate_btc_signal_bar(
                data.iloc[row_index], signal_side, min_body_ratio=0.40, close_position_pct=0.35
            )
        return self.pattern_detector.validate_btc_signal_bar(data.iloc[row_index], signal_side)

    async def process_h2_signal(
        self,
        h2_machine: H2StateMachine,
        data: pd.DataFrame,
        ctx: BarContext,
        cached_delta_snapshot: Optional[object],
        htf_trend: HTFTrend,
    ) -> Optional[SignalResult]:
        """处理 H2 状态机信号。cached_delta_snapshot 为 DeltaSnapshot 或 None。"""
        h2_signal = h2_machine.update(
            ctx.close, ctx.high, ctx.low, ctx.ema, ctx.atr, data, ctx.i,
            self.pattern_detector.calculate_unified_stop_loss,
        )
        if not h2_signal:
            return None
        if self._check_cooldown(h2_signal.signal_type, h2_signal.side, ctx.i, ctx.is_latest_bar):
            return None
        allowed, reason = self.htf_filter.allows_h2_buy(ctx.close)
        if not allowed:
            if ctx.is_latest_bar:
                logging.info(f"🚫 H2 背景过滤: {reason}")
            return None
        bar_valid, bar_reason = self.validate_h2l2_signal_bar(ctx, data, h2_signal.side, ctx.i)
        if not bar_valid:
            if ctx.is_latest_bar:
                logging.info(f"🚫 H2信号棒质量不合格: {h2_signal.signal_type} - {bar_reason}")
            return None
        delta_modifier = 1.0
        if cached_delta_snapshot is not None and getattr(cached_delta_snapshot, "trade_count", 0) > 0:
            delta_ratio = getattr(cached_delta_snapshot, "delta_ratio", 0.0)
            if delta_ratio < -0.3:
                if ctx.is_latest_bar:
                    logging.info(
                        f"🚫 H2 Delta强烈反向: {h2_signal.signal_type} - "
                        f"买入信号但Delta={delta_ratio:.2f}<-0.3，强卖压"
                    )
                return None
            elif delta_ratio < 0:
                delta_modifier = 0.7
                if ctx.is_latest_bar:
                    logging.info(
                        f"⚠️ H2 Delta轻微反向: {h2_signal.signal_type} - "
                        f"Delta={delta_ratio:.2f}，信号减弱"
                    )
            else:
                kline_open = data.iloc[ctx.i]["open"]
                price_change_pct = ((ctx.close - kline_open) / kline_open * 100) if kline_open > 0 else 0.0
                delta_modifier, delta_reason = self._calc_delta(
                    cached_delta_snapshot, h2_signal.side, price_change_pct
                )
                if delta_modifier == 0.0:
                    if ctx.is_latest_bar:
                        logging.info(f"🚫 H2 Delta阻止: {h2_signal.signal_type} - {delta_reason}")
                    return None
                elif ctx.is_latest_bar and delta_modifier != 1.0:
                    logging.info(
                        f"{'✅' if delta_modifier > 1 else '⚠️'} H2 Delta{'增强' if delta_modifier > 1 else '减弱'}: "
                        f"{h2_signal.signal_type} (调节={delta_modifier:.2f}) - {delta_reason}"
                    )
        if htf_trend == HTFTrend.BULLISH:
            delta_modifier *= 1.2
            if ctx.is_latest_bar:
                logging.info(f"✅ H2 HTF增强: 1h上升趋势，买入信号增强 x1.2")
        return SignalResult(
            signal_type=h2_signal.signal_type,
            side=h2_signal.side,
            stop_loss=h2_signal.stop_loss,
            base_height=h2_signal.base_height,
            delta_modifier=delta_modifier,
            risk_reward=2.0,
        )

    async def process_l2_signal(
        self,
        l2_machine: L2StateMachine,
        data: pd.DataFrame,
        ctx: BarContext,
        cached_delta_snapshot: Optional[object],
        htf_trend: HTFTrend,
    ) -> Optional[SignalResult]:
        """处理 L2 状态机信号。"""
        l2_signal = l2_machine.update(
            ctx.close, ctx.high, ctx.low, ctx.ema, ctx.atr, data, ctx.i,
            self.pattern_detector.calculate_unified_stop_loss,
        )
        if not l2_signal:
            return None
        if self._check_cooldown(l2_signal.signal_type, l2_signal.side, ctx.i, ctx.is_latest_bar):
            return None
        allowed, reason = self.htf_filter.allows_l2_sell(ctx.close)
        if not allowed:
            if ctx.is_latest_bar:
                logging.info(f"🚫 L2 背景过滤: {reason}")
            return None
        bar_valid, bar_reason = self.validate_h2l2_signal_bar(ctx, data, l2_signal.side, ctx.i)
        if not bar_valid:
            if ctx.is_latest_bar:
                logging.info(f"🚫 L2信号棒质量不合格: {l2_signal.signal_type} - {bar_reason}")
            return None
        delta_modifier = 1.0
        if cached_delta_snapshot is not None and getattr(cached_delta_snapshot, "trade_count", 0) > 0:
            delta_ratio = getattr(cached_delta_snapshot, "delta_ratio", 0.0)
            if delta_ratio > 0.3:
                if ctx.is_latest_bar:
                    logging.info(
                        f"🚫 L2 Delta强烈反向: {l2_signal.signal_type} - "
                        f"卖出信号但Delta={delta_ratio:.2f}>0.3，强买压"
                    )
                return None
            elif delta_ratio > 0:
                delta_modifier = 0.7
                if ctx.is_latest_bar:
                    logging.info(
                        f"⚠️ L2 Delta轻微反向: {l2_signal.signal_type} - "
                        f"Delta={delta_ratio:.2f}，信号减弱"
                    )
            else:
                kline_open = data.iloc[ctx.i]["open"]
                price_change_pct = ((ctx.close - kline_open) / kline_open * 100) if kline_open > 0 else 0.0
                delta_modifier, delta_reason = self._calc_delta(
                    cached_delta_snapshot, l2_signal.side, price_change_pct
                )
                if delta_modifier == 0.0:
                    if ctx.is_latest_bar:
                        logging.info(f"🚫 L2 Delta阻止: {l2_signal.signal_type} - {delta_reason}")
                    return None
                elif ctx.is_latest_bar and delta_modifier != 1.0:
                    logging.info(
                        f"{'✅' if delta_modifier > 1 else '⚠️'} L2 Delta{'增强' if delta_modifier > 1 else '减弱'}: "
                        f"{l2_signal.signal_type} (调节={delta_modifier:.2f}) - {delta_reason}"
                    )
        if htf_trend == HTFTrend.BEARISH:
            delta_modifier *= 1.2
            if ctx.is_latest_bar:
                logging.info(f"✅ L2 HTF增强: 1h下降趋势，卖出信号增强 x1.2")
        return SignalResult(
            signal_type=l2_signal.signal_type,
            side=l2_signal.side,
            stop_loss=l2_signal.stop_loss,
            base_height=l2_signal.base_height,
            delta_modifier=delta_modifier,
            risk_reward=2.0,
        )
