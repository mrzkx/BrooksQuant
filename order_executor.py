"""
订单执行模块

负责观察模式和实盘模式的订单执行逻辑
将下单逻辑从 main.py 中抽离，提高代码可维护性

实盘开仓逻辑（Al Brooks 软止损版）：
- 开仓后用币安真实持仓更新内部状态与数量
- 开仓后挂 TP1 止盈市价单
- TP1 触发后立即挂 TP2 限价止盈单（不再挂硬止损）
- 止损由本地在 K 线收盘时检查并市价执行（软止损）
- 平仓前先撤销所有关联挂单，避免重复平仓

Al Brooks 软止损逻辑：
- Crypto 市场"插针"频繁，硬止损容易被假突破误触发
- 软止损等 K 线收盘确认，使用收盘价判断是否真正跌破止损位
- 触发后立即市价平仓，确保成交
"""

import asyncio
import logging
from typing import Dict

from config import SYMBOL as CONFIG_SYMBOL, ORDER_PRICE_OFFSET_PCT, ORDER_PRICE_OFFSET_TICKS
from logic.trader_equation import satisfies_trader_equation
from trade_logger import TradeLogger
from user_manager import TradingUser
from utils import round_quantity_to_step_size

SYMBOL = CONFIG_SYMBOL


def _extract_signal_params(signal: Dict) -> Dict:
    """
    提取信号中的通用参数（避免重复代码）
    
    Args:
        signal: 信号字典
    
    Returns:
        提取的参数字典
    """
    return {
        "tp1_price": signal.get("tp1_price"),
        "tp2_price": signal.get("tp2_price"),
        "market_state": signal.get("market_state", "Unknown"),
        "tight_channel_score": signal.get("tight_channel_score", 0.0),
        "signal_strength": signal.get("signal_strength", 0.0),
        "tp1_close_ratio": signal.get("tp1_close_ratio", 0.5),
        "is_climax_bar": signal.get("is_climax_bar", False),
        "move_stop_to_breakeven_at_tp1": signal.get("move_stop_to_breakeven_at_tp1", False),
    }


def _satisfies_trader_equation(signal: Dict) -> bool:
    """交易者方程：WinRate × Reward > Risk 时才允许执行（委托公共函数）。"""
    params = _extract_signal_params(signal)
    entry = float(signal.get("price", 0))
    stop_loss = float(signal.get("stop_loss", 0))
    tp1 = params.get("tp1_price")
    tp2 = params.get("tp2_price")
    if not tp1 or not tp2 or entry <= 0:
        return True
    tp1, tp2 = float(tp1), float(tp2)
    tp1_close_ratio = float(params.get("tp1_close_ratio", 0.5))
    side = (signal.get("side") or "").lower()
    return satisfies_trader_equation(
        entry, stop_loss, tp1, tp2, tp1_close_ratio, side, win_rate=None, enabled=True
    )


