"""
楔形反转检测模块

Al Brooks: 三推楔形是极高胜率反转/中继信号；
- 上升楔形（高点递升 + 动能递减）→ 卖出
- 下降楔形（低点递降 + 动能递减）→ 买入
"""

import logging
import pandas as pd
from typing import Optional, Tuple, List


# ========== 三推楔形：递归波动峰/谷识别（Al Brooks 数字化）==========

def find_swing_peaks(
    df: pd.DataFrame,
    start: int,
    end: int,
    min_left: int = 2,
    min_right: int = 2,
) -> List[Tuple[int, float]]:
    """
    递归识别波动峰值（局部高点）：high[i] 为峰当且仅当
    左侧至少 min_left 根、右侧至少 min_right 根 K 线的高点均严格低于 high[i]。
    
    用于三推楔形：高点逐渐降低的三个连续峰值 / 高点逐渐升高的三个连续峰值。
    
    Returns:
        [(index, high), ...] 按 index 升序
    """
    peaks: List[Tuple[int, float]] = []
    for j in range(start + min_left, end - min_right):
        if j < 0 or j >= len(df):
            continue
        h = float(df.iloc[j]["high"])
        left_ok = all(float(df.iloc[k]["high"]) < h for k in range(j - min_left, j))
        right_ok = all(float(df.iloc[k]["high"]) < h for k in range(j + 1, j + 1 + min_right))
        if left_ok and right_ok:
            peaks.append((j, h))
    return peaks


def find_swing_troughs(
    df: pd.DataFrame,
    start: int,
    end: int,
    min_left: int = 2,
    min_right: int = 2,
) -> List[Tuple[int, float]]:
    """
    递归识别波动谷底（局部低点）：low[i] 为谷当且仅当
    左侧至少 min_left 根、右侧至少 min_right 根 K 线的低点均严格高于 low[i]。
    
    用于三推楔形：低点逐渐升高的三个连续谷底 / 低点逐渐降低的三个连续谷底。
    
    Returns:
        [(index, low), ...] 按 index 升序
    """
    troughs: List[Tuple[int, float]] = []
    for j in range(start + min_left, end - min_right):
        if j < 0 or j >= len(df):
            continue
        l = float(df.iloc[j]["low"])
        left_ok = all(float(df.iloc[k]["low"]) > l for k in range(j - min_left, j))
        right_ok = all(float(df.iloc[k]["low"]) > l for k in range(j + 1, j + 1 + min_right))
        if left_ok and right_ok:
            troughs.append((j, l))
    return troughs


def find_three_lower_highs(
    peaks: List[Tuple[int, float]],
    min_span: int = 3,
    require_convergence: bool = True,
    require_momentum_decay: bool = True,
) -> Optional[Tuple[List[int], List[float]]]:
    """
    从波动峰值序列中找出「高点逐渐降低」的最近三峰：P1 > P2 > P3。
    
    Al Brooks 楔形核心原则（修正版）：
    - require_convergence: 要求动能递减（第二推幅度 < 第一推幅度）
    - require_momentum_decay: 要求时间递减（第三推时间不应超过第一推的 90%）
    
    "三推楔形的本质是动能递减" - Al Brooks
    
    Args:
        peaks: 峰值序列 [(index, value), ...]
        min_span: 最小索引间隔
        require_convergence: 是否要求幅度收敛（默认 True）
        require_momentum_decay: 是否要求动能/时间递减（默认 True）
    
    Returns:
        (peak_indices, peak_values) 或 None
    """
    if len(peaks) < 3:
        return None
    for k in range(len(peaks) - 2, -1, -1):
        if k + 2 >= len(peaks):
            continue
        idx1, p1 = peaks[k]
        idx2, p2 = peaks[k + 1]
        idx3, p3 = peaks[k + 2]
        if p1 <= p2 or p2 <= p3:
            continue
        if idx2 - idx1 < min_span or idx3 - idx2 < min_span:
            continue
        
        # Al Brooks 修正：幅度收敛检测
        if require_convergence:
            push1 = p1 - p2  # 第一推（从 P1 到 P2 的跌幅）
            push2 = p2 - p3  # 第二推（从 P2 到 P3 的跌幅）
            if push1 <= 0 or push2 >= push1:
                logging.debug(
                    f"Wedge 跳过: 幅度未收敛 push1={push1:.2f}, push2={push2:.2f}"
                )
                continue
        
        # Al Brooks 修正：动能/时间递减检测
        # "第三推时间不应超过第一推的 90%，否则动能未衰减"
        if require_momentum_decay:
            push1_bars = idx2 - idx1  # 第一推的 K 线数
            push2_bars = idx3 - idx2  # 第二推的 K 线数
            if push2_bars >= push1_bars * 0.9:
                logging.debug(
                    f"Wedge 跳过: 动能未衰减 push1_bars={push1_bars}, push2_bars={push2_bars}"
                )
                continue
        
        return ([idx1, idx2, idx3], [p1, p2, p3])
    return None


