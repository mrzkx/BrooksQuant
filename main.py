"""
BrooksQuant 交易系统 - 主入口

基于 Al Brooks 价格行为策略的量化交易系统

模块结构：
- main.py: 主入口和程序初始化
- strategy.py: BrooksStrategy（EA 逻辑的 Python 实现）
- logic/: 策略核心模块（constants, indicators, swing, hl, market_state, filters, signals, scan, sl, tp）
- workers/: 异步工作者模块（kline_producer, user_worker, stats_worker, helpers）
- order_executor.py: 订单执行逻辑（混合入场: Spike 市价 / 其他限价）
- trade_logger.py: 交易日志（内存 + 可选 Redis）
- user_manager.py: 用户管理
- delta_flow.py: 订单流分析（可选模块，默认不启用）
"""

import asyncio
import logging

from config import (
    load_user_credentials,
    REDIS_URL,
    DELTA_ENABLED,
    OBSERVE_BALANCE,
    POSITION_SIZE_PERCENT,
    LEVERAGE,
    SYMBOL as CONFIG_SYMBOL,
    KLINE_INTERVAL,
    OBSERVE_MODE,
)
from strategy import BrooksStrategy
from trade_logger import TradeLogger
from user_manager import TradingUser
from workers import kline_producer, user_worker, print_stats_periodically

SYMBOL = CONFIG_SYMBOL


def setup_logging():
    """配置日志系统"""
    from config import setup_logging as config_setup_logging
    return config_setup_logging()


async def main() -> None:
    """主函数"""
    logging.info("=" * 60)
    logging.info("BrooksQuant 交易系统启动")
    logging.info("=" * 60)

    credentials = load_user_credentials()
    logging.info(f"已加载 {len(credentials)} 组用户凭据")

    if OBSERVE_MODE and len(credentials) == 0:
        from config import UserCredentials
        credentials = [UserCredentials(api_key="", api_secret="")]
        logging.info("观察模式：使用默认用户（无需 API 密钥）")

    if len(credentials) == 0:
        raise RuntimeError(
            "需要在环境变量中配置至少一组用户凭据：USER1_API_KEY/USER1_API_SECRET"
        )

    users = [TradingUser(f"User{i+1}", cred) for i, cred in enumerate(credentials)]
    logging.info(f"已创建 {len(users)} 个交易用户: {[u.name for u in users]}")

    _log_mode_info()
    logging.info(f"交易对: {SYMBOL}, K线周期: {KLINE_INTERVAL}")

    strategy = BrooksStrategy()

    trade_logger = TradeLogger(redis_url=REDIS_URL)

    user_queues = [asyncio.Queue() for _ in users]
    close_queues = {user.name: asyncio.Queue() for user in users}

    logging.info("正在启动所有任务...")

    tasks = [
        asyncio.create_task(
            kline_producer(user_queues, close_queues, strategy, trade_logger)
        ),
    ]

    if DELTA_ENABLED:
        from delta_flow import aggtrade_worker
        tasks.append(
            asyncio.create_task(
                aggtrade_worker(SYMBOL, REDIS_URL, KLINE_INTERVAL)
            )
        )
        logging.info("Delta 订单流分析已启用")
    else:
        logging.info("Delta 订单流分析已禁用（DELTA_ENABLED=false）")

    for user, queue in zip(users, user_queues):
        tasks.append(
            asyncio.create_task(
                user_worker(user, queue, close_queues[user.name], trade_logger)
            )
        )

    tasks.append(
        asyncio.create_task(
            print_stats_periodically(trade_logger, users)
        )
    )

    logging.info(f"已创建 {len(tasks)} 个任务")
    logging.info("所有任务已启动，程序运行中...")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logging.info("任务已被取消")
    except Exception as e:
        logging.error(f"发生错误: {e}", exc_info=True)
    finally:
        logging.info("正在清理资源...")

        for task in tasks:
            if not task.done():
                task.cancel()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        for user in users:
            try:
                await user.close()
            except Exception as e:
                logging.warning(f"关闭用户 {user.name} 连接时出错: {e}")

        try:
            await trade_logger.close()
        except Exception as e:
            logging.warning(f"关闭交易日志器时出错: {e}")

        logging.info("程序已正常退出")


def _log_mode_info():
    """记录运行模式信息"""
    sep = "=" * 60
    if OBSERVE_MODE:
        line1 = "观察模式已启用 - 将进行模拟交易，不会实际下单"
        line2 = f"模拟资金: {OBSERVE_BALANCE} USDT, 仓位: {POSITION_SIZE_PERCENT}%, 杠杆: {LEVERAGE}x"
    else:
        line1 = "实际交易模式 - 将进行真实下单"
        line2 = f"仓位: {POSITION_SIZE_PERCENT}%, 杠杆: {LEVERAGE}x"
    for line in (sep, line1, line2, sep):
        logging.info(line)
        print(line)


if __name__ == "__main__":
    setup_logging()
    asyncio.run(main())
