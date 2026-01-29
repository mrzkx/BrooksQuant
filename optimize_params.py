"""
BrooksQuant 参数优化脚本（网格搜索）

对「信号棒质量阈值」等参数做网格搜索，调用回测得到目标指标（如夏普、胜率、总盈亏），
输出最优参数或排序表。遗传算法为可选扩展（如 DEAP）。

使用示例:
    python optimize_params.py --start 2024-01-01 --end 2024-06-30
    python optimize_params.py --start 2024-01-01 --end 2024-06-30 --param close_position_pct 0.15 0.2 0.25 0.3 --param min_body_ratio 0.45 0.5 0.55 0.6
    python optimize_params.py --start 2024-01-01 --end 2024-06-30 --objective sharpe
"""

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import replace
from typing import Any, Dict, List, Optional

import pandas as pd

# 避免写入实盘日志
os.environ.setdefault("LOG_DIR", "/tmp/optimize_params_logs")
os.makedirs(os.environ.get("LOG_DIR"), exist_ok=True)

from backtest import BacktestConfig, BacktestEngine, BacktestResult
from logic.interval_params import INTERVAL_PARAMS


def grid_generator(param_space: Dict[str, List[float]]) -> List[Dict[str, float]]:
    """生成参数网格（笛卡尔积）"""
    if not param_space:
        return [{}]
    keys = list(param_space.keys())
    values = list(param_space.values())
    n = len(keys)
    out: List[Dict[str, float]] = []

    def recurse(idx: int, combo: Dict[str, float]) -> None:
        if idx == n:
            out.append(dict(combo))
            return
        for v in values[idx]:
            combo[keys[idx]] = v
            recurse(idx + 1, combo)

    recurse(0, {})
    return out


def run_single_backtest(
    config: BacktestConfig,
    df: pd.DataFrame,
    interval_key: str,
    param_overrides: Optional[Dict[str, float]] = None,
) -> BacktestResult:
    """运行单次回测；param_overrides 会临时覆盖 interval_params 中对应键（如 close_position_pct, min_body_ratio）。"""
    base_saved = None
    if param_overrides and interval_key in INTERVAL_PARAMS:
        base_saved = INTERVAL_PARAMS[interval_key]
        kwargs = {}
        if "close_position_pct" in param_overrides:
            kwargs["close_position_pct"] = param_overrides["close_position_pct"]
        if "min_body_ratio" in param_overrides:
            kwargs["min_body_ratio"] = param_overrides["min_body_ratio"]
        if kwargs:
            INTERVAL_PARAMS[interval_key] = replace(base_saved, **kwargs)
    try:
        engine = BacktestEngine(config)
        result = engine.run(df)
        return result
    finally:
        if base_saved is not None:
            INTERVAL_PARAMS[interval_key] = base_saved


def objective_value(result: BacktestResult, objective: str) -> float:
    """根据目标指标计算单一数值（越大越好）"""
    if objective == "sharpe":
        return result.sharpe_ratio
    if objective == "win_rate":
        return result.win_rate
    if objective == "total_pnl":
        return result.total_pnl
    if objective == "total_pnl_pct":
        return result.total_pnl_percent
    if objective == "profit_factor":
        return result.profit_factor
    # 默认：夏普
    return result.sharpe_ratio


async def main() -> None:
    parser = argparse.ArgumentParser(description="BrooksQuant 参数网格搜索")
    parser.add_argument("--symbol", type=str, default="BTCUSDT")
    parser.add_argument("--interval", type=str, default="5m")
    parser.add_argument("--start", type=str, default="2024-01-01")
    parser.add_argument("--end", type=str, default="2024-12-31")
    parser.add_argument("--capital", type=float, default=10000.0)
    parser.add_argument("--objective", type=str, default="sharpe", choices=["sharpe", "win_rate", "total_pnl", "total_pnl_pct", "profit_factor"])
    parser.add_argument("--param", action="append", nargs="+", help="例如: --param close_position_pct 0.15 0.2 0.25 --param min_body_ratio 0.5 0.6")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 解析参数空间（支持多组 --param name v1 v2）
    param_space: Dict[str, List[float]] = {}
    if args.param:
        for p in args.param:
            if len(p) >= 2:
                name = p[0]
                values = [float(x) for x in p[1:] if _is_float(x)]
                if values:
                    param_space[name] = values

    config = BacktestConfig(
        symbol=args.symbol,
        interval=args.interval,
        start_date=args.start,
        end_date=args.end,
        initial_capital=args.capital,
    )
    engine = BacktestEngine(config)
    logging.info("正在加载 K 线数据...")
    df = await engine.load_data()
    logging.info(f"数据加载完成: {len(df)} 根 K 线")

    interval_key = args.interval
    if interval_key not in INTERVAL_PARAMS:
        interval_key = "5m"

    grid = grid_generator(param_space)
    if not grid:
        grid = [{}]
    logging.info(f"参数网格数量: {len(grid)}")

    results: List[Dict[str, Any]] = []
    for i, combo in enumerate(grid):
        logging.info(f"运行 {i+1}/{len(grid)}: {combo}")
        try:
            result = run_single_backtest(config, df, interval_key, combo if combo else None)
            score = objective_value(result, args.objective)
            results.append({
                "params": dict(combo),
                "objective": score,
                "total_trades": result.total_trades,
                "win_rate": result.win_rate,
                "sharpe_ratio": result.sharpe_ratio,
                "total_pnl": result.total_pnl,
                "total_pnl_percent": result.total_pnl_percent,
                "profit_factor": result.profit_factor,
            })
        except Exception as e:
            logging.warning(f"组合 {combo} 回测失败: {e}")
            results.append({
                "params": dict(combo),
                "objective": float("-inf"),
                "total_trades": 0,
                "win_rate": 0.0,
                "sharpe_ratio": 0.0,
                "total_pnl": 0.0,
                "total_pnl_percent": 0.0,
                "profit_factor": 0.0,
            })

    # 按目标指标排序
    results.sort(key=lambda x: x["objective"], reverse=True)

    print("\n========== 参数优化结果 ==========")
    print(f"目标指标: {args.objective}")
    print(f"参数空间: {param_space or '无（单次回测）'}")
    print()
    for i, r in enumerate(results[:20]):
        print(f"  #{i+1}  {r['objective']:.4f}  |  {r['params']}  |  交易数={r['total_trades']}  胜率={r['win_rate']:.1f}%  Sharpe={r['sharpe_ratio']:.2f}  PnL%={r['total_pnl_percent']:.2f}%")
    if results:
        best = results[0]
        print(f"\n最优参数: {best['params']}  (目标={best['objective']:.4f})")
    print()


def _is_float(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    asyncio.run(main())
