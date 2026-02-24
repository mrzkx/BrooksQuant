"""
技术指标计算 — EMA / ATR

EA 仅使用 EMA(20) 和 ATR(20)，这里保持最简实现。
优先使用 TA-Lib (C 加速)，回退到 Pandas 纯 Python 计算。
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

try:
    import talib
    _TALIB = True
except ImportError:
    _TALIB = False
    logging.warning("TA-Lib 未安装，使用 Pandas 备用计算")


def compute_ema(close: pd.Series, period: int = 20) -> pd.Series:
    if _TALIB:
        arr = talib.EMA(close.values.astype(np.float64), timeperiod=period)
        ema = pd.Series(arr, index=close.index)
    else:
        ema = close.ewm(span=period, adjust=False).mean()
    return ema.ffill().bfill()


def compute_atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 20,
) -> pd.Series:
    if _TALIB:
        arr = talib.ATR(
            high.values.astype(np.float64),
            low.values.astype(np.float64),
            close.values.astype(np.float64),
            timeperiod=period,
        )
        atr = pd.Series(arr, index=close.index)
    else:
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.ewm(span=period, adjust=False).mean()
    initial_tr = high - low
    atr = atr.fillna(initial_tr).ffill().bfill()
    return atr


def compute_htf_ema(close_htf: pd.Series, period: int = 20) -> pd.Series:
    """HTF (H1) EMA，用于 HTF 过滤器。"""
    return compute_ema(close_htf, period)