def find_three_higher_lows(
    troughs: List[Tuple[int, float]],
    min_span: int = 3,
    require_convergence: bool = True,
    require_momentum_decay: bool = True,
) -> Optional[Tuple[List[int], List[float]]]:
    """
    从波动谷底序列中找出「低点逐渐升高」的最近三谷：T1 < T2 < T3。
    
    Al Brooks 楔形核心原则（修正版）：
    - require_convergence: 要求动能递减（第二推幅度 < 第一推幅度）
    - require_momentum_decay: 要求时间递减（第三推时间不应超过第一推的 90%）
    
    "三推楔形的本质是动能递减" - Al Brooks
    
    Args:
        troughs: 谷底序列 [(index, value), ...]
        min_span: 最小索引间隔
        require_convergence: 是否要求幅度收敛（默认 True）
        require_momentum_decay: 是否要求动能/时间递减（默认 True）
    
    Returns:
        (trough_indices, trough_values) 或 None
    """
    if len(troughs) < 3:
        return None
    for k in range(len(troughs) - 2, -1, -1):
        if k + 2 >= len(troughs):
            continue
        idx1, t1 = troughs[k]
        idx2, t2 = troughs[k + 1]
        idx3, t3 = troughs[k + 2]
        if t1 >= t2 or t2 >= t3:
            continue
        if idx2 - idx1 < min_span or idx3 - idx2 < min_span:
            continue
        
        # Al Brooks 修正：幅度收敛检测
        if require_convergence:
            push1 = t2 - t1  # 第一推（从 T1 到 T2 的升幅）
            push2 = t3 - t2  # 第二推（从 T2 到 T3 的升幅）
            if push1 <= 0 or push2 >= push1:
                logging.debug(
                    f"Wedge 跳过: 幅度未收敛 push1={push1:.2f}, push2={push2:.2f}"
                )
                continue
        
        # Al Brooks 修正：动能/时间递减检测
        # "第三推时间不应超过第一推的 90%，否则动能未衰减"
        if require_momentum_decay:
            push1_bars = idx2 - idx1  # 第一推的 K 线数
            push2_bars = idx3 - idx2  # 第二推的 K 线数
            if push2_bars >= push1_bars * 0.9:
                logging.debug(
                    f"Wedge 跳过: 动能未衰减 push1_bars={push1_bars}, push2_bars={push2_bars}"
                )
                continue
        
        return ([idx1, idx2, idx3], [t1, t2, t3])
    return None


