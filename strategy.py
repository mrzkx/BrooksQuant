"""
Al Brooks 价格行为策略 - 核心入口

整合四大阿布价格行为策略（异步版本）：
1. Strong Spike - 强突破直接入场
2. H2/L2 Pullback - 通道回调策略
3. Failed Breakout - 失败突破反转策略
4. Wedge Reversal - 楔形反转策略

模块化架构：
- logic/market_analyzer.py: 市场状态识别
- logic/patterns.py: 模式检测
- logic/state_machines.py: H2/L2 状态机

订单流过滤：
- delta_flow.py: 动态订单流 Delta 分析（替代静态 OBI）
"""

import json
import logging
import pandas as pd
from typing import List, Optional, Tuple, Dict

import redis.asyncio as aioredis

# 导入模块化组件
from logic.market_analyzer import MarketState, MarketAnalyzer
from logic.patterns import PatternDetector
from logic.state_machines import HState, LState, H2StateMachine, L2StateMachine
from logic.interval_params import get_interval_params, IntervalParams
from logic.htf_filter import get_htf_filter, HTFFilter, HTFTrend
from logic.talib_patterns import (
    get_talib_detector, 
    calculate_talib_boost,
    TALibPatternDetector,
    TALIB_AVAILABLE,
)
from logic.talib_indicators import compute_ema, compute_atr

# 导入动态订单流模块
from delta_flow import (
    DeltaAnalyzer, 
    DeltaSnapshot, 
    DeltaTrend,
    DeltaSignalModifier, 
    get_delta_analyzer
)


