"""
订单执行模块

负责观察模式和实盘模式的订单执行逻辑
将下单逻辑从 main.py 中抽离，提高代码可维护性

实盘开仓逻辑：
- 开仓后用币安真实持仓更新内部状态与数量
- 开仓后挂 TP1 止盈单（仅 TP1 挂单）
- TP1 触发后由程序决定移动止盈止损（不挂 TP2/止损单，程序监控后市价平仓）
"""

import asyncio
import logging
from decimal import Decimal, ROUND_DOWN
from typing import Dict

from config import SYMBOL as CONFIG_SYMBOL
from trade_logger import TradeLogger
from user_manager import TradingUser

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


def round_quantity_to_step_size(quantity: float, step_size: float = 0.001) -> float:
    """
    将数量按 stepSize 截断（向下取整）
    
    BTCUSDT 的 stepSize = 0.001，所以数量必须是 0.001 的整数倍
    
    Args:
        quantity: 原始数量
        step_size: 步长（默认 0.001）
    
    Returns:
        按步长截断后的数量
    """
    if step_size <= 0:
        step_size = 0.001
    
    # 使用 Decimal 确保精度
    qty_decimal = Decimal(str(quantity))
    step_decimal = Decimal(str(step_size))
    
    # 向下取整到最近的 step_size
    rounded = (qty_decimal / step_decimal).quantize(Decimal('1'), rounding=ROUND_DOWN) * step_decimal
    
    return max(float(rounded), step_size)  # 确保至少是最小数量


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
    # 提取信号参数（使用公共函数避免重复）
    params = _extract_signal_params(signal)
    
    # 记录观察模式交易
    trade = trade_logger.open_position(
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
    
    # 日志输出
    if params["tp1_price"] and params["tp2_price"]:
        logging.info(
            f"[{user.name}] 📝 观察模式记录: {signal['signal']} {signal['side']} @ {signal['price']:.2f}, "
            f"数量={order_qty:.4f} BTC (≈{position_value:.2f} USDT), 止损={signal['stop_loss']:.2f}, "
            f"TP1={params['tp1_price']:.2f}(50%), TP2={params['tp2_price']:.2f}(50%)"
        )
        print(
            f"[{user.name}] 📝 观察模式: {signal['signal']} {signal['side']} @ {signal['price']:.2f}, "
            f"止损={signal['stop_loss']:.2f}, TP1={params['tp1_price']:.2f}(50%), TP2={params['tp2_price']:.2f}(50%)"
        )
    else:
        logging.info(
            f"[{user.name}] 📝 观察模式记录: {signal['signal']} {signal['side']} @ {signal['price']:.2f}, "
            f"数量={order_qty:.4f} BTC (≈{position_value:.2f} USDT), 止损={signal['stop_loss']:.2f}, 止盈={signal['take_profit']:.2f}"
        )
        print(
            f"[{user.name}] 📝 观察模式: {signal['signal']} {signal['side']} @ {signal['price']:.2f}, "
            f"止损={signal['stop_loss']:.2f}, 止盈={signal['take_profit']:.2f}"
        )


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
    # 提取信号参数（使用公共函数避免重复）
    params = _extract_signal_params(signal)
    
    # 判断信号类型
    signal_type = signal["signal"]
    
    # 突破型信号：需要快速入场，使用市价单
    BREAKOUT_SIGNALS = [
        "Spike_Buy", "Spike_Sell", 
        "Failed_Breakout_Buy", "Failed_Breakout_Sell",
        "Climax_Buy", "Climax_Sell"
    ]
    
    is_breakout_signal = signal_type in BREAKOUT_SIGNALS
    
    try:
        if is_breakout_signal:
            # ===== 突破型信号：市价入场 =====
            logging.info(
                f"[{user.name}] 🚀 执行市价入场（突破型）: "
                f"{signal_type} {signal['side'].upper()} @ 市价, 数量={order_qty:.4f} BTC, "
                f"持仓价值≈{position_value:.2f} USDT"
            )
            
            entry_response = await user.create_market_order(
                symbol=SYMBOL,
                side=signal["side"].upper(),
                quantity=order_qty,
                reduce_only=False,
            )
            
            order_id = entry_response.get("orderId")
            order_status = entry_response.get("status", "FILLED")
            
            logging.info(f"[{user.name}] 市价开仓单已成交: ID={order_id}, 状态={order_status}")
            
            # 获取实际成交价
            avg_price = entry_response.get("avgPrice", "0")
            actual_price = float(avg_price) if avg_price and float(avg_price) > 0 else float(signal["price"])
        else:
            # ===== 回撤型信号：限价入场 =====
            signal_atr = signal.get("atr")
            
            limit_price = user.calculate_limit_price(
                current_price=signal["price"],
                side=signal["side"],
                slippage_pct=0.05,
                symbol=SYMBOL,
                atr=signal_atr,
            )
            
            logging.info(
                f"[{user.name}] 🎯 执行限价入场（回撤型）: "
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
            
            # 获取实际成交价（限价单可能还未成交）
            price = entry_response.get("price", "0")
            avg_price = entry_response.get("avgPrice", "0")
            # 优先使用成交均价，其次使用限价，最后使用信号价格
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
        
        # 开仓后挂 TP1 止盈单；TP1 触发后由程序决定移动止盈止损
        if params.get("tp1_price") and actual_qty > 0:
            tp1_close_ratio = params.get("tp1_close_ratio", 0.5)
            tp1_qty = round_quantity_to_step_size(actual_qty * tp1_close_ratio)
            stop_side = "SELL" if signal["side"].lower() == "buy" else "BUY"
            try:
                await user.create_take_profit_market_order(
                    symbol=SYMBOL,
                    side=stop_side,
                    quantity=tp1_qty,
                    stop_price=round(float(params["tp1_price"]), 2),
                    reduce_only=True,
                )
                trade_logger.mark_tp1_order_placed(user.name)
                logging.info(
                    f"[{user.name}] ✅ TP1 止盈单已挂: 触发价={params['tp1_price']:.2f}, "
                    f"数量={tp1_qty:.4f} ({int(tp1_close_ratio*100)}%)，TP1 触发后由程序决定止盈止损"
                )
            except Exception as tp1_err:
                logging.error(f"[{user.name}] ⚠️ TP1 止盈单挂单失败: {tp1_err}")
        
        # 日志输出
        status_emoji = "✅" if order_status == "FILLED" else "📝"
        order_type_text = "市价单" if is_breakout_signal else "限价单"
        status_text = "已成交" if order_status == "FILLED" else f"挂单中({order_status})"
        
        if params["tp1_price"] and params["tp2_price"]:
            logging.info(
                f"[{user.name}] {status_emoji} 实盘{order_type_text}{status_text}: {signal['signal']} {signal['side']} @ {actual_price:.2f}, "
                f"数量={actual_qty:.4f} BTC, 止损={signal['stop_loss']:.2f}, "
                f"TP1={params['tp1_price']:.2f}(50%), TP2={params['tp2_price']:.2f}(50%) [K线动态退出]"
            )
        else:
            logging.info(
                f"[{user.name}] {status_emoji} 实盘{order_type_text}{status_text}: {signal['signal']} {signal['side']} @ {actual_price:.2f}, "
                f"数量={actual_qty:.4f} BTC, 止损={signal['stop_loss']:.2f}, 止盈={signal['take_profit']:.2f} [K线动态退出]"
            )
        
        print(
            f"[{user.name}] {status_emoji} 实盘{order_type_text}{status_text}: {signal['signal']} {signal['side']} @ {actual_price:.2f}"
        )
        
        return True
        
    except Exception as exc:
        logging.exception(f"[{user.name}] ❌ 实盘下单失败: {exc}")
        print(f"[{user.name}] ❌ 实盘下单失败: {exc}")
        return False


async def handle_close_request(
    user: TradingUser,
    close_request: Dict,
    trade_logger: TradeLogger,
) -> bool:
    """
    处理平仓请求（止盈止损由程序执行，不挂委托）
    
    程序根据 K 线监控判断止损/TP1/TP2 触发后，在此执行市价平仓。
    
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
            # TP1触发：执行动态比例平仓并更新止损（动态保本）
            close_qty = close_request["close_quantity"]
            total_qty = close_qty + close_request.get("remaining_quantity", close_qty)
            close_pct = int((close_qty / total_qty) * 100) if total_qty > 0 else 50
            
            logging.info(f"[{user.name}] 🎯 执行TP1: 平仓{close_pct}%")
            
            # 按 stepSize 截断数量（修复精度问题）
            tp1_qty = round_quantity_to_step_size(close_request["close_quantity"])
            await user.close_position_market(
                symbol=SYMBOL,
                side=close_request["side"],
                quantity=tp1_qty,
            )
            
            # 查询实际成交价
            try:
                await asyncio.sleep(1)  # 等待成交记录更新
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
            # 保本止损与 TP2 由程序监控执行，不挂委托
        
        else:
            # 完全平仓（止盈/止损由程序触发，此处执行市价平仓）
            logging.info(f"[{user.name}] 🔴 执行平仓: {close_request}")
            
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
                    f"[{user.name}] ✅ 平仓成功: {close_request['exit_reason']}, "
                    f"数量={close_request['quantity']:.4f} BTC"
                )
                print(
                    f"[{user.name}] ✅ 平仓成功: {close_request['exit_reason']}, "
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
            
            await user.cancel_all_orders(SYMBOL)  # 清理可能存在的其他挂单
        
        return True
        
    except Exception as close_err:
        logging.error(f"[{user.name}] ❌ 平仓失败: {close_err}")
        print(f"[{user.name}] ❌ 平仓失败: {close_err}")
        return False
