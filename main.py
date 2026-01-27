"""
BrooksQuant 交易系统 - 主入口

基于 Al Brooks 价格行为策略的量化交易系统

模块结构：
- main.py: 主入口和程序初始化
- workers/: 异步工作者模块
  - kline_producer.py: K线数据流生产者
  - user_worker.py: 用户信号处理工作者
  - stats_worker.py: 统计打印工作者
  - helpers.py: 辅助函数
- order_executor.py: 订单执行逻辑
- strategy.py: 策略逻辑
- trade_logger.py: 交易日志
- user_manager.py: 用户管理
- delta_flow.py: 订单流分析
"""

import asyncio
import logging
import os

# 注意：BinanceSocketManager 队列大小需要在创建实例时通过 max_queue_size 参数设置
# 类属性设置在新版 binance 库中无效，已在 delta_flow.py 和 kline_producer.py 中修复
from binance import BinanceSocketManager

from config import (
    load_user_credentials, 
    REDIS_URL,
    OBSERVE_BALANCE,
    POSITION_SIZE_PERCENT,
    LEVERAGE,
    SYMBOL as CONFIG_SYMBOL,
    KLINE_INTERVAL,
)
from strategy import AlBrooksStrategy
from trade_logger import TradeLogger
from user_manager import TradingUser

# 动态订单流模块
from delta_flow import aggtrade_worker

# 工作者模块
from workers import kline_producer, user_worker, print_stats_periodically


# 交易参数
SYMBOL = CONFIG_SYMBOL

# 观察模式：设置为 True 时只模拟交易，不实际下单
OBSERVE_MODE = os.getenv("OBSERVE_MODE", "true").lower() == "true"


def setup_logging():
    """配置日志系统"""
    from config import setup_logging as config_setup_logging
    return config_setup_logging()


async def main() -> None:
    """主函数"""
    logging.info("=" * 60)
    logging.info("BrooksQuant 交易系统启动")
    logging.info("=" * 60)

    # 加载用户凭证
    credentials = load_user_credentials()
    logging.info(f"已加载 {len(credentials)} 组用户凭据")

    # 观察模式下，如果没有配置凭据，创建一个默认用户
    if OBSERVE_MODE and len(credentials) == 0:
        from config import UserCredentials
        credentials = [UserCredentials(api_key="", api_secret="")]
        logging.info("观察模式：使用默认用户（无需 API 密钥）")

    if len(credentials) == 0:
        raise RuntimeError(
            "需要在环境变量中配置至少一组用户凭据：USER1_API_KEY/USER1_API_SECRET"
        )

    # 创建用户
    users = [TradingUser(f"User{i+1}", cred) for i, cred in enumerate(credentials)]
    logging.info(f"已创建 {len(users)} 个交易用户: {[u.name for u in users]}")

    # 显示运行模式
    _log_mode_info()

    logging.info(f"交易对: {SYMBOL}, K线周期: {KLINE_INTERVAL}")

    # 初始化策略
    strategy = AlBrooksStrategy(
        ema_period=20,
        kline_interval=KLINE_INTERVAL,
        redis_url=REDIS_URL,
    )
    # 获取 Delta 窗口大小
    delta_window = strategy.delta_analyzer.WINDOW_SECONDS if strategy.delta_analyzer else 300
    logging.info(f"策略已初始化: EMA周期=20, K线周期={KLINE_INTERVAL}, Delta窗口={delta_window}秒")

    # 初始化交易日志器
    trade_logger = TradeLogger()
    trade_logger.sync_from_db()
    logging.info("交易日志器已初始化")

    # 创建队列
    user_queues = [asyncio.Queue() for _ in users]
    close_queues = {user.name: asyncio.Queue() for user in users}

    logging.info("正在启动所有任务...")
    
    # 创建任务列表
    tasks = [
        asyncio.create_task(
            kline_producer(user_queues, close_queues, strategy, trade_logger)
        ),
        asyncio.create_task(
            aggtrade_worker(SYMBOL, REDIS_URL, KLINE_INTERVAL)
        ),
    ]
    
    # 为每个用户创建工作者
    for user, queue in zip(users, user_queues):
        tasks.append(
            asyncio.create_task(
                user_worker(user, queue, close_queues[user.name], trade_logger)
            )
        )

    # 添加统计打印任务
    tasks.append(
        asyncio.create_task(
            print_stats_periodically(trade_logger, users)
        )
    )

    logging.info(f"已创建 {len(tasks)} 个任务（含动态订单流监控，Delta窗口={KLINE_INTERVAL}）")
    logging.info("所有任务已启动，程序运行中...")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logging.info("任务已被取消")
    except Exception as e:
        logging.error(f"发生错误: {e}", exc_info=True)
    finally:
        logging.info("正在清理资源...")
        for user in users:
            await user.close()
        logging.info("程序已正常退出")


def _log_mode_info():
    """记录运行模式信息"""
    if OBSERVE_MODE:
        logging.info("=" * 60)
        logging.info("观察模式已启用 - 将进行模拟交易，不会实际下单")
        logging.info(f"模拟资金: {OBSERVE_BALANCE} USDT, 仓位: {POSITION_SIZE_PERCENT}%, 杠杆: {LEVERAGE}x")
        logging.info("=" * 60)
        print("=" * 60)
        print("观察模式已启用 - 将进行模拟交易，不会实际下单")
        print(f"模拟资金: {OBSERVE_BALANCE} USDT, 仓位: {POSITION_SIZE_PERCENT}%, 杠杆: {LEVERAGE}x")
        print("=" * 60)
    else:
        logging.info("=" * 60)
        logging.info("实际交易模式 - 将进行真实下单")
        logging.info(f"仓位: {POSITION_SIZE_PERCENT}%, 杠杆: {LEVERAGE}x")
        logging.info("=" * 60)
        print("=" * 60)
        print("实际交易模式 - 将进行真实下单")
        print(f"仓位: {POSITION_SIZE_PERCENT}%, 杠杆: {LEVERAGE}x")
        print("=" * 60)


if __name__ == "__main__":
    setup_logging()
    asyncio.run(main())
