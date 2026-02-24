"""
ScanMarket — 严格按 EA 优先级顺序扫描信号

对给定方向 (DIR_LONG / DIR_SHORT) 依次检测 17 类信号，返回第一个有效信号。
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from logic.constants import (
    SignalResult, SignalType, MarketState, MarketCycle,
    DIR_LONG, DIR_SHORT, signal_side,
    ENABLE_SPIKE, ENABLE_H2L2, ENABLE_WEDGE, ENABLE_CLIMAX,
    ENABLE_MTR, ENABLE_FAILED_BO, ENABLE_DTDB, ENABLE_TREND_BAR,
    ENABLE_REV_BAR, ENABLE_II_PATTERN, ENABLE_OUTSIDE_BAR,
    ENABLE_MEASURED_MOVE, ENABLE_TR_BREAKOUT, ENABLE_BO_PULLBACK,
    ENABLE_GAP_BAR, REVERSAL_ALLOWED_STATES,
)
from logic.signals import (
    SignalContext,
    check_spike, check_micro_channel, check_hl_count,
    check_breakout_pullback, check_trend_bar, check_gap_bar,
    check_tr_breakout, check_climax, check_wedge, check_mtr,
    check_failed_breakout, check_double_top_bottom,
    check_outside_bar, check_reversal_bar, check_ii_pattern,
    check_measured_move, check_final_flag,
)


def scan_market(
    direction: int,
    highs: pd.Series,
    lows: pd.Series,
    opens: pd.Series,
    closes: pd.Series,
    atr: float,
    is_ttr: bool,
    ctx: SignalContext,
) -> Optional[SignalResult]:
    h = highs.values
    l = lows.values
    o = opens.values
    c = closes.values
    want = "buy" if direction == DIR_LONG else "sell"

    def _match(r: Optional[SignalResult]) -> Optional[SignalResult]:
        if r is not None and signal_side(r.signal_type) == want:
            return r
        return None

    # 1. Spike (非TTR)
    if not is_ttr and ENABLE_SPIKE:
        r = _match(check_spike(h, l, o, c, atr, ctx))
        if r:
            return r

    # 2. MicroChannel (非TTR)
    if not is_ttr:
        r = _match(check_micro_channel(h, l, o, c, atr, ctx))
        if r:
            return r

    # 3. H/L Count
    if ENABLE_H2L2:
        r = check_hl_count(h, l, o, c, atr, direction, ctx)
        if r:
            return r

    # 4. BreakoutPullback (非TTR)
    if not is_ttr and ENABLE_BO_PULLBACK:
        r = _match(check_breakout_pullback(h, l, o, c, atr, ctx))
        if r:
            return r

    # 5. TrendBarEntry (非TTR)
    if not is_ttr and ENABLE_TREND_BAR:
        r = _match(check_trend_bar(h, l, o, c, atr, ctx))
        if r:
            return r

    # 6. GapBar (非TTR)
    if not is_ttr and ENABLE_GAP_BAR:
        r = _match(check_gap_bar(h, l, o, c, atr, ctx))
        if r:
            return r

    # 7. TRBreakout (仅 TradingRange)
    if ENABLE_TR_BREAKOUT and ctx.mstate.state == MarketState.TRADING_RANGE:
        r = _match(check_tr_breakout(h, l, o, c, atr, ctx))
        if r:
            return r

    allow_rev = (
        ctx.mstate.state in REVERSAL_ALLOWED_STATES
        or ctx.mstate.cycle == MarketCycle.SPIKE
    )

    # 8. Climax
    if ENABLE_CLIMAX:
        r = _match(check_climax(h, l, o, c, atr, ctx))
        if r:
            return r

    # 9. Wedge (允许反转)
    if ENABLE_WEDGE and allow_rev:
        r = check_wedge(h, l, o, c, atr, direction, ctx)
        if r:
            return r

    # 10. MTR (允许反转)
    if ENABLE_MTR and allow_rev:
        r = _match(check_mtr(h, l, o, c, atr, ctx))
        if r:
            return r

    # 11. FailedBreakout (仅 TradingRange)
    if ENABLE_FAILED_BO and ctx.mstate.state == MarketState.TRADING_RANGE:
        r = _match(check_failed_breakout(h, l, o, c, atr, ctx))
        if r:
            return r

    # 12. DoubleTopBottom (允许反转)
    if ENABLE_DTDB and allow_rev:
        r = check_double_top_bottom(h, l, o, c, atr, direction, ctx)
        if r:
            return r

    # 13. OutsideBar (允许反转)
    if ENABLE_OUTSIDE_BAR and allow_rev:
        r = _match(check_outside_bar(h, l, o, c, atr, ctx))
        if r:
            return r

    # 14. ReversalBar (允许反转)
    if ENABLE_REV_BAR and allow_rev:
        r = _match(check_reversal_bar(h, l, o, c, atr, ctx))
        if r:
            return r

    # 15. IIPattern (允许反转)
    if ENABLE_II_PATTERN and allow_rev:
        r = _match(check_ii_pattern(h, l, o, c, atr, ctx))
        if r:
            return r

    # 16. MeasuredMove
    if ENABLE_MEASURED_MOVE:
        r = _match(check_measured_move(h, l, o, c, atr, ctx))
        if r:
            return r

    # 17. FinalFlag (仅 FinalFlag)
    if ctx.mstate.state == MarketState.FINAL_FLAG:
        r = _match(check_final_flag(h, l, o, c, atr, ctx))
        if r:
            return r

    return None
