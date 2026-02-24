"""
订单执行模块 — 混合入场（Spike 市价 / 其他限价）

实盘逻辑:
- Spike 信号: 市价入场（已有连续确认 K 线）
- 其他信号: 限价单挂在信号棒极值（buy=bar[1].high, sell=bar[1].low）
- 限价单超时 60s 撤销
- TP1 触发后挂 TP2 限价止盈单（软止损由本地收盘检查）
"""
import asyncio
import logging
from typing import Dict

from config import SYMBOL as CONFIG_SYMBOL
from trade_logger import TradeLogger
from user_manager import TradingUser
from utils import round_quantity_to_step_size

SYMBOL = CONFIG_SYMBOL


def _extract_signal_params(signal: Dict) -> Dict:
    return {
        "tp1_price": signal.get("tp1_price"),
        "tp2_price": signal.get("tp2_price"),
        "market_state": signal.get("market_state", ""),
        "signal_strength": signal.get("signal_strength", 1.0),
        "tp1_close_ratio": signal.get("tp1_close_ratio", 0.5),
        "is_spike": signal.get("is_spike", False),
    }


async def execute_observe_order(
    user: TradingUser,
    signal: Dict,
    order_qty: float,
    position_value: float,
    trade_logger: TradeLogger,
    calculate_order_quantity_func,
) -> None:
    params = _extract_signal_params(signal)
    trade_logger.open_position(
        user=user.name,
        signal=signal["signal"],
        side=signal["side"],
        entry_price=signal["price"],
        quantity=order_qty,
        stop_loss=signal["stop_loss"],
        take_profit=signal["take_profit"],
        signal_strength=params["signal_strength"],
        tp1_price=params["tp1_price"],
        tp2_price=params["tp2_price"],
        market_state=params["market_state"],
        tight_channel_score=0.0,
        is_observe=True,
        tp1_close_ratio=params["tp1_close_ratio"],
        is_climax_bar=False,
    )
    entry_type = "市价" if params["is_spike"] else "限价"
    logging.info(
        f"[{user.name}] 观察模式: {signal['signal']} {signal['side']} @ {signal['price']:.2f} ({entry_type}), "
        f"数量={order_qty:.4f}, SL={signal['stop_loss']:.2f}, "
        f"TP1={params['tp1_price']:.2f}, TP2={params['tp2_price']:.2f}"
    )


async def execute_live_order(
    user: TradingUser,
    signal: Dict,
    order_qty: float,
    position_value: float,
    trade_logger: TradeLogger,
    signal_queue: asyncio.Queue,
) -> bool:
    params = _extract_signal_params(signal)
    is_spike = params["is_spike"]

    try:
        if is_spike:
            actual_price, actual_qty = await _execute_market_entry(
                user, signal, order_qty
            )
        else:
            actual_price, actual_qty = await _execute_limit_entry(
                user, signal, order_qty
            )
            if actual_price is None:
                return False

        trade = trade_logger.open_position(
            user=user.name,
            signal=signal["signal"],
            side=signal["side"],
            entry_price=actual_price,
            quantity=actual_qty,
            stop_loss=signal["stop_loss"],
            take_profit=signal["take_profit"],
            signal_strength=params["signal_strength"],
            tp1_price=params["tp1_price"],
            tp2_price=params["tp2_price"],
            market_state=params["market_state"],
            tight_channel_score=0.0,
            is_observe=False,
            tp1_close_ratio=params["tp1_close_ratio"],
            is_climax_bar=False,
            hard_stop_loss=None,
        )

        await asyncio.sleep(1)
        try:
            pos = await user.get_position_info(SYMBOL)
            if pos:
                binance_qty = abs(float(pos["positionAmt"]))
                binance_entry = float(pos["entryPrice"])
                trade_logger.update_position_from_binance(user.name, binance_qty, binance_entry)
                actual_qty = binance_qty
                actual_price = binance_entry
        except Exception as sync_err:
            logging.warning(f"[{user.name}] 开仓后同步币安持仓失败: {sync_err}")

        if params.get("tp1_price") and actual_qty > 0:
            tp1_close_ratio = params.get("tp1_close_ratio", 0.5)
            tp1_qty = round_quantity_to_step_size(actual_qty * tp1_close_ratio)
            stop_side = "SELL" if signal["side"].lower() == "buy" else "BUY"
            try:
                tp1_response = await user.create_take_profit_market_order(
                    symbol=SYMBOL,
                    side=stop_side,
                    quantity=tp1_qty,
                    stop_price=round(float(params["tp1_price"]), 2),
                    reduce_only=True,
                )
                tp1_order_id = tp1_response.get("orderId")
                trade_logger.mark_tp1_order_placed(user.name, order_id=tp1_order_id)
                logging.info(f"[{user.name}] TP1 止盈单已挂: ID={tp1_order_id}")
            except Exception as tp1_err:
                logging.error(f"[{user.name}] TP1 挂单失败: {tp1_err}")

        entry_type = "市价" if is_spike else "限价"
        logging.info(
            f"[{user.name}] 实盘{entry_type}开仓: {signal['signal']} {signal['side']} "
            f"@ {actual_price:.2f}, 数量={actual_qty:.4f}"
        )
        return True

    except Exception as exc:
        logging.exception(f"[{user.name}] 实盘下单失败: {exc}")
        return False


