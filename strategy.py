"""
Al Brooks 价格行为策略 - 核心入口（决策层）

整合四大阿布价格行为策略（异步版本）：
1. Strong Spike - 强突破直接入场
2. H2/L2 Pullback - 通道回调策略
3. Failed Breakout - 失败突破反转策略
4. Wedge Reversal - 楔形反转策略

关注点分离架构：
┌─────────────────────────────────────────────────────────────────┐
│  strategy.py（决策层）                                           │
│  - 唯一的决策入口，协调所有子模块                                   │
│  - HTF 硬过滤（allows_h2_buy/allows_l2_sell）                    │
│  - HTF 软过滤（get_signal_modifier → _apply_htf_modifier）       │
│  - Delta 过滤协调                                                │
│  - 信号记录与止盈止损计算                                          │
└─────────────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────────┐
│  logic/signal_h2l2.py（H2/L2 形态识别层）                         │
│  - 纯形态识别：H2/L2 状态机                                        │
│  - 信号棒质量校验                                                  │
│  - Delta 基础过滤（强烈反向阻止、轻微反向减弱）                      │
│  - 不负责 HTF 过滤和权重调节                                       │
└─────────────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────────┐
│  logic/htf_filter.py（HTF 数据层）                               │
│  - 获取 1h EMA20 方向和斜率                                       │
│  - 提供趋势判断（Bullish/Bearish/Neutral）                        │
│  - 提供硬过滤方法（allows_h2_buy/allows_l2_sell）                 │
│  - 提供软过滤权重（get_signal_modifier）                          │
│  - 不直接修改信号，由 strategy 统一处理                            │
└─────────────────────────────────────────────────────────────────┘

其他模块：
- logic/market_analyzer.py: 市场状态识别
- logic/patterns.py: 模式检测（Spike/Wedge/Climax/FB/MTR/FinalFlag）
- logic/state_machines.py: H2/L2 状态机
- logic/signal_models.py: BarContext、SignalArrays、SignalResult 数据模型
- logic/signal_tp.py: 止盈与 Climax 检测（SIGNAL_RR_RATIO、calculate_tp1_tp2）
- delta_flow.py: 动态订单流 Delta 分析
"""

import logging
import pandas as pd
from typing import List, Optional, Tuple, Dict

from logic.market_analyzer import MarketState, MarketCycle
from logic.signal_models import BarContext, SignalArrays, SignalResult
from logic.signal_tp import calculate_tp1_tp2 as _calculate_tp1_tp2_fn
from logic.signal_checks import SignalChecker
from logic.signal_h2l2 import H2L2Processor
from logic.signal_recorder import (
    record_signal_impl,
    record_signal_with_tp_impl,
    apply_talib_boost_impl,
    write_results_to_dataframe_impl,
)

