"""
H2/L2 状态机管理

负责 HState 和 LState 的复杂状态机管理

Al Brooks H2/L2 回调策略（修正版）：
- H2: 上升趋势中的第二次回调买入点（Higher High 2）
- L2: 下降趋势中的第二次反弹卖出点（Lower Low 2）

Al Brooks H2/L2 定义（核心原则）：
- H2 是基于 swing high/low 结构识别：
  1. 上升趋势中，价格回调后创出第一个 Higher High（H1）
  2. 再次回调后，第二次突破 H1 高点即为 H2
- EMA 作为**趋势过滤器**，而非 H2/L2 的触发条件
- 增加 EMA 容差（ema_tolerance），允许价格略低于 EMA 仍视为趋势中

Outside Bar 处理原则 (Al Brooks)：
- Outside Bar 是指当前 K 线高点 > 前一根高点，且低点 < 前一根低点
- Outside Bar 的方向由收盘价位置决定：
  - 收盘在上半部分 (>50%) = 看涨 Outside Bar
  - 收盘在下半部分 (<50%) = 看跌 Outside Bar
- "Outside Bar 本质上是市场的犹豫，收盘价告诉我们谁赢了"

Tight Channel H1/L1 风险 (Al Brooks)：
- 在 Tight Channel 中，第一次回调（H1/L1）通常失败
- 成功率 < 40%，应标记为高风险或跳过
"""

import logging
import pandas as pd
from enum import Enum
from typing import Optional, Tuple
from dataclasses import dataclass

from .market_analyzer import MarketState


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
    is_high_risk: bool = False  # Al Brooks: H1 在 Tight Channel 中成功率 < 40%


@dataclass
class L2Signal:
    """L2 信号数据"""
    signal_type: str
    side: str
    stop_loss: float
    base_height: float
    is_high_risk: bool = False  # Al Brooks: L1 在 Tight Channel 中成功率 < 40%


