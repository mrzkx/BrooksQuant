"""
工作者辅助函数

包含K线数据加载、补全等辅助功能
"""

import asyncio
import logging
from typing import Dict, List, Optional

from binance import AsyncClient

from config import (
    OBSERVE_BALANCE,
    POSITION_SIZE_PERCENT,
    LEVERAGE,
    SYMBOL as CONFIG_SYMBOL,
    KLINE_INTERVAL,
)

# 交易参数
SYMBOL = CONFIG_SYMBOL
INTERVAL = AsyncClient.KLINE_INTERVAL_5MINUTE

# K线周期对应的毫秒数
KLINE_INTERVAL_MS = {
    "1m": 60 * 1000,
    "3m": 3 * 60 * 1000,
    "5m": 5 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "30m": 30 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "2h": 2 * 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}


def calculate_order_quantity(current_price: float) -> float:
    """
    计算下单数量（仅用于观察模式）
    
    ⚠️ 注意：此函数仅用于观察模式，使用配置文件中的 OBSERVE_BALANCE
    实盘模式下使用 TradingUser.calculate_order_quantity()
    
    公式: 下单数量 = (总资金 × 仓位百分比 × 杠杆) / 当前价格
    
    返回: BTC 数量（保留3位小数）
    """
    if current_price <= 0:
        return 0.001  # 默认最小值
    
    # 开仓金额 = 总资金 × 仓位百分比
    position_value = OBSERVE_BALANCE * (POSITION_SIZE_PERCENT / 100)
    
    # 实际购买力 = 开仓金额 × 杠杆
    buying_power = position_value * LEVERAGE
    
    # 下单数量 = 购买力 / 当前价格
    quantity = buying_power / current_price
    
    # 保留3位小数（Binance BTC 最小精度）
    quantity = round(quantity, 3)
    
    # 确保不低于最小交易量
    return max(quantity, 0.001)


async def load_historical_klines(
    client: AsyncClient, history: List[Dict], limit: int = 200
) -> Optional[int]:
    """
    加载历史K线数据到history列表
    
    返回: 最后一根K线的开盘时间戳（毫秒），用于后续补全
    """
    last_timestamp = None
    try:
        logging.info(f"正在下载历史K线数据（{SYMBOL} {INTERVAL}，{limit}根）...")
        historical_klines = await client.get_historical_klines(
            symbol=SYMBOL,
            interval=INTERVAL,
            limit=limit,
        )
        logging.info(f"成功下载 {len(historical_klines)} 根历史K线数据")

        # 清空并重新填充历史数据
        history.clear()
        for kline in historical_klines:
            history.append(
                {
                    "timestamp": int(kline[0]),
                    "open": float(kline[1]),
                    "high": float(kline[2]),
                    "low": float(kline[3]),
                    "close": float(kline[4]),
                }
            )
        
        if history:
            last_timestamp = history[-1]["timestamp"]
            
        logging.info(f"历史数据已加载到内存，共 {len(history)} 根K线")
    except Exception as e:
        logging.error(f"下载历史K线数据失败: {e}", exc_info=True)
        if len(history) == 0:
            logging.warning("历史数据为空，需要等待K线数据积累")
    
    return last_timestamp


async def fill_missing_klines(
    client: AsyncClient, history: List[Dict], last_timestamp: Optional[int] = None
) -> Optional[int]:
    """
    补全缺失的K线数据（重连后使用）
    
    基于时间戳精确补全，避免重复或遗漏
    
    返回: 补全后最后一根K线的时间戳
    """
    import time
    
    try:
        if len(history) == 0:
            return await load_historical_klines(client, history)

        interval_ms = KLINE_INTERVAL_MS.get(KLINE_INTERVAL, 5 * 60 * 1000)
        
        if last_timestamp is None:
            last_timestamp = history[-1].get("timestamp")
        
        if last_timestamp is None:
            logging.warning("历史数据无时间戳，使用简单补全模式")
            limit = min(100, 500 - len(history))
            missing_klines = await client.get_historical_klines(
                symbol=SYMBOL,
                interval=INTERVAL,
                limit=limit,
            )
        else:
            current_time_ms = int(time.time() * 1000)
            time_gap_ms = current_time_ms - last_timestamp
            missing_count = time_gap_ms // interval_ms
            
            if missing_count <= 0:
                logging.info("没有缺失的K线数据")
                return last_timestamp
            
            missing_count = min(missing_count + 1, 200)
            
            logging.info(
                f"正在补全缺失的K线数据（从 {last_timestamp} 开始，预计 {missing_count} 根）..."
            )
            
            missing_klines = await client.get_historical_klines(
                symbol=SYMBOL,
                interval=INTERVAL,
                start_str=str(last_timestamp),
                limit=missing_count,
            )

        if not missing_klines:
            logging.info("没有新的K线数据需要补全")
            return last_timestamp

        existing_timestamps = {kline.get("timestamp") for kline in history if kline.get("timestamp")}
        
        new_klines = []
        for kline in missing_klines:
            kline_timestamp = int(kline[0])
            
            if kline_timestamp in existing_timestamps:
                continue
            
            kline_data = {
                "timestamp": kline_timestamp,
                "open": float(kline[1]),
                "high": float(kline[2]),
                "low": float(kline[3]),
                "close": float(kline[4]),
            }
            new_klines.append(kline_data)
            existing_timestamps.add(kline_timestamp)

        if new_klines:
            history.extend(new_klines)
            history.sort(key=lambda x: x.get("timestamp", 0))
            while len(history) > 500:
                history.pop(0)
            
            new_last_timestamp = history[-1].get("timestamp") if history else None
            logging.info(
                f"✅ 已补全 {len(new_klines)} 根K线，当前历史数据: {len(history)} 根"
            )
            return new_last_timestamp
        else:
            logging.info("所有K线数据已是最新")
            return history[-1].get("timestamp") if history else None
            
    except Exception as e:
        logging.error(f"补全K线数据失败: {e}", exc_info=True)
        return last_timestamp
