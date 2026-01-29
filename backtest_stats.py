"""
BrooksQuant 回测统计脚本 - 仅统计各策略交易次数与胜率，不影响实盘

- 不导入 config，不连接数据库/Redis，不触发实盘逻辑
- 仅使用 backtest 模块做历史回测，输出按信号类型统计的交易次数与胜率
- 可用于评估各形态（Spike / FailedBreakout / Climax / Wedge / H2 / L2）的表现

使用:
    python backtest_stats.py --symbol BTCUSDT --interval 5m --start 2024-01-01 --end 2024-12-31
    python backtest_stats.py   # 默认最近 30 天
"""

import asyncio
import argparse
import os
import sys
from datetime import datetime, timedelta

# 在导入任何可能写日志的模块之前，将日志目录指向临时目录，避免写入实盘日志
os.environ["LOG_DIR"] = os.environ.get("BACKTEST_STATS_LOG_DIR", "/tmp/backtest_stats_logs")
os.makedirs(os.environ["LOG_DIR"], exist_ok=True)

# 只导入回测相关模块（不导入 config / order_executor / main 等）
from backtest import BacktestConfig, BacktestEngine, BacktestResult


def print_stats_report(result: BacktestResult) -> None:
    """打印各策略交易次数与胜率（核心统计）"""
    cfg = result.config
    print()
    print("=" * 72)
    print("  BrooksQuant 回测统计 - 各策略交易次数与胜率")
    print("=" * 72)
    print(f"  交易对: {cfg.symbol}  周期: {cfg.interval}")
    print(f"  时间: {cfg.start_date} ~ {cfg.end_date}")
    print(f"  总交易数: {result.total_trades}  总胜率: {result.win_rate:.1f}%")
    print("=" * 72)
    print(f"  {'策略(信号类型)':<24} {'交易次数':>8} {'胜率':>10} {'盈利次数':>8} {'亏损次数':>8} {'总盈亏':>12}")
    print("-" * 72)

    if not result.stats_by_signal:
        print("  (无交易)")
        print("=" * 72)
        return

    # 按交易次数从高到低排序
    for signal, stats in sorted(
        result.stats_by_signal.items(),
        key=lambda x: -x[1]["count"],
    ):
        count = stats["count"]
        wins = stats["wins"]
        losses = stats["losses"]
        win_rate = stats["win_rate"]
        total_pnl = stats["total_pnl"]
        print(f"  {signal:<24} {count:>8} {win_rate:>9.1f}% {wins:>8} {losses:>8} {total_pnl:>+11.2f}")

    print("=" * 72)
    print()


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="回测统计：各策略交易次数与胜率（不影响实盘）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--symbol", default="BTCUSDT", help="交易对")
    parser.add_argument("--interval", default="5m", help="K 线周期")
    parser.add_argument("--start", default=None, help="开始日期 YYYY-MM-DD，默认 30 天前")
    parser.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--capital", type=float, default=10000.0, help="初始资金 USDT")
    parser.add_argument("--leverage", type=int, default=20, help="杠杆倍数")
    parser.add_argument("--sl-atr", type=float, default=2.0, help="止损 ATR 倍数")
    args = parser.parse_args()

    end_dt = datetime.now() if not args.end else datetime.strptime(args.end, "%Y-%m-%d")
    start_date = args.start or (end_dt - timedelta(days=30)).strftime("%Y-%m-%d")
    end_date = end_dt.strftime("%Y-%m-%d")

    config = BacktestConfig(
        symbol=args.symbol,
        interval=args.interval,
        start_date=start_date,
        end_date=end_date,
        initial_capital=args.capital,
        leverage=args.leverage,
        stop_loss_atr_multiplier=args.sl_atr,
    )

    engine = BacktestEngine(config)
    print("正在下载 K 线数据...", file=sys.stderr)
    df = await engine.load_data()
    print("正在回测...", file=sys.stderr)
    result = await engine.run(df)
    print_stats_report(result)


if __name__ == "__main__":
    asyncio.run(main())
