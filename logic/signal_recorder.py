"""
信号记录模块

负责将信号结果记录到数组和 DataFrame 中，
包括止盈计算、交易者方程校验、插针过滤、TA-Lib 加成等。

关注点分离：
- strategy.py: 决策层（if...then... 逻辑）
- signal_recorder.py: 记录层（数据写入逻辑）
"""

import logging
import pandas as pd
from typing import Optional, Tuple, Callable

from .signal_models import BarContext, SignalArrays, SignalResult


def record_signal_impl(
    arrays: SignalArrays,
    i: int,
    result: SignalResult,
    market_state_value: str,
    tight_channel_score: float,
    tp1: float,
    tp2: float,
    entry_price: float,
    data: Optional[pd.DataFrame],
    atr: Optional[float],
    is_likely_wick_bar_func: Callable,
    satisfies_trader_equation_func: Callable,
) -> None:
    """
    记录信号到结果数组。入场前校验交易者方程与插针过滤，不满足则跳过。
    
    Args:
        arrays: 信号数组集合
        i: K线索引
        result: 信号结果
        market_state_value: 市场状态字符串
        tight_channel_score: 紧凑通道评分
        tp1, tp2: 止盈价格
        entry_price: 入场价（用于交易者方程）
        data: 用于插针检测的 DataFrame（可选）
        atr: 当前 ATR（用于插针检测，可选）
        is_likely_wick_bar_func: 插针检测函数
        satisfies_trader_equation_func: 交易者方程校验函数
    """
    # 插针过滤：信号棒（前一根）为疑似插针则跳过
    if data is not None and atr is not None and i >= 1:
        if is_likely_wick_bar_func(data, i - 1, atr):
            logging.debug(
                f"⏭ 插针过滤跳过: {result.signal_type} {result.side} bar={i-1}（疑似插针）"
            )
            return
    
    if not satisfies_trader_equation_func(
        entry_price, result.stop_loss, tp1, tp2, result.tp1_close_ratio, result.side
    ):
        logging.debug(
            f"⏭ 交易者方程不满足跳过: {result.signal_type} {result.side}, "
            f"entry={entry_price:.2f}, SL={result.stop_loss:.2f}, Risk过大或Reward不足"
        )
        return
    
    arrays.signals[i] = result.signal_type
    arrays.sides[i] = result.side
    arrays.stops[i] = result.stop_loss
    arrays.base_heights[i] = result.base_height
    arrays.risk_reward_ratios[i] = result.risk_reward
    arrays.market_states[i] = market_state_value
    arrays.tight_channel_scores[i] = tight_channel_score
    arrays.tp1_prices[i] = tp1
    arrays.tp2_prices[i] = tp2
    arrays.tp1_close_ratios[i] = result.tp1_close_ratio
    arrays.is_climax_bars[i] = result.is_climax
    arrays.delta_modifiers[i] = result.delta_modifier
    arrays.entry_modes[i] = getattr(result, "entry_mode", None)
    arrays.is_high_risk[i] = getattr(result, "is_high_risk", False)
    arrays.move_stop_to_breakeven_at_tp1[i] = getattr(result, "move_stop_to_breakeven_at_tp1", False)