def detect_wedge_reversal_impl(
    df: pd.DataFrame,
    i: int,
    ema: float,
    atr: Optional[float],
    market_state,  # MarketState 枚举
    relaxed_signal_bar: bool,
    params,  # IntervalParams
    btc_min_body_ratio: float,
    btc_close_position_pct: float,
    validate_signal_close_func,  # PatternDetector.validate_signal_close
) -> Optional[Tuple[str, str, float, float, float, float, bool]]:
    """
    检测 Wedge Reversal（楔形反转，三次推进）- Al Brooks 加固版
    
    relaxed_signal_bar: 交易区间 BLSH 时 True，信号棒门槛降为 40% 实体、35% 收盘区域
    
    返回: (signal_type, side, stop_loss, base_height, wedge_tp1, wedge_tp2, is_strong_reversal_bar) 或 None
    """
    from .market_analyzer import MarketState
    
    close_ratio = 0.65 if relaxed_signal_bar else 0.75
    body_ratio = 0.40 if relaxed_signal_bar else btc_min_body_ratio
    position_pct = 0.35 if relaxed_signal_bar else btc_close_position_pct
    
    # 上下文过滤：禁止在紧凑通道中反转
    if market_state == MarketState.TIGHT_CHANNEL:
        return None
    
    # 必须在价格偏离 EMA 超过 1.2 * ATR 时才考虑反转
    if atr is not None and atr > 0:
        current_close = float(df.iloc[i]["close"])
        if abs(current_close - ema) < 1.2 * atr:
            return None
    
    # 三推指数间隔：至少 3 根 K 线
    LEG_SPAN_MIN = 3
    min_total_span = params.wedge_min_total_span
    
    if atr and atr > 0:
        dynamic_span = max(
            int(min_total_span * 0.6),
            min(min_total_span, int(300 / atr))
        )
        min_total_span = dynamic_span
    
    if i < 15:
        return None
    
    lookback_start = max(0, i - 30)
    recent_data = df.iloc[lookback_start : i + 1]
    leg_span = max(3, params.wedge_min_leg_span)
    
    # ========== 递归三推：高点逐渐降低的三个峰值（Al Brooks 数字化）==========
    # Al Brooks 修正：启用收敛检测和动能递减检测
    peaks_rec = find_swing_peaks(df, lookback_start, i + 1, min_left=2, min_right=2)
    three_lower = find_three_lower_highs(
        peaks_rec, min_span=leg_span, 
        require_convergence=True, require_momentum_decay=True
    )
    if three_lower is not None:
        peak_indices, peak_values = three_lower
        idx3 = peak_indices[2]
        if idx3 <= i and (i - idx3) <= 8:  # 第三峰后 8 根内视为有效
            current_bar = df.iloc[i]
            current_close = float(current_bar["close"])
            current_open = float(current_bar["open"])
            third_high = peak_values[2]
            if current_close < peak_values[2] * 0.99 and current_close < current_open:
                if validate_signal_close_func(current_bar, "sell", min_close_ratio=close_ratio):
                    stop_loss = third_high + (0.5 * atr) if atr and atr > 0 else third_high * 1.001
                    wedge_height = peak_values[0] - peak_values[2]
                    wedge_tp1 = ema
                    wedge_tp2 = float(df.iloc[peak_indices[0]]["low"])
                    sb_range = float(current_bar["high"]) - float(current_bar["low"])
                    sb_upper = float(current_bar["high"]) - max(float(current_bar["open"]), float(current_bar["close"]))
                    is_strong = sb_range > 0 and (sb_upper / sb_range) > 0.3
                    logging.debug("✅ Wedge_Sell(三推高点递降) 递归识别触发")
                    return ("Wedge_Sell", "sell", stop_loss, wedge_height, wedge_tp1, wedge_tp2, is_strong)
    
    # ========== 递归三推：低点逐渐升高的三个谷底 ==========
    # Al Brooks 修正：启用收敛检测和动能递减检测
    troughs_rec = find_swing_troughs(df, lookback_start, i + 1, min_left=2, min_right=2)
    three_higher = find_three_higher_lows(
        troughs_rec, min_span=leg_span,
        require_convergence=True, require_momentum_decay=True
    )
    if three_higher is not None:
        trough_indices, trough_values = three_higher
        idx3 = trough_indices[2]
        if idx3 <= i and (i - idx3) <= 8:
            current_bar = df.iloc[i]
            current_close = float(current_bar["close"])
            third_low = trough_values[2]
            if current_close > third_low * 1.01 and validate_signal_close_func(current_bar, "buy", min_close_ratio=close_ratio):
                sb_high = float(current_bar["high"])
                sb_low = float(current_bar["low"])
                sb_open = float(current_bar["open"])
                sb_close = float(current_bar["close"])
                sb_body = abs(sb_close - sb_open)
                sb_lower = min(sb_open, sb_close) - sb_low
                if sb_body > 0 and sb_lower > 1.5 * sb_body:
                    stop_loss = third_low - (0.5 * atr) if atr and atr > 0 else third_low * 0.999
                    wedge_height = trough_values[2] - trough_values[0]
                    wedge_tp1 = ema
                    wedge_tp2 = float(df.iloc[trough_indices[0]]["high"])
                    sb_range = sb_high - sb_low
                    is_strong = sb_range > 0 and (sb_lower / sb_range) > 0.3
                    logging.debug("✅ Wedge_Buy(三推低点递升) 递归识别触发")
                    return ("Wedge_Buy", "buy", stop_loss, wedge_height, wedge_tp1, wedge_tp2, is_strong)
    
    # ========== 原有逻辑：上升楔形（高点递升 + 动能递减）、下降楔形（低点递降 + 动能递减）==========
    # 检测 High 3（上升楔形）
    recent_highs = [recent_data.iloc[j]["high"] for j in range(len(recent_data))]
    if len(recent_highs) >= 10:
        peaks = []
        for j in range(1, len(recent_highs) - 1):
            if recent_highs[j] > recent_highs[j - 1] and recent_highs[j] > recent_highs[j + 1]:
                actual_idx = lookback_start + j
                peaks.append((actual_idx, recent_highs[j]))
        
        if len(peaks) >= 3:
            last_3_peaks = peaks[-3:]
            peak_indices = [p[0] for p in last_3_peaks]
            peak_values = [p[1] for p in last_3_peaks]
            
            if (peak_values[0] < peak_values[1] < peak_values[2] and 
                (peak_values[1] - peak_values[0]) > (peak_values[2] - peak_values[1])):
                
                # 纵向距离：第一推 (P1→P2) vs 第三推 (P2→P3)。若第三推 > 第一推的 120% 说明趋势在加速非衰减，跳过
                first_push = peak_values[1] - peak_values[0]
                third_push = peak_values[2] - peak_values[1]
                if first_push > 0 and third_push > 1.2 * first_push:
                    logging.debug(
                        f"Wedge_Sell 跳过: 第三推纵向({third_push:.2f}) > 第一推120%({1.2*first_push:.2f})，趋势加速"
                    )
                else:
                    # 三推指数间隔：idx2-idx1>=3 且 idx3-idx2>=3
                    if peak_indices[2] - peak_indices[0] < min_total_span:
                        pass
                    elif (peak_indices[1] - peak_indices[0] < LEG_SPAN_MIN
                          or peak_indices[2] - peak_indices[1] < LEG_SPAN_MIN):
                        pass
                    elif df.iloc[peak_indices[2]]["body_size"] >= df.iloc[peak_indices[0]]["body_size"]:
                        pass
                    else:
                        third_bar = df.iloc[peak_indices[2]]
                        is_bearish = third_bar["close"] < third_bar["open"]
                        upper_shadow = third_bar["high"] - max(third_bar["open"], third_bar["close"])
                        body_size = abs(third_bar["close"] - third_bar["open"])
                        has_long_upper = upper_shadow > body_size * 2 if body_size > 0 else upper_shadow > (third_bar["high"] - third_bar["low"]) * 0.3
                        
                        if is_bearish or has_long_upper:
                            current_close = float(df.iloc[i]["close"])
                            if current_close < peak_values[2] * 0.98:
                                current_bar = df.iloc[i]
                                if validate_signal_close_func(current_bar, "sell", min_close_ratio=close_ratio):
                                    third_high = peak_values[2]
                                    # SL = 极值 + 0.5 * ATR
                                    stop_loss = third_high + (0.5 * atr) if atr and atr > 0 else third_high * 1.001
                                    wedge_height = peak_values[2] - peak_values[0]
                                    wedge_tp1 = ema  # TP1 = EMA20
                                    wedge_tp2 = float(df.iloc[peak_indices[0]]["low"])  # TP2 = 楔形起点
                                    # Signal Bar 强反转棒：上影线占比 > 30%
                                    sb_range = float(current_bar["high"]) - float(current_bar["low"])
                                    sb_upper = float(current_bar["high"]) - max(float(current_bar["open"]), float(current_bar["close"]))
                                    is_strong_reversal_bar = sb_range > 0 and (sb_upper / sb_range) > 0.3
                                    return ("Wedge_Sell", "sell", stop_loss, wedge_height, wedge_tp1, wedge_tp2, is_strong_reversal_bar)
    
    # 检测 Low 3（下降楔形）
    recent_lows = [recent_data.iloc[j]["low"] for j in range(len(recent_data))]
    if len(recent_lows) >= 10:
        troughs = []
        for j in range(1, len(recent_lows) - 1):
            if recent_lows[j] < recent_lows[j - 1] and recent_lows[j] < recent_lows[j + 1]:
                actual_idx = lookback_start + j
                troughs.append((actual_idx, recent_lows[j]))
        
        if len(troughs) >= 3:
            last_3_troughs = troughs[-3:]
            trough_indices = [t[0] for t in last_3_troughs]
            trough_values = [t[1] for t in last_3_troughs]
            
            if (trough_values[0] > trough_values[1] > trough_values[2] and 
                (trough_values[0] - trough_values[1]) > (trough_values[1] - trough_values[2])):
                
                # 纵向距离：第一推 (P1→P2) vs 第三推 (P2→P3)。若第三推 > 第一推的 120% 说明趋势在加速非衰减，跳过
                first_push = trough_values[0] - trough_values[1]
                third_push = trough_values[1] - trough_values[2]
                if first_push > 0 and third_push > 1.2 * first_push:
                    logging.debug(
                        f"Wedge_Buy 跳过: 第三推纵向({third_push:.2f}) > 第一推120%({1.2*first_push:.2f})，趋势加速"
                    )
                else:
                    # 三推指数间隔：idx2-idx1>=3 且 idx3-idx2>=3
                    if trough_indices[2] - trough_indices[0] < min_total_span:
                        pass
                    elif (trough_indices[1] - trough_indices[0] < LEG_SPAN_MIN
                          or trough_indices[2] - trough_indices[1] < LEG_SPAN_MIN):
                        pass
                    elif df.iloc[trough_indices[2]]["body_size"] >= df.iloc[trough_indices[0]]["body_size"]:
                        pass
                    else:
                        third_bar = df.iloc[trough_indices[2]]
                        is_bullish = third_bar["close"] > third_bar["open"]
                        lower_shadow = min(third_bar["open"], third_bar["close"]) - third_bar["low"]
                        body_size = abs(third_bar["close"] - third_bar["open"])
                        has_long_lower = lower_shadow > body_size * 2 if body_size > 0 else lower_shadow > (third_bar["high"] - third_bar["low"]) * 0.3
                        
                        if is_bullish or has_long_lower:
                            current_close = float(df.iloc[i]["close"])
                            if current_close > trough_values[2] * 1.02:
                                current_bar = df.iloc[i]
                                if not validate_signal_close_func(current_bar, "buy", min_close_ratio=close_ratio):
                                    logging.debug("Wedge_Buy 跳过: Signal Bar 收盘未在全长前25%区域")
                                    pass
                                else:
                                    sb_high = float(current_bar["high"])
                                    sb_low = float(current_bar["low"])
                                    sb_open = float(current_bar["open"])
                                    sb_close = float(current_bar["close"])
                                    sb_body = abs(sb_close - sb_open)
                                    sb_lower_shadow = min(sb_open, sb_close) - sb_low
                                    if sb_body > 0 and sb_lower_shadow <= 1.5 * sb_body:
                                        logging.debug(
                                            f"Wedge_Buy 跳过: Signal Bar 下影线未大于实体1.5倍，非探底回升"
                                        )
                                    elif sb_body == 0 and sb_lower_shadow <= 0:
                                        logging.debug("Wedge_Buy 跳过: Signal Bar 无实体且无下影线")
                                    else:
                                        third_low = trough_values[2]
                                        # SL = 极值 - 0.5 * ATR
                                        stop_loss = third_low - (0.5 * atr) if atr and atr > 0 else third_low * 0.999
                                        wedge_height = trough_values[0] - trough_values[2]
                                        wedge_tp1 = ema  # TP1 = EMA20
                                        wedge_tp2 = float(df.iloc[trough_indices[0]]["high"])  # TP2 = 楔形起点
                                        # Signal Bar 强反转棒：下影线占比 > 30%
                                        sb_range = sb_high - sb_low
                                        is_strong_reversal_bar = sb_range > 0 and (sb_lower_shadow / sb_range) > 0.3
                                        return ("Wedge_Buy", "buy", stop_loss, wedge_height, wedge_tp1, wedge_tp2, is_strong_reversal_bar)
    
    return None
