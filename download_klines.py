"""
下载近 1 年的 K 线数据到本地（1m / 5m / 15m）

- 不依赖 config，可直接运行
- 数据保存到 data/klines/ 目录，CSV 格式
- Binance API 单次最多 1000 根，脚本内分块请求

使用:
    python download_klines.py
    python download_klines.py --symbol BTCUSDT --days 365 --output-dir data/klines
"""

import asyncio
import argparse
import os
import sys
from datetime import datetime, timedelta

import pandas as pd
from binance import AsyncClient

# Binance 单次请求最多返回 1000 根 K 线
CHUNK_LIMIT = 1000

# 默认列名（与 Binance 返回顺序一致）
KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades", "taker_buy_base",
    "taker_buy_quote", "ignore",
]


async def fetch_klines_chunk(client: AsyncClient, symbol: str, interval: str, start_ts: int, end_ts: int):
    """单次请求一批 K 线（最多 1000 根）"""
    klines = await client.get_klines(
        symbol=symbol,
        interval=interval,
        startTime=start_ts,
        endTime=end_ts,
        limit=CHUNK_LIMIT,
    )
    return klines


async def fetch_klines_full(
    client: AsyncClient,
    symbol: str,
    interval: str,
    start_dt: datetime,
    end_dt: datetime,
) -> pd.DataFrame:
    """分块下载指定时间范围内的全部 K 线"""
    start_ts = int(start_dt.timestamp() * 1000)
    end_ts = int(end_dt.timestamp() * 1000)
    all_klines = []

    while start_ts < end_ts:
        chunk = await fetch_klines_chunk(client, symbol, interval, start_ts, end_ts)
        if not chunk:
            break
        all_klines.extend(chunk)
        # 下一段的起点：最后一根的 close_time + 1ms
        start_ts = chunk[-1][6] + 1
        if len(chunk) < CHUNK_LIMIT:
            break

    if not all_klines:
        return pd.DataFrame()

    df = pd.DataFrame(all_klines, columns=KLINE_COLUMNS)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


def save_klines(df: pd.DataFrame, filepath: str) -> None:
    """保存为 CSV（含 open_time 列，便于后续读取）"""
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    df.to_csv(filepath, index=False)
    print(f"  已保存: {filepath} ({len(df)} 行)")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="下载近 1 年 K 线到本地（1m / 5m / 15m）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--symbol", default="BTCUSDT", help="交易对")
    parser.add_argument("--days", type=int, default=365, help="下载天数")
    parser.add_argument("--output-dir", default="data/klines", help="保存目录")
    parser.add_argument("--intervals", nargs="+", default=["1m", "5m", "15m"], help="K 线周期")
    args = parser.parse_args()

    end_dt = datetime.utcnow()
    start_dt = end_dt - timedelta(days=args.days)

    print("=" * 60)
    print("  K 线数据下载")
    print("=" * 60)
    print(f"  交易对: {args.symbol}")
    print(f"  周期: {args.intervals}")
    print(f"  时间: {start_dt.strftime('%Y-%m-%d')} ~ {end_dt.strftime('%Y-%m-%d')} (UTC)")
    print(f"  保存目录: {args.output_dir}")
    print("=" * 60)

    client = await AsyncClient.create()
    try:
        for interval in args.intervals:
            print(f"\n正在下载 {args.symbol} {interval} ...")
            df = await fetch_klines_full(client, args.symbol, interval, start_dt, end_dt)
            if df.empty:
                print(f"  ⚠️ 未获取到数据: {interval}")
                continue
            # 文件名示例: BTCUSDT_5m_20250101_20260129.csv
            start_str = df["open_time"].min().strftime("%Y%m%d")
            end_str = df["open_time"].max().strftime("%Y%m%d")
            filename = f"{args.symbol}_{interval}_{start_str}_{end_str}.csv"
            filepath = os.path.join(args.output_dir, filename)
            save_klines(df, filepath)
    finally:
        await client.close_connection()

    print("\n" + "=" * 60)
    print("  下载完成")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
