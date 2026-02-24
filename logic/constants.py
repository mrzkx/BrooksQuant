"""
EA 枚举、输入参数、数据类 — 严格对齐 BrooksQuant_EA.mq5
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Dict, Optional


# ── 枚举 ──────────────────────────────────────────────────────────────

class MarketState(Enum):
    STRONG_TREND = "StrongTrend"
    BREAKOUT = "Breakout"
    CHANNEL = "Channel"
    TRADING_RANGE = "TradingRange"
    TIGHT_CHANNEL = "TightChannel"
    FINAL_FLAG = "FinalFlag"


class MarketCycle(Enum):
    SPIKE = "Spike"
    CHANNEL = "Channel"
    TRADING_RANGE = "TradingRange"


class AlwaysIn(Enum):
    LONG = 1
    SHORT = -1
    NEUTRAL = 0


class SignalType(IntEnum):
    NONE = 0
    SPIKE_BUY = 1
    SPIKE_SELL = 2
    H1_BUY = 3
    H2_BUY = 4
    L1_SELL = 5
    L2_SELL = 6
    MICRO_CH_BUY = 7
    MICRO_CH_SELL = 8
    DT_BUY = 9
    DT_SELL = 10
    TREND_BAR_BUY = 11
    TREND_BAR_SELL = 12
    REV_BAR_BUY = 13
    REV_BAR_SELL = 14
    II_BUY = 15
    II_SELL = 16
    OUTSIDE_BAR_BUY = 17
    OUTSIDE_BAR_SELL = 18
    MEASURED_MOVE_BUY = 19
    MEASURED_MOVE_SELL = 20
    TR_BREAKOUT_BUY = 21
    TR_BREAKOUT_SELL = 22
    BO_PULLBACK_BUY = 23
    BO_PULLBACK_SELL = 24
    GAP_BAR_BUY = 25
    GAP_BAR_SELL = 26
    WEDGE_BUY = 27
    WEDGE_SELL = 28
    CLIMAX_BUY = 29
    CLIMAX_SELL = 30
    MTR_BUY = 31
    MTR_SELL = 32
    FAILED_BO_BUY = 33
    FAILED_BO_SELL = 34
    FINAL_FLAG_BUY = 35
    FINAL_FLAG_SELL = 36


DIR_LONG: int = 1
DIR_SHORT: int = -1


def signal_side(sig: SignalType) -> str:
    if sig == SignalType.NONE:
        return ""
    name = sig.name
    if name.endswith("_BUY"):
        return "buy"
    if name.endswith("_SELL"):
        return "sell"
    return ""


def is_spike_signal(sig: SignalType) -> bool:
    return sig in (SignalType.SPIKE_BUY, SignalType.SPIKE_SELL)


# ── 数据类 ────────────────────────────────────────────────────────────

@dataclass
class SignalResult:
    signal_type: SignalType = SignalType.NONE
    direction: int = 0            # DIR_LONG / DIR_SHORT
    entry_price: float = 0.0      # 限价单：信号棒极值；市价单：收盘价
    stop_loss: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    reason: str = ""


@dataclass
class SwingPoint:
    price: float
    bar_index: int
    is_high: bool


@dataclass
class MeasuringGapInfo:
    gap_high: float = 0.0
    gap_low: float = 0.0
    direction: str = ""           # "up" / "down"
    bar_index: int = 0
    is_valid: bool = False


@dataclass
class SoftStopInfo:
    ticket: str = ""              # Binance order_id
    technical_sl: float = 0.0
    side: str = ""
    tp1_price: float = 0.0       # Runner: TP1 触及后移保本


# ── EA 输入参数（精确对齐 EA const / input 值）──────────────────────────

# 基础
EMA_PERIOD: int = 20
ATR_PERIOD: int = 20

# 信号棒验证
MIN_BODY_RATIO: float = 0.50
CLOSE_POSITION_PCT: float = 0.25
LOOKBACK_PERIOD: int = 20

# 市场状态
STRONG_TREND_SCORE: float = 0.50

# 信号冷却
SIGNAL_COOLDOWN: int = 3

# Spike
MIN_SPIKE_BARS: int = 3
SPIKE_OVERLAP_MAX: float = 0.30

# Climax
SPIKE_CLIMAX_ATR_MULT: float = 3.0
REQUIRE_SECOND_ENTRY: bool = True
SECOND_ENTRY_LOOKBACK: int = 10

# 风险管理
MAX_STOP_ATR_MULT: float = 3.0
TP1_CLOSE_PERCENT: float = 50.0

# 20 Gap Bar 法则
ENABLE_20_GAP_RULE: bool = True
GAP_BAR_THRESHOLD: int = 20
BLOCK_FIRST_PULLBACK: bool = True
CONSOLIDATION_BARS: int = 5
CONSOLIDATION_RANGE: float = 1.5

# HTF 过滤
ENABLE_HTF_FILTER: bool = True
HTF_EMA_PERIOD: int = 20

# 混合止损
ENABLE_HARD_STOP: bool = True
HARD_STOP_BUFFER_MULT: float = 1.5
ENABLE_SOFT_STOP: bool = True

# 点差过滤
ENABLE_SPREAD_FILTER: bool = True
MAX_SPREAD_MULT: float = 2.0
SPREAD_LOOKBACK: int = 20

# Stop Order 入场
USE_STOP_ORDERS: bool = True
STOP_ORDER_OFFSET: float = 0.0

# Barb Wire
ENABLE_BARB_WIRE_FILTER: bool = True
BARB_WIRE_MIN_BARS: int = 3
BARB_WIRE_BODY_RATIO: float = 0.35
BARB_WIRE_RANGE_RATIO: float = 0.5

# Measuring Gap
ENABLE_MEASURING_GAP: bool = True
MEASURING_GAP_MIN_SIZE: float = 0.3

# Breakout Mode
ENABLE_BREAKOUT_MODE: bool = True
BREAKOUT_MODE_BARS: int = 5
BREAKOUT_MODE_ATR_MULT: float = 1.5

# ATR 动态阈值
NEAR_TRENDLINE_ATR_MULT: float = 0.2
MIN_BUFFER_ATR_MULT: float = 0.2

# TTR
TTR_OVERLAP_THRESHOLD: float = 0.40
TTR_RANGE_ATR_MULT: float = 2.5

# Swing / H-L Count
SWING_CONFIRM_DEPTH: int = 3
HL_RESET_NEW_EXTREME_ATR: float = 0.5
HL_MIN_PULLBACK_ATR: float = 0.2

# 保本
BREAKEVEN_ATR_MULT: float = 0.1
BREAKEVEN_POINTS: int = 5

# 软止损确认
SOFT_STOP_CONFIRM_MODE: int = 0
SOFT_STOP_CONFIRM_BARS: int = 2

# 周末过滤（加密市场 24/7，默认关闭）
ENABLE_WEEKEND_FILTER: bool = False

# 信号开关（全部默认开启）
ENABLE_SPIKE: bool = True
ENABLE_H2L2: bool = True
ENABLE_WEDGE: bool = True
ENABLE_CLIMAX: bool = True
ENABLE_MTR: bool = True
ENABLE_FAILED_BO: bool = True
ENABLE_DTDB: bool = True
ENABLE_TREND_BAR: bool = True
ENABLE_REV_BAR: bool = True
ENABLE_II_PATTERN: bool = True
ENABLE_OUTSIDE_BAR: bool = True
ENABLE_MEASURED_MOVE: bool = True
ENABLE_TR_BREAKOUT: bool = True
ENABLE_BO_PULLBACK: bool = True
ENABLE_GAP_BAR: bool = True

# 状态惯性最小保持 K 线数
STATE_MIN_HOLD: Dict[MarketState, int] = {
    MarketState.STRONG_TREND: 3,
    MarketState.TIGHT_CHANNEL: 3,
    MarketState.TRADING_RANGE: 2,
    MarketState.BREAKOUT: 2,
    MarketState.CHANNEL: 1,
    MarketState.FINAL_FLAG: 1,
}

# 信号 → 是否允许反转方向
REVERSAL_ALLOWED_STATES = frozenset({
    MarketState.TRADING_RANGE,
    MarketState.FINAL_FLAG,
})