def record_signal_with_tp_impl(
    arrays: SignalArrays,
    i: int,
    result: SignalResult,
    ctx: BarContext,
    entry_price: float,
    data: pd.DataFrame,
    calculate_tp1_tp2_func: Callable,
    is_likely_wick_bar_func: Callable,
    satisfies_trader_equation_func: Callable,
    update_signal_cooldown_func: Callable,
    pattern_origin: Optional[float] = None,
) -> None:
    """
    计算 TP、写入结果、更新冷却期（统一流程）。
    
    Args:
        arrays: 信号数组集合
        i: K线索引
        result: 信号结果
        ctx: K线上下文
        entry_price: 入场价
        data: K线数据
        calculate_tp1_tp2_func: TP 计算函数
        is_likely_wick_bar_func: 插针检测函数
        satisfies_trader_equation_func: 交易者方程校验函数
        update_signal_cooldown_func: 冷却期更新函数
        pattern_origin: 形态起始点极值（用于 Wedge/FailedBreakout 的 TP2）
    """
    # 从 result 中获取 pattern_origin（如果有 wedge_tp2_price 则使用它）
    effective_pattern_origin = pattern_origin
    if effective_pattern_origin is None and result.wedge_tp2_price is not None:
        effective_pattern_origin = result.wedge_tp2_price
    
    tp1, tp2, tp1_ratio, is_climax = calculate_tp1_tp2_func(
        entry_price, result.stop_loss, result.side, result.base_height,
        result.signal_type, ctx.market_state.value, data, i,
        ema=ctx.ema, pattern_origin=effective_pattern_origin,
    )
    result.tp1_close_ratio = tp1_ratio
    result.is_climax = is_climax
    
    record_signal_impl(
        arrays, i, result, ctx.market_state.value, ctx.tight_channel_score,
        tp1, tp2, entry_price, data, ctx.atr,
        is_likely_wick_bar_func, satisfies_trader_equation_func
    )
    update_signal_cooldown_func(result.signal_type, i)


def apply_talib_boost_impl(
    data: pd.DataFrame,
    arrays: SignalArrays,
    talib_detector,  # Optional[TALibPatternDetector]
    calculate_talib_boost_func: Callable,
) -> None:
    """
    应用 TA-Lib 形态加成
    
    当 TA-Lib 形态与 PA 信号重合时，给予置信度加成
    
    Args:
        data: K线数据
        arrays: 信号数组集合
        talib_detector: TA-Lib 检测器实例（None 则跳过）
        calculate_talib_boost_func: 计算加成的函数
    """
    if talib_detector is None:
        return
    
    for i in range(len(data)):
        if arrays.signals[i] is not None:
            df_slice = data.iloc[:i+1]
            if len(df_slice) >= 10:
                boost, pattern_names = calculate_talib_boost_func(df_slice, arrays.signals[i])
                arrays.talib_boosts[i] = boost
                arrays.talib_patterns[i] = ", ".join(pattern_names) if pattern_names else None
                
                if boost > 0:
                    logging.debug(
                        f"🎯 TA-Lib 形态加成 @ bar {i}: {arrays.signals[i]} +{boost:.2f}, "
                        f"形态: {arrays.talib_patterns[i]}"
                    )


def write_results_to_dataframe_impl(
    data: pd.DataFrame,
    arrays: SignalArrays
) -> pd.DataFrame:
    """
    将信号结果写入 DataFrame
    
    Args:
        data: 原始 K 线数据（带指标）
        arrays: 信号数组集合
    
    Returns:
        添加了信号列的 DataFrame
    """
    data["market_state"] = arrays.market_states
    data["signal"] = arrays.signals
    data["side"] = arrays.sides
    data["stop_loss"] = arrays.stops
    data["risk_reward_ratio"] = arrays.risk_reward_ratios
    data["base_height"] = arrays.base_heights
    data["tp1_price"] = arrays.tp1_prices
    data["tp2_price"] = arrays.tp2_prices
    data["tight_channel_score"] = arrays.tight_channel_scores
    data["delta_modifier"] = arrays.delta_modifiers
    data["tp1_close_ratio"] = arrays.tp1_close_ratios
    data["is_climax_bar"] = arrays.is_climax_bars
    data["talib_boost"] = arrays.talib_boosts
    data["talib_patterns"] = arrays.talib_patterns
    data["entry_mode"] = arrays.entry_modes
    data["is_high_risk"] = arrays.is_high_risk
    data["move_stop_to_breakeven_at_tp1"] = arrays.move_stop_to_breakeven_at_tp1
    
    return data