class AlBrooksStrategy:
    """
    Al Brooks 价格行为策略（异步版本）- 优化版
    
    通过组合各模块实现完整的交易信号生成
    
    订单流过滤：
    - 使用动态订单流 Delta 分析（基于 aggTrade）替代静态 OBI
    - Delta 分析能够区分：主动买入、主动卖出、流动性撤离、吸收
    - Delta 窗口与 K 线周期对齐，确保信号同步
    
    优化措施：
    - 信号冷却期：同一类型信号至少间隔 5 根 K 线
    - 严格逆势过滤：StrongTrend 中完全禁止逆势交易
    - 收紧 Spike 条件：3 根同向 K 线 + 3 倍平均实体 + 突破确认
    """

    def __init__(
        self, 
        ema_period: int = 20, 
        lookback_period: int = 20, 
        redis_url: Optional[str] = None,
        kline_interval: str = "5m"
    ):
        self.ema_period = ema_period
        self.lookback_period = lookback_period
        self.kline_interval = kline_interval
        
        # 加载周期自适应参数
        self._params: IntervalParams = get_interval_params(kline_interval)
        
        # 初始化模块化组件（传入周期参数）
        self.market_analyzer = MarketAnalyzer(
            ema_period=ema_period, 
            kline_interval=kline_interval
        )
        self.pattern_detector = PatternDetector(
            lookback_period=lookback_period,
            kline_interval=kline_interval
        )
        
        # 信号冷却期管理（周期自适应）
        self.SIGNAL_COOLDOWN_BARS = self._params.signal_cooldown_bars
        self._last_signal_bar: Dict[str, int] = {}  # {"Spike_Buy": 100, "Spike_Sell": 95, ...}
        
        # Redis 客户端（用于 Delta 数据缓存，可选）
        self.redis_client: Optional[aioredis.Redis] = None
        self.redis_url = redis_url
        self._redis_connected = False
        
        # Delta 分析器（从全局获取，与 aggtrade_worker 共享，窗口与 K 线周期对齐）
        self.delta_analyzer: DeltaAnalyzer = get_delta_analyzer(kline_interval=kline_interval)
        
        # HTF 过滤器（1h EMA20 方向过滤）
        # Al Brooks: "大周期的趋势是日内交易最好的保护伞"
        self.htf_filter: HTFFilter = get_htf_filter(htf_interval="1h", ema_period=20)
        
        # TA-Lib 形态检测器（信号增强器）
        # 当 TA-Lib 形态与 PA 信号重合时，给予置信度加成
        self.talib_detector: Optional[TALibPatternDetector] = None
        if TALIB_AVAILABLE:
            self.talib_detector = get_talib_detector()
            logging.info("📊 TA-Lib 形态检测器已启用")
        else:
            logging.warning("⚠️ TA-Lib 不可用，形态增强功能已禁用")
        
        logging.info(
            f"策略已初始化: EMA周期={ema_period}, K线周期={kline_interval}, "
            f"Delta窗口={self.delta_analyzer.WINDOW_SECONDS}秒, "
            f"信号冷却={self.SIGNAL_COOLDOWN_BARS}根K线, "
            f"HTF过滤=1h EMA20, TA-Lib={'启用' if TALIB_AVAILABLE else '禁用'}"
        )
    
    def _is_signal_in_cooldown(self, signal_type: str, current_bar: int) -> bool:
        """检查信号是否在冷却期内"""
        last_bar = self._last_signal_bar.get(signal_type)
        if last_bar is None:
            return False
        return (current_bar - last_bar) < self.SIGNAL_COOLDOWN_BARS
    
    def _update_signal_cooldown(self, signal_type: str, current_bar: int) -> None:
        """更新信号冷却期记录"""
        self._last_signal_bar[signal_type] = current_bar
    
    async def connect_redis(self) -> bool:
        """异步连接 Redis（可选，用于 Delta 数据缓存）"""
        if not self.redis_url:
            logging.info("✅ 策略已初始化（Delta 分析使用内存模式）")
            return False
        
        try:
            self.redis_client = await aioredis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=2,
            )
            await self.redis_client.ping()
            self._redis_connected = True
            logging.info("✅ 策略已连接 Redis（用于 Delta 缓存）")
            return True
        except Exception as e:
            logging.warning(f"⚠️ 策略无法连接 Redis: {e}，Delta 数据将使用内存模式")
            self.redis_client = None
            self._redis_connected = False
            return False
    
    async def close_redis(self):
        """关闭 Redis 连接"""
        if self.redis_client:
            try:
                await self.redis_client.aclose()
            except:
                pass
            self.redis_client = None
            self._redis_connected = False

    def _compute_ema(self, df: pd.DataFrame) -> pd.Series:
        """计算 EMA (使用 TA-Lib)"""
        return compute_ema(df["close"], self.ema_period)

    def _compute_atr(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        """计算 ATR (使用 TA-Lib)"""
        return compute_atr(df["high"], df["low"], df["close"], period)
    
    async def _get_delta_snapshot(self, symbol: str = "BTCUSDT") -> Optional[DeltaSnapshot]:
        """
        获取动态订单流 Delta 快照
        
        优先从内存获取（与 aggtrade_worker 共享），
        如果 Redis 可用也可以从 Redis 获取备用数据。
        
        Returns:
            DeltaSnapshot: 包含 Delta 分析结果的快照
        """
        # 优先从全局 Delta 分析器获取（实时数据）
        try:
            snapshot = await self.delta_analyzer.get_snapshot(symbol)
            if snapshot.trade_count > 0:
                return snapshot
        except Exception as e:
            logging.debug(f"从 Delta 分析器获取快照失败: {e}")
        
        # 备用：从 Redis 获取缓存数据
        if self.redis_client and self._redis_connected:
            try:
                data = await self.redis_client.get(f"cache:delta:{symbol}")
                if data:
                    parsed = json.loads(data)
                    return DeltaSnapshot(
                        cumulative_delta=parsed.get("cumulative_delta", 0.0),
                        buy_volume=parsed.get("buy_volume", 0.0),
                        sell_volume=parsed.get("sell_volume", 0.0),
                        delta_ratio=parsed.get("delta_ratio", 0.0),
                        delta_avg=parsed.get("delta_avg", 0.0),
                        delta_acceleration=parsed.get("delta_acceleration", 0.0),
                        delta_trend=DeltaTrend(parsed.get("delta_trend", "neutral")),
                        is_absorption=parsed.get("is_absorption", False),
                        is_climax_buy=parsed.get("is_climax_buy", False),
                        is_climax_sell=parsed.get("is_climax_sell", False),
                        trade_count=parsed.get("trade_count", 0),
                        timestamp=parsed.get("timestamp", 0),
                    )
            except Exception as e:
                logging.debug(f"从 Redis 获取 Delta 缓存失败: {e}")
        
        return None
    
    def _calculate_delta_signal_modifier(
        self, snapshot: DeltaSnapshot, side: str, price_change_pct: float = 0.0
    ) -> Tuple[float, str]:
        """
        计算动态订单流 Delta 对信号的调节作用
        
        核心逻辑（基于 Al Brooks 价格行为）：
        
        1. 买单吃进 (Aggressive Buying)：
           - Delta 为正且趋势看涨 → 增强买入信号
           - 这是真正的"Spike"，有机构资金支撑
        
        2. 卖单撤离 (Liquidity Withdrawal)：
           - 价格上涨但 Delta 不匹配 → 减弱买入信号
           - 这是"假突破"的典型特征
        
        3. 吸收 (Absorption)：
           - Delta 很大但价格不动 → 强烈减弱信号
           - 隐藏的大单在悄悄出货/吸筹
        
        Returns:
            (modifier, reason)
            - modifier > 1.0: 增强信号（订单流确认）
            - modifier = 1.0: 不调整
            - modifier < 1.0: 减弱信号（订单流不支持）
            - modifier = 0.0: 阻止信号（强烈反向订单流）
        """
        return DeltaSignalModifier.calculate_modifier(snapshot, side, price_change_pct)
    
    # Al Brooks 风格：根据信号类型的动态盈亏比
    # 高胜率信号用较低盈亏比，低胜率信号需要更高盈亏比
    SIGNAL_RR_RATIO = {
        # Spike 信号：低胜率（40-50%），需要高盈亏比
        "Spike_Buy": {"tp1_r": 1.0, "tp2_r": 2.5},
        "Spike_Sell": {"tp1_r": 1.0, "tp2_r": 2.5},
        
        # FailedBreakout：高胜率（60-70%），可用较低盈亏比
        "FailedBreakout_Buy": {"tp1_r": 0.8, "tp2_r": 1.5},
        "FailedBreakout_Sell": {"tp1_r": 0.8, "tp2_r": 1.5},
        
        # Climax 反转：低胜率（35-45%），需要高盈亏比
        "Climax_Buy": {"tp1_r": 1.2, "tp2_r": 3.0},
        "Climax_Sell": {"tp1_r": 1.2, "tp2_r": 3.0},
        
        # Wedge 反转：中等胜率（40-50%）
        "Wedge_Buy": {"tp1_r": 1.0, "tp2_r": 2.5},
        "Wedge_Sell": {"tp1_r": 1.0, "tp2_r": 2.5},
        
        # H2/L2 回调：中高胜率（50-60%）
        "H2_Buy": {"tp1_r": 0.8, "tp2_r": 2.0},
        "L2_Sell": {"tp1_r": 0.8, "tp2_r": 2.0},
        "H1_Buy": {"tp1_r": 0.8, "tp2_r": 1.8},
        "L1_Sell": {"tp1_r": 0.8, "tp2_r": 1.8},
    }
    
    def detect_climax_signal_bar(
        self, df: pd.DataFrame, i: int, multiplier: float = 3.0
    ) -> Tuple[bool, float]:
        """
        检测 Climax 信号棒（大炮冲刺）
        
        Al Brooks: "Climax 是市场极端情绪的表现，通常预示着反转或调整"
        
        条件：Signal Bar 长度超过过去 10 根 K 线平均长度的 multiplier 倍
        
        Args:
            df: K 线数据
            i: 当前索引
            multiplier: 倍数阈值（默认 3.0）
        
        Returns:
            (is_climax, bar_ratio): 是否是 Climax，以及相对倍数
        """
        if i < 10:
            return (False, 1.0)
        
        # 计算过去 10 根 K 线的平均长度
        lookback = df.iloc[max(0, i - 10):i]
        avg_range = (lookback["high"] - lookback["low"]).mean()
        
        if avg_range <= 0:
            return (False, 1.0)
        
        # 当前 K 线长度
        current_range = df.iloc[i]["high"] - df.iloc[i]["low"]
        bar_ratio = current_range / avg_range
        
        is_climax = bar_ratio >= multiplier
        
        return (is_climax, bar_ratio)
    
    def _calculate_tp1_tp2(
        self, entry_price: float, stop_loss: float, side: str, 
        base_height: float, atr: Optional[float] = None,
        signal_type: Optional[str] = None,
        market_state: Optional[str] = None,
        df: Optional[pd.DataFrame] = None,
        current_idx: Optional[int] = None,
    ) -> Tuple[float, float, float, bool]:
        """
        Al Brooks 风格分批止盈目标位（动态分时出场版）
        
        根据市场状态动态调整 TP2：
        - TightChannel: TP2 延长至 RR 3:1（让利润奔跑）
        - TradingRange: TP2 严格限制在区间边缘（早点出场）
        - 其他状态: 标准盈亏比
        
        Climax 信号棒处理：
        - 检测到 Climax（信号棒 > 3x 平均长度）
        - 调低盈亏比（预期回调）
        - TP1 平仓比例从 50% 提高到 75%
        
        Returns:
            (tp1, tp2, tp1_close_ratio, is_climax)
        """
        risk = abs(entry_price - stop_loss)
        
        # 周期自适应默认盈亏比
        default_rr = {
            "tp1_r": self._params.default_tp1_r, 
            "tp2_r": self._params.default_tp2_r
        }
        
        # 获取该信号类型的盈亏比
        rr_config = self.SIGNAL_RR_RATIO.get(signal_type, default_rr)
        tp1_multiplier = rr_config["tp1_r"]
        tp2_multiplier = rr_config["tp2_r"]
        
        # 默认 TP1 平仓比例
        tp1_close_ratio = 0.5
        is_climax = False
        
        # ========== Climax 信号棒检测 ==========
        # Al Brooks: "Climax 后通常有回调，要保守出场"
        if df is not None and current_idx is not None:
            is_climax, bar_ratio = self.detect_climax_signal_bar(df, current_idx, multiplier=3.0)
            
            if is_climax:
                # Climax 时：
                # 1. 调低 TP2 倍数（预期回调，不要贪心）
                tp2_multiplier = min(tp2_multiplier, 1.5)
                # 2. TP1 平仓 75%（早点锁定利润）
                tp1_close_ratio = 0.75
                logging.debug(
                    f"📊 Climax 信号棒检测: 长度={bar_ratio:.1f}x平均, "
                    f"TP2调整为{tp2_multiplier}R, TP1平仓{tp1_close_ratio*100:.0f}%"
                )
        
        # ========== 市场状态动态调整 TP2 ==========
        # Al Brooks 分时出场原则
        if market_state == "TightChannel" and not is_climax:
            # TightChannel: 趋势强劲，让利润奔跑
            tp2_multiplier = max(tp2_multiplier, 3.0)  # 至少 RR 3:1
            logging.debug(f"🔒 TightChannel: TP2 延长至 {tp2_multiplier}R")
        
        elif market_state == "TradingRange":
            # TradingRange: 区间震荡，严格限制在区间边缘
            # 使用 base_height（区间宽度）而非固定倍数
            if base_height > 0 and base_height < risk * tp2_multiplier:
                tp2_multiplier = base_height / risk if risk > 0 else tp2_multiplier
                tp2_multiplier = max(tp2_multiplier, 1.2)  # 最低 RR 1.2:1
                logging.debug(f"📦 TradingRange: TP2 限制在区间边缘 {tp2_multiplier:.1f}R")
        
        # ========== 计算 TP1 和 TP2 ==========
        if side == "buy":
            tp1 = entry_price + (risk * tp1_multiplier)
            
            # TP2: 取 Measured Move 和 R 倍数中较大者
            measured_move = entry_price + base_height if base_height > 0 else entry_price + (risk * tp2_multiplier)
            tp2 = max(measured_move, entry_price + (risk * tp2_multiplier))
            
            # TradingRange 时强制限制
            if market_state == "TradingRange" and base_height > 0:
                tp2 = min(tp2, entry_price + base_height)
            
            # 如果 base_height 太小，使用更保守的目标
            if base_height > 0 and base_height < risk * 1.5 and market_state != "TradingRange":
                tp2 = max(tp2, entry_price + (risk * (tp2_multiplier + 0.5)))
        else:
            tp1 = entry_price - (risk * tp1_multiplier)
            
            # TP2: 取 Measured Move 和 R 倍数中较大者
            measured_move = entry_price - base_height if base_height > 0 else entry_price - (risk * tp2_multiplier)
            tp2 = min(measured_move, entry_price - (risk * tp2_multiplier))
            
            # TradingRange 时强制限制
            if market_state == "TradingRange" and base_height > 0:
                tp2 = max(tp2, entry_price - base_height)
            
            # 如果 base_height 太小，使用更保守的目标
            if base_height > 0 and base_height < risk * 1.5 and market_state != "TradingRange":
                tp2 = min(tp2, entry_price - (risk * (tp2_multiplier + 0.5)))
        
        return (tp1, tp2, tp1_close_ratio, is_climax)

    async def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        异步生成交易信号
        
        返回包含信号的 DataFrame:
        - ema, atr: 技术指标
        - market_state: 市场状态
        - signal, side: 交易信号
        - stop_loss, risk_reward_ratio: 风险管理
        - base_height, tp1_price, tp2_price: 止盈目标
        - tight_channel_score: 紧凑通道评分
        - delta_modifier: Delta调节因子 (>1增强, <1减弱, None未启用)
          基于动态订单流分析（aggTrade），可识别：
          - 主动买入/卖出（真实突破）
          - 流动性撤离（假突破）
          - 吸收（隐藏大单出货/吸筹）
        """
        data = df.copy()
        
        # ========== 向量化预计算（避免循环中重复计算）==========
        # 确保数据类型为 float
        for col in ["open", "high", "low", "close"]:
            data[col] = data[col].astype(float)
        
        # 技术指标（TA-Lib）
        data["ema"] = self._compute_ema(data)
        if len(data) >= 20:
            data["atr"] = self._compute_atr(data, period=20)
        else:
            data["atr"] = data["high"] - data["low"]  # 用波幅代替
        
        # 基础向量化计算
        data["body_size"] = (data["close"] - data["open"]).abs()
        data["kline_range"] = data["high"] - data["low"]
        data["is_bullish"] = data["close"] > data["open"]
        data["is_bearish"] = data["close"] < data["open"]
        
        # 避免除零
        data["body_ratio"] = data["body_size"] / data["kline_range"].replace(0, float('nan'))
        data["body_ratio"] = data["body_ratio"].fillna(0)
        
        # 价格与 EMA 关系
        data["above_ema"] = data["close"] > data["ema"]
        data["ema_distance"] = (data["close"] - data["ema"]).abs()
        data["ema_distance_pct"] = data["ema_distance"] / data["ema"]
        
        # EMA 穿越检测（向量化）
        data["ema_cross"] = data["above_ema"].astype(int).diff().abs()
        
        # 滚动计算（用于 Spike/Climax 检测）
        data["body_size_ma10"] = data["body_size"].rolling(window=10, min_periods=1).mean()
        data["kline_range_ma10"] = data["kline_range"].rolling(window=10, min_periods=1).mean()

        # 初始化结果列表
        signals: List[Optional[str]] = [None] * len(data)
        sides: List[Optional[str]] = [None] * len(data)
        stops: List[Optional[float]] = [None] * len(data)
        market_states: List[Optional[str]] = [None] * len(data)
        risk_reward_ratios: List[Optional[float]] = [None] * len(data)
        base_heights: List[Optional[float]] = [None] * len(data)
        tp1_prices: List[Optional[float]] = [None] * len(data)
        tp2_prices: List[Optional[float]] = [None] * len(data)
        tight_channel_scores: List[Optional[float]] = [None] * len(data)
        delta_modifiers: List[Optional[float]] = [None] * len(data)  # Delta调节因子
        tp1_close_ratios: List[Optional[float]] = [None] * len(data)  # TP1 平仓比例
        is_climax_bars: List[Optional[bool]] = [None] * len(data)  # Climax 信号棒标记
        talib_boosts: List[Optional[float]] = [None] * len(data)  # TA-Lib 形态加成
        talib_patterns: List[Optional[str]] = [None] * len(data)  # 匹配的 TA-Lib 形态

        # Spike 回撤入场状态
        pending_spike: Optional[Tuple[str, str, float, float, float, int]] = None

        # H2/L2 状态机
        h2_machine = H2StateMachine()
        l2_machine = L2StateMachine()
        
        # ========== 缓存快照（避免循环中重复获取）==========
        # HTF 快照（1h 级别，整个 5m 循环中不变）
        cached_htf_snapshot = self.htf_filter.get_snapshot()
        cached_htf_trend = cached_htf_snapshot.trend if cached_htf_snapshot else HTFTrend.NEUTRAL
        cached_htf_allow_buy = cached_htf_snapshot.allow_buy if cached_htf_snapshot else True
        cached_htf_allow_sell = cached_htf_snapshot.allow_sell if cached_htf_snapshot else True
        
        # Delta 快照缓存（同一次 generate_signals 调用中只获取一次）
        cached_delta_snapshot: Optional[DeltaSnapshot] = None
        delta_snapshot_fetched = False

        for i in range(1, len(data)):
            row = data.iloc[i]
            close, high, low = row["close"], row["high"], row["low"]
            ema = row["ema"]
            atr = row["atr"] if "atr" in data.columns else None
            
            # 只在处理最新 K 线时打印日志（避免历史数据重复打印）
            is_latest_bar = (i == len(data) - 1)

            # 检测市场状态
            market_state = self.market_analyzer.detect_market_state(data, i, ema)
            market_states[i] = market_state.value
            
            # 获取趋势方向和强度（用于逆势交易过滤）
            trend_direction = self.market_analyzer.get_trend_direction()
            trend_strength = self.market_analyzer.get_trend_strength()
            
            # 计算紧凑通道评分
            tight_channel_scores[i] = self.market_analyzer.calculate_tight_channel_score(data, i, ema)
            
            # 紧凑通道方向
            tight_channel_direction = None
            if market_state == MarketState.TIGHT_CHANNEL:
                tight_channel_direction = self.market_analyzer.get_tight_channel_direction(data, i)
            
            # ========== Al Brooks 核心：强趋势模式判断 ==========
            # 在 TIGHT_CHANNEL 或 STRONG_TREND 中，完全禁止反转，只允许顺势
            is_strong_trend_mode = (
                market_state == MarketState.TIGHT_CHANNEL or 
                market_state == MarketState.STRONG_TREND or
                trend_strength >= 0.7
            )
            
            # 确定允许的交易方向（None = 任意方向，"buy" = 只做多，"sell" = 只做空）
            allowed_side: Optional[str] = None
            if is_strong_trend_mode:
                if tight_channel_direction == "up" or trend_direction == "up":
                    allowed_side = "buy"  # 上升趋势只允许做多
                elif tight_channel_direction == "down" or trend_direction == "down":
                    allowed_side = "sell"  # 下降趋势只允许做空

            # 处理待处理的 Spike 回撤入场
            if pending_spike is not None:
                signal_type, side, stop_loss, limit_price, base_height, spike_idx = pending_spike

                if side == "buy" and low <= limit_price:
                    signals[i] = signal_type
                    sides[i] = side
                    stops[i] = stop_loss
                    base_heights[i] = base_height
                    risk_reward_ratios[i] = 2.0
                    tp1, tp2, tp1_ratio, is_climax = self._calculate_tp1_tp2(
                        limit_price, stop_loss, side, base_height, atr, signal_type,
                        market_state.value, data, i
                    )
                    tp1_prices[i], tp2_prices[i] = tp1, tp2
                    tp1_close_ratios[i] = tp1_ratio
                    is_climax_bars[i] = is_climax
                    pending_spike = None
                    h2_machine.set_strong_trend()
                    continue
                elif side == "sell" and high >= limit_price:
                    signals[i] = signal_type
                    sides[i] = side
                    stops[i] = stop_loss
                    base_heights[i] = base_height
                    risk_reward_ratios[i] = 2.0
                    tp1, tp2, tp1_ratio, is_climax = self._calculate_tp1_tp2(
                        limit_price, stop_loss, side, base_height, atr, signal_type,
                        market_state.value, data, i
                    )
                    tp1_prices[i], tp2_prices[i] = tp1, tp2
                    tp1_close_ratios[i] = tp1_ratio
                    is_climax_bars[i] = is_climax
                    pending_spike = None
                    l2_machine.set_strong_trend()
                    continue
                elif (side == "buy" and close > limit_price * 1.05) or (side == "sell" and close < limit_price * 0.95):
                    pending_spike = None
                elif i - spike_idx > 5:
                    pending_spike = None

            # 优先级1: Failed Breakout（区间策略最高优先级）
            # ⭐ Al Brooks: FailedBreakout 是反转信号，在强趋势中完全禁止
            if market_state == MarketState.TRADING_RANGE and not is_strong_trend_mode:
                result = self.pattern_detector.detect_failed_breakout(data, i, ema, atr, market_state)
                if result:
                    signal_type, side, stop_loss, base_height = result
                    
                    # 检查是否符合允许的方向
                    if allowed_side is not None and side != allowed_side:
                        if is_latest_bar:
                            logging.info(
                                f"🚫 强趋势禁止反转: {signal_type} {side} - "
                                f"趋势={trend_direction}(强度={trend_strength:.2f})，只允许{allowed_side}"
                            )
                        continue
                    
                    signals[i] = signal_type
                    sides[i] = side
                    stops[i] = stop_loss
                    base_heights[i] = base_height
                    risk_reward_ratios[i] = 1.0
                    tp1, tp2, tp1_ratio, is_climax = self._calculate_tp1_tp2(
                        close, stop_loss, side, base_height, atr, signal_type,
                        market_state.value, data, i
                    )
                    tp1_prices[i], tp2_prices[i] = tp1, tp2
                    tp1_close_ratios[i] = tp1_ratio
                    is_climax_bars[i] = is_climax
                    continue

            # 优先级2: Strong Spike
            # ⭐ Spike 是顺势信号，在强趋势中只允许顺势方向
            spike_result = self.pattern_detector.detect_strong_spike(data, i, ema, atr, market_state)
            if spike_result:
                signal_type, side, stop_loss, limit_price, base_height = spike_result
                
                # ⭐ 新增：信号冷却期检查（同一类型信号至少间隔 5 根 K 线）
                if self._is_signal_in_cooldown(signal_type, i):
                    if is_latest_bar:
                        logging.debug(f"⏳ 信号冷却中: {signal_type} (需间隔 {self.SIGNAL_COOLDOWN_BARS} 根K线)")
                    continue
                
                # ⭐ 新增：严格逆势过滤 - StrongTrend 中完全禁止逆势
                # 即使趋势强度不足 0.7，只要是 StrongTrend 状态也禁止
                if market_state == MarketState.STRONG_TREND:
                    if trend_direction == "up" and side == "sell":
                        if is_latest_bar:
                            logging.info(f"🚫 StrongTrend禁止做空: {signal_type} - 上涨趋势中禁止卖出")
                        continue
                    if trend_direction == "down" and side == "buy":
                        if is_latest_bar:
                            logging.info(f"🚫 StrongTrend禁止做多: {signal_type} - 下跌趋势中禁止买入")
                        continue
                
                # 检查是否符合允许的方向
                if allowed_side is not None and side != allowed_side:
                    if is_latest_bar:
                        logging.info(
                            f"🚫 强趋势只顺势: {signal_type} {side} 被禁止 - "
                            f"趋势={trend_direction}，只允许{allowed_side}"
                        )
                    continue

                if limit_price is not None:
                    pending_spike = (signal_type, side, stop_loss, limit_price, base_height, i)
                else:
                    # 动态订单流 Delta 过滤（替代静态 OBI）
                    delta_modifier = 1.0
                    delta_reason = "Delta未启用"
                    
                    # 计算 K 线价格变化百分比
                    kline_open = data.iloc[i]["open"]
                    price_change_pct = ((close - kline_open) / kline_open * 100) if kline_open > 0 else 0.0
                    
                    if market_state == MarketState.BREAKOUT:
                        # 只在最新 K 线时获取 Delta 快照（历史数据无需获取）
                        if is_latest_bar and not delta_snapshot_fetched:
                            cached_delta_snapshot = await self._get_delta_snapshot("BTCUSDT")
                            delta_snapshot_fetched = True
                        delta_snapshot = cached_delta_snapshot if is_latest_bar else None
                        if delta_snapshot is not None and delta_snapshot.trade_count > 0:
                            delta_modifier, delta_reason = self._calculate_delta_signal_modifier(
                                delta_snapshot, side, price_change_pct
                            )
                            
                            # 只在最新K线打印Delta日志
                            if is_latest_bar:
                                if delta_modifier == 0.0:
                                    logging.info(f"🚫 Delta阻止: {signal_type} {side} - {delta_reason}")
                                elif delta_modifier < 1.0:
                                    logging.info(f"⚠️ Delta减弱: {signal_type} {side} (调节={delta_modifier:.2f}) - {delta_reason}")
                                elif delta_modifier > 1.0:
                                    logging.info(f"✅ Delta增强: {signal_type} {side} (调节={delta_modifier:.2f}) - {delta_reason}")
                    
                    if delta_modifier > 0:
                        signals[i] = signal_type
                        sides[i] = side
                        stops[i] = stop_loss
                        base_heights[i] = base_height
                        risk_reward_ratios[i] = 2.0
                        delta_modifiers[i] = delta_modifier  # 记录Delta调节因子
                        tp1, tp2, tp1_ratio, is_climax = self._calculate_tp1_tp2(
                            close, stop_loss, side, base_height, atr, signal_type,
                            market_state.value, data, i
                        )
                        tp1_prices[i], tp2_prices[i] = tp1, tp2
                        tp1_close_ratios[i] = tp1_ratio
                        is_climax_bars[i] = is_climax
                        # 更新信号冷却期
                        self._update_signal_cooldown(signal_type, i)
                        if side == "buy":
                            h2_machine.set_strong_trend()
                        else:
                            l2_machine.set_strong_trend()
                continue

            # 优先级3: Climax 反转
            # ⭐ Al Brooks: Climax 是反转信号，在强趋势中完全禁止
            # "在紧凑通道中做反转是自杀行为" - Al Brooks
            if is_strong_trend_mode:
                # 强趋势模式：完全跳过 Climax 反转检测
                pass
            else:
                climax_result = self.pattern_detector.detect_climax_reversal(data, i, ema, atr)
                if climax_result:
                    signal_type, side, stop_loss, base_height = climax_result
                    
                    # 检查是否符合允许的方向
                    if allowed_side is not None and side != allowed_side:
                        if is_latest_bar:
                            logging.info(
                                f"🚫 强趋势禁止反转: {signal_type} {side} - "
                                f"趋势={trend_direction}，Climax反转在强趋势中胜率<20%"
                            )
                        continue
                    
                    signals[i] = signal_type
                    sides[i] = side
                    stops[i] = stop_loss
                    base_heights[i] = base_height
                    risk_reward_ratios[i] = 2.0
                    tp1, tp2, tp1_ratio, is_climax = self._calculate_tp1_tp2(
                        close, stop_loss, side, base_height, atr, signal_type,
                        market_state.value, data, i
                    )
                    tp1_prices[i], tp2_prices[i] = tp1, tp2
                    tp1_close_ratios[i] = tp1_ratio
                    is_climax_bars[i] = is_climax
                    continue

            # 优先级4: Wedge 反转
            # ⭐ Al Brooks: Wedge 是反转信号，在强趋势中完全禁止
            if is_strong_trend_mode:
                # 强趋势模式：完全跳过 Wedge 反转检测
                pass
            else:
                wedge_result = self.pattern_detector.detect_wedge_reversal(data, i, ema, atr)
                if wedge_result:
                    signal_type, side, stop_loss, base_height = wedge_result
                    
                    # 检查是否符合允许的方向
                    if allowed_side is not None and side != allowed_side:
                        if is_latest_bar:
                            logging.info(
                                f"🚫 强趋势禁止反转: {signal_type} {side} - "
                                f"趋势={trend_direction}，Wedge反转在强趋势中胜率<15%"
                            )
                        continue
                    
                    signals[i] = signal_type
                    sides[i] = side
                    stops[i] = stop_loss
                    base_heights[i] = base_height
                    risk_reward_ratios[i] = 2.0
                    tp1, tp2, tp1_ratio, is_climax = self._calculate_tp1_tp2(
                        close, stop_loss, side, base_height, atr, signal_type,
                        market_state.value, data, i
                    )
                    tp1_prices[i], tp2_prices[i] = tp1, tp2
                    tp1_close_ratios[i] = tp1_ratio
                    is_climax_bars[i] = is_climax
                    continue

            # H2/L2 状态机更新
            # ========== 多周期分析：1h EMA20 方向过滤 ==========
            # Al Brooks: "大周期的趋势是日内交易最好的保护伞"
            # 使用缓存的 HTF 快照（避免每次循环都调用）
            htf_trend = cached_htf_trend
            
            # ⭐ H2 是顺势做多信号
            # 条件：本周期允许买入 + HTF 不是下降趋势
            htf_allow_buy, htf_buy_reason = self.htf_filter.should_allow_signal("buy")
            
            if (allowed_side is None or allowed_side == "buy") and htf_allow_buy:
                h2_signal = h2_machine.update(
                    close, high, low, ema, atr, data, i,
                    self.pattern_detector.calculate_unified_stop_loss
                )
                if h2_signal:
                    # ========== BTC 高波动过滤1: 信号棒质量验证 ==========
                    # Al Brooks: "信号棒的质量决定了交易的成功率"
                    # BTC 长影线多，要求实体占全长 60%+，收盘在顶部 20% 区域
                    bar_valid, bar_reason = self.pattern_detector.validate_btc_signal_bar(
                        data.iloc[i], h2_signal.side
                    )
                    if not bar_valid:
                        if is_latest_bar:
                            logging.info(
                                f"🚫 H2信号棒质量不合格: {h2_signal.signal_type} - {bar_reason}"
                            )
                        # 信号棒不合格，跳过此信号
                    else:
                        # ========== BTC 高波动过滤2: Delta 方向一致性验证 ==========
                        # Al Brooks: "入场棒的 Delta 方向必须与信号方向一致"
                        # 如果 Delta 反向（吸收现象），放弃交易
                        delta_approved = True
                        delta_modifier = 1.0
                        
                        # 使用缓存的 Delta 快照
                        if is_latest_bar and not delta_snapshot_fetched:
                            cached_delta_snapshot = await self._get_delta_snapshot("BTCUSDT")
                            delta_snapshot_fetched = True
                        delta_snapshot = cached_delta_snapshot if is_latest_bar else None
                        if delta_snapshot is not None and delta_snapshot.trade_count > 0:
                            # 买入信号要求 Delta 为正（买盘主导）
                            if delta_snapshot.delta_ratio < 0:
                                delta_approved = False
                                if is_latest_bar:
                                    logging.info(
                                        f"🚫 H2 Delta方向不一致(吸收): {h2_signal.signal_type} - "
                                        f"买入信号但Delta={delta_snapshot.delta_ratio:.2f}<0，卖盘主导"
                                    )
                            else:
                                # 计算 Delta 调节因子
                                kline_open = data.iloc[i]["open"]
                                price_change_pct = ((close - kline_open) / kline_open * 100) if kline_open > 0 else 0.0
                                delta_modifier, delta_reason = self._calculate_delta_signal_modifier(
                                    delta_snapshot, h2_signal.side, price_change_pct
                                )
                                if delta_modifier == 0.0:
                                    delta_approved = False
                                    if is_latest_bar:
                                        logging.info(f"🚫 H2 Delta阻止: {h2_signal.signal_type} - {delta_reason}")
                                elif is_latest_bar and delta_modifier != 1.0:
                                    logging.info(
                                        f"{'✅' if delta_modifier > 1 else '⚠️'} H2 Delta{'增强' if delta_modifier > 1 else '减弱'}: "
                                        f"{h2_signal.signal_type} (调节={delta_modifier:.2f}) - {delta_reason}"
                                    )
                        
                        if delta_approved:
                            # HTF 趋势一致时增强信号
                            if htf_trend == HTFTrend.BULLISH:
                                delta_modifier *= 1.2
                                if is_latest_bar:
                                    logging.info(f"✅ H2 HTF增强: 1h上升趋势，买入信号增强 x1.2")
                            
                            signals[i] = h2_signal.signal_type
                            sides[i] = h2_signal.side
                            stops[i] = h2_signal.stop_loss
                            base_heights[i] = h2_signal.base_height
                            risk_reward_ratios[i] = 2.0
                            delta_modifiers[i] = delta_modifier
                            tp1, tp2, tp1_ratio, is_climax = self._calculate_tp1_tp2(
                                close, h2_signal.stop_loss, h2_signal.side, h2_signal.base_height, 
                                atr, h2_signal.signal_type, market_state.value, data, i
                            )
                            tp1_prices[i], tp2_prices[i] = tp1, tp2
                            tp1_close_ratios[i] = tp1_ratio
                            is_climax_bars[i] = is_climax
            
            elif (allowed_side is None or allowed_side == "buy") and not htf_allow_buy:
                # HTF 禁止买入，记录日志
                h2_signal = h2_machine.update(
                    close, high, low, ema, atr, data, i,
                    self.pattern_detector.calculate_unified_stop_loss
                )
                if h2_signal and is_latest_bar:
                    logging.info(
                        f"🚫 HTF过滤H2: {h2_signal.signal_type} - {htf_buy_reason}"
                    )

            # ⭐ L2 是顺势做空信号
            # 条件：本周期允许卖出 + HTF 不是上升趋势
            htf_allow_sell, htf_sell_reason = self.htf_filter.should_allow_signal("sell")
            
            if (allowed_side is None or allowed_side == "sell") and htf_allow_sell:
                l2_signal = l2_machine.update(
                    close, high, low, ema, atr, data, i,
                    self.pattern_detector.calculate_unified_stop_loss
                )
                if l2_signal:
                    # ========== BTC 高波动过滤1: 信号棒质量验证 ==========
                    bar_valid, bar_reason = self.pattern_detector.validate_btc_signal_bar(
                        data.iloc[i], l2_signal.side
                    )
                    if not bar_valid:
                        if is_latest_bar:
                            logging.info(
                                f"🚫 L2信号棒质量不合格: {l2_signal.signal_type} - {bar_reason}"
                            )
                        # 信号棒不合格，跳过此信号
                    else:
                        # ========== BTC 高波动过滤2: Delta 方向一致性验证 ==========
                        delta_approved = True
                        delta_modifier = 1.0
                        
                        # 使用缓存的 Delta 快照
                        if is_latest_bar and not delta_snapshot_fetched:
                            cached_delta_snapshot = await self._get_delta_snapshot("BTCUSDT")
                            delta_snapshot_fetched = True
                        delta_snapshot = cached_delta_snapshot if is_latest_bar else None
                        if delta_snapshot is not None and delta_snapshot.trade_count > 0:
                            # 卖出信号要求 Delta 为负（卖盘主导）
                            if delta_snapshot.delta_ratio > 0:
                                delta_approved = False
                                if is_latest_bar:
                                    logging.info(
                                        f"🚫 L2 Delta方向不一致(吸收): {l2_signal.signal_type} - "
                                        f"卖出信号但Delta={delta_snapshot.delta_ratio:.2f}>0，买盘主导"
                                    )
                            else:
                                # 计算 Delta 调节因子
                                kline_open = data.iloc[i]["open"]
                                price_change_pct = ((close - kline_open) / kline_open * 100) if kline_open > 0 else 0.0
                                delta_modifier, delta_reason = self._calculate_delta_signal_modifier(
                                    delta_snapshot, l2_signal.side, price_change_pct
                                )
                                if delta_modifier == 0.0:
                                    delta_approved = False
                                    if is_latest_bar:
                                        logging.info(f"🚫 L2 Delta阻止: {l2_signal.signal_type} - {delta_reason}")
                                elif is_latest_bar and delta_modifier != 1.0:
                                    logging.info(
                                        f"{'✅' if delta_modifier > 1 else '⚠️'} L2 Delta{'增强' if delta_modifier > 1 else '减弱'}: "
                                        f"{l2_signal.signal_type} (调节={delta_modifier:.2f}) - {delta_reason}"
                                    )
                        
                        if delta_approved:
                            # HTF 趋势一致时增强信号
                            if htf_trend == HTFTrend.BEARISH:
                                delta_modifier *= 1.2
                                if is_latest_bar:
                                    logging.info(f"✅ L2 HTF增强: 1h下降趋势，卖出信号增强 x1.2")
                            
                            signals[i] = l2_signal.signal_type
                            sides[i] = l2_signal.side
                            stops[i] = l2_signal.stop_loss
                            base_heights[i] = l2_signal.base_height
                            risk_reward_ratios[i] = 2.0
                            delta_modifiers[i] = delta_modifier
                            tp1, tp2, tp1_ratio, is_climax = self._calculate_tp1_tp2(
                                close, l2_signal.stop_loss, l2_signal.side, l2_signal.base_height, 
                                atr, l2_signal.signal_type, market_state.value, data, i
                            )
                            tp1_prices[i], tp2_prices[i] = tp1, tp2
                            tp1_close_ratios[i] = tp1_ratio
                            is_climax_bars[i] = is_climax
            
            elif (allowed_side is None or allowed_side == "sell") and not htf_allow_sell:
                # HTF 禁止卖出，记录日志
                l2_signal = l2_machine.update(
                    close, high, low, ema, atr, data, i,
                    self.pattern_detector.calculate_unified_stop_loss
                )
                if l2_signal and is_latest_bar:
                    logging.info(
                        f"🚫 HTF过滤L2: {l2_signal.signal_type} - {htf_sell_reason}"
                    )

        # 写入结果
        data["market_state"] = market_states
        # ========== TA-Lib 形态加成计算 ==========
        # 遍历所有有信号的行，计算 TA-Lib 形态加成
        if self.talib_detector is not None:
            for i in range(len(data)):
                if signals[i] is not None:
                    # 获取到该点为止的数据
                    df_slice = data.iloc[:i+1]
                    if len(df_slice) >= 10:  # 确保有足够的数据
                        boost, pattern_names = calculate_talib_boost(df_slice, signals[i])
                        talib_boosts[i] = boost
                        talib_patterns[i] = ", ".join(pattern_names) if pattern_names else None
                        
                        if boost > 0:
                            logging.debug(
                                f"🎯 TA-Lib 形态加成 @ bar {i}: {signals[i]} +{boost:.2f}, "
                                f"形态: {talib_patterns[i]}"
                            )
        
        data["signal"] = signals
        data["side"] = sides
        data["stop_loss"] = stops
        data["risk_reward_ratio"] = risk_reward_ratios
        data["base_height"] = base_heights
        data["tp1_price"] = tp1_prices
        data["tp2_price"] = tp2_prices
        data["tight_channel_score"] = tight_channel_scores
        data["delta_modifier"] = delta_modifiers  # Delta调节因子
        data["tp1_close_ratio"] = tp1_close_ratios  # TP1 平仓比例（Climax 时 75%）
        data["is_climax_bar"] = is_climax_bars  # Climax 信号棒标记
        data["talib_boost"] = talib_boosts  # TA-Lib 形态加成
        data["talib_patterns"] = talib_patterns  # 匹配的 TA-Lib 形态
        
        return data
