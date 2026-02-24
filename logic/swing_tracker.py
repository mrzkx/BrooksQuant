"""
波段点追踪 — 精确对齐 EA UpdateSwingPoints / UpdateM5SwingPoints

* 确认波段 (depth=3): 用于 H/L 计数、形态识别
* 临时波段 (depth=1): 用于止损计算，降低延迟
* M5 波段点: 用于 Runner 结构跟踪
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd

from logic.constants import SwingPoint, SWING_CONFIRM_DEPTH

MAX_SWING_POINTS = 40
MAX_M5_SWINGS = 12


@dataclass
class SwingTracker:
    """有状态的波段点追踪器，每根新 K 线调用 update() 一次。"""

    depth: int = SWING_CONFIRM_DEPTH

    # 确认波段（最新在 index 0）
    swings: List[SwingPoint] = field(default_factory=list)

    # 缓存最近 2 个 SH / SL
    cached_sh1: float = 0.0
    cached_sh2: float = 0.0
    cached_sl1: float = 0.0
    cached_sl2: float = 0.0

    # 临时波段（depth=1）
    temp_swing_high: float = 0.0
    temp_swing_low: float = 0.0
    temp_swing_high_bar: int = -1
    temp_swing_low_bar: int = -1

    # M5 结构跟踪
    m5_swing_lows: List[float] = field(default_factory=list)
    m5_swing_highs: List[float] = field(default_factory=list)
    m5_swing_low_bars: List[int] = field(default_factory=list)
    m5_swing_high_bars: List[int] = field(default_factory=list)

    # ── 主时间框架更新 ──────────────────────────────────────────────

    def update(self, highs: pd.Series, lows: pd.Series) -> None:
        """
        每根新 K 线调用。highs/lows 为当前可用 K 线历史（iloc[-1] = 最新收盘棒）。
        EA 使用 bar[0]=当前未收盘，bar[1]=最新收盘棒。这里 df.iloc[-1] 对应 bar[1]。
        """
        n = len(highs)
        if n < 4:
            return

        # 递增 bar 索引 & 清理
        for sp in self.swings:
            sp.bar_index += 1
        self.swings = [sp for sp in self.swings if sp.bar_index <= 40]

        # --- 临时波段 (depth=1) ---
        # tempBar=2 → iloc[-3] (EA bar[2])
        if n >= 4:
            h = highs.values
            l = lows.values
            tb = -3  # iloc offset for tempBar
            if h[-2] < h[tb] and h[-4] < h[tb]:
                self.temp_swing_high = float(h[tb])
                self.temp_swing_high_bar = 2
            if l[-2] > l[tb] and l[-4] > l[tb]:
                self.temp_swing_low = float(l[tb])
                self.temp_swing_low_bar = 2

        # --- 确认波段 (depth=self.depth) ---
        depth = self.depth
        check_bar = depth + 1  # EA: bar index of candidate
        need = check_bar + depth + 1
        if n < need:
            return

        h = highs.values
        l = lows.values

        # 将 EA 的 bar[checkBar] 映射到 iloc 偏移: -(check_bar+1)
        cb = -(check_bar + 1)

        is_sh = True
        center_h = h[cb]
        for i in range(1, depth + 1):
            left = cb + i   # 更近的棒
            right = cb - i  # 更远的棒
            if -left > n or -right > n:
                is_sh = False
                break
            if h[left] >= center_h or h[right] >= center_h:
                is_sh = False
                break

        is_sl = True
        center_l = l[cb]
        for i in range(1, depth + 1):
            left = cb + i
            right = cb - i
            if -left > n or -right > n:
                is_sl = False
                break
            if l[left] <= center_l or l[right] <= center_l:
                is_sl = False
                break

        if is_sh:
            self._add(float(center_h), check_bar, True)
        if is_sl:
            self._add(float(center_l), check_bar, False)

    # ── M5 波段点更新 ─────────────────────────────────────────────

    def update_m5(self, m5_highs: pd.Series, m5_lows: pd.Series) -> None:
        depth = 3
        need = depth * 2 + 5
        n = len(m5_highs)
        if n < need:
            return

        h = m5_highs.values
        l = m5_lows.values

        tmp_lows: list[tuple[float, int]] = []
        tmp_highs: list[tuple[float, int]] = []

        for cb in range(depth + 1, need - depth - 1):
            idx = -(cb + 1)
            # swing low
            is_sl = True
            cl = l[idx]
            for i in range(1, depth + 1):
                if l[idx + i] <= cl or l[idx - i] <= cl:
                    is_sl = False
                    break
            if is_sl and len(tmp_lows) < MAX_M5_SWINGS:
                tmp_lows.append((float(cl), cb))
            # swing high
            is_sh = True
            ch = h[idx]
            for i in range(1, depth + 1):
                if h[idx + i] >= ch or h[idx - i] >= ch:
                    is_sh = False
                    break
            if is_sh and len(tmp_highs) < MAX_M5_SWINGS:
                tmp_highs.append((float(ch), cb))

        tmp_lows.sort(key=lambda x: x[1])
        tmp_highs.sort(key=lambda x: x[1])

        self.m5_swing_lows = [p for p, _ in tmp_lows]
        self.m5_swing_low_bars = [b for _, b in tmp_lows]
        self.m5_swing_highs = [p for p, _ in tmp_highs]
        self.m5_swing_high_bars = [b for _, b in tmp_highs]

    # ── 结构跟踪 ──────────────────────────────────────────────────

    def get_m5_structural_stop_buy(
        self, entry_price: float, current_sl: float, atr: float
    ) -> float:
        if len(self.m5_swing_lows) < 2 or atr <= 0:
            return 0.0
        buf = atr * 0.2
        for i in range(len(self.m5_swing_lows) - 1):
            new_low = self.m5_swing_lows[i]
            prev_low = self.m5_swing_lows[i + 1]
            if (
                new_low > entry_price
                and new_low > prev_low
                and (current_sl <= 0 or new_low > current_sl + buf)
            ):
                return new_low - buf
        return 0.0

    def get_m5_structural_stop_sell(
        self, entry_price: float, current_sl: float, atr: float
    ) -> float:
        if len(self.m5_swing_highs) < 2 or atr <= 0:
            return 0.0
        buf = atr * 0.2
        for i in range(len(self.m5_swing_highs) - 1):
            new_high = self.m5_swing_highs[i]
            prev_high = self.m5_swing_highs[i + 1]
            if (
                new_high < entry_price
                and new_high < prev_high
                and (current_sl <= 0 or new_high < current_sl - buf)
            ):
                return new_high + buf
        return 0.0

    # ── 查询 ──────────────────────────────────────────────────────

    def get_recent_swing_high(self, nth: int = 1, allow_temp: bool = False) -> float:
        if nth == 1 and self.cached_sh1 > 0:
            return self.cached_sh1
        if nth == 2 and self.cached_sh2 > 0:
            return self.cached_sh2
        if nth == 1 and allow_temp and self.temp_swing_high > 0:
            return self.temp_swing_high
        count = 0
        for sp in self.swings:
            if sp.is_high:
                count += 1
                if count == nth:
                    return sp.price
        return 0.0

    def get_recent_swing_low(self, nth: int = 1, allow_temp: bool = False) -> float:
        if nth == 1 and self.cached_sl1 > 0:
            return self.cached_sl1
        if nth == 2 and self.cached_sl2 > 0:
            return self.cached_sl2
        if nth == 1 and allow_temp and self.temp_swing_low > 0:
            return self.temp_swing_low
        count = 0
        for sp in self.swings:
            if not sp.is_high:
                count += 1
                if count == nth:
                    return sp.price
        return 0.0

    # ── 内部 ──────────────────────────────────────────────────────

    def _add(self, price: float, bar_index: int, is_high: bool) -> None:
        for sp in self.swings:
            if sp.bar_index == bar_index and sp.is_high == is_high:
                return
        if len(self.swings) >= MAX_SWING_POINTS:
            self.swings.pop()
        self.swings.insert(0, SwingPoint(price=price, bar_index=bar_index, is_high=is_high))
        self._update_cache()

    def _update_cache(self) -> None:
        self.cached_sh1 = 0.0
        self.cached_sh2 = 0.0
        self.cached_sl1 = 0.0
        self.cached_sl2 = 0.0
        sh_count = 0
        sl_count = 0
        for sp in self.swings:
            if sh_count >= 2 and sl_count >= 2:
                break
            if sp.is_high and sh_count < 2:
                if sh_count == 0:
                    self.cached_sh1 = sp.price
                else:
                    self.cached_sh2 = sp.price
                sh_count += 1
            elif not sp.is_high and sl_count < 2:
                if sl_count == 0:
                    self.cached_sl1 = sp.price
                else:
                    self.cached_sl2 = sp.price
                sl_count += 1