def _log_order_execution(
    user: TradingUser,
    signal: Dict,
    params: Dict,
    order_qty: float,
    position_value: float,
    is_observe: bool,
    entry_price: float = None,
    quantity: float = None,
    status_emoji: str = None,
    status_text: str = None,
) -> None:
    """统一订单执行后的日志与 print（有 TP1/TP2 与无两种分支）。"""
    entry = entry_price if entry_price is not None else signal["price"]
    qty = quantity if quantity is not None else order_qty
    has_tp = params.get("tp1_price") and params.get("tp2_price")
    if is_observe:
        if has_tp:
            logging.info(
                f"[{user.name}] 📝 观察模式记录: {signal['signal']} {signal['side']} @ {entry:.2f}, "
                f"数量={qty:.4f} BTC (≈{position_value:.2f} USDT), 止损={signal['stop_loss']:.2f}, "
                f"TP1={params['tp1_price']:.2f}(50%), TP2={params['tp2_price']:.2f}(50%)"
            )
            print(
                f"[{user.name}] 📝 观察模式: {signal['signal']} {signal['side']} @ {entry:.2f}, "
                f"止损={signal['stop_loss']:.2f}, TP1={params['tp1_price']:.2f}(50%), TP2={params['tp2_price']:.2f}(50%)"
            )
        else:
            logging.info(
                f"[{user.name}] 📝 观察模式记录: {signal['signal']} {signal['side']} @ {entry:.2f}, "
                f"数量={qty:.4f} BTC (≈{position_value:.2f} USDT), 止损={signal['stop_loss']:.2f}, 止盈={signal['take_profit']:.2f}"
            )
            print(
                f"[{user.name}] 📝 观察模式: {signal['signal']} {signal['side']} @ {entry:.2f}, "
                f"止损={signal['stop_loss']:.2f}, 止盈={signal['take_profit']:.2f}"
            )
    else:
        emoji = status_emoji or "✅"
        text = status_text or "已成交"
        if has_tp:
            logging.info(
                f"[{user.name}] {emoji} 实盘限价单{text}: {signal['signal']} {signal['side']} @ {entry:.2f}, "
                f"数量={qty:.4f} BTC, 止损={signal['stop_loss']:.2f}, "
                f"TP1={params['tp1_price']:.2f}(50%), TP2={params['tp2_price']:.2f}(50%) [K线动态退出]"
            )
        else:
            logging.info(
                f"[{user.name}] {emoji} 实盘限价单{text}: {signal['signal']} {signal['side']} @ {entry:.2f}, "
                f"数量={qty:.4f} BTC, 止损={signal['stop_loss']:.2f}, 止盈={signal['take_profit']:.2f} [K线动态退出]"
            )
        print(f"[{user.name}] {emoji} 实盘限价单{text}: {signal['signal']} {signal['side']} @ {entry:.2f}")


async def execute_observe_order(
    user: TradingUser,
    signal: Dict,
    order_qty: float,
    position_value: float,
    trade_logger: TradeLogger,
    calculate_order_quantity_func,
) -> None:
    """
    执行观察模式下单（模拟交易）
    
    Args:
        user: 交易用户
        signal: 信号字典
        order_qty: 下单数量
        position_value: 仓位价值
        trade_logger: 交易日志器
        calculate_order_quantity_func: 计算下单数量的函数
    """
    # 交易者方程：WinRate×Reward>Risk 不满足则跳过
    if not _satisfies_trader_equation(signal):
        logging.info(
            f"[{user.name}] ⏭ 交易者方程不满足跳过: {signal.get('signal')} {signal.get('side')}, "
            "Risk过大或Reward不足"
        )
        return

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
        tight_channel_score=params["tight_channel_score"],
        is_observe=True,
        tp1_close_ratio=params["tp1_close_ratio"],
        is_climax_bar=params["is_climax_bar"],
    )
    _log_order_execution(user, signal, params, order_qty, position_value, is_observe=True)


