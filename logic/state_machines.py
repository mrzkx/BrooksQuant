"""
H2/L2 状态机管理

负责 HState 和 LState 的复杂状态机管理

Al Brooks H2/L2 回调策略：
- H2: 上升趋势中的第二次回调买入点
- L2: 下降趋势中的第二次反弹卖出点
"""

import pandas as pd
from enum import Enum
from typing import Optional
from dataclasses import dataclass


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
                # Outside Bar 处理：优先检测突破失败（低点突破）
                # 因为失败信号比延续信号更重要
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
                # Outside Bar 处理：优先检测突破失败（高点突破）
                # 因为失败信号比延续信号更重要
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
