"""
市场状态分析器

负责 MarketState（含 TightChannel）与市场周期状态机（Market Cycle）的识别逻辑

Al Brooks 核心市场状态：
- STRONG_TREND: 强劲趋势（连续同向K线，禁止逆势交易）
- BREAKOUT: 强趋势突破
- CHANNEL: 通道模式，EMA附近有序运行
- TRADING_RANGE: 交易区间，价格频繁穿越EMA
- TIGHT_CHANNEL: 紧凑通道，强劲单边趋势（禁止反转）
- FINAL_FLAG: 终极旗形（TightChannel 后远离 EMA 处的横盘，高胜率反转点）

市场周期状态机（严格三阶段）：
- SPIKE（尖峰）：强突破阶段，逻辑“Always In”，忽略小回调
- CHANNEL（通道）：趋势延续，EMA 附近有序运行
- TRADING_RANGE（交易区间）：高空低多 BLSH，自动降低信号棒准入门槛
"""

import logging
import pandas as pd
from enum import Enum
from typing import Optional

from .interval_params import get_interval_params, IntervalParams


class MarketState(Enum):
    """市场状态分类"""
    STRONG_TREND = "StrongTrend"  # 强劲趋势状态
    BREAKOUT = "Breakout"
    CHANNEL = "Channel"
    TRADING_RANGE = "TradingRange"
    TIGHT_CHANNEL = "TightChannel"
    FINAL_FLAG = "FinalFlag"  # 终极旗形：TightChannel 后远离 EMA 处的横盘/小回调


class MarketCycle(Enum):
    """
    市场周期状态机（Al Brooks 严格三阶段）
    
    将市场严格划分为三种周期，对应不同交易逻辑：
    - SPIKE：尖峰阶段，Always In，忽略小回调
    - CHANNEL：通道阶段，趋势延续
    - TRADING_RANGE：交易区间，高空低多 BLSH，降低信号棒门槛
    """
    SPIKE = "Spike"           # 尖峰：强突破，Always In，忽略小回调
    CHANNEL = "Channel"       # 通道：趋势延续
    TRADING_RANGE = "TradingRange"  # 交易区间：BLSH，放宽信号棒


class AlwaysInDirection(Enum):
    """
    Al Brooks "Always In" 方向
    
    核心概念：市场始终处于某一方向的控制之下
    - LONG：多头控制，优先寻找做多机会
    - SHORT：空头控制，优先寻找做空机会
    - NEUTRAL：无明确控制方，双向皆可
    
    用途：
    - 决定反转交易的置信度（逆 Always In 方向的信号需要更强确认）
    - 指导趋势跟踪策略（顺 Always In 方向交易）
    """
    LONG = "long"       # Always In Long：多头控制
    SHORT = "short"     # Always In Short：空头控制
    NEUTRAL = "neutral"  # 无明确方向


