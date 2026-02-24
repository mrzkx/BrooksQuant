"""
过滤器集合 — 精确对齐 EA

包含：BarbWire / TTR / 20 Gap Bar Rule / HTF Filter /
      SpreadFilter / ValidateSignalBar / SignalCooldown /
      MeasuringGap / BreakoutMode / GapCount
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from logic.constants import (
    MarketState, MeasuringGapInfo,
    ENABLE_BARB_WIRE_FILTER, BARB_WIRE_MIN_BARS, BARB_WIRE_BODY_RATIO,
    BARB_WIRE_RANGE_RATIO, ENABLE_20_GAP_RULE, GAP_BAR_THRESHOLD,
    CONSOLIDATION_BARS, CONSOLIDATION_RANGE,
    ENABLE_MEASURING_GAP, MEASURING_GAP_MIN_SIZE,
    ENABLE_BREAKOUT_MODE, BREAKOUT_MODE_BARS, BREAKOUT_MODE_ATR_MULT,
    MIN_BODY_RATIO, CLOSE_POSITION_PCT, SIGNAL_COOLDOWN,
    ENABLE_HTF_FILTER, ENABLE_SPREAD_FILTER, MAX_SPREAD_MULT,
    SPREAD_LOOKBACK,
)


# ── ValidateSignalBar ─────────────────────────────────────────────

def validate_signal_bar(
    h1: float, l1: float, o1: float, c1: float, side: str,
) -> bool:
    rng = h1 - l1
    if rng <= 0:
        return False
    body = abs(c1 - o1)
    if body / rng < MIN_BODY_RATIO:
        return False
    if side == "buy" and c1 <= o1:
        return False
    if side == "sell" and c1 >= o1:
        return False
    upper_tail = h1 - max(c1, o1)
    lower_tail = min(c1, o1) - l1
    if side == "buy" and upper_tail / rng > CLOSE_POSITION_PCT:
        return False
    if side == "sell" and lower_tail / rng > CLOSE_POSITION_PCT:
        return False
    return True


# ── BarbWire ──────────────────────────────────────────────────────

@dataclass
class BarbWireFilter:
    active: bool = False
    bar_count: int = 0
    bw_high: float = 0.0
    bw_low: float = 0.0

    def update(self, highs: pd.Series, lows: pd.Series,
               opens: pd.Series, closes: pd.Series, atr: float) -> None:
        if not ENABLE_BARB_WIRE_FILTER or atr <= 0:
            self.active = False
            return
        n = len(highs)
        if n < BARB_WIRE_MIN_BARS + 2:
            self.active = False
            return

        h = highs.values
        l = lows.values
        o = opens.values
        c = closes.values

        small = doji = overlap = 0
        rh = h[-2]
        rl = l[-2]
        check = BARB_WIRE_MIN_BARS + 2
        for i in range(1, min(check + 1, n)):
            idx = -1 - i
            rng = h[idx] - l[idx]
            body = abs(c[idx] - o[idx])
            if rng <= 0:
                continue
            if h[idx] > rh:
                rh = h[idx]
            if l[idx] < rl:
                rl = l[idx]
            is_small = rng < atr * BARB_WIRE_RANGE_RATIO or body / rng < BARB_WIRE_BODY_RATIO
            is_doji = body / rng < 0.15
            if is_small:
                small += 1
            if is_doji:
                doji += 1
            if i > 1:
                prev = idx + 1
                ov_h = min(h[idx], h[prev])
                ov_l = max(l[idx], l[prev])
                if ov_h > ov_l and rng > 0 and (ov_h - ov_l) / rng > 0.5:
                    overlap += 1

        total_rng = rh - rl
        high_overlap = total_rng < atr * 1.5 or overlap >= BARB_WIRE_MIN_BARS - 1

        if small >= BARB_WIRE_MIN_BARS and doji >= 1 and high_overlap:
            if not self.active:
                self.active = True
                self.bar_count = 0
                self.bw_high = rh
                self.bw_low = rl
            self.bar_count += 1
            if h[-2] > self.bw_high:
                self.bw_high = float(h[-2])
            if l[-2] < self.bw_low:
                self.bw_low = float(l[-2])
        else:
            if self.active:
                cc = c[-2]
                cr = h[-2] - l[-2]
                cb = abs(c[-2] - o[-2])
                strong = cr > atr * 0.5 and cr > 0 and cb / cr > 0.5
                bo_up = cc > self.bw_high and strong and c[-2] > o[-2]
                bo_dn = cc < self.bw_low and strong and c[-2] < o[-2]
                self._breakout_dir = "up" if bo_up else ("down" if bo_dn else "")
                self.active = False
                self.bar_count = 0
            else:
                self._breakout_dir = ""

    _breakout_dir: str = ""

    @property
    def breakout_direction(self) -> str:
        return self._breakout_dir


# ── 20 Gap Bar Rule ───────────────────────────────────────────────

@dataclass
class GapBar20Rule:
    gap_count: int = 0
    gap_count_extreme: float = 0.0
    is_overextended: bool = False
    overextend_dir: str = ""
    first_pullback_blocked: bool = False
    waiting_for_recovery: bool = False
    first_pullback_complete: bool = False
    consolidation_count: int = 0
    pullback_extreme: float = 0.0

    def calculate_gap_count(
        self, closes: pd.Series, lows: pd.Series, highs: pd.Series,
        ema: pd.Series, atr: float,
    ) -> int:
        if atr <= 0:
            return 0
        n = len(closes)
        threshold = atr * 0.3
        c1 = closes.iloc[-2]
        e1 = ema.iloc[-2]
        above = c1 > e1 + threshold
        below = c1 < e1 - threshold
        if not above and not below:
            self.gap_count = 0
            self.gap_count_extreme = 0.0
            return 0

        extreme = float('-inf') if above else float('inf')
        count = 0
        maxlb = min(50, n - 1)
        for i in range(1, maxlb + 1):
            idx = -1 - i
            bar_ema = ema.iloc[idx]
            if above:
                if lows.iloc[idx] > bar_ema:
                    count += 1
                    if highs.iloc[idx] > extreme:
                        extreme = float(highs.iloc[idx])
                else:
                    break
            else:
                if highs.iloc[idx] < bar_ema:
                    count += 1
                    if lows.iloc[idx] < extreme:
                        extreme = float(lows.iloc[idx])
                else:
                    break
        self.gap_count = count
        self.gap_count_extreme = extreme
        return count

    def update(
        self, closes: pd.Series, highs: pd.Series, lows: pd.Series,
        opens: pd.Series, ema: pd.Series, atr: float,
    ) -> None:
        if not ENABLE_20_GAP_RULE or atr <= 0:
            return
        n = len(closes)
        threshold = atr * 0.3
        c1 = closes.iloc[-2]
        e1 = ema.iloc[-2]
        above = c1 > e1 + threshold
        below = c1 < e1 - threshold
        touching = not above and not below

        if not self.is_overextended and self.gap_count >= GAP_BAR_THRESHOLD:
            self.is_overextended = True
            self.overextend_dir = "up" if above else "down"
            self.first_pullback_blocked = False
            self.waiting_for_recovery = False
            self.first_pullback_complete = False
            self.consolidation_count = 0
            self.pullback_extreme = 0.0

        if self.is_overextended:
            if not self.first_pullback_complete and touching:
                if not self.first_pullback_blocked:
                    self.first_pullback_blocked = True
                    self.waiting_for_recovery = True
                    self.pullback_extreme = float(
                        lows.iloc[-2] if self.overextend_dir == "up" else highs.iloc[-2]
                    )
                self.consolidation_count += 1

            if self.waiting_for_recovery:
                recovered = False
                if self.consolidation_count >= CONSOLIDATION_BARS and atr > 0:
                    rH = float(highs.iloc[-2])
                    rL = float(lows.iloc[-2])
                    for i in range(2, min(CONSOLIDATION_BARS + 1, n)):
                        if highs.iloc[-1 - i] > rH:
                            rH = float(highs.iloc[-1 - i])
                        if lows.iloc[-1 - i] < rL:
                            rL = float(lows.iloc[-1 - i])
                    if (rH - rL) <= atr * CONSOLIDATION_RANGE:
                        recovered = True
                if not recovered and self.pullback_extreme > 0 and atr > 0:
                    tol = atr * 0.3
                    if self.overextend_dir == "up":
                        if (lows.iloc[-2] <= self.pullback_extreme + tol and
                                lows.iloc[-2] >= self.pullback_extreme - tol and
                                closes.iloc[-2] > opens.iloc[-2]):
                            recovered = True
                    else:
                        if (highs.iloc[-2] >= self.pullback_extreme - tol and
                                highs.iloc[-2] <= self.pullback_extreme + tol and
                                closes.iloc[-2] < opens.iloc[-2]):
                            recovered = True
                if not recovered:
                    if (self.overextend_dir == "up" and below) or (self.overextend_dir == "down" and above):
                        recovered = True
                if recovered:
                    self.first_pullback_complete = True
                    self.waiting_for_recovery = False

            should_reset = False
            if self.gap_count == 0:
                should_reset = True
            elif self.overextend_dir == "up" and below and n >= 3:
                if closes.iloc[-3] < ema.iloc[-3] - threshold:
                    should_reset = True
            elif self.overextend_dir == "down" and above and n >= 3:
                if closes.iloc[-3] > ema.iloc[-3] + threshold:
                    should_reset = True
            if should_reset:
                self._reset()

    def check_block(self, signal_name: str) -> bool:
        if not self.is_overextended:
            return False
        if signal_name in ("H1", "L1"):
            if self.first_pullback_blocked and not self.first_pullback_complete:
                return True
        return False

    def _reset(self) -> None:
        self.is_overextended = False
        self.first_pullback_blocked = False
        self.overextend_dir = ""
        self.waiting_for_recovery = False
        self.first_pullback_complete = False
        self.consolidation_count = 0
        self.pullback_extreme = 0.0


# ── HTF Filter ────────────────────────────────────────────────────

@dataclass
class HTFFilter:
    trend_dir: str = ""

    def update(self, current_close: float, htf_ema: float, atr: float) -> None:
        if not ENABLE_HTF_FILTER or atr <= 0:
            self.trend_dir = ""
            return
        threshold = atr * 0.5
        if current_close > htf_ema + threshold:
            self.trend_dir = "up"
        elif current_close < htf_ema - threshold:
            self.trend_dir = "down"
        else:
            self.trend_dir = ""


# ── Spread Filter (币安适配: 用 bid-ask spread) ────────────────────

@dataclass
class SpreadFilter:
    history: list = field(default_factory=list)
    average: float = 0.0
    current: float = 0.0
    active: bool = False

    def update(self, spread: float) -> None:
        if not ENABLE_SPREAD_FILTER:
            self.active = False
            return
        self.current = spread
        self.history.append(spread)
        if len(self.history) > SPREAD_LOOKBACK:
            self.history = self.history[-SPREAD_LOOKBACK:]
        if self.history:
            self.average = sum(self.history) / len(self.history)
        self.active = self.average > 0 and self.current > self.average * MAX_SPREAD_MULT


# ── Signal Cooldown ───────────────────────────────────────────────

@dataclass
class SignalCooldownTracker:
    last_buy_bar: int = -999
    last_sell_bar: int = -999
    last_buy_price: float = 0.0
    last_sell_price: float = 0.0
    bar_counter: int = 0

    def tick(self) -> None:
        self.bar_counter += 1

    def check(
        self, side: str, current_price: float, atr: float,
        highs: pd.Series, lows: pd.Series,
    ) -> bool:
        n = len(highs)
        if side == "buy":
            if self.bar_counter - self.last_buy_bar < SIGNAL_COOLDOWN:
                return False
            if self.last_buy_price > 0 and atr > 0:
                diff = abs(current_price - self.last_buy_price)
                if diff < atr * 1.5:
                    rh = float(highs.iloc[-2])
                    rl = float(lows.iloc[-2])
                    cb = min(SIGNAL_COOLDOWN + 2, n - 1)
                    for i in range(2, cb + 1):
                        if highs.iloc[-1 - i] > rh:
                            rh = float(highs.iloc[-1 - i])
                        if lows.iloc[-1 - i] < rl:
                            rl = float(lows.iloc[-1 - i])
                    if rh - rl < atr * 2.0:
                        return False
        else:
            if self.bar_counter - self.last_sell_bar < SIGNAL_COOLDOWN:
                return False
            if self.last_sell_price > 0 and atr > 0:
                diff = abs(self.last_sell_price - current_price)
                if diff < atr * 1.5:
                    rh = float(highs.iloc[-2])
                    rl = float(lows.iloc[-2])
                    cb = min(SIGNAL_COOLDOWN + 2, n - 1)
                    for i in range(2, cb + 1):
                        if highs.iloc[-1 - i] > rh:
                            rh = float(highs.iloc[-1 - i])
                        if lows.iloc[-1 - i] < rl:
                            rl = float(lows.iloc[-1 - i])
                    if rh - rl < atr * 2.0:
                        return False
        return True

    def record(self, side: str, price: float) -> None:
        if side == "buy":
            self.last_buy_bar = self.bar_counter
            self.last_buy_price = price
        else:
            self.last_sell_bar = self.bar_counter
            self.last_sell_price = price


# ── Measuring Gap ─────────────────────────────────────────────────

@dataclass
class MeasuringGapTracker:
    gap: MeasuringGapInfo = field(default_factory=MeasuringGapInfo)
    has_gap: bool = False

    def update(self, highs: pd.Series, lows: pd.Series,
               opens: pd.Series, closes: pd.Series, atr: float) -> None:
        if not ENABLE_MEASURING_GAP or atr <= 0 or len(highs) < 3:
            return
        if self.has_gap and self.gap.is_valid:
            self.gap.bar_index += 1
            mid = (self.gap.gap_high + self.gap.gap_low) / 2.0
            if self.gap.direction == "up" and lows.iloc[-2] < mid:
                self.gap.is_valid = False
            if self.gap.direction == "down" and highs.iloc[-2] > mid:
                self.gap.is_valid = False
            if self.gap.bar_index > 20:
                self.gap.is_valid = False
                self.has_gap = False
            if self.gap.is_valid:
                return

        h1 = highs.iloc[-2]
        l1 = lows.iloc[-2]
        o1 = opens.iloc[-2]
        c1 = closes.iloc[-2]
        h2 = highs.iloc[-3]
        l2 = lows.iloc[-3]
        rng = h1 - l1
        if rng <= 0:
            return
        body = abs(c1 - o1)
        gap_up = l1 - h2
        if gap_up >= atr * MEASURING_GAP_MIN_SIZE and c1 > o1 and body / rng > 0.5:
            self.has_gap = True
            self.gap = MeasuringGapInfo(gap_high=l1, gap_low=h2, direction="up", bar_index=0, is_valid=True)
            return
        gap_dn = l2 - h1
        if gap_dn >= atr * MEASURING_GAP_MIN_SIZE and c1 < o1 and body / rng > 0.5:
            self.has_gap = True
            self.gap = MeasuringGapInfo(gap_high=l2, gap_low=h1, direction="down", bar_index=0, is_valid=True)


# ── Breakout Mode ─────────────────────────────────────────────────

@dataclass
class BreakoutModeTracker:
    active: bool = False
    direction: str = ""
    bar_count: int = 0
    entry: float = 0.0
    extreme: float = 0.0

    def activate(self, direction: str, entry: float, extreme: float) -> None:
        if not ENABLE_BREAKOUT_MODE:
            return
        self.active = True
        self.direction = direction
        self.bar_count = 0
        self.entry = entry
        self.extreme = extreme

    def tick(self, highs: pd.Series, lows: pd.Series, atr: float) -> None:
        if not self.active:
            return
        self.bar_count += 1
        if self.direction == "up" and highs.iloc[-2] > self.extreme:
            self.extreme = float(highs.iloc[-2])
        if self.direction == "down" and lows.iloc[-2] < self.extreme:
            self.extreme = float(lows.iloc[-2])
        if self.bar_count >= BREAKOUT_MODE_BARS:
            self.active = False
