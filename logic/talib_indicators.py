"""
TA-Lib 指标计算模块

统一使用 TA-Lib 计算系统中所有用到的技术指标：
- EMA (指数移动平均)
- ATR (平均真实波幅)
- 自适应 EMA：波动率调节因子 σ，高 ATR 拉长周期平滑噪音，低 ATR 缩短周期保持灵敏度

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


# ========== 波动率调节因子 σ 与自适应 EMA 周期 ==========

def get_adaptive_ema_period(
    atr_series: pd.Series,
    i: int,
    base_period: int = 20,
    atr_lookback: int = 50,
    min_period: int = 10,
    max_period: int = 35,
) -> int:
    """
    由波动率（ATR）计算当前 bar 的自适应 EMA 周期。
    
    高 ATR（高波动）→ 周期拉长以平滑噪音；
    低 ATR（横盘）→ 周期缩短以保持灵敏度。
    
    公式：σ_ratio = ATR[i] / median(ATR[i-lookback:i+1])，限制在 [0.5, 1.5]；
    period_mult = 0.8 + 0.4 * σ_ratio → 周期倍数 [1.0, 1.4]；
    period = clamp(base_period * period_mult, min_period, max_period)。
    
    Args:
        atr_series: ATR 序列
        i: 当前 bar 索引
        base_period: 基准周期（默认 20）
        atr_lookback: ATR 中位数回看根数（默认 50）
        min_period: 最小周期（默认 10）
        max_period: 最大周期（默认 35）
    
    Returns:
        整数 EMA 周期
    """
    start = max(0, i - atr_lookback + 1)
    window = atr_series.iloc[start : i + 1]
    if window.empty or window.isna().all():
        return base_period
    atr_median = window.median()
    if atr_median is None or atr_median <= 0:
        return base_period
    atr_current = atr_series.iloc[i]
    if pd.isna(atr_current) or atr_current <= 0:
        return base_period
    sigma_ratio = float(atr_current / atr_median)
    sigma_ratio = max(0.5, min(1.5, sigma_ratio))
    period_mult = 0.8 + 0.4 * sigma_ratio
    period = int(round(base_period * period_mult))
    return max(min_period, min(max_period, period))


def compute_ema_adaptive(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    base_period: int = 20,
    atr_period: int = 14,
    atr_lookback: int = 50,
    min_period: int = 10,
    max_period: int = 35,
    fillna: bool = True,
) -> pd.Series:
    """
    基于波动率（σ = ATR）的自适应 EMA。
    
    每个 bar i 的 EMA 值 = EMA(close[:i+1], period_i) 的末值，
    其中 period_i 由 get_adaptive_ema_period(ATR, i) 得到。
    
    高波动时 period 自动拉长，横盘时缩短。
    
    Args:
        close, high, low: 价格序列
        base_period: 基准 EMA 周期（默认 20）
        atr_period: ATR 计算周期（默认 14）
        atr_lookback: ATR 中位数回看根数（默认 50）
        min_period, max_period: 周期上下限
        fillna: 是否填充 NaN
    
    Returns:
        pd.Series: 自适应 EMA 序列
    """
    atr = compute_atr(high, low, close, period=atr_period, fillna=fillna)
    n = len(close)
    ema_values = np.full(n, np.nan, dtype=np.float64)
    
    for i in range(n):
        if i < atr_lookback:
            period_i = base_period
        else:
            period_i = get_adaptive_ema_period(
                atr, i, base_period, atr_lookback, min_period, max_period
            )
        window = close.iloc[: i + 1]
        ema_i = compute_ema(window, period=period_i, fillna=False)
        if len(ema_i) > 0 and not pd.isna(ema_i.iloc[-1]):
            ema_values[i] = float(ema_i.iloc[-1])
    
    ema = pd.Series(ema_values, index=close.index, name="ema_adaptive")
    if fillna:
        ema = ema.ffill().bfill()
    return ema


# 日志记录 TA-Lib 状态
if TALIB_AVAILABLE:
    logging.info("✅ TA-Lib 指标计算模块已加载 (C 加速)")
else:
    logging.warning("⚠️ TA-Lib 不可用，使用 Pandas 备用计算")
