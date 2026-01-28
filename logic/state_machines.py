"""
H2/L2 状态机管理

负责 HState 和 LState 的复杂状态机管理

Al Brooks H2/L2 回调策略：
- H2: 上升趋势中的第二次回调买入点
- L2: 下降趋势中的第二次反弹卖出点

Outside Bar 处理原则 (Al Brooks)：
- Outside Bar 是指当前 K 线高点 > 前一根高点，且低点 < 前一根低点
- Outside Bar 的方向由收盘价位置决定：
  - 收盘在上半部分 (>50%) = 看涨 Outside Bar
  - 收盘在下半部分 (<50%) = 看跌 Outside Bar
- "Outside Bar 本质上是市场的犹豫，收盘价告诉我们谁赢了"
"""

import logging
import pandas as pd
from enum import Enum
from typing import Optional, Tuple
from dataclasses import dataclass


def is_outside_bar(
    current_high: float, current_low: float,
    prev_high: float, prev_low: float
) -> bool:
    """
    判断是否是 Outside Bar
    
    Al Brooks 定义：当前 K 线完全包含前一根 K 线
    - 当前高点 > 前一根高点
    - 当前低点 < 前一根低点
    """
    return current_high > prev_high and current_low < prev_low


def get_outside_bar_bias(
    high: float, low: float, close: float
) -> str:
    """
    获取 Outside Bar 的方向偏好
    
    Al Brooks: "Outside Bar 本质上是市场的犹豫，收盘价告诉我们谁赢了"
    
    Returns:
        "bullish": 收盘在上半部分，看涨
        "bearish": 收盘在下半部分，看跌
        "neutral": 收盘在中间（少见）
    """
    bar_range = high - low
    if bar_range == 0:
        return "neutral"
    
    close_position = (close - low) / bar_range
    
    if close_position >= 0.55:  # 收盘在上55%区域
        return "bullish"
    elif close_position <= 0.45:  # 收盘在下45%区域
        return "bearish"
    else:
        return "neutral"


class HState(Enum):
    """H2 信号的状态机状态"""
    WAITING_FOR_PULLBACK = "等待回调"
    IN_PULLBACK = "回调中"
    H1_DETECTED = "H1已检测"
    WAITING_FOR_H2 = "等待H2"


class LState(Enum):
    """L2 信号的状态机状态"""
    WAITING_FOR_BOUNCE = "等待反弹"
    IN_BOUNCE = "反弹中"
    L1_DETECTED = "L1已检测"
    WAITING_FOR_L2 = "等待L2"


@dataclass
class H2Signal:
    """H2 信号数据"""
    signal_type: str
    side: str
    stop_loss: float
    base_height: float


@dataclass
class L2Signal:
    """L2 信号数据"""
    signal_type: str
    side: str
    stop_loss: float
    base_height: float


