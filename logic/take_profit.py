"""
止盈计算 — 精确对齐 EA GetScalpTP1 / GetMeasuredMoveTP2
"""
from __future__ import annotations

from logic.constants import MarketState


def get_scalp_tp1(side: str, entry: float, initial_sl: float) -> float:
    """TP1 = 1:1 盈亏比 (Scalp)"""
    risk = (entry - initial_sl) if side == "buy" else (initial_sl - entry)
    if risk <= 0:
        return 0.0
    return (entry + risk) if side == "buy" else (entry - risk)


def get_measured_move_tp2(
    side: str,
    entry: float,
    atr: float,
    h1: float, l1: float, h2: float, l2: float,
    market_state: MarketState = MarketState.CHANNEL,
    tight_channel_dir: str = "",
    tight_channel_extreme: float = 0.0,
) -> float:
    """TP2 = Measured Move 或 Channel 极值；保护不足 1ATR 扩至 1.5ATR"""
    if atr <= 0:
        return 0.0

    tp2 = 0.0
    use_channel = (
        market_state == MarketState.TIGHT_CHANNEL
        and tight_channel_extreme > 0
    )
    if use_channel:
        if side == "buy" and tight_channel_dir == "up" and tight_channel_extreme > entry:
            tp2 = tight_channel_extreme
        elif side == "sell" and tight_channel_dir == "down" and tight_channel_extreme < entry:
            tp2 = tight_channel_extreme

    if tp2 <= 0:
        high12 = max(h1, h2)
        low12 = min(l1, l2)
        height = high12 - low12
        if height <= 0:
            height = atr * 0.5
        mapped = height * 2.0
        tp2 = (entry + mapped) if side == "buy" else (entry - mapped)

    tp2_dist = (tp2 - entry) if side == "buy" else (entry - tp2)
    if tp2_dist < atr:
        min_dist = atr * 1.5
        tp2 = (entry + min_dist) if side == "buy" else (entry - min_dist)

    return tp2
