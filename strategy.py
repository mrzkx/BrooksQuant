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

代码结构（重构后）：
- generate_signals(): 主入口，协调各子方法
- _precompute_indicators(): 预计算技术指标
- _init_signal_arrays(): 初始化信号结果数组
- _get_bar_context(): 获取单根K线的市场上下文
- _process_pending_spike(): 处理待处理的Spike回撤入场
- _check_pattern_signals(): 检测形态信号（FailedBreakout/Spike/Climax/Wedge）
- _process_h2l2_signals(): 处理H2/L2状态机信号
- _record_signal(): 记录信号到结果数组
- _apply_talib_boost(): 应用TA-Lib形态加成
"""

import json
import logging
import pandas as pd
from typing import List, Optional, Tuple, Dict, Any
from dataclasses import dataclass, field

import redis.asyncio as aioredis

# ⭐ 提前导入 MarketState，供 dataclass 使用
from logic.market_analyzer import MarketState


# ============================================================================
# 辅助数据类（用于拆分 generate_signals）
# ============================================================================

@dataclass
class BarContext:
    """
    单根 K 线的市场上下文信息
    
    Al Brooks: "交易前必须先确定市场上下文（趋势/区间）"
    """
    # 基础数据
    i: int                           # K线索引
    close: float                     # 收盘价
    high: float                      # 最高价
    low: float                       # 最低价
    ema: float                       # EMA值
    atr: Optional[float]             # ATR值
    
    # 市场状态
    market_state: MarketState        # 市场状态枚举
    trend_direction: Optional[str]   # 趋势方向 ("up"/"down"/None)
    trend_strength: float            # 趋势强度 (0-1)
    tight_channel_score: float       # 紧凑通道评分
    tight_channel_direction: Optional[str]  # 紧凑通道方向
    
    # 交易限制
    is_strong_trend_mode: bool       # 是否是强趋势模式
    allowed_side: Optional[str]      # 允许的交易方向 ("buy"/"sell"/None)
    is_latest_bar: bool              # 是否是最新K线


@dataclass
class SignalArrays:
    """
    信号结果数组集合
    
    存储生成的所有信号数据
    """
    signals: List[Optional[str]]
    sides: List[Optional[str]]
    stops: List[Optional[float]]
    market_states: List[Optional[str]]
    risk_reward_ratios: List[Optional[float]]
    base_heights: List[Optional[float]]
    tp1_prices: List[Optional[float]]
    tp2_prices: List[Optional[float]]
    tight_channel_scores: List[Optional[float]]
    delta_modifiers: List[Optional[float]]
    tp1_close_ratios: List[Optional[float]]
    is_climax_bars: List[Optional[bool]]
    talib_boosts: List[Optional[float]]
    talib_patterns: List[Optional[str]]
    entry_modes: List[Optional[str]]      # Spike: "Market_Entry" / "Limit_Entry"
    is_high_risk: List[Optional[bool]]    # Spike 高风险时 True，仓位 50%
    move_stop_to_breakeven_at_tp1: List[Optional[bool]]  # TP1 后移动止损到保本（Wedge 必做）
    
    @classmethod
    def create(cls, length: int) -> "SignalArrays":
        """创建指定长度的空数组集合"""
        return cls(
            signals=[None] * length,
            sides=[None] * length,
            stops=[None] * length,
            market_states=[None] * length,
            risk_reward_ratios=[None] * length,
            base_heights=[None] * length,
            tp1_prices=[None] * length,
            tp2_prices=[None] * length,
            tight_channel_scores=[None] * length,
            delta_modifiers=[None] * length,
            tp1_close_ratios=[None] * length,
            is_climax_bars=[None] * length,
            talib_boosts=[None] * length,
            talib_patterns=[None] * length,
            entry_modes=[None] * length,
            is_high_risk=[None] * length,
            move_stop_to_breakeven_at_tp1=[None] * length,
        )


@dataclass
class SignalResult:
    """
    单个信号的检测结果
    
    用于在各检测方法之间传递信号信息
    """
    signal_type: str
    side: str
    stop_loss: float
    base_height: float
    limit_price: Optional[float] = None  # 限价入场价格（Spike Limit_Entry 用）
    risk_reward: float = 2.0
    delta_modifier: float = 1.0
    tp1_close_ratio: float = 0.5
    is_climax: bool = False
    strength: float = 1.0               # 信号强度（HTF 权重调节用）
    htf_modifier: float = 1.0           # HTF 权重调节因子
    entry_mode: Optional[str] = None    # Spike 入场模式: "Market_Entry" / "Limit_Entry"
    is_high_risk: bool = False          # Spike 止损距离 > 2.5*ATR 时 True，仓位 50%
    wedge_tp1_price: Optional[float] = None  # Wedge 专用 TP1（EMA20）
    wedge_tp2_price: Optional[float] = None  # Wedge 专用 TP2（楔形起点）
    wedge_strong_reversal_bar: bool = False  # Wedge Signal Bar 是否为大影线强反转棒（强度+0.2）
    move_stop_to_breakeven_at_tp1: bool = False  # TP1 触发后移动止损到保本（Wedge 必做，Brooks 高波动保命）

# 导入模块化组件（MarketState 已在文件顶部导入）
from logic.market_analyzer import MarketAnalyzer
from logic.patterns import PatternDetector
from logic.state_machines import H2StateMachine, L2StateMachine
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
    get_delta_analyzer,
    compute_wedge_buy_delta_boost,
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
        """检查信号是否在冷却期内（同类型信号）"""
        last_bar = self._last_signal_bar.get(signal_type)
        if last_bar is None:
            return False
        return (current_bar - last_bar) < self.SIGNAL_COOLDOWN_BARS
    
    def _is_direction_in_cooldown(self, side: str, current_bar: int) -> bool:
        """
        检查该方向的任意信号是否在冷却期内
        
        Al Brooks 原则：
        - 同方向信号需要冷却期（避免过度交易）
        - 买入信号之间、卖出信号之间需要间隔
        - 这比只检查同类型信号更严格，但更符合风险管理
        
        Args:
            side: 交易方向 ("buy" 或 "sell")
            current_bar: 当前 K 线索引
        
        Returns:
            True 如果该方向有信号在冷却期内
        """
        # 定义各方向的信号类型
        buy_signals = [
            "Spike_Buy", "FailedBreakout_Buy", "Climax_Buy", 
            "Wedge_Buy", "H1_Buy", "H2_Buy"
        ]
        sell_signals = [
            "Spike_Sell", "FailedBreakout_Sell", "Climax_Sell", 
            "Wedge_Sell", "L1_Sell", "L2_Sell"
        ]
        
        signals_to_check = buy_signals if side == "buy" else sell_signals
        
        for signal_type in signals_to_check:
            last_bar = self._last_signal_bar.get(signal_type)
            if last_bar is not None:
                if (current_bar - last_bar) < self.SIGNAL_COOLDOWN_BARS:
                    return True
        
        return False
    
    def _update_signal_cooldown(self, signal_type: str, current_bar: int) -> None:
        """更新信号冷却期记录"""
        self._last_signal_bar[signal_type] = current_bar
    
    def _check_signal_cooldown(
        self, signal_type: str, side: str, current_bar: int, is_latest_bar: bool
    ) -> bool:
        """
        统一的信号冷却期检查
        
        Al Brooks 原则：
        - 同方向信号需要冷却期（避免过度交易）
        - 市场需要时间证明方向
        
        Args:
            signal_type: 信号类型名称
            side: 交易方向
            current_bar: 当前 K 线索引
            is_latest_bar: 是否是最新 K 线
        
        Returns:
            True 如果信号应该被跳过（在冷却期内）
        """
        # 检查同方向的冷却期
        if self._is_direction_in_cooldown(side, current_bar):
            if is_latest_bar:
                logging.debug(
                    f"⏳ 信号冷却中: {signal_type} {side} "
                    f"(需间隔 {self.SIGNAL_COOLDOWN_BARS} 根K线)"
                )
            return True
        return False
    
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
        
        # ========== 计算 TP1 和 TP2（使用方向因子消除重复）==========
        # 方向因子：buy = +1, sell = -1
        direction = 1 if side == "buy" else -1
        
        # TP1 计算
        tp1 = entry_price + direction * (risk * tp1_multiplier)
        
        # TP2: 取 Measured Move 和 R 倍数中较有利者
        measured_move = entry_price + direction * base_height if base_height > 0 else entry_price + direction * (risk * tp2_multiplier)
        r_based_tp2 = entry_price + direction * (risk * tp2_multiplier)
        
        # buy 取 max（更远的目标），sell 取 min（更远的目标）
        tp2 = max(measured_move, r_based_tp2) if side == "buy" else min(measured_move, r_based_tp2)
        
        # TradingRange 时强制限制在区间边缘
        if market_state == "TradingRange" and base_height > 0:
            range_limit = entry_price + direction * base_height
            # buy 取 min（不超过上边缘），sell 取 max（不超过下边缘）
            tp2 = min(tp2, range_limit) if side == "buy" else max(tp2, range_limit)
        
        # 如果 base_height 太小，使用更保守的目标
        if base_height > 0 and base_height < risk * 1.5 and market_state != "TradingRange":
            conservative_tp2 = entry_price + direction * (risk * (tp2_multiplier + 0.5))
            tp2 = max(tp2, conservative_tp2) if side == "buy" else min(tp2, conservative_tp2)
        
        return (tp1, tp2, tp1_close_ratio, is_climax)

    # ========================================================================
    # generate_signals 辅助方法（拆分后）
    # ========================================================================
    
    def _precompute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        预计算技术指标和向量化列
        
        Al Brooks 使用的核心指标：
        - EMA(20): 趋势过滤器
        - ATR(20): 波动率和止损计算
        - 实体大小、K线范围: 信号质量评估
        
        Returns:
            添加了指标列的 DataFrame
        """
        data = df.copy()
        
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
        
        return data
    
    def _get_bar_context(
        self, data: pd.DataFrame, i: int, total_bars: int
    ) -> BarContext:
        """
        获取单根 K 线的市场上下文
        
        Al Brooks: "交易前必须先确定市场上下文"
        
        Args:
            data: 带指标的 DataFrame
            i: K线索引
            total_bars: 总K线数
        
        Returns:
            BarContext: 包含该K线所有上下文信息
        """
        row = data.iloc[i]
        close = row["close"]
        high = row["high"]
        low = row["low"]
        ema = row["ema"]
        atr = row["atr"] if "atr" in data.columns else None
        
        # 检测市场状态
        market_state = self.market_analyzer.detect_market_state(data, i, ema)
        
        # 获取趋势方向和强度
        trend_direction = self.market_analyzer.get_trend_direction()
        trend_strength = self.market_analyzer.get_trend_strength()
        
        # 计算紧凑通道评分
        tight_channel_score = self.market_analyzer.calculate_tight_channel_score(data, i, ema)
        
        # 紧凑通道方向
        tight_channel_direction = None
        if market_state == MarketState.TIGHT_CHANNEL:
            tight_channel_direction = self.market_analyzer.get_tight_channel_direction(data, i)
        
        # Al Brooks 核心：强趋势模式判断
        is_strong_trend_mode = (
            market_state == MarketState.TIGHT_CHANNEL or 
            market_state == MarketState.STRONG_TREND or
            trend_strength >= 0.7
        )
        
        # 确定允许的交易方向
        allowed_side: Optional[str] = None
        if is_strong_trend_mode:
            if tight_channel_direction == "up" or trend_direction == "up":
                allowed_side = "buy"
            elif tight_channel_direction == "down" or trend_direction == "down":
                allowed_side = "sell"
        
        return BarContext(
            i=i,
            close=close,
            high=high,
            low=low,
            ema=ema,
            atr=atr,
            market_state=market_state,
            trend_direction=trend_direction,
            trend_strength=trend_strength,
            tight_channel_score=tight_channel_score,
            tight_channel_direction=tight_channel_direction,
            is_strong_trend_mode=is_strong_trend_mode,
            allowed_side=allowed_side,
            is_latest_bar=(i == total_bars - 1),
        )
    
    def _record_signal(
        self, 
        arrays: SignalArrays, 
        i: int, 
        result: SignalResult,
        market_state_value: str,
        tight_channel_score: float,
        tp1: float,
        tp2: float,
    ) -> None:
        """
        记录信号到结果数组
        
        Args:
            arrays: 信号数组集合
            i: K线索引
            result: 信号结果
            market_state_value: 市场状态字符串
            tight_channel_score: 紧凑通道评分
            tp1, tp2: 止盈价格
        """
        arrays.signals[i] = result.signal_type
        arrays.sides[i] = result.side
        arrays.stops[i] = result.stop_loss
        arrays.base_heights[i] = result.base_height
        arrays.risk_reward_ratios[i] = result.risk_reward
        arrays.market_states[i] = market_state_value
        arrays.tight_channel_scores[i] = tight_channel_score
        arrays.tp1_prices[i] = tp1
        arrays.tp2_prices[i] = tp2
        arrays.tp1_close_ratios[i] = result.tp1_close_ratio
        arrays.is_climax_bars[i] = result.is_climax
        arrays.delta_modifiers[i] = result.delta_modifier
        arrays.entry_modes[i] = getattr(result, "entry_mode", None)
        arrays.is_high_risk[i] = getattr(result, "is_high_risk", False)
        arrays.move_stop_to_breakeven_at_tp1[i] = getattr(result, "move_stop_to_breakeven_at_tp1", False)
    
    def _check_failed_breakout(
        self, 
        data: pd.DataFrame, 
        ctx: BarContext
    ) -> Optional[SignalResult]:
        """
        检测 Failed Breakout 信号
        
        Al Brooks: "FailedBreakout 是区间交易中最高概率的策略"
        
        条件：
        1. 必须在 TRADING_RANGE 状态
        2. 不能在强趋势模式
        3. 通过方向过滤
        4. 通过冷却期检查
        
        Returns:
            SignalResult 或 None
        """
        # 只在 TRADING_RANGE 且非强趋势模式下检测
        if ctx.market_state != MarketState.TRADING_RANGE or ctx.is_strong_trend_mode:
            return None
        
        result = self.pattern_detector.detect_failed_breakout(
            data, ctx.i, ctx.ema, ctx.atr, ctx.market_state
        )
        
        if not result:
            return None
        
        signal_type, side, stop_loss, base_height = result
        
        # 冷却期检查
        if self._check_signal_cooldown(signal_type, side, ctx.i, ctx.is_latest_bar):
            return None
        
        # 方向过滤
        if ctx.allowed_side is not None and side != ctx.allowed_side:
            if ctx.is_latest_bar:
                logging.info(
                    f"🚫 强趋势禁止反转: {signal_type} {side} - "
                    f"趋势={ctx.trend_direction}(强度={ctx.trend_strength:.2f})，只允许{ctx.allowed_side}"
                )
            return None
        
        return SignalResult(
            signal_type=signal_type,
            side=side,
            stop_loss=stop_loss,
            base_height=base_height,
            risk_reward=1.0,  # FailedBreakout 使用 1:1 初始盈亏比
        )
    
    def _check_spike(
        self, 
        data: pd.DataFrame, 
        ctx: BarContext
    ) -> Optional[SignalResult]:
        """
        检测 Strong Spike 信号
        
        Al Brooks: "Spike 是强突破的表现，代表机构资金入场"
        
        条件：
        1. 只在 BREAKOUT 状态下触发
        2. 连续 3 根同向 K 线
        3. 实体 > 3 倍平均实体
        4. 突破前 10 根 K 线的高/低点
        
        Returns:
            SignalResult 或 None
        """
        result = self.pattern_detector.detect_strong_spike(
            data, ctx.i, ctx.ema, ctx.atr, ctx.market_state
        )
        
        if not result:
            return None
        
        signal_type, side, stop_loss, limit_price, base_height, entry_mode, is_high_risk = result
        
        # 冷却期检查
        if self._check_signal_cooldown(signal_type, side, ctx.i, ctx.is_latest_bar):
            return None
        
        # StrongTrend 严格逆势过滤
        if ctx.market_state == MarketState.STRONG_TREND:
            if ctx.trend_direction == "up" and side == "sell":
                if ctx.is_latest_bar:
                    logging.info(f"🚫 StrongTrend禁止做空: {signal_type} - 上涨趋势中禁止卖出")
                return None
            if ctx.trend_direction == "down" and side == "buy":
                if ctx.is_latest_bar:
                    logging.info(f"🚫 StrongTrend禁止做多: {signal_type} - 下跌趋势中禁止买入")
                return None
        
        # 方向过滤
        if ctx.allowed_side is not None and side != ctx.allowed_side:
            if ctx.is_latest_bar:
                logging.info(
                    f"🚫 强趋势只顺势: {signal_type} {side} 被禁止 - "
                    f"趋势={ctx.trend_direction}，只允许{ctx.allowed_side}"
                )
            return None
        
        if ctx.is_latest_bar and is_high_risk:
            logging.info(
                f"⚠️ Spike 高风险: {signal_type} 止损距离>2.5*ATR，建议仓位 50%"
            )
        
        return SignalResult(
            signal_type=signal_type,
            side=side,
            stop_loss=stop_loss,
            base_height=base_height,
            limit_price=limit_price,
            risk_reward=2.0,
            entry_mode=entry_mode,
            is_high_risk=is_high_risk,
        )
    
    def _check_climax(
        self, 
        data: pd.DataFrame, 
        ctx: BarContext
    ) -> Optional[SignalResult]:
        """
        检测 Climax 反转信号
        
        Al Brooks: "Climax 是市场极端情绪的表现，通常预示着反转"
        
        条件：
        1. 不能在强趋势模式（反转信号）
        2. 前一根 K 线长度 > 2.5 ATR
        3. 当前 K 线显示反转迹象
        
        Returns:
            SignalResult 或 None
        """
        # 强趋势模式下禁止反转
        if ctx.is_strong_trend_mode:
            return None
        
        result = self.pattern_detector.detect_climax_reversal(
            data, ctx.i, ctx.ema, ctx.atr
        )
        
        if not result:
            return None
        
        signal_type, side, stop_loss, base_height = result
        
        # 冷却期检查
        if self._check_signal_cooldown(signal_type, side, ctx.i, ctx.is_latest_bar):
            return None
        
        # 方向过滤
        if ctx.allowed_side is not None and side != ctx.allowed_side:
            if ctx.is_latest_bar:
                logging.info(
                    f"🚫 强趋势禁止反转: {signal_type} {side} - "
                    f"趋势={ctx.trend_direction}，Climax反转在强趋势中胜率<20%"
                )
            return None
        
        return SignalResult(
            signal_type=signal_type,
            side=side,
            stop_loss=stop_loss,
            base_height=base_height,
            risk_reward=2.0,
        )
    
    def _check_wedge(
        self, 
        data: pd.DataFrame, 
        ctx: BarContext
    ) -> Optional[SignalResult]:
        """
        检测 Wedge 反转信号（三推反转）
        
        Al Brooks: "三推形成的楔形是高概率反转形态"
        
        条件：
        1. 不能在强趋势模式（反转信号）
        2. 三次推进形成收敛的楔形
        3. 每次推进的动能递减
        4. 第三次推进显示疲软
        
        Returns:
            SignalResult 或 None
        """
        # 强趋势模式下禁止反转
        if ctx.is_strong_trend_mode:
            return None
        
        result = self.pattern_detector.detect_wedge_reversal(
            data, ctx.i, ctx.ema, ctx.atr, ctx.market_state
        )
        
        if not result:
            return None
        
        signal_type, side, stop_loss, base_height, wedge_tp1, wedge_tp2, is_strong_reversal_bar = result
        
        # 冷却期检查
        if self._check_signal_cooldown(signal_type, side, ctx.i, ctx.is_latest_bar):
            return None
        
        # 方向过滤
        if ctx.allowed_side is not None and side != ctx.allowed_side:
            if ctx.is_latest_bar:
                logging.info(
                    f"🚫 强趋势禁止反转: {signal_type} {side} - "
                    f"趋势={ctx.trend_direction}，Wedge反转在强趋势中胜率<15%"
                )
            return None
        
        # Wedge 信号强度：初始 0.5，强反转棒 +0.2；Delta 背离在 generate_signals 中 +0.3
        strength = 0.5 + (0.2 if is_strong_reversal_bar else 0.0)
        
        return SignalResult(
            signal_type=signal_type,
            side=side,
            stop_loss=stop_loss,
            base_height=base_height,
            risk_reward=2.0,
            wedge_tp1_price=wedge_tp1,
            wedge_tp2_price=wedge_tp2,
            wedge_strong_reversal_bar=is_strong_reversal_bar,
            strength=strength,
        )
    
    async def _process_h2_signal(
        self, 
        h2_machine: H2StateMachine,
        data: pd.DataFrame,
        ctx: BarContext,
        cached_delta_snapshot: Optional[DeltaSnapshot],
        htf_trend: HTFTrend,
    ) -> Optional[SignalResult]:
        """
        处理 H2 状态机信号
        
        Al Brooks H2 原则:
        - 上升趋势中的第二次回调买入点
        - 需要 HTF 趋势确认
        - 需要信号棒质量验证
        - 需要 Delta 方向一致性
        
        Returns:
            SignalResult 或 None
        """
        h2_signal = h2_machine.update(
            ctx.close, ctx.high, ctx.low, ctx.ema, ctx.atr, data, ctx.i,
            self.pattern_detector.calculate_unified_stop_loss
        )
        
        if not h2_signal:
            return None
        
        # 冷却期检查
        if self._check_signal_cooldown(h2_signal.signal_type, h2_signal.side, ctx.i, ctx.is_latest_bar):
            return None
        
        # 信号棒质量验证
        bar_valid, bar_reason = self.pattern_detector.validate_btc_signal_bar(
            data.iloc[ctx.i], h2_signal.side
        )
        if not bar_valid:
            if ctx.is_latest_bar:
                logging.info(f"🚫 H2信号棒质量不合格: {h2_signal.signal_type} - {bar_reason}")
            return None
        
        # Delta 方向一致性验证
        # ⭐ 优化：只在极端反向时阻止，轻微反向只减弱
        # Al Brooks: Delta 用于调整仓位，而非绝对禁止
        delta_modifier = 1.0
        if cached_delta_snapshot is not None and cached_delta_snapshot.trade_count > 0:
            # ⭐ 优化：只有 delta_ratio < -0.3 才阻止（严重卖压才阻止买入）
            if cached_delta_snapshot.delta_ratio < -0.3:
                if ctx.is_latest_bar:
                    logging.info(
                        f"🚫 H2 Delta强烈反向: {h2_signal.signal_type} - "
                        f"买入信号但Delta={cached_delta_snapshot.delta_ratio:.2f}<-0.3，强卖压"
                    )
                return None
            elif cached_delta_snapshot.delta_ratio < 0:
                # 轻微反向：只减弱信号，不阻止
                delta_modifier = 0.7
                if ctx.is_latest_bar:
                    logging.info(
                        f"⚠️ H2 Delta轻微反向: {h2_signal.signal_type} - "
                        f"Delta={cached_delta_snapshot.delta_ratio:.2f}，信号减弱"
                    )
            else:
                kline_open = data.iloc[ctx.i]["open"]
                price_change_pct = ((ctx.close - kline_open) / kline_open * 100) if kline_open > 0 else 0.0
                delta_modifier, delta_reason = self._calculate_delta_signal_modifier(
                    cached_delta_snapshot, h2_signal.side, price_change_pct
                )
                
                if delta_modifier == 0.0:
                    if ctx.is_latest_bar:
                        logging.info(f"🚫 H2 Delta阻止: {h2_signal.signal_type} - {delta_reason}")
                    return None
                elif ctx.is_latest_bar and delta_modifier != 1.0:
                    logging.info(
                        f"{'✅' if delta_modifier > 1 else '⚠️'} H2 Delta{'增强' if delta_modifier > 1 else '减弱'}: "
                        f"{h2_signal.signal_type} (调节={delta_modifier:.2f}) - {delta_reason}"
                    )
        
        # HTF 趋势一致时增强信号
        if htf_trend == HTFTrend.BULLISH:
            delta_modifier *= 1.2
            if ctx.is_latest_bar:
                logging.info(f"✅ H2 HTF增强: 1h上升趋势，买入信号增强 x1.2")
        
        return SignalResult(
            signal_type=h2_signal.signal_type,
            side=h2_signal.side,
            stop_loss=h2_signal.stop_loss,
            base_height=h2_signal.base_height,
            delta_modifier=delta_modifier,
            risk_reward=2.0,
        )
    
    async def _process_l2_signal(
        self, 
        l2_machine: L2StateMachine,
        data: pd.DataFrame,
        ctx: BarContext,
        cached_delta_snapshot: Optional[DeltaSnapshot],
        htf_trend: HTFTrend,
    ) -> Optional[SignalResult]:
        """
        处理 L2 状态机信号
        
        Al Brooks L2 原则:
        - 下降趋势中的第二次反弹卖出点
        - 需要 HTF 趋势确认
        - 需要信号棒质量验证
        - 需要 Delta 方向一致性
        
        Returns:
            SignalResult 或 None
        """
        l2_signal = l2_machine.update(
            ctx.close, ctx.high, ctx.low, ctx.ema, ctx.atr, data, ctx.i,
            self.pattern_detector.calculate_unified_stop_loss
        )
        
        if not l2_signal:
            return None
        
        # 冷却期检查
        if self._check_signal_cooldown(l2_signal.signal_type, l2_signal.side, ctx.i, ctx.is_latest_bar):
            return None
        
        # 信号棒质量验证
        bar_valid, bar_reason = self.pattern_detector.validate_btc_signal_bar(
            data.iloc[ctx.i], l2_signal.side
        )
        if not bar_valid:
            if ctx.is_latest_bar:
                logging.info(f"🚫 L2信号棒质量不合格: {l2_signal.signal_type} - {bar_reason}")
            return None
        
        # Delta 方向一致性验证
        # ⭐ 优化：只在极端反向时阻止，轻微反向只减弱
        delta_modifier = 1.0
        if cached_delta_snapshot is not None and cached_delta_snapshot.trade_count > 0:
            # ⭐ 优化：只有 delta_ratio > 0.3 才阻止（严重买压才阻止卖出）
            if cached_delta_snapshot.delta_ratio > 0.3:
                if ctx.is_latest_bar:
                    logging.info(
                        f"🚫 L2 Delta强烈反向: {l2_signal.signal_type} - "
                        f"卖出信号但Delta={cached_delta_snapshot.delta_ratio:.2f}>0.3，强买压"
                    )
                return None
            elif cached_delta_snapshot.delta_ratio > 0:
                # 轻微反向：只减弱信号，不阻止
                delta_modifier = 0.7
                if ctx.is_latest_bar:
                    logging.info(
                        f"⚠️ L2 Delta轻微反向: {l2_signal.signal_type} - "
                        f"Delta={cached_delta_snapshot.delta_ratio:.2f}，信号减弱"
                    )
            else:
                kline_open = data.iloc[ctx.i]["open"]
                price_change_pct = ((ctx.close - kline_open) / kline_open * 100) if kline_open > 0 else 0.0
                delta_modifier, delta_reason = self._calculate_delta_signal_modifier(
                    cached_delta_snapshot, l2_signal.side, price_change_pct
                )
                
                if delta_modifier == 0.0:
                    if ctx.is_latest_bar:
                        logging.info(f"🚫 L2 Delta阻止: {l2_signal.signal_type} - {delta_reason}")
                    return None
                elif ctx.is_latest_bar and delta_modifier != 1.0:
                    logging.info(
                        f"{'✅' if delta_modifier > 1 else '⚠️'} L2 Delta{'增强' if delta_modifier > 1 else '减弱'}: "
                        f"{l2_signal.signal_type} (调节={delta_modifier:.2f}) - {delta_reason}"
                    )
        
        # HTF 趋势一致时增强信号
        if htf_trend == HTFTrend.BEARISH:
            delta_modifier *= 1.2
            if ctx.is_latest_bar:
                logging.info(f"✅ L2 HTF增强: 1h下降趋势，卖出信号增强 x1.2")
        
        return SignalResult(
            signal_type=l2_signal.signal_type,
            side=l2_signal.side,
            stop_loss=l2_signal.stop_loss,
            base_height=l2_signal.base_height,
            delta_modifier=delta_modifier,
            risk_reward=2.0,
        )
    
    def _apply_talib_boost(
        self, 
        data: pd.DataFrame, 
        arrays: SignalArrays
    ) -> None:
        """
        应用 TA-Lib 形态加成
        
        当 TA-Lib 形态与 PA 信号重合时，给予置信度加成
        """
        if self.talib_detector is None:
            return
        
        for i in range(len(data)):
            if arrays.signals[i] is not None:
                df_slice = data.iloc[:i+1]
                if len(df_slice) >= 10:
                    boost, pattern_names = calculate_talib_boost(df_slice, arrays.signals[i])
                    arrays.talib_boosts[i] = boost
                    arrays.talib_patterns[i] = ", ".join(pattern_names) if pattern_names else None
                    
                    if boost > 0:
                        logging.debug(
                            f"🎯 TA-Lib 形态加成 @ bar {i}: {arrays.signals[i]} +{boost:.2f}, "
                            f"形态: {arrays.talib_patterns[i]}"
                        )
    
    def _write_results_to_dataframe(
        self, 
        data: pd.DataFrame, 
        arrays: SignalArrays
    ) -> pd.DataFrame:
        """
        将信号结果写入 DataFrame
        """
        data["market_state"] = arrays.market_states
        data["signal"] = arrays.signals
        data["side"] = arrays.sides
        data["stop_loss"] = arrays.stops
        data["risk_reward_ratio"] = arrays.risk_reward_ratios
        data["base_height"] = arrays.base_heights
        data["tp1_price"] = arrays.tp1_prices
        data["tp2_price"] = arrays.tp2_prices
        data["tight_channel_score"] = arrays.tight_channel_scores
        data["delta_modifier"] = arrays.delta_modifiers
        data["tp1_close_ratio"] = arrays.tp1_close_ratios
        data["is_climax_bar"] = arrays.is_climax_bars
        data["talib_boost"] = arrays.talib_boosts
        data["talib_patterns"] = arrays.talib_patterns
        data["entry_mode"] = arrays.entry_modes
        data["is_high_risk"] = arrays.is_high_risk
        data["move_stop_to_breakeven_at_tp1"] = arrays.move_stop_to_breakeven_at_tp1
        
        return data

    async def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        异步生成交易信号（重构后主入口）
        
        使用模块化的辅助方法来简化主循环逻辑：
        - _precompute_indicators(): 预计算技术指标
        - _get_bar_context(): 获取单根K线的市场上下文
        - _check_failed_breakout/spike/climax/wedge(): 检测各类形态信号
        - _process_h2/l2_signal(): 处理H2/L2状态机信号
        - _record_signal(): 记录信号到结果数组
        - _apply_talib_boost(): 应用TA-Lib形态加成
        
        返回包含信号的 DataFrame:
        - ema, atr: 技术指标
        - market_state: 市场状态
        - signal, side: 交易信号
        - stop_loss, risk_reward_ratio: 风险管理
        - base_height, tp1_price, tp2_price: 止盈目标
        - tight_channel_score: 紧凑通道评分
        - delta_modifier: Delta调节因子
        """
        # ========== Step 1: 预计算技术指标 ==========
        data = self._precompute_indicators(df)
        total_bars = len(data)
        
        # ========== Step 2: 初始化信号数组 ==========
        arrays = SignalArrays.create(total_bars)
        
        # ========== Step 3: 初始化状态机和缓存 ==========
        # Spike 回撤入场状态 (Limit_Entry: signal_type, side, stop_loss, limit_price, base_height, spike_idx, is_high_risk)
        pending_spike: Optional[Tuple[str, str, float, float, float, int, bool]] = None
        
        # H2/L2 状态机
        h2_machine = H2StateMachine()
        l2_machine = L2StateMachine()
        
        # HTF 快照缓存（1h 级别，整个循环中不变）
        cached_htf_snapshot = self.htf_filter.get_snapshot()
        cached_htf_trend = cached_htf_snapshot.trend if cached_htf_snapshot else HTFTrend.NEUTRAL
        
        # HTF 权重调节因子缓存（v2.0 软过滤）
        cached_htf_buy_modifier = self.htf_filter.get_signal_modifier("buy")
        cached_htf_sell_modifier = self.htf_filter.get_signal_modifier("sell")
        
        # Delta 快照缓存
        cached_delta_snapshot: Optional[DeltaSnapshot] = None
        delta_snapshot_fetched = False

        # ========== Step 4: 主循环 - 逐根K线处理 ==========
        for i in range(1, total_bars):
            # 获取当前K线的市场上下文
            ctx = self._get_bar_context(data, i, total_bars)
            arrays.market_states[i] = ctx.market_state.value
            arrays.tight_channel_scores[i] = ctx.tight_channel_score
            
            # ---------- 处理待处理的 Spike 回撤入场（Limit_Entry）----------
            if pending_spike is not None:
                signal_type, side, stop_loss, limit_price, base_height, spike_idx, is_high_risk = pending_spike
                
                # 检查是否触发限价入场
                triggered = False
                if side == "buy" and ctx.low <= limit_price:
                    triggered = True
                elif side == "sell" and ctx.high >= limit_price:
                    triggered = True
                
                if triggered:
                    tp1, tp2, tp1_ratio, is_climax = self._calculate_tp1_tp2(
                        limit_price, stop_loss, side, base_height, ctx.atr, signal_type,
                        ctx.market_state.value, data, i
                    )
                    result = SignalResult(
                        signal_type=signal_type, side=side, stop_loss=stop_loss,
                        base_height=base_height, tp1_close_ratio=tp1_ratio, is_climax=is_climax,
                        entry_mode="Limit_Entry", is_high_risk=is_high_risk
                    )
                    self._record_signal(arrays, i, result, ctx.market_state.value, ctx.tight_channel_score, tp1, tp2)
                    self._update_signal_cooldown(signal_type, i)
                    pending_spike = None
                    if side == "buy":
                        h2_machine.set_strong_trend()
                    else:
                        l2_machine.set_strong_trend()
                    continue
                
                # 检查是否应该取消待处理的 Spike
                if (side == "buy" and ctx.close > limit_price * 1.05) or \
                   (side == "sell" and ctx.close < limit_price * 0.95):
                    pending_spike = None
                elif i - spike_idx > 5:
                    pending_spike = None
            
            # ---------- 优先级1: Failed Breakout ----------
            fb_result = self._check_failed_breakout(data, ctx)
            if fb_result:
                # 应用 HTF 权重调节（v2.0 软过滤）
                htf_modifier = cached_htf_buy_modifier if fb_result.side == "buy" else cached_htf_sell_modifier
                fb_result.htf_modifier = htf_modifier
                fb_result.strength = fb_result.strength * htf_modifier
                
                if ctx.is_latest_bar and htf_modifier != 1.0:
                    logging.info(f"📊 HTF权重调节 FB: ×{htf_modifier} → 强度={fb_result.strength:.2f}")
                
                tp1, tp2, tp1_ratio, is_climax = self._calculate_tp1_tp2(
                    ctx.close, fb_result.stop_loss, fb_result.side, fb_result.base_height,
                    ctx.atr, fb_result.signal_type, ctx.market_state.value, data, i
                )
                fb_result.tp1_close_ratio = tp1_ratio
                fb_result.is_climax = is_climax
                self._record_signal(arrays, i, fb_result, ctx.market_state.value, ctx.tight_channel_score, tp1, tp2)
                self._update_signal_cooldown(fb_result.signal_type, i)
                continue
            
            # ---------- 优先级2: Strong Spike ----------
            spike_result = self._check_spike(data, ctx)
            if spike_result:
                if spike_result.limit_price is not None:
                    # Limit_Entry: 设置待处理的 Spike 回撤入场（入场价 = Signal Bar 实体 50%）
                    pending_spike = (
                        spike_result.signal_type, spike_result.side, spike_result.stop_loss,
                        spike_result.limit_price, spike_result.base_height, i,
                        getattr(spike_result, "is_high_risk", False)
                    )
                else:
                    # 直接入场（需要 Delta 过滤）
                    delta_modifier = 1.0
                    
                    if ctx.market_state == MarketState.BREAKOUT:
                        # 只在最新 K 线时获取 Delta 快照
                        if ctx.is_latest_bar and not delta_snapshot_fetched:
                            cached_delta_snapshot = await self._get_delta_snapshot("BTCUSDT")
                            delta_snapshot_fetched = True
                        
                        delta_snapshot = cached_delta_snapshot if ctx.is_latest_bar else None
                        if delta_snapshot is not None and delta_snapshot.trade_count > 0:
                            kline_open = data.iloc[i]["open"]
                            price_change_pct = ((ctx.close - kline_open) / kline_open * 100) if kline_open > 0 else 0.0
                            delta_modifier, delta_reason = self._calculate_delta_signal_modifier(
                                delta_snapshot, spike_result.side, price_change_pct
                            )
                            
                            if ctx.is_latest_bar:
                                if delta_modifier == 0.0:
                                    logging.info(f"🚫 Delta阻止: {spike_result.signal_type} {spike_result.side} - {delta_reason}")
                                elif delta_modifier < 1.0:
                                    logging.info(f"⚠️ Delta减弱: {spike_result.signal_type} (调节={delta_modifier:.2f}) - {delta_reason}")
                                elif delta_modifier > 1.0:
                                    logging.info(f"✅ Delta增强: {spike_result.signal_type} (调节={delta_modifier:.2f}) - {delta_reason}")
                    
                    if delta_modifier > 0:
                        spike_result.delta_modifier = delta_modifier
                        
                        # 应用 HTF 权重调节（v2.0 软过滤）
                        htf_modifier = cached_htf_buy_modifier if spike_result.side == "buy" else cached_htf_sell_modifier
                        spike_result.htf_modifier = htf_modifier
                        spike_result.strength = spike_result.strength * htf_modifier
                        
                        if ctx.is_latest_bar and htf_modifier != 1.0:
                            logging.info(f"📊 HTF权重调节 Spike: ×{htf_modifier} → 强度={spike_result.strength:.2f}")
                        
                        tp1, tp2, tp1_ratio, is_climax = self._calculate_tp1_tp2(
                            ctx.close, spike_result.stop_loss, spike_result.side, spike_result.base_height,
                            ctx.atr, spike_result.signal_type, ctx.market_state.value, data, i
                        )
                        spike_result.tp1_close_ratio = tp1_ratio
                        spike_result.is_climax = is_climax
                        self._record_signal(arrays, i, spike_result, ctx.market_state.value, ctx.tight_channel_score, tp1, tp2)
                        self._update_signal_cooldown(spike_result.signal_type, i)
                        if spike_result.side == "buy":
                            h2_machine.set_strong_trend()
                        else:
                            l2_machine.set_strong_trend()
                continue
            
            # ---------- 优先级3: Climax 反转 ----------
            climax_result = self._check_climax(data, ctx)
            if climax_result:
                # 应用 HTF 权重调节（v2.0 软过滤）
                htf_modifier = cached_htf_buy_modifier if climax_result.side == "buy" else cached_htf_sell_modifier
                climax_result.htf_modifier = htf_modifier
                climax_result.strength = climax_result.strength * htf_modifier
                
                if ctx.is_latest_bar and htf_modifier != 1.0:
                    logging.info(f"📊 HTF权重调节 Climax: ×{htf_modifier} → 强度={climax_result.strength:.2f}")
                
                tp1, tp2, tp1_ratio, is_climax = self._calculate_tp1_tp2(
                    ctx.close, climax_result.stop_loss, climax_result.side, climax_result.base_height,
                    ctx.atr, climax_result.signal_type, ctx.market_state.value, data, i
                )
                climax_result.tp1_close_ratio = tp1_ratio
                climax_result.is_climax = is_climax
                self._record_signal(arrays, i, climax_result, ctx.market_state.value, ctx.tight_channel_score, tp1, tp2)
                self._update_signal_cooldown(climax_result.signal_type, i)
                continue
            
            # ---------- 优先级4: Wedge 反转 ----------
            wedge_result = self._check_wedge(data, ctx)
            if wedge_result:
                # Wedge_Buy 专用：Delta 背离（价格新低但卖压减弱）则强度 +0.3
                if wedge_result.signal_type == "Wedge_Buy" and ctx.is_latest_bar:
                    if not delta_snapshot_fetched:
                        cached_delta_snapshot = await self._get_delta_snapshot("BTCUSDT")
                        delta_snapshot_fetched = True
                    if cached_delta_snapshot is not None and cached_delta_snapshot.trade_count > 0:
                        kline_open = data.iloc[i]["open"]
                        price_change_pct = (
                            (ctx.close - kline_open) / kline_open * 100
                            if kline_open > 0
                            else 0.0
                        )
                        wedge_boost, wedge_boost_reason = compute_wedge_buy_delta_boost(
                            cached_delta_snapshot, price_change_pct
                        )
                        wedge_result.delta_modifier = wedge_boost
                        if wedge_boost > 1.0:
                            wedge_result.strength += 0.3  # Delta 背离加权
                            logging.info(
                                f"✅ Wedge_Buy Delta背离: 强度+0.3, ×{wedge_boost} - {wedge_boost_reason}"
                            )
                
                # 应用 HTF 权重调节（v2.0 软过滤）
                htf_modifier = cached_htf_buy_modifier if wedge_result.side == "buy" else cached_htf_sell_modifier
                wedge_result.htf_modifier = htf_modifier
                wedge_result.strength = wedge_result.strength * htf_modifier
                
                if ctx.is_latest_bar and htf_modifier != 1.0:
                    logging.info(f"📊 HTF权重调节 Wedge: ×{htf_modifier} → 强度={wedge_result.strength:.2f}")
                
                # Wedge 专用止盈：TP1=EMA，TP2=楔形起点。BTC 5m 楔形易演变为 Wedge Bull/Bear Flag（深度回调），
                # Brooks 高波动保命：TP1(EMA) 处至少平 50% 仓位并移动止损到保本价
                if wedge_result.wedge_tp1_price is not None and wedge_result.wedge_tp2_price is not None:
                    tp1 = wedge_result.wedge_tp1_price
                    tp2 = wedge_result.wedge_tp2_price
                    tp1_ratio = 0.5  # 至少 50% 在 TP1 平仓
                    wedge_result.move_stop_to_breakeven_at_tp1 = True  # TP1 触发后止损移至保本
                    is_climax = False
                else:
                    tp1, tp2, tp1_ratio, is_climax = self._calculate_tp1_tp2(
                        ctx.close, wedge_result.stop_loss, wedge_result.side, wedge_result.base_height,
                        ctx.atr, wedge_result.signal_type, ctx.market_state.value, data, i
                    )
                wedge_result.tp1_close_ratio = tp1_ratio
                wedge_result.is_climax = is_climax
                self._record_signal(arrays, i, wedge_result, ctx.market_state.value, ctx.tight_channel_score, tp1, tp2)
                self._update_signal_cooldown(wedge_result.signal_type, i)
                continue
            
            # ---------- H2/L2 状态机处理 ----------
            # 使用缓存的 HTF 权重调节因子（v2.0 软过滤）
            htf_buy_modifier = cached_htf_buy_modifier
            htf_sell_modifier = cached_htf_sell_modifier
            
            # 获取 Delta 快照（如果需要）
            if ctx.is_latest_bar and not delta_snapshot_fetched:
                cached_delta_snapshot = await self._get_delta_snapshot("BTCUSDT")
                delta_snapshot_fetched = True
            delta_snapshot_for_hl = cached_delta_snapshot if ctx.is_latest_bar else None
            
            # H2 信号处理（允许所有方向，通过权重调节）
            if ctx.allowed_side is None or ctx.allowed_side == "buy":
                h2_result = await self._process_h2_signal(
                    h2_machine, data, ctx, delta_snapshot_for_hl, cached_htf_trend
                )
                if h2_result:
                    # 应用 HTF 权重调节信号强度
                    h2_result.htf_modifier = htf_buy_modifier
                    h2_result.strength = h2_result.strength * htf_buy_modifier
                    
                    # 日志记录 HTF 权重调节
                    if ctx.is_latest_bar and htf_buy_modifier != 1.0:
                        logging.info(f"📊 HTF权重调节 H2: ×{htf_buy_modifier} → 强度={h2_result.strength:.2f}")
                    
                    tp1, tp2, tp1_ratio, is_climax = self._calculate_tp1_tp2(
                        ctx.close, h2_result.stop_loss, h2_result.side, h2_result.base_height,
                        ctx.atr, h2_result.signal_type, ctx.market_state.value, data, i
                    )
                    h2_result.tp1_close_ratio = tp1_ratio
                    h2_result.is_climax = is_climax
                    self._record_signal(arrays, i, h2_result, ctx.market_state.value, ctx.tight_channel_score, tp1, tp2)
                    self._update_signal_cooldown(h2_result.signal_type, i)
            
            # L2 信号处理（允许所有方向，通过权重调节）
            if ctx.allowed_side is None or ctx.allowed_side == "sell":
                l2_result = await self._process_l2_signal(
                    l2_machine, data, ctx, delta_snapshot_for_hl, cached_htf_trend
                )
                if l2_result:
                    # 应用 HTF 权重调节信号强度
                    l2_result.htf_modifier = htf_sell_modifier
                    l2_result.strength = l2_result.strength * htf_sell_modifier
                    
                    # 日志记录 HTF 权重调节
                    if ctx.is_latest_bar and htf_sell_modifier != 1.0:
                        logging.info(f"📊 HTF权重调节 L2: ×{htf_sell_modifier} → 强度={l2_result.strength:.2f}")
                    
                    tp1, tp2, tp1_ratio, is_climax = self._calculate_tp1_tp2(
                        ctx.close, l2_result.stop_loss, l2_result.side, l2_result.base_height,
                        ctx.atr, l2_result.signal_type, ctx.market_state.value, data, i
                    )
                    l2_result.tp1_close_ratio = tp1_ratio
                    l2_result.is_climax = is_climax
                    self._record_signal(arrays, i, l2_result, ctx.market_state.value, ctx.tight_channel_score, tp1, tp2)
                    self._update_signal_cooldown(l2_result.signal_type, i)
        
        # ========== Step 5: 应用 TA-Lib 形态加成 ==========
        self._apply_talib_boost(data, arrays)
        
        # ========== Step 6: 写入结果到 DataFrame ==========
        return self._write_results_to_dataframe(data, arrays)