async def _execute_market_entry(
    user: TradingUser, signal: Dict, order_qty: float,
) -> tuple:
    """Spike 信号: 市价入场"""
    response = await user.create_market_order(
        symbol=SYMBOL,
        side=signal["side"].upper(),
        quantity=order_qty,
    )
    avg_price = float(response.get("avgPrice", 0))
    if avg_price <= 0:
        avg_price = signal["price"]
    actual_qty = float(response.get("origQty", order_qty))
    return avg_price, actual_qty


async def _execute_limit_entry(
    user: TradingUser, signal: Dict, order_qty: float,
) -> tuple:
    """非 Spike 信号: 限价单挂在信号棒极值"""
    limit_price = signal["price"]
    logging.info(
        f"[{user.name}] 限价开仓: {signal['side'].upper()} @ {limit_price:.2f}, "
        f"数量={order_qty:.4f}"
    )
    entry_response = await user.create_limit_order(
        symbol=SYMBOL,
        side=signal["side"].upper(),
        quantity=order_qty,
        price=limit_price,
        time_in_force="GTC",
    )
    order_id = entry_response.get("orderId")
    order_status = entry_response.get("status", "NEW")
    if order_status == "NEW":
        try:
            entry_response = await user.wait_for_order_fill(
                symbol=SYMBOL,
                order_id=order_id,
                timeout_seconds=60.0,
                poll_interval=2.0,
            )
            order_status = entry_response.get("status", "FILLED")
        except TimeoutError:
            logging.warning(f"[{user.name}] 限价单超时未成交，撤销")
            try:
                await user.cancel_order(SYMBOL, order_id)
            except Exception:
                pass
            return None, 0.0
        except Exception as wait_err:
            logging.error(f"[{user.name}] 等待限价单成交出错: {wait_err}")
            return None, 0.0

    avg_price = float(entry_response.get("avgPrice", 0))
    if avg_price <= 0:
        price_str = entry_response.get("price", "0")
        avg_price = float(price_str) if float(price_str) > 0 else limit_price
    actual_qty = float(entry_response.get("origQty", order_qty))
    return avg_price, actual_qty


async def _cancel_related_orders(
    user: TradingUser,
    trade_logger: TradeLogger,
    reason: str = "平仓前撤单",
) -> None:
    order_ids = trade_logger.get_pending_order_ids(user.name)
    cancelled = []
    for order_type, order_id in order_ids.items():
        if order_id:
            try:
                await user.cancel_order(SYMBOL, order_id)
                cancelled.append(f"{order_type}={order_id}")
            except Exception as e:
                logging.warning(f"[{user.name}] 撤销 {order_type}={order_id} 失败: {e}")
    if cancelled:
        logging.info(f"[{user.name}] {reason} - 已撤销: {', '.join(cancelled)}")
        trade_logger.clear_order_ids(user.name)


async def handle_close_request(
    user: TradingUser,
    close_request: Dict,
    trade_logger: TradeLogger,
) -> bool:
    action_type = close_request.get("action", "close")
    try:
        if action_type == "tp1":
            close_qty = close_request["close_quantity"]
            remaining_qty = close_request.get("remaining_quantity", close_qty)
            tp1_qty = round_quantity_to_step_size(close_qty)
            await user.close_position_market(
                symbol=SYMBOL,
                side=close_request["side"],
                quantity=tp1_qty,
            )
            logging.info(f"[{user.name}] TP1 平仓成功: {tp1_qty:.4f} BTC")

            tp2_price = close_request.get("tp2_price")
            position_side = close_request.get("position_side", "buy")
            if tp2_price and remaining_qty > 0:
                await _place_tp2_order(user, trade_logger, remaining_qty, float(tp2_price), position_side)
        else:
            exit_reason = close_request.get("exit_reason", "close")
            await _cancel_related_orders(user, trade_logger, reason=f"平仓({exit_reason})")
            try:
                has_position = await user.has_open_position(SYMBOL)
            except Exception:
                has_position = True
            if has_position:
                close_qty = round_quantity_to_step_size(close_request["quantity"])
                await user.close_position_market(
                    symbol=SYMBOL,
                    side=close_request["side"],
                    quantity=close_qty,
                )
                logging.info(f"[{user.name}] 平仓成功: {exit_reason}")
            await user.cancel_all_orders(SYMBOL)
            trade_logger.clear_order_ids(user.name)
        return True
    except Exception as close_err:
        logging.error(f"[{user.name}] 平仓失败: {close_err}")
        return False


async def _place_tp2_order(
    user: TradingUser,
    trade_logger: TradeLogger,
    remaining_qty: float,
    tp2_price: float,
    position_side: str,
) -> bool:
    close_side = "SELL" if position_side.lower() == "buy" else "BUY"
    qty = round_quantity_to_step_size(remaining_qty)
    try:
        if close_side == "SELL":
            tp2_limit = round(tp2_price * 0.9995, 2)
        else:
            tp2_limit = round(tp2_price * 1.0005, 2)
        response = await user.create_take_profit_limit_order(
            symbol=SYMBOL,
            side=close_side,
            quantity=qty,
            price=tp2_limit,
            stop_price=round(tp2_price, 2),
            reduce_only=True,
        )
        tp2_order_id = response.get("orderId")
        trade_logger.update_tp2_sl_order_ids(user.name, tp2_order_id, None)
        logging.info(f"[{user.name}] TP2 限价止盈单已挂: ID={tp2_order_id}")
        return True
    except Exception as e:
        logging.error(f"[{user.name}] TP2 挂单失败: {e}")
        return False
