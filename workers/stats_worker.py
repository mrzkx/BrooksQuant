"""
统计打印工作者模块
"""

import asyncio
import logging
from typing import List

from config import OBSERVE_MODE
from trade_logger import TradeLogger
from user_manager import TradingUser


async def print_stats_periodically(
    trade_logger: TradeLogger, 
    users: List[TradingUser]
) -> None:
    """
    定期打印交易统计（根据当前模式过滤）
    
    日志级别优化：
    - 有交易或持仓时：INFO 级别（重要信息）
    - 无交易且无持仓时：DEBUG 级别（避免刷屏）
    """
    await asyncio.sleep(60)  # 启动后等待1分钟再开始统计
    
    while True:
        await asyncio.sleep(300)  # 每5分钟打印一次
        
        mode_label = "观察模式" if OBSERVE_MODE else "实盘模式"
        
        # 先收集所有用户的统计信息，判断是否有活动
        has_activity = False
        user_stats_list = []
        
        for user in users:
            stats = trade_logger.get_user_stats(user.name, is_observe=OBSERVE_MODE)
            
            # 检查是否有持仓
            has_position = (
                user.name in trade_logger.positions
                and trade_logger.positions[user.name] is not None
            )
            position_info = ""
            if has_position:
                pos = trade_logger.positions[user.name]
                entry_price = getattr(pos, 'entry_price', 0) or 0
                if entry_price > 0:
                    position_info = f", 当前持仓: {pos.signal} {pos.side} @ {entry_price:.2f}"
                    has_activity = True
            
            # 有交易记录也算活动
            if stats['total_trades'] > 0:
                has_activity = True

            mode_tag = "🔍观察" if OBSERVE_MODE else "💰实盘"
            stats_msg = (
                f"[{user.name}] {mode_tag} | 总交易: {stats['total_trades']}, "
                f"盈利: {stats['winning_trades']}, 亏损: {stats['losing_trades']}, "
                f"胜率: {stats['win_rate']:.2f}%, 总盈亏: {stats['total_pnl']:.4f} USDT{position_info}"
            )
            user_stats_list.append(stats_msg)
        
        # 根据是否有活动选择日志级别
        log_func = logging.info if has_activity else logging.debug
        
        log_func("=" * 60)
        log_func(f"定期交易统计 ({mode_label}):")
        
        # 只在有活动时打印到控制台
        if has_activity:
            print("\n" + "=" * 60)
            print(f"📊 定期交易统计 ({mode_label}):")
        
        for stats_msg in user_stats_list:
            log_func(stats_msg)
            if has_activity:
                print(stats_msg)
        
        log_func("=" * 60)
        if has_activity:
            print("=" * 60 + "\n")