async def execute_live_order(
    user: TradingUser,
    signal: Dict,
    order_qty: float,
    position_value: float,
    trade_logger: TradeLogger,
    signal_queue: asyncio.Queue,
) -> bool:
    """
    执行实盘下单
    
    Args:
        user: 交易用户
        signal: 信号字典
        order_qty: 下单数量
        position_value: 仓位价值
        trade_logger: 交易日志器
        signal_queue: 信号队列（用于 task_done）
    
    Returns:
        bool: 是否成功
    """
    # 交易者方程：WinRate×Reward>Risk 不满足则跳过
    if not _satisfies_trader_equation(signal):
        logging.info(
            f"[{user.name}] ⏭ 交易者方程不满足跳过: {signal.get('signal')} {signal.get('side')}, "
            "Risk过大或Reward不足"
        )
        return False
    
    # 提取信号参数（使用公共函数避免重复）
    params = _extract_signal_params(signal)
    
    signal_type = signal["signal"]
    
    try:
        # ===== 所有信号统一：追价限价单（订单簿最优价 + 可选偏移）=====
        limit_price = await user.get_limit_price_from_order_book(
            SYMBOL,
            signal["side"].upper(),
            offset_pct=ORDER_PRICE_OFFSET_PCT,
            offset_ticks=ORDER_PRICE_OFFSET_TICKS,
        )
        
        logging.info(
            f"[{user.name}] 🎯 执行限价开仓（追价限价单 offset_pct={ORDER_PRICE_OFFSET_PCT} ticks={ORDER_PRICE_OFFSET_TICKS}）: "
            f"{signal_type} {signal['side'].upper()} @ {limit_price:.2f}, 数量={order_qty:.4f} BTC, "
            f"持仓价值≈{position_value:.2f} USDT"
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
        
        logging.info(f"[{user.name}] 限价开仓单已提交: ID={order_id}, 状态={order_status}")
        
        # 等待限价单成交（超时60秒）
        if order_status == "NEW":
            try:
                entry_response = await user.wait_for_order_fill(
                    symbol=SYMBOL,
                    order_id=order_id,
                    timeout_seconds=60.0,
                    poll_interval=2.0,
                )
                order_status = entry_response.get("status", "FILLED")
                logging.info(f"[{user.name}] 限价单成交确认: 状态={order_status}")
            except TimeoutError:
                logging.warning(f"[{user.name}] 限价单超时未成交，跳过此信号")
                return False
            except Exception as wait_err:
                logging.error(f"[{user.name}] 等待限价单成交出错: {wait_err}")
                return False
        
        # 实际成交价
        price = entry_response.get("price", "0")
        avg_price = entry_response.get("avgPrice", "0")
        if avg_price and float(avg_price) > 0:
            actual_price = float(avg_price)
        elif price and float(price) > 0:
            actual_price = float(price)
        else:
            actual_price = limit_price
        
        actual_qty = float(entry_response.get("origQty", order_qty))
        
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
            tight_channel_score=params["tight_channel_score"],
            is_observe=False,
            tp1_close_ratio=params["tp1_close_ratio"],
            is_climax_bar=params["is_climax_bar"],
            hard_stop_loss=None,
        )
        
        # 使用币安真实持仓更新状态与数量
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
            logging.warning(f"[{user.name}] 开仓后同步币安持仓失败: {sync_err}，使用下单数量")
        
        # 开仓后挂 TP1 止盈单；TP1 触发后立即挂 TP2 限价止盈单 + 止损单（OCO 风格）
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
                logging.info(
                    f"[{user.name}] ✅ TP1 止盈单已挂: ID={tp1_order_id}, 触发价={params['tp1_price']:.2f}, "
                    f"数量={tp1_qty:.4f} ({int(tp1_close_ratio*100)}%)，TP1 触发后将自动挂 TP2+止损"
                )
            except Exception as tp1_err:
                logging.error(f"[{user.name}] ⚠️ TP1 止盈单挂单失败: {tp1_err}")
        
        status_emoji = "✅" if order_status == "FILLED" else "📝"
        status_text = "已成交" if order_status == "FILLED" else f"挂单中({order_status})"
        _log_order_execution(
            user, signal, params, order_qty, position_value, is_observe=False,
            entry_price=actual_price, quantity=actual_qty,
            status_emoji=status_emoji, status_text=status_text,
        )
        return True
        
    except Exception as exc:
        logging.exception(f"[{user.name}] ❌ 实盘下单失败: {exc}")
        print(f"[{user.name}] ❌ 实盘下单失败: {exc}")
        return False


async def _cancel_related_orders(
    user: TradingUser,
    trade_logger: TradeLogger,
    reason: str = "平仓前撤单",
) -> None:
    """
    撤销当前持仓关联的所有挂单（TP1/TP2/SL）
    
    在平仓前调用，避免重复平仓或挂单残留
    
    Args:
        user: 交易用户
        trade_logger: 交易日志器
        reason: 撤单原因（用于日志）
    """
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
        logging.info(f"[{user.name}] 🗑️ {reason} - 已撤销: {', '.join(cancelled)}")
        trade_logger.clear_order_ids(user.name)


