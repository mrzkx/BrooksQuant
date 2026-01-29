"""
策略止盈与 Climax 检测

SIGNAL_RR_RATIO、detect_climax_signal_bar、calculate_tp1_tp2 供 strategy 统一调用。
"""

import logging
from typing import Optional, Tuple, Dict

import pandas as pd

from .interval_params import IntervalParams


# Al Brooks 风格：根据信号类型的动态盈亏比
SIGNAL_RR_RATIO: Dict[str, Dict[str, float]] = {
    "Spike_Buy": {"tp1_r": 1.0, "tp2_r": 2.5},
    "Spike_Sell": {"tp1_r": 1.0, "tp2_r": 2.5},
    "FailedBreakout_Buy": {"tp1_r": 0.8, "tp2_r": 1.5},
    "FailedBreakout_Sell": {"tp1_r": 0.8, "tp2_r": 1.5},
    "Wedge_FailedBreakout_Buy": {"tp1_r": 0.8, "tp2_r": 1.5},
    "Wedge_FailedBreakout_Sell": {"tp1_r": 0.8, "tp2_r": 1.5},
    "Climax_Buy": {"tp1_r": 1.2, "tp2_r": 3.0},
    "Climax_Sell": {"tp1_r": 1.2, "tp2_r": 3.0},
    "Wedge_Buy": {"tp1_r": 1.0, "tp2_r": 2.5},
    "Wedge_Sell": {"tp1_r": 1.0, "tp2_r": 2.5},
    "H2_Buy": {"tp1_r": 0.8, "tp2_r": 2.0},
    "L2_Sell": {"tp1_r": 0.8, "tp2_r": 2.0},
    "H1_Buy": {"tp1_r": 0.8, "tp2_r": 1.8},
    "L1_Sell": {"tp1_r": 0.8, "tp2_r": 1.8},
}


def detect_climax_signal_bar(
    df: pd.DataFrame, i: int, multiplier: float = 3.0
) -> Tuple[bool, float]:
    """
    检测 Climax 信号棒（大炮冲刺）。

    Al Brooks: "Climax 是市场极端情绪的表现，通常预示着反转或调整"
    条件：Signal Bar 长度超过过去 10 根 K 线平均长度的 multiplier 倍。

    Returns:
        (is_climax, bar_ratio)
    """
    if i < 10:
        return (False, 1.0)
    lookback = df.iloc[max(0, i - 10):i]
    avg_range = (lookback["high"] - lookback["low"]).mean()
    if avg_range <= 0:
        return (False, 1.0)
    current_range = df.iloc[i]["high"] - df.iloc[i]["low"]
    bar_ratio = current_range / avg_range
    return (bar_ratio >= multiplier, bar_ratio)


def calculate_tp1_tp2(
    params: IntervalParams,
    entry_price: float,
    stop_loss: float,
    side: str,
    base_height: float,
    signal_type: Optional[str] = None,
    market_state: Optional[str] = None,
    df: Optional[pd.DataFrame] = None,
    current_idx: Optional[int] = None,
) -> Tuple[float, float, float, bool]:
    """
    Al Brooks 风格分批止盈目标位（动态分时出场版）。

    Returns:
        (tp1, tp2, tp1_close_ratio, is_climax)
    """
    risk = abs(entry_price - stop_loss)
    default_rr = {"tp1_r": params.default_tp1_r, "tp2_r": params.default_tp2_r}
    rr_config = SIGNAL_RR_RATIO.get(signal_type, default_rr)
    tp1_multiplier = rr_config["tp1_r"]
    tp2_multiplier = rr_config["tp2_r"]
    tp1_close_ratio = 0.5
    is_climax = False

    if df is not None and current_idx is not None:
        is_climax, bar_ratio = detect_climax_signal_bar(df, current_idx, multiplier=3.0)
        if is_climax:
            tp2_multiplier = min(tp2_multiplier, 1.5)
            tp1_close_ratio = 0.75
            logging.debug(
                f"📊 Climax 信号棒检测: 长度={bar_ratio:.1f}x平均, "
                f"TP2调整为{tp2_multiplier}R, TP1平仓{tp1_close_ratio*100:.0f}%"
            )

    if market_state == "TightChannel" and not is_climax:
        tp2_multiplier = max(tp2_multiplier, 3.0)
        logging.debug(f"🔒 TightChannel: TP2 延长至 {tp2_multiplier}R")
    elif market_state == "TradingRange":
        if base_height > 0 and base_height < risk * tp2_multiplier:
            tp2_multiplier = base_height / risk if risk > 0 else tp2_multiplier
            tp2_multiplier = max(tp2_multiplier, 1.2)
            logging.debug(f"📦 TradingRange: TP2 限制在区间边缘 {tp2_multiplier:.1f}R")

    direction = 1 if side == "buy" else -1
    tp1 = entry_price + direction * (risk * tp1_multiplier)
    measured_move = (
        entry_price + direction * base_height
        if base_height > 0
        else entry_price + direction * (risk * tp2_multiplier)
    )
    r_based_tp2 = entry_price + direction * (risk * tp2_multiplier)
    tp2 = (
        max(measured_move, r_based_tp2)
        if side == "buy"
        else min(measured_move, r_based_tp2)
    )

    if market_state == "TradingRange" and base_height > 0:
        range_limit = entry_price + direction * base_height
        tp2 = min(tp2, range_limit) if side == "buy" else max(tp2, range_limit)

    if (
        base_height > 0
        and base_height < risk * 1.5
        and market_state != "TradingRange"
    ):
        conservative_tp2 = entry_price + direction * (risk * (tp2_multiplier + 0.5))
        tp2 = (
            max(tp2, conservative_tp2)
            if side == "buy"
            else min(tp2, conservative_tp2)
        )

    return (tp1, tp2, tp1_close_ratio, is_climax)
