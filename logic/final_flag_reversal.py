"""
终极旗形反转检测模块（Al Brooks 修正版）

Al Brooks: "Final Flag 是趋势耗尽的最后挣扎。当价格突破旗形后迅速失败，
这是高胜率的反转入场点，因为趋势已经耗尽了所有动能。"

修正要点：
1. 明确定义旗形边界（使用 TightChannel 结束时的极值）
2. 要求"有意义的突破"（突破幅度 >= 0.1% 或 0.2×ATR）
3. 突破失败确认：盘中突破但收盘拉回旗形内
4. 强反转棒确认
"""

import logging
import pandas as pd
from typing import Optional, Tuple


# 突破有效性阈值
BREAKOUT_MIN_PCT = 0.001  # 最小突破幅度 0.1%
BREAKOUT_ATR_MULT = 0.2   # 或 0.2×ATR


def _is_meaningful_breakout(
    breakout_high: float, 
    threshold: float, 
    atr: Optional[float],
    direction: str
) -> bool:
    """
    判断突破是否有意义
    
    Al Brooks: "突破需要有意义，仅仅触及边界不算真正的突破"
    
    Args:
        breakout_high: 突破价格（盘中高点或低点）
        threshold: 突破阈值（旗形高点或低点）
        atr: ATR 值
        direction: "up" 或 "down"
    
    Returns:
        True 如果突破有意义（超过最小阈值）
    """
    if direction == "up":
        # 向上突破：突破价格需要明显高于阈值
        min_break = threshold * BREAKOUT_MIN_PCT
        if atr and atr > 0:
            min_break = max(min_break, atr * BREAKOUT_ATR_MULT)
        return breakout_high > threshold + min_break
    else:
        # 向下突破：突破价格需要明显低于阈值
        min_break = threshold * BREAKOUT_MIN_PCT
        if atr and atr > 0:
            min_break = max(min_break, atr * BREAKOUT_ATR_MULT)
        return breakout_high < threshold - min_break


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
    检测 Final Flag Reversal（终极旗形反转）- Al Brooks 高胜率反转（修正版）
    
    Al Brooks: "Final Flag 是趋势耗尽的最后挣扎。当价格突破旗形后迅速失败，
    这是高胜率的反转入场点，因为趋势已经耗尽了所有动能。"
    
    修正后的逻辑要求：
    1. 当前市场状态必须是 FINAL_FLAG
    2. 价格尝试突破旗形边界（顺势方向）
    3. **突破必须有意义**：突破幅度 >= 0.1% 或 0.2×ATR
    4. 突破失败：盘中突破后收盘拉回（Failed Breakout）
    5. 出现强反转棒（Signal Bar）确认反转
    
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
            # Al Brooks 修正：检查突破是否有意义
            is_meaningful = _is_meaningful_breakout(current_high, flag_high, atr, "up")
            if not is_meaningful:
                logging.debug(
                    f"Final_Flag_Sell 跳过: 突破不够有意义 "
                    f"(high={current_high:.2f}, flag_high={flag_high:.2f})"
                )
            else:
                # 条件3：突破失败 - 收盘拉回到旗形内部
                close_back_below = current_close < flag_high * 0.999
                close_in_lower = (current_high - current_close) / kline_range >= 0.5
                
                if close_back_below or close_in_lower:
                    # 条件4：强反转棒确认（阴线）
                    is_bearish = current_close < current_open
                    if not is_bearish:
                        logging.debug(f"Final_Flag_Sell 跳过: 非阴线反转棒")
                    else:
                        # 使用信号棒质量验证（含背景比较）
                        bar_valid, bar_reason = validate_btc_signal_bar_func(
                            current_bar, "sell", df=df, i=i, signal_type="FinalFlag_Sell"
                        )
                        if not bar_valid:
                            logging.debug(f"Final_Flag_Sell 跳过: 信号棒质量不足 - {bar_reason}")
                        else:
                            # 止损设在 TightChannel 极值（前高）之上
                            stop_loss = tc_extreme + (0.5 * atr) if atr and atr > 0 else tc_extreme * 1.001
                            base_height = tc_extreme - ema  # 目标：回到 EMA
                            if atr and atr > 0 and base_height < atr:
                                base_height = atr * 2.0
                            
                            logging.debug(
                                f"✅ Final_Flag_Reversal_Sell 触发: "
                                f"旗形高点={flag_high:.2f}, 突破高点={current_high:.2f}, "
                                f"前高={tc_extreme:.2f}, 有意义突破后收盘拉回, 强反转棒确认"
                            )
                            return ("Final_Flag_Reversal_Sell", "sell", stop_loss, base_height)
                else:
                    logging.debug(
                        f"Final_Flag_Sell 跳过: 收盘未拉回旗形内 "
                        f"(close={current_close:.2f}, flag_high={flag_high:.2f})"
                    )
    
    # ========== Final_Flag_Reversal_Buy：下跌趋势后的旗形 ==========
    elif tc_direction == "down":
        # 条件2：当前棒尝试向下突破旗形低点
        if current_low < flag_low:
            # Al Brooks 修正：检查突破是否有意义
            is_meaningful = _is_meaningful_breakout(current_low, flag_low, atr, "down")
            if not is_meaningful:
                logging.debug(
                    f"Final_Flag_Buy 跳过: 突破不够有意义 "
                    f"(low={current_low:.2f}, flag_low={flag_low:.2f})"
                )
            else:
                # 条件3：突破失败 - 收盘拉回到旗形内部
                close_back_above = current_close > flag_low * 1.001
                close_in_upper = (current_close - current_low) / kline_range >= 0.5
                
                if close_back_above or close_in_upper:
                    # 条件4：强反转棒确认（阳线）
                    is_bullish = current_close > current_open
                    if not is_bullish:
                        logging.debug(f"Final_Flag_Buy 跳过: 非阳线反转棒")
                    else:
                        # 使用信号棒质量验证（含背景比较）
                        bar_valid, bar_reason = validate_btc_signal_bar_func(
                            current_bar, "buy", df=df, i=i, signal_type="FinalFlag_Buy"
                        )
                        if not bar_valid:
                            logging.debug(f"Final_Flag_Buy 跳过: 信号棒质量不足 - {bar_reason}")
                        else:
                            # 止损设在 TightChannel 极值（前低）之下
                            stop_loss = tc_extreme - (0.5 * atr) if atr and atr > 0 else tc_extreme * 0.999
                            base_height = ema - tc_extreme  # 目标：回到 EMA
                            if atr and atr > 0 and base_height < atr:
                                base_height = atr * 2.0
                            
                            logging.debug(
                                f"✅ Final_Flag_Reversal_Buy 触发: "
                                f"旗形低点={flag_low:.2f}, 突破低点={current_low:.2f}, "
                                f"前低={tc_extreme:.2f}, 有意义突破后收盘拉回, 强反转棒确认"
                            )
                            return ("Final_Flag_Reversal_Buy", "buy", stop_loss, base_height)
                else:
                    logging.debug(
                        f"Final_Flag_Buy 跳过: 收盘未拉回旗形内 "
                        f"(close={current_close:.2f}, flag_low={flag_low:.2f})"
                    )
    
    return None
