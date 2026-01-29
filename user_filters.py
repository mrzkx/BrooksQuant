"""
交易所规则解析（从 Binance exchange_info 提取 stepSize、tickSize 等）

供 TradingUser.get_symbol_filters 使用，将解析逻辑从 user_manager 拆出。
"""

import logging
from typing import Any, Dict


DEFAULT_FILTERS = {
    "stepSize": 0.001,
    "minQty": 0.001,
    "maxQty": 10000,
    "tickSize": 0.01,
    "minNotional": 5.0,
}


def parse_symbol_filters_from_exchange_info(
    exchange_info: Dict[str, Any],
    symbol: str,
    log_prefix: str = "",
) -> Dict[str, float]:
    """
    从 futures_exchange_info() 返回中解析指定交易对的过滤器。

    Args:
        exchange_info: futures_exchange_info() 的返回值
        symbol: 交易对（如 "BTCUSDT"）
        log_prefix: 日志前缀（如 "[User1]"）

    Returns:
        {"stepSize", "minQty", "maxQty", "tickSize", "minNotional"}，未找到则返回默认值
    """
    for sym_info in exchange_info.get("symbols", []):
        if sym_info.get("symbol") != symbol:
            continue
        filters: Dict[str, float] = {}
        for f in sym_info.get("filters", []):
            filter_type = f.get("filterType")
            if filter_type == "LOT_SIZE":
                filters["stepSize"] = float(f.get("stepSize", "0.001"))
                filters["minQty"] = float(f.get("minQty", "0.001"))
                filters["maxQty"] = float(f.get("maxQty", "10000"))
            elif filter_type == "PRICE_FILTER":
                filters["tickSize"] = float(f.get("tickSize", "0.01"))
            elif filter_type == "MIN_NOTIONAL":
                filters["minNotional"] = float(f.get("notional", "5"))
        for k, v in DEFAULT_FILTERS.items():
            filters.setdefault(k, v)
        if log_prefix:
            logging.info(
                f"{log_prefix} 获取 {symbol} 过滤器: "
                f"stepSize={filters['stepSize']}, minQty={filters['minQty']}, tickSize={filters['tickSize']}"
            )
        return filters

    if log_prefix:
        logging.warning(f"{log_prefix} 未找到 {symbol} 的过滤器，使用默认值")
    return dict(DEFAULT_FILTERS)
