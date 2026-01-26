"""
用户工作者模块

负责处理用户的信号执行和平仓请求
"""

import asyncio
import logging
import os
from typing import Dict, List

from config import LEVERAGE, SYMBOL as CONFIG_SYMBOL
from trade_logger import TradeLogger
from user_manager import TradingUser
from order_executor import execute_observe_order, execute_live_order, handle_close_request
from workers.helpers import calculate_order_quantity

SYMBOL = CONFIG_SYMBOL
OBSERVE_MODE = os.getenv("OBSERVE_MODE", "true").lower() == "true"


async def user_worker(
    user: TradingUser, 
    signal_queue: asyncio.Queue, 
    close_queue: asyncio.Queue,
    trade_logger: TradeLogger
) -> None:
    """
    用户信号处理工作者
    
    消费信号并为该用户下单（观察模式或实际下单）
    """
    logging.info(f"用户工作线程 [{user.name}] 已启动")

    if not OBSERVE_MODE:
        await _setup_live_trading(user)

    signal_count = 0
    while True:
        try:
            # 检查是否需要挂 TP2 订单
            if not OBSERVE_MODE and trade_logger.needs_tp2_order(user.name):
                await _handle_tp2_order(user, trade_logger)
            
            # 等待信号或平仓请求
            signal_task = asyncio.create_task(signal_queue.get())
            close_task = asyncio.create_task(close_queue.get())
            
            done, pending = await asyncio.wait(
                [signal_task, close_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            
            # 取消未完成的任务
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            
            completed_task = done.pop()
            result = completed_task.result()
            
            # 处理平仓请求（优先级高）
            if completed_task == close_task or (isinstance(result, dict) and result.get("action") in ["close", "tp1"]):
                if not OBSERVE_MODE:
                    await handle_close_request(user, result, trade_logger)
                continue
            
            # 处理信号
            signal: Dict = result
            signal_count += 1
            logging.info(
                f"[{user.name}] 收到信号 #{signal_count}: {signal['signal']} {signal['side']} @ {signal['price']:.2f}"
            )

            # 检查冷却期和反手条件
            if not _should_process_signal(user, signal, trade_logger):
                signal_queue.task_done()
                continue

            # 计算下单数量
            order_qty, position_value = await _calculate_position(user, signal)

            if OBSERVE_MODE:
                await execute_observe_order(
                    user, signal, order_qty, position_value, 
                    trade_logger, calculate_order_quantity
                )
            else:
                success = await execute_live_order(
                    user, signal, order_qty, position_value, 
                    trade_logger, signal_queue
                )
                if not success:
                    signal_queue.task_done()
                    continue

            signal_queue.task_done()
            
        except asyncio.CancelledError:
            logging.info(f"用户工作线程 [{user.name}] 已取消")
            break
        except Exception as e:
            logging.error(f"用户工作线程 [{user.name}] 出错: {e}", exc_info=True)
            signal_queue.task_done()


async def _setup_live_trading(user: TradingUser) -> None:
    """设置实盘交易环境"""
    logging.info(f"正在为用户 [{user.name}] 连接 Binance API...")
    await user.connect()
    logging.info(f"用户 [{user.name}] 已连接 Binance API")
    
    # 获取交易规则
    try:
        filters = await user.get_symbol_filters(SYMBOL)
        logging.info(
            f"[{user.name}] 获取交易规则: stepSize={filters['stepSize']}, "
            f"minQty={filters['minQty']}, tickSize={filters['tickSize']}"
        )
    except Exception as e:
        logging.warning(f"[{user.name}] 获取交易规则失败: {e}，将使用默认值")
    
    # 设置杠杆
    leverage_ok = await user.set_leverage(SYMBOL, leverage=LEVERAGE)
    if not leverage_ok:
        logging.error(f"[{user.name}] 设置杠杆失败，交易可能使用错误的杠杆倍数！")
    
    # 显示初始余额
    try:
        initial_balance = await user.get_futures_balance()
        position_pct = user.calculate_position_size_percent(initial_balance)
        logging.info(
            f"[{user.name}] 实盘模式: 余额={initial_balance:.2f} USDT, "
            f"仓位比例={position_pct:.0f}%, 杠杆={LEVERAGE}x"
        )
        print(
            f"[{user.name}] 实盘模式: 余额={initial_balance:.2f} USDT, "
            f"仓位比例={position_pct:.0f}% ({'全仓' if position_pct == 100 else '20%仓位'}), "
            f"杠杆={LEVERAGE}x"
        )
    except Exception as e:
        logging.error(f"[{user.name}] 获取初始余额失败: {e}")


async def _handle_tp2_order(user: TradingUser, trade_logger: TradeLogger) -> None:
    """处理TP2订单挂单"""
    trade = trade_logger.positions.get(user.name)
    if not trade:
        return
    
    try:
        tp2_qty = trade.remaining_quantity or (trade.quantity * 0.5)
        tp2_qty = max(round(float(tp2_qty), 3), 0.001)
        
        stop_side = "SELL" if trade.side == "buy" else "BUY"
        
        tp2_response = await user.create_take_profit_market_order(
            symbol=SYMBOL,
            side=stop_side,
            quantity=tp2_qty,
            stop_price=round(float(trade.tp2_price), 2),
            reduce_only=True,
        )
        tp2_order_id = tp2_response.get("orderId")
        trade_logger.mark_tp2_order_placed(user.name)
        
        logging.info(
            f"[{user.name}] ✅ TP2止盈单已设置: ID={tp2_order_id}, "
            f"触发价={trade.tp2_price:.2f}, 数量={tp2_qty:.4f} BTC (剩余50%)"
        )
        print(
            f"[{user.name}] ✅ TP2止盈单已设置: 触发价={trade.tp2_price:.2f}, "
            f"数量={tp2_qty:.4f} BTC"
        )
    except Exception as tp2_err:
        logging.error(f"[{user.name}] ⚠️ TP2止盈单设置失败: {tp2_err}")


def _should_process_signal(
    user: TradingUser, 
    signal: Dict, 
    trade_logger: TradeLogger
) -> bool:
    """检查是否应该处理信号"""
    # 检查冷却期
    if trade_logger.is_in_cooldown(user.name):
        logging.info(
            f"⏳ [{user.name}] 在冷却期内，跳过信号: {signal['signal']} {signal['side']}"
        )
        return False
    
    # 检查反手强度
    signal_strength = signal.get("signal_strength", 0.0)
    market_state_str = signal.get("market_state", "")
    
    # 动态反手阈值
    if market_state_str in ["Breakout", "StrongTrend"]:
        reversal_threshold = 1.5
    elif market_state_str == "TradingRange":
        reversal_threshold = 1.3  # 问题5修复：提高震荡市阈值
    else:
        reversal_threshold = 1.2
    
    if not trade_logger.should_allow_reversal(user.name, signal_strength, reversal_threshold):
        logging.info(
            f"❌ [{user.name}] 反手信号强度不足，跳过: {signal['signal']} {signal['side']} "
            f"(强度={signal_strength:.2f}, 阈值={reversal_threshold:.1f}x, 市场={market_state_str})"
        )
        return False
    
    return True


async def _calculate_position(user: TradingUser, signal: Dict) -> tuple:
    """计算仓位和下单数量"""
    from config import OBSERVE_BALANCE, POSITION_SIZE_PERCENT
    
    if OBSERVE_MODE:
        order_qty = calculate_order_quantity(signal["price"])
        position_value = OBSERVE_BALANCE * (POSITION_SIZE_PERCENT / 100) * LEVERAGE
    else:
        try:
            real_balance = await user.get_futures_balance(force_refresh=True)
            order_qty = user.calculate_order_quantity(
                balance=real_balance,
                current_price=signal["price"],
                leverage=LEVERAGE,
            )
            position_pct = user.calculate_position_size_percent(real_balance)
            position_value = real_balance * (position_pct / 100) * LEVERAGE
            
            logging.info(
                f"[{user.name}] 仓位计算: 余额={real_balance:.2f} USDT, "
                f"仓位比例={position_pct:.0f}%, 杠杆={LEVERAGE}x, "
                f"下单数量={order_qty:.6f} BTC (≈{position_value:.2f} USDT), "
                f"stepSize={user._symbol_filters.get(SYMBOL, {}).get('stepSize', 'N/A')}"
            )
        except Exception as e:
            logging.error(f"[{user.name}] 获取余额失败: {e}，使用默认仓位")
            order_qty = calculate_order_quantity(signal["price"])
            position_value = 0
    
    return order_qty, position_value
