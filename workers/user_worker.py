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
        # 恢复币安真实持仓
        await _recover_binance_position(user, trade_logger)

    signal_count = 0
    while True:
        try:
            # 等待信号或平仓请求（TP1 是否触发在 K 线周期结束时由 kline_producer 投递 sync_tp1 检测）
            signal_task = asyncio.create_task(signal_queue.get())
            close_task = asyncio.create_task(close_queue.get())
            done, pending = await asyncio.wait(
                [signal_task, close_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            completed_task = close_task if close_task in done else signal_task
            result = completed_task.result()

            # 周期结束时 TP1 触发检测（由 kline_producer 在 K 线收盘时投递）
            if isinstance(result, dict) and result.get("action") == "sync_tp1":
                if not OBSERVE_MODE:
                    await _sync_tp1_if_filled(user, trade_logger)
                continue

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


async def _recover_binance_position(user: TradingUser, trade_logger: TradeLogger) -> None:
    """根据币安真实持仓恢复交易状态"""
    try:
        # 获取币安真实持仓
        position_info = await user.get_position_info(SYMBOL)
        
        if not position_info:
            logging.info(f"[{user.name}] 币安无持仓，无需恢复")
            return
        
        # 获取当前价格（用于计算止损止盈）
        try:
            ticker = await user.client.futures_symbol_ticker(symbol=SYMBOL)
            current_price = float(ticker.get("price", 0))
        except Exception as e:
            logging.warning(f"[{user.name}] 获取当前价格失败: {e}，使用标记价格")
            current_price = position_info.get("markPrice", position_info.get("entryPrice", 0))
        
        if current_price <= 0:
            logging.error(f"[{user.name}] 无法获取有效价格，跳过持仓恢复")
            return
        
        # 获取 ATR（用于计算止损距离）
        # 使用入场价的 1% 作为默认止损距离
        atr = current_price * 0.01  # 1% 作为默认 ATR
        
        # 调用 trade_logger 恢复持仓状态
        trade = trade_logger.recover_from_binance_position(
            user=user.name,
            position_info=position_info,
            current_price=current_price,
            atr=atr,
        )
        
        if trade:
            logging.info(
                f"[{user.name}] ✅ 成功恢复持仓: "
                f"{trade.side.upper()} {trade.quantity:.6f} BTC @ {trade.entry_price:.2f}, "
                f"止损={trade.stop_loss:.2f}, TP1={trade.tp1_price:.2f}(1R), TP2={trade.tp2_price:.2f}(2R)"
            )
            
            # 恢复后立即检查是否已经达到 TP1（使用原始策略逻辑）
            # 这样可以在下一个周期正常触发 50% 止盈
            try:
                tp1_result = trade_logger.check_stop_loss_take_profit(user.name, current_price)
                
                if tp1_result and isinstance(tp1_result, dict) and tp1_result.get("action") == "tp1":
                    # TP1 已触发，发送到队列处理
                    logging.info(
                        f"[{user.name}] 🎯 恢复持仓时检测到 TP1 已触发: "
                        f"当前价={current_price:.2f} >= TP1={trade.tp1_price:.2f}, "
                        f"将在下个周期执行 50% 止盈"
                    )
                    # 注意：这里不立即执行，而是等待下一个 K 线周期
                    # 因为需要确保所有系统状态都已恢复
            except Exception as check_err:
                logging.warning(f"[{user.name}] 恢复后检查 TP1 失败: {check_err}")
        else:
            logging.warning(f"[{user.name}] ⚠️ 持仓恢复失败")
        
    except Exception as e:
        logging.error(f"[{user.name}] 恢复币安持仓失败: {e}", exc_info=True)


async def _handle_tp2_order(_user: TradingUser, _trade_logger: TradeLogger) -> None:
    """TP2 由程序监控执行平仓，不再挂单（保留函数签名兼容）"""
    return


async def _sync_tp1_if_filled(user: TradingUser, trade_logger: TradeLogger) -> None:
    """
    检测 TP1 是否已被交易所触发；若持仓减半则同步状态，之后由程序决定止盈止损。
    """
    if not trade_logger.needs_tp1_fill_sync(user.name):
        return
    try:
        pos = await user.get_position_info(SYMBOL)
        if not pos:
            return
        trade = trade_logger.positions.get(user.name)
        if not trade:
            return
        amt = abs(float(pos["positionAmt"]))
        entry_price = float(pos["entryPrice"])
        # 持仓明显减少（约一半）说明 TP1 已由交易所执行
        if amt <= float(trade.quantity) * 0.6:
            ok = trade_logger.sync_after_tp1_filled(user.name, amt, entry_price)
            if ok:
                await user.cancel_all_orders(SYMBOL)
                logging.info(
                    f"[{user.name}] TP1 已由交易所触发，已同步剩余仓位 {amt:.4f}，"
                    "后续由程序决定止盈止损"
                )
    except Exception as e:
        logging.debug(f"[{user.name}] TP1 同步检测: {e}")


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
            
            # 获取已占用的保证金（如果有未平仓的仓位）
            used_margin = await user.get_used_margin(SYMBOL)
            available_balance = real_balance - used_margin
            
            if available_balance <= 0:
                logging.warning(
                    f"[{user.name}] ⚠️ 可用余额不足: 总余额={real_balance:.2f}, "
                    f"已占用保证金={used_margin:.2f}, 可用余额={available_balance:.2f}"
                )
                return 0.0, 0.0
            
            # 使用可用余额计算仓位
            order_qty = user.calculate_order_quantity(
                balance=available_balance,  # 使用可用余额而不是总余额
                current_price=signal["price"],
                leverage=LEVERAGE,
            )
            
            if order_qty <= 0:
                logging.warning(f"[{user.name}] ⚠️ 计算出的数量为 0，无法下单")
                return 0.0, 0.0
            
            position_pct = user.calculate_position_size_percent(available_balance)
            position_value = available_balance * (position_pct / 100) * LEVERAGE
            
            if used_margin > 0:
                logging.info(
                    f"[{user.name}] 仓位计算: 总余额={real_balance:.2f} USDT, "
                    f"已占用保证金={used_margin:.2f} USDT, 可用余额={available_balance:.2f} USDT, "
                    f"仓位比例={position_pct:.0f}%, 杠杆={LEVERAGE}x, "
                    f"下单数量={order_qty:.6f} BTC (≈{order_qty * signal['price']:.2f} USDT)"
                )
            else:
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