# 导入模块化组件
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
from logic.talib_indicators import compute_ema, compute_atr, compute_ema_adaptive
from logic.trader_equation import satisfies_trader_equation as _trader_equation_satisfies

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
        kline_interval: str = "5m",
        use_adaptive_ema: bool = True,
        use_signal_bar_only_stop: Optional[bool] = None,
        tick_size: Optional[float] = None,
    ):
        self.ema_period = ema_period
        self.lookback_period = lookback_period
        self.kline_interval = kline_interval
        self.use_adaptive_ema = use_adaptive_ema
        try:
            from config import USE_SIGNAL_BAR_ONLY_STOP, TICK_SIZE
            default_stop, default_tick = USE_SIGNAL_BAR_ONLY_STOP, TICK_SIZE
        except ImportError:
            default_stop, default_tick = True, 0.01
        self._use_signal_bar_only_stop = use_signal_bar_only_stop if use_signal_bar_only_stop is not None else default_stop
        self._tick_size = tick_size if tick_size is not None else default_tick
        
        # 加载周期自适应参数
        self._params: IntervalParams = get_interval_params(kline_interval)
        
        # 初始化模块化组件（传入周期参数与止损模式）
        self.market_analyzer = MarketAnalyzer(
            ema_period=ema_period,
            kline_interval=kline_interval
        )
        self.pattern_detector = PatternDetector(
            lookback_period=lookback_period,
            kline_interval=kline_interval,
            use_signal_bar_only_stop=self._use_signal_bar_only_stop,
            tick_size=self._tick_size,
        )
        
        # 信号冷却期管理（周期自适应）
        self.SIGNAL_COOLDOWN_BARS = self._params.signal_cooldown_bars
        self._last_signal_bar: Dict[str, int] = {}  # {"Spike_Buy": 100, "Spike_Sell": 95, ...}
        
        # Delta 分析器（从全局获取，与 aggtrade_worker 共享，窗口与 K 线周期对齐）
        self.delta_analyzer: DeltaAnalyzer = get_delta_analyzer(kline_interval=kline_interval)
        
        # HTF 过滤器（1h EMA20 方向过滤）
        # Al Brooks: "大周期的趋势是日内交易最好的保护伞"
        self.htf_filter: HTFFilter = get_htf_filter(htf_interval="1h", ema_period=20)
        
        # TA-Lib 形态检测器（信号增强器）
        self.talib_detector: Optional[TALibPatternDetector] = None
        if TALIB_AVAILABLE:
            self.talib_detector = get_talib_detector()
            logging.info("📊 TA-Lib 形态检测器已启用")
        else:
            logging.warning("⚠️ TA-Lib 不可用，形态增强功能已禁用")

        # 形态检测与 H2/L2 处理器（解耦到 logic.signal_checks / signal_h2l2）
        # 关注点分离：signal_checker 和 h2l2 只做形态识别，HTF 过滤由 strategy 统一处理
        self._signal_checker = SignalChecker(
            self.pattern_detector,
            check_signal_cooldown=self._check_signal_cooldown,
            volume_confirms_breakout=self._volume_confirms_breakout,
        )
        self._h2l2 = H2L2Processor(
            self.pattern_detector,
            check_signal_cooldown=self._check_signal_cooldown,
            calculate_delta_modifier=self._calculate_delta_signal_modifier,
        )
        
        logging.info(
            f"策略已初始化: EMA周期={ema_period}{'(自适应σ)' if use_adaptive_ema else ''}, "
            f"K线周期={kline_interval}, Delta窗口={self.delta_analyzer.WINDOW_SECONDS}秒, "
            f"信号冷却={self.SIGNAL_COOLDOWN_BARS}根K线, "
            f"HTF过滤=1h EMA20, TA-Lib={'启用' if TALIB_AVAILABLE else '禁用'}"
        )
    
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
            "Spike_Buy", "FailedBreakout_Buy", "Wedge_FailedBreakout_Buy", "Climax_Buy",
            "Wedge_Buy", "MTR_Buy", "Final_Flag_Reversal_Buy", "H1_Buy", "H2_Buy"
        ]
        sell_signals = [
            "Spike_Sell", "FailedBreakout_Sell", "Wedge_FailedBreakout_Sell", "Climax_Sell",
            "Wedge_Sell", "MTR_Sell", "Final_Flag_Reversal_Sell", "L1_Sell", "L2_Sell"
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
    
    def _compute_ema(self, df: pd.DataFrame) -> pd.Series:
        """计算 EMA：自适应波动率时用 σ 调节周期，否则固定周期"""
        if self.use_adaptive_ema:
            return compute_ema_adaptive(
                df["close"], df["high"], df["low"],
                base_period=self.ema_period,
                atr_period=14,
                atr_lookback=50,
                min_period=10,
                max_period=35,
            )
        return compute_ema(df["close"], self.ema_period)

    def _compute_atr(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        """计算 ATR (使用 TA-Lib)"""
        return compute_atr(df["high"], df["low"], df["close"], period)
    
    async def _get_delta_snapshot(self, symbol: str = "BTCUSDT") -> Optional[DeltaSnapshot]:
        """
        获取动态订单流 Delta 快照（从全局 Delta 分析器，与 aggtrade_worker 共享）。
        
        Returns:
            DeltaSnapshot: 包含 Delta 分析结果的快照，失败或无数据时返回 None
        """
        try:
            snapshot = await self.delta_analyzer.get_snapshot(symbol)
            if snapshot.trade_count > 0:
                return snapshot
        except Exception as e:
            logging.debug(f"从 Delta 分析器获取快照失败: {e}")
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

    def _calculate_tp1_tp2(
        self, entry_price: float, stop_loss: float, side: str,
        base_height: float,
        signal_type: Optional[str] = None,
        market_state: Optional[str] = None,
        df: Optional[pd.DataFrame] = None,
        current_idx: Optional[int] = None,
        ema: Optional[float] = None,
        pattern_origin: Optional[float] = None,
    ) -> Tuple[float, float, float, bool]:
        """
        委托 logic.signal_tp 计算 TP1/TP2。
        
        新增参数：
        - ema: EMA 值（用于 Wedge/FailedBreakout 的 TP1）
        - pattern_origin: 形态起始点极值（用于 Wedge/FailedBreakout 的 TP2）
        """
        return _calculate_tp1_tp2_fn(
            self._params, entry_price, stop_loss, side, base_height,
            signal_type=signal_type, market_state=market_state, df=df, current_idx=current_idx,
            ema=ema, pattern_origin=pattern_origin,
        )

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
        # 市场周期状态机（Spike / Channel / Trading Range）
        market_cycle = self.market_analyzer.get_market_cycle(data, i, ema, market_state)
        
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
        
        # 力量维度：成交量（用于有效突破确认，可选）
        volume: Optional[float] = None
        avg_volume: Optional[float] = None
        if "volume" in data.columns and i < len(data):
            try:
                volume = float(data.iloc[i]["volume"])
                lookback = 20
                start = max(0, i - lookback)
                if start < i:
                    avg_volume = float(data["volume"].iloc[start:i].mean())
            except (TypeError, ValueError, KeyError):
                pass
        
        return BarContext(
            i=i,
            close=close,
            high=high,
            low=low,
            ema=ema,
            atr=atr,
            market_state=market_state,
            market_cycle=market_cycle,
            trend_direction=trend_direction,
            trend_strength=trend_strength,
            tight_channel_score=tight_channel_score,
            tight_channel_direction=tight_channel_direction,
            is_strong_trend_mode=is_strong_trend_mode,
            allowed_side=allowed_side,
            is_latest_bar=(i == total_bars - 1),
            volume=volume,
            avg_volume=avg_volume,
        )
    
    def _volume_confirms_breakout(self, ctx: BarContext) -> bool:
        """
        成交量确认突破：当次或近期成交量 > 近期均量×系数时，视为有效突破。
        可选过滤，默认关闭；开启后仅对突破类信号（如 Spike）要求放量。
        """
        try:
            from config import VOLUME_BREAKOUT_CONFIRM_ENABLED, VOLUME_BREAKOUT_MULTIPLIER
            if not VOLUME_BREAKOUT_CONFIRM_ENABLED:
                return True
            mult = VOLUME_BREAKOUT_MULTIPLIER
        except ImportError:
            return True
        if ctx.volume is None or ctx.avg_volume is None or ctx.avg_volume <= 0:
            return True  # 无成交量数据时不拦截
        return ctx.volume >= ctx.avg_volume * mult
    
    def _satisfies_trader_equation(
        self,
        entry_price: float,
        stop_loss: float,
        tp1: float,
        tp2: float,
        tp1_close_ratio: float,
        side: str,
        win_rate: Optional[float] = None,
    ) -> bool:
        """交易者方程：WinRate × Reward > Risk 时才允许交易（委托公共函数）。"""
        return _trader_equation_satisfies(
            entry_price, stop_loss, tp1, tp2, tp1_close_ratio, side,
            win_rate=win_rate, enabled=True,
        )

    def _apply_htf_modifier_to_result(
        self,
        result: SignalResult,
        cached_htf_buy_modifier: float,
        cached_htf_sell_modifier: float,
        ctx: BarContext,
    ) -> None:
        """对信号结果应用 HTF 权重调节并写日志。"""
        htf_modifier = cached_htf_buy_modifier if result.side == "buy" else cached_htf_sell_modifier
        result.htf_modifier = htf_modifier
        result.strength = result.strength * htf_modifier
        if ctx.is_latest_bar and htf_modifier != 1.0:
            # HTF权重调节是常见操作，降级为 DEBUG
            logging.debug(f"📊 HTF权重调节 {result.signal_type}: ×{htf_modifier} → 强度={result.strength:.2f}")

    def _record_signal_with_tp(
        self,
        arrays: SignalArrays,
        i: int,
        result: SignalResult,
        ctx: BarContext,
        entry_price: float,
        data: pd.DataFrame,
    ) -> None:
        """调用入口 - 实际逻辑已提取到 signal_recorder.py"""
        record_signal_with_tp_impl(
            arrays, i, result, ctx, entry_price, data,
            calculate_tp1_tp2_func=self._calculate_tp1_tp2,
            is_likely_wick_bar_func=self.pattern_detector.is_likely_wick_bar,
            satisfies_trader_equation_func=self._satisfies_trader_equation,
            update_signal_cooldown_func=self._update_signal_cooldown,
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
        entry_price: float,
        data: Optional[pd.DataFrame] = None,
        atr: Optional[float] = None,
    ) -> None:
        """调用入口 - 实际逻辑已提取到 signal_recorder.py"""
        record_signal_impl(
            arrays, i, result, market_state_value, tight_channel_score,
            tp1, tp2, entry_price, data, atr,
            is_likely_wick_bar_func=self.pattern_detector.is_likely_wick_bar,
            satisfies_trader_equation_func=self._satisfies_trader_equation,
        )
    
    def _check_failed_breakout(
        self, data: pd.DataFrame, ctx: BarContext
    ) -> Optional[SignalResult]:
        """委托 logic.signal_checks 检测 Failed Breakout。"""
        return self._signal_checker.check_failed_breakout(data, ctx)

    def _check_spike(
        self, data: pd.DataFrame, ctx: BarContext
    ) -> Optional[SignalResult]:
        """委托 logic.signal_checks 检测 Strong Spike。"""
        return self._signal_checker.check_spike(data, ctx)

    def _check_climax(
        self, data: pd.DataFrame, ctx: BarContext
    ) -> Optional[SignalResult]:
        """委托 logic.signal_checks 检测 Climax 反转。"""
        return self._signal_checker.check_climax(data, ctx)

    def _check_wedge(
        self, data: pd.DataFrame, ctx: BarContext
    ) -> Optional[SignalResult]:
        """委托 logic.signal_checks 检测 Wedge 反转。"""
        return self._signal_checker.check_wedge(data, ctx)

    def _check_mtr(
        self, data: pd.DataFrame, ctx: BarContext
    ) -> Optional[SignalResult]:
        """委托 logic.signal_checks 检测 MTR 主要趋势反转（利用 BarContext 市场状态）。"""
        return self._signal_checker.check_mtr(data, ctx)

    def _check_final_flag(
        self, data: pd.DataFrame, ctx: BarContext
    ) -> Optional[SignalResult]:
        """委托 logic.signal_checks 检测 Final Flag Reversal（终极旗形反转）。"""
        final_flag_info = self.market_analyzer.get_final_flag_info()
        return self._signal_checker.check_final_flag(data, ctx, final_flag_info)

    def _validate_h2l2_signal_bar(
        self, ctx: BarContext, data: pd.DataFrame, signal_side: str, row_index: int
    ) -> Tuple[bool, str]:
        """委托 logic.signal_h2l2 校验 H2/L2 信号棒。"""
        return self._h2l2.validate_h2l2_signal_bar(ctx, data, signal_side, row_index)

    async def _process_h2_signal(
        self,
        h2_machine: H2StateMachine,
        data: pd.DataFrame,
        ctx: BarContext,
        cached_delta_snapshot: Optional[DeltaSnapshot],
    ) -> Optional[SignalResult]:
        """
        委托 logic.signal_h2l2 处理 H2 信号（纯形态识别）
        
        注意：HTF 硬过滤在调用此方法前完成，HTF 权重由 strategy 统一应用
        """
        return await self._h2l2.process_h2_signal(
            h2_machine, data, ctx, cached_delta_snapshot
        )

    async def _process_l2_signal(
        self,
        l2_machine: L2StateMachine,
        data: pd.DataFrame,
        ctx: BarContext,
        cached_delta_snapshot: Optional[DeltaSnapshot],
    ) -> Optional[SignalResult]:
        """
        委托 logic.signal_h2l2 处理 L2 信号（纯形态识别）
        
        注意：HTF 硬过滤在调用此方法前完成，HTF 权重由 strategy 统一应用
        """
        return await self._h2l2.process_l2_signal(
            l2_machine, data, ctx, cached_delta_snapshot
        )

    def _apply_talib_boost(
        self, 
        data: pd.DataFrame, 
        arrays: SignalArrays
    ) -> None:
        """调用入口 - 实际逻辑已提取到 signal_recorder.py"""
        apply_talib_boost_impl(
            data, arrays, self.talib_detector, calculate_talib_boost
        )
    
    def _write_results_to_dataframe(
        self, 
        data: pd.DataFrame, 
        arrays: SignalArrays
    ) -> pd.DataFrame:
        """调用入口 - 实际逻辑已提取到 signal_recorder.py"""
        return write_results_to_dataframe_impl(data, arrays)

    async def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        异步生成交易信号（重构后主入口）
        
        使用模块化的辅助方法来简化主循环逻辑：
        - _precompute_indicators(): 预计算技术指标
        - _get_bar_context(): 获取单根K线的市场上下文
        - _check_failed_breakout/spike/climax/wedge/mtr/final_flag(): 检测各类形态信号
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
            
            # ========== 最新 K 线：记录市场状态日志（DEBUG 级别）==========
            if ctx.is_latest_bar:
                trend_icon = "📈" if ctx.trend_direction == "up" else "📉" if ctx.trend_direction == "down" else "➡️"
                allowed_icon = "🔒" if ctx.allowed_side else "🔓"
                h2_state = h2_machine.state.value if h2_machine else "N/A"
                l2_state = l2_machine.state.value if l2_machine else "N/A"
                # 市场状态详情降级为 DEBUG（生产环境不需要每根K线都打印）
                logging.debug(
                    f"📍 市场状态: {ctx.market_state.value} | "
                    f"周期: {ctx.market_cycle.value} | "
                    f"{trend_icon} 趋势: {ctx.trend_direction or '无'}({ctx.trend_strength:.0%}) | "
                    f"{allowed_icon} 允许方向: {ctx.allowed_side or '双向'} | "
                    f"H2状态: {h2_state} | L2状态: {l2_state}"
                )
            
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
                        limit_price, stop_loss, side, base_height, signal_type,
                        ctx.market_state.value, data, i
                    )
                    result = SignalResult(
                        signal_type=signal_type, side=side, stop_loss=stop_loss,
                        base_height=base_height, tp1_close_ratio=tp1_ratio, is_climax=is_climax,
                        entry_mode="Limit_Entry", is_high_risk=is_high_risk
                    )
                    self._record_signal(arrays, i, result, ctx.market_state.value, ctx.tight_channel_score, tp1, tp2, limit_price, data, ctx.atr)
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
            
            # ==========================================================================
            # Al Brooks 形态优先级（符合 PA 交易理念）
            # ==========================================================================
            # 优先级原则：
            # 1. Climax - 极端信号需要立即响应
            # 2. Spike - 趋势确立，Always In
            # 3. H2/L2 - 二次入场是最常用、最可靠的方式
            # 4. Failed Breakout - 只在 TradingRange 边界触发
            # 5. Wedge - 楔形反转需要明确结构
            # 6. MTR - 主要趋势反转，需要更多确认
            # 7. Final Flag - 趋势耗尽的最后挣扎
            # ==========================================================================
            
            # ---------- 优先级1: Climax 反转（极端信号，需要立即响应）----------
            # Al Brooks: "Climax 是市场极端情绪的表现，错过就没了"
            climax_result = self._check_climax(data, ctx)
            if climax_result:
                self._apply_htf_modifier_to_result(climax_result, cached_htf_buy_modifier, cached_htf_sell_modifier, ctx)
                self._record_signal_with_tp(arrays, i, climax_result, ctx, ctx.close, data)
                continue
            
            # ---------- 优先级2: Strong Spike（趋势确立，Always In）----------
            # Al Brooks: "强突破后应该 Always In，站在趋势一边"
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
                                    # 信号被完全阻止，使用 WARNING 级别
                                    logging.warning(f"🚫 Delta阻止: {spike_result.signal_type} {spike_result.side} - {delta_reason}")
                                elif delta_modifier < 1.0:
                                    # 信号被减弱，使用 DEBUG 级别（常见情况）
                                    logging.debug(f"⚠️ Delta减弱: {spike_result.signal_type} (调节={delta_modifier:.2f}) - {delta_reason}")
                                elif delta_modifier > 1.0:
                                    # 信号被增强，使用 DEBUG 级别
                                    logging.debug(f"✅ Delta增强: {spike_result.signal_type} (调节={delta_modifier:.2f}) - {delta_reason}")
                    
                    if delta_modifier > 0:
                        spike_result.delta_modifier = delta_modifier
                        self._apply_htf_modifier_to_result(spike_result, cached_htf_buy_modifier, cached_htf_sell_modifier, ctx)
                        self._record_signal_with_tp(arrays, i, spike_result, ctx, ctx.close, data)
                        if spike_result.side == "buy":
                            h2_machine.set_strong_trend()
                        else:
                            l2_machine.set_strong_trend()
                continue
            
            # ---------- 优先级3: H2/L2 顺势二次入场（最常用、最可靠）----------
            # Al Brooks: "大多数交易日我只做 H2 买入或 L2 卖出"
            # 在趋势/通道市场中，H2/L2 是主力入场方式
            if ctx.market_cycle != MarketCycle.SPIKE:  # Spike 周期内不做 H2/L2，等待回调
                # 获取 Delta 快照（如果需要）
                if ctx.is_latest_bar and not delta_snapshot_fetched:
                    cached_delta_snapshot = await self._get_delta_snapshot("BTCUSDT")
                    delta_snapshot_fetched = True
                delta_snapshot_for_hl = cached_delta_snapshot if ctx.is_latest_bar else None
                
                h2l2_triggered = False
                
                # H2 信号处理
                if ctx.allowed_side is None or ctx.allowed_side == "buy":
                    htf_allowed, htf_reason = self.htf_filter.allows_h2_buy(ctx.close)
                    if not htf_allowed:
                        if ctx.is_latest_bar:
                            logging.debug(f"🚫 H2 HTF硬过滤: {htf_reason}")
                    else:
                        h2_result = await self._process_h2_signal(
                            h2_machine, data, ctx, delta_snapshot_for_hl
                        )
                        if h2_result:
                            self._apply_htf_modifier_to_result(h2_result, cached_htf_buy_modifier, cached_htf_sell_modifier, ctx)
                            self._record_signal_with_tp(arrays, i, h2_result, ctx, ctx.close, data)
                            h2l2_triggered = True
                
                # L2 信号处理（H2 未触发时才检查 L2）
                if not h2l2_triggered and (ctx.allowed_side is None or ctx.allowed_side == "sell"):
                    htf_allowed, htf_reason = self.htf_filter.allows_l2_sell(ctx.close)
                    if not htf_allowed:
                        if ctx.is_latest_bar:
                            logging.debug(f"🚫 L2 HTF硬过滤: {htf_reason}")
                    else:
                        l2_result = await self._process_l2_signal(
                            l2_machine, data, ctx, delta_snapshot_for_hl
                        )
                        if l2_result:
                            self._apply_htf_modifier_to_result(l2_result, cached_htf_buy_modifier, cached_htf_sell_modifier, ctx)
                            self._record_signal_with_tp(arrays, i, l2_result, ctx, ctx.close, data)
                            h2l2_triggered = True
                
                if h2l2_triggered:
                    continue
            
            # ---------- 优先级4: Failed Breakout（只在 TradingRange 边界触发）----------
            # Al Brooks: "假突破在区间边界最有效，趋势中假突破反转成功率低"
            if ctx.market_state == MarketState.TRADING_RANGE and ctx.market_cycle != MarketCycle.SPIKE:
                fb_result = self._check_failed_breakout(data, ctx)
                if fb_result:
                    self._apply_htf_modifier_to_result(fb_result, cached_htf_buy_modifier, cached_htf_sell_modifier, ctx)
                    self._record_signal_with_tp(arrays, i, fb_result, ctx, ctx.close, data)
                    continue
            
            # ---------- 优先级5: Wedge 反转（仅在非 Spike 周期）----------
            # Al Brooks: "楔形三推是经典反转形态，第三推失败是高胜率入场点"
            if ctx.market_cycle != MarketCycle.SPIKE:
                wedge_result = self._check_wedge(data, ctx)
            else:
                wedge_result = None
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
                            # Delta 背离加权是正常分析过程，降级为 DEBUG
                            logging.debug(
                                f"✅ Wedge_Buy Delta背离: 强度+0.3, ×{wedge_boost} - {wedge_boost_reason}"
                            )
                
                self._apply_htf_modifier_to_result(wedge_result, cached_htf_buy_modifier, cached_htf_sell_modifier, ctx)
                # Wedge 专用止盈：TP1=EMA，TP2=楔形起点
                if wedge_result.wedge_tp1_price is not None and wedge_result.wedge_tp2_price is not None:
                    tp1 = wedge_result.wedge_tp1_price
                    tp2 = wedge_result.wedge_tp2_price
                    tp1_ratio = 0.5
                    wedge_result.move_stop_to_breakeven_at_tp1 = True
                    is_climax = False
                else:
                    tp1, tp2, tp1_ratio, is_climax = self._calculate_tp1_tp2(
                        ctx.close, wedge_result.stop_loss, wedge_result.side, wedge_result.base_height,
                        wedge_result.signal_type, ctx.market_state.value, data, i
                    )
                wedge_result.tp1_close_ratio = tp1_ratio
                wedge_result.is_climax = is_climax
                self._record_signal(arrays, i, wedge_result, ctx.market_state.value, ctx.tight_channel_score, tp1, tp2, ctx.close, data, ctx.atr)
                self._update_signal_cooldown(wedge_result.signal_type, i)
                continue
            
            # ---------- 优先级6: MTR 主要趋势反转----------
            # Al Brooks: "MTR 需要多重确认：强趋势 → 突破 EMA → 回测极值 → 强反转棒"
            if ctx.market_cycle != MarketCycle.SPIKE:
                mtr_result = self._check_mtr(data, ctx)
            else:
                mtr_result = None
            if mtr_result:
                self._apply_htf_modifier_to_result(mtr_result, cached_htf_buy_modifier, cached_htf_sell_modifier, ctx)
                self._record_signal_with_tp(arrays, i, mtr_result, ctx, ctx.close, data)
                continue
            
            # ---------- 优先级7: Final Flag Reversal（终极旗形反转）----------
            # Al Brooks: "Final Flag 是趋势耗尽的最后挣扎，突破失败后是高胜率反转入场点"
            if ctx.market_state == MarketState.FINAL_FLAG:
                final_flag_result = self._check_final_flag(data, ctx)
                if final_flag_result:
                    self._apply_htf_modifier_to_result(final_flag_result, cached_htf_buy_modifier, cached_htf_sell_modifier, ctx)
                    self._record_signal_with_tp(arrays, i, final_flag_result, ctx, ctx.close, data)
                    continue
            
            # ========== 最新 K 线：如果没有信号，记录原因 ==========
            if ctx.is_latest_bar:
                # 收集跳过原因
                skip_reasons = []
                
                # Spike 周期阻断反转信号
                if ctx.market_cycle == MarketCycle.SPIKE:
                    skip_reasons.append("Spike周期(反转信号阻断)")
                
                # 强趋势模式
                if ctx.is_strong_trend_mode:
                    skip_reasons.append(f"强趋势模式(只允许{ctx.allowed_side or '无'})")
                
                # H2/L2 状态
                if h2_machine.state.value != "WAITING_FOR_PULLBACK":
                    skip_reasons.append(f"H2等待中({h2_machine.state.value})")
                if l2_machine.state.value != "WAITING_FOR_BOUNCE":
                    skip_reasons.append(f"L2等待中({l2_machine.state.value})")
                
                # 无信号原因降级为 DEBUG（生产环境不需要每根K线都打印）
                if skip_reasons:
                    logging.debug(f"⏸️ 无信号原因: {', '.join(skip_reasons)}")
                else:
                    logging.debug(f"⏸️ 无信号: 当前形态不满足入场条件")
        
        # ========== Step 5: 应用 TA-Lib 形态加成 ==========
        self._apply_talib_boost(data, arrays)
        
        # ========== Step 6: 写入结果到 DataFrame ==========
        return self._write_results_to_dataframe(data, arrays)
