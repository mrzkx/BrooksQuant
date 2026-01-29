"""
高时间框架过滤器 (Higher Time Frame Filter)

职责（关注点分离）：
- 获取 1h EMA20 方向和斜率
- 提供趋势判断（Bullish/Bearish/Neutral）
- 提供硬过滤方法（allows_h2_buy/allows_l2_sell）供 strategy 调用
- 提供软过滤权重（get_signal_modifier）供 strategy 调用

不负责：
- 直接修改信号强度（由 strategy 统一处理）
- 直接阻止信号（由 strategy 决策）

Al Brooks 核心原则：
"背景（Context）胜过一切"
"大周期的趋势是日内交易最好的保护伞"
"没有 100% 的确定性，只有概率和盈亏比"

功能：
1. 获取 1h EMA20 方向作为趋势过滤器
2. 上升趋势：增强买入信号（×1.2），削弱卖出信号（×0.5）
3. 下降趋势：增强卖出信号（×1.2），削弱买入信号（×0.5）
4. 中性趋势：双向交易（×1.0）

H2/L2 硬过滤（Context 优先）：
- 5m 做多（H1/H2）：仅在 1h 强多头且价格回调至 1h EMA20 附近时允许
- 5m 做空（L1/L2）：仅在 1h 强空头且价格反弹至 1h EMA20 附近时允许

软过滤策略（v2.0 优化）：
- 其他信号（Spike/Wedge/Climax/FB）仍用权重调节，不硬禁止
- 通过信号强度 × 权重来自动筛选
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
    高时间框架过滤器（v2.0 软过滤版本）
    
    使用 1h EMA20 方向过滤日内交易信号
    
    Al Brooks 原则：
    - 大周期上涨 → 增强做多信号，削弱做空信号
    - 大周期下跌 → 增强做空信号，削弱做多信号
    - 大周期横盘 → 双向交易
    
    EMA 斜率计算：
    - 比较最近 6 根 1h K 线的 EMA 变化（6 小时）
    - 斜率 > 0.3% → 上升趋势
    - 斜率 < -0.3% → 下降趋势
    - 介于之间 → 中性
    
    信号权重调节：
    - 顺势信号：×1.2（增强）
    - 逆势信号：×0.5（削弱，但不禁止）
    - 中性趋势：×1.0
    """
    
    # EMA 参数
    EMA_PERIOD = 20
    
    # 斜率阈值（%）
    # BTC 1h 周期，0.3% 的 EMA 变化是更稳定的趋势信号
    SLOPE_THRESHOLD_PCT = 0.003  # 0.3%（从 0.1% 提高）
    # 强趋势阈值：H2/L2 硬过滤要求 1h 处于强趋势（Al Brooks 背景优先）
    STRONG_SLOPE_THRESHOLD_PCT = 0.005  # 0.5% 视为强多头/强空头
    
    # 价格“靠近 HTF EMA20”的容差（%）：用于 H2/L2 仅允许在回调至 EMA 附近触发
    PRICE_NEAR_EMA_PCT = 0.008  # 0.8% 内视为靠近 1h EMA20
    
    # 斜率计算使用的 K 线数（6 小时，更能反映趋势）
    SLOPE_LOOKBACK_BARS = 6  # 从 3 根提高到 6 根
    
    # 信号权重因子
    TREND_BOOST_FACTOR = 1.2      # 顺势增强
    COUNTER_TREND_FACTOR = 0.5    # 逆势削弱（不是 0）
    NEUTRAL_FACTOR = 1.0          # 中性
    
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
                col_names = [
                    "timestamp", "open", "high", "low", "close", 
                    "volume", "close_time", "quote_volume", "trades",
                    "taker_buy_base", "taker_buy_quote", "ignore"
                ]
                df = pd.DataFrame(klines, columns=pd.Index(col_names))
                df["close"] = df["close"].astype(float)
                df["high"] = df["high"].astype(float)
                df["low"] = df["low"].astype(float)
                df["open"] = df["open"].astype(float)
                
                # 计算 EMA (使用 TA-Lib)
                close_series: pd.Series = df["close"]  # type: ignore[assignment]
                df["ema"] = compute_ema(close_series, self.ema_period)
                
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
        判断是否允许该方向的信号（v2.0 软过滤）
        
        软过滤策略：总是允许交易，但通过权重调节信号强度
        - 逆势信号不再被完全禁止
        - 由信号强度 × 权重来决定是否达到入场阈值
        
        Args:
            side: 交易方向 ("buy" 或 "sell")
        
        Returns:
            (is_allowed, reason): 总是返回 True，reason 说明权重调节
        """
        if self._snapshot is None:
            return (True, "HTF 数据未初始化，权重=1.0")
        
        trend = self._snapshot.trend
        modifier = self.get_signal_modifier(side)
        
        if side == "buy":
            if trend == HTFTrend.BEARISH:
                return (True, f"HTF({self.htf_interval}) 下降趋势，买入权重×{modifier}（逆势削弱）")
            elif trend == HTFTrend.BULLISH:
                return (True, f"HTF({self.htf_interval}) 上升趋势，买入权重×{modifier}（顺势增强）")
            else:
                return (True, f"HTF({self.htf_interval}) 中性趋势，买入权重×{modifier}")
        
        else:  # side == "sell"
            if trend == HTFTrend.BULLISH:
                return (True, f"HTF({self.htf_interval}) 上升趋势，卖出权重×{modifier}（逆势削弱）")
            elif trend == HTFTrend.BEARISH:
                return (True, f"HTF({self.htf_interval}) 下降趋势，卖出权重×{modifier}（顺势增强）")
            else:
                return (True, f"HTF({self.htf_interval}) 中性趋势，卖出权重×{modifier}")
    
    def get_signal_modifier(self, side: str) -> float:
        """
        获取 HTF 信号调节因子（v2.0 软过滤）
        
        Args:
            side: 交易方向 ("buy" 或 "sell")
        
        Returns:
            float: 调节因子
            - 1.2: 趋势方向一致（顺势增强）
            - 1.0: 中性趋势
            - 0.5: 趋势方向相反（逆势削弱，但不禁止）
        """
        if self._snapshot is None:
            return self.NEUTRAL_FACTOR
        
        trend = self._snapshot.trend
        
        if side == "buy":
            if trend == HTFTrend.BULLISH:
                return self.TREND_BOOST_FACTOR      # 1.2 顺势增强
            elif trend == HTFTrend.BEARISH:
                return self.COUNTER_TREND_FACTOR    # 0.5 逆势削弱（不是 0）
            else:
                return self.NEUTRAL_FACTOR          # 1.0 中性
        else:
            if trend == HTFTrend.BEARISH:
                return self.TREND_BOOST_FACTOR      # 1.2 顺势增强
            elif trend == HTFTrend.BULLISH:
                return self.COUNTER_TREND_FACTOR    # 0.5 逆势削弱（不是 0）
            else:
                return self.NEUTRAL_FACTOR          # 1.0 中性

    def is_price_near_htf_ema(
        self, current_price: float, tolerance_pct: Optional[float] = None
    ) -> bool:
        """
        当前价格是否在 HTF EMA20 附近（用于 H2/L2 背景过滤）
        
        Al Brooks：只有在价格回调至大周期 EMA 附近时，才做 H2/L2 顺势单。
        
        Args:
            current_price: 当前 K 线价格（如 5m 收盘价）
            tolerance_pct: 容差百分比，默认使用 PRICE_NEAR_EMA_PCT
        
        Returns:
            True 表示在 EMA 附近（|price - ema| / ema <= tolerance）
        """
        if self._snapshot is None or current_price <= 0:
            return False
        tol = tolerance_pct if tolerance_pct is not None else self.PRICE_NEAR_EMA_PCT
        ema = self._snapshot.ema_value
        if ema <= 0:
            return False
        pct = abs(current_price - ema) / ema
        return pct <= tol

    def allows_h2_buy(self, current_price: float) -> tuple[bool, str]:
        """
        是否允许 5m 级别的 H1/H2 买入（Al Brooks 背景优先）
        
        条件：1h 处于强多头趋势 且 当前价格回调至 1h EMA20 附近。
        
        Args:
            current_price: 当前 K 线价格（如 5m 收盘价）
        
        Returns:
            (allowed, reason)
        """
        if self._snapshot is None:
            return (False, "HTF 数据未就绪，禁止 H2 买入")
        s = self._snapshot
        strong_bull = (
            s.trend == HTFTrend.BULLISH
            and s.ema_slope >= self.STRONG_SLOPE_THRESHOLD_PCT
        )
        if not strong_bull:
            return (
                False,
                f"HTF({self.htf_interval}) 非强多头(斜率={s.ema_slope:.3%}<{self.STRONG_SLOPE_THRESHOLD_PCT:.2%})，禁止 H2 买入",
            )
        if not self.is_price_near_htf_ema(current_price):
            return (
                False,
                f"价格{current_price:.2f} 未回调至 1h EMA20({s.ema_value:.2f}) 附近(>{self.PRICE_NEAR_EMA_PCT:.2%})，禁止 H2 买入",
            )
        return (True, f"HTF 强多头且价格近 EMA，允许 H2 买入")

    def allows_l2_sell(self, current_price: float) -> tuple[bool, str]:
        """
        是否允许 5m 级别的 L1/L2 卖出（Al Brooks 背景优先）
        
        条件：1h 处于强空头趋势 且 当前价格反弹至 1h EMA20 附近。
        
        Args:
            current_price: 当前 K 线价格（如 5m 收盘价）
        
        Returns:
            (allowed, reason)
        """
        if self._snapshot is None:
            return (False, "HTF 数据未就绪，禁止 L2 卖出")
        s = self._snapshot
        strong_bear = (
            s.trend == HTFTrend.BEARISH
            and s.ema_slope <= -self.STRONG_SLOPE_THRESHOLD_PCT
        )
        if not strong_bear:
            return (
                False,
                f"HTF({self.htf_interval}) 非强空头(斜率={s.ema_slope:.3%}>-{self.STRONG_SLOPE_THRESHOLD_PCT:.2%})，禁止 L2 卖出",
            )
        if not self.is_price_near_htf_ema(current_price):
            return (
                False,
                f"价格{current_price:.2f} 未反弹至 1h EMA20({s.ema_value:.2f}) 附近(>{self.PRICE_NEAR_EMA_PCT:.2%})，禁止 L2 卖出",
            )
        return (True, "HTF 强空头且价格近 EMA，允许 L2 卖出")


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
