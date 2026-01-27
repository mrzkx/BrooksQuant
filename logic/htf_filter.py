"""
高时间框架过滤器 (Higher Time Frame Filter)

Al Brooks 核心原则：
"大周期的趋势是日内交易最好的保护伞"

功能：
1. 获取 1h EMA20 方向作为趋势过滤器
2. 上升趋势：只允许买入信号（H1/H2），屏蔽卖出信号（L1/L2）
3. 下降趋势：只允许卖出信号（L1/L2），屏蔽买入信号（H1/H2）
4. 中性趋势：允许双向交易

多周期共振：
- 当 1h 和 5m 趋势方向一致时，信号质量最高
- 当方向相反时，禁止交易
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, List, Dict

import pandas as pd
from binance import AsyncClient

from logic.talib_indicators import compute_ema


class HTFTrend(Enum):
    """高时间框架趋势方向"""
    BULLISH = "bullish"       # 上升趋势：EMA 向上
    BEARISH = "bearish"       # 下降趋势：EMA 向下
    NEUTRAL = "neutral"       # 中性：EMA 横盘


@dataclass
class HTFSnapshot:
    """高时间框架快照数据"""
    trend: HTFTrend                 # 趋势方向
    ema_value: float               # 当前 EMA 值
    ema_slope: float               # EMA 斜率（%）
    ema_slope_bars: int            # 计算斜率使用的 K 线数
    last_close: float              # 最新收盘价
    price_vs_ema: str              # 价格相对 EMA 位置 ("above" / "below" / "at")
    timestamp: int                 # 更新时间戳（毫秒）
    interval: str                  # 时间框架（如 "1h"）
    
    @property
    def allow_buy(self) -> bool:
        """是否允许买入信号"""
        return self.trend != HTFTrend.BEARISH
    
    @property
    def allow_sell(self) -> bool:
        """是否允许卖出信号"""
        return self.trend != HTFTrend.BULLISH


class HTFFilter:
    """
    高时间框架过滤器
    
    使用 1h EMA20 方向过滤日内交易信号
    
    Al Brooks 原则：
    - 大周期上涨 → 只做多，不做空
    - 大周期下跌 → 只做空，不做多
    - 大周期横盘 → 双向交易，但要谨慎
    
    EMA 斜率计算：
    - 比较最近 3 根 1h K 线的 EMA 变化
    - 斜率 > 0.1% → 上升趋势
    - 斜率 < -0.1% → 下降趋势
    - 介于之间 → 中性
    """
    
    # EMA 参数
    EMA_PERIOD = 20
    
    # 斜率阈值（%）
    # BTC 1h 周期，0.1% 的 EMA 变化已经是明显的趋势
    SLOPE_THRESHOLD_PCT = 0.001  # 0.1%
    
    # 斜率计算使用的 K 线数
    SLOPE_LOOKBACK_BARS = 3
    
    # 更新间隔（秒）- 每 5 分钟更新一次
    UPDATE_INTERVAL_SECONDS = 300
    
    def __init__(self, htf_interval: str = "1h", ema_period: int = 20):
        """
        初始化高时间框架过滤器
        
        Args:
            htf_interval: 高时间框架周期（默认 "1h"）
            ema_period: EMA 周期（默认 20）
        """
        self.htf_interval = htf_interval
        self.ema_period = ema_period
        
        # 缓存的快照
        self._snapshot: Optional[HTFSnapshot] = None
        self._last_update_time: float = 0
        
        # 历史 K 线数据
        self._klines: List[Dict] = []
        
        # 锁
        self._lock = asyncio.Lock()
        
        logging.info(
            f"📈 HTF 过滤器初始化: 周期={htf_interval}, EMA={ema_period}, "
            f"斜率阈值={self.SLOPE_THRESHOLD_PCT:.2%}"
        )
    
    async def update(self, client: AsyncClient, symbol: str = "BTCUSDT") -> Optional[HTFSnapshot]:
        """
        更新高时间框架数据
        
        Args:
            client: Binance 异步客户端
            symbol: 交易对符号
        
        Returns:
            HTFSnapshot: 更新后的快照
        """
        async with self._lock:
            current_time = time.time()
            
            # 检查是否需要更新（每 5 分钟更新一次）
            if (self._snapshot is not None and 
                current_time - self._last_update_time < self.UPDATE_INTERVAL_SECONDS):
                return self._snapshot
            
            try:
                # 获取 1h K 线数据（需要 EMA 周期 + 斜率回看 + 缓冲）
                limit = self.ema_period + self.SLOPE_LOOKBACK_BARS + 5
                
                klines = await client.get_klines(
                    symbol=symbol,
                    interval=self.htf_interval,
                    limit=limit
                )
                
                if not klines or len(klines) < self.ema_period:
                    logging.warning(f"⚠️ HTF K 线数据不足: 获取到 {len(klines) if klines else 0} 根")
                    return self._snapshot
                
                # 转换为 DataFrame
                df = pd.DataFrame(klines, columns=[
                    "timestamp", "open", "high", "low", "close", 
                    "volume", "close_time", "quote_volume", "trades",
                    "taker_buy_base", "taker_buy_quote", "ignore"
                ])
                df["close"] = df["close"].astype(float)
                df["high"] = df["high"].astype(float)
                df["low"] = df["low"].astype(float)
                df["open"] = df["open"].astype(float)
                
                # 计算 EMA (使用 TA-Lib)
                df["ema"] = compute_ema(df["close"], self.ema_period)
                
                # 获取最新数据
                last_row = df.iloc[-1]
                last_close = last_row["close"]
                last_ema = last_row["ema"]
                
                # 计算 EMA 斜率
                ema_values = df["ema"].tail(self.SLOPE_LOOKBACK_BARS + 1).values
                if len(ema_values) >= 2:
                    ema_start = ema_values[0]
                    ema_end = ema_values[-1]
                    ema_slope = (ema_end - ema_start) / ema_start if ema_start > 0 else 0
                else:
                    ema_slope = 0
                
                # 判断趋势方向
                if ema_slope > self.SLOPE_THRESHOLD_PCT:
                    trend = HTFTrend.BULLISH
                elif ema_slope < -self.SLOPE_THRESHOLD_PCT:
                    trend = HTFTrend.BEARISH
                else:
                    trend = HTFTrend.NEUTRAL
                
                # 价格相对 EMA 位置
                if last_close > last_ema * 1.001:
                    price_vs_ema = "above"
                elif last_close < last_ema * 0.999:
                    price_vs_ema = "below"
                else:
                    price_vs_ema = "at"
                
                # 创建快照
                self._snapshot = HTFSnapshot(
                    trend=trend,
                    ema_value=last_ema,
                    ema_slope=ema_slope,
                    ema_slope_bars=self.SLOPE_LOOKBACK_BARS,
                    last_close=last_close,
                    price_vs_ema=price_vs_ema,
                    timestamp=int(time.time() * 1000),
                    interval=self.htf_interval,
                )
                
                self._last_update_time = current_time
                
                # 日志
                trend_emoji = {
                    HTFTrend.BULLISH: "🟢",
                    HTFTrend.BEARISH: "🔴",
                    HTFTrend.NEUTRAL: "⚪",
                }
                logging.info(
                    f"{trend_emoji.get(trend, '⚪')} HTF({self.htf_interval}) 更新: "
                    f"趋势={trend.value}, EMA={last_ema:.2f}, 斜率={ema_slope:.3%}, "
                    f"价格={last_close:.2f} ({price_vs_ema} EMA)"
                )
                
                return self._snapshot
                
            except Exception as e:
                logging.error(f"❌ HTF 数据更新失败: {e}", exc_info=True)
                return self._snapshot
    
    def get_snapshot(self) -> Optional[HTFSnapshot]:
        """
        获取缓存的高时间框架快照
        
        Returns:
            HTFSnapshot: 最新的快照，如果没有则返回 None
        """
        return self._snapshot
    
    def should_allow_signal(self, side: str) -> tuple[bool, str]:
        """
        判断是否允许该方向的信号
        
        Args:
            side: 交易方向 ("buy" 或 "sell")
        
        Returns:
            (is_allowed, reason): 是否允许及原因
        """
        if self._snapshot is None:
            return (True, "HTF 数据未初始化，允许交易")
        
        trend = self._snapshot.trend
        
        if side == "buy":
            if trend == HTFTrend.BEARISH:
                return (False, f"HTF({self.htf_interval}) 下降趋势，禁止买入")
            elif trend == HTFTrend.BULLISH:
                return (True, f"HTF({self.htf_interval}) 上升趋势，买入信号增强")
            else:
                return (True, f"HTF({self.htf_interval}) 中性趋势，允许买入")
        
        else:  # side == "sell"
            if trend == HTFTrend.BULLISH:
                return (False, f"HTF({self.htf_interval}) 上升趋势，禁止卖出")
            elif trend == HTFTrend.BEARISH:
                return (True, f"HTF({self.htf_interval}) 下降趋势，卖出信号增强")
            else:
                return (True, f"HTF({self.htf_interval}) 中性趋势，允许卖出")
    
    def get_signal_modifier(self, side: str) -> float:
        """
        获取 HTF 信号调节因子
        
        Args:
            side: 交易方向 ("buy" 或 "sell")
        
        Returns:
            float: 调节因子
            - 1.2: 趋势方向一致（增强）
            - 1.0: 中性
            - 0.0: 趋势方向相反（禁止）
        """
        if self._snapshot is None:
            return 1.0
        
        trend = self._snapshot.trend
        
        if side == "buy":
            if trend == HTFTrend.BULLISH:
                return 1.2  # 增强
            elif trend == HTFTrend.BEARISH:
                return 0.0  # 禁止
            else:
                return 1.0  # 中性
        else:
            if trend == HTFTrend.BEARISH:
                return 1.2  # 增强
            elif trend == HTFTrend.BULLISH:
                return 0.0  # 禁止
            else:
                return 1.0  # 中性


# ============================================================================
# 全局 HTF 过滤器实例
# ============================================================================

_htf_filter: Optional[HTFFilter] = None


def get_htf_filter(htf_interval: str = "1h", ema_period: int = 20) -> HTFFilter:
    """
    获取全局 HTF 过滤器实例
    
    Args:
        htf_interval: 高时间框架周期（默认 "1h"）
        ema_period: EMA 周期（默认 20）
    
    Returns:
        HTFFilter: 全局单例实例
    """
    global _htf_filter
    
    if _htf_filter is None:
        _htf_filter = HTFFilter(htf_interval=htf_interval, ema_period=ema_period)
    
    return _htf_filter


async def htf_updater_worker(
    client: AsyncClient,
    symbol: str = "BTCUSDT",
    update_interval: int = 300
) -> None:
    """
    HTF 数据更新工作线程
    
    定期更新高时间框架数据（每 5 分钟）
    
    Args:
        client: Binance 异步客户端
        symbol: 交易对符号
        update_interval: 更新间隔（秒）
    """
    htf_filter = get_htf_filter()
    
    logging.info(f"🔄 HTF 更新器已启动: 更新间隔={update_interval}秒")
    
    while True:
        try:
            await htf_filter.update(client, symbol)
            await asyncio.sleep(update_interval)
        except asyncio.CancelledError:
            logging.info("HTF 更新器任务已取消")
            break
        except Exception as e:
            logging.error(f"HTF 更新器错误: {e}", exc_info=True)
            await asyncio.sleep(60)  # 出错后等待 1 分钟重试
