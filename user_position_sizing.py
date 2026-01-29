"""
仓位与限价计算（纯逻辑，无 Binance 依赖）

供 TradingUser 调用，将大块计算从 user_manager 中拆出。
"""

import logging
from typing import Callable, Optional


def get_position_size_percent(
    balance: float,
    small_threshold: float,
    small_pct: float,
    large_pct: float,
) -> float:
    """
    根据账户余额计算仓位百分比。

    规则：余额 <= small_threshold 用 small_pct，否则用 large_pct。

    Args:
        balance: 账户 USDT 余额
        small_threshold: 小资金阈值（USDT）
        small_pct: 小资金仓位比例（0-100）
        large_pct: 大资金仓位比例（0-100）

    Returns:
        仓位百分比（0-100）
    """
    if balance <= small_threshold:
        return small_pct
    return large_pct


def compute_order_quantity(
    balance: float,
    current_price: float,
    leverage: int,
    position_pct: float,
    step_size: float,
    min_qty: float,
    min_notional: float,
    round_step_fn: Callable[[float, float], float],
    log_prefix: str = "",
) -> float:
    """
    计算符合交易所规则的下单数量。

    公式: 下单数量 = (余额 × 仓位百分比 × 杠杆) / 当前价格，
    再按 stepSize 取整并做保证金与最小名义价值检查。

    Args:
        balance: 账户 USDT 余额
        current_price: 当前价格
        leverage: 杠杆倍数
        position_pct: 仓位百分比（0-100）
        step_size: 数量步长
        min_qty: 最小数量
        min_notional: 最小名义价值
        round_step_fn: (quantity, step_size) -> 取整后数量
        log_prefix: 日志前缀（如 "[User1]"）

    Returns:
        符合 stepSize 的数量；若余额不足则返回 0.0
    """
    if current_price <= 0 or balance <= 0:
        logging.warning(f"{log_prefix} 无效参数: 余额={balance}, 价格={current_price}")
        return min_qty

    position_value = balance * (position_pct / 100)
    buying_power = position_value * leverage
    quantity = buying_power / current_price
    quantity = round_step_fn(quantity, step_size)
    quantity = max(quantity, min_qty)

    notional_value = quantity * current_price
    if notional_value < min_notional:
        quantity = min_notional / current_price
        quantity = round_step_fn(quantity, step_size)
        if quantity * current_price < min_notional:
            quantity += step_size
        logging.warning(
            f"{log_prefix} 调整数量以满足最小名义价值: "
            f"{notional_value:.2f} < {min_notional}, 新数量={quantity:.6f}"
        )

    notional_value = quantity * current_price
    required_margin = notional_value / leverage

    if required_margin > balance:
        max_quantity = balance * leverage / current_price
        max_quantity = round_step_fn(max_quantity, step_size)
        if max_quantity >= min_qty:
            quantity = max_quantity
            notional_value = quantity * current_price
            required_margin = notional_value / leverage
            logging.warning(
                f"{log_prefix} 保证金不足，调整数量: "
                f"需要保证金={required_margin:.2f} > 余额={balance:.2f}, 调整为 {quantity:.6f}"
            )
        else:
            logging.error(
                f"{log_prefix} 余额不足，无法满足最小交易量: "
                f"余额={balance:.2f}, 最小数量需要保证金={min_qty * current_price / leverage:.2f}"
            )
            return 0.0

    logging.info(
        f"{log_prefix} 仓位计算: 余额={balance:.2f} USDT, 仓位比例={position_pct:.0f}%, "
        f"杠杆={leverage}x, 下单数量={quantity:.6f} (≈{notional_value:.2f} USDT), "
        f"保证金={required_margin:.2f}, stepSize={step_size}"
    )
    return quantity


def compute_limit_price(
    current_price: float,
    side: str,
    slippage_pct: float,
    tick_size: float,
    round_tick_fn: Callable[[float, float], float],
    atr: Optional[float] = None,
) -> float:
    """
    计算带滑点的限价（符合 tickSize）。

    Args:
        current_price: 当前价格
        side: "buy" 或 "sell"
        slippage_pct: 滑点百分比（如 0.05 表示 0.05%）
        tick_size: 价格步长
        round_tick_fn: (price, tick_size) -> 取整后价格
        atr: 可选 ATR，用于动态滑点（滑点至少 ATR 的 10%，不超过 0.3%）

    Returns:
        符合 tickSize 的限价
    """
    if atr is not None and atr > 0:
        atr_pct = (atr / current_price) * 100
        dynamic_slippage = max(slippage_pct, min(atr_pct * 0.1, 0.3))
        slippage_pct = dynamic_slippage

    slippage = current_price * (slippage_pct / 100)
    if side.lower() == "buy":
        limit_price = current_price + slippage
    else:
        limit_price = current_price - slippage
    return round_tick_fn(limit_price, tick_size)
