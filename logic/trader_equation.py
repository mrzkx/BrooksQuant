"""
交易者方程公共逻辑：WinRate × Reward > Risk 时才允许交易。

供 strategy、order_executor、tools/backtest 统一调用。
"""

from typing import Optional


def satisfies_trader_equation(
    entry_price: float,
    stop_loss: float,
    tp1: float,
    tp2: float,
    tp1_close_ratio: float,
    side: str,
    win_rate: Optional[float] = None,
    enabled: bool = True,
) -> bool:
    """
    交易者方程：WinRate × Reward > Risk 时才返回 True。

    Args:
        entry_price: 入场价
        stop_loss: 止损价
        tp1, tp2: 止盈价
        tp1_close_ratio: TP1 平仓比例 (0~1)
        side: "buy" 或 "sell"
        win_rate: 胜率 (0~1)，若 None 则从 config 读取
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
        wr = win_rate if win_rate is not None else TRADER_EQUATION_WIN_RATE
    except ImportError:
        if win_rate is None:
            return True
        wr = win_rate
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
