"""
交易者方程公共逻辑：WinRate × Reward > Risk 时才允许交易（Al Brooks 修正版）

Al Brooks 修正：根据信号类型设置不同的胜率
- MTR：低胜率（30-40%），高盈亏比
- H2/L2：高胜率（50-60%），中盈亏比
- Spike：中等胜率（45-55%），看跟随情况
- Failed Breakout in TR：高胜率（55-65%）

供 strategy、order_executor、tools/backtest 统一调用。
"""

from typing import Optional, Dict


# Al Brooks 修正：根据信号类型的不同胜率
# 不同信号类型有不同的历史胜率，应该在交易者方程中体现
SIGNAL_WIN_RATES: Dict[str, float] = {
    # Spike 信号：中等胜率
    "Spike_Buy": 0.50,
    "Spike_Sell": 0.50,
    
    # H2/L2 信号：高胜率（顺势交易）
    "H2_Buy": 0.55,
    "L2_Sell": 0.55,
    "H1_Buy": 0.48,  # H1 比 H2 低
    "L1_Sell": 0.48,  # L1 比 L2 低
    
    # Failed Breakout 信号：高胜率（交易区间内）
    "FailedBreakout_Buy": 0.55,
    "FailedBreakout_Sell": 0.55,
    "Wedge_FailedBreakout_Buy": 0.55,
    "Wedge_FailedBreakout_Sell": 0.55,
    
    # Wedge 信号：中低胜率（反转信号）
    "Wedge_Buy": 0.45,
    "Wedge_Sell": 0.45,
    
    # MTR 信号：低胜率（主要趋势反转）
    "MTR_Buy": 0.38,
    "MTR_Sell": 0.38,
    
    # Climax 信号：中低胜率
    "Climax_Buy": 0.42,
    "Climax_Sell": 0.42,
    
    # Final Flag 信号：中等胜率
    "FinalFlag_Buy": 0.48,
    "FinalFlag_Sell": 0.48,
    "Final_Flag_Reversal_Buy": 0.48,
    "Final_Flag_Reversal_Sell": 0.48,
}

# 默认胜率（用于未知信号类型）
DEFAULT_WIN_RATE = 0.40


def get_signal_win_rate(signal_type: Optional[str]) -> float:
    """
    获取信号类型对应的历史胜率
    
    Args:
        signal_type: 信号类型（如 "Spike_Buy", "H2_Buy" 等）
    
    Returns:
        胜率（0-1）
    """
    if signal_type is None:
        return DEFAULT_WIN_RATE
    
    # 直接匹配
    if signal_type in SIGNAL_WIN_RATES:
        return SIGNAL_WIN_RATES[signal_type]
    
    # 模糊匹配（前缀匹配）
    for key, rate in SIGNAL_WIN_RATES.items():
        if signal_type.startswith(key.split("_")[0]):
            return rate
    
    return DEFAULT_WIN_RATE


def satisfies_trader_equation(
    entry_price: float,
    stop_loss: float,
    tp1: float,
    tp2: float,
    tp1_close_ratio: float,
    side: str,
    win_rate: Optional[float] = None,
    signal_type: Optional[str] = None,
    enabled: bool = True,
) -> bool:
    """
    交易者方程：WinRate × Reward > Risk 时才返回 True（Al Brooks 修正版）

    Al Brooks 修正：根据信号类型自动选择对应的历史胜率

    Args:
        entry_price: 入场价
        stop_loss: 止损价
        tp1, tp2: 止盈价
        tp1_close_ratio: TP1 平仓比例 (0~1)
        side: "buy" 或 "sell"
        win_rate: 胜率 (0~1)，若 None 则根据 signal_type 自动选择
        signal_type: 信号类型（用于自动选择胜率）
        enabled: 是否启用方程，False 时直接返回 True

    Returns:
        True 表示通过或未启用，False 表示不满足
    """
    if not enabled:
        return True
    
    try:
        from config import TRADER_EQUATION_ENABLED, TRADER_EQUATION_WIN_RATE
        if not TRADER_EQUATION_ENABLED:
            return True
        
        # Al Brooks 修正：优先使用信号类型对应的胜率
        if win_rate is not None:
            wr = win_rate
        elif signal_type is not None:
            wr = get_signal_win_rate(signal_type)
        else:
            wr = TRADER_EQUATION_WIN_RATE
    except ImportError:
        if win_rate is not None:
            wr = win_rate
        elif signal_type is not None:
            wr = get_signal_win_rate(signal_type)
        else:
            return True  # 无法获取胜率时默认通过
    
    risk = abs(entry_price - stop_loss)
    if risk <= 0:
        return True
    
    if side == "buy":
        reward = tp1_close_ratio * (tp1 - entry_price) + (1.0 - tp1_close_ratio) * (tp2 - entry_price)
    else:
        reward = tp1_close_ratio * (entry_price - tp1) + (1.0 - tp1_close_ratio) * (entry_price - tp2)
    
    if reward <= 0:
        return False
    
    return (wr * reward) > risk