class H2StateMachine:
    """
    H2 状态机
    
    管理上升趋势中的回调买入逻辑
    """
    
    def __init__(self):
        self.reset()
    
    def reset(self):
        """重置状态机"""
        self.state = HState.WAITING_FOR_PULLBACK
        self.trend_high: Optional[float] = None
        self.pullback_start_low: Optional[float] = None
        self.h1_high: Optional[float] = None
        self.is_strong_trend = False
    
    def set_strong_trend(self):
        """标记为强趋势背景"""
        self.is_strong_trend = True
    
    def _validate_state(self) -> bool:
        """
        验证状态一致性（问题8修复）
        
        确保状态和相关变量的一致性，防止Outside Bar等边缘情况导致的状态混乱
        """
        if self.state == HState.WAITING_FOR_PULLBACK:
            # 等待回调状态：h1_high 应该为 None
            if self.h1_high is not None:
                self.h1_high = None
                return False
        elif self.state == HState.IN_PULLBACK:
            # 回调中状态：pullback_start_low 必须有值
            if self.pullback_start_low is None:
                self.state = HState.WAITING_FOR_PULLBACK
                return False
        elif self.state == HState.H1_DETECTED:
            # H1已检测状态：h1_high 必须有值
            if self.h1_high is None:
                self.state = HState.WAITING_FOR_PULLBACK
                return False
        elif self.state == HState.WAITING_FOR_H2:
            # 等待H2状态：h1_high 必须有值
            if self.h1_high is None:
                self.state = HState.WAITING_FOR_PULLBACK
                return False
        return True
    
    def update(
        self, close: float, high: float, low: float, ema: float,
        atr: Optional[float], df: pd.DataFrame, i: int,
        stop_loss_func
    ) -> Optional[H2Signal]:
        """
        更新状态机并检测信号
        
        参数:
            close, high, low: 当前K线数据
            ema: EMA值
            atr: ATR值
            df: 完整数据框
            i: 当前索引
            stop_loss_func: 止损计算函数
        
        返回:
            H2Signal 或 None
        """
        signal = None
        
        # 问题8修复：验证状态一致性
        self._validate_state()
        
        # 获取前一根 K 线数据用于 Outside Bar 检测
        prev_high = df.iloc[i - 1]["high"] if i > 0 else high
        prev_low = df.iloc[i - 1]["low"] if i > 0 else low
        
        if close > ema:
            if self.state == HState.WAITING_FOR_PULLBACK:
                if self.trend_high is None or high > self.trend_high:
                    self.trend_high = high
            
            elif self.state == HState.IN_PULLBACK:
                if self.trend_high is not None and high > self.trend_high:
                    self.state = HState.H1_DETECTED
                    self.h1_high = high
                    
                    if self.is_strong_trend:
                        stop_loss = stop_loss_func(df, i, "buy", close, atr)
                        base_height = (atr * 2) if atr and atr > 0 else 0
                        signal = H2Signal("H1_Buy", "buy", stop_loss, base_height)
                        self.is_strong_trend = False
            
            elif self.state == HState.H1_DETECTED:
                # ========== Outside Bar 处理（Al Brooks 原则）==========
                # Outside Bar 是市场犹豫的表现，收盘价决定方向
                is_ob = is_outside_bar(high, low, prev_high, prev_low)
                
                if is_ob:
                    # Outside Bar: 根据收盘价位置判断方向
                    ob_bias = get_outside_bar_bias(high, low, close)
                    
                    if ob_bias == "bullish":
                        # 看涨 Outside Bar: 趋势延续，更新高点
                        self.h1_high = high
                        logging.debug(
                            f"📊 H2状态机: 看涨 Outside Bar @ bar {i}, "
                            f"收盘偏上，趋势延续"
                        )
                    elif ob_bias == "bearish":
                        # 看跌 Outside Bar: 检查是否跌破回调起点
                        if self.pullback_start_low is not None and low < self.pullback_start_low:
                            # 跌破回调起点，重置状态机
                            self.state = HState.WAITING_FOR_PULLBACK
                            self.trend_high = high
                            self.pullback_start_low = None
                            self.h1_high = None
                            logging.debug(
                                f"📊 H2状态机: 看跌 Outside Bar @ bar {i}, "
                                f"跌破回调起点，重置状态机"
                            )
                        else:
                            # 未跌破回调起点，进入等待 H2 状态
                            self.state = HState.WAITING_FOR_H2
                            logging.debug(
                                f"📊 H2状态机: 看跌 Outside Bar @ bar {i}, "
                                f"进入等待 H2"
                            )
                    else:
                        # 中性 Outside Bar: 保持当前状态，更新高点
                        self.h1_high = max(self.h1_high or high, high)
                else:
                    # ========== 非 Outside Bar 的标准处理 ==========
                    if self.pullback_start_low is not None and low < self.pullback_start_low:
                        # 突破失败：低点跌破回调起点 -> 重置状态机
                        self.state = HState.WAITING_FOR_PULLBACK
                        self.trend_high = high if self.trend_high is None or high > self.trend_high else self.trend_high
                        self.pullback_start_low = None
                        self.h1_high = None
                    elif high > self.h1_high:
                        # 延续上涨：更新高点
                        self.h1_high = high
                    elif self.h1_high is not None and low < self.h1_high:
                        # 开始回调：进入等待 H2 状态
                        if self.pullback_start_low is not None and low >= self.pullback_start_low:
                            self.state = HState.WAITING_FOR_H2
                        elif self.pullback_start_low is None:
                            # 防护：如果 pullback_start_low 未设置，设置当前低点
                            self.pullback_start_low = low
                            self.state = HState.WAITING_FOR_H2
            
            elif self.state == HState.WAITING_FOR_H2:
                if self.h1_high is not None and high > self.h1_high:
                    stop_loss = stop_loss_func(df, i, "buy", close, atr)
                    base_height = (atr * 2) if atr and atr > 0 else 0
                    signal = H2Signal("H2_Buy", "buy", stop_loss, base_height)
                    
                    self.state = HState.WAITING_FOR_PULLBACK
                    self.trend_high = high
                    self.pullback_start_low = None
                    self.h1_high = None
                
                elif self.pullback_start_low is not None and low < self.pullback_start_low:
                    self.state = HState.WAITING_FOR_PULLBACK
                    self.trend_high = high if self.trend_high is None or high > self.trend_high else self.trend_high
                    self.pullback_start_low = None
                    self.h1_high = None
        
        else:  # close <= ema
            if self.state == HState.WAITING_FOR_PULLBACK:
                if close < ema or low < ema:
                    self.state = HState.IN_PULLBACK
                    self.pullback_start_low = low
            
            elif self.state == HState.IN_PULLBACK:
                if self.pullback_start_low is None or low < self.pullback_start_low:
                    self.pullback_start_low = low
            
            elif self.state == HState.H1_DETECTED:
                if self.pullback_start_low is not None and low < self.pullback_start_low:
                    self.state = HState.WAITING_FOR_PULLBACK
                    self.trend_high = None
                    self.pullback_start_low = None
                    self.h1_high = None
            
            elif self.state == HState.WAITING_FOR_H2:
                if self.pullback_start_low is not None and low < self.pullback_start_low:
                    self.state = HState.WAITING_FOR_PULLBACK
                    self.trend_high = None
                    self.pullback_start_low = None
                    self.h1_high = None
        
        return signal


