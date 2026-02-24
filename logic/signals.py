"""
17 类信号检测函数 — 精确对齐 EA 每个 Check* 函数

每个函数签名统一:
    check_xxx(h, l, o, c, atr, ctx) -> Optional[SignalResult]
    ctx 是 SignalContext dataclass，包含所有需要的状态引用。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from logic.constants import (
    SignalType, SignalResult, AlwaysIn, MarketState, MarketCycle,
    DIR_LONG, DIR_SHORT,
    MIN_SPIKE_BARS, SPIKE_OVERLAP_MAX, SPIKE_CLIMAX_ATR_MULT,
    MAX_STOP_ATR_MULT, NEAR_TRENDLINE_ATR_MULT, REQUIRE_SECOND_ENTRY,
)
from logic.swing_tracker import SwingTracker
from logic.hl_counter import HLCounter
from logic.market_state import MarketStateTracker
from logic.filters import (
    validate_signal_bar, SignalCooldownTracker, GapBar20Rule, HTFFilter,
)


@dataclass
class SignalContext:
    """检测函数所需的只读上下文引用"""
    swings: SwingTracker
    hl: HLCounter
    mstate: MarketStateTracker
    cooldown: SignalCooldownTracker
    gap20: GapBar20Rule
    htf: HTFFilter
    # 趋势线（简化版 — 不单独追踪趋势线对象，MTR 用 swing 替代）
    trend_line_broken: bool = False
    trend_line_price: float = 0.0
    trend_line_break_price: float = 0.0
    # 突破回调
    recent_breakout: bool = False
    breakout_dir: str = ""
    breakout_level: float = 0.0
    breakout_bar_age: int = 0
    # 反转尝试
    reversal_attempt_dir: str = ""
    reversal_attempt_price: float = 0.0
    reversal_attempt_count: int = 0


# ── helpers ────────────────────────────────────────────────────────

def _b(arr, bar: int):
    """EA bar[bar] → numpy 偏移。bar=1 → arr[-2]（最新收盘棒）。"""
    return arr[-1 - bar]


def _validate_and_cool(side: str, h, l, o, c, atr: float, ctx: SignalContext) -> bool:
    return (
        validate_signal_bar(_b(h, 1), _b(l, 1), _b(o, 1), _b(c, 1), side)
        and ctx.cooldown.check(side, _b(c, 1), atr,
                               pd.Series(h), pd.Series(l))
    )


# ── 1. Spike ──────────────────────────────────────────────────────

def _count_spike_bull(h, l, o, c, atr: float, n: int) -> int:
    count = 0
    mx = min(20, n - 2)
    for i in range(2, mx + 1):
        idx = -1 - i
        body = c[idx] - o[idx]
        rng = h[idx] - l[idx]
        if rng <= 0:
            break
        trend = body > 0 and body / rng > 0.50
        if not trend:
            cp = (c[idx] - l[idx]) / rng
            trend = cp > 0.6 and rng > atr * 0.5
        if not trend:
            break
        if i > 2:
            prev = idx + 1
            prev_mid = (h[prev] + l[prev]) / 2.0
            overlap = prev_mid - l[idx]
            prev_rng = h[prev] - l[prev]
            if prev_rng > 0 and overlap / prev_rng > SPIKE_OVERLAP_MAX:
                break
        count += 1
    return count


def _count_spike_bear(h, l, o, c, atr: float, n: int) -> int:
    count = 0
    mx = min(20, n - 2)
    for i in range(2, mx + 1):
        idx = -1 - i
        body = o[idx] - c[idx]
        rng = h[idx] - l[idx]
        if rng <= 0:
            break
        trend = body > 0 and body / rng > 0.50
        if not trend:
            cp = (h[idx] - c[idx]) / rng
            trend = cp > 0.6 and rng > atr * 0.5
        if not trend:
            break
        if i > 2:
            prev = idx + 1
            prev_mid = (h[prev] + l[prev]) / 2.0
            overlap = h[idx] - prev_mid
            prev_rng = h[prev] - l[prev]
            if prev_rng > 0 and overlap / prev_rng > SPIKE_OVERLAP_MAX:
                break
        count += 1
    return count


def check_spike(h, l, o, c, atr: float, ctx: SignalContext) -> Optional[SignalResult]:
    n = len(h)
    if atr <= 0 or n < 8:
        return None
    ai = ctx.mstate.always_in

    bull = _count_spike_bull(h, l, o, c, atr, n)
    if bull >= MIN_SPIKE_BARS:
        if ai == AlwaysIn.SHORT and bull < 5:
            pass
        elif _validate_and_cool("buy", h, l, o, c, atr, ctx) and c[-2] > o[-2]:
            bot = l[-2]
            for i in range(1, bull + 2):
                if -1 - i >= -n and l[-1 - i] < bot:
                    bot = l[-1 - i]
            sl = bot - atr * 0.3
            if c[-2] - sl > atr * MAX_STOP_ATR_MULT:
                rsl = ctx.swings.get_recent_swing_low(1)
                if rsl > 0:
                    sl = rsl - atr * 0.3
                if c[-2] - sl > atr * MAX_STOP_ATR_MULT:
                    return None
            ctx.cooldown.record("buy", c[-2])
            return SignalResult(SignalType.SPIKE_BUY, DIR_LONG, float(c[-2]), sl, reason="Spike")

    bear = _count_spike_bear(h, l, o, c, atr, n)
    if bear >= MIN_SPIKE_BARS:
        if ai == AlwaysIn.LONG and bear < 5:
            return None
        if _validate_and_cool("sell", h, l, o, c, atr, ctx) and c[-2] < o[-2]:
            top = h[-2]
            for i in range(1, bear + 2):
                if -1 - i >= -n and h[-1 - i] > top:
                    top = h[-1 - i]
            sl = top + atr * 0.3
            if sl - c[-2] > atr * MAX_STOP_ATR_MULT:
                rsh = ctx.swings.get_recent_swing_high(1)
                if rsh > 0:
                    sl = rsh + atr * 0.3
                if sl - c[-2] > atr * MAX_STOP_ATR_MULT:
                    return None
            ctx.cooldown.record("sell", c[-2])
            return SignalResult(SignalType.SPIKE_SELL, DIR_SHORT, float(c[-2]), sl, reason="Spike")
    return None


# ── 2. MicroChannel ───────────────────────────────────────────────

def check_micro_channel(h, l, o, c, atr: float, ctx: SignalContext) -> Optional[SignalResult]:
    n = len(h)
    if atr <= 0 or n < 8:
        return None
    ai = ctx.mstate.always_in

    up = 0
    for i in range(2, min(11, n - 1)):
        idx, nxt = -1 - i, -2 - i
        if -nxt > n:
            break
        if h[idx] <= h[nxt] or l[idx] < l[nxt]:
            break
        pr = h[nxt] - l[nxt]
        if pr > 0 and l[idx] < l[nxt] + pr * 0.75:
            break
        up += 1
    if up >= 5 and ai == AlwaysIn.LONG:
        if h[-2] > h[-3] and c[-2] > o[-2]:
            if _validate_and_cool("buy", h, l, o, c, atr, ctx):
                mc_low = l[-3]
                for i in range(2, up + 2):
                    if -1 - i >= -n and l[-1 - i] < mc_low:
                        mc_low = l[-1 - i]
                sl = mc_low - atr * 0.3
                if c[-2] - sl > atr * MAX_STOP_ATR_MULT:
                    sl = min(l[-2], l[-3]) - atr * 0.3
                if c[-2] - sl > atr * MAX_STOP_ATR_MULT:
                    return None
                ctx.cooldown.record("buy", c[-2])
                return SignalResult(SignalType.MICRO_CH_BUY, DIR_LONG, float(c[-2]), sl, reason="MicroCH")

    dn = 0
    for i in range(2, min(11, n - 1)):
        idx, nxt = -1 - i, -2 - i
        if -nxt > n:
            break
        if l[idx] >= l[nxt] or h[idx] > h[nxt]:
            break
        pr = h[nxt] - l[nxt]
        if pr > 0 and h[idx] > h[nxt] - pr * 0.75:
            break
        dn += 1
    if dn >= 5 and ai == AlwaysIn.SHORT:
        if l[-2] < l[-3] and c[-2] < o[-2]:
            if _validate_and_cool("sell", h, l, o, c, atr, ctx):
                mc_high = h[-3]
                for i in range(2, dn + 2):
                    if -1 - i >= -n and h[-1 - i] > mc_high:
                        mc_high = h[-1 - i]
                sl = mc_high + atr * 0.3
                if sl - c[-2] > atr * MAX_STOP_ATR_MULT:
                    sl = max(h[-2], h[-3]) + atr * 0.3
                if sl - c[-2] > atr * MAX_STOP_ATR_MULT:
                    return None
                ctx.cooldown.record("sell", c[-2])
                return SignalResult(SignalType.MICRO_CH_SELL, DIR_SHORT, float(c[-2]), sl, reason="MicroCH")
    return None


# ── 3. HLCount ────────────────────────────────────────────────────

def check_hl_count(h, l, o, c, atr: float, direction: int, ctx: SignalContext) -> Optional[SignalResult]:
    if atr <= 0:
        return None
    count = ctx.hl.h_count if direction == DIR_LONG else ctx.hl.l_count
    need_ai = AlwaysIn.LONG if direction == DIR_LONG else AlwaysIn.SHORT
    if ctx.mstate.always_in != need_ai:
        return None
    side = "buy" if direction == DIR_LONG else "sell"
    if ctx.htf.trend_dir and ((direction == DIR_LONG and ctx.htf.trend_dir == "down") or
                               (direction == DIR_SHORT and ctx.htf.trend_dir == "up")):
        return None
    if ctx.mstate.state == MarketState.TRADING_RANGE:
        return None
    extreme = ctx.hl.h_last_pullback_low if direction == DIR_LONG else ctx.hl.l_last_bounce_high
    sl = (extreme - atr * 0.3) if direction == DIR_LONG else (extreme + atr * 0.3)
    risk = (c[-2] - sl) if direction == DIR_LONG else (sl - c[-2])
    if risk > atr * MAX_STOP_ATR_MULT:
        return None

    if count == 1:
        is_very_strong = (
            (ctx.mstate.state == MarketState.STRONG_TREND and ctx.mstate.trend_strength >= 0.65) or
            ctx.mstate.state == MarketState.TIGHT_CHANNEL
        )
        n = len(c)
        same = 0
        for i in range(1, min(6, n)):
            body = c[-1 - i] - o[-1 - i]
            if (direction == DIR_LONG and body > 0) or (direction == DIR_SHORT and body < 0):
                same += 1
        if not is_very_strong or same < 4:
            return None
        label = "H1" if direction == DIR_LONG else "L1"
        if ctx.gap20.check_block(label):
            return None
    elif count < 2:
        return None

    if not ctx.cooldown.check(side, c[-2], atr, pd.Series(h), pd.Series(l)):
        return None
    if not validate_signal_bar(h[-2], l[-2], o[-2], c[-2], side):
        return None

    ctx.cooldown.record(side, c[-2])
    if direction == DIR_LONG:
        ctx.hl.h_count = 0
        sig = SignalType.H1_BUY if count == 1 else SignalType.H2_BUY
    else:
        ctx.hl.l_count = 0
        sig = SignalType.L1_SELL if count == 1 else SignalType.L2_SELL
    return SignalResult(sig, direction, float(c[-2]), sl, reason=sig.name)


# ── 4. GapBar ─────────────────────────────────────────────────────

def check_gap_bar(h, l, o, c, atr: float, ctx: SignalContext) -> Optional[SignalResult]:
    n = len(h)
    if atr <= 0 or n < 3:
        return None
    gap_thresh = atr * 0.3
    gap_up = o[-2] - h[-3]
    gap_dn = l[-3] - o[-2]

    if gap_up >= gap_thresh and c[-2] > o[-2]:
        if ctx.mstate.always_in == AlwaysIn.LONG and _validate_and_cool("buy", h, l, o, c, atr, ctx):
            sl = min(l[-2], h[-3]) - atr * 0.3
            if c[-2] - sl > atr * MAX_STOP_ATR_MULT:
                return None
            ctx.cooldown.record("buy", c[-2])
            return SignalResult(SignalType.GAP_BAR_BUY, DIR_LONG, float(c[-2]), sl, reason="GapBar")

    if gap_dn >= gap_thresh and c[-2] < o[-2]:
        if ctx.mstate.always_in == AlwaysIn.SHORT and _validate_and_cool("sell", h, l, o, c, atr, ctx):
            sl = max(h[-2], l[-3]) + atr * 0.3
            if sl - c[-2] > atr * MAX_STOP_ATR_MULT:
                return None
            ctx.cooldown.record("sell", c[-2])
            return SignalResult(SignalType.GAP_BAR_SELL, DIR_SHORT, float(c[-2]), sl, reason="GapBar")
    return None


# ── 5. TrendBarEntry ──────────────────────────────────────────────

def check_trend_bar(h, l, o, c, atr: float, ctx: SignalContext) -> Optional[SignalResult]:
    if atr <= 0:
        return None
    rng = h[-2] - l[-2]
    if rng <= 0 or rng < atr * 0.8:
        return None
    body = abs(c[-2] - o[-2])
    if body / rng < 0.70:
        return None
    if c[-2] > o[-2] and ctx.mstate.always_in == AlwaysIn.LONG:
        cp = (c[-2] - l[-2]) / rng
        if cp >= 0.75 and ctx.cooldown.check("buy", c[-2], atr, pd.Series(h), pd.Series(l)):
            sl = l[-2] - atr * 0.3
            if c[-2] - sl > atr * MAX_STOP_ATR_MULT:
                return None
            ctx.cooldown.record("buy", c[-2])
            return SignalResult(SignalType.TREND_BAR_BUY, DIR_LONG, float(c[-2]), sl, reason="TrendBar")
    if c[-2] < o[-2] and ctx.mstate.always_in == AlwaysIn.SHORT:
        cp = (h[-2] - c[-2]) / rng
        if cp >= 0.75 and ctx.cooldown.check("sell", c[-2], atr, pd.Series(h), pd.Series(l)):
            sl = h[-2] + atr * 0.3
            if sl - c[-2] > atr * MAX_STOP_ATR_MULT:
                return None
            ctx.cooldown.record("sell", c[-2])
            return SignalResult(SignalType.TREND_BAR_SELL, DIR_SHORT, float(c[-2]), sl, reason="TrendBar")
    return None


# ── 6. ReversalBarEntry ───────────────────────────────────────────

def check_reversal_bar(h, l, o, c, atr: float, ctx: SignalContext) -> Optional[SignalResult]:
    n = len(h)
    if atr <= 0 or n < 11:
        return None
    rng = h[-2] - l[-2]
    if rng <= 0 or rng < atr * 0.5:
        return None
    body = abs(c[-2] - o[-2])
    ut = h[-2] - max(c[-2], o[-2])
    lt = min(c[-2], o[-2]) - l[-2]
    lb_low = l[-2]
    lb_high = h[-2]
    for i in range(2, min(11, n)):
        if l[-1 - i] < lb_low:
            lb_low = l[-1 - i]
        if h[-1 - i] > lb_high:
            lb_high = h[-1 - i]

    if lt > rng * 0.4 and c[-2] > o[-2] and lt > body:
        drop = h[-2] - lb_low
        if drop >= atr * 1.5 and ctx.cooldown.check("buy", c[-2], atr, pd.Series(h), pd.Series(l)):
            sl = l[-2] - atr * 0.3
            if c[-2] - sl > atr * MAX_STOP_ATR_MULT:
                return None
            ctx.cooldown.record("buy", c[-2])
            return SignalResult(SignalType.REV_BAR_BUY, DIR_LONG, float(c[-2]), sl, reason="RevBar")
    if ut > rng * 0.4 and c[-2] < o[-2] and ut > body:
        rise = lb_high - l[-2]
        if rise >= atr * 1.5 and ctx.cooldown.check("sell", c[-2], atr, pd.Series(h), pd.Series(l)):
            sl = h[-2] + atr * 0.3
            if sl - c[-2] > atr * MAX_STOP_ATR_MULT:
                return None
            ctx.cooldown.record("sell", c[-2])
            return SignalResult(SignalType.REV_BAR_SELL, DIR_SHORT, float(c[-2]), sl, reason="RevBar")
    return None


# ── 7. IIPattern ──────────────────────────────────────────────────

def check_ii_pattern(h, l, o, c, atr: float, ctx: SignalContext) -> Optional[SignalResult]:
    n = len(h)
    if atr <= 0 or n < 7:
        return None
    inside = 0
    p_h = h[-3]
    p_l = l[-3]
    max_check = min(4, n - 3)
    for i in range(2, max_check + 1):
        idx = -1 - i
        m_h = h[idx - 1]
        m_l = l[idx - 1]
        if h[idx] <= m_h and l[idx] >= m_l:
            inside += 1
            if h[idx] > p_h:
                p_h = h[idx]
            if l[idx] < p_l:
                p_l = l[idx]
        else:
            break
    if inside < 2:
        return None
    if h[-2] > p_h and c[-2] > o[-2] and ctx.cooldown.check("buy", c[-2], atr, pd.Series(h), pd.Series(l)):
        sl = p_l - atr * 0.3
        if c[-2] - sl > atr * MAX_STOP_ATR_MULT:
            return None
        ctx.cooldown.record("buy", c[-2])
        return SignalResult(SignalType.II_BUY, DIR_LONG, float(c[-2]), sl, reason="ii")
    if l[-2] < p_l and c[-2] < o[-2] and ctx.cooldown.check("sell", c[-2], atr, pd.Series(h), pd.Series(l)):
        sl = p_h + atr * 0.3
        if sl - c[-2] > atr * MAX_STOP_ATR_MULT:
            return None
        ctx.cooldown.record("sell", c[-2])
        return SignalResult(SignalType.II_SELL, DIR_SHORT, float(c[-2]), sl, reason="ii")
    return None


# ── 8. OutsideBarReversal ─────────────────────────────────────────

def check_outside_bar(h, l, o, c, atr: float, ctx: SignalContext) -> Optional[SignalResult]:
    n = len(h)
    if atr <= 0 or n < 3:
        return None
    rng = h[-2] - l[-2]
    if rng <= 0:
        return None
    if not (h[-2] > h[-3] and l[-2] < l[-3]):
        return None
    body = abs(c[-2] - o[-2])
    if body / rng < 0.40:
        return None
    lb_low = l[-2]
    lb_high = h[-2]
    for i in range(2, min(9, n)):
        if l[-1 - i] < lb_low:
            lb_low = l[-1 - i]
        if h[-1 - i] > lb_high:
            lb_high = h[-1 - i]
    if c[-2] > o[-2]:
        drop = h[-2] - lb_low
        if drop >= atr * 1.0 and ctx.cooldown.check("buy", c[-2], atr, pd.Series(h), pd.Series(l)):
            sl = l[-2] - atr * 0.3
            if c[-2] - sl > atr * MAX_STOP_ATR_MULT:
                return None
            ctx.cooldown.record("buy", c[-2])
            return SignalResult(SignalType.OUTSIDE_BAR_BUY, DIR_LONG, float(c[-2]), sl, reason="OutsideBar")
    if c[-2] < o[-2]:
        rise = lb_high - l[-2]
        if rise >= atr * 1.0 and ctx.cooldown.check("sell", c[-2], atr, pd.Series(h), pd.Series(l)):
            sl = h[-2] + atr * 0.3
            if sl - c[-2] > atr * MAX_STOP_ATR_MULT:
                return None
            ctx.cooldown.record("sell", c[-2])
            return SignalResult(SignalType.OUTSIDE_BAR_SELL, DIR_SHORT, float(c[-2]), sl, reason="OutsideBar")
    return None


# ── 9. MeasuredMove ───────────────────────────────────────────────

def check_measured_move(h, l, o, c, atr: float, ctx: SignalContext) -> Optional[SignalResult]:
    if atr <= 0 or len(ctx.swings.swings) < 4:
        return None
    sh1 = ctx.swings.get_recent_swing_high(1)
    sh2 = ctx.swings.get_recent_swing_high(2)
    sl1 = ctx.swings.get_recent_swing_low(1)
    sl2 = ctx.swings.get_recent_swing_low(2)
    if sh1 <= 0 or sh2 <= 0 or sl1 <= 0 or sl2 <= 0:
        return None
    tol = atr * 0.5
    if sl2 < sl1 and sh2 < sh1:
        leg = sh2 - sl2
        target = sl1 + leg
        if h[-2] >= target - tol and h[-2] <= target + tol:
            if c[-2] < o[-2] and ctx.cooldown.check("sell", c[-2], atr, pd.Series(h), pd.Series(l)):
                sl = h[-2] + atr * 0.3
                if sl - c[-2] > atr * MAX_STOP_ATR_MULT:
                    return None
                ctx.cooldown.record("sell", c[-2])
                return SignalResult(SignalType.MEASURED_MOVE_SELL, DIR_SHORT, float(c[-2]), sl, reason="MM")
    if sh2 > sh1 and sl2 > sl1:
        leg = sh2 - sl2
        target = sh1 - leg
        if l[-2] <= target + tol and l[-2] >= target - tol:
            if c[-2] > o[-2] and ctx.cooldown.check("buy", c[-2], atr, pd.Series(h), pd.Series(l)):
                sl = l[-2] - atr * 0.3
                if c[-2] - sl > atr * MAX_STOP_ATR_MULT:
                    return None
                ctx.cooldown.record("buy", c[-2])
                return SignalResult(SignalType.MEASURED_MOVE_BUY, DIR_LONG, float(c[-2]), sl, reason="MM")
    return None


# ── 10. TRBreakout ────────────────────────────────────────────────

def check_tr_breakout(h, l, o, c, atr: float, ctx: SignalContext) -> Optional[SignalResult]:
    ms = ctx.mstate
    if atr <= 0 or ms.tr_high <= 0 or ms.tr_low <= 0:
        return None
    tr_rng = ms.tr_high - ms.tr_low
    if tr_rng < atr * 1.5:
        return None
    rng = h[-2] - l[-2]
    if rng <= 0:
        return None
    body = abs(c[-2] - o[-2])
    if body / rng < 0.50:
        return None
    if c[-2] > ms.tr_high and c[-2] > o[-2]:
        if ms.always_in != AlwaysIn.SHORT and _validate_and_cool("buy", h, l, o, c, atr, ctx):
            sl = max(l[-2], ms.tr_high - tr_rng * 0.3) - atr * 0.2
            if c[-2] - sl > atr * MAX_STOP_ATR_MULT:
                sl = l[-2] - atr * 0.3
            if c[-2] - sl > atr * MAX_STOP_ATR_MULT:
                return None
            ctx.cooldown.record("buy", c[-2])
            ctx.recent_breakout = True
            ctx.breakout_dir = "up"
            ctx.breakout_level = ms.tr_high
            ctx.breakout_bar_age = 0
            return SignalResult(SignalType.TR_BREAKOUT_BUY, DIR_LONG, float(c[-2]), sl, reason="TRBreakout")
    if c[-2] < ms.tr_low and c[-2] < o[-2]:
        if ms.always_in != AlwaysIn.LONG and _validate_and_cool("sell", h, l, o, c, atr, ctx):
            sl = min(h[-2], ms.tr_low + tr_rng * 0.3) + atr * 0.2
            if sl - c[-2] > atr * MAX_STOP_ATR_MULT:
                sl = h[-2] + atr * 0.3
            if sl - c[-2] > atr * MAX_STOP_ATR_MULT:
                return None
            ctx.cooldown.record("sell", c[-2])
            ctx.recent_breakout = True
            ctx.breakout_dir = "down"
            ctx.breakout_level = ms.tr_low
            ctx.breakout_bar_age = 0
            return SignalResult(SignalType.TR_BREAKOUT_SELL, DIR_SHORT, float(c[-2]), sl, reason="TRBreakout")
    return None


# ── 11. BreakoutPullback ──────────────────────────────────────────

def check_breakout_pullback(h, l, o, c, atr: float, ctx: SignalContext) -> Optional[SignalResult]:
    if atr <= 0 or not ctx.recent_breakout or ctx.breakout_level <= 0:
        return None
    if ctx.breakout_bar_age < 2 or ctx.breakout_bar_age > 8:
        return None
    tol = atr * 0.5
    if ctx.breakout_dir == "up":
        if l[-2] <= ctx.breakout_level + tol and c[-2] > o[-2] and c[-2] > ctx.breakout_level:
            if ctx.cooldown.check("buy", c[-2], atr, pd.Series(h), pd.Series(l)):
                sl = min(l[-2], ctx.breakout_level) - atr * 0.3
                if c[-2] - sl > atr * MAX_STOP_ATR_MULT:
                    return None
                ctx.cooldown.record("buy", c[-2])
                ctx.recent_breakout = False
                return SignalResult(SignalType.BO_PULLBACK_BUY, DIR_LONG, float(c[-2]), sl, reason="BOPullback")
    if ctx.breakout_dir == "down":
        if h[-2] >= ctx.breakout_level - tol and c[-2] < o[-2] and c[-2] < ctx.breakout_level:
            if ctx.cooldown.check("sell", c[-2], atr, pd.Series(h), pd.Series(l)):
                sl = max(h[-2], ctx.breakout_level) + atr * 0.3
                if sl - c[-2] > atr * MAX_STOP_ATR_MULT:
                    return None
                ctx.cooldown.record("sell", c[-2])
                ctx.recent_breakout = False
                return SignalResult(SignalType.BO_PULLBACK_SELL, DIR_SHORT, float(c[-2]), sl, reason="BOPullback")
    return None


# ── 12. Wedge ─────────────────────────────────────────────────────

def check_wedge(h, l, o, c, atr: float, direction: int, ctx: SignalContext) -> Optional[SignalResult]:
    n = len(h)
    if atr <= 0 or n < 10:
        return None
    lookback = min(40, n - 3)
    ext = []
    ext_bars = []
    ext_bodies = []
    for i in range(3, lookback + 1):
        idx = -1 - i
        if -idx - 2 > n or -idx + 2 > n:
            continue
        ei = l[idx] if direction == DIR_LONG else h[idx]
        e1 = l[idx + 1] if direction == DIR_LONG else h[idx + 1]
        e2 = l[idx + 2] if direction == DIR_LONG else h[idx + 2]
        e3 = l[idx - 1] if direction == DIR_LONG else h[idx - 1]
        e4 = l[idx - 2] if direction == DIR_LONG else h[idx - 2]
        is_local = (ei < e1 and ei < e2 and ei < e3 and ei < e4) if direction == DIR_LONG else (ei > e1 and ei > e2 and ei > e3 and ei > e4)
        if not is_local:
            continue
        seq = len(ext) == 0 or (ei < ext[-1] if direction == DIR_LONG else ei > ext[-1])
        if not seq:
            continue
        has_retrace = True
        if ext:
            prev_bar_idx = ext_bars[-1]
            opp = h[idx] if direction == DIR_LONG else l[idx]
            for j_off in range(prev_bar_idx + 1, i):
                jdx = -1 - j_off
                if -jdx > n:
                    break
                if direction == DIR_LONG and h[jdx] > opp:
                    opp = h[jdx]
                if direction == DIR_SHORT and l[jdx] < opp:
                    opp = l[jdx]
            retrace = (opp - ext[-1]) if direction == DIR_LONG else (ext[-1] - opp)
            if retrace < atr * 0.3:
                has_retrace = False
        if not has_retrace:
            continue
        max_body = 0.0
        start_j = ext_bars[-1] if ext_bars else min(i + 5, n - 1)
        for j_off in range(i, start_j + 1):
            jdx = -1 - j_off
            if -jdx > n:
                break
            b = (o[jdx] - c[jdx]) if direction == DIR_LONG else (c[jdx] - o[jdx])
            if b > max_body:
                max_body = b
        ext.append(ei)
        ext_bars.append(i)
        ext_bodies.append(max_body)
        if len(ext) >= 3:
            break

    if len(ext) < 3:
        return None
    if not (ext_bodies[0] > ext_bodies[1] and ext_bodies[1] > ext_bodies[2]):
        return None
    curr_ext = l[-2] if direction == DIR_LONG else h[-2]
    if abs(curr_ext - ext[2]) > atr * NEAR_TRENDLINE_ATR_MULT:
        return None
    rng = h[-2] - l[-2]
    if rng <= 0:
        return None
    bar_dir = (c[-2] > o[-2]) if direction == DIR_LONG else (c[-2] < o[-2])
    cp = ((c[-2] - l[-2]) / rng) if direction == DIR_LONG else ((h[-2] - c[-2]) / rng)
    if not bar_dir or cp < 0.50:
        return None
    side = "buy" if direction == DIR_LONG else "sell"
    if not ctx.cooldown.check(side, c[-2], atr, pd.Series(h), pd.Series(l)):
        return None
    sl = ext[2] - direction * atr * 0.5
    ctx.cooldown.record(side, c[-2])
    sig = SignalType.WEDGE_BUY if direction == DIR_LONG else SignalType.WEDGE_SELL
    return SignalResult(sig, direction, float(c[-2]), sl, reason="Wedge")


# ── 13. Climax ────────────────────────────────────────────────────

def check_climax(h, l, o, c, atr: float, ctx: SignalContext) -> Optional[SignalResult]:
    n = len(h)
    if atr <= 0 or n < 12:
        return None
    strict = ctx.mstate.cycle == MarketCycle.SPIKE
    mult = SPIKE_CLIMAX_ATR_MULT if strict else 2.5

    # bar[2] = climax, bar[1] = reversal
    p_rng = h[-3] - l[-3]
    p_body = abs(c[-3] - o[-3])
    c_rng = h[-2] - l[-2]
    c_body = abs(c[-2] - o[-2])
    if c_rng <= 0 or p_body <= 0:
        return None

    # up climax → sell
    if p_rng > atr * mult and c[-3] > o[-3]:
        if c[-2] < o[-2] and c[-2] < c[-3]:
            lt = min(o[-2], c[-2]) - l[-2]
            if c_rng > 0 and lt / c_rng > 0.25:
                pass
            else:
                lb_low = l[-4] if n > 4 else l[-3]
                for i in range(3, min(11, n)):
                    if l[-1 - i] < lb_low:
                        lb_low = l[-1 - i]
                prior = h[-3] - lb_low
                min_prior = atr * 4.0 if strict else atr * 2.0
                if prior >= min_prior:
                    if ctx.cooldown.check("sell", c[-2], atr, pd.Series(h), pd.Series(l)):
                        sl = _calc_sl_sell(h, l, atr, ctx)
                        if sl > 0:
                            ctx.cooldown.record("sell", c[-2])
                            return SignalResult(SignalType.CLIMAX_SELL, DIR_SHORT, float(c[-2]), sl, reason="Climax")

    # down climax → buy
    if p_rng > atr * mult and c[-3] < o[-3]:
        if c[-2] > o[-2] and c[-2] > c[-3]:
            ut = h[-2] - max(o[-2], c[-2])
            if c_rng > 0 and ut / c_rng > 0.25:
                pass
            else:
                lb_high = h[-4] if n > 4 else h[-3]
                for i in range(3, min(11, n)):
                    if h[-1 - i] > lb_high:
                        lb_high = h[-1 - i]
                prior = lb_high - l[-3]
                min_prior = atr * 4.0 if strict else atr * 2.0
                if prior >= min_prior:
                    if ctx.cooldown.check("buy", c[-2], atr, pd.Series(h), pd.Series(l)):
                        sl = _calc_sl_buy(h, l, atr, ctx)
                        if sl > 0:
                            ctx.cooldown.record("buy", c[-2])
                            return SignalResult(SignalType.CLIMAX_BUY, DIR_LONG, float(c[-2]), sl, reason="Climax")
    return None


def _calc_sl_buy(h, l, atr, ctx):
    """CalculateUnifiedStopLoss 简化版 — buy"""
    strong = ctx.mstate.state in (MarketState.STRONG_TREND, MarketState.BREAKOUT, MarketState.TIGHT_CHANNEL)
    buf = (atr * 0.3 if strong else atr * 0.5)
    buf = max(buf, atr * 0.2)
    if strong:
        sl = min(l[-2], l[-3]) - buf
    else:
        sw = ctx.swings.get_recent_swing_low(1, allow_temp=True)
        if sw > 0 and (l[-2] - (sw - buf)) <= atr * MAX_STOP_ATR_MULT:
            sl = sw - buf
        else:
            sl = min(l[-2], l[-3]) - buf
    entry = l[-2]
    return sl if (entry - sl) <= atr * MAX_STOP_ATR_MULT else 0.0


def _calc_sl_sell(h, l, atr, ctx):
    strong = ctx.mstate.state in (MarketState.STRONG_TREND, MarketState.BREAKOUT, MarketState.TIGHT_CHANNEL)
    buf = (atr * 0.3 if strong else atr * 0.5)
    buf = max(buf, atr * 0.2)
    if strong:
        sl = max(h[-2], h[-3]) + buf
    else:
        sw = ctx.swings.get_recent_swing_high(1, allow_temp=True)
        sl = (sw + buf) if (sw > 0 and (sw + buf - h[-2]) <= atr * MAX_STOP_ATR_MULT) else (max(h[-2], h[-3]) + buf)
    return sl if (sl - h[-2]) <= atr * MAX_STOP_ATR_MULT else 0.0


# ── 14. MTR ───────────────────────────────────────────────────────

def check_mtr(h, l, o, c, atr: float, ctx: SignalContext) -> Optional[SignalResult]:
    if atr <= 0 or not ctx.trend_line_broken:
        return None
    rng = h[-2] - l[-2]
    if rng <= 0:
        return None

    # Sell MTR: 上升趋势线被突破
    if ctx.mstate.trend_direction == "up" or ctx.mstate.always_in == AlwaysIn.LONG:
        sh1 = ctx.swings.get_recent_swing_high(1)
        sh2 = ctx.swings.get_recent_swing_high(2)
        if sh1 > 0 and sh2 > 0 and sh1 < sh2:
            if c[-2] < o[-2]:
                cp = (h[-2] - c[-2]) / rng
                if cp >= 0.5 and _validate_and_cool("sell", h, l, o, c, atr, ctx):
                    sl = sh1 + atr * 0.5
                    if sl - c[-2] > atr * MAX_STOP_ATR_MULT:
                        return None
                    ctx.cooldown.record("sell", c[-2])
                    ctx.trend_line_broken = False
                    return SignalResult(SignalType.MTR_SELL, DIR_SHORT, float(c[-2]), sl, reason="MTR")

    # Buy MTR: 下降趋势线被突破
    if ctx.mstate.trend_direction == "down" or ctx.mstate.always_in == AlwaysIn.SHORT:
        sl1 = ctx.swings.get_recent_swing_low(1)
        sl2 = ctx.swings.get_recent_swing_low(2)
        if sl1 > 0 and sl2 > 0 and sl1 > sl2:
            if c[-2] > o[-2]:
                cp = (c[-2] - l[-2]) / rng
                if cp >= 0.5 and _validate_and_cool("buy", h, l, o, c, atr, ctx):
                    sl = sl1 - atr * 0.5
                    if c[-2] - sl > atr * MAX_STOP_ATR_MULT:
                        return None
                    ctx.cooldown.record("buy", c[-2])
                    ctx.trend_line_broken = False
                    return SignalResult(SignalType.MTR_BUY, DIR_LONG, float(c[-2]), sl, reason="MTR")
    return None


# ── 15. FailedBreakout ────────────────────────────────────────────

def check_failed_breakout(h, l, o, c, atr: float, ctx: SignalContext) -> Optional[SignalResult]:
    ms = ctx.mstate
    if atr <= 0 or ms.tr_high <= 0 or ms.tr_low <= 0:
        return None
    tr_rng = ms.tr_high - ms.tr_low
    if tr_rng < atr * 1.0:
        return None
    rng = h[-2] - l[-2]
    if rng <= 0:
        return None
    # 向上突破失败 → sell
    if h[-2] > ms.tr_high and c[-2] < ms.tr_high and c[-2] < o[-2]:
        cp = (h[-2] - c[-2]) / rng
        if cp >= 0.60 and ctx.cooldown.check("sell", c[-2], atr, pd.Series(h), pd.Series(l)):
            sl = h[-2] + atr * 0.3
            if sl - c[-2] > atr * MAX_STOP_ATR_MULT:
                return None
            ctx.cooldown.record("sell", c[-2])
            return SignalResult(SignalType.FAILED_BO_SELL, DIR_SHORT, float(c[-2]), sl, reason="FailedBO")
    # 向下突破失败 → buy
    if l[-2] < ms.tr_low and c[-2] > ms.tr_low and c[-2] > o[-2]:
        cp = (c[-2] - l[-2]) / rng
        if cp >= 0.60 and ctx.cooldown.check("buy", c[-2], atr, pd.Series(h), pd.Series(l)):
            sl = l[-2] - atr * 0.3
            if c[-2] - sl > atr * MAX_STOP_ATR_MULT:
                return None
            ctx.cooldown.record("buy", c[-2])
            return SignalResult(SignalType.FAILED_BO_BUY, DIR_LONG, float(c[-2]), sl, reason="FailedBO")
    return None


# ── 16. DoubleTopBottom ───────────────────────────────────────────

def check_double_top_bottom(h, l, o, c, atr: float, direction: int, ctx: SignalContext) -> Optional[SignalResult]:
    if atr <= 0 or len(ctx.swings.swings) < 4:
        return None
    lv1 = ctx.swings.get_recent_swing_low(1) if direction == DIR_LONG else ctx.swings.get_recent_swing_high(1)
    lv2 = ctx.swings.get_recent_swing_low(2) if direction == DIR_LONG else ctx.swings.get_recent_swing_high(2)
    if lv1 <= 0 or lv2 <= 0:
        return None
    rng = h[-2] - l[-2]
    if rng <= 0:
        return None
    tol = atr * 0.3
    if abs(lv1 - lv2) > tol:
        return None
    curr_ext = l[-2] if direction == DIR_LONG else h[-2]
    level_ok = (curr_ext <= lv1 + tol) if direction == DIR_LONG else (curr_ext >= lv1 - tol)
    bar_dir = (c[-2] > o[-2]) if direction == DIR_LONG else (c[-2] < o[-2])
    cp = ((c[-2] - l[-2]) / rng) if direction == DIR_LONG else ((h[-2] - c[-2]) / rng)
    if not level_ok or not bar_dir or cp < 0.55:
        return None
    side = "buy" if direction == DIR_LONG else "sell"
    if not ctx.cooldown.check(side, c[-2], atr, pd.Series(h), pd.Series(l)):
        return None
    sl = (min(lv1, lv2) - atr * 0.3) if direction == DIR_LONG else (max(lv1, lv2) + atr * 0.3)
    risk = (c[-2] - sl) if direction == DIR_LONG else (sl - c[-2])
    if risk > atr * MAX_STOP_ATR_MULT:
        return None
    ctx.cooldown.record(side, c[-2])
    sig = SignalType.DT_BUY if direction == DIR_LONG else SignalType.DT_SELL
    return SignalResult(sig, direction, float(c[-2]), sl, reason="DT/DB")


# ── 17. FinalFlag ─────────────────────────────────────────────────

def check_final_flag(h, l, o, c, atr: float, ctx: SignalContext) -> Optional[SignalResult]:
    if ctx.mstate.state != MarketState.FINAL_FLAG or atr <= 0:
        return None
    rng = h[-2] - l[-2]
    if rng <= 0:
        return None
    tc_dir = ctx.mstate.tight_channel_dir
    tc_ext = ctx.mstate.tight_channel_extreme
    if tc_dir == "up" and c[-2] < o[-2]:
        cp = (h[-2] - c[-2]) / rng
        if cp >= 0.60 and _validate_and_cool("sell", h, l, o, c, atr, ctx):
            sl = (tc_ext + atr * 0.5) if tc_ext > 0 else (h[-2] + atr * 0.5)
            ctx.cooldown.record("sell", c[-2])
            return SignalResult(SignalType.FINAL_FLAG_SELL, DIR_SHORT, float(c[-2]), sl, reason="FinalFlag")
    if tc_dir == "down" and c[-2] > o[-2]:
        cp = (c[-2] - l[-2]) / rng
        if cp >= 0.60 and _validate_and_cool("buy", h, l, o, c, atr, ctx):
            sl = (tc_ext - atr * 0.5) if tc_ext > 0 else (l[-2] - atr * 0.5)
            ctx.cooldown.record("buy", c[-2])
            return SignalResult(SignalType.FINAL_FLAG_BUY, DIR_LONG, float(c[-2]), sl, reason="FinalFlag")
    return None
