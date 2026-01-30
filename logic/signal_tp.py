"""
策略止盈与 Climax 检测

SIGNAL_RR_RATIO、detect_climax_signal_bar、calculate_tp1_tp2 供 strategy 统一调用。

止盈逻辑优先级（Al Brooks 风格）：
1. Spike: TP1 = 1R, TP2 = base_height（Spike 高度）从入场点翻一倍
2. Wedge/Failed Breakout: TP1 = EMA, TP2 = 形态起始点（pattern_origin）
3. 其他信号/回退: TP1 = R 倍数, TP2 = 2.0 × R
"""

import logging
from typing import Optional, Tuple, Dict, List

import pandas as pd

from .interval_params import IntervalParams


# Al Brooks 风格：根据信号类型的动态盈亏比（修正版）
# 
# Al Brooks 修正：
# - H2/L2 是高胜率信号（50-60%），TP1 从 0.8R 提高到 1.0R
# - "顺势交易应该让利润奔跑" - Al Brooks
# - 反转交易（Wedge/MTR）胜率较低，TP1 保持 0.8R-1.0R
#
SIGNAL_RR_RATIO: Dict[str, Dict[str, float]] = {
    "Spike_Buy": {"tp1_r": 1.0, "tp2_r": 2.0},
    "Spike_Sell": {"tp1_r": 1.0, "tp2_r": 2.0},
    "FailedBreakout_Buy": {"tp1_r": 1.0, "tp2_r": 2.0},
    "FailedBreakout_Sell": {"tp1_r": 1.0, "tp2_r": 2.0},
    "Wedge_FailedBreakout_Buy": {"tp1_r": 1.0, "tp2_r": 2.0},
    "Wedge_FailedBreakout_Sell": {"tp1_r": 1.0, "tp2_r": 2.0},
    "Climax_Buy": {"tp1_r": 1.2, "tp2_r": 2.0},
    "Climax_Sell": {"tp1_r": 1.2, "tp2_r": 2.0},
    "Wedge_Buy": {"tp1_r": 0.8, "tp2_r": 2.0},  # 反转信号保守
    "Wedge_Sell": {"tp1_r": 0.8, "tp2_r": 2.0},  # 反转信号保守
    "MTR_Buy": {"tp1_r": 0.8, "tp2_r": 2.5},  # MTR 低胜率高盈亏比
    "MTR_Sell": {"tp1_r": 0.8, "tp2_r": 2.5},  # MTR 低胜率高盈亏比
    "FinalFlag_Buy": {"tp1_r": 1.0, "tp2_r": 2.0},
    "FinalFlag_Sell": {"tp1_r": 1.0, "tp2_r": 2.0},
    "H2_Buy": {"tp1_r": 1.0, "tp2_r": 2.0},  # Al Brooks 修正：从 0.8R 提高到 1.0R
    "L2_Sell": {"tp1_r": 1.0, "tp2_r": 2.0},  # Al Brooks 修正：从 0.8R 提高到 1.0R
    "H1_Buy": {"tp1_r": 0.8, "tp2_r": 2.0},  # H1 保守（成功率低于 H2）
    "L1_Sell": {"tp1_r": 0.8, "tp2_r": 2.0},  # L1 保守（成功率低于 L2）
}

# 默认回退 R 倍数
DEFAULT_FALLBACK_TP2_R = 2.0


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


def _is_spike_signal(signal_type: Optional[str]) -> bool:
    """判断是否为 Spike 类信号"""
    if signal_type is None:
        return False
    return signal_type.startswith("Spike_")


def _is_wedge_or_failed_breakout_signal(signal_type: Optional[str]) -> bool:
    """判断是否为 Wedge 或 Failed Breakout 类信号"""
    if signal_type is None:
        return False
    return (
        signal_type.startswith("Wedge_") or
        signal_type.startswith("FailedBreakout_")
    )


