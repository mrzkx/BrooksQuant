"""
终极旗形反转检测模块

Al Brooks: "Final Flag 是趋势耗尽的最后挣扎。当价格突破旗形后迅速失败，
这是高胜率的反转入场点，因为趋势已经耗尽了所有动能。"
"""

import logging
import pandas as pd
from typing import Optional, Tuple


def detect_final_flag_reversal_impl(
    df: pd.DataFrame,
    i: int,
    ema: float,
    atr: Optional[float],
    market_state,  # MarketState 枚举
    final_flag_info: Optional[dict],
    validate_btc_signal_bar_func,  # PatternDetector.validate_btc_signal_bar
) -> Optional[Tuple[str, str, float, float]]:
    """
    检测 Final Flag Reversal（终极旗形反转）- Al Brooks 高胜率反转
    
    Al Brooks: "Final Flag 是趋势耗尽的最后挣扎。当价格突破旗形后迅速失败，
    这是高胜率的反转入场点，因为趋势已经耗尽了所有动能。"
    
    逻辑要求：
    1. 当前市场状态必须是 FINAL_FLAG
    2. 价格尝试突破旗形边界（顺势方向）
    3. 突破失败：盘中突破后收盘拉回（Failed Breakout）
    4. 出现强反转棒（Signal Bar）确认反转
    
    Args:
        df: K线数据
        i: 当前 K 线索引
        ema: EMA20 值
        atr: ATR 值
        market_state: 市场状态（必须是 FINAL_FLAG）
        final_flag_info: Final Flag 信息（来自 MarketAnalyzer）
        validate_btc_signal_bar_func: 信号棒质量验证函数
    
    返回: (signal_type, side, stop_loss, base_height) 或 None
    """
    from .market_analyzer import MarketState
    
    # 条件1：市场状态必须是 FINAL_FLAG
    if market_state != MarketState.FINAL_FLAG:
        return None
    
    if final_flag_info is None:
        return None
    
    tc_direction = final_flag_info.get('direction')
    tc_extreme = final_flag_info.get('extreme')
    tc_end_bar = final_flag_info.get('tc_end_bar')
    
    if tc_direction is None or tc_extreme is None or tc_end_bar is None:
        return None
    
    if i < 3:
        return None
    
    current_bar = df.iloc[i]
    current_high = float(current_bar["high"])
    current_low = float(current_bar["low"])
    current_close = float(current_bar["close"])
    current_open = float(current_bar["open"])
    kline_range = current_high - current_low
    if kline_range <= 0:
        return None
    
    # 计算旗形区间（自 TightChannel 结束以来）
    flag_start = tc_end_bar + 1
    if flag_start >= i:
        return None
    flag_data = df.iloc[flag_start : i]  # 不含当前棒
    if len(flag_data) < 2:
        return None
    
    flag_high = float(flag_data["high"].max())
    flag_low = float(flag_data["low"].min())
    
    # ========== Final_Flag_Reversal_Sell：上涨趋势后的旗形 ==========
    if tc_direction == "up":
        # 条件2：当前棒尝试向上突破旗形高点
        if current_high > flag_high:
            # 条件3：突破失败 - 收盘拉回到旗形内部
            close_back_below = current_close < flag_high * 0.999
            close_in_lower = (current_high - current_close) / kline_range >= 0.5
            
            if close_back_below or close_in_lower:
                # 条件4：强反转棒确认（阴线）
                is_bearish = current_close < current_open
                if is_bearish:
                    # 使用信号棒质量验证
                    bar_valid, _ = validate_btc_signal_bar_func(current_bar, "sell")
                    if bar_valid:
                        # 止损设在 TightChannel 极值（前高）之上
                        stop_loss = tc_extreme + (0.5 * atr) if atr and atr > 0 else tc_extreme * 1.001
                        base_height = tc_extreme - ema  # 目标：回到 EMA
                        if atr and atr > 0 and base_height < atr:
                            base_height = atr * 2.0
                        
                        logging.debug(
                            f"✅ Final_Flag_Reversal_Sell 触发: "
                            f"旗形高点={flag_high:.2f}, 前高={tc_extreme:.2f}, "
                            f"突破后收盘拉回, 强反转棒确认"
                        )
                        return ("Final_Flag_Reversal_Sell", "sell", stop_loss, base_height)
    
    # ========== Final_Flag_Reversal_Buy：下跌趋势后的旗形 ==========
    elif tc_direction == "down":
        # 条件2：当前棒尝试向下突破旗形低点
        if current_low < flag_low:
            # 条件3：突破失败 - 收盘拉回到旗形内部
            close_back_above = current_close > flag_low * 1.001
            close_in_upper = (current_close - current_low) / kline_range >= 0.5
            
            if close_back_above or close_in_upper:
                # 条件4：强反转棒确认（阳线）
                is_bullish = current_close > current_open
                if is_bullish:
                    # 使用信号棒质量验证
                    bar_valid, _ = validate_btc_signal_bar_func(current_bar, "buy")
                    if bar_valid:
                        # 止损设在 TightChannel 极值（前低）之下
                        stop_loss = tc_extreme - (0.5 * atr) if atr and atr > 0 else tc_extreme * 0.999
                        base_height = ema - tc_extreme  # 目标：回到 EMA
                        if atr and atr > 0 and base_height < atr:
                            base_height = atr * 2.0
                        
                        logging.debug(
                            f"✅ Final_Flag_Reversal_Buy 触发: "
                            f"旗形低点={flag_low:.2f}, 前低={tc_extreme:.2f}, "
                            f"突破后收盘拉回, 强反转棒确认"
                        )
                        return ("Final_Flag_Reversal_Buy", "buy", stop_loss, base_height)
    
    return None
