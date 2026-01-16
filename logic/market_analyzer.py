"""
市场状态分析器

负责 MarketState（含 TightChannel）的识别逻辑

Al Brooks 核心市场状态：
- BREAKOUT: 强趋势突破
- CHANNEL: 通道模式，EMA附近有序运行
- TRADING_RANGE: 交易区间，价格频繁穿越EMA
- TIGHT_CHANNEL: 紧凑通道，强劲单边趋势（禁止反转）
"""

import pandas as pd
from enum import Enum
from typing import Optional


class MarketState(Enum):
    """市场状态分类"""
    BREAKOUT = "Breakout"
    CHANNEL = "Channel"
    TRADING_RANGE = "TradingRange"
    TIGHT_CHANNEL = "TightChannel"


class MarketAnalyzer:
    """
    市场状态分析器
    
    负责检测当前市场处于哪种状态，指导信号生成策略
    """
    
    def __init__(self, ema_period: int = 20):
        self.ema_period = ema_period
    
    @staticmethod
    def compute_body_size(row: pd.Series) -> float:
        """计算K线实体大小"""
        return abs(row["close"] - row["open"])
    
    def detect_market_state(self, df: pd.DataFrame, i: int, ema: float) -> MarketState:
        """
        检测当前市场状态
        
        优先级：
        1. Tight Channel（紧凑通道）- 最高优先级
        2. Breakout（强趋势突破）
        3. Trading Range（交易区间）
        4. Channel（通道模式）- 默认
        """
        if i < 10:
            return MarketState.CHANNEL
        
        # 优先检测 TIGHT_CHANNEL
        tight_channel_state = self._detect_tight_channel(df, i, ema)
        if tight_channel_state is not None:
            return tight_channel_state
        
        # 计算最近20根K线的EMA穿越次数
        recent = df.iloc[max(0, i - 20) : i + 1]
        ema_crosses = 0
        prev_above = None
        
        for idx in recent.index:
            close = recent.at[idx, "close"]
            above_ema = close > recent.at[idx, "ema"]
            if prev_above is not None and prev_above != above_ema:
                ema_crosses += 1
            prev_above = above_ema
        
        # 频繁穿越EMA -> Trading Range
        if ema_crosses >= 4:
            return MarketState.TRADING_RANGE
        
        # 检测强突破（Spike）
        if i >= 2:
            recent_bodies = [
                self.compute_body_size(df.iloc[j])
                for j in range(max(0, i - 10), i + 1)
            ]
            avg_body = sum(recent_bodies) / len(recent_bodies) if recent_bodies else 0
            
            current_body = self.compute_body_size(df.iloc[i])
            prev_body = self.compute_body_size(df.iloc[i - 1]) if i > 0 else 0
            
            if avg_body > 0:
                if current_body > avg_body * 2 and prev_body > avg_body * 2:
                    close = df.iloc[i]["close"]
                    high = df.iloc[i]["high"]
                    low = df.iloc[i]["low"]
                    
                    if (high - low) > 0:
                        if close > ema and (close - low) / (high - low) > 0.9:
                            return MarketState.BREAKOUT
                        elif close < ema and (high - close) / (high - low) > 0.9:
                            return MarketState.BREAKOUT
        
        return MarketState.CHANNEL
    
    def _detect_tight_channel(self, df: pd.DataFrame, i: int, ema: float) -> Optional[MarketState]:
        """
        检测紧凑通道（Tight Channel）
        
        Al Brooks 核心原则：
        在强劲的单边趋势（紧凑通道）中做反转是"自杀行为"
        
        条件 A：最近10根K线中，没有任何一根触碰到EMA
        条件 B：最近5根K线中至少有3根是同向趋势棒
        """
        if i < 10:
            return None
        
        lookback_10 = df.iloc[max(0, i - 9) : i + 1]
        
        all_above_ema = True
        all_below_ema = True
        
        for idx in lookback_10.index:
            bar_high = lookback_10.at[idx, "high"]
            bar_low = lookback_10.at[idx, "low"]
            bar_ema = lookback_10.at[idx, "ema"] if "ema" in lookback_10.columns else ema
            
            if bar_low <= bar_ema * 1.001:
                all_above_ema = False
            if bar_high >= bar_ema * 0.999:
                all_below_ema = False
        
        if not all_above_ema and not all_below_ema:
            return None
        
        lookback_5 = df.iloc[max(0, i - 4) : i + 1]
        
        bullish_bars = 0
        bearish_bars = 0
        
        for idx in lookback_5.index:
            bar_close = lookback_5.at[idx, "close"]
            bar_open = lookback_5.at[idx, "open"]
            
            if bar_close > bar_open:
                bullish_bars += 1
            elif bar_close < bar_open:
                bearish_bars += 1
        
        if all_above_ema and bullish_bars >= 3:
            return MarketState.TIGHT_CHANNEL
        
        if all_below_ema and bearish_bars >= 3:
            return MarketState.TIGHT_CHANNEL
        
        return None
    
    def calculate_tight_channel_score(self, df: pd.DataFrame, i: int, ema: float) -> float:
        """
        计算紧凑通道评分（0-1）
        
        评分因子：
        1. EMA距离因子（0-0.4）
        2. 方向一致性因子（0-0.3）
        3. 连续性因子（0-0.3）
        """
        if i < 10:
            return 0.0
        
        lookback_10 = df.iloc[max(0, i - 9) : i + 1]
        
        # 因子1: EMA距离因子
        total_distance = 0.0
        count = 0
        
        for idx in lookback_10.index:
            bar_ema = lookback_10.at[idx, "ema"] if "ema" in lookback_10.columns else ema
            bar_close = lookback_10.at[idx, "close"]
            distance_pct = abs(bar_close - bar_ema) / bar_ema
            total_distance += distance_pct
            count += 1
        
        avg_distance_pct = total_distance / count if count > 0 else 0
        ema_distance_score = min(avg_distance_pct / 0.01 * 0.4, 0.4)
        
        # 因子2: 方向一致性因子
        lookback_5 = df.iloc[max(0, i - 4) : i + 1]
        bullish_bars = sum(1 for idx in lookback_5.index 
                          if lookback_5.at[idx, "close"] > lookback_5.at[idx, "open"])
        bearish_bars = sum(1 for idx in lookback_5.index 
                          if lookback_5.at[idx, "close"] < lookback_5.at[idx, "open"])
        
        max_same_direction = max(bullish_bars, bearish_bars)
        direction_score = (max_same_direction / 5.0) * 0.3
        
        # 因子3: 连续性因子
        max_consecutive = 0
        current_consecutive = 1
        prev_direction = None
        
        for idx in lookback_10.index:
            bar_close = lookback_10.at[idx, "close"]
            bar_open = lookback_10.at[idx, "open"]
            current_direction = "bull" if bar_close > bar_open else "bear" if bar_close < bar_open else "doji"
            
            if prev_direction == current_direction and current_direction != "doji":
                current_consecutive += 1
                max_consecutive = max(max_consecutive, current_consecutive)
            else:
                current_consecutive = 1
            
            prev_direction = current_direction
        
        consecutive_score = min(max_consecutive / 10.0, 1.0) * 0.3
        
        return round(ema_distance_score + direction_score + consecutive_score, 3)
    
    def get_tight_channel_direction(self, df: pd.DataFrame, i: int) -> Optional[str]:
        """
        获取紧凑通道方向
        
        返回:
            "up": 上升紧凑通道
            "down": 下降紧凑通道
            None: 非紧凑通道
        """
        if i < 10:
            return None
        
        lookback_10 = df.iloc[max(0, i - 9) : i + 1]
        
        try:
            all_above_ema = all(lookback_10["low"] > lookback_10["ema"] * 0.999)
            all_below_ema = all(lookback_10["high"] < lookback_10["ema"] * 1.001)
            
            if all_above_ema:
                return "up"
            elif all_below_ema:
                return "down"
        except:
            pass
        
        return None