class MarketAnalyzer:
    """
    市场状态分析器（周期自适应版）
    
    负责检测当前市场处于哪种状态，指导信号生成策略
    
    周期自适应：
    - 参数根据 K 线周期自动调整
    - 短周期趋势检测更敏感
    - 长周期趋势检测更稳定
    """
    
    def __init__(self, ema_period: int = 20, kline_interval: str = "5m"):
        self.ema_period = ema_period
        self.kline_interval = kline_interval
        
        # 加载周期自适应参数
        self._params: IntervalParams = get_interval_params(kline_interval)
        
        # 趋势方向缓存（用于禁止逆势交易）
        self._trend_direction: Optional[str] = None  # "up" / "down" / None
        self._trend_strength: float = 0.0  # 0-1
        
        # 市场周期状态机：滞后保持，避免尖峰/区间频繁切换
        self._last_cycle: Optional[MarketCycle] = None
        self._cycle_hold_bars: int = 0  # 剩余保持周期数（>0 时沿用上一周期）
        
        # Final Flag 检测：TightChannel 历史追踪
        self._tight_channel_bars: int = 0  # 连续 TightChannel 计数
        self._tight_channel_direction: Optional[str] = None  # TightChannel 方向
        self._tight_channel_extreme: Optional[float] = None  # TightChannel 期间的极值
        self._last_tight_channel_end_bar: Optional[int] = None  # 最近 TightChannel 结束的 bar 索引
        
        logging.info(
            f"📊 MarketAnalyzer 初始化: 周期={kline_interval}, "
            f"斜率阈值={self._params.slope_threshold_pct:.2%}, "
            f"趋势阈值={self._params.strong_trend_threshold}"
        )
    
    def get_trend_direction(self) -> Optional[str]:
        """获取当前趋势方向"""
        return self._trend_direction
    
    def get_trend_strength(self) -> float:
        """获取当前趋势强度 (0-1)"""
        return self._trend_strength
    
    def calculate_ema_deviation(
        self, df: pd.DataFrame, i: int, ema: float, atr: Optional[float] = None
    ) -> tuple:
        """
        计算 EMA 偏离度评分（替代 Gap Bar 概念）
        
        Al Brooks: "Gap Bar（开盘跳空远离 EMA 的棒）在传统市场表示趋势紧迫性。
        但加密市场 24/7 交易，很少有真正的跳空。因此用 EMA 偏离度替代 Gap Bar 概念。"
        
        EMA 偏离度定义：
        - 当前价格与 EMA20 的距离，以 ATR 为单位
        - 偏离度 > 2.0 ATR：趋势紧迫，可能是追涨/追跌的好时机
        - 偏离度 < 0.5 ATR：价格贴近 EMA，适合回调入场
        
        Args:
            df: K线数据
            i: 当前 K 线索引
            ema: EMA 值
            atr: ATR 值
        
        Returns:
            (deviation_score, deviation_direction, urgency_level)
            - deviation_score: 偏离度评分（以 ATR 为单位）
            - deviation_direction: 偏离方向 "above" / "below" / "neutral"
            - urgency_level: 紧迫度 "high" / "medium" / "low"
        """
        if i < 1 or ema <= 0:
            return (0.0, "neutral", "low")
        
        close = float(df.iloc[i]["close"])
        
        # 计算偏离度
        deviation = close - ema
        
        # 用 ATR 标准化偏离度
        if atr and atr > 0:
            deviation_score = abs(deviation) / atr
        else:
            # 无 ATR 时用百分比（假设 2% = 高偏离）
            deviation_score = abs(deviation / ema) * 50  # 2% ≈ 1.0
        
        # 偏离方向
        if deviation > 0:
            deviation_direction = "above"
        elif deviation < 0:
            deviation_direction = "below"
        else:
            deviation_direction = "neutral"
        
        # 紧迫度等级
        if deviation_score >= 2.0:
            urgency_level = "high"  # 远离 EMA，趋势紧迫
        elif deviation_score >= 1.0:
            urgency_level = "medium"  # 中等偏离
        else:
            urgency_level = "low"  # 贴近 EMA，适合回调入场
        
        logging.debug(
            f"EMA偏离度: score={deviation_score:.2f}ATR, "
            f"方向={deviation_direction}, 紧迫度={urgency_level}"
        )
        
        return (deviation_score, deviation_direction, urgency_level)
    
    def detect_market_state(self, df: pd.DataFrame, i: int, ema: float) -> MarketState:
        """
        检测当前市场状态
        
        优先级：
        1. Strong Trend（强趋势）- 最高优先级，禁止逆势交易
        2. Tight Channel（紧凑通道）
        3. Final Flag（终极旗形）- TightChannel 后远离 EMA 处的横盘
        4. Breakout（强趋势突破）
        5. Trading Range（交易区间）
        6. Channel（通道模式）- 默认
        """
        if i < 10:
            self._trend_direction = None
            self._trend_strength = 0.0
            return MarketState.CHANNEL
        
        # ========== 优先检测 STRONG_TREND（强趋势）==========
        # Al Brooks: 连续同向K线 = 趋势，不要逆势交易
        strong_trend = self._detect_strong_trend(df, i, ema)
        if strong_trend is not None:
            self._tight_channel_bars = 0  # 强趋势时重置 TightChannel 计数
            return strong_trend
        
        # 优先检测 TIGHT_CHANNEL
        tight_channel_state = self._detect_tight_channel(df, i, ema)
        if tight_channel_state is not None:
            # 追踪 TightChannel 历史
            self._tight_channel_bars += 1
            tc_dir = self.get_tight_channel_direction(df, i)
            self._tight_channel_direction = tc_dir
            # 更新极值
            if tc_dir == "up":
                current_high = float(df.iloc[i]["high"])
                if self._tight_channel_extreme is None or current_high > self._tight_channel_extreme:
                    self._tight_channel_extreme = current_high
            elif tc_dir == "down":
                current_low = float(df.iloc[i]["low"])
                if self._tight_channel_extreme is None or current_low < self._tight_channel_extreme:
                    self._tight_channel_extreme = current_low
            return tight_channel_state
        
        # TightChannel 刚结束：记录结束点
        if self._tight_channel_bars > 0:
            self._last_tight_channel_end_bar = i - 1
        
        # ========== 检测 FINAL_FLAG（终极旗形）==========
        final_flag = self._detect_final_flag(df, i, ema)
        if final_flag is not None:
            return final_flag
        
        # 重置 TightChannel 追踪（若不在 TightChannel 且不在 FinalFlag）
        self._tight_channel_bars = 0
        self._tight_channel_direction = None
        self._tight_channel_extreme = None
        
        # 计算最近20根K线的EMA穿越次数（向量化）
        recent = df.iloc[max(0, i - 20) : i + 1]
        
        # 使用预计算的 above_ema 列或即时计算
        if "above_ema" in recent.columns:
            above_ema_series = recent["above_ema"]
        else:
            above_ema_series = recent["close"] > recent["ema"]
        
        # 向量化计算穿越次数：检测布尔值变化
        ema_crosses = int(above_ema_series.astype(int).diff().abs().sum())
        
        # 频繁穿越EMA -> Trading Range
        if ema_crosses >= 4:
            return MarketState.TRADING_RANGE
        
        # 检测强突破（Spike）- ⭐ 优化：进一步放宽条件
        if i >= 1 and "body_size" in df.columns:
            # 使用预计算的 body_size 列（向量化）
            recent_bodies = df["body_size"].iloc[max(0, i - 10):i + 1]
            avg_body = recent_bodies.mean() if len(recent_bodies) > 0 else 0
            current_body = df.iloc[i]["body_size"]
            
            if avg_body > 0:
                # ⭐ 优化：从 1.8x 降到 1.5x（更容易触发 BREAKOUT）
                if current_body > avg_body * 1.5:
                    close = df.iloc[i]["close"]
                    high = df.iloc[i]["high"]
                    low = df.iloc[i]["low"]
                    
                    if (high - low) > 0:
                        # ⭐ 优化：body_ratio 从 0.8 降到 0.7（双向）
                        if close > ema and (close - low) / (high - low) > 0.7:
                            return MarketState.BREAKOUT
                        elif close < ema and (high - close) / (high - low) > 0.7:
                            return MarketState.BREAKOUT
        
        return MarketState.CHANNEL
    
    def get_market_cycle(
        self, df: pd.DataFrame, i: int, ema: float, market_state: MarketState
    ) -> MarketCycle:
        """
        市场周期状态机：将市场严格划分为 Spike / Channel / Trading Range。
        
        - Spike（尖峰）：BREAKOUT → Always In，忽略小回调
        - Channel（通道）：STRONG_TREND / TIGHT_CHANNEL / CHANNEL
        - Trading Range（交易区间）：TRADING_RANGE → BLSH，降低信号棒门槛
        
        带简单滞后：一旦进入 Spike 保持 2 根 K 线，避免尖峰与通道来回切换。
        """
        # 滞后：若仍在保持期内，沿用上一周期
        if self._cycle_hold_bars > 0 and self._last_cycle is not None:
            self._cycle_hold_bars -= 1
            return self._last_cycle
        
        if market_state == MarketState.BREAKOUT:
            cycle = MarketCycle.SPIKE
            self._cycle_hold_bars = 2  # 尖峰后保持 2 根
        elif market_state == MarketState.TRADING_RANGE:
            cycle = MarketCycle.TRADING_RANGE
            self._cycle_hold_bars = 0
        else:
            # STRONG_TREND, TIGHT_CHANNEL, CHANNEL
            cycle = MarketCycle.CHANNEL
            self._cycle_hold_bars = 0
        
        self._last_cycle = cycle
        return cycle
    
    def _detect_strong_trend(self, df: pd.DataFrame, i: int, ema: float) -> Optional[MarketState]:
        """
        检测强趋势状态（Al Brooks 价格行为核心）
        
        优化增强（提前响应）：
        1. 连续同向 K 线阈值从 4 降到 3
        2. 新增"早期趋势"检测（5 根 K 线快速涨跌）
        3. STRONG_TREND 触发阈值从 0.6 降到 0.5
        4. ⭐ Gap 检测 - Al Brooks 最强趋势信号
        
        强趋势条件（组合评分）：
        1. 连续3根以上同向K线（收盘>开盘 或 收盘<开盘）
        2. 连续4根K线都创新高/新低
        3. 价格持续远离EMA（距离 > 0.5% 且持续5根以上）
        4. 最近5根K线快速涨跌超过0.8%
        5. Gap（缺口）- Bar Gap 或 Body Gap（最强信号，+0.25~0.4 分）
        
        Al Brooks: "A gap is the strongest form of urgency"
        
        在强趋势中禁止逆势交易！
        """
        if i < 10:
            return None
        
        lookback = 10  # 看最近10根K线
        recent = df.iloc[max(0, i - lookback + 1) : i + 1]
        
        if len(recent) < 5:
            return None
        
        # ========== 指标1: 连续同向K线（向量化）==========
        # 使用预计算列或即时计算
        if "is_bullish" in recent.columns:
            is_bullish = recent["is_bullish"]
            is_bearish = recent["is_bearish"]
        else:
            is_bullish = recent["close"] > recent["open"]
            is_bearish = recent["close"] < recent["open"]
        
        # 向量化计算最大连续阳线/阴线数
        def max_consecutive(series):
            """计算布尔序列中最大连续 True 的数量"""
            if series.empty:
                return 0
            groups = (series != series.shift()).cumsum()
            return series.groupby(groups).sum().max() if series.any() else 0
        
        max_bullish_streak = max_consecutive(is_bullish)
        max_bearish_streak = max_consecutive(is_bearish)
        
        # ========== 指标2: 连续创新高/新低（向量化）==========
        higher_highs = int((recent["high"].diff() > 0).sum())
        lower_lows = int((recent["low"].diff() < 0).sum())
        
        # ========== 指标3: 持续远离EMA（向量化）==========
        if "ema" in recent.columns:
            ema_col = recent["ema"]
        else:
            ema_col = pd.Series([ema] * len(recent), index=recent.index)
        
        # 使用预计算列或即时计算
        if "above_ema" in recent.columns:
            bars_above_ema = int(recent["above_ema"].sum())
            bars_below_ema = int((~recent["above_ema"]).sum())
        else:
            bars_above_ema = int((recent["close"] > ema_col).sum())
            bars_below_ema = len(recent) - bars_above_ema
        
        # 平均距离百分比
        distance_pct_series = (recent["close"] - ema_col) / ema_col.replace(0, float('nan'))
        avg_distance_pct = distance_pct_series.mean() if not distance_pct_series.isna().all() else 0
        
        # ========== 指标4: 早期趋势检测 - 5 根 K 线快速涨跌 ==========
        recent_5 = df.iloc[max(0, i - 4) : i + 1]
        price_change_pct = 0.0
        if len(recent_5) >= 5 and recent_5.iloc[0]["open"] > 0:
            price_change_pct = (recent_5.iloc[-1]["close"] - recent_5.iloc[0]["open"]) / recent_5.iloc[0]["open"]
        
        # ========== 指标5（新增）: Gap 检测 - Al Brooks 最强趋势信号 ==========
        # Al Brooks: "A gap is the strongest form of urgency"
        # Gap 类型：
        # - Bar Gap（K线缺口）：当前低点 > 前一根高点（上涨），或当前高点 < 前一根低点（下跌）
        # - Body Gap（实体缺口）：开盘价跳空于前一根收盘价
        gap_up_count = 0.0
        gap_down_count = 0.0
        
        for j in range(max(0, i - 2), i):  # 检查最近 3 根 K 线之间的缺口
            curr_idx = j + 1
            if curr_idx > i:
                break
            
            prev_high = df.iloc[j]["high"]
            prev_low = df.iloc[j]["low"]
            prev_close = df.iloc[j]["close"]
            curr_low = df.iloc[curr_idx]["low"]
            curr_high = df.iloc[curr_idx]["high"]
            curr_open = df.iloc[curr_idx]["open"]
            
            # 上涨 Bar Gap：当前低点 > 前一根高点（完全跳空）
            if curr_low > prev_high:
                gap_up_count += 1.0
                logging.debug(f"📈 检测到上涨 Bar Gap: K线{curr_idx} 低点 {curr_low:.2f} > K线{j} 高点 {prev_high:.2f}")
            # 上涨 Body Gap：开盘跳空高于前收盘（至少 0.1%）
            elif prev_close > 0 and curr_open > prev_close * 1.001:
                gap_up_count += 0.5
            
            # 下跌 Bar Gap：当前高点 < 前一根低点（完全跳空）
            if curr_high < prev_low:
                gap_down_count += 1.0
                logging.debug(f"📉 检测到下跌 Bar Gap: K线{curr_idx} 高点 {curr_high:.2f} < K线{j} 低点 {prev_low:.2f}")
            # 下跌 Body Gap：开盘跳空低于前收盘（至少 0.1%）
            elif prev_close > 0 and curr_open < prev_close * 0.999:
                gap_down_count += 0.5
        
        # ========== 指标6（Al Brooks 修正）：最大回调幅度检测 ==========
        # Al Brooks: "强趋势的特征是没有任何有意义的回调"
        # 即使有回调，回调幅度也非常小（< 前一波动的 30%）
        max_pullback_up = 0.0  # 上涨趋势中的最大回调（最大跌幅）
        max_pullback_down = 0.0  # 下跌趋势中的最大反弹（最大涨幅）
        
        for j in range(1, len(recent)):
            curr_high = float(recent.iloc[j]["high"])
            curr_low = float(recent.iloc[j]["low"])
            prev_high = float(recent.iloc[j - 1]["high"])
            prev_low = float(recent.iloc[j - 1]["low"])
            
            # 上涨趋势中的回调：当前低点相对于前一根高点的跌幅
            pullback_from_high = prev_high - curr_low
            max_pullback_up = max(max_pullback_up, pullback_from_high)
            
            # 下跌趋势中的反弹：当前高点相对于前一根低点的涨幅
            bounce_from_low = curr_high - prev_low
            max_pullback_down = max(max_pullback_down, bounce_from_low)
        
        # 计算整体走势幅度
        overall_high = float(recent["high"].max())
        overall_low = float(recent["low"].min())
        overall_move = overall_high - overall_low
        
        # ========== 综合判断趋势方向和强度 ==========
        trend_direction = None
        trend_strength = 0.0
        
        # 上涨趋势判断（优化：阈值降低，更早响应）
        up_score = 0.0
        if max_bullish_streak >= 3:  # 从 4 降到 3
            up_score += 0.25
        if max_bullish_streak >= 5:  # 从 6 降到 5
            up_score += 0.25
        if higher_highs >= 4:
            up_score += 0.2
        if bars_above_ema >= 8:
            up_score += 0.15
        if avg_distance_pct > 0.005:  # 价格在EMA上方0.5%以上
            up_score += 0.1
        # 早期趋势检测
        if price_change_pct > 0.008:  # 5 根 K 线内涨超 0.8%
            up_score += 0.15
        # ⭐ 新增：Gap 检测 - Al Brooks 最强趋势信号
        if gap_up_count >= 1:
            up_score += 0.25  # 1 个缺口加 0.25
        if gap_up_count >= 2:
            up_score += 0.15  # 2 个缺口额外加 0.15
        
        # Al Brooks 修正：最大回调幅度惩罚
        # 如果回调幅度 > 整体走势的 30%，说明趋势不够强，减分
        if overall_move > 0 and max_pullback_up > overall_move * 0.3:
            pullback_penalty = min((max_pullback_up / overall_move - 0.3) * 0.5, 0.15)
            up_score -= pullback_penalty
            logging.debug(
                f"Strong Trend 回调惩罚(上涨): 最大回调={max_pullback_up:.2f}, "
                f"整体走势={overall_move:.2f}, 惩罚={pullback_penalty:.2f}"
            )
        
        # 下跌趋势判断（优化：阈值降低，更早响应）
        down_score = 0.0
        if max_bearish_streak >= 3:  # 从 4 降到 3
            down_score += 0.25
        if max_bearish_streak >= 5:  # 从 6 降到 5
            down_score += 0.25
        if lower_lows >= 4:
            down_score += 0.2
        if bars_below_ema >= 8:
            down_score += 0.15
        if avg_distance_pct < -0.005:  # 价格在EMA下方0.5%以上
            down_score += 0.1
        # 早期趋势检测
        if price_change_pct < -0.008:  # 5 根 K 线内跌超 0.8%
            down_score += 0.15
        # ⭐ 新增：Gap 检测 - Al Brooks 最强趋势信号
        if gap_down_count >= 1:
            down_score += 0.25  # 1 个缺口加 0.25
        if gap_down_count >= 2:
            down_score += 0.15  # 2 个缺口额外加 0.15
        
        # Al Brooks 修正：最大反弹幅度惩罚
        # 如果反弹幅度 > 整体走势的 30%，说明趋势不够强，减分
        if overall_move > 0 and max_pullback_down > overall_move * 0.3:
            bounce_penalty = min((max_pullback_down / overall_move - 0.3) * 0.5, 0.15)
            down_score -= bounce_penalty
            logging.debug(
                f"Strong Trend 反弹惩罚(下跌): 最大反弹={max_pullback_down:.2f}, "
                f"整体走势={overall_move:.2f}, 惩罚={bounce_penalty:.2f}"
            )
        
        # 确定趋势方向
        if up_score >= 0.5 and up_score > down_score:
            trend_direction = "up"
            trend_strength = up_score
        elif down_score >= 0.5 and down_score > up_score:
            trend_direction = "down"
            trend_strength = down_score
        
        # 更新缓存
        self._trend_direction = trend_direction
        self._trend_strength = trend_strength
        
        # 判断是否达到强趋势状态（周期自适应阈值）
        if trend_strength >= self._params.strong_trend_threshold:
            # 构建 Gap 信息字符串
            gap_info = ""
            if gap_up_count > 0:
                gap_info = f", Gap↑={gap_up_count:.1f}"
            elif gap_down_count > 0:
                gap_info = f", Gap↓={gap_down_count:.1f}"
            
            logging.debug(
                f"🔥 检测到强趋势: 方向={trend_direction}, 强度={trend_strength:.2f}, "
                f"连续阳线={max_bullish_streak}, 连续阴线={max_bearish_streak}, "
                f"连续新高={higher_highs}, 连续新低={lower_lows}, "
                f"5K涨跌={price_change_pct:.2%}{gap_info}"
            )
            return MarketState.STRONG_TREND
        
        return None
    
    def _detect_tight_channel(self, df: pd.DataFrame, i: int, ema: float) -> Optional[MarketState]:
        """
        检测紧凑通道（Tight Channel）- 强单边斜率检测
        
        Al Brooks 核心原则：
        在强劲的单边趋势（紧凑通道）中做反转是"自杀行为"
        
        BTC 高波动优化 - 三重条件检测：
        条件 A：最近10根K线中，没有任何一根触碰到EMA（趋势强度）
        条件 B：最近5根K线中至少有3根是同向趋势棒（方向一致性）
        条件 C（新增）：斜率检测 - 10根K线的价格变化率 > 0.8%（强单边斜率）
        
        符合任意两个条件即判定为 Tight Channel
        """
        if i < 10:
            return None
        
        lookback_10 = df.iloc[max(0, i - 9) : i + 1]
        
        # ========== 条件 A：EMA 距离检测 ==========
        all_above_ema = True
        all_below_ema = True
        
        for idx in lookback_10.index:
            bar_high = lookback_10.at[idx, "high"]
            bar_low = lookback_10.at[idx, "low"]
            bar_ema = lookback_10.at[idx, "ema"] if "ema" in lookback_10.columns else ema
            
            if bar_low <= bar_ema * 1.001:
                all_above_ema = False
            if bar_high >= bar_ema * 0.999:
                all_below_ema = False
        
        condition_a_up = all_above_ema
        condition_a_down = all_below_ema
        
        # ========== 条件 B：方向一致性检测 ==========
        lookback_5 = df.iloc[max(0, i - 4) : i + 1]
        
        bullish_bars = 0
        bearish_bars = 0
        
        for idx in lookback_5.index:
            bar_close = lookback_5.at[idx, "close"]
            bar_open = lookback_5.at[idx, "open"]
            
            if bar_close > bar_open:
                bullish_bars += 1
            elif bar_close < bar_open:
                bearish_bars += 1
        
        condition_b_up = bullish_bars >= 3
        condition_b_down = bearish_bars >= 3
        
        # ========== 条件 C（新增）：强单边斜率检测（周期自适应）==========
        # Al Brooks: "强单边斜率"意味着价格持续向一个方向移动
        # 斜率阈值根据 K 线周期自动调整
        SLOPE_THRESHOLD_PCT = self._params.slope_threshold_pct
        
        first_close = lookback_10.iloc[0]["close"]
        last_close = lookback_10.iloc[-1]["close"]
        slope_pct = (last_close - first_close) / first_close if first_close > 0 else 0
        
        condition_c_up = slope_pct > SLOPE_THRESHOLD_PCT
        condition_c_down = slope_pct < -SLOPE_THRESHOLD_PCT
        
        # ========== 条件 D（Al Brooks 修正）：K 线重叠度检测 ==========
        # Al Brooks: "Tight Channel 的 K 线之间高度重叠，没有任何有意义的回调"
        # 后一根 K 线与前一根重叠 > 50% 视为高重叠
        overlap_count = 0
        for j in range(1, len(lookback_10)):
            curr_high = float(lookback_10.iloc[j]["high"])
            curr_low = float(lookback_10.iloc[j]["low"])
            prev_high = float(lookback_10.iloc[j - 1]["high"])
            prev_low = float(lookback_10.iloc[j - 1]["low"])
            
            # 计算重叠区域
            overlap = min(curr_high, prev_high) - max(curr_low, prev_low)
            curr_range = curr_high - curr_low
            
            if overlap > 0 and curr_range > 0 and (overlap / curr_range) > 0.5:
                overlap_count += 1
        
        # 至少 6/9 根（66%）有高重叠才算 Tight Channel
        condition_d = overlap_count >= 6
        
        logging.debug(
            f"TightChannel 重叠度检测: 高重叠K线数={overlap_count}/9, "
            f"条件D满足={condition_d}"
        )
        
        # ========== 综合判断：符合任意两个条件即为 Tight Channel ==========
        # Al Brooks 修正：增加条件 D（重叠度）作为加分项
        
        # 上升 Tight Channel
        up_conditions_met = sum([condition_a_up, condition_b_up, condition_c_up])
        # 重叠度可以作为第四个条件
        if condition_d:
            up_conditions_met += 1
        
        if up_conditions_met >= 2:
            logging.debug(
                f"🔒 Tight Channel(上升): EMA距离={condition_a_up}, "
                f"方向一致={condition_b_up}(阳线{bullish_bars}/5), "
                f"斜率={condition_c_up}({slope_pct:.2%}), "
                f"重叠度={condition_d}({overlap_count}/9)"
            )
            return MarketState.TIGHT_CHANNEL
        
        # 下降 Tight Channel
        down_conditions_met = sum([condition_a_down, condition_b_down, condition_c_down])
        if condition_d:
            down_conditions_met += 1
        
        if down_conditions_met >= 2:
            logging.debug(
                f"🔒 Tight Channel(下降): EMA距离={condition_a_down}, "
                f"方向一致={condition_b_down}(阴线{bearish_bars}/5), "
                f"斜率={condition_c_down}({slope_pct:.2%}), "
                f"重叠度={condition_d}({overlap_count}/9)"
            )
            return MarketState.TIGHT_CHANNEL
        
        return None
    
    def calculate_tight_channel_score(self, df: pd.DataFrame, i: int, ema: float) -> float:
        """
        计算紧凑通道评分（0-1）
        
        评分因子：
        1. EMA距离因子（0-0.4）
        2. 方向一致性因子（0-0.3）
        3. 连续性因子（0-0.3）
        """
        if i < 10:
            return 0.0
        
        lookback_10 = df.iloc[max(0, i - 9) : i + 1]
        
        # 因子1: EMA距离因子
        total_distance = 0.0
        count = 0
        
        for idx in lookback_10.index:
            bar_ema = lookback_10.at[idx, "ema"] if "ema" in lookback_10.columns else ema
            bar_close = lookback_10.at[idx, "close"]
            distance_pct = abs(bar_close - bar_ema) / bar_ema
            total_distance += distance_pct
            count += 1
        
        avg_distance_pct = total_distance / count if count > 0 else 0
        ema_distance_score = min(avg_distance_pct / 0.01 * 0.4, 0.4)
        
        # 因子2: 方向一致性因子
        lookback_5 = df.iloc[max(0, i - 4) : i + 1]
        bullish_bars = sum(1 for idx in lookback_5.index 
                          if lookback_5.at[idx, "close"] > lookback_5.at[idx, "open"])
        bearish_bars = sum(1 for idx in lookback_5.index 
                          if lookback_5.at[idx, "close"] < lookback_5.at[idx, "open"])
        
        max_same_direction = max(bullish_bars, bearish_bars)
        direction_score = (max_same_direction / 5.0) * 0.3
        
        # 因子3: 连续性因子
        max_consecutive = 0
        current_consecutive = 1
        prev_direction = None
        
        for idx in lookback_10.index:
            bar_close = lookback_10.at[idx, "close"]
            bar_open = lookback_10.at[idx, "open"]
            current_direction = "bull" if bar_close > bar_open else "bear" if bar_close < bar_open else "doji"
            
            if prev_direction == current_direction and current_direction != "doji":
                current_consecutive += 1
                max_consecutive = max(max_consecutive, current_consecutive)
            else:
                current_consecutive = 1
            
            prev_direction = current_direction
        
        consecutive_score = min(max_consecutive / 10.0, 1.0) * 0.3
        
        return round(ema_distance_score + direction_score + consecutive_score, 3)
    
    def get_tight_channel_direction(self, df: pd.DataFrame, i: int) -> Optional[str]:
        """
        获取紧凑通道方向
        
        返回:
            "up": 上升紧凑通道
            "down": 下降紧凑通道
            None: 非紧凑通道
        """
        if i < 10:
            return None
        
        lookback_10 = df.iloc[max(0, i - 9) : i + 1]
        
        try:
            all_above_ema = all(lookback_10["low"] > lookback_10["ema"] * 0.999)
            all_below_ema = all(lookback_10["high"] < lookback_10["ema"] * 1.001)
            
            if all_above_ema:
                return "up"
            elif all_below_ema:
                return "down"
        except:
            pass
        
        return None
    
    def _detect_final_flag(self, df: pd.DataFrame, i: int, ema: float) -> Optional[MarketState]:
        """
        检测 Final Flag（终极旗形）- Al Brooks 高胜率反转形态
        
        Al Brooks: "Final Flag 是趋势耗尽的最后挣扎，通常出现在长时间趋势后的
        小幅横盘或回调中，是高胜率的反转入场点。"
        
        识别条件：
        1. 之前必须有至少 5 根连续的 TightChannel（强趋势）
        2. 当前价格处于横盘或小幅回调（非 TightChannel）
        3. 价格仍远离 EMA20（距离 > 1% = Climax 区域）
        4. 横盘/回调持续 3-8 根 K 线（旗形结构）
        
        返回:
            MarketState.FINAL_FLAG 或 None
        """
        MIN_TIGHT_CHANNEL_BARS = 5  # TightChannel 至少持续 5 根
        CLIMAX_DISTANCE_PCT = 0.01  # 价格与 EMA 距离 > 1% 视为 Climax 区域
        FLAG_MIN_BARS = 3  # 旗形最少 3 根
        FLAG_MAX_BARS = 8  # 旗形最多 8 根
        
        # 条件1：必须刚从至少 5 根的 TightChannel 退出
        if self._tight_channel_bars < MIN_TIGHT_CHANNEL_BARS:
            return None
        if self._last_tight_channel_end_bar is None:
            return None
        
        bars_since_tc_end = i - self._last_tight_channel_end_bar
        if bars_since_tc_end < FLAG_MIN_BARS or bars_since_tc_end > FLAG_MAX_BARS:
            return None
        
        # 条件2：价格仍远离 EMA（Climax 区域）
        current_close = float(df.iloc[i]["close"])
        distance_pct = (current_close - ema) / ema if ema > 0 else 0
        
        if self._tight_channel_direction == "up":
            # 上涨趋势后：价格应仍在 EMA 上方且距离 > 1%
            if distance_pct < CLIMAX_DISTANCE_PCT:
                return None
        elif self._tight_channel_direction == "down":
            # 下跌趋势后：价格应仍在 EMA 下方且距离 > 1%
            if distance_pct > -CLIMAX_DISTANCE_PCT:
                return None
        else:
            return None
        
        # 条件3：当前处于横盘或小幅回调（旗形结构）
        # 检查自 TightChannel 结束以来的波动幅度
        flag_start = self._last_tight_channel_end_bar + 1
        if flag_start >= len(df):
            return None
        flag_data = df.iloc[flag_start : i + 1]
        if len(flag_data) < FLAG_MIN_BARS:
            return None
        
        flag_high = float(flag_data["high"].max())
        flag_low = float(flag_data["low"].min())
        flag_range = flag_high - flag_low
        
        # 旗形波动幅度应小于之前 TightChannel 的 50%
        # 用 ATR 或极值来估算 TightChannel 的波动
        if self._tight_channel_extreme is not None:
            if self._tight_channel_direction == "up":
                tc_range = self._tight_channel_extreme - ema
            else:
                tc_range = ema - self._tight_channel_extreme
            
            if tc_range > 0 and flag_range > tc_range * 0.5:
                # 回调幅度过大，不是旗形
                return None
        
        # 条件4：旗形内没有强力突破（保持横盘特征）
        if "body_size" in flag_data.columns:
            avg_body = float(flag_data["body_size"].mean())
            max_body = float(flag_data["body_size"].max())
            if avg_body > 0 and max_body > avg_body * 2.5:
                # 旗形内有强力 K 线，不是典型旗形
                return None
        
        logging.debug(
            f"🏁 检测到 Final Flag: 方向={self._tight_channel_direction}, "
            f"TightChannel持续={self._tight_channel_bars}根, "
            f"旗形持续={bars_since_tc_end}根, "
            f"EMA距离={distance_pct:.2%}, 旗形波幅={flag_range:.2f}"
        )
        
        return MarketState.FINAL_FLAG
    
    def get_final_flag_info(self) -> dict:
        """
        获取 Final Flag 相关信息（供 patterns 检测使用）
        
        返回:
            dict: {
                'direction': 'up'/'down',  # 之前趋势方向（反转方向相反）
                'extreme': float,  # TightChannel 的极值（做空止损位/做多止损位）
                'tc_bars': int,  # TightChannel 持续根数
            }
        """
        return {
            'direction': self._tight_channel_direction,
            'extreme': self._tight_channel_extreme,
            'tc_bars': self._tight_channel_bars,
            'tc_end_bar': self._last_tight_channel_end_bar,
        }

    def get_always_in_direction(
        self, df: pd.DataFrame, i: int, ema: float, market_cycle: MarketCycle
    ) -> AlwaysInDirection:
        """
        判断当前 Always In 方向
        
        Al Brooks: "在任何给定时刻，市场都处于多头或空头的控制之下"
        
        判断逻辑：
        1. SPIKE 周期：由 Spike 方向决定（价格在 EMA 上方做多，下方做空）
        2. TIGHT_CHANNEL：由 TightChannel 方向决定
        3. 其他情况：根据趋势方向和强度判断
        
        Args:
            df: K线数据
            i: 当前索引
            ema: EMA值
            market_cycle: 市场周期
        
        Returns:
            AlwaysInDirection: LONG/SHORT/NEUTRAL
        """
        # ========== 1. SPIKE 周期：强烈的 Always In ==========
        if market_cycle == MarketCycle.SPIKE:
            current_close = float(df.iloc[i]["close"])
            if current_close > ema:
                return AlwaysInDirection.LONG
            elif current_close < ema:
                return AlwaysInDirection.SHORT
            return AlwaysInDirection.NEUTRAL
        
        # ========== 2. Tight Channel：由通道方向决定 ==========
        if self._tight_channel_direction is not None:
            if self._tight_channel_direction == "up":
                return AlwaysInDirection.LONG
            elif self._tight_channel_direction == "down":
                return AlwaysInDirection.SHORT
        
        # ========== 3. 强趋势：由趋势方向决定 ==========
        if self._trend_strength >= self._params.strong_trend_threshold:
            if self._trend_direction == "up":
                return AlwaysInDirection.LONG
            elif self._trend_direction == "down":
                return AlwaysInDirection.SHORT
        
        # ========== 4. 其他情况：NEUTRAL ==========
        return AlwaysInDirection.NEUTRAL
