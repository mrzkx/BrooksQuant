"""
模式检测器

负责 Wedge、Failed Breakout、Spike、Climax 的检测逻辑

Al Brooks 核心模式：
- Strong Spike: 强突破直接入场
- Failed Breakout: 失败突破反转
- Wedge Reversal: 楔形反转（三次推进）
- Climax Reversal: 高潮竭尽反转
"""

import pandas as pd
from typing import Optional, Tuple
from .market_analyzer import MarketState


class PatternDetector:
    """
    模式检测器
    
    封装所有 Al Brooks 价格行为模式的检测逻辑
    """
    
    def __init__(self, lookback_period: int = 20):
        self.lookback_period = lookback_period
    
    @staticmethod
    def compute_body_size(row: pd.Series) -> float:
        """计算K线实体大小"""
        return abs(row["close"] - row["open"])
    
    @staticmethod
    def validate_signal_close(row: pd.Series, side: str) -> bool:
        """
        验证K线收盘价位置是否符合信号要求
        
        买入信号：收盘价必须在K线顶部25%区域
        卖出信号：收盘价必须在K线底部25%区域
        """
        high = row["high"]
        low = row["low"]
        close = row["close"]
        
        kline_range = high - low
        if kline_range == 0:
            return False
        
        if side == "buy":
            return (close - low) / kline_range >= 0.75
        else:
            return (high - close) / kline_range >= 0.75
    
    @staticmethod
    def calculate_unified_stop_loss(
        df: pd.DataFrame, i: int, side: str, entry_price: float, atr: Optional[float] = None
    ) -> float:
        """
        统一止损计算
        
        买入：min(前两根K线最低价, 入场价 - 2*ATR)
        卖出：max(前两根K线最高价, 入场价 + 2*ATR)
        """
        if i < 2:
            return entry_price * (0.98 if side == "buy" else 1.02)
        
        prev_bar_1 = df.iloc[i - 1]
        prev_bar_2 = df.iloc[i - 2]
        
        if side == "buy":
            two_bar_low = min(prev_bar_1["low"], prev_bar_2["low"])
            if atr and atr > 0:
                return min(two_bar_low, entry_price - (2 * atr))
            return two_bar_low
        else:
            two_bar_high = max(prev_bar_1["high"], prev_bar_2["high"])
            if atr and atr > 0:
                return max(two_bar_high, entry_price + (2 * atr))
            return two_bar_high
    
    def calculate_measured_move(
        self, df: pd.DataFrame, i: int, side: str, 
        market_state: MarketState, atr: Optional[float] = None
    ) -> float:
        """
        计算 Measured Move（测量涨幅）
        
        - 区间突破：base_height = 区间宽度
        - 强趋势：base_height = 前一个波动的长度
        - 默认：2 * ATR
        """
        if i < self.lookback_period:
            return (atr * 2) if atr and atr > 0 else 0
        
        lookback_data = df.iloc[max(0, i - self.lookback_period) : i + 1]
        
        try:
            if market_state == MarketState.TRADING_RANGE:
                range_high = lookback_data["high"].max()
                range_low = lookback_data["low"].min()
                base_height = range_high - range_low
                
                if atr and atr > 0:
                    if base_height < atr * 0.5 or base_height > atr * 5:
                        return atr * 2
                
                return base_height
            
            elif market_state in [MarketState.BREAKOUT, MarketState.CHANNEL]:
                lows = lookback_data["low"].values
                highs = lookback_data["high"].values
                
                if side == "buy":
                    recent_low_idx = None
                    for j in range(len(lows) - 2, 0, -1):
                        if lows[j] < lows[j-1] and lows[j] < lows[j+1]:
                            recent_low_idx = j
                            break
                    
                    if recent_low_idx is not None:
                        base_height = highs[recent_low_idx:].max() - lows[recent_low_idx]
                    else:
                        base_height = lookback_data["high"].max() - lookback_data["low"].min()
                else:
                    recent_high_idx = None
                    for j in range(len(highs) - 2, 0, -1):
                        if highs[j] > highs[j-1] and highs[j] > highs[j+1]:
                            recent_high_idx = j
                            break
                    
                    if recent_high_idx is not None:
                        base_height = highs[recent_high_idx] - lows[recent_high_idx:].min()
                    else:
                        base_height = lookback_data["high"].max() - lookback_data["low"].min()
                
                if atr and atr > 0:
                    if base_height < atr * 0.5 or base_height > atr * 8:
                        return atr * 2
                
                return base_height
        
        except Exception:
            pass
        
        return (atr * 2) if atr and atr > 0 else 0
    
    def detect_strong_spike(
        self, df: pd.DataFrame, i: int, ema: float, atr: Optional[float] = None,
        market_state: Optional[MarketState] = None
    ) -> Optional[Tuple[str, str, float, Optional[float], float]]:
        """
        检测 Strong Spike（强突破入场）
        
        Al Brooks 核心原则：
        1. 严禁在 TRADING_RANGE 中做突破单
        2. 只在 BREAKOUT/CHANNEL/TIGHT_CHANNEL 状态下交易
        3. 连续性检查：过去2根K线必须同向
        
        返回: (signal_type, side, stop_loss, limit_price, base_height) 或 None
        """
        if i < 2:
            return None
        
        if market_state == MarketState.TRADING_RANGE:
            return None
        
        if market_state not in [MarketState.BREAKOUT, MarketState.CHANNEL, MarketState.TIGHT_CHANNEL]:
            return None
        
        recent_bodies = [
            self.compute_body_size(df.iloc[j]) for j in range(max(0, i - 10), i)
        ]
        if not recent_bodies:
            return None
        
        avg_body = sum(recent_bodies) / len(recent_bodies)
        current_body = self.compute_body_size(df.iloc[i])
        
        if avg_body == 0 or current_body < avg_body * 2:
            return None
        
        close = df.iloc[i]["close"]
        high = df.iloc[i]["high"]
        low = df.iloc[i]["low"]
        open_price = df.iloc[i]["open"]
        
        prev_bar = df.iloc[i - 1]
        prev_prev_bar = df.iloc[i - 2]
        
        # ATR 过滤：Climax 不追涨
        if atr is not None and atr > 0:
            if (high - low) > atr * 3.5:
                return None
        
        # 向上突破
        if close > ema and close > open_price:
            body_ratio = (close - low) / (high - low) if (high - low) > 0 else 0
            if body_ratio > 0.8:
                if not (prev_bar["close"] > prev_bar["open"] and prev_prev_bar["close"] > prev_prev_bar["open"]):
                    return None
                
                stop_loss = self.calculate_unified_stop_loss(df, i, "buy", close, atr)
                base_height = self.calculate_measured_move(df, i, "buy", market_state, atr)
                
                distance_from_ema = abs(close - ema)
                if atr is not None and atr > 0 and distance_from_ema > atr * 3:
                    prev_body_mid = (prev_bar["open"] + prev_bar["close"]) / 2
                    limit_price = max(prev_body_mid, prev_bar["close"])
                    return ("Spike_Buy", "buy", stop_loss, limit_price, base_height)
                
                return ("Spike_Buy", "buy", stop_loss, None, base_height)
        
        # 向下突破
        elif close < ema and close < open_price:
            body_ratio = (high - close) / (high - low) if (high - low) > 0 else 0
            if body_ratio > 0.8:
                if not (prev_bar["close"] < prev_bar["open"] and prev_prev_bar["close"] < prev_prev_bar["open"]):
                    return None
                
                stop_loss = self.calculate_unified_stop_loss(df, i, "sell", close, atr)
                base_height = self.calculate_measured_move(df, i, "sell", market_state, atr)
                
                distance_from_ema = abs(ema - close)
                if atr is not None and atr > 0 and distance_from_ema > atr * 3:
                    prev_body_mid = (prev_bar["open"] + prev_bar["close"]) / 2
                    limit_price = min(prev_body_mid, prev_bar["close"])
                    return ("Spike_Sell", "sell", stop_loss, limit_price, base_height)
                
                return ("Spike_Sell", "sell", stop_loss, None, base_height)
        
        return None
    
    def detect_climax_reversal(
        self, df: pd.DataFrame, i: int, ema: float, atr: Optional[float] = None
    ) -> Optional[Tuple[str, str, float, float]]:
        """
        检测 Climax 反转信号
        
        当检测到 Climax（Spike 长度超过 3.5 倍 ATR）后，寻找反转信号
        
        返回: (signal_type, side, stop_loss, base_height) 或 None
        """
        if i < 2 or atr is None or atr <= 0:
            return None
        
        current_bar = df.iloc[i]
        prev_bar = df.iloc[i - 1]
        
        close = current_bar["close"]
        open_price = current_bar["open"]
        prev_close = prev_bar["close"]
        prev_high = prev_bar["high"]
        prev_low = prev_bar["low"]
        prev_open = prev_bar["open"]
        prev_range = prev_high - prev_low
        
        # 向上 Climax
        if prev_range > atr * 3.5 and prev_close > prev_open:
            if close < open_price and close < prev_close:
                if not self.validate_signal_close(current_bar, "sell"):
                    return None
                
                stop_loss = self.calculate_unified_stop_loss(df, i, "sell", close, atr)
                return ("Climax_Sell", "sell", stop_loss, prev_range)
        
        # 向下 Climax
        if prev_range > atr * 3.5 and prev_close < prev_open:
            if close > open_price and close > prev_close:
                if not self.validate_signal_close(current_bar, "buy"):
                    return None
                
                stop_loss = self.calculate_unified_stop_loss(df, i, "buy", close, atr)
                return ("Climax_Buy", "buy", stop_loss, prev_range)
        
        return None
    
    def detect_failed_breakout(
        self, df: pd.DataFrame, i: int, ema: float, atr: Optional[float] = None,
        market_state: Optional[MarketState] = None
    ) -> Optional[Tuple[str, str, float, float]]:
        """
        检测 Failed Breakout（失败突破反转）
        
        仅在 TRADING_RANGE 状态下激活
        
        返回: (signal_type, side, stop_loss, base_height) 或 None
        """
        if i < self.lookback_period + 1:
            return None
        
        if market_state != MarketState.TRADING_RANGE:
            return None
        
        current_high = df.iloc[i]["high"]
        current_low = df.iloc[i]["low"]
        current_bar = df.iloc[i]
        
        lookback_highs = [df.iloc[j]["high"] for j in range(max(0, i - self.lookback_period), i)]
        lookback_lows = [df.iloc[j]["low"] for j in range(max(0, i - self.lookback_period), i)]
        
        max_lookback_high = max(lookback_highs) if lookback_highs else current_high
        min_lookback_low = min(lookback_lows) if lookback_lows else current_low
        
        lookback_range = df.iloc[max(0, i - self.lookback_period) : i + 1]
        range_width = lookback_range["high"].max() - lookback_range["low"].min()
        
        # 创新高后反转
        if current_high > max_lookback_high:
            if current_bar["close"] < current_bar["open"] and current_bar["close"] < current_bar["high"] * 0.98:
                if not self.validate_signal_close(current_bar, "sell"):
                    return None
                
                stop_loss = self.calculate_unified_stop_loss(df, i, "sell", current_bar["close"], atr)
                return ("FailedBreakout_Sell", "sell", stop_loss, range_width)
        
        # 创新低后反转
        if current_low < min_lookback_low:
            if current_bar["close"] > current_bar["open"] and current_bar["close"] > current_bar["low"] * 1.02:
                if not self.validate_signal_close(current_bar, "buy"):
                    return None
                
                stop_loss = self.calculate_unified_stop_loss(df, i, "buy", current_bar["close"], atr)
                return ("FailedBreakout_Buy", "buy", stop_loss, range_width)
        
        return None
    
    def detect_wedge_reversal(
        self, df: pd.DataFrame, i: int, ema: float, atr: Optional[float] = None
    ) -> Optional[Tuple[str, str, float, float]]:
        """
        检测 Wedge Reversal（楔形反转，三次推进）
        
        条件：
        1. 第1次和第3次推进之间至少相隔15根K线
        2. 相邻推进间隔至少3根K线
        3. 实体缩减
        4. 第3次推进显示疲软
        
        返回: (signal_type, side, stop_loss, base_height) 或 None
        """
        if i < 20:
            return None
        
        lookback_start = max(0, i - 30)
        recent_data = df.iloc[lookback_start : i + 1]
        
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
                    
                    if peak_indices[2] - peak_indices[0] < 15:
                        pass
                    elif peak_indices[1] - peak_indices[0] < 3 or peak_indices[2] - peak_indices[1] < 3:
                        pass
                    elif self.compute_body_size(df.iloc[peak_indices[2]]) >= self.compute_body_size(df.iloc[peak_indices[0]]):
                        pass
                    else:
                        third_bar = df.iloc[peak_indices[2]]
                        is_bearish = third_bar["close"] < third_bar["open"]
                        upper_shadow = third_bar["high"] - max(third_bar["open"], third_bar["close"])
                        body_size = abs(third_bar["close"] - third_bar["open"])
                        has_long_upper = upper_shadow > body_size * 2 if body_size > 0 else upper_shadow > (third_bar["high"] - third_bar["low"]) * 0.3
                        
                        if is_bearish or has_long_upper:
                            current_close = df.iloc[i]["close"]
                            if current_close < peak_values[2] * 0.98:
                                current_bar = df.iloc[i]
                                if self.validate_signal_close(current_bar, "sell"):
                                    stop_loss = self.calculate_unified_stop_loss(df, i, "sell", current_close, atr)
                                    wedge_height = peak_values[2] - peak_values[0]
                                    return ("Wedge_Sell", "sell", stop_loss, wedge_height)
        
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
                    
                    if trough_indices[2] - trough_indices[0] < 15:
                        pass
                    elif trough_indices[1] - trough_indices[0] < 3 or trough_indices[2] - trough_indices[1] < 3:
                        pass
                    elif self.compute_body_size(df.iloc[trough_indices[2]]) >= self.compute_body_size(df.iloc[trough_indices[0]]):
                        pass
                    else:
                        third_bar = df.iloc[trough_indices[2]]
                        is_bullish = third_bar["close"] > third_bar["open"]
                        lower_shadow = min(third_bar["open"], third_bar["close"]) - third_bar["low"]
                        body_size = abs(third_bar["close"] - third_bar["open"])
                        has_long_lower = lower_shadow > body_size * 2 if body_size > 0 else lower_shadow > (third_bar["high"] - third_bar["low"]) * 0.3
                        
                        if is_bullish or has_long_lower:
                            current_close = df.iloc[i]["close"]
                            if current_close > trough_values[2] * 1.02:
                                current_bar = df.iloc[i]
                                if self.validate_signal_close(current_bar, "buy"):
                                    stop_loss = self.calculate_unified_stop_loss(df, i, "buy", current_close, atr)
                                    wedge_height = trough_values[0] - trough_values[2]
                                    return ("Wedge_Buy", "buy", stop_loss, wedge_height)
        
        return None
