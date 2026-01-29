"""
公共工具函数：数量/价格精度等，供 order_executor、user_manager、tools/backtest 统一调用。
"""

from decimal import Decimal, ROUND_DOWN


def round_quantity_to_step_size(quantity: float, step_size: float = 0.001) -> float:
    """
    将数量按 stepSize 向下取整。

    Args:
        quantity: 原始数量
        step_size: 步长（如 0.001）

    Returns:
        按步长截断后的数量，且不小于 step_size
    """
    if step_size <= 0:
        step_size = 0.001
    qty_decimal = Decimal(str(quantity))
    step_decimal = Decimal(str(step_size))
    rounded = (qty_decimal / step_decimal).quantize(Decimal("1"), rounding=ROUND_DOWN) * step_decimal
    return max(float(rounded), step_size)


def round_tick_size(price: float, tick_size: float) -> float:
    """
    按 tickSize 取整价格（四舍五入到 tickSize 的倍数）。

    Args:
        price: 原始价格
        tick_size: 步长（如 0.01）

    Returns:
        取整后的价格
    """
    if tick_size <= 0:
        return price
    price_decimal = Decimal(str(price))
    tick_decimal = Decimal(str(tick_size))
    rounded = round(price_decimal / tick_decimal) * tick_decimal
    return float(rounded)
