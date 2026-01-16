"""
Al Brooks 价格行为策略模块

模块结构：
- market_analyzer: 市场状态识别（MarketState, TightChannel）
- patterns: 模式检测（Spike, FailedBreakout, Wedge, Climax）
- state_machines: H2/L2 状态机管理
"""

from .market_analyzer import MarketState, MarketAnalyzer
from .patterns import PatternDetector
from .state_machines import HState, LState, H2StateMachine, L2StateMachine

__all__ = [
    "MarketState",
    "MarketAnalyzer", 
    "PatternDetector",
    "HState",
    "LState",
    "H2StateMachine",
    "L2StateMachine",
]
