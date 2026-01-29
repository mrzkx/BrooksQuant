"""
策略信号数据模型

BarContext / SignalArrays / SignalResult 用于 generate_signals 流程中的上下文与结果传递。
"""

from typing import List, Optional
from dataclasses import dataclass

from .market_analyzer import MarketState, MarketCycle


@dataclass
class BarContext:
    """
    单根 K 线的市场上下文信息

    Al Brooks: "交易前必须先确定市场上下文（趋势/区间）"
    """
    i: int
    close: float
    high: float
    low: float
    ema: float
    atr: Optional[float]

    market_state: MarketState
    market_cycle: MarketCycle
    trend_direction: Optional[str]
    trend_strength: float
    tight_channel_score: float
    tight_channel_direction: Optional[str]

    is_strong_trend_mode: bool
    allowed_side: Optional[str]
    is_latest_bar: bool

    volume: Optional[float] = None
    avg_volume: Optional[float] = None


@dataclass
class SignalArrays:
    """
    信号结果数组集合，存储 generate_signals 生成的所有信号字段。
    """
    signals: List[Optional[str]]
    sides: List[Optional[str]]
    stops: List[Optional[float]]
    market_states: List[Optional[str]]
    risk_reward_ratios: List[Optional[float]]
    base_heights: List[Optional[float]]
    tp1_prices: List[Optional[float]]
    tp2_prices: List[Optional[float]]
    tight_channel_scores: List[Optional[float]]
    delta_modifiers: List[Optional[float]]
    tp1_close_ratios: List[Optional[float]]
    is_climax_bars: List[Optional[bool]]
    talib_boosts: List[Optional[float]]
    talib_patterns: List[Optional[str]]
    entry_modes: List[Optional[str]]
    is_high_risk: List[Optional[bool]]
    move_stop_to_breakeven_at_tp1: List[Optional[bool]]

    @classmethod
    def create(cls, length: int) -> "SignalArrays":
        """创建指定长度的空数组集合"""
        return cls(
            signals=[None] * length,
            sides=[None] * length,
            stops=[None] * length,
            market_states=[None] * length,
            risk_reward_ratios=[None] * length,
            base_heights=[None] * length,
            tp1_prices=[None] * length,
            tp2_prices=[None] * length,
            tight_channel_scores=[None] * length,
            delta_modifiers=[None] * length,
            tp1_close_ratios=[None] * length,
            is_climax_bars=[None] * length,
            talib_boosts=[None] * length,
            talib_patterns=[None] * length,
            entry_modes=[None] * length,
            is_high_risk=[None] * length,
            move_stop_to_breakeven_at_tp1=[None] * length,
        )


@dataclass
class SignalResult:
    """
    单个信号的检测结果，在各检测方法之间传递。
    """
    signal_type: str
    side: str
    stop_loss: float
    base_height: float
    limit_price: Optional[float] = None
    risk_reward: float = 2.0
    delta_modifier: float = 1.0
    tp1_close_ratio: float = 0.5
    is_climax: bool = False
    strength: float = 1.0
    htf_modifier: float = 1.0
    entry_mode: Optional[str] = None
    is_high_risk: bool = False
    wedge_tp1_price: Optional[float] = None
    wedge_tp2_price: Optional[float] = None
    wedge_strong_reversal_bar: bool = False
    move_stop_to_breakeven_at_tp1: bool = False
