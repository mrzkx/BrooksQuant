"""
动态订单流 Delta 分析模块

基于 Binance aggTrade 流的实时订单流分析：
1. 主动买入 (Aggressive Buying): 买方吃进卖单，Delta 为正
2. 主动卖出 (Aggressive Selling): 卖方吃进买单，Delta 为负
3. 流动性撤离 (Liquidity Withdrawal): 价格变化但 Delta 不匹配
4. 吸收 (Absorption): Delta 很大但价格不动

核心概念：
- 价格波动的本质是主动方压倒被动方
- 上涨：主动买入 (Market Buy) 吃光卖方挂单 (Limit Sell)
- 下跌：主动卖出 (Market Sell) 吃光买方挂单 (Limit Buy)

Binance aggTrade 字段：
- p: 成交价格
- q: 成交数量
- m: 是否为买方做市商 (true=卖方主动, false=买方主动)
- T: 成交时间戳
"""

import asyncio
import logging
import json
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional, Tuple, List
from enum import Enum

import numpy as np
import redis.asyncio as aioredis
from binance import AsyncClient, BinanceSocketManager
from binance.exceptions import ReadLoopClosed

# 注意：BinanceSocketManager 的队列大小必须在构造函数中通过 max_queue_size 参数设置
# 类属性 QUEUE_SIZE 在新版本中无效，需要在创建实例时传入 max_queue_size=10000

# 尝试导入 websockets 异常
try:
    from websockets.exceptions import ConnectionClosed
except ImportError:
    ConnectionClosed = Exception  # type: ignore


class DeltaTrend(Enum):
    """Delta 趋势方向"""
    STRONG_BULLISH = "strong_bullish"   # 强烈买盘主导
    BULLISH = "bullish"                 # 买盘占优
    NEUTRAL = "neutral"                 # 中性
    BEARISH = "bearish"                 # 卖盘占优
    STRONG_BEARISH = "strong_bearish"   # 强烈卖盘主导


@dataclass
class DeltaSnapshot:
    """Delta 快照数据"""
    # 基础 Delta 数据
    cumulative_delta: float = 0.0       # 累计 Delta（买-卖）
    buy_volume: float = 0.0             # 买方主动成交量
    sell_volume: float = 0.0            # 卖方主动成交量
    
    # 派生指标
    delta_ratio: float = 0.0            # Delta 比率 (-1 到 1)
    delta_avg: float = 0.0              # 滑动平均 Delta
    delta_acceleration: float = 0.0     # Delta 加速度（变化率）
    delta_trend: DeltaTrend = DeltaTrend.NEUTRAL  # 趋势方向
    
    # 异常检测
    is_climax_buy: bool = False         # 买入高潮（大量买入但价格不涨）
    is_climax_sell: bool = False        # 卖出高潮（大量卖出但价格不跌）
    is_absorption: bool = False         # 吸收信号（大量成交无价格变化）
    
    # 元数据
    timestamp: int = 0                  # 毫秒时间戳
    trade_count: int = 0                # 统计的交易笔数
    window_seconds: int = 60            # 统计窗口（秒）