async def _place_tp2_order(
    user: TradingUser,
    trade_logger: TradeLogger,
    remaining_qty: float,
    tp2_price: float,
    position_side: str,
) -> bool:
    """
    TP1 触发后挂 TP2 限价止盈单（Al Brooks 软止损版）
    
    Al Brooks 软止损修正：
    - 不再挂交易所止损单（硬止损）
    - 止损由本地在 K 线收盘时检查并市价执行（软止损）
    - Crypto 市场"插针"频繁，软止损可避免被假突破误触发
    
    使用限价止盈单（TAKE_PROFIT_LIMIT）替代市价止盈单，降低滑点风险。
    
    Args:
        user: 交易用户
        trade_logger: 交易日志器
        remaining_qty: 剩余仓位数量
        tp2_price: TP2 止盈价格
        position_side: 原始开仓方向（"buy" 或 "sell"）
    
    Returns:
        bool: 是否成功挂单
    """
    # 平仓方向：买入开仓用卖出平仓，反之亦然
    close_side = "SELL" if position_side.lower() == "buy" else "BUY"
    qty = round_quantity_to_step_size(remaining_qty)
    
    tp2_order_id = None
    
    # ========== 挂 TP2 限价止盈单 ==========
    # 使用 TAKE_PROFIT_LIMIT 类型：触发价 = TP2，限价 = TP2 - 小偏移（确保成交）
    try:
        # 限价略微让利以确保成交（买入平仓加价，卖出平仓减价）
        if close_side == "SELL":
            # 做多平仓：限价略低于触发价
            tp2_limit_price = round(tp2_price * 0.9995, 2)  # 0.05% 让利
        else:
            # 做空平仓：限价略高于触发价
            tp2_limit_price = round(tp2_price * 1.0005, 2)
        
        tp2_response = await user.create_take_profit_limit_order(
            symbol=SYMBOL,
            side=close_side,
            quantity=qty,
            price=tp2_limit_price,
            stop_price=round(tp2_price, 2),
            reduce_only=True,
        )
        tp2_order_id = tp2_response.get("orderId")
        logging.info(
            f"[{user.name}] ✅ TP2 限价止盈单已挂: ID={tp2_order_id}, "
            f"触发价={tp2_price:.2f}, 限价={tp2_limit_price:.2f}, 数量={qty:.4f}"
        )
    except Exception as tp2_err:
        logging.error(f"[{user.name}] ⚠️ TP2 限价止盈单挂单失败: {tp2_err}")
    
    # 更新订单 ID 到 trade_logger（止损 ID 不再需要）
    if tp2_order_id:
        trade_logger.update_tp2_sl_order_ids(user.name, tp2_order_id, None)
        return True
    
    return False


