"""
TA-Lib 指标计算模块

统一使用 TA-Lib 计算系统中所有用到的技术指标：
- EMA (指数移动平均)
- ATR (平均真实波幅)

优势：
- C 语言实现，计算速度快
- 经过广泛验证，结果准确

注意：
- TA-Lib 计算初期会产生 NaN（周期不足）
- 本模块自动处理 NaN，确保不影响下游逻辑
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd

try:
    import talib
    TALIB_AVAILABLE = True
except ImportError:
    TALIB_AVAILABLE = False
    logging.warning("⚠️ TA-Lib 未安装，将使用 Pandas 备用计算")


def compute_ema(
    close: pd.Series, 
    period: int = 20,
    fillna: bool = True
) -> pd.Series:
    """
    计算指数移动平均线 (EMA)
    
    Args:
        close: 收盘价序列
        period: 周期（默认 20）
        fillna: 是否填充 NaN（默认 True，使用前向填充）
    
    Returns:
        pd.Series: EMA 序列（NaN 已处理）
    """
    if TALIB_AVAILABLE:
        result = talib.EMA(close.values.astype(np.float64), timeperiod=period)
        ema = pd.Series(result, index=close.index, name=f"ema_{period}")
    else:
        ema = close.ewm(span=period, adjust=False).mean()
    
    # 处理 NaN：使用前向填充，剩余用首个有效值回填
    if fillna:
        ema = ema.ffill().bfill()
    
    return ema


def compute_atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
    fillna: bool = True
) -> pd.Series:
    """
    计算平均真实波幅 (ATR)
    
    Args:
        high: 最高价序列
        low: 最低价序列
        close: 收盘价序列
        period: 周期（默认 14）
        fillna: 是否填充 NaN（默认 True）
    
    Returns:
        pd.Series: ATR 序列（NaN 已处理）
    """
    if TALIB_AVAILABLE:
        result = talib.ATR(
            high.values.astype(np.float64),
            low.values.astype(np.float64),
            close.values.astype(np.float64),
            timeperiod=period
        )
        atr = pd.Series(result, index=close.index, name=f"atr_{period}")
    else:
        # Pandas 备用计算
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.ewm(span=period, adjust=False).mean()
    
    # 处理 NaN：ATR 初期 NaN 用当前 K 线波幅填充
    if fillna:
        # 首先用 high-low 作为初始 ATR 估计
        initial_tr = high - low
        atr = atr.fillna(initial_tr)
        # 前向填充剩余 NaN
        atr = atr.ffill().bfill()
    
    return atr


# 日志记录 TA-Lib 状态
if TALIB_AVAILABLE:
    logging.info("✅ TA-Lib 指标计算模块已加载 (C 加速)")
else:
    logging.warning("⚠️ TA-Lib 不可用，使用 Pandas 备用计算")
