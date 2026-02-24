"""
止损计算 — 精确对齐 EA

* GetBrooksStopLoss: swing point ± buffer，备选 2 根 K 线极值
* CalculateUnifiedStopLoss: 强趋势取信号棒止损更紧者
* SoftStop: 收盘破位检查
"""
from __future__ import annotations

from logic.constants import (
    MarketState, MAX_STOP_ATR_MULT, MIN_BUFFER_ATR_MULT,
    SOFT_STOP_CONFIRM_MODE, SOFT_STOP_CONFIRM_BARS,
)
from logic.swing_tracker import SwingTracker


def get_brooks_stop_loss(
    side: str,
    entry: float,
    atr: float,
    swings: SwingTracker,
    h1: float, l1: float, h2: float, l2: float,
    spread: float = 0.0,
) -> float:
    buf = (atr * 0.3 if atr > 0 else 0) + spread
    min_buf = atr * MIN_BUFFER_ATR_MULT if atr > 0 else 0
    if buf < min_buf:
        buf = min_buf

    if side == "buy":
        sw = swings.get_recent_swing_low(1, allow_temp=True)
        if sw > 0 and sw < entry:
            dist = entry - sw
            if atr <= 0 or dist <= atr * MAX_STOP_ATR_MULT:
                return sw - buf
        bar_low = min(l1, l2) if l2 > 0 else l1
        if bar_low <= 0:
            return 0.0
        sl = bar_low - buf
        if sl >= entry:
            sl = entry - (atr * 0.3 if atr > 0 else buf)
        if atr > 0 and (entry - sl) > atr * MAX_STOP_ATR_MULT:
            sl = entry - atr * MAX_STOP_ATR_MULT
        return sl
    else:
        sw = swings.get_recent_swing_high(1, allow_temp=True)
        if sw > 0 and sw > entry:
            dist = sw - entry
            if atr <= 0 or dist <= atr * MAX_STOP_ATR_MULT:
                return sw + buf
        bar_high = max(h1, h2) if h2 > 0 else h1
        if bar_high <= 0:
            return 0.0
        sl = bar_high + buf
        if sl <= entry:
            sl = entry + (atr * 0.3 if atr > 0 else buf)
        if atr > 0 and (sl - entry) > atr * MAX_STOP_ATR_MULT:
            sl = entry + atr * MAX_STOP_ATR_MULT
        return sl


def calculate_unified_stop_loss(
    side: str,
    atr: float,
    entry: float,
    market_state: MarketState,
    swings: SwingTracker,
    h1: float, l1: float, h2: float, l2: float,
    spread: float = 0.0,
) -> float:
    is_strong = market_state in (
        MarketState.STRONG_TREND,
        MarketState.BREAKOUT,
        MarketState.TIGHT_CHANNEL,
    )
    atr_buf = (atr * 0.3 if is_strong else atr * 0.5) if atr > 0 else 0
    min_buf = atr * MIN_BUFFER_ATR_MULT if atr > 0 else 0
    total_buf = max(atr_buf, min_buf) + spread

    if is_strong:
        if side == "buy":
            sl = min(l1, l2) - total_buf
            dist = entry - sl
        else:
            sl = max(h1, h2) + total_buf
            dist = sl - entry
    else:
        if side == "buy":
            sw = swings.get_recent_swing_low(1, allow_temp=True)
            if sw > 0 and (entry - sw - total_buf) <= atr * MAX_STOP_ATR_MULT:
                sl = sw - total_buf
            else:
                sl = min(l1, l2) - total_buf
            dist = entry - sl
        else:
            sw = swings.get_recent_swing_high(1, allow_temp=True)
            if sw > 0 and (sw + total_buf - entry) <= atr * MAX_STOP_ATR_MULT:
                sl = sw + total_buf
            else:
                sl = max(h1, h2) + total_buf
            dist = sl - entry

    if atr > 0 and dist > atr * MAX_STOP_ATR_MULT:
        return 0.0
    return sl


def check_soft_stop(
    side: str,
    technical_sl: float,
    close: float,
    confirm_closes: list[float] | None = None,
) -> bool:
    """
    软止损收盘检查。返回 True 表示应平仓。

    confirm_mode:
      0 = 收盘破
      1 = 实体破 (调用方传入 body 方向)
      2 = 连续 N 根收破
    """
    mode = SOFT_STOP_CONFIRM_MODE
    if mode == 0:
        if side == "buy":
            return close < technical_sl
        return close > technical_sl
    if mode == 2 and confirm_closes:
        need = SOFT_STOP_CONFIRM_BARS
        broken = 0
        for cc in confirm_closes[-need:]:
            if (side == "buy" and cc < technical_sl) or (side == "sell" and cc > technical_sl):
                broken += 1
        return broken >= need
    if side == "buy":
        return close < technical_sl
    return close > technical_sl
