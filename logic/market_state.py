"""
市场状态检测 + Always In 方向 — 精确对齐 EA

包含：
  DetectStrongTrend / DetectTightChannel / DetectTradingRange /
  DetectBreakout / DetectFinalFlag / ApplyStateInertia /
  UpdateAlwaysInDirection / GetMarketCycle
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from logic.constants import (
    MarketState, MarketCycle, AlwaysIn,
    STRONG_TREND_SCORE, STATE_MIN_HOLD,
    TTR_OVERLAP_THRESHOLD, TTR_RANGE_ATR_MULT,
)
from logic.swing_tracker import SwingTracker


@dataclass
class MarketStateTracker:
    state: MarketState = MarketState.CHANNEL
    cycle: MarketCycle = MarketCycle.CHANNEL
    always_in: AlwaysIn = AlwaysIn.NEUTRAL

    trend_direction: str = ""
    trend_strength: float = 0.0
    tight_channel_dir: str = ""
    tight_channel_bars: int = 0
    tight_channel_extreme: float = 0.0
    last_tight_channel_end_bar: int = -1

    tr_high: float = 0.0
    tr_low: float = 0.0

    # 状态惯性
    _locked_state: MarketState = MarketState.CHANNEL
    _hold_bars: int = 0

    # ── 主入口 ─────────────────────────────────────────────────────

    def update(
        self,
        highs: pd.Series,
        lows: pd.Series,
        opens: pd.Series,
        closes: pd.Series,
        ema: pd.Series,
        atr_val: float,
        swings: SwingTracker,
    ) -> None:
        n = len(closes)
        if n < 12 or atr_val <= 0:
            return

        h = highs.values
        l = lows.values
        o = opens.values
        c = closes.values
        e = ema.values

        detected = MarketState.CHANNEL

        if self._detect_strong_trend(h, l, o, c, e, atr_val, n):
            detected = MarketState.STRONG_TREND
        elif self._detect_tight_channel(h, l, o, c, atr_val, n):
            detected = MarketState.TIGHT_CHANNEL
            self.tight_channel_bars += 1
            self._update_tight_channel_tracking(h, l)
        elif self._detect_final_flag(c, e, atr_val):
            detected = MarketState.FINAL_FLAG
            if self.tight_channel_bars > 0:
                self.last_tight_channel_end_bar = 1
        elif self._detect_trading_range(h, l, c, e, atr_val, n):
            detected = MarketState.TRADING_RANGE
            if self.tight_channel_bars > 0:
                self.last_tight_channel_end_bar = 1
            self.tight_channel_bars = 0
        elif self._detect_breakout(h, l, o, c, e, atr_val, n):
            detected = MarketState.BREAKOUT
        else:
            if self.tight_channel_bars > 0:
                self.last_tight_channel_end_bar = 1
            self.tight_channel_bars = 0

        self._apply_inertia(detected)
        self.cycle = _get_cycle(self.state)

        self._update_always_in(h, l, o, c, e, atr_val, n, swings)

    # ── Detect* ────────────────────────────────────────────────────

    def _detect_strong_trend(self, h, l, o, c, e, atr, n) -> bool:
        lookback = 10
        bull_streak = bear_streak = 0
        cur_bull = cur_bear = 0
        hh = ll_ = 0
        above = below = 0

        for i in range(1, min(lookback + 1, n)):
            idx = -1 - i
            if -idx > n:
                break
            is_bull = c[idx] > o[idx]
            is_bear = c[idx] < o[idx]
            if is_bull:
                cur_bull += 1; cur_bear = 0
            elif is_bear:
                cur_bear += 1; cur_bull = 0
            bull_streak = max(bull_streak, cur_bull)
            bear_streak = max(bear_streak, cur_bear)

            if i + 1 < n:
                idx2 = idx - 1
                if -idx2 <= n:
                    if h[idx] > h[idx2]:
                        hh += 1
                    if l[idx] < l[idx2]:
                        ll_ += 1

            if -idx <= len(e):
                if c[idx] > e[idx]:
                    above += 1
                else:
                    below += 1

        up = down = 0.0
        if bull_streak >= 3:
            up += 0.25
        if bull_streak >= 5:
            up += 0.25
        if hh >= 4:
            up += 0.2
        if above >= 8:
            up += 0.15
        if bear_streak >= 3:
            down += 0.25
        if bear_streak >= 5:
            down += 0.25
        if ll_ >= 4:
            down += 0.2
        if below >= 8:
            down += 0.15

        dist = (c[-2] - e[-2]) / atr if atr > 0 else 0
        if dist > 1.0:
            up += 0.15
        if dist < -1.0:
            down += 0.15

        if up >= STRONG_TREND_SCORE and up > down:
            self.trend_direction = "up"
            self.trend_strength = up
            return True
        if down >= STRONG_TREND_SCORE and down > up:
            self.trend_direction = "down"
            self.trend_strength = down
            return True

        self.trend_direction = ""
        self.trend_strength = max(up, down)
        return False

    def _detect_tight_channel(self, h, l, o, c, atr, n) -> bool:
        if n < 15 or atr <= 0:
            return False
        lookback = 12
        bull = bear = 0
        new_highs = new_lows = 0
        shallow = 0

        for i in range(1, min(lookback + 1, n - 1)):
            idx = -1 - i
            idx2 = idx - 1
            if -idx2 > n:
                break
            if c[idx] > o[idx]:
                bull += 1
            elif c[idx] < o[idx]:
                bear += 1
            if h[idx] > h[idx2]:
                new_highs += 1
            if l[idx] < l[idx2]:
                new_lows += 1
            prev_range = h[idx2] - l[idx2]
            if prev_range > 0:
                if l[idx] >= l[idx2] + prev_range * 0.75:
                    shallow += 1
                if h[idx] <= h[idx2] - prev_range * 0.75:
                    shallow += 1

        if bull >= lookback * 0.6 and new_highs >= lookback * 0.5 and shallow >= lookback * 0.4:
            self.tight_channel_dir = "up"
            return True
        if bear >= lookback * 0.6 and new_lows >= lookback * 0.5 and shallow >= lookback * 0.4:
            self.tight_channel_dir = "down"
            return True
        self.tight_channel_dir = ""
        return False

    def _detect_trading_range(self, h, l, c, e, atr, n) -> bool:
        if n < 25 or atr <= 0:
            return False
        lookback = 20
        rh = h[-2]
        rl = l[-2]
        for i in range(2, min(lookback + 1, n)):
            idx = -1 - i
            if -idx > n:
                break
            if h[idx] > rh:
                rh = h[idx]
            if l[idx] < rl:
                rl = l[idx]
        total = rh - rl
        if total < atr * 2.0:
            return False
        upper = rh - total * 0.2
        lower = rl + total * 0.2
        touch_h = touch_l = 0
        crosses = 0
        prev_above = c[-(lookback + 1)] > e[-(lookback + 1)] if n > lookback else True
        for i in range(1, min(lookback + 1, n)):
            idx = -1 - i
            if h[idx] >= upper:
                touch_h += 1
            if l[idx] <= lower:
                touch_l += 1
            cur_above = c[idx] > e[idx]
            if cur_above != prev_above:
                crosses += 1
                prev_above = cur_above
        if touch_h >= 2 and touch_l >= 2 and crosses >= 4:
            self.tr_high = rh
            self.tr_low = rl
            return True
        return False

    def _detect_breakout(self, h, l, o, c, e, atr, n) -> bool:
        if n < 12 or atr <= 0:
            return False
        body = abs(c[-2] - o[-2])
        rng = h[-2] - l[-2]
        if rng <= 0:
            return False
        avg_body = 0.0
        cnt = 0
        for i in range(2, min(12, n)):
            avg_body += abs(c[-1 - i] - o[-1 - i])
            cnt += 1
        if cnt > 0:
            avg_body /= cnt
        if avg_body > 0 and body > avg_body * 1.5:
            close = c[-2]
            if close > e[-2] and (close - l[-2]) / rng > 0.7:
                return True
            if close < e[-2] and (h[-2] - close) / rng > 0.7:
                return True
        return False

    def _detect_final_flag(self, c, e, atr) -> bool:
        if self.tight_channel_bars < 5 or self.last_tight_channel_end_bar < 0:
            return False
        bars_since = self.last_tight_channel_end_bar
        if bars_since < 3 or bars_since > 8:
            return False
        if atr <= 0:
            return False
        dist = (c[-2] - e[-2]) / atr
        if self.tight_channel_dir == "up" and dist < 0.5:
            return False
        if self.tight_channel_dir == "down" and dist > -0.5:
            return False
        if not self.tight_channel_dir:
            return False
        return True

    # ── 辅助 ──────────────────────────────────────────────────────

    def _update_tight_channel_tracking(self, h, l) -> None:
        if self.tight_channel_dir == "up":
            if self.tight_channel_extreme == 0 or h[-2] > self.tight_channel_extreme:
                self.tight_channel_extreme = float(h[-2])
        elif self.tight_channel_dir == "down":
            if self.tight_channel_extreme == 0 or l[-2] < self.tight_channel_extreme:
                self.tight_channel_extreme = float(l[-2])

    def _apply_inertia(self, new: MarketState) -> None:
        if self._hold_bars > 0:
            self._hold_bars -= 1
            self.state = self._locked_state
            return
        if new != self._locked_state:
            min_hold = STATE_MIN_HOLD.get(self._locked_state, 1)
            self._locked_state = new
            self._hold_bars = min_hold
        if self.state != new:
            self.state = new

    # ── TTR 查询 ──────────────────────────────────────────────────

    def is_ttr(self, highs: pd.Series, lows: pd.Series, atr: float) -> bool:
        if self.state != MarketState.TRADING_RANGE or atr <= 0:
            return False
        if self.tr_high <= self.tr_low:
            return False
        tr_range = self.tr_high - self.tr_low
        if tr_range >= atr * TTR_RANGE_ATR_MULT:
            return False
        overlap = _get_bar_overlap_ratio(highs, lows, 20)
        return overlap < TTR_OVERLAP_THRESHOLD

    # ── AlwaysIn ──────────────────────────────────────────────────

    def _update_always_in(self, h, l, o, c, e, atr, n, swings: SwingTracker) -> None:
        if n < 20 or atr <= 0:
            self.always_in = AlwaysIn.NEUTRAL
            return

        body1 = c[-2] - o[-2]
        rng1 = h[-2] - l[-2]
        close_pos = (c[-2] - l[-2]) / rng1 if rng1 > 0 else 0.5
        body_ratio = abs(body1) / rng1 if rng1 > 0 else 0

        # --- 两棒确认 ---
        if n >= 4:
            b1 = c[-2] - o[-2]
            b2 = c[-3] - o[-3]
            r1 = h[-2] - l[-2]
            r2 = h[-3] - l[-3]
            e2 = e[-3] if len(e) >= 3 else e[-2]
            bull1 = r1 > 0 and b1 / r1 > 0.55
            bear1 = r1 > 0 and b1 / r1 < -0.55
            bull2 = r2 > 0 and b2 / r2 > 0.55
            bear2 = r2 > 0 and b2 / r2 < -0.55
            if bull1 and bull2 and c[-2] > e[-2] and c[-3] > e2:
                self.always_in = AlwaysIn.LONG
                return
            if bear1 and bear2 and c[-2] < e[-2] and c[-3] < e2:
                self.always_in = AlwaysIn.SHORT
                return

        # --- 极强趋势棒 ---
        if n >= 5 and rng1 > atr * 1.0:
            avg3 = sum(abs(c[-1 - k] - o[-1 - k]) for k in range(2, 5)) / 3.0
            body_len = abs(body1)
            break_ema = (body1 > 0 and c[-2] > e[-2]) or (body1 < 0 and c[-2] < e[-2])
            break_struct = False
            sh1 = swings.get_recent_swing_high(1)
            sl1 = swings.get_recent_swing_low(1)
            if body1 > 0 and sh1 > 0 and c[-2] > sh1:
                break_struct = True
            if body1 < 0 and sl1 > 0 and c[-2] < sl1:
                break_struct = True
            if avg3 > 0 and body_len > avg3 * 2.0 and body_ratio > 0.6 and (break_ema or break_struct):
                if body1 > 0 and close_pos > 0.75:
                    self.always_in = AlwaysIn.LONG
                    return
                if body1 < 0 and close_pos < 0.25:
                    self.always_in = AlwaysIn.SHORT
                    return

        # --- 直接翻转 ---
        if rng1 > atr * 1.2 and body_ratio > 0.65:
            if body1 > 0 and close_pos > 0.75:
                self.always_in = AlwaysIn.LONG
                return
            if body1 < 0 and close_pos < 0.25:
                self.always_in = AlwaysIn.SHORT
                return

        # --- 评分制 ---
        bull_cnt = bear_cnt = 0
        overlap_pen = 0
        for i in range(1, min(6, n)):
            idx = -1 - i
            body = c[idx] - o[idx]
            rng = h[idx] - l[idx]
            if rng <= 0:
                continue
            br = abs(body) / rng
            has_ov = False
            if i < n - 1:
                idx2 = idx - 1
                if -idx2 <= n:
                    ov_h = min(h[idx], h[idx2])
                    ov_l = max(l[idx], l[idx2])
                    if ov_h > ov_l and (ov_h - ov_l) / rng > 0.6:
                        has_ov = True
            if body > 0 and br > 0.5:
                bull_cnt += 1
                if has_ov:
                    overlap_pen += 1
            if body < 0 and br > 0.5:
                bear_cnt += 1
                if has_ov:
                    overlap_pen += 1

        hh_cnt = hl_cnt = lh_cnt = ll_cnt = 0
        sp = swings.swings
        for i in range(1, min(len(sp) - 1, 4)):
            j = i + 1
            if j >= len(sp):
                break
            if sp[i].is_high and sp[j].is_high:
                if sp[i].price > sp[j].price:
                    hh_cnt += 1
                else:
                    lh_cnt += 1
            if not sp[i].is_high and not sp[j].is_high:
                if sp[i].price > sp[j].price:
                    hl_cnt += 1
                else:
                    ll_cnt += 1

        above_ema = c[-2] > e[-2]
        bull_score = bear_score = 0.0
        cw = 0.25 if overlap_pen >= 2 else (0.35 if overlap_pen >= 1 else 0.4)
        if bull_cnt >= 3:
            bull_score += cw
        elif bull_cnt >= 2:
            bull_score += cw * 0.5
        if bear_cnt >= 3:
            bear_score += cw
        elif bear_cnt >= 2:
            bear_score += cw * 0.5
        if hh_cnt > 0 and hl_cnt > 0:
            bull_score += 0.30
        if lh_cnt > 0 and ll_cnt > 0:
            bear_score += 0.30
        if above_ema:
            bull_score += 0.12
        else:
            bear_score += 0.12
        if rng1 > 0 and rng1 > atr * 1.5:
            if body1 > 0:
                bull_score += 0.35 if body_ratio > 0.7 else 0.25
            else:
                bear_score += 0.35 if body_ratio > 0.7 else 0.25
        if close_pos > 0.8:
            bull_score += 0.20
        if close_pos < 0.2:
            bear_score += 0.20

        if bull_score >= 0.5 and bull_score > bear_score + 0.1:
            self.always_in = AlwaysIn.LONG
        elif bear_score >= 0.5 and bear_score > bull_score + 0.1:
            self.always_in = AlwaysIn.SHORT
        else:
            self.always_in = AlwaysIn.NEUTRAL


# ── 模块级辅助 ────────────────────────────────────────────────────

def _get_cycle(state: MarketState) -> MarketCycle:
    if state == MarketState.BREAKOUT:
        return MarketCycle.SPIKE
    if state == MarketState.TRADING_RANGE:
        return MarketCycle.TRADING_RANGE
    return MarketCycle.CHANNEL


def _get_bar_overlap_ratio(highs: pd.Series, lows: pd.Series, lookback: int = 20) -> float:
    n = len(highs)
    if n < lookback + 1:
        return 1.0
    h = highs.values
    l = lows.values
    rh = h[-2]
    rl = l[-2]
    sum_range = 0.0
    for i in range(1, min(lookback + 1, n)):
        idx = -1 - i
        if h[idx] > rh:
            rh = h[idx]
        if l[idx] < rl:
            rl = l[idx]
        br = h[idx] - l[idx]
        if br > 0:
            sum_range += br
    total = rh - rl
    if sum_range <= 0 or total <= 0:
        return 1.0
    return total / sum_range