class H2StateMachine:
    """
    H2 状态机（Al Brooks 修正版）
    
    管理上升趋势中的回调买入逻辑
    
    Al Brooks H2 定义：
    - H2 是 "Higher High 2"，即第二次突破 H1 高点
    - EMA 作为趋势过滤器，而非触发条件
    - 增加 ema_tolerance 允许价格略低于 EMA 仍视为趋势中
    """
    
    # EMA 容差：价格在 EMA ± tolerance% 内仍视为在趋势中
    # Al Brooks: "价格靠近 EMA 时仍可能处于趋势中，不应过于刚性"
    EMA_TOLERANCE_PCT = 0.003  # 0.3%
    
    def __init__(self, ema_tolerance: Optional[float] = None):
        """
        初始化 H2 状态机
        
        Args:
            ema_tolerance: EMA 容差比例（默认 0.3%）
        """
        self.ema_tolerance = ema_tolerance if ema_tolerance is not None else self.EMA_TOLERANCE_PCT
        self.reset()
    
    def reset(self):
        """重置状态机"""
        self.state = HState.WAITING_FOR_PULLBACK
        self.trend_high: Optional[float] = None
        self.pullback_start_low: Optional[float] = None
        self.h1_high: Optional[float] = None
        self.h1_bar_index: Optional[int] = None  # Al Brooks: Counting Bars - 记录 H1 的索引
        self.is_strong_trend = False
    
    def set_strong_trend(self):
        """标记为强趋势背景"""
        self.is_strong_trend = True
    
    def _is_above_ema_with_tolerance(self, close: float, ema: float) -> bool:
        """
        判断价格是否在 EMA 上方（带容差）
        
        Al Brooks 原则：EMA 作为趋势过滤器，而非刚性边界
        价格略低于 EMA（在容差范围内）仍可视为在上升趋势中
        
        Args:
            close: 当前收盘价
            ema: EMA 值
        
        Returns:
            True 如果价格 >= EMA * (1 - tolerance)
        """
        if ema <= 0:
            return False
        return close >= ema * (1 - self.ema_tolerance)
    
    def _has_counting_bars(
        self, df: pd.DataFrame, h1_idx: int, h2_idx: int, min_bars: int = 1
    ) -> Tuple[bool, int]:
        """
        验证 H1 → H2 之间是否有足够的 Counting Bars（空头 K 线）
        
        Al Brooks: "H2 的有效性取决于 H1 后的回调深度。
        如果 H1→H2 之间没有空头棒，说明回调太浅，信号无效。"
        
        Counting Bars 定义：收盘 < 开盘 的 K 线（阴线）
        
        Args:
            df: K线数据
            h1_idx: H1 K 线索引
            h2_idx: H2 K 线索引（当前 K 线）
            min_bars: 最少需要的空头 K 线数量
        
        Returns:
            (is_valid, bearish_bar_count)
        """
        if h1_idx is None or h1_idx >= h2_idx:
            return (False, 0)
        
        bearish_count = 0
        for j in range(h1_idx + 1, h2_idx):
            if j >= len(df):
                break
            bar = df.iloc[j]
            if float(bar["close"]) < float(bar["open"]):
                bearish_count += 1
        
        return (bearish_count >= min_bars, bearish_count)
    
    def _validate_state(self) -> bool:
        """
        验证状态一致性（问题8修复）
        
        确保状态和相关变量的一致性，防止Outside Bar等边缘情况导致的状态混乱
        """
        if self.state == HState.WAITING_FOR_PULLBACK:
            # 等待回调状态：h1_high 和 h1_bar_index 应该为 None
            if self.h1_high is not None:
                self.h1_high = None
                self.h1_bar_index = None
                return False
        elif self.state == HState.IN_PULLBACK:
            # 回调中状态：pullback_start_low 必须有值
            if self.pullback_start_low is None:
                self.state = HState.WAITING_FOR_PULLBACK
                return False
        elif self.state == HState.H1_DETECTED:
            # H1已检测状态：h1_high 和 h1_bar_index 必须有值
            if self.h1_high is None or self.h1_bar_index is None:
                self.state = HState.WAITING_FOR_PULLBACK
                self.h1_high = None
                self.h1_bar_index = None
                return False
        elif self.state == HState.WAITING_FOR_H2:
            # 等待H2状态：h1_high 和 h1_bar_index 必须有值
            if self.h1_high is None or self.h1_bar_index is None:
                self.state = HState.WAITING_FOR_PULLBACK
                self.h1_high = None
                self.h1_bar_index = None
                return False
        return True
    
    def update(
        self, close: float, high: float, low: float, ema: float,
        atr: Optional[float], df: pd.DataFrame, i: int,
        stop_loss_func,
        market_state: Optional[MarketState] = None,
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
            market_state: 市场状态（用于 H1 风险标记）
        
        返回:
            H2Signal 或 None
        """
        signal = None
        
        # 问题8修复：验证状态一致性
        self._validate_state()
        
        # Al Brooks: Tight Channel 中 H1 成功率 < 40%
        is_tight_channel = market_state == MarketState.TIGHT_CHANNEL
        
        # 获取前一根 K 线数据用于 Outside Bar 检测
        prev_high = df.iloc[i - 1]["high"] if i > 0 else high
        prev_low = df.iloc[i - 1]["low"] if i > 0 else low
        
        # Al Brooks 修正：使用带容差的 EMA 判断
        # 价格略低于 EMA（在容差范围内）仍可视为在上升趋势中
        is_in_uptrend = self._is_above_ema_with_tolerance(close, ema)
        
        if is_in_uptrend:
            if self.state == HState.WAITING_FOR_PULLBACK:
                if self.trend_high is None or high > self.trend_high:
                    self.trend_high = high
            
            elif self.state == HState.IN_PULLBACK:
                if self.trend_high is not None and high > self.trend_high:
                    self.state = HState.H1_DETECTED
                    self.h1_high = high
                    self.h1_bar_index = i  # Al Brooks: Counting Bars - 记录 H1 出现的索引
                    
                    if self.is_strong_trend:
                        stop_loss = stop_loss_func(df, i, "buy", close, atr)
                        if stop_loss is not None:
                            base_height = (atr * 2) if atr and atr > 0 else 0
                            # Al Brooks: Tight Channel 中 H1 标记为高风险
                            signal = H2Signal("H1_Buy", "buy", stop_loss, base_height, is_high_risk=is_tight_channel)
                            if is_tight_channel:
                                logging.debug(f"⚠️ H1_Buy 高风险: Tight Channel 中 H1 成功率 < 40%")
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
                        self.h1_bar_index = i  # 更新 H1 索引
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
                            self.h1_bar_index = None
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
                        if high > (self.h1_high or high):
                            self.h1_high = high
                            self.h1_bar_index = i
                else:
                    # ========== 非 Outside Bar 的标准处理 ==========
                    if self.pullback_start_low is not None and low < self.pullback_start_low:
                        # 突破失败：低点跌破回调起点 -> 重置状态机
                        self.state = HState.WAITING_FOR_PULLBACK
                        self.trend_high = high if self.trend_high is None or high > self.trend_high else self.trend_high
                        self.pullback_start_low = None
                        self.h1_high = None
                        self.h1_bar_index = None
                    elif high > self.h1_high:
                        # 延续上涨：更新高点
                        self.h1_high = high
                        self.h1_bar_index = i  # 更新 H1 索引
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
                    # ========== Al Brooks 修正：Counting Bars 验证 ==========
                    # H2 需要 H1→H2 之间至少有 1 根空头 K 线（阴线）
                    # 如果没有空头棒，说明回调太浅，信号无效
                    has_counting, bearish_count = self._has_counting_bars(
                        df, self.h1_bar_index, i, min_bars=1
                    )
                    
                    if has_counting:
                        stop_loss = stop_loss_func(df, i, "buy", close, atr)
                        if stop_loss is not None:
                            base_height = (atr * 2) if atr and atr > 0 else 0
                            signal = H2Signal("H2_Buy", "buy", stop_loss, base_height)
                            logging.debug(
                                f"✅ H2_Buy 触发: H1@{self.h1_bar_index}, "
                                f"Counting Bars={bearish_count}"
                            )
                    else:
                        logging.debug(
                            f"⚠️ H2 跳过: H1→H2 之间无空头棒 (回调太浅), "
                            f"H1@{self.h1_bar_index}, H2@{i}"
                        )
                    
                    self.state = HState.WAITING_FOR_PULLBACK
                    self.trend_high = high
                    self.pullback_start_low = None
                    self.h1_high = None
                    self.h1_bar_index = None
                
                elif self.pullback_start_low is not None and low < self.pullback_start_low:
                    self.state = HState.WAITING_FOR_PULLBACK
                    self.trend_high = high if self.trend_high is None or high > self.trend_high else self.trend_high
                    self.pullback_start_low = None
                    self.h1_high = None
                    self.h1_bar_index = None
        
        else:  # not in uptrend (close below EMA - tolerance)
            if self.state == HState.WAITING_FOR_PULLBACK:
                # Al Brooks: 回调开始 - 价格明确跌破 EMA
                if close < ema * (1 - self.ema_tolerance) or low < ema * (1 - self.ema_tolerance):
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
                    self.h1_bar_index = None
            
            elif self.state == HState.WAITING_FOR_H2:
                if self.pullback_start_low is not None and low < self.pullback_start_low:
                    self.state = HState.WAITING_FOR_PULLBACK
                    self.trend_high = None
                    self.pullback_start_low = None
                    self.h1_high = None
                    self.h1_bar_index = None
        
        return signal


class L2StateMachine:
    """
    L2 状态机（Al Brooks 修正版）
    
    管理下降趋势中的反弹卖出逻辑
    
    Al Brooks L2 定义：
    - L2 是 "Lower Low 2"，即第二次跌破 L1 低点
    - EMA 作为趋势过滤器，而非触发条件
    - 增加 ema_tolerance 允许价格略高于 EMA 仍视为下降趋势中
    """
    
    # EMA 容差：价格在 EMA ± tolerance% 内仍视为在趋势中
    EMA_TOLERANCE_PCT = 0.003  # 0.3%
    
    def __init__(self, ema_tolerance: Optional[float] = None):
        """
        初始化 L2 状态机
        
        Args:
            ema_tolerance: EMA 容差比例（默认 0.3%）
        """
        self.ema_tolerance = ema_tolerance if ema_tolerance is not None else self.EMA_TOLERANCE_PCT
        self.reset()
    
    def reset(self):
        """重置状态机"""
        self.state = LState.WAITING_FOR_BOUNCE
        self.trend_low: Optional[float] = None
        self.bounce_start_high: Optional[float] = None
        self.l1_low: Optional[float] = None
        self.l1_bar_index: Optional[int] = None  # Al Brooks: Counting Bars - 记录 L1 的索引
        self.is_strong_trend = False
    
    def set_strong_trend(self):
        """标记为强趋势背景"""
        self.is_strong_trend = True
    
    def _is_below_ema_with_tolerance(self, close: float, ema: float) -> bool:
        """
        判断价格是否在 EMA 下方（带容差）
        
        Al Brooks 原则：EMA 作为趋势过滤器，而非刚性边界
        价格略高于 EMA（在容差范围内）仍可视为在下降趋势中
        
        Args:
            close: 当前收盘价
            ema: EMA 值
        
        Returns:
            True 如果价格 <= EMA * (1 + tolerance)
        """
        if ema <= 0:
            return False
        return close <= ema * (1 + self.ema_tolerance)
    
    def _has_counting_bars(
        self, df: pd.DataFrame, l1_idx: int, l2_idx: int, min_bars: int = 1
    ) -> Tuple[bool, int]:
        """
        验证 L1 → L2 之间是否有足够的 Counting Bars（多头 K 线）
        
        Al Brooks: "L2 的有效性取决于 L1 后的反弹深度。
        如果 L1→L2 之间没有多头棒，说明反弹太浅，信号无效。"
        
        Counting Bars 定义：收盘 > 开盘 的 K 线（阳线）
        
        Args:
            df: K线数据
            l1_idx: L1 K 线索引
            l2_idx: L2 K 线索引（当前 K 线）
            min_bars: 最少需要的多头 K 线数量
        
        Returns:
            (is_valid, bullish_bar_count)
        """
        if l1_idx is None or l1_idx >= l2_idx:
            return (False, 0)
        
        bullish_count = 0
        for j in range(l1_idx + 1, l2_idx):
            if j >= len(df):
                break
            bar = df.iloc[j]
            if float(bar["close"]) > float(bar["open"]):
                bullish_count += 1
        
        return (bullish_count >= min_bars, bullish_count)
    
    def _validate_state(self) -> bool:
        """
        验证状态一致性（问题8修复）
        
        确保状态和相关变量的一致性，防止Outside Bar等边缘情况导致的状态混乱
        """
        if self.state == LState.WAITING_FOR_BOUNCE:
            # 等待反弹状态：l1_low 和 l1_bar_index 应该为 None
            if self.l1_low is not None:
                self.l1_low = None
                self.l1_bar_index = None
                return False
        elif self.state == LState.IN_BOUNCE:
            # 反弹中状态：bounce_start_high 必须有值
            if self.bounce_start_high is None:
                self.state = LState.WAITING_FOR_BOUNCE
                return False
        elif self.state == LState.L1_DETECTED:
            # L1已检测状态：l1_low 和 l1_bar_index 必须有值
            if self.l1_low is None or self.l1_bar_index is None:
                self.state = LState.WAITING_FOR_BOUNCE
                self.l1_low = None
                self.l1_bar_index = None
                return False
        elif self.state == LState.WAITING_FOR_L2:
            # 等待L2状态：l1_low 和 l1_bar_index 必须有值
            if self.l1_low is None or self.l1_bar_index is None:
                self.state = LState.WAITING_FOR_BOUNCE
                self.l1_low = None
                self.l1_bar_index = None
                return False
        return True
    
    def update(
        self, close: float, high: float, low: float, ema: float,
        atr: Optional[float], df: pd.DataFrame, i: int,
        stop_loss_func,
        market_state: Optional[MarketState] = None,
    ) -> Optional[L2Signal]:
        """
        更新状态机并检测信号
        
        参数:
            close, high, low: 当前K线数据
            ema: EMA值
            atr: ATR值
            df: 完整数据框
            i: 当前索引
            stop_loss_func: 止损计算函数
            market_state: 市场状态（用于 L1 风险标记）
        """
        signal = None
        
        # 问题8修复：验证状态一致性
        self._validate_state()
        
        # Al Brooks: Tight Channel 中 L1 成功率 < 40%
        is_tight_channel = market_state == MarketState.TIGHT_CHANNEL
        
        # 获取前一根 K 线数据用于 Outside Bar 检测
        prev_high = df.iloc[i - 1]["high"] if i > 0 else high
        prev_low = df.iloc[i - 1]["low"] if i > 0 else low
        
        # Al Brooks 修正：使用带容差的 EMA 判断
        # 价格略高于 EMA（在容差范围内）仍可视为在下降趋势中
        is_in_downtrend = self._is_below_ema_with_tolerance(close, ema)
        
        if is_in_downtrend:
            if self.state == LState.WAITING_FOR_BOUNCE:
                if self.trend_low is None or low < self.trend_low:
                    self.trend_low = low
            
            elif self.state == LState.IN_BOUNCE:
                if self.trend_low is not None and low < self.trend_low:
                    self.state = LState.L1_DETECTED
                    self.l1_low = low
                    self.l1_bar_index = i  # Al Brooks: Counting Bars - 记录 L1 出现的索引
                    
                    if self.is_strong_trend:
                        stop_loss = stop_loss_func(df, i, "sell", close, atr)
                        if stop_loss is not None:
                            base_height = (atr * 2) if atr and atr > 0 else 0
                            # Al Brooks: Tight Channel 中 L1 标记为高风险
                            signal = L2Signal("L1_Sell", "sell", stop_loss, base_height, is_high_risk=is_tight_channel)
                            if is_tight_channel:
                                logging.debug(f"⚠️ L1_Sell 高风险: Tight Channel 中 L1 成功率 < 40%")
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
                        self.l1_bar_index = i  # 更新 L1 索引
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
                            self.l1_bar_index = None
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
                        if low < (self.l1_low or low):
                            self.l1_low = low
                            self.l1_bar_index = i
                else:
                    # ========== 非 Outside Bar 的标准处理 ==========
                    if self.bounce_start_high is not None and high > self.bounce_start_high:
                        # 突破失败：高点突破反弹起点 -> 重置状态机
                        self.state = LState.WAITING_FOR_BOUNCE
                        self.trend_low = low if self.trend_low is None or low < self.trend_low else self.trend_low
                        self.bounce_start_high = None
                        self.l1_low = None
                        self.l1_bar_index = None
                    elif low < self.l1_low:
                        # 延续下跌：更新低点
                        self.l1_low = low
                        self.l1_bar_index = i  # 更新 L1 索引
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
                    # ========== Al Brooks 修正：Counting Bars 验证 ==========
                    # L2 需要 L1→L2 之间至少有 1 根多头 K 线（阳线）
                    # 如果没有多头棒，说明反弹太浅，信号无效
                    has_counting, bullish_count = self._has_counting_bars(
                        df, self.l1_bar_index, i, min_bars=1
                    )
                    
                    if has_counting:
                        stop_loss = stop_loss_func(df, i, "sell", close, atr)
                        if stop_loss is not None:
                            base_height = (atr * 2) if atr and atr > 0 else 0
                            signal = L2Signal("L2_Sell", "sell", stop_loss, base_height)
                            logging.debug(
                                f"✅ L2_Sell 触发: L1@{self.l1_bar_index}, "
                                f"Counting Bars={bullish_count}"
                            )
                    else:
                        logging.debug(
                            f"⚠️ L2 跳过: L1→L2 之间无多头棒 (反弹太浅), "
                            f"L1@{self.l1_bar_index}, L2@{i}"
                        )
                    
                    self.state = LState.WAITING_FOR_BOUNCE
                    self.trend_low = low
                    self.bounce_start_high = None
                    self.l1_low = None
                    self.l1_bar_index = None
                
                elif self.bounce_start_high is not None and high > self.bounce_start_high:
                    self.state = LState.WAITING_FOR_BOUNCE
                    self.trend_low = low if self.trend_low is None or low < self.trend_low else self.trend_low
                    self.bounce_start_high = None
                    self.l1_low = None
                    self.l1_bar_index = None
        
        else:  # not in downtrend (close above EMA + tolerance)
            if self.state == LState.WAITING_FOR_BOUNCE:
                # Al Brooks: 反弹开始 - 价格明确突破 EMA
                if close > ema * (1 + self.ema_tolerance) or high > ema * (1 + self.ema_tolerance):
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
                    self.l1_bar_index = None
            
            elif self.state == LState.WAITING_FOR_L2:
                if self.bounce_start_high is not None and high > self.bounce_start_high:
                    self.state = LState.WAITING_FOR_BOUNCE
                    self.trend_low = None
                    self.bounce_start_high = None
                    self.l1_low = None
                    self.l1_bar_index = None
        
        return signal
