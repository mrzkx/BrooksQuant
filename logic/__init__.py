"""
Al Brooks 价格行为策略模块（基于 MT5 EA 精确实现）

模块结构：
- constants: 枚举（MarketState, MarketCycle, AlwaysIn, SignalType）、Input 参数、数据类
- indicators: EMA / ATR 计算
- swing_tracker: Swing Point 追踪（depth=3 确认 + depth=1 临时）
- hl_counter: H1/H2/L1/L2 计数器
- market_state: 市场状态检测 + Always In 方向
- filters: 信号过滤器（BarbWire, Gap20, HTF, Spread, Cooldown, MeasuringGap, BreakoutMode）
- signals: 17 种信号检测函数
- scan_market: ScanMarket 扫描入口
- stop_loss: Brooks 止损 + 软止损
- take_profit: Scalp TP1 + Measured Move TP2
"""

from .constants import (
    MarketState,
    MarketCycle,
    AlwaysIn,
    SignalType,
    SignalResult,
    DIR_LONG,
    DIR_SHORT,
)

__all__ = [
    "MarketState",
    "MarketCycle",
    "AlwaysIn",
    "SignalType",
    "SignalResult",
    "DIR_LONG",
    "DIR_SHORT",
]