async def handle_close_request(
    user: TradingUser,
    close_request: Dict,
    trade_logger: TradeLogger,
) -> bool:
    """
    处理平仓请求（Al Brooks 软止损版）
    
    TP1 触发：立即挂 TP2 限价止盈单（不再挂硬止损）
    止损触发：由本地在 K 线收盘时检查，触发后市价平仓
    完全平仓：先撤销所有关联挂单，再执行市价平仓
    
    Args:
        user: 交易用户
        close_request: 平仓请求字典
        trade_logger: 交易日志器
    
    Returns:
        bool: 是否成功
    """
    action_type = close_request.get("action", "close")
    
    try:
        if action_type == "tp1":
            # ========== TP1 触发：平仓部分仓位 + 挂 TP2/SL ==========
            close_qty = close_request["close_quantity"]
            remaining_qty = close_request.get("remaining_quantity", close_qty)
            total_qty = close_qty + remaining_qty
            close_pct = int((close_qty / total_qty) * 100) if total_qty > 0 else 50
            
            logging.info(f"[{user.name}] 🎯 执行TP1: 平仓{close_pct}%，剩余{remaining_qty:.4f} BTC")
            
            # 按 stepSize 截断数量
            tp1_qty = round_quantity_to_step_size(close_qty)
            await user.close_position_market(
                symbol=SYMBOL,
                side=close_request["side"],
                quantity=tp1_qty,
            )
            
            # 查询实际成交价
            try:
                await asyncio.sleep(1)
                trade_details = await user.get_trade_details(SYMBOL, tp1_qty)
                actual_tp1_price = trade_details["avg_price"] if trade_details["avg_price"] > 0 else close_request['close_price']
                tp1_commission = trade_details["commission"]
            except Exception as detail_err:
                logging.warning(f"[{user.name}] 获取TP1成交详情失败: {detail_err}")
                actual_tp1_price = close_request['close_price']
                tp1_commission = 0
            
            logging.info(
                f"[{user.name}] ✅ TP1平仓成功: 数量={tp1_qty:.4f} BTC ({close_pct}%), "
                f"实际价格={actual_tp1_price:.2f}, 手续费={tp1_commission:.4f}"
            )
            print(f"[{user.name}] ✅ TP1平仓成功: 数量={tp1_qty:.4f} BTC ({close_pct}%), 价格={actual_tp1_price:.2f}")
            
            # ========== TP1 触发后挂 TP2 限价止盈单（Al Brooks 软止损版）==========
            # 不再挂硬止损单，止损由本地在 K 线收盘时检查并市价执行
            tp2_price = close_request.get("tp2_price")
            position_side = close_request.get("position_side", "buy")
            
            if tp2_price and remaining_qty > 0:
                await _place_tp2_order(
                    user=user,
                    trade_logger=trade_logger,
                    remaining_qty=remaining_qty,
                    tp2_price=float(tp2_price),
                    position_side=position_side,
                )
                logging.info(
                    f"[{user.name}] 🎯 TP1后已挂TP2限价止盈单: TP2={tp2_price:.2f} "
                    f"（软止损由本地收盘时检查）"
                )
            else:
                logging.warning(f"[{user.name}] ⚠️ 无TP2价格或剩余仓位为0，跳过TP2挂单")
        
        else:
            # ========== 完全平仓：先撤销所有关联挂单 ==========
            exit_reason = close_request.get('exit_reason', 'close')
            logging.info(f"[{user.name}] 🔴 执行平仓: {exit_reason}")
            
            # 先撤销所有关联挂单（TP1/TP2/SL）
            await _cancel_related_orders(user, trade_logger, reason=f"平仓({exit_reason})前撤单")
            
            try:
                has_position = await user.has_open_position(SYMBOL)
            except Exception as check_err:
                logging.warning(f"[{user.name}] 检查仓位失败: {check_err}，假设仓位存在继续平仓")
                has_position = True
            
            if has_position:
                close_qty = round_quantity_to_step_size(close_request["quantity"])
                await user.close_position_market(
                    symbol=SYMBOL,
                    side=close_request["side"],
                    quantity=close_qty,
                )
                logging.info(
                    f"[{user.name}] ✅ 平仓成功: {exit_reason}, "
                    f"数量={close_request['quantity']:.4f} BTC"
                )
                print(
                    f"[{user.name}] ✅ 平仓成功: {exit_reason}, "
                    f"数量={close_request['quantity']:.4f} BTC"
                )
                try:
                    await asyncio.sleep(1)
                    trade_details = await user.get_trade_details(SYMBOL, close_qty)
                    if trade_details["avg_price"] > 0:
                        trade_logger.update_trade_with_actual_pnl(
                            user=user.name,
                            actual_exit_price=trade_details["avg_price"],
                            commission=trade_details["commission"],
                        )
                except Exception as pnl_err:
                    logging.warning(f"[{user.name}] 更新实际盈亏失败: {pnl_err}")
            else:
                logging.info(f"[{user.name}] ℹ️ 仓位已不存在，仅更新盈亏")
                try:
                    trade_details = await user.get_trade_details(SYMBOL, close_request["quantity"])
                    if trade_details["avg_price"] > 0:
                        trade_logger.update_trade_with_actual_pnl(
                            user=user.name,
                            actual_exit_price=trade_details["avg_price"],
                            commission=trade_details["commission"],
                        )
                except Exception as pnl_err:
                    logging.warning(f"[{user.name}] 更新实际盈亏失败: {pnl_err}")
            
            # 最后确保清理所有挂单（双重保险）
            await user.cancel_all_orders(SYMBOL)
            trade_logger.clear_order_ids(user.name)
        
        return True
        
    except Exception as close_err:
        logging.error(f"[{user.name}] ❌ 平仓失败: {close_err}")
        print(f"[{user.name}] ❌ 平仓失败: {close_err}")
        return False
