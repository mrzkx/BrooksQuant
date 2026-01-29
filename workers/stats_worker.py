"""
统计打印工作者模块
"""

import asyncio
import logging
import os
from typing import List

from trade_logger import TradeLogger
from user_manager import TradingUser

OBSERVE_MODE = os.getenv("OBSERVE_MODE", "true").lower() == "true"


async def print_stats_periodically(
    trade_logger: TradeLogger, 
    users: List[TradingUser]
) -> None:
    """
    定期打印交易统计（根据当前模式过滤）
    """
    await asyncio.sleep(60)  # 启动后等待1分钟再开始统计
    
    while True:
        await asyncio.sleep(300)  # 每5分钟打印一次
        
        mode_label = "观察模式" if OBSERVE_MODE else "实盘模式"
        logging.info("=" * 60)
        logging.info(f"定期交易统计 ({mode_label}):")
        print("\n" + "=" * 60)
        print(f"📊 定期交易统计 ({mode_label}):")
        
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
                # 确保 entry_price 有效（不为 0）
                entry_price = getattr(pos, 'entry_price', 0) or 0
                if entry_price > 0:
                    position_info = f", 当前持仓: {pos.signal} {pos.side} @ {entry_price:.2f}"

            mode_tag = "🔍观察" if OBSERVE_MODE else "💰实盘"
            stats_msg = (
                f"[{user.name}] {mode_tag} | 总交易: {stats['total_trades']}, "
                f"盈利: {stats['winning_trades']}, 亏损: {stats['losing_trades']}, "
                f"胜率: {stats['win_rate']:.2f}%, 总盈亏: {stats['total_pnl']:.4f} USDT{position_info}"
            )
            logging.info(stats_msg)
            print(stats_msg)
        
        logging.info("=" * 60)
        print("=" * 60 + "\n")