def detect_magnets(
    df: pd.DataFrame,
    i: int,
    side: str,
    entry_price: float,
    atr: Optional[float] = None,
    lookback: int = 50,
) -> List[float]:
    """
    检测价格磁吸位（Al Brooks: Magnets）
    
    Al Brooks: "价格总是被磁吸位吸引 - 前一 swing high/low、区间边界、
    Measured Move 目标位。止盈应设在磁吸位，而非固定 R 倍数。"
    
    磁吸位优先级：
    1. 前一 swing high/low（最近的结构点）
    2. 区间边界（TradingRange 的 high/low）
    3. Measured Move 目标位
    
    Args:
        df: K线数据
        i: 当前 K 线索引
        side: 交易方向 "buy" / "sell"
        entry_price: 入场价格
        atr: ATR 值（用于过滤太近的磁吸位）
        lookback: 回看周期
    
    Returns:
        磁吸位列表（按距离排序，最远的在前）
    """
    if df is None or i < 10:
        return []
    
    magnets: List[float] = []
    lookback_data = df.iloc[max(0, i - lookback):i + 1]
    
    # 最小距离过滤：磁吸位至少要离入场价 0.5 * ATR
    min_distance = (atr * 0.5) if atr and atr > 0 else entry_price * 0.003
    
    if side == "buy":
        # 上方磁吸位
        # 1. 找所有高于入场价的 swing highs
        for j in range(2, len(lookback_data) - 1):
            curr_high = float(lookback_data.iloc[j]["high"])
            prev_high = float(lookback_data.iloc[j - 1]["high"])
            next_high = float(lookback_data.iloc[j + 1]["high"])
            
            # swing high: 高于相邻两根
            if curr_high > prev_high and curr_high > next_high:
                if curr_high > entry_price + min_distance:
                    magnets.append(curr_high)
        
        # 2. 区间上边界
        range_high = float(lookback_data["high"].max())
        if range_high > entry_price + min_distance:
            magnets.append(range_high)
        
        # 去重并按距离排序（最远的在前）
        magnets = sorted(set(magnets), reverse=True)
    
    else:
        # 下方磁吸位
        # 1. 找所有低于入场价的 swing lows
        for j in range(2, len(lookback_data) - 1):
            curr_low = float(lookback_data.iloc[j]["low"])
            prev_low = float(lookback_data.iloc[j - 1]["low"])
            next_low = float(lookback_data.iloc[j + 1]["low"])
            
            # swing low: 低于相邻两根
            if curr_low < prev_low and curr_low < next_low:
                if curr_low < entry_price - min_distance:
                    magnets.append(curr_low)
        
        # 2. 区间下边界
        range_low = float(lookback_data["low"].min())
        if range_low < entry_price - min_distance:
            magnets.append(range_low)
        
        # 去重并按距离排序（最远的在前）
        magnets = sorted(set(magnets))
    
    logging.debug(
        f"磁吸位检测: side={side}, entry={entry_price:.2f}, "
        f"找到 {len(magnets)} 个磁吸位: {[f'{m:.2f}' for m in magnets[:3]]}"
    )
    
    return magnets


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
    ema: Optional[float] = None,
    pattern_origin: Optional[float] = None,
    atr: Optional[float] = None,
) -> Tuple[float, float, float, bool]:
    """
    Al Brooks 风格分批止盈目标位（结构目标优先版 + 磁吸位增强 + Crypto 适配）。
    
    止盈逻辑优先级：
    1. Spike 信号:
       - TP1 = 1R
       - TP2 = entry_price ± base_height（Spike 高度翻一倍）
       - 回退: 2.0 × R
    
    2. Wedge / Failed Breakout 信号:
       - TP1 = EMA（均线回归）
       - TP2 = pattern_origin（形态起始点极值）
       - 回退: 2.0 × R
    
    3. TradingRange（Crypto 假突破优化）:
       - TP1 = 60% 区间等宽（动态减仓点 - Crypto 假突破保护）
       - TP2 = 100% 区间等宽（完整 Measured Move）
       - 原因: Crypto 市场假突破频繁，价格常在 60% 位置开始震荡
    
    4. 其他信号（H2/L2、MTR 等）:
       - TP1 = R 倍数
       - TP2 = 磁吸位优先（swing high/low、区间边界）
       - 回退: 2.0 × R
    
    Args:
        params: 周期参数
        entry_price: 入场价格
        stop_loss: 止损价格
        side: 交易方向 "buy" / "sell"
        base_height: 结构高度（Spike 高度、区间宽度等）
        signal_type: 信号类型
        market_state: 市场状态
        df: K线数据（用于 Climax 检测 + 磁吸位检测）
        current_idx: 当前 K 线索引
        ema: EMA 值（用于 Wedge/FailedBreakout 的 TP1）
        pattern_origin: 形态起始点极值（用于 Wedge/FailedBreakout 的 TP2）
        atr: ATR 值（用于磁吸位检测）
    
    Returns:
        (tp1, tp2, tp1_close_ratio, is_climax)
    """
    risk = abs(entry_price - stop_loss)
    if risk <= 0:
        risk = entry_price * 0.01  # 防止除零
    
    direction = 1 if side == "buy" else -1
    tp1_close_ratio = 0.5
    is_climax = False
    
    # ========== Climax 信号棒检测 ==========
    if df is not None and current_idx is not None:
        is_climax, bar_ratio = detect_climax_signal_bar(df, current_idx, multiplier=3.0)
        if is_climax:
            tp1_close_ratio = 0.75  # Climax 时 TP1 平仓比例提高
            logging.debug(
                f"📊 Climax 信号棒检测: 长度={bar_ratio:.1f}x平均, "
                f"TP1平仓{tp1_close_ratio*100:.0f}%"
            )
    
    # ========== Spike 信号：TP2 优先使用 base_height 翻一倍 ==========
    if _is_spike_signal(signal_type):
        # TP1: 1R
        tp1 = entry_price + direction * risk
        
        # TP2: 优先使用 base_height（Spike 高度）从入场点翻一倍
        if base_height > 0:
            tp2 = entry_price + direction * base_height
            logging.debug(
                f"🚀 Spike TP计算: TP1=1R, TP2=base_height({base_height:.2f}) "
                f"→ TP2={tp2:.2f}"
            )
        else:
            # 回退: 2.0 × R
            tp2 = entry_price + direction * (risk * DEFAULT_FALLBACK_TP2_R)
            logging.debug(
                f"🚀 Spike TP计算(回退): TP1=1R, TP2={DEFAULT_FALLBACK_TP2_R}R "
                f"→ TP2={tp2:.2f}"
            )
        
        # Climax 时限制 TP2
        if is_climax:
            climax_tp2 = entry_price + direction * (risk * 1.5)
            if side == "buy":
                tp2 = min(tp2, climax_tp2)
            else:
                tp2 = max(tp2, climax_tp2)
            logging.debug(f"📊 Climax 限制 Spike TP2 至 1.5R")
        
        return (tp1, tp2, tp1_close_ratio, is_climax)
    
    # ========== Wedge / Failed Breakout 信号：TP1=EMA, TP2=形态起点 ==========
    if _is_wedge_or_failed_breakout_signal(signal_type):
        # TP1: 优先使用 EMA
        if ema is not None and ema > 0:
            tp1 = ema
            tp1_method = "EMA"
        else:
            # EMA 不可用，回退到 1R
            tp1 = entry_price + direction * risk
            tp1_method = "1R(回退)"
        
        # TP2: 优先使用 pattern_origin（形态起始点极值）
        if pattern_origin is not None and pattern_origin > 0:
            tp2 = pattern_origin
            tp2_method = "pattern_origin"
        elif base_height > 0:
            # 次优先：使用 base_height
            tp2 = entry_price + direction * base_height
            tp2_method = "base_height"
        else:
            # 回退: 2.0 × R
            tp2 = entry_price + direction * (risk * DEFAULT_FALLBACK_TP2_R)
            tp2_method = f"{DEFAULT_FALLBACK_TP2_R}R(回退)"
        
        # 验证 TP1/TP2 方向正确性
        if side == "buy":
            # 买入: TP1 和 TP2 都应该高于入场价
            if tp1 < entry_price:
                tp1 = entry_price + direction * risk
                tp1_method = "1R(方向修正)"
            if tp2 < tp1:
                tp2 = entry_price + direction * (risk * DEFAULT_FALLBACK_TP2_R)
                tp2_method = f"{DEFAULT_FALLBACK_TP2_R}R(方向修正)"
        else:
            # 卖出: TP1 和 TP2 都应该低于入场价
            if tp1 > entry_price:
                tp1 = entry_price + direction * risk
                tp1_method = "1R(方向修正)"
            if tp2 > tp1:
                tp2 = entry_price + direction * (risk * DEFAULT_FALLBACK_TP2_R)
                tp2_method = f"{DEFAULT_FALLBACK_TP2_R}R(方向修正)"
        
        logging.debug(
            f"📐 Wedge/FB TP计算: TP1={tp1_method}({tp1:.2f}), "
            f"TP2={tp2_method}({tp2:.2f})"
        )
        
        # Climax 时限制 TP2
        if is_climax:
            climax_tp2 = entry_price + direction * (risk * 1.5)
            if side == "buy":
                tp2 = min(tp2, climax_tp2)
            else:
                tp2 = max(tp2, climax_tp2)
            logging.debug(f"📊 Climax 限制 Wedge/FB TP2 至 1.5R")
        
        return (tp1, tp2, tp1_close_ratio, is_climax)
    
    # ========== 其他信号：优先使用磁吸位，回退到 R 倍数 ==========
    default_rr = {"tp1_r": params.default_tp1_r, "tp2_r": DEFAULT_FALLBACK_TP2_R}
    rr_config = SIGNAL_RR_RATIO.get(signal_type, default_rr)
    tp1_multiplier = rr_config["tp1_r"]
    tp2_multiplier = rr_config["tp2_r"]
    
    # ========== Al Brooks 修正：根据市场状态动态调整 H2/L2 的 TP1 ==========
    is_h2_l2_signal = signal_type in ["H2_Buy", "L2_Sell", "H1_Buy", "L1_Sell"]
    if is_h2_l2_signal and market_state is not None:
        if market_state == "Channel":
            # Channel 状态：顺势交易可以更激进
            tp1_multiplier = 1.2  # 从 1.0R 提高到 1.2R
            logging.debug(f"📈 Channel: H2/L2 TP1 延长至 {tp1_multiplier}R")
        elif market_state == "TradingRange":
            # Trading Range 状态：保守
            tp1_multiplier = 0.8
            logging.debug(f"📦 TradingRange: H2/L2 TP1 缩短至 {tp1_multiplier}R")
    
    # TightChannel 市场状态调整
    if market_state == "TightChannel" and not is_climax:
        tp2_multiplier = max(tp2_multiplier, 3.0)
        logging.debug(f"🔒 TightChannel: TP2 延长至 {tp2_multiplier}R")
    
    tp1 = entry_price + direction * (risk * tp1_multiplier)
    
    # ========== Al Brooks 修正：TP2 优先使用磁吸位（Magnets）==========
    # Al Brooks: "价格被结构目标吸引，止盈应设在磁吸位而非固定 R 倍数"
    tp2_method = f"{tp2_multiplier}R"
    tp2 = entry_price + direction * (risk * tp2_multiplier)  # 默认 R 倍数
    
    if df is not None and current_idx is not None:
        magnets = detect_magnets(df, current_idx, side, entry_price, atr=atr, lookback=50)
        
        if magnets:
            # 选择最合适的磁吸位
            # 优先选择 >= 1.5R 但 <= 3.0R 距离的磁吸位
            min_target = entry_price + direction * (risk * 1.5)
            max_target = entry_price + direction * (risk * 3.0)
            
            for magnet in magnets:
                if side == "buy":
                    if min_target <= magnet <= max_target:
                        tp2 = magnet
                        tp2_method = "磁吸位"
                        break
                else:
                    if max_target <= magnet <= min_target:
                        tp2 = magnet
                        tp2_method = "磁吸位"
                        break
            
            # 如果没有找到合适范围的磁吸位，使用最近的一个（如果它比 R 倍数更远）
            if tp2_method != "磁吸位" and magnets:
                best_magnet = magnets[0]  # 已按距离排序
                if side == "buy" and best_magnet > tp2:
                    tp2 = best_magnet
                    tp2_method = "磁吸位(远)"
                elif side == "sell" and best_magnet < tp2:
                    tp2 = best_magnet
                    tp2_method = "磁吸位(远)"
    
    # ========== TradingRange 市场状态调整（Crypto 假突破优化）==========
    # Al Brooks 修正（Crypto 适配）：
    # - Crypto 市场假突破（Overshoot）频繁，价格经常在 60% 位置开始震荡
    # - TP1 = 60% 区间等宽（动态减仓点）
    # - TP2 = 100% 区间等宽（Measured Move 完整目标）
    if market_state == "TradingRange" and df is not None and current_idx is not None:
        lookback_data = df.iloc[max(0, current_idx - 30):current_idx + 1]
        range_width = float(lookback_data["high"].max() - lookback_data["low"].min())
        
        if range_width > 0:
            # TP1 = 60% 区间等宽（Crypto 假突破保护 - 动态减仓点）
            tr_partial_target = range_width * 0.6
            # TP2 = 100% 区间等宽（完整 Measured Move）
            tr_full_target = range_width
            
            if side == "buy":
                tr_tp1 = entry_price + tr_partial_target
                tr_tp2 = entry_price + tr_full_target
            else:
                tr_tp1 = entry_price - tr_partial_target
                tr_tp2 = entry_price - tr_full_target
            
            # 验证 TR 目标的合理性（至少 1.0R，最多 4.0R）
            tr_tp1_distance = abs(tr_tp1 - entry_price)
            tr_tp2_distance = abs(tr_tp2 - entry_price)
            
            if tr_tp1_distance >= risk * 0.8 and tr_tp2_distance <= risk * 4.0:
                tp1 = tr_tp1
                tp2 = tr_tp2
                tp2_method = "TR等宽(Crypto)"
                logging.debug(
                    f"📦 TradingRange(Crypto): TP1=60%等宽({tr_partial_target:.2f}), "
                    f"TP2=100%等宽({tr_full_target:.2f}), 区间宽度={range_width:.2f}"
                )
    
    # Climax 时限制 TP2
    if is_climax:
        climax_tp2 = entry_price + direction * (risk * 1.5)
        if side == "buy":
            tp2 = min(tp2, climax_tp2)
        else:
            tp2 = max(tp2, climax_tp2)
        tp2_method = "1.5R(Climax限制)"
        logging.debug(f"📊 Climax 限制 TP2 至 1.5R")
    
    logging.debug(
        f"📊 默认TP计算: signal={signal_type}, TP1={tp1_multiplier}R, "
        f"TP2={tp2_method} → TP1={tp1:.2f}, TP2={tp2:.2f}"
    )
    
    return (tp1, tp2, tp1_close_ratio, is_climax)
