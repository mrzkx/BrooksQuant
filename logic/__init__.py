"""
Al Brooks 价格行为策略模块

模块结构：
- market_analyzer: 市场状态识别（MarketState, TightChannel, AlwaysInDirection）
- patterns: 模式检测（Spike, FailedBreakout, Wedge, Climax）
- state_machines: H2/L2 状态机管理
"""

from .market_analyzer import MarketState, MarketCycle, MarketAnalyzer, AlwaysInDirection
from .patterns import PatternDetector
from .state_machines import HState, LState, H2StateMachine, L2StateMachine

__all__ = [
    "MarketState",
    "MarketCycle",
    "AlwaysInDirection",
    "MarketAnalyzer", 
    "PatternDetector",
    "HState",
    "LState",
    "H2StateMachine",
    "L2StateMachine",
]
