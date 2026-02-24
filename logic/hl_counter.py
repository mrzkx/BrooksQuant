"""
H/L 计数 — 精确对齐 EA UpdateHLCount

Brooks Push 定义:
  H 计数: 回调后突破前 swing high → H1, H2, ...
  L 计数: 反弹后跌破前 swing low  → L1, L2, ...
  回调/反弹深度 >= HL_MIN_PULLBACK_ATR * ATR 才算有效 Push
  重置: lower low / 显著新低(HL_RESET_NEW_EXTREME_ATR * ATR) / 强反转 K 线
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from logic.constants import HL_RESET_NEW_EXTREME_ATR, HL_MIN_PULLBACK_ATR
from logic.swing_tracker import SwingTracker


@dataclass
class HLCounter:
    # H 计数
    h_count: int = 0
    h_last_swing_high: float = 0.0
    h_last_pullback_low: float = 0.0
    h_last_pb_low_bar: int = -1

    # L 计数
    l_count: int = 0
    l_last_swing_low: float = 0.0
    l_last_bounce_high: float = 0.0
    l_last_bounce_bar: int = -1

    def update(
        self,
        highs: pd.Series,
        lows: pd.Series,
        opens: pd.Series,
        closes: pd.Series,
        atr: float,
        swings: SwingTracker,
    ) -> None:
        if len(swings.swings) < 4 or atr <= 0:
            return

        sh1, sh2, sl1, sl2 = self._get_recent_swings(swings)

        reset_extreme = atr * HL_RESET_NEW_EXTREME_ATR
        min_pullback = atr * HL_MIN_PULLBACK_ATR

        h1 = highs.iloc[-2]
        l1_val = lows.iloc[-2]
        o1 = opens.iloc[-2]
        c1 = closes.iloc[-2]
        rng = h1 - l1_val
        rng_safe = max(rng, 1e-10)

        strong_rev_down = (
            rng > atr * 0.8
            and c1 < o1
            and (h1 - c1) / rng_safe < 0.3
        )
        strong_rev_up = (
            rng > atr * 0.8
            and c1 > o1
            and (c1 - l1_val) / rng_safe < 0.3
        )

        # --- H 计数 ---
        if sh1 > 0 and sh2 > 0 and sl1 > 0:
            if h1 > sh1 and sl1 < sh2 and self.h_last_swing_high < sh1:
                pullback_depth = sh2 - sl1
                if pullback_depth >= min_pullback:
                    self.h_count += 1
                    self.h_last_swing_high = sh1
                    self.h_last_pullback_low = sl1
                    self.h_last_pb_low_bar = 1

            if sl1 > 0 and sl2 > 0 and l1_val < sl1 and sl1 < sl2:
                self._reset_h()
            elif sl1 > 0 and l1_val < sl1 - reset_extreme:
                self._reset_h()
            elif strong_rev_down:
                self._reset_h()

        # --- L 计数 ---
        if sl1 > 0 and sl2 > 0 and sh1 > 0:
            if l1_val < sl1 and sh1 > sl2 and (self.l_last_swing_low == 0 or sl1 < self.l_last_swing_low):
                bounce_depth = sh1 - sl2
                if bounce_depth >= min_pullback:
                    self.l_count += 1
                    self.l_last_swing_low = sl1
                    self.l_last_bounce_high = sh1
                    self.l_last_bounce_bar = 1

            if sh1 > 0 and sh2 > 0 and h1 > sh1 and sh1 > sh2:
                self._reset_l()
            elif sh1 > 0 and h1 > sh1 + reset_extreme:
                self._reset_l()
            elif strong_rev_up:
                self._reset_l()

    # ── helpers ────────────────────────────────────────────────────

    def _reset_h(self) -> None:
        self.h_count = 0
        self.h_last_swing_high = 0.0
        self.h_last_pullback_low = 0.0

    def _reset_l(self) -> None:
        self.l_count = 0
        self.l_last_swing_low = 0.0
        self.l_last_bounce_high = 0.0

    @staticmethod
    def _get_recent_swings(swings: SwingTracker) -> tuple[float, float, float, float]:
        """返回 (sh1, sh2, sl1, sl2)"""
        sh1 = swings.cached_sh1
        sh2 = swings.cached_sh2
        sl1 = swings.cached_sl1
        sl2 = swings.cached_sl2
        return sh1, sh2, sl1, sl2