class DeltaAnalyzer:
    """
    动态订单流 Delta 分析器（性能优化版）
    
    核心功能：
    1. 实时计算买卖 Delta（主动买入 - 主动卖出）
    2. 检测异常模式（Climax、Absorption、Liquidity Withdrawal）
    3. 生成交易信号调节因子
    
    窗口设计（与 K 线周期对齐）：
    - 主窗口：与 K 线周期相同（5分钟 K 线 = 300秒窗口）
    - 短窗口：主窗口的 1/5（用于计算加速度和短期趋势）
    
    性能优化：
    1. deque 大小动态计算，基于预估 TPS 而非固定值
    2. 增量式 Delta 计算，避免每次全量遍历
    3. 分层异常检测，区分吸收、流动性撤离、Climax
    4. 批量清理过期数据，减少 popleft 调用次数
    
    内存估算（5分钟窗口）：
    - 正常市场：~1000 TPS -> 300秒 x 1000 = 300,000 条 ≈ 12 MB
    - 高波动：~5000 TPS -> 300秒 x 5000 = 1,500,000 条 ≈ 60 MB
    - 极端情况：deque maxlen 限制在 200 万条 ≈ 80 MB
    """
    
    # K线周期到秒数的映射
    KLINE_INTERVAL_SECONDS = {
        "1m": 60,
        "3m": 180,
        "5m": 300,
        "15m": 900,
        "30m": 1800,
        "1h": 3600,
        "2h": 7200,
        "4h": 14400,
        "1d": 86400,
    }
    
    # TPS 估算（用于计算 deque 大小）
    NORMAL_TPS = 1000       # 正常市场 TPS
    PEAK_TPS = 5000         # 高波动 TPS
    EXTREME_TPS = 10000     # 极端情况 TPS
    
    # 阈值参数（优化后的检测阈值）
    STRONG_DELTA_THRESHOLD = 0.5    # 强 Delta 阈值
    
    # 吸收检测参数（优化：更精细的分层）
    ABSORPTION_PRICE_THRESHOLD = 0.05   # 价格变化阈值（%）
    ABSORPTION_DELTA_THRESHOLD = 0.3    # Delta 阈值
    ABSORPTION_VOLUME_THRESHOLD = 1.5   # 成交量倍数（相对平均）
    
    # 流动性撤离检测参数（新增）
    WITHDRAWAL_PRICE_THRESHOLD = 0.2    # 价格变化阈值（%）
    WITHDRAWAL_DELTA_MISMATCH = 0.15    # Delta 不匹配阈值
    
    # 清理批次大小
    CLEANUP_BATCH_SIZE = 1000
    
    def __init__(self, kline_interval: str = "5m"):
        """
        初始化 Delta 分析器
        
        Args:
            kline_interval: K 线周期（如 "1m", "5m", "15m", "1h"）
        """
        # 根据 K 线周期设置窗口大小
        self.WINDOW_SECONDS = self.KLINE_INTERVAL_SECONDS.get(
            kline_interval, 300
        )
        self.SHORT_WINDOW_SECONDS = max(self.WINDOW_SECONDS // 5, 30)
        self._kline_interval = kline_interval
        
        # 动态计算 deque 大小（基于窗口和预估 TPS）
        # 问题9修复：增加上限以应对极端市场条件
        # 极端情况（10,000 TPS × 300秒 = 300万条）
        self.MAX_TRADES = min(
            self.WINDOW_SECONDS * self.EXTREME_TPS,  # 基于极端 TPS
            3_500_000  # 硬上限：350 万条 ≈ 140 MB，确保5分钟窗口不溢出
        )
        
        logging.info(
            f"Delta 分析器初始化: K线周期={kline_interval}, "
            f"主窗口={self.WINDOW_SECONDS}秒, 短窗口={self.SHORT_WINDOW_SECONDS}秒, "
            f"deque上限={self.MAX_TRADES:,}条"
        )
        
        # 交易记录队列：(timestamp_ms, price, qty, is_buyer_maker)
        self._trades: deque = deque(maxlen=self.MAX_TRADES)
        
        # ========== 增量式计算缓存 ==========
        # 避免每次 get_snapshot 都全量遍历
        self._incremental_buy_volume: float = 0.0
        self._incremental_sell_volume: float = 0.0
        self._last_cleanup_ts: int = 0
        self._trades_since_cleanup: int = 0
        
        # 价格追踪
        self._last_price: float = 0.0
        self._first_price_in_window: float = 0.0
        
        # 历史 Delta 记录（用于计算趋势和加速度）
        history_size = max(30, 300 // max(self.WINDOW_SECONDS // 60, 1))
        self._delta_history: deque = deque(maxlen=history_size)
        
        # 滑动平均成交量（用于异常检测基准）
        self._avg_volume_per_window: float = 0.0
        self._volume_samples: deque = deque(maxlen=20)  # 最近 20 个窗口的成交量
        
        # 锁
        self._lock = asyncio.Lock()
    
    async def add_trade(self, price: float, qty: float, is_buyer_maker: bool, timestamp_ms: int):
        """
        添加一笔成交记录（增量式更新）
        
        优化点：
        - 增量更新 buy/sell volume，避免每次全量遍历
        - 批量清理过期数据，减少 popleft 调用
        """
        async with self._lock:
            self._trades.append((timestamp_ms, price, qty, is_buyer_maker))
            self._last_price = price
            
            # 增量更新 volume
            if is_buyer_maker:
                self._incremental_sell_volume += qty
            else:
                self._incremental_buy_volume += qty
            
            self._trades_since_cleanup += 1
            
            # 批量清理（每 CLEANUP_BATCH_SIZE 条或时间间隔）
            if self._trades_since_cleanup >= self.CLEANUP_BATCH_SIZE:
                await self._batch_cleanup(timestamp_ms)
    
    async def add_trades_batch(self, trades: List[Tuple[int, float, float, bool]]):
        """
        批量添加交易记录（高性能版本）
        
        Args:
            trades: [(timestamp_ms, price, qty, is_buyer_maker), ...]
        
        优化点：
        - 一次性获取锁，减少锁竞争
        - 批量更新 volume
        - NumPy 向量化计算
        """
        if not trades:
            return
        
        async with self._lock:
            # 转换为 numpy 数组加速计算
            trades_arr = np.array(trades, dtype=[
                ('ts', np.int64), ('price', np.float64), 
                ('qty', np.float64), ('is_buyer_maker', np.bool_)
            ])
            
            # 批量添加到 deque
            for trade in trades:
                self._trades.append(trade)
            
            # 批量更新 volume（向量化）
            buyer_maker_mask = trades_arr['is_buyer_maker']
            self._incremental_sell_volume += float(np.sum(trades_arr['qty'][buyer_maker_mask]))
            self._incremental_buy_volume += float(np.sum(trades_arr['qty'][~buyer_maker_mask]))
            
            # 更新最新价格
            self._last_price = float(trades_arr['price'][-1])
            
            self._trades_since_cleanup += len(trades)
            
            # 批量清理
            if self._trades_since_cleanup >= self.CLEANUP_BATCH_SIZE:
                await self._batch_cleanup(int(trades_arr['ts'][-1]))
    
    async def _batch_cleanup(self, current_ts_ms: int):
        """
        批量清理过期数据（优化版）
        
        优化点：
        - 一次性清理所有过期数据
        - 同步更新增量 volume
        """
        cutoff = current_ts_ms - (self.WINDOW_SECONDS * 1000)
        
        # 记录清理前的 volume
        removed_buy = 0.0
        removed_sell = 0.0
        
        # 批量移除过期数据
        while self._trades and self._trades[0][0] < cutoff:
            ts, price, qty, is_buyer_maker = self._trades.popleft()
            if is_buyer_maker:
                removed_sell += qty
            else:
                removed_buy += qty
        
        # 更新增量 volume
        self._incremental_buy_volume -= removed_buy
        self._incremental_sell_volume -= removed_sell
        
        # 确保不会出现负数（浮点精度问题）
        self._incremental_buy_volume = max(0.0, self._incremental_buy_volume)
        self._incremental_sell_volume = max(0.0, self._incremental_sell_volume)
        
        self._last_cleanup_ts = current_ts_ms
        self._trades_since_cleanup = 0
    
    async def get_snapshot(self, symbol: str = "BTCUSDT") -> DeltaSnapshot:
        """
        获取当前 Delta 快照（优化版）
        
        优化点：
        - 使用增量 volume，避免全量遍历计算总量
        - 只遍历短窗口数据计算短期指标
        - 缓存历史平均值
        """
        async with self._lock:
            current_ts = int(time.time() * 1000)
            
            # 批量清理
            await self._batch_cleanup(current_ts)
            
            if not self._trades:
                return DeltaSnapshot(timestamp=current_ts, window_seconds=self.WINDOW_SECONDS)
            
            # 使用增量 volume（O(1) 而非 O(n)）
            buy_volume = self._incremental_buy_volume
            sell_volume = self._incremental_sell_volume
            
            # 只遍历短窗口计算短期指标
            short_cutoff = current_ts - (self.SHORT_WINDOW_SECONDS * 1000)
            short_buy_volume = 0.0
            short_sell_volume = 0.0
            
            # 从尾部开始遍历（短窗口数据在尾部）
            first_price = None
            last_price = None
            trade_count = 0
            
            for ts, price, qty, is_buyer_maker in reversed(self._trades):
                if ts < short_cutoff:
                    # 记录窗口起始价格
                    if first_price is None:
                        first_price = price
                    break
                
                if last_price is None:
                    last_price = price
                first_price = price
                trade_count += 1
                
                if is_buyer_maker:
                    short_sell_volume += qty
                else:
                    short_buy_volume += qty
            
            # 如果没有遍历到窗口起始，使用第一条记录
            if first_price is None and self._trades:
                first_price = self._trades[0][1]
            if last_price is None and self._trades:
                last_price = self._trades[-1][1]
            
            # 计算 Delta 指标
            cumulative_delta = buy_volume - sell_volume
            short_delta = short_buy_volume - short_sell_volume
            total_volume = buy_volume + sell_volume
            
            delta_ratio = cumulative_delta / total_volume if total_volume > 0 else 0.0
            
            # 计算滑动平均和加速度（优化：使用缓存）
            delta_avg, delta_acceleration = self._calculate_trend_metrics(cumulative_delta)
            
            # 确定趋势方向（优化：加入短期 Delta 权重）
            delta_trend = self._determine_trend_enhanced(
                delta_ratio, delta_acceleration, short_delta, total_volume
            )
            
            # 价格变化
            price_change_pct = 0.0
            if first_price and last_price and first_price > 0:
                price_change_pct = ((last_price - first_price) / first_price) * 100
            
            # 更新平均成交量基准
            self._volume_samples.append(total_volume)
            self._avg_volume_per_window = (
                sum(self._volume_samples) / len(self._volume_samples)
                if self._volume_samples else total_volume
            )
            
            # 异常检测（优化：分层检测）
            is_climax_buy, is_climax_sell, is_absorption, is_withdrawal = self._detect_anomalies_enhanced(
                buy_volume, sell_volume, price_change_pct, total_volume, delta_ratio
            )
            
            snapshot = DeltaSnapshot(
                cumulative_delta=cumulative_delta,
                buy_volume=buy_volume,
                sell_volume=sell_volume,
                delta_ratio=delta_ratio,
                delta_avg=delta_avg,
                delta_acceleration=delta_acceleration,
                delta_trend=delta_trend,
                is_climax_buy=is_climax_buy,
                is_climax_sell=is_climax_sell,
                is_absorption=is_absorption,
                timestamp=current_ts,
                trade_count=len(self._trades),
                window_seconds=self.WINDOW_SECONDS,
            )
            
            self._delta_history.append(snapshot)
            return snapshot
    
    def _calculate_trend_metrics(self, current_delta: float) -> Tuple[float, float]:
        """
        计算趋势指标（优化：使用缓存避免重复计算）
        
        Returns:
            (delta_avg, delta_acceleration)
        """
        if not self._delta_history:
            return (current_delta, 0.0)
        
        # 计算滑动平均
        history_deltas = [d.cumulative_delta for d in self._delta_history]
        delta_avg = sum(history_deltas) / len(history_deltas)
        
        # 计算加速度（最近 5 个 vs 之前 5 个）
        if len(history_deltas) >= 10:
            recent = sum(history_deltas[-5:]) / 5
            older = sum(history_deltas[-10:-5]) / 5
            delta_acceleration = recent - older
        elif len(history_deltas) >= 2:
            mid = len(history_deltas) // 2
            recent = sum(history_deltas[mid:]) / (len(history_deltas) - mid)
            older = sum(history_deltas[:mid]) / mid
            delta_acceleration = recent - older
        else:
            delta_acceleration = 0.0
        
        return (delta_avg, delta_acceleration)
    
    def _determine_trend_enhanced(
        self, 
        delta_ratio: float, 
        acceleration: float,
        short_delta: float,
        total_volume: float
    ) -> DeltaTrend:
        """
        增强版趋势判断（加入短期 Delta 权重）
        
        优化点：
        - 综合考虑 delta_ratio、acceleration、short_delta
        - 使用评分机制而非简单阈值
        """
        # 基础评分（-1 到 1）
        base_score = delta_ratio
        
        # 加速度加成（±0.2）
        if acceleration > 0.1:
            base_score += 0.2
        elif acceleration < -0.1:
            base_score -= 0.2
        
        # 短期 Delta 确认加成（±0.1）
        if total_volume > 0:
            short_ratio = short_delta / (total_volume * 0.2)  # 短窗口占 1/5
            if short_ratio > 0.3 and delta_ratio > 0:
                base_score += 0.1
            elif short_ratio < -0.3 and delta_ratio < 0:
                base_score -= 0.1
        
        # 映射到趋势
        if base_score > 0.5:
            return DeltaTrend.STRONG_BULLISH
        elif base_score > 0.3:
            return DeltaTrend.BULLISH
        elif base_score < -0.5:
            return DeltaTrend.STRONG_BEARISH
        elif base_score < -0.3:
            return DeltaTrend.BEARISH
        else:
            return DeltaTrend.NEUTRAL
    
    def _detect_anomalies_enhanced(
        self, 
        buy_vol: float, 
        sell_vol: float, 
        price_change_pct: float, 
        total_vol: float,
        delta_ratio: float
    ) -> Tuple[bool, bool, bool, bool]:
        """
        增强版异常检测（精确区分吸收和流动性撤离）
        
        检测类型：
        1. 吸收 (Absorption): 大量 Delta 但价格几乎不动
           - 特征：|delta_ratio| 高，|price_change| 低，成交量高于平均
           - 含义：有隐藏的大单在对手方向悄悄出货/吸筹
        
        2. 流动性撤离 (Liquidity Withdrawal): 价格变化但 Delta 不匹配
           - 特征：|price_change| 高，delta_ratio 与价格方向不一致或很小
           - 含义：挂单被撤走导致价格跳动，而非真实的买卖力量
        
        3. Climax: 极端成交量后的反转信号
           - 特征：成交量远高于平均，且出现在价格极端位置
        
        Returns:
            (is_climax_buy, is_climax_sell, is_absorption, is_withdrawal)
        """
        is_climax_buy = False
        is_climax_sell = False
        is_absorption = False
        is_withdrawal = False
        
        if total_vol == 0:
            return (False, False, False, False)
        
        # 计算成交量相对于平均的倍数
        volume_multiple = (
            total_vol / self._avg_volume_per_window 
            if self._avg_volume_per_window > 0 else 1.0
        )
        
        # ========== 吸收检测 ==========
        # 条件：价格变化小 + Delta 偏向明显 + 成交量高于平均
        if abs(price_change_pct) < self.ABSORPTION_PRICE_THRESHOLD:
            if abs(delta_ratio) > self.ABSORPTION_DELTA_THRESHOLD:
                if volume_multiple >= self.ABSORPTION_VOLUME_THRESHOLD:
                    is_absorption = True
                    if delta_ratio > 0:
                        # 大量买入但价格不涨 -> 隐藏卖家在吸收（看跌信号）
                        is_climax_buy = True
                    else:
                        # 大量卖出但价格不跌 -> 隐藏买家在吸收（看涨信号）
                        is_climax_sell = True
        
        # ========== 流动性撤离检测 ==========
        # 条件：价格变化明显 + Delta 与价格方向不匹配
        elif abs(price_change_pct) >= self.WITHDRAWAL_PRICE_THRESHOLD:
            # 价格上涨但 Delta 不支持（买盘不足）
            if price_change_pct > 0 and delta_ratio < self.WITHDRAWAL_DELTA_MISMATCH:
                is_withdrawal = True
            # 价格下跌但 Delta 不支持（卖盘不足）
            elif price_change_pct < 0 and delta_ratio > -self.WITHDRAWAL_DELTA_MISMATCH:
                is_withdrawal = True
        
        return (is_climax_buy, is_climax_sell, is_absorption, is_withdrawal)


class DeltaSignalModifier:
    """
    Delta 信号调节器（优化版）
    
    根据动态订单流分析结果调节交易信号强度
    
    检测场景：
    1. 主动买入/卖出 (Aggressive) -> 增强信号
    2. 吸收 (Absorption) -> 强烈减弱信号（隐藏反向力量）
    3. 流动性撤离 (Withdrawal) -> 中度减弱信号（假突破）
    4. Delta 反向 -> 减弱或阻止信号
    """
    
    @staticmethod
    def calculate_modifier(
        snapshot: DeltaSnapshot, 
        side: str,
        price_change_pct: float = 0.0
    ) -> Tuple[float, str]:
        """
        计算信号调节因子
        
        Args:
            snapshot: Delta 快照
            side: 交易方向 ("buy" 或 "sell")
            price_change_pct: K 线价格变化百分比
        
        Returns:
            (modifier, reason)
            - modifier > 1.0: 增强信号（订单流确认）
            - modifier = 1.0: 不调整
            - modifier < 1.0: 减弱信号（订单流不支持）
            - modifier = 0.0: 阻止信号（强烈反向信号）
        """
        modifier = 1.0
        reasons: List[str] = []
        
        delta_ratio = snapshot.delta_ratio
        trend = snapshot.delta_trend
        
        if side == "buy":
            # ====== 买入信号检测 ======
            
            # 场景 A：主动买入（Aggressive Buying）- 增强
            if trend in [DeltaTrend.STRONG_BULLISH, DeltaTrend.BULLISH]:
                if delta_ratio > 0.3:
                    modifier *= 1.2
                    reasons.append(f"买盘主导(Δ={delta_ratio:.2f})")
                    
                    if snapshot.delta_acceleration > 0.1:
                        modifier *= 1.1
                        reasons.append("买盘加速")
            
            # 场景 B：吸收检测 - 隐藏卖家正在出货（强烈减弱）
            # 特征：大量买入但价格不涨，说明有人在悄悄派发
            if snapshot.is_absorption and delta_ratio > 0:
                modifier *= 0.4  # 更强的减弱
                reasons.append(f"⚠️ 检测到吸收(隐藏卖家在派发)")
            elif snapshot.is_climax_buy:
                modifier *= 0.5
                reasons.append("买入高潮(可能见顶)")
            
            # 场景 C：流动性撤离检测 - 假突破风险（中度减弱）
            # 特征：价格上涨但 Delta 不支持，说明是挂单撤离而非真实买盘
            if price_change_pct > 0.2 and delta_ratio < 0.1:
                withdrawal_severity = min((0.2 - delta_ratio) / 0.3, 1.0)  # 0-1
                modifier *= (0.6 + 0.2 * (1 - withdrawal_severity))  # 0.6-0.8
                reasons.append(f"流动性撤离(价涨{price_change_pct:.2f}%但Δ={delta_ratio:.2f})")
            
            # 场景 D：卖盘主导 - 减弱
            if trend in [DeltaTrend.STRONG_BEARISH, DeltaTrend.BEARISH]:
                if delta_ratio < -0.3:
                    modifier *= 0.6
                    reasons.append(f"卖盘主导(Δ={delta_ratio:.2f})")
                
                # 极端卖压 -> 阻止买入
                if delta_ratio < -0.5 and snapshot.delta_acceleration < -0.1:
                    modifier = 0.0
                    reasons = [f"🚫 极端卖压(Δ={delta_ratio:.2f}, 加速下跌)"]
        
        else:  # side == "sell"
            # ====== 卖出信号检测 ======
            
            # 场景 A：主动卖出（Aggressive Selling）- 增强
            if trend in [DeltaTrend.STRONG_BEARISH, DeltaTrend.BEARISH]:
                if delta_ratio < -0.3:
                    modifier *= 1.2
                    reasons.append(f"卖盘主导(Δ={delta_ratio:.2f})")
                    
                    if snapshot.delta_acceleration < -0.1:
                        modifier *= 1.1
                        reasons.append("卖盘加速")
            
            # 场景 B：吸收检测 - 隐藏买家正在吸筹（强烈减弱）
            # 特征：大量卖出但价格不跌，说明有人在悄悄吸筹
            if snapshot.is_absorption and delta_ratio < 0:
                modifier *= 0.4  # 更强的减弱
                reasons.append(f"⚠️ 检测到吸收(隐藏买家在吸筹)")
            elif snapshot.is_climax_sell:
                modifier *= 0.5
                reasons.append("卖出高潮(可能见底)")
            
            # 场景 C：流动性撤离检测 - 假突破风险（中度减弱）
            # 特征：价格下跌但 Delta 不支持，说明是挂单撤离而非真实卖盘
            if price_change_pct < -0.2 and delta_ratio > -0.1:
                withdrawal_severity = min((delta_ratio + 0.2) / 0.3, 1.0)  # 0-1
                modifier *= (0.6 + 0.2 * (1 - withdrawal_severity))  # 0.6-0.8
                reasons.append(f"流动性撤离(价跌{price_change_pct:.2f}%但Δ={delta_ratio:.2f})")
            
            # 场景 D：买盘主导 - 减弱
            if trend in [DeltaTrend.STRONG_BULLISH, DeltaTrend.BULLISH]:
                if delta_ratio > 0.3:
                    modifier *= 0.6
                    reasons.append(f"买盘主导(Δ={delta_ratio:.2f})")
                
                # 极端买压 -> 阻止卖出
                if delta_ratio > 0.5 and snapshot.delta_acceleration > 0.1:
                    modifier = 0.0
                    reasons = [f"🚫 极端买压(Δ={delta_ratio:.2f}, 加速上涨)"]
        
        reason = ", ".join(reasons) if reasons else "Delta中性"
        return (round(modifier, 2), reason)


def compute_wedge_buy_delta_boost(
    snapshot: DeltaSnapshot, price_change_pct: float = 0.0
) -> Tuple[float, str]:
    """
    Wedge_Buy 专用：根据当前/过去若干 K 线窗口的 Delta 对信号加权。
    
    当检测到 Wedge_Buy 时，形态已保证价格创新低（第三推）。若此时 Delta 显示：
    1. 正背离：价格创新低但 Delta 大幅回升（当前窗口 delta_ratio > 0 或趋势转多）→ 加权
    2. 吸收：巨大负 Delta 但价格跌不动（is_absorption 且 delta_ratio < 0，买盘暗中吸筹）→ 加权
    
    Args:
        snapshot: 当前 Delta 快照（与 K 线周期对齐，可视为近期 1～3 根 K 线内的订单流）
        price_change_pct: 当前窗口内价格变化百分比（可选，用于辅助判断吸收）
    
    Returns:
        (multiplier, reason): 加权倍数（1.0～1.35），及说明
    """
    if snapshot.trade_count == 0:
        return (1.0, "无Delta数据")
    
    multiplier = 1.0
    reasons: List[str] = []
    
    # 吸收：巨大负 Delta 但价格跌不动 → 买盘吸筹，利好 Wedge_Buy
    if snapshot.is_absorption and snapshot.delta_ratio < 0:
        multiplier = 1.25
        reasons.append("吸收(巨大负Delta但价格跌不动，买盘吸筹)")
    
    # 正背离：价格创新低（由 Wedge 形态保证）但 Delta 大幅回升
    if (
        snapshot.delta_ratio > 0.2
        or snapshot.delta_trend in (DeltaTrend.BULLISH, DeltaTrend.STRONG_BULLISH)
    ):
        boost = 1.2
        if multiplier > 1.0:
            multiplier = min(multiplier * boost, 1.35)  # 两者都满足时封顶 1.35
        else:
            multiplier = boost
        reasons.append("正背离(价格创新低后Delta回升)")
    
    reason = ", ".join(reasons) if reasons else "Delta中性"
    return (round(multiplier, 2), reason)


# 全局 Delta 分析器实例
_delta_analyzer: Optional[DeltaAnalyzer] = None
_delta_analyzer_kline_interval: Optional[str] = None


def get_delta_analyzer(kline_interval: str = "5m") -> DeltaAnalyzer:
    """
    获取全局 Delta 分析器实例
    
    Args:
        kline_interval: K 线周期（如 "1m", "5m", "15m", "1h"）
                       首次调用时用于初始化，后续调用忽略此参数
    
    Returns:
        DeltaAnalyzer: 全局单例实例
    """
    global _delta_analyzer, _delta_analyzer_kline_interval
    
    if _delta_analyzer is None:
        _delta_analyzer = DeltaAnalyzer(kline_interval=kline_interval)
        _delta_analyzer_kline_interval = kline_interval
    elif _delta_analyzer_kline_interval != kline_interval:
        # 如果 K 线周期变化，发出警告（不重新创建，保持单例）
        logging.warning(
            f"Delta 分析器已使用 {_delta_analyzer_kline_interval} 周期初始化，"
            f"忽略新的 {kline_interval} 周期请求"
        )
    
    return _delta_analyzer


def reset_delta_analyzer(kline_interval: str = "5m") -> DeltaAnalyzer:
    """
    重置并重新创建全局 Delta 分析器（用于更换 K 线周期）
    
    Args:
        kline_interval: 新的 K 线周期
    
    Returns:
        DeltaAnalyzer: 新创建的实例
    """
    global _delta_analyzer, _delta_analyzer_kline_interval
    
    _delta_analyzer = DeltaAnalyzer(kline_interval=kline_interval)
    _delta_analyzer_kline_interval = kline_interval
    logging.info(f"Delta 分析器已重置为 {kline_interval} 周期")
    
    return _delta_analyzer


async def aggtrade_worker(symbol: str = "BTCUSDT", redis_url: Optional[str] = None, kline_interval: str = "5m") -> None:
    """
    aggTrade 数据流工作线程
    
    功能：
    1. 订阅 Binance WebSocket aggTrade 数据流
    2. 实时计算动态订单流 Delta（窗口与 K 线周期对齐）
    3. 将结果存入 Redis，缓存时间为窗口的 1/5
    
    aggTrade 字段说明：
    - e: 事件类型 (aggTrade)
    - s: 交易对
    - p: 成交价格
    - q: 成交数量
    - m: 是否为买方做市商
        - true: 卖方主动 (Market Sell)
        - false: 买方主动 (Market Buy)
    - T: 成交时间
    
    Args:
        symbol: 交易对符号（如 "BTCUSDT"）
        redis_url: Redis 连接 URL
        kline_interval: K 线周期（用于对齐 Delta 窗口）
    """
    redis_client: Optional[aioredis.Redis] = None
    client: Optional[AsyncClient] = None
    reconnect_attempt = 0
    max_reconnect_attempts = 10
    base_delay = 1
    
    # 获取全局 Delta 分析器（使用 K 线周期初始化）
    analyzer = get_delta_analyzer(kline_interval=kline_interval)
    
    while reconnect_attempt < max_reconnect_attempts:
        try:
            logging.info(
                f"正在连接 Binance WebSocket (aggTrade 订单流)..."
                + (
                    f" (重连尝试 {reconnect_attempt + 1}/{max_reconnect_attempts})"
                    if reconnect_attempt > 0
                    else ""
                )
            )
            
            # 连接 Redis（可选）
            if redis_url:
                try:
                    redis_client = await aioredis.from_url(
                        redis_url,
                        encoding="utf-8",
                        decode_responses=True,
                        socket_connect_timeout=5,
                    )
                    await redis_client.ping()
                    logging.info(f"✅ Redis 连接成功（用于 Delta 缓存）")
                except Exception as e:
                    logging.warning(f"⚠️ Redis 连接失败: {e}，Delta 数据将仅保存在内存中")
                    redis_client = None
            
            # 创建 Binance 客户端
            try:
                if client is not None:
                    try:
                        await client.close_connection()
                    except:
                        pass
                client = await AsyncClient.create()
                logging.info("✅ Binance WebSocket 客户端创建成功")
            except Exception as e:
                logging.error(f"❌ Binance 客户端创建失败: {e}")
                raise
            
            # 创建 WebSocket 管理器（必须在构造函数中传入 max_queue_size）
            bsm = BinanceSocketManager(client, user_timeout=60, max_queue_size=10000)
            
            # 订阅 aggTrade 数据流
            trade_socket = bsm.aggtrade_socket(symbol)
            
            # 统计计数器
            trade_count = 0
            last_log_time = time.time()
            # 日志间隔：与短窗口对齐，最小 30 秒
            LOG_INTERVAL = max(analyzer.SHORT_WINDOW_SECONDS, 30)
            # Redis 缓存过期时间：短窗口的一半，确保数据新鲜
            REDIS_CACHE_EXPIRE = max(analyzer.SHORT_WINDOW_SECONDS // 2, 10)
            
            async with trade_socket as stream:
                logging.info(
                    f"🔄 动态订单流监控已启动: {symbol} (aggTrade Delta, "
                    f"窗口={analyzer.WINDOW_SECONDS}秒, 日志间隔={LOG_INTERVAL}秒)"
                )
                reconnect_attempt = 0  # 重置重连计数
                
                # ========== 批量处理优化 ==========
                # 收集一批交易后一次性处理，减少锁竞争和函数调用开销
                BATCH_SIZE = 100  # 每批处理 100 条
                BATCH_TIMEOUT = 0.1  # 最长等待 100ms
                trade_batch: List[Tuple[int, float, float, bool]] = []
                last_batch_time = time.time()
                
                while True:
                    try:
                        # 非阻塞接收，支持批量处理
                        try:
                            msg = await asyncio.wait_for(stream.recv(), timeout=BATCH_TIMEOUT)
                        except asyncio.TimeoutError:
                            # 超时，处理当前批次
                            if trade_batch:
                                await analyzer.add_trades_batch(trade_batch)
                                trade_count += len(trade_batch)
                                trade_batch = []
                                last_batch_time = time.time()
                            continue
                        
                        if msg is None:
                            logging.warning("aggTrade 数据流返回 None，可能连接断开")
                            break
                        
                        # 解析 aggTrade 数据
                        if "p" not in msg or "q" not in msg:
                            continue
                        
                        price = float(msg["p"])
                        qty = float(msg["q"])
                        is_buyer_maker = msg.get("m", False)  # true=卖方主动, false=买方主动
                        timestamp = msg.get("T", int(time.time() * 1000))
                        
                        # 添加到批次
                        trade_batch.append((timestamp, price, qty, is_buyer_maker))
                        
                        # 批次满或超时，处理批次
                        current_time = time.time()
                        if len(trade_batch) >= BATCH_SIZE or (current_time - last_batch_time) >= BATCH_TIMEOUT:
                            await analyzer.add_trades_batch(trade_batch)
                            trade_count += len(trade_batch)
                            trade_batch = []
                            last_batch_time = current_time
                        
                        # 定期获取快照并存入 Redis
                        current_time = time.time()
                        if current_time - last_log_time >= LOG_INTERVAL:
                            snapshot = await analyzer.get_snapshot(symbol)
                            
                            # 存入 Redis（带重连逻辑）
                            if redis_client:
                                try:
                                    redis_key = f"cache:delta:{symbol}"
                                    await redis_client.setex(
                                        redis_key,
                                        REDIS_CACHE_EXPIRE,  # 动态过期时间
                                        json.dumps({
                                            "cumulative_delta": round(snapshot.cumulative_delta, 4),
                                            "buy_volume": round(snapshot.buy_volume, 4),
                                            "sell_volume": round(snapshot.sell_volume, 4),
                                            "delta_ratio": round(snapshot.delta_ratio, 4),
                                            "delta_avg": round(snapshot.delta_avg, 4),
                                            "delta_acceleration": round(snapshot.delta_acceleration, 4),
                                            "delta_trend": snapshot.delta_trend.value,
                                            "is_absorption": snapshot.is_absorption,
                                            "is_climax_buy": snapshot.is_climax_buy,
                                            "is_climax_sell": snapshot.is_climax_sell,
                                            "trade_count": snapshot.trade_count,
                                            "timestamp": snapshot.timestamp,
                                        })
                                    )
                                except Exception as redis_err:
                                    logging.warning(f"⚠️ Redis 写入失败: {redis_err}，尝试重连...")
                                    # 尝试重连 Redis
                                    try:
                                        await redis_client.aclose()
                                    except:
                                        pass
                                    
                                    if redis_url:
                                        try:
                                            redis_client = await aioredis.from_url(
                                                redis_url,
                                                encoding="utf-8",
                                                decode_responses=True,
                                                socket_connect_timeout=5,
                                            )
                                            await redis_client.ping()
                                            logging.info(f"✅ Redis 重连成功")
                                        except Exception as reconnect_err:
                                            logging.warning(f"⚠️ Redis 重连失败: {reconnect_err}，继续使用内存模式")
                                            redis_client = None
                            
                            # 日志输出
                            trend_emoji = {
                                DeltaTrend.STRONG_BULLISH: "🟢🟢",
                                DeltaTrend.BULLISH: "🟢",
                                DeltaTrend.NEUTRAL: "⚪",
                                DeltaTrend.BEARISH: "🔴",
                                DeltaTrend.STRONG_BEARISH: "🔴🔴",
                            }
                            logging.debug(
                                f"📊 Delta更新: {trend_emoji.get(snapshot.delta_trend, '⚪')} "
                                f"累计={snapshot.cumulative_delta:.4f}, "
                                f"比率={snapshot.delta_ratio:.4f}, "
                                f"买量={snapshot.buy_volume:.2f}, "
                                f"卖量={snapshot.sell_volume:.2f}, "
                                f"趋势={snapshot.delta_trend.value}, "
                                f"成交数={trade_count}"
                            )
                            
                            last_log_time = current_time
                            trade_count = 0
                    
                    except ReadLoopClosed:
                        logging.warning("WebSocket 读取循环已关闭，准备重连...")
                        break
                    except asyncio.TimeoutError:
                        # 超时只是没有新数据，继续等待
                        continue
                    except Exception as e:
                        logging.error(f"处理 aggTrade 数据失败: {e}", exc_info=True)
                        await asyncio.sleep(1)
        
        except ReadLoopClosed:
            reconnect_attempt += 1
            delay = min(base_delay * (2 ** reconnect_attempt), 60)
            logging.warning(
                f"aggTrade WebSocket 读取循环已关闭，"
                f"{delay}秒后重连 ({reconnect_attempt}/{max_reconnect_attempts})"
            )
            await asyncio.sleep(delay)
        except ConnectionClosed as e:
            reconnect_attempt += 1
            delay = min(base_delay * (2 ** reconnect_attempt), 60)
            logging.warning(
                f"aggTrade WebSocket 连接关闭: {e}，"
                f"{delay}秒后重连 ({reconnect_attempt}/{max_reconnect_attempts})"
            )
            await asyncio.sleep(delay)
        except Exception as e:
            reconnect_attempt += 1
            delay = min(base_delay * (2 ** reconnect_attempt), 60)
            logging.error(
                f"aggTrade 监控异常: {e}，"
                f"{delay}秒后重连 ({reconnect_attempt}/{max_reconnect_attempts})",
                exc_info=True
            )
            await asyncio.sleep(delay)
            
            # 只关闭 Binance 客户端（Redis 保持复用）
            if client is not None:
                try:
                    await client.close_connection()
                except:
                    pass
                client = None
    
    # 循环结束后，清理所有资源
    logging.error(f"aggTrade 监控达到最大重连次数 ({max_reconnect_attempts})，已停止")
    
    # 最终清理
    if client is not None:
        try:
            await client.close_connection()
        except:
            pass
    if redis_client is not None:
        try:
            await redis_client.aclose()
        except:
            pass
