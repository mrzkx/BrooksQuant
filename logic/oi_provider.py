"""
持仓量（Open Interest）数据提供层（预留）

Al Brooks：成交量与持仓量反映博弈的“力量”，突破时 OI 激增可确认有效突破。
当前为占位实现，需数据源支持（如 Binance 期货 OI API 或第三方数据）。
"""

import logging
from typing import Optional


def get_open_interest(symbol: str = "BTCUSDT", interval: str = "5m") -> Optional[float]:
    """
    获取当前或指定周期的持仓量（Open Interest）。
    
    需数据源支持：Binance 期货 OI 需通过 fapi 接口或第三方数据获取，
    此处仅预留接口，返回 None 表示未启用或数据不可用。
    
    Args:
        symbol: 交易对（如 BTCUSDT）
        interval: K 线周期（与策略周期对齐）
    
    Returns:
        持仓量数值，或 None（未实现/未启用）
    """
    # 占位：未接入 OI 数据源
    return None


def oi_confirms_breakout(
    symbol: str = "BTCUSDT",
    current_oi: Optional[float] = None,
    avg_oi: Optional[float] = None,
    multiplier: float = 1.1,
) -> bool:
    """
    判断持仓量是否确认突破（当前 OI > 近期均量×系数）。
    
    需数据源支持；当 current_oi/avg_oi 为 None 时返回 True（不拦截）。
    
    Args:
        symbol: 交易对
        current_oi: 当前 OI
        avg_oi: 近期平均 OI
        multiplier: 倍数阈值
    
    Returns:
        True 表示通过或未启用，False 表示 OI 未放量
    """
    if current_oi is None or avg_oi is None or avg_oi <= 0:
        return True
    return current_oi >= avg_oi * multiplier