class L2StateMachine:
    """
    L2 状态机
    
    管理下降趋势中的反弹卖出逻辑
    """
    
    def __init__(self):
        self.reset()
    
    def reset(self):
        """重置状态机"""
        self.state = LState.WAITING_FOR_BOUNCE
        self.trend_low: Optional[float] = None
        self.bounce_start_high: Optional[float] = None
        self.l1_low: Optional[float] = None
        self.is_strong_trend = False
    
    def set_strong_trend(self):
        """标记为强趋势背景"""
        self.is_strong_trend = True
    
    def _validate_state(self) -> bool:
        """
        验证状态一致性（问题8修复）
        
        确保状态和相关变量的一致性，防止Outside Bar等边缘情况导致的状态混乱
        """
        if self.state == LState.WAITING_FOR_BOUNCE:
            # 等待反弹状态：l1_low 应该为 None
            if self.l1_low is not None:
                self.l1_low = None
                return False
        elif self.state == LState.IN_BOUNCE:
            # 反弹中状态：bounce_start_high 必须有值
            if self.bounce_start_high is None:
                self.state = LState.WAITING_FOR_BOUNCE
                return False
        elif self.state == LState.L1_DETECTED:
            # L1已检测状态：l1_low 必须有值
            if self.l1_low is None:
                self.state = LState.WAITING_FOR_BOUNCE
                return False
        elif self.state == LState.WAITING_FOR_L2:
            # 等待L2状态：l1_low 必须有值
            if self.l1_low is None:
                self.state = LState.WAITING_FOR_BOUNCE
                return False
        return True
    
    def update(
        self, close: float, high: float, low: float, ema: float,
        atr: Optional[float], df: pd.DataFrame, i: int,
        stop_loss_func
    ) -> Optional[L2Signal]:
        """
        更新状态机并检测信号
        """
        signal = None
        
        # 问题8修复：验证状态一致性
        self._validate_state()
        
        # 获取前一根 K 线数据用于 Outside Bar 检测
        prev_high = df.iloc[i - 1]["high"] if i > 0 else high
        prev_low = df.iloc[i - 1]["low"] if i > 0 else low
        
        if close < ema:
            if self.state == LState.WAITING_FOR_BOUNCE:
                if self.trend_low is None or low < self.trend_low:
                    self.trend_low = low
            
            elif self.state == LState.IN_BOUNCE:
                if self.trend_low is not None and low < self.trend_low:
                    self.state = LState.L1_DETECTED
                    self.l1_low = low
                    
                    if self.is_strong_trend:
                        stop_loss = stop_loss_func(df, i, "sell", close, atr)
                        base_height = (atr * 2) if atr and atr > 0 else 0
                        signal = L2Signal("L1_Sell", "sell", stop_loss, base_height)
                        self.is_strong_trend = False
            
            elif self.state == LState.L1_DETECTED:
                # ========== Outside Bar 处理（Al Brooks 原则）==========
                # Outside Bar 是市场犹豫的表现，收盘价决定方向
                is_ob = is_outside_bar(high, low, prev_high, prev_low)
                
                if is_ob:
                    # Outside Bar: 根据收盘价位置判断方向
                    ob_bias = get_outside_bar_bias(high, low, close)
                    
                    if ob_bias == "bearish":
                        # 看跌 Outside Bar: 趋势延续，更新低点
                        self.l1_low = low
                        logging.debug(
                            f"📊 L2状态机: 看跌 Outside Bar @ bar {i}, "
                            f"收盘偏下，趋势延续"
                        )
                    elif ob_bias == "bullish":
                        # 看涨 Outside Bar: 检查是否突破反弹起点
                        if self.bounce_start_high is not None and high > self.bounce_start_high:
                            # 突破反弹起点，重置状态机
                            self.state = LState.WAITING_FOR_BOUNCE
                            self.trend_low = low
                            self.bounce_start_high = None
                            self.l1_low = None
                            logging.debug(
                                f"📊 L2状态机: 看涨 Outside Bar @ bar {i}, "
                                f"突破反弹起点，重置状态机"
                            )
                        else:
                            # 未突破反弹起点，进入等待 L2 状态
                            self.state = LState.WAITING_FOR_L2
                            logging.debug(
                                f"📊 L2状态机: 看涨 Outside Bar @ bar {i}, "
                                f"进入等待 L2"
                            )
                    else:
                        # 中性 Outside Bar: 保持当前状态，更新低点
                        self.l1_low = min(self.l1_low or low, low)
                else:
                    # ========== 非 Outside Bar 的标准处理 ==========
                    if self.bounce_start_high is not None and high > self.bounce_start_high:
                        # 突破失败：高点突破反弹起点 -> 重置状态机
                        self.state = LState.WAITING_FOR_BOUNCE
                        self.trend_low = low if self.trend_low is None or low < self.trend_low else self.trend_low
                        self.bounce_start_high = None
                        self.l1_low = None
                    elif low < self.l1_low:
                        # 延续下跌：更新低点
                        self.l1_low = low
                    elif self.l1_low is not None and high > self.l1_low:
                        # 开始反弹：进入等待 L2 状态
                        if self.bounce_start_high is not None and high <= self.bounce_start_high:
                            self.state = LState.WAITING_FOR_L2
                        elif self.bounce_start_high is None:
                            # 防护：如果 bounce_start_high 未设置，设置当前高点
                            self.bounce_start_high = high
                            self.state = LState.WAITING_FOR_L2
            
            elif self.state == LState.WAITING_FOR_L2:
                if self.l1_low is not None and low < self.l1_low:
                    stop_loss = stop_loss_func(df, i, "sell", close, atr)
                    base_height = (atr * 2) if atr and atr > 0 else 0
                    signal = L2Signal("L2_Sell", "sell", stop_loss, base_height)
                    
                    self.state = LState.WAITING_FOR_BOUNCE
                    self.trend_low = low
                    self.bounce_start_high = None
                    self.l1_low = None
                
                elif self.bounce_start_high is not None and high > self.bounce_start_high:
                    self.state = LState.WAITING_FOR_BOUNCE
                    self.trend_low = low if self.trend_low is None or low < self.trend_low else self.trend_low
                    self.bounce_start_high = None
                    self.l1_low = None
        
        else:  # close >= ema
            if self.state == LState.WAITING_FOR_BOUNCE:
                if close > ema or high > ema:
                    self.state = LState.IN_BOUNCE
                    self.bounce_start_high = high
            
            elif self.state == LState.IN_BOUNCE:
                if self.bounce_start_high is None or high > self.bounce_start_high:
                    self.bounce_start_high = high
            
            elif self.state == LState.L1_DETECTED:
                if self.bounce_start_high is not None and high > self.bounce_start_high:
                    self.state = LState.WAITING_FOR_BOUNCE
                    self.trend_low = None
                    self.bounce_start_high = None
                    self.l1_low = None
            
            elif self.state == LState.WAITING_FOR_L2:
                if self.bounce_start_high is not None and high > self.bounce_start_high:
                    self.state = LState.WAITING_FOR_BOUNCE
                    self.trend_low = None
                    self.bounce_start_high = None
                    self.l1_low = None
        
        return signal
