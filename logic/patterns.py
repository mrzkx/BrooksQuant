"""
模式检测器

负责 Wedge、Failed Breakout、Spike、Climax、MTR 的检测逻辑

Al Brooks 核心模式：
- Strong Spike: 强突破直接入场
- Failed Breakout: 失败突破反转
- Wedge Reversal: 楔形反转（三次推进）
- Climax Reversal: 高潮竭尽反转
- MTR (Major Trend Reversal): 主要趋势反转（强趋势→突破 EMA→回测极值→强反转棒/H2/L2）
- Final Flag Reversal: 终极旗形反转（TightChannel 后远离 EMA 处的横盘失败突破）
"""

import logging
import pandas as pd
from typing import Optional, Tuple, List
from .market_analyzer import MarketState
from .interval_params import get_interval_params, IntervalParams
from .wedge_reversal import (
    find_swing_peaks,
    find_swing_troughs,
    find_three_lower_highs,
    find_three_higher_lows,
    detect_wedge_reversal_impl,
)
from .final_flag_reversal import detect_final_flag_reversal_impl


class PatternDetector:
    """
    模式检测器
    
    封装所有 Al Brooks 价格行为模式的检测逻辑
    
    周期自适应：
    - 参数根据 K 线周期自动调整
    - 短周期更严格，长周期更宽松
    """
    
    # ========== 默认参数（5m 周期）==========
    # 这些默认值仅在未指定周期时使用
    BTC_MIN_BODY_RATIO = 0.60
    BTC_CLOSE_POSITION_PCT = 0.20
    
    def __init__(
        self,
        lookback_period: int = 20,
        kline_interval: str = "5m",
        use_signal_bar_only_stop: bool = False,
        tick_size: float = 0.01,
    ):
        self.lookback_period = lookback_period
        self.kline_interval = kline_interval
        self._use_signal_bar_only_stop = use_signal_bar_only_stop
        self._tick_size = max(0.0, float(tick_size))
        
        # 加载周期自适应参数
        self._params: IntervalParams = get_interval_params(kline_interval)
        
        # 更新类属性为周期参数
        self.BTC_MIN_BODY_RATIO = self._params.min_body_ratio
        self.BTC_CLOSE_POSITION_PCT = self._params.close_position_pct
        
        logging.info(
            f"📐 PatternDetector 初始化: 周期={kline_interval}, "
            f"实体占比≥{self._params.min_body_ratio:.0%}, "
            f"收盘位置≤{self._params.close_position_pct:.0%}, "
            f"止损模式={'信号棒极值+TickSize' if use_signal_bar_only_stop else '两棒+ATR'}"
        )
    
    @staticmethod
    def is_likely_wick_bar(
        df: pd.DataFrame,
        i: int,
        atr: Optional[float] = None,
        range_atr_mult: float = 2.0,
        body_ratio_max: float = 0.25,
    ) -> bool:
        """
        插针行情检测：单根 K 线内“插针”后快速收回（影线极大、实体极小）时返回 True。
        此类 bar 不作为有效信号棒或入场 bar，避免非理性波动触发假信号。
        
        条件：(high - low) > range_atr_mult * ATR 且 实体/全长 <= body_ratio_max。
        """
        if i < 0 or i >= len(df):
            return False
        row = df.iloc[i]
        kline_range = float(row["high"]) - float(row["low"])
        if kline_range <= 0:
            return False
        body_size = abs(float(row["close"]) - float(row["open"]))
        body_ratio = body_size / kline_range
        if body_ratio > body_ratio_max:
            return False
        if atr is None or atr <= 0:
            return False
        return kline_range > range_atr_mult * atr
    
    @staticmethod
    def _should_enable_sensitive_mode(
        df: pd.DataFrame,
        i: int,
        lookback: int = 20,
    ) -> bool:
        """
        检测是否应该启用灵敏模式
        
        条件：如果过去 N 根 K 线都没有生成交易信号，则启用灵敏模式
        
        实现：检查过去 20 根 K 线的 'signal' 列是否全为空或 None
        如果没有 'signal' 列，则检查波动率是否过低（ATR < 平均 ATR 的 50%）
        
        Args:
            df: K线数据
            i: 当前 K 线索引
            lookback: 回看周期
        
        Returns:
            True 表示应该启用灵敏模式
        """
        if i < lookback:
            return False
        
        recent = df.iloc[max(0, i - lookback + 1) : i + 1]
        
        # 方法1：检查 'signal' 列（如果存在）
        if "signal" in recent.columns:
            # 检查是否所有信号都为空
            signals = recent["signal"]
            non_empty_signals = signals.dropna()
            if len(non_empty_signals) == 0 or (non_empty_signals == "").all():
                # 使用 DEBUG 级别，避免历史回放时大量输出
                logging.debug(
                    f"🔧 检测到无信号期: 过去 {lookback} 根 K 线无交易信号，启用灵敏模式"
                )
                return True
        
        # 方法2：检查波动率（ATR）是否过低
        if "atr" in recent.columns:
            current_atr = float(recent.iloc[-1]["atr"]) if len(recent) > 0 else 0
            avg_atr = float(recent["atr"].mean()) if len(recent) > 0 else 0
            
            # 当前 ATR < 平均 ATR 的 50% 表示波动率极低
            if avg_atr > 0 and current_atr < avg_atr * 0.5:
                # 使用 DEBUG 级别，避免历史回放时大量输出
                logging.debug(
                    f"🔧 检测到低波动率: ATR={current_atr:.2f} < 平均{avg_atr:.2f}×50%，启用灵敏模式"
                )
                return True
        
        # 方法3：检查实体大小是否持续偏小
        if "body_size" in recent.columns:
            avg_body = float(recent["body_size"].mean()) if len(recent) > 0 else 0
            max_body = float(recent["body_size"].max()) if len(recent) > 0 else 0
            
            # 最大实体 < 平均实体的 1.5 倍，说明没有明显的趋势棒
            if avg_body > 0 and max_body < avg_body * 1.5:
                logging.debug(
                    f"检测到弱势期: 最大实体={max_body:.2f} < 平均{avg_body:.2f}×1.5"
                )
                return True
        
        return False
    
    @staticmethod
    def validate_signal_close(row: pd.Series, side: str, min_close_ratio: float = 0.75) -> bool:
        """
        验证K线收盘价位置是否符合信号要求（通用版）
        
        买入信号：收盘价必须在K线顶部 (1-min_close_ratio) 区域
        卖出信号：收盘价必须在K线底部 (1-min_close_ratio) 区域
        min_close_ratio=0.75 即顶部/底部 25%；交易区间 BLSH 可放宽为 0.65（35%）
        """
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        
        kline_range = high - low
        if kline_range == 0:
            return False
        
        if side == "buy":
            return bool((close - low) / kline_range >= min_close_ratio)
        else:
            return bool((high - close) / kline_range >= min_close_ratio)
    
    @classmethod
    def validate_btc_signal_bar(
        cls, 
        row: pd.Series, 
        side: str,
        min_body_ratio: Optional[float] = None,
        close_position_pct: Optional[float] = None,
        df: Optional[pd.DataFrame] = None,
        i: Optional[int] = None,
        signal_type: Optional[str] = None,
        sensitive_mode: bool = False,
    ) -> tuple[bool, str]:
        """
        BTC 专用信号棒质量验证（针对高波动长影线特性 + 背景比较 + 灵敏模式）
        
        Al Brooks: "信号棒的质量决定了交易的成功率"
        
        基础要求：
        1. 实体必须占全长的 60% 以上（过滤长影线噪音）
        2. 买入信号：收盘价必须在最高 20% 区域（强势收盘）
        3. 卖出信号：收盘价必须在最低 20% 区域（弱势收盘）
        4. 信号棒方向必须与交易方向一致（买=阳线，卖=阴线）
        
        背景比较（需提供 df 和 i）：
        5. 相对大小：信号棒实体必须大于前三根 K 线实体的平均值
        6. 低重叠度：信号棒实体与前一根棒的实体重叠部分不应超过 50%
        7. 影线要求：反转棒（Wedge/MTR）的反向影线必须极小（<15%）
        
        灵敏模式（sensitive_mode=True）：
        - 当过去 20 根 K 线没有成交时自动启用
        - min_body_ratio 从 50% 下调至 40%
        - close_position_pct 从 20% 放宽至 28%
        
        Args:
            row: K线数据
            side: 交易方向 ("buy" 或 "sell")
            min_body_ratio: 最小实体占比（默认 0.60）
            close_position_pct: 收盘位置要求（默认 0.20，即顶部/底部 20%）
            df: K线 DataFrame（用于背景比较，可选）
            i: 当前 K 线索引（用于背景比较，可选）
            signal_type: 信号类型（用于判断影线要求，可选）
            sensitive_mode: 是否启用灵敏模式（自动检测或手动指定）
        
        Returns:
            (is_valid, reason): 是否有效及原因
        """
        # ⭐ 灵敏模式自动检测：如果过去 20 根 K 线没有生成信号
        if df is not None and i is not None and not sensitive_mode:
            sensitive_mode = cls._should_enable_sensitive_mode(df, i, lookback=20)
        
        if min_body_ratio is None:
            min_body_ratio = cls.BTC_MIN_BODY_RATIO
        if close_position_pct is None:
            close_position_pct = cls.BTC_CLOSE_POSITION_PCT
        
        # ⭐ 灵敏模式：下调门槛
        if sensitive_mode:
            # 实体占比从默认值下调 20%（例如 50% → 40%）
            min_body_ratio = max(0.35, min_body_ratio - 0.10)
            # 收盘位置放宽 40%（例如 20% → 28%）
            close_position_pct = min(0.35, close_position_pct + 0.08)
            logging.debug(
                f"🔧 启用灵敏模式: min_body_ratio={min_body_ratio:.0%}, "
                f"close_position_pct={close_position_pct:.0%}"
            )
        
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        open_price = float(row["open"])
        
        kline_range = high - low
        if kline_range == 0:
            return (False, "K线范围为0")
        
        body_size = abs(close - open_price)
        body_ratio = body_size / kline_range
        
        # 实体的上下边界
        body_top = max(close, open_price)
        body_bottom = min(close, open_price)
        
        # ========== Al Brooks 修正：Pin Bar 提前检测 ==========
        # "Pin Bar（长影线）是强烈的反转信号" - Al Brooks
        # Pin Bar 的特征是影线 > 实体×2.5，且影线 > K线范围×30%
        # 关键是影线的方向和长度，而非实体占比
        is_pin_bar = False
        if side == "buy":
            # 买入 Pin Bar：下影线（拒绝更低价格）
            lower_wick = body_bottom - low
            if body_size > 0 and lower_wick > body_size * 2.5 and lower_wick > kline_range * 0.30:
                is_pin_bar = True
                logging.debug(f"✅ Pin Bar 信号棒(买入): 下影线{lower_wick:.2f} > 实体{body_size:.2f}×2.5, 占比{lower_wick/kline_range:.1%}")
        else:
            # 卖出 Pin Bar：上影线（拒绝更高价格）
            upper_wick = high - body_top
            if body_size > 0 and upper_wick > body_size * 2.5 and upper_wick > kline_range * 0.30:
                is_pin_bar = True
                logging.debug(f"✅ Pin Bar 信号棒(卖出): 上影线{upper_wick:.2f} > 实体{body_size:.2f}×2.5, 占比{upper_wick/kline_range:.1%}")
        
        # ========== 条件1: 实体占比检查 ==========
        # Al Brooks 修正：Pin Bar 例外 - 直接跳过实体占比检查
        if is_pin_bar:
            # Pin Bar 仍需验证收盘位置和方向
            logging.debug(f"Pin Bar 例外: 跳过实体占比检查")
        elif body_ratio < min_body_ratio:
            return (False, f"实体占比不足({body_ratio:.1%}<{min_body_ratio:.0%})")
        
        # ========== 条件2: 信号棒方向检查 ==========
        is_bullish = close > open_price
        is_bearish = close < open_price
        
        if side == "buy" and not is_bullish:
            return (False, "买入信号需要阳线")
        if side == "sell" and not is_bearish:
            return (False, "卖出信号需要阴线")
        
        # ========== 条件3: 收盘价位置检查 ==========
        if side == "buy":
            # 买入信号：收盘价必须在顶部 20% 区域
            close_from_high = (high - close) / kline_range
            if close_from_high > close_position_pct:
                return (False, f"收盘价未在顶部{close_position_pct:.0%}区域(距顶{close_from_high:.1%})")
        else:
            # 卖出信号：收盘价必须在底部 20% 区域
            close_from_low = (close - low) / kline_range
            if close_from_low > close_position_pct:
                return (False, f"收盘价未在底部{close_position_pct:.0%}区域(距底{close_from_low:.1%})")
        
        # ========== 背景比较（需要 df 和 i）==========
        if df is not None and i is not None and i >= 3:
            # Pin Bar 已在前面检测过（is_pin_bar 变量）
            # Al Brooks: Pin Bar 例外跳过部分背景比较检查
            
            # ---------- 条件4: 相对大小 ----------
            # 信号棒实体必须大于前三根 K 线实体的平均值
            # Pin Bar 例外：跳过此检查
            if not is_pin_bar:
                prev_bodies = []
                for j in range(i - 3, i):
                    if j >= 0 and j < len(df):
                        prev_bar = df.iloc[j]
                        prev_body = abs(float(prev_bar["close"]) - float(prev_bar["open"]))
                        prev_bodies.append(prev_body)
                
                if prev_bodies:
                    avg_prev_body = sum(prev_bodies) / len(prev_bodies)
                    if body_size <= avg_prev_body:
                        return (False, f"实体不够大(当前{body_size:.2f}≤前3根均值{avg_prev_body:.2f})")
            
            # ---------- 条件5: 低重叠度 ----------
            # 信号棒实体与前一根棒的实体重叠部分不应超过 50%
            if i >= 1:
                prev_bar = df.iloc[i - 1]
                prev_close = float(prev_bar["close"])
                prev_open = float(prev_bar["open"])
                prev_body_top = max(prev_close, prev_open)
                prev_body_bottom = min(prev_close, prev_open)
                prev_body_size = abs(prev_close - prev_open)
                
                # 计算重叠区域
                overlap_top = min(body_top, prev_body_top)
                overlap_bottom = max(body_bottom, prev_body_bottom)
                overlap_size = max(0, overlap_top - overlap_bottom)
                
                # 重叠比例（相对于信号棒实体）
                if body_size > 0:
                    overlap_ratio = overlap_size / body_size
                    if overlap_ratio > 0.50:
                        return (False, f"实体重叠过多({overlap_ratio:.1%}>50%，市场震荡)")
            
            # ---------- 条件6: 影线要求（反转棒）----------
            # 判断是否为反转信号（Wedge/MTR/Climax/FailedBreakout）
            is_reversal_signal = (
                signal_type is not None and (
                    signal_type.startswith("Wedge_") or
                    signal_type.startswith("MTR_") or
                    signal_type.startswith("Climax_") or
                    signal_type.startswith("FailedBreakout_") or
                    signal_type.startswith("FinalFlag_")
                )
            )
            
            # Al Brooks: Climax 后的反转信号可以容忍更长的反向影线
            # 因为 Climax 本身已经证明了极端情绪
            is_climax_signal = signal_type is not None and signal_type.startswith("Climax_")
            
            if is_reversal_signal and not is_pin_bar:
                # 反转棒的"反向影线"（推力方向的影线）必须极小
                # 买入反转：上影线必须极小（看空的推力被拒绝）
                # 卖出反转：下影线必须极小（看多的推力被拒绝）
                # Climax 信号放宽到 25%，其他反转信号 15%
                max_opposing_wick_ratio = 0.25 if is_climax_signal else 0.15
                
                if side == "buy":
                    # 买入反转：检查上影线（空头推力）
                    upper_wick = high - body_top
                    upper_wick_ratio = upper_wick / kline_range if kline_range > 0 else 0
                    if upper_wick_ratio > max_opposing_wick_ratio:
                        return (False, f"反转棒上影线过大({upper_wick_ratio:.1%}>{max_opposing_wick_ratio:.0%})")
                else:
                    # 卖出反转：检查下影线（多头推力）
                    lower_wick = body_bottom - low
                    lower_wick_ratio = lower_wick / kline_range if kline_range > 0 else 0
                    if lower_wick_ratio > max_opposing_wick_ratio:
                        return (False, f"反转棒下影线过大({lower_wick_ratio:.1%}>{max_opposing_wick_ratio:.0%})")
        
        return (True, "信号棒质量合格")
    
    def calculate_unified_stop_loss(
        self, df: pd.DataFrame, i: int, side: str, entry_price: float, atr: Optional[float] = None
    ) -> Optional[float]:
        """
        Al Brooks 风格止损计算（简化版 + 动态缓冲）
        
        核心原则：止损放在 Signal Bar 和 Entry Bar 的极值外
        
        Al Brooks: "如果市场回到 Signal Bar 之外，说明你的判断错了"
        
        止损逻辑：
        - 买入止损：min(SignalBar.Low, EntryBar.Low) - buffer
        - 卖出止损：max(SignalBar.High, EntryBar.High) + buffer
        
        Al Brooks 修正：动态止损缓冲
        - buffer = max(0.3 * ATR, 0.5% * entry_price)
        - 原因：BTC 高波动时固定比例太小，低波动时 ATR 太窄
        - 取两者较大值确保足够的缓冲空间
        
        High Risk Filter（保护性约束）：
        - 如果止损距离超过 3 × ATR，认为风险过大，返回 None 放弃信号
        
        Args:
            df: K线数据
            i: 当前 K 线索引（Entry Bar）
            side: 交易方向 "buy" / "sell"
            entry_price: 入场价格
            atr: ATR 值
        
        Returns:
            止损价格，或 None（表示风险过大，应放弃信号）
        """
        if i < 1:
            return entry_price * (0.98 if side == "buy" else 1.02)
        
        signal_bar = df.iloc[i - 1]  # Signal Bar = 前一根 K 线
        entry_bar = df.iloc[i]       # Entry Bar = 当前 K 线
        
        signal_low = float(signal_bar["low"])
        signal_high = float(signal_bar["high"])
        entry_low = float(entry_bar["low"])
        entry_high = float(entry_bar["high"])
        
        # Al Brooks 修正：动态止损缓冲 = max(0.3 * ATR, 0.5% * entry_price)
        # - 高波动时使用 ATR 缓冲（0.3 * ATR）
        # - 低波动或无 ATR 时使用固定比例（0.5% * entry_price）
        atr_buffer = (atr * 0.3) if atr and atr > 0 else 0
        pct_buffer = entry_price * 0.005  # 0.5%
        buffer = max(atr_buffer, pct_buffer)
        
        if side == "buy":
            # 买入止损：min(SignalBar.Low, EntryBar.Low) - buffer
            stop_loss = min(signal_low, entry_low) - buffer
            stop_distance = entry_price - stop_loss
        else:
            # 卖出止损：max(SignalBar.High, EntryBar.High) + buffer
            stop_loss = max(signal_high, entry_high) + buffer
            stop_distance = stop_loss - entry_price
        
        # High Risk Filter: 止损距离超过 3 × ATR 则放弃信号
        if atr and atr > 0:
            max_stop_distance = atr * 3.0
            if stop_distance > max_stop_distance:
                logging.debug(
                    f"⚠️ High Risk Filter: 止损距离 {stop_distance:.2f} > 3×ATR ({max_stop_distance:.2f})，"
                    f"放弃信号 side={side}"
                )
                return None
        
        return stop_loss
    
    def calculate_measured_move(
        self, df: pd.DataFrame, i: int, side: str, 
        market_state: MarketState, atr: Optional[float] = None
    ) -> float:
        """
        计算 Measured Move（测量涨幅）
        
        - 区间突破：base_height = 区间宽度
        - 强趋势：base_height = 前一个波动的长度
        - 默认：2 * ATR
        """
        if i < self.lookback_period:
            return (atr * 2) if atr and atr > 0 else 0
        
        lookback_data = df.iloc[max(0, i - self.lookback_period) : i + 1]
        
        try:
            if market_state == MarketState.TRADING_RANGE:
                range_high = lookback_data["high"].max()
                range_low = lookback_data["low"].min()
                base_height = range_high - range_low
                
                if atr and atr > 0:
                    if base_height < atr * 0.5 or base_height > atr * 5:
                        return atr * 2
                
                return base_height
            
            elif market_state in [MarketState.BREAKOUT, MarketState.CHANNEL]:
                lows = lookback_data["low"].values
                highs = lookback_data["high"].values
                
                if side == "buy":
                    recent_low_idx = None
                    for j in range(len(lows) - 2, 0, -1):
                        if lows[j] < lows[j-1] and lows[j] < lows[j+1]:
                            recent_low_idx = j
                            break
                    
                    if recent_low_idx is not None:
                        base_height = highs[recent_low_idx:].max() - lows[recent_low_idx]
                    else:
                        base_height = lookback_data["high"].max() - lookback_data["low"].min()
                else:
                    recent_high_idx = None
                    for j in range(len(highs) - 2, 0, -1):
                        if highs[j] > highs[j-1] and highs[j] > highs[j+1]:
                            recent_high_idx = j
                            break
                    
                    if recent_high_idx is not None:
                        base_height = highs[recent_high_idx] - lows[recent_high_idx:].min()
                    else:
                        base_height = lookback_data["high"].max() - lookback_data["low"].min()
                
                if atr and atr > 0:
                    if base_height < atr * 0.5 or base_height > atr * 8:
                        return atr * 2
                
                return base_height
        
        except Exception:
            pass
        
        return (atr * 2) if atr and atr > 0 else 0
    
    @staticmethod
    def _spike_stop_at_signal_bar_extreme(
        signal_bar_high: float, signal_bar_low: float, side: str, buffer_pct: float = 0.001
    ) -> float:
        """
        动态止损：设在 Signal Bar 极值外 buffer_pct 位置
        
        Al Brooks: 止损在 Signal Bar 极值外，避免被噪音扫损
        
        Args:
            signal_bar_high, signal_bar_low: Signal Bar 高低点
            side: "buy" / "sell"
            buffer_pct: 极值外缓冲比例（默认 0.1%）
        
        Returns:
            止损价
        """
        if side == "buy":
            return signal_bar_low * (1.0 - buffer_pct)
        else:
            return signal_bar_high * (1.0 + buffer_pct)
    
    def detect_strong_spike(
        self, df: pd.DataFrame, i: int, ema: float, atr: Optional[float] = None,
        market_state: Optional[MarketState] = None
    ) -> Optional[Tuple[str, str, float, Optional[float], float, str, bool]]:
        """
        检测 Strong Spike（强突破入场）- Al Brooks Spike & Channel 对齐版（v2.0 累积突破）
        
        Al Brooks 修正：Spike 更注重连续性和跟随情况，单根 K 线的实体占比不是唯一标准
        BTC 高波动性下，65% 实体占比的强趋势棒也应被识别
        
        增强突破定义（两种模式）：
        
        模式 A - 单棒突破（原逻辑）：
        1. Signal Bar（前一根 i-1）实体占比 > 65%，且必须突破过去 10 根 K 线的极值
        2. Entry Bar（当前 Bar i）续延性验证：同向强 K 线，实体 > 50%
        
        模式 B - 累积突破（新增）：
        1. 连续 3 根 K 线均为同向趋势棒（阳线或阴线）
        2. 累计涨/跌幅 > 1.5 * ATR
        3. 即便单根棒实体没到 50%，也判定为有效 Spike
        
        入场模式：
        - EMA 偏离度 <= 1.5*ATR → Market_Entry（市价入场）
        - EMA 偏离度 > 1.5*ATR → Limit_Entry（限价入场，入场价 = Signal Bar 实体 50% 处）
        
        动态止损：止损设在 Signal Bar 极值外 0.1%。若止损距离 > 2.5*ATR 标记为高风险（仓位 50%）
        
        返回: (signal_type, side, stop_loss, limit_price, base_height, entry_mode, is_high_risk) 或 None
        """
        # 需要至少 12 根历史（Signal Bar=i-1，过去10根=i-11..i-2）
        if i < 12:
            return None
        
        if market_state not in [MarketState.BREAKOUT, MarketState.CHANNEL, MarketState.STRONG_TREND]:
            return None
        
        if "body_size" not in df.columns or "kline_range" not in df.columns:
            return None
        
        signal_bar = df.iloc[i - 1]
        entry_bar = df.iloc[i]
        
        s_high = float(signal_bar["high"])
        s_low = float(signal_bar["low"])
        s_open = float(signal_bar["open"])
        s_close = float(signal_bar["close"])
        s_body = float(signal_bar["body_size"])
        s_range = float(signal_bar["kline_range"]) if signal_bar["kline_range"] > 0 else (s_high - s_low)
        
        e_close = float(entry_bar["close"])
        e_open = float(entry_bar["open"])
        e_high = float(entry_bar["high"])
        e_low = float(entry_bar["low"])
        e_body = float(entry_bar["body_size"])
        e_range = float(entry_bar["kline_range"]) if entry_bar["kline_range"] > 0 else (e_high - e_low)
        
        # 过去 10 根 K 线（不含 Signal Bar）= i-11 到 i-2
        lookback = df.iloc[i - 11 : i - 1]
        max_10_high = lookback["high"].max()
        min_10_low = lookback["low"].min()
        
        # ATR 过滤：Entry Bar 范围过大视为 Climax 不追
        if atr is not None and atr > 0 and e_range > atr * self._params.atr_spike_filter_mult:
            return None
        
        # ========== 模式 B: 累积突破检测（优先检测）==========
        cumulative_result = self._detect_cumulative_spike(df, i, ema, atr, market_state)
        if cumulative_result is not None:
            return cumulative_result
        
        # ========== 模式 A: 单棒突破（原逻辑）==========
        # ---------- 向上突破 ----------
        if s_close > s_open and e_close > e_open:
            # Signal Bar: 实体占比 > 65%（Al Brooks 修正：从 70% 降低），且突破过去 10 根最高点
            if s_range <= 0:
                return None
            signal_body_ratio = s_body / s_range
            if signal_body_ratio <= 0.65:  # Al Brooks 修正：从 0.70 降至 0.65
                return None
            if s_high <= max_10_high:
                return None
            
            # Entry Bar: 同向强 K 线，实体 > 50%
            if e_range <= 0:
                return None
            entry_body_ratio = e_body / e_range
            if entry_body_ratio <= 0.50:
                return None
            
            # 价格需在 EMA 上方（顺势）
            if e_close <= ema:
                return None
            
            # 动态止损：Signal Bar 极值外 0.1%
            stop_loss = self._spike_stop_at_signal_bar_extreme(s_high, s_low, "buy", buffer_pct=0.001)
            entry_price = e_close
            risk_distance = entry_price - stop_loss
            is_high_risk = atr is not None and atr > 0 and risk_distance > 2.5 * atr
            
            base_height = self.calculate_measured_move(df, i, "buy", market_state, atr)
            
            # 入场模式：EMA 偏离度
            ema_deviation = abs(entry_price - ema) if ema > 0 else 0.0
            if atr is not None and atr > 0 and ema_deviation > 1.5 * atr:
                entry_mode = "Limit_Entry"
                limit_price = (s_open + s_close) / 2.0  # Signal Bar 实体 50% 处
            else:
                entry_mode = "Market_Entry"
                limit_price = None
            
            return (
                "Spike_Buy", "buy", stop_loss, limit_price, base_height,
                entry_mode, is_high_risk
            )
        
        # ---------- 向下突破 ----------
        if s_close < s_open and e_close < e_open:
            if s_range <= 0:
                return None
            signal_body_ratio = s_body / s_range
            if signal_body_ratio <= 0.65:  # Al Brooks 修正：从 0.70 降至 0.65
                return None
            if s_low >= min_10_low:
                return None
            
            if e_range <= 0:
                return None
            entry_body_ratio = e_body / e_range
            if entry_body_ratio <= 0.50:
                return None
            
            if e_close >= ema:
                return None
            
            stop_loss = self._spike_stop_at_signal_bar_extreme(s_high, s_low, "sell", buffer_pct=0.001)
            entry_price = e_close
            risk_distance = stop_loss - entry_price
            is_high_risk = atr is not None and atr > 0 and risk_distance > 2.5 * atr
            
            base_height = self.calculate_measured_move(df, i, "sell", market_state, atr)
            
            ema_deviation = abs(ema - entry_price) if ema > 0 else 0.0
            if atr is not None and atr > 0 and ema_deviation > 1.5 * atr:
                entry_mode = "Limit_Entry"
                limit_price = (s_open + s_close) / 2.0
            else:
                entry_mode = "Market_Entry"
                limit_price = None
            
            return (
                "Spike_Sell", "sell", stop_loss, limit_price, base_height,
                entry_mode, is_high_risk
            )
        
        return None
    
    def _detect_cumulative_spike(
        self, df: pd.DataFrame, i: int, ema: float, atr: Optional[float],
        market_state: Optional[MarketState]
    ) -> Optional[Tuple[str, str, float, Optional[float], float, str, bool]]:
        """
        检测累积突破（Cumulative Spike）- Al Brooks 多棒组合突破
        
        Al Brooks: "突破不必是单根大阳线，连续的同向趋势棒也是有效突破"
        
        条件：
        1. 连续 3 根 K 线均为同向趋势棒（阳线或阴线）
        2. 累计涨/跌幅 > 1.5 * ATR
        3. 即便单根棒实体没到 50%，也判定为有效 Spike
        
        Returns:
            (signal_type, side, stop_loss, limit_price, base_height, entry_mode, is_high_risk) 或 None
        """
        if i < 4 or atr is None or atr <= 0:
            return None
        
        # 累积突破需要的参数
        CUMULATIVE_BARS = 3  # 连续 3 根同向 K 线
        CUMULATIVE_ATR_MULT = 1.5  # 累计涨跌幅 > 1.5 * ATR
        
        # 检查最近 3 根 K 线（i-2, i-1, i）
        bars = [df.iloc[i - j] for j in range(CUMULATIVE_BARS - 1, -1, -1)]
        
        # ---------- 检测向上累积突破 ----------
        all_bullish = all(float(b["close"]) > float(b["open"]) for b in bars)
        if all_bullish:
            # 计算累计涨幅：从第一根开盘到最后一根收盘
            first_open = float(bars[0]["open"])
            last_close = float(bars[-1]["close"])
            cumulative_move = last_close - first_open
            
            # 检查累计涨幅是否 > 1.5 * ATR
            if cumulative_move > atr * CUMULATIVE_ATR_MULT:
                # 价格需在 EMA 上方
                if last_close <= ema:
                    return None
                
                # 计算 3 根 K 线的最低点作为止损参考
                combined_low = min(float(b["low"]) for b in bars)
                stop_loss = combined_low * (1.0 - 0.001)  # 低点外 0.1%
                
                entry_price = last_close
                risk_distance = entry_price - stop_loss
                is_high_risk = risk_distance > 2.5 * atr
                
                base_height = self.calculate_measured_move(df, i, "buy", market_state, atr)
                
                # 入场模式
                ema_deviation = abs(entry_price - ema) if ema > 0 else 0.0
                if ema_deviation > 1.5 * atr:
                    entry_mode = "Limit_Entry"
                    # 限价入场设在第二根 K 线的中点
                    limit_price = (float(bars[1]["open"]) + float(bars[1]["close"])) / 2.0
                else:
                    entry_mode = "Market_Entry"
                    limit_price = None
                
                logging.debug(
                    f"✅ 累积突破(买入): {CUMULATIVE_BARS}根连续阳线, "
                    f"累计涨幅={cumulative_move:.2f} > {atr * CUMULATIVE_ATR_MULT:.2f}"
                )
                return (
                    "Spike_Buy", "buy", stop_loss, limit_price, base_height,
                    entry_mode, is_high_risk
                )
        
        # ---------- 检测向下累积突破 ----------
        all_bearish = all(float(b["close"]) < float(b["open"]) for b in bars)
        if all_bearish:
            # 计算累计跌幅：从第一根开盘到最后一根收盘
            first_open = float(bars[0]["open"])
            last_close = float(bars[-1]["close"])
            cumulative_move = first_open - last_close  # 跌幅为正数
            
            # 检查累计跌幅是否 > 1.5 * ATR
            if cumulative_move > atr * CUMULATIVE_ATR_MULT:
                # 价格需在 EMA 下方
                if last_close >= ema:
                    return None
                
                # 计算 3 根 K 线的最高点作为止损参考
                combined_high = max(float(b["high"]) for b in bars)
                stop_loss = combined_high * (1.0 + 0.001)  # 高点外 0.1%
                
                entry_price = last_close
                risk_distance = stop_loss - entry_price
                is_high_risk = risk_distance > 2.5 * atr
                
                base_height = self.calculate_measured_move(df, i, "sell", market_state, atr)
                
                # 入场模式
                ema_deviation = abs(ema - entry_price) if ema > 0 else 0.0
                if ema_deviation > 1.5 * atr:
                    entry_mode = "Limit_Entry"
                    limit_price = (float(bars[1]["open"]) + float(bars[1]["close"])) / 2.0
                else:
                    entry_mode = "Market_Entry"
                    limit_price = None
                
                logging.debug(
                    f"✅ 累积突破(卖出): {CUMULATIVE_BARS}根连续阴线, "
                    f"累计跌幅={cumulative_move:.2f} > {atr * CUMULATIVE_ATR_MULT:.2f}"
                )
                return (
                    "Spike_Sell", "sell", stop_loss, limit_price, base_height,
                    entry_mode, is_high_risk
                )
        
        return None
    
    def detect_ma_gap_bar(
        self, df: pd.DataFrame, i: int, ema: float, atr: Optional[float] = None,
        market_state: Optional[MarketState] = None
    ) -> Optional[Tuple[str, str, float, Optional[float], float, str]]:
        """
        检测 Moving Average Gap Bar（MA 缺口棒）- 加密货币 24 小时市场专用
        
        Al Brooks 修正版：在加密市场中，Gap 的定义是 "Moving Average Gap"
        
        定义：
        - 上涨 MA Gap：连续 3 根 K 线的 Low 始终高于 20 EMA
        - 下跌 MA Gap：连续 3 根 K 线的 High 始终低于 20 EMA
        
        当检测到 MA Gap 时：
        1. 解除 "必须触碰 EMA" 的回调限制
        2. 只要当前棒是顺势趋势棒（Trend Bar），且突破前一根棒的极值
        3. 允许直接入场（限价单，订单簿最优价）
        
        返回: (signal_type, side, stop_loss, limit_price, base_height, entry_mode) 或 None
        """
        # 需要至少 5 根历史（3 根 Gap + 当前棒 + 前一棒）
        if i < 5:
            return None
        
        # 只在强趋势/通道状态下触发
        if market_state not in [MarketState.STRONG_TREND, MarketState.TIGHT_CHANNEL, 
                                MarketState.CHANNEL, MarketState.BREAKOUT]:
            return None
        
        if "body_size" not in df.columns or "kline_range" not in df.columns:
            return None
        
        # 获取当前棒和前一棒
        current_bar = df.iloc[i]
        prev_bar = df.iloc[i - 1]
        
        curr_close = float(current_bar["close"])
        curr_open = float(current_bar["open"])
        curr_high = float(current_bar["high"])
        curr_low = float(current_bar["low"])
        curr_body = float(current_bar["body_size"])
        curr_range = float(current_bar["kline_range"]) if current_bar["kline_range"] > 0 else (curr_high - curr_low)
        
        prev_high = float(prev_bar["high"])
        prev_low = float(prev_bar["low"])
        
        # ========== 检测 MA Gap（连续 3 根 K 线与 EMA 的关系）==========
        MA_GAP_BARS = 3
        
        # 检查过去 3 根 K 线（i-3, i-2, i-1）
        gap_bars = [df.iloc[i - j] for j in range(MA_GAP_BARS, 0, -1)]
        
        # 上涨 MA Gap：所有 3 根 K 线的 Low > EMA
        all_low_above_ema = True
        for bar in gap_bars:
            bar_low = float(bar["low"])
            bar_ema = float(bar["ema"]) if "ema" in bar else ema
            if bar_low <= bar_ema:
                all_low_above_ema = False
                break
        
        # 下跌 MA Gap：所有 3 根 K 线的 High < EMA
        all_high_below_ema = True
        for bar in gap_bars:
            bar_high = float(bar["high"])
            bar_ema = float(bar["ema"]) if "ema" in bar else ema
            if bar_high >= bar_ema:
                all_high_below_ema = False
                break
        
        # 如果没有检测到 MA Gap，返回 None
        if not all_low_above_ema and not all_high_below_ema:
            return None
        
        # ========== 检测当前棒是否为顺势趋势棒 ==========
        # 趋势棒定义：实体占比 > 50%，收盘方向与 Gap 方向一致
        if curr_range <= 0:
            return None
        
        body_ratio = curr_body / curr_range
        MIN_BODY_RATIO = 0.50  # 趋势棒最低实体占比
        
        if body_ratio < MIN_BODY_RATIO:
            return None
        
        # ========== 上涨 MA Gap Bar ==========
        if all_low_above_ema:
            # 当前棒必须是阳线
            if curr_close <= curr_open:
                return None
            
            # 当前棒必须突破前一棒最高点
            if curr_high <= prev_high:
                return None
            
            # 当前棒 Low 也必须高于 EMA（保持 Gap 状态）
            if curr_low <= ema:
                return None
            
            # 止损：前一棒低点外 0.1%（Gap 状态下止损较紧）
            stop_loss = prev_low * (1.0 - 0.001)
            
            # 入场模式：限价单（订单簿最优价）
            # 使用前一棒高点作为限价入场点（突破后回撤入场）
            entry_mode = "Limit_Entry"
            limit_price = prev_high
            
            # 计算目标
            base_height = self.calculate_measured_move(df, i, "buy", market_state, atr)
            
            logging.debug(
                f"✅ MA Gap Bar (买入): {MA_GAP_BARS}根K线Low>EMA, "
                f"当前棒突破前高 {prev_high:.2f}, 实体比={body_ratio:.0%}"
            )
            
            return (
                "GapBar_Buy", "buy", stop_loss, limit_price, base_height,
                entry_mode
            )
        
        # ========== 下跌 MA Gap Bar ==========
        if all_high_below_ema:
            # 当前棒必须是阴线
            if curr_close >= curr_open:
                return None
            
            # 当前棒必须突破前一棒最低点
            if curr_low >= prev_low:
                return None
            
            # 当前棒 High 也必须低于 EMA（保持 Gap 状态）
            if curr_high >= ema:
                return None
            
            # 止损：前一棒高点外 0.1%
            stop_loss = prev_high * (1.0 + 0.001)
            
            # 入场模式：限价单
            entry_mode = "Limit_Entry"
            limit_price = prev_low
            
            # 计算目标
            base_height = self.calculate_measured_move(df, i, "sell", market_state, atr)
            
            logging.debug(
                f"✅ MA Gap Bar (卖出): {MA_GAP_BARS}根K线High<EMA, "
                f"当前棒突破前低 {prev_low:.2f}, 实体比={body_ratio:.0%}"
            )
            
            return (
                "GapBar_Sell", "sell", stop_loss, limit_price, base_height,
                entry_mode
            )
        
        return None
    
    def detect_climax_reversal(
        self, df: pd.DataFrame, i: int, ema: float, atr: Optional[float] = None
    ) -> Optional[Tuple[str, str, float, float]]:
        """
        检测 Climax 反转信号（Al Brooks 修正版）
        
        当检测到 Climax（Spike 长度超过 2.5 倍 ATR）后，寻找反转信号
        
        Al Brooks 修正：
        1. 尾部影线检查 - 真正的 Climax 有明显的"拒绝影线"
        2. 前期走势深度检查 - 扩展到 5-8 根 K 线（从 3 根扩展）
        3. 趋势持续性检查 - 至少 5 根 K 线都在 EMA 同一侧
        
        "Climax 通常出现在趋势的极端位置" - Al Brooks
        
        返回: (signal_type, side, stop_loss, base_height) 或 None
        """
        # Al Brooks 修正：需要至少 8 根 K 线来检查前期走势（从 3 根扩展）
        CLIMAX_LOOKBACK = 8
        MIN_LOOKBACK = 5  # 最少需要 5 根
        
        if i < CLIMAX_LOOKBACK or atr is None or atr <= 0:
            return None
        
        current_bar = df.iloc[i]
        prev_bar = df.iloc[i - 1]
        
        close = current_bar["close"]
        open_price = current_bar["open"]
        high = current_bar["high"]
        low = current_bar["low"]
        prev_close = prev_bar["close"]
        prev_high = prev_bar["high"]
        prev_low = prev_bar["low"]
        prev_open = prev_bar["open"]
        prev_range = prev_high - prev_low
        
        # Climax 阈值（周期自适应）
        CLIMAX_ATR_MULTIPLIER = self._params.atr_climax_mult
        
        # 当前 K 线范围（用于尾部影线计算）
        current_range = high - low
        if current_range == 0:
            return None
        
        # 向上 Climax -> Climax_Sell（做空反转）
        if prev_range > atr * CLIMAX_ATR_MULTIPLIER and prev_close > prev_open:
            if close < open_price and close < prev_close:
                if not self.validate_signal_close(current_bar, "sell"):
                    return None
                
                # ⭐ 尾部影线检查（上影线 = 拒绝更高价格）
                upper_tail = high - max(open_price, close)
                tail_ratio = upper_tail / current_range
                if tail_ratio < 0.15:  # 上影线至少占 K 线的 15%
                    logging.debug(f"Climax_Sell 被跳过: 上影线不足 ({tail_ratio:.1%} < 15%)")
                    return None
                
                # ⭐ Al Brooks 修正：前期走势深度检查（扩展到 5-8 根 K 线）
                # 检查前 5-8 根 K 线的整体涨幅
                lookback_data = df.iloc[i - CLIMAX_LOOKBACK : i]
                lookback_low = lookback_data["low"].min()
                prior_move = prev_high - lookback_low  # 从回看期的低点到 Climax 高点
                if prior_move < atr * 2.0:  # 提高阈值：从 1.5 ATR 提高到 2.0 ATR
                    logging.debug(f"Climax_Sell 被跳过: 前期涨幅不足 ({prior_move:.2f} < {atr * 2.0:.2f})")
                    return None
                
                # ⭐ Al Brooks 修正：趋势持续性检查
                # 至少 5 根 K 线都在 EMA 上方（确保是真正的超买）
                bars_above_ema = sum(1 for j in range(i - MIN_LOOKBACK, i) if df.iloc[j]["close"] > ema)
                if bars_above_ema < MIN_LOOKBACK:
                    logging.debug(f"Climax_Sell 被跳过: 趋势持续性不足 (仅 {bars_above_ema}/{MIN_LOOKBACK} 根在 EMA 上方)")
                    return None
                
                stop_loss = self.calculate_unified_stop_loss(df, i, "sell", close, atr)
                if stop_loss is None:
                    logging.debug(f"Climax_Sell 被跳过: High Risk Filter 止损距离过大")
                    return None
                logging.debug(f"✅ Climax_Sell 触发: 上影线={tail_ratio:.1%}, 前期涨幅={prior_move:.2f}, 趋势持续={bars_above_ema}根")
                return ("Climax_Sell", "sell", stop_loss, prev_range)
        
        # 向下 Climax -> Climax_Buy（做多反转）
        if prev_range > atr * CLIMAX_ATR_MULTIPLIER and prev_close < prev_open:
            if close > open_price and close > prev_close:
                if not self.validate_signal_close(current_bar, "buy"):
                    return None
                
                # ⭐ 尾部影线检查（下影线 = 拒绝更低价格）
                lower_tail = min(open_price, close) - low
                tail_ratio = lower_tail / current_range
                if tail_ratio < 0.15:  # 下影线至少占 K 线的 15%
                    logging.debug(f"Climax_Buy 被跳过: 下影线不足 ({tail_ratio:.1%} < 15%)")
                    return None
                
                # ⭐ Al Brooks 修正：前期走势深度检查（扩展到 5-8 根 K 线）
                # 检查前 5-8 根 K 线的整体跌幅
                lookback_data = df.iloc[i - CLIMAX_LOOKBACK : i]
                lookback_high = lookback_data["high"].max()
                prior_move = lookback_high - prev_low  # 从回看期的高点到 Climax 低点
                if prior_move < atr * 2.0:  # 提高阈值：从 1.5 ATR 提高到 2.0 ATR
                    logging.debug(f"Climax_Buy 被跳过: 前期跌幅不足 ({prior_move:.2f} < {atr * 2.0:.2f})")
                    return None
                
                # ⭐ Al Brooks 修正：趋势持续性检查
                # 至少 5 根 K 线都在 EMA 下方（确保是真正的超卖）
                bars_below_ema = sum(1 for j in range(i - MIN_LOOKBACK, i) if df.iloc[j]["close"] < ema)
                if bars_below_ema < MIN_LOOKBACK:
                    logging.debug(f"Climax_Buy 被跳过: 趋势持续性不足 (仅 {bars_below_ema}/{MIN_LOOKBACK} 根在 EMA 下方)")
                    return None
                
                stop_loss = self.calculate_unified_stop_loss(df, i, "buy", close, atr)
                if stop_loss is None:
                    logging.debug(f"Climax_Buy 被跳过: High Risk Filter 止损距离过大")
                    return None
                logging.debug(f"✅ Climax_Buy 触发: 下影线={tail_ratio:.1%}, 前期跌幅={prior_move:.2f}, 趋势持续={bars_below_ema}根")
                return ("Climax_Buy", "buy", stop_loss, prev_range)
        
        return None
    
    # ========== 三推楔形：递归波动峰/谷识别（已提取到 wedge_reversal.py）==========
    
    @staticmethod
    def _find_swing_peaks(
        df: pd.DataFrame,
        start: int,
        end: int,
        min_left: int = 2,
        min_right: int = 2,
    ) -> List[Tuple[int, float]]:
        """调用入口 - 实际逻辑已提取到 wedge_reversal.py"""
        return find_swing_peaks(df, start, end, min_left, min_right)
    
    @staticmethod
    def _find_swing_troughs(
        df: pd.DataFrame,
        start: int,
        end: int,
        min_left: int = 2,
        min_right: int = 2,
    ) -> List[Tuple[int, float]]:
        """调用入口 - 实际逻辑已提取到 wedge_reversal.py"""
        return find_swing_troughs(df, start, end, min_left, min_right)
    
    @staticmethod
    def _find_three_lower_highs(
        peaks: List[Tuple[int, float]],
        min_span: int = 3,
        require_convergence: bool = True,
        require_momentum_decay: bool = True,
    ) -> Optional[Tuple[List[int], List[float]]]:
        """
        调用入口 - 实际逻辑已提取到 wedge_reversal.py
        
        Al Brooks 修正：默认启用收敛检测和动能递减检测
        """
        return find_three_lower_highs(peaks, min_span, require_convergence, require_momentum_decay)
    
    @staticmethod
    def _find_three_higher_lows(
        troughs: List[Tuple[int, float]],
        min_span: int = 3,
        require_convergence: bool = True,
        require_momentum_decay: bool = True,
    ) -> Optional[Tuple[List[int], List[float]]]:
        """
        调用入口 - 实际逻辑已提取到 wedge_reversal.py
        
        Al Brooks 修正：默认启用收敛检测和动能递减检测
        """
        return find_three_higher_lows(troughs, min_span, require_convergence, require_momentum_decay)
    
    def detect_failed_breakout(
        self, df: pd.DataFrame, i: int, ema: float, atr: Optional[float] = None,
        market_state: Optional[MarketState] = None,
        relaxed_signal_bar: bool = False,
    ) -> Optional[Tuple[str, str, float, float]]:
        """
        检测 Failed Breakout（失败突破反转）
        
        relaxed_signal_bar: 交易区间 BLSH 时 True，收盘位置门槛从 60% 降到 50%
        
        返回: (signal_type, side, stop_loss, base_height) 或 None
        """
        # 优化：使用更短期的回看周期（10根）
        SHORT_LOOKBACK = 10
        
        if i < SHORT_LOOKBACK + 1:
            return None
        
        if market_state != MarketState.TRADING_RANGE:
            return None
        
        # 一次性提取当前行数据（减少多次 iloc 访问）
        current_bar = df.iloc[i]
        close = current_bar["close"]
        open_price = current_bar["open"]
        high = current_bar["high"]
        low = current_bar["low"]
        current_high = high
        current_low = low
        
        # 向量化获取近期高低点
        lookback_slice = df.iloc[max(0, i - SHORT_LOOKBACK):i]
        max_lookback_high = lookback_slice["high"].max() if len(lookback_slice) > 0 else current_high
        min_lookback_low = lookback_slice["low"].min() if len(lookback_slice) > 0 else current_low
        
        # 用更长周期计算区间宽度（用于止盈）
        lookback_range = df.iloc[max(0, i - self.lookback_period) : i + 1]
        range_width = lookback_range["high"].max() - lookback_range["low"].min()
        
        # 使用预计算的 kline_range 列
        kline_range = current_bar["kline_range"] if "kline_range" in df.columns else (high - low)
        if kline_range == 0:
            return None
        
        # ⭐ 新增：检查最近3根K线是否已经在持续创新高/新低
        recent_3_bars = df.iloc[max(0, i - 2) : i]  # 前2根K线
        
        # 创新高后反转
        if current_high > max_lookback_high:
            # ⭐ 防误判：检查前2根是否已经在创新高（向量化）
            prior_highs_above = int((recent_3_bars["high"] > max_lookback_high * 0.999).sum())
            if prior_highs_above >= 2:
                # 之前2根K线都在高位，这是趋势延续不是假突破
                logging.debug(f"FailedBreakout_Sell 被跳过: 前{prior_highs_above}根K线已在新高，是趋势延续")
                return None
            
            # ⭐ 防误判：检查前1根K线收盘是否也在高位（说明上涨趋势未结束）
            prev_bar = df.iloc[i - 1]
            prev_close_in_upper = (prev_bar["close"] - prev_bar["low"]) / (prev_bar["high"] - prev_bar["low"]) > 0.7 if (prev_bar["high"] - prev_bar["low"]) > 0 else False
            if prev_close_in_upper and prev_bar["close"] > prev_bar["open"]:
                # 前一根是收盘价在高位的阳线，趋势可能延续
                logging.debug(f"FailedBreakout_Sell 被跳过: 前一根阳线收盘在高位，趋势可能延续")
                return None
            
            # 条件：阴线 + 收盘价远离高点
            if close < open_price:
                close_position = (high - close) / kline_range
                threshold = 0.5 if relaxed_signal_bar else 0.6
                if close_position >= threshold:
                    stop_loss = self.calculate_unified_stop_loss(df, i, "sell", close, atr)
                    if stop_loss is None:
                        logging.debug(f"FailedBreakout_Sell 被跳过: High Risk Filter 止损距离过大")
                        return None
                    logging.debug(f"✅ FailedBreakout_Sell 触发: 创新高{current_high:.2f}后反转，收盘位置={close_position:.1%}")
                    return ("FailedBreakout_Sell", "sell", stop_loss, range_width)
        
        # 创新低后反转
        if current_low < min_lookback_low:
            # ⭐ 防误判：检查前2根是否已经在创新低
            prior_lows_below = sum(1 for j in recent_3_bars.index if recent_3_bars.at[j, "low"] < min_lookback_low * 1.001)
            if prior_lows_below >= 2:
                # 之前2根K线都在低位，这是趋势延续不是假突破
                logging.debug(f"FailedBreakout_Buy 被跳过: 前{prior_lows_below}根K线已在新低，是趋势延续")
                return None
            
            # ⭐ 防误判：检查前1根K线收盘是否也在低位（说明下跌趋势未结束）
            prev_bar = df.iloc[i - 1]
            prev_close_in_lower = (prev_bar["high"] - prev_bar["close"]) / (prev_bar["high"] - prev_bar["low"]) > 0.7 if (prev_bar["high"] - prev_bar["low"]) > 0 else False
            if prev_close_in_lower and prev_bar["close"] < prev_bar["open"]:
                # 前一根是收盘价在低位的阴线，趋势可能延续
                logging.debug(f"FailedBreakout_Buy 被跳过: 前一根阴线收盘在低位，趋势可能延续")
                return None
            
            # 条件：阳线 + 收盘价远离低点
            if close > open_price:
                close_position = (close - low) / kline_range
                threshold = 0.5 if relaxed_signal_bar else 0.6
                if close_position >= threshold:
                    stop_loss = self.calculate_unified_stop_loss(df, i, "buy", close, atr)
                    if stop_loss is None:
                        logging.debug(f"FailedBreakout_Buy 被跳过: High Risk Filter 止损距离过大")
                        return None
                    logging.debug(f"✅ FailedBreakout_Buy 触发: 创新低{current_low:.2f}后反转，收盘位置={close_position:.1%}")
                    return ("FailedBreakout_Buy", "buy", stop_loss, range_width)
        
        return None
    
    def detect_wedge_failed_breakout(
        self,
        df: pd.DataFrame,
        i: int,
        ema: float,
        atr: Optional[float] = None,
        market_state: Optional[MarketState] = None,
    ) -> Optional[Tuple[str, str, float, float]]:
        """
        楔形 + 失败突破：三推结构后对楔形极值的假突破，反向切入。
        
        Al Brooks：三推楔形是极高胜率反转/中继信号；配合失败突破（突破极值后收盘拉回）
        做反向入场。
        
        - 三推高点递降（P1>P2>P3）后：若价格突破楔形高点后收盘拉回 → 失败突破楔顶 → 卖出
        - 三推低点递升（T1<T2<T3）后：若价格跌破楔形低点后收盘拉回 → 失败突破楔底 → 买入
        
        返回: (signal_type, side, stop_loss, range_width) 或 None
        """
        if market_state != MarketState.TRADING_RANGE:
            return None
        if i < 20:
            return None
        
        lookback_start = max(0, i - 30)
        leg_span = max(3, self._params.wedge_min_leg_span)
        current_bar = df.iloc[i]
        high_i = float(current_bar["high"])
        low_i = float(current_bar["low"])
        close_i = float(current_bar["close"])
        kline_range = high_i - low_i
        if kline_range <= 0:
            return None
        
        # 三推高点递降：楔顶失败突破（突破楔顶后收盘拉回 → 卖）
        # Al Brooks 修正：启用收敛检测和动能递减检测
        peaks_rec = self._find_swing_peaks(df, lookback_start, i + 1, min_left=2, min_right=2)
        three_lower = self._find_three_lower_highs(
            peaks_rec, min_span=leg_span, 
            require_convergence=True, require_momentum_decay=True
        )
        if three_lower is not None:
            peak_indices, peak_values = three_lower
            wedge_high = max(peak_values)
            wedge_low = float(df.iloc[peak_indices[0] : peak_indices[2] + 1]["low"].min())
            if high_i > wedge_high * 1.001:  # 盘中突破楔顶
                close_back_below = close_i < wedge_high * 0.999
                close_in_lower = (high_i - close_i) / kline_range >= 0.5
                if close_back_below or close_in_lower:
                    if close_i < float(current_bar["open"]):
                        stop_loss = wedge_high + (0.5 * atr) if atr and atr > 0 else wedge_high * 1.001
                        range_width = wedge_high - wedge_low
                        logging.debug(f"✅ Wedge_FailedBreakout_Sell: 三推高点递降后突破楔顶{wedge_high:.2f}后收盘拉回")
                        return ("Wedge_FailedBreakout_Sell", "sell", stop_loss, range_width)
        
        # 三推低点递升：楔底失败突破（跌破楔底后收盘拉回 → 买）
        # Al Brooks 修正：启用收敛检测和动能递减检测
        troughs_rec = self._find_swing_troughs(df, lookback_start, i + 1, min_left=2, min_right=2)
        three_higher = self._find_three_higher_lows(
            troughs_rec, min_span=leg_span,
            require_convergence=True, require_momentum_decay=True
        )
        if three_higher is not None:
            trough_indices, trough_values = three_higher
            wedge_low = min(trough_values)
            wedge_high = float(df.iloc[trough_indices[0] : trough_indices[2] + 1]["high"].max())
            if low_i < wedge_low * 0.999:  # 盘中跌破楔底
                close_back_above = close_i > wedge_low * 1.001
                close_in_upper = (close_i - low_i) / kline_range >= 0.5
                if close_back_above or close_in_upper:
                    if close_i > float(current_bar["open"]):
                        stop_loss = wedge_low - (0.5 * atr) if atr and atr > 0 else wedge_low * 0.999
                        range_width = wedge_high - wedge_low
                        logging.debug(f"✅ Wedge_FailedBreakout_Buy: 三推低点递升后跌破楔底{wedge_low:.2f}后收盘拉回")
                        return ("Wedge_FailedBreakout_Buy", "buy", stop_loss, range_width)
        
        return None
    
    def detect_wedge_reversal(
        self,
        df: pd.DataFrame,
        i: int,
        ema: float,
        atr: Optional[float] = None,
        market_state: Optional[MarketState] = None,
        relaxed_signal_bar: bool = False,
    ) -> Optional[Tuple[str, str, float, float, float, float, bool]]:
        """
        检测 Wedge Reversal（楔形反转，三次推进）- Al Brooks 加固版
        
        调用入口 - 实际逻辑已提取到 wedge_reversal.py
        
        relaxed_signal_bar: 交易区间 BLSH 时 True，信号棒门槛降为 40% 实体、35% 收盘区域
        
        返回: (signal_type, side, stop_loss, base_height, wedge_tp1, wedge_tp2, is_strong_reversal_bar) 或 None
        """
        return detect_wedge_reversal_impl(
            df=df,
            i=i,
            ema=ema,
            atr=atr,
            market_state=market_state,
            relaxed_signal_bar=relaxed_signal_bar,
            params=self._params,
            btc_min_body_ratio=self.BTC_MIN_BODY_RATIO,
            btc_close_position_pct=self.BTC_CLOSE_POSITION_PCT,
            validate_signal_close_func=self.validate_signal_close,
        )
    
    # ========== MTR 辅助函数：纯价格行为趋势识别 ==========
    
    @staticmethod
    def _identify_significant_trend(
        df: pd.DataFrame,
        end_idx: int,
        lookback: int = 60,
        min_swing_points: int = 3,
    ) -> Optional[Tuple[str, List[Tuple[int, float]], List[Tuple[int, float]], float]]:
        """
        识别显著趋势（纯价格行为，不依赖 EMA）
        
        Al Brooks: 趋势由连续的 Higher High + Higher Low（上升）或 
        Lower High + Lower Low（下降）定义。
        
        Args:
            df: K线数据
            end_idx: 当前 K 线索引
            lookback: 回看周期（默认 60 根）
            min_swing_points: 最少需要的 swing 点数量
        
        Returns:
            (trend_direction, swing_highs, swing_lows, trend_strength) 或 None
            - trend_direction: "up" / "down"
            - swing_highs: [(idx, high), ...] 趋势中的主要高点
            - swing_lows: [(idx, low), ...] 趋势中的主要低点
            - trend_strength: 趋势强度（0-1，基于价格变动幅度）
        """
        start_idx = max(0, end_idx - lookback)
        if end_idx - start_idx < 20:
            return None
        
        # 识别 swing highs 和 swing lows
        swing_highs: List[Tuple[int, float]] = []
        swing_lows: List[Tuple[int, float]] = []
        
        for j in range(start_idx + 2, end_idx - 1):
            h = float(df.iloc[j]["high"])
            l = float(df.iloc[j]["low"])
            
            # Swing High: 左右两根的高点都更低
            left_h1 = float(df.iloc[j - 1]["high"])
            left_h2 = float(df.iloc[j - 2]["high"])
            right_h1 = float(df.iloc[j + 1]["high"])
            if h > left_h1 and h > left_h2 and h > right_h1:
                swing_highs.append((j, h))
            
            # Swing Low: 左右两根的低点都更高
            left_l1 = float(df.iloc[j - 1]["low"])
            left_l2 = float(df.iloc[j - 2]["low"])
            right_l1 = float(df.iloc[j + 1]["low"])
            if l < left_l1 and l < left_l2 and l < right_l1:
                swing_lows.append((j, l))
        
        if len(swing_highs) < min_swing_points or len(swing_lows) < min_swing_points:
            return None
        
        # 分析趋势方向
        # 上升趋势: 后续的 swing high 更高，swing low 也更高
        # 下降趋势: 后续的 swing high 更低，swing low 也更低
        
        recent_highs = swing_highs[-min_swing_points:]
        recent_lows = swing_lows[-min_swing_points:]
        
        # 检查上升趋势
        hh_count = 0  # Higher High 计数
        hl_count = 0  # Higher Low 计数
        for k in range(1, len(recent_highs)):
            if recent_highs[k][1] > recent_highs[k - 1][1]:
                hh_count += 1
        for k in range(1, len(recent_lows)):
            if recent_lows[k][1] > recent_lows[k - 1][1]:
                hl_count += 1
        
        # 检查下降趋势
        lh_count = 0  # Lower High 计数
        ll_count = 0  # Lower Low 计数
        for k in range(1, len(recent_highs)):
            if recent_highs[k][1] < recent_highs[k - 1][1]:
                lh_count += 1
        for k in range(1, len(recent_lows)):
            if recent_lows[k][1] < recent_lows[k - 1][1]:
                ll_count += 1
        
        # 计算趋势强度
        total_range = float(df.iloc[start_idx:end_idx + 1]["high"].max() - 
                           df.iloc[start_idx:end_idx + 1]["low"].min())
        if total_range == 0:
            return None
        
        # 判断趋势方向
        up_score = hh_count + hl_count
        down_score = lh_count + ll_count
        required_score = min_swing_points - 1  # 至少需要这么多连续的同向 swing
        
        if up_score >= required_score and up_score > down_score:
            # 上升趋势：从最早的 swing low 到最近的 swing high
            trend_move = recent_highs[-1][1] - recent_lows[0][1]
            trend_strength = min(1.0, abs(trend_move) / total_range)
            return ("up", swing_highs, swing_lows, trend_strength)
        
        elif down_score >= required_score and down_score > up_score:
            # 下降趋势：从最早的 swing high 到最近的 swing low
            trend_move = recent_highs[0][1] - recent_lows[-1][1]
            trend_strength = min(1.0, abs(trend_move) / total_range)
            return ("down", swing_highs, swing_lows, trend_strength)
        
        return None
    
    @staticmethod
    def _calculate_trendline(
        swing_points: List[Tuple[int, float]],
        direction: str,
    ) -> Optional[Tuple[float, float, int, int]]:
        """
        计算趋势线（连接主要 swing 点）
        
        Args:
            swing_points: [(idx, price), ...] swing 高点或低点
            direction: "up" 连接低点（支撑线），"down" 连接高点（压力线）
        
        Returns:
            (slope, intercept, start_idx, end_idx) 或 None
            趋势线方程: price = slope * idx + intercept
        """
        if len(swing_points) < 2:
            return None
        
        # 使用最近的两个主要 swing 点来画趋势线
        # 上升趋势：连接 swing lows（支撑线）
        # 下降趋势：连接 swing highs（压力线）
        
        # 取最近的 2-3 个点，选择形成最有效趋势线的两点
        recent_points = swing_points[-3:] if len(swing_points) >= 3 else swing_points[-2:]
        
        best_line = None
        best_touches = 0
        
        for i in range(len(recent_points)):
            for j in range(i + 1, len(recent_points)):
                p1_idx, p1_price = recent_points[i]
                p2_idx, p2_price = recent_points[j]
                
                if p2_idx == p1_idx:
                    continue
                
                slope = (p2_price - p1_price) / (p2_idx - p1_idx)
                intercept = p1_price - slope * p1_idx
                
                # 验证斜率方向与趋势一致
                if direction == "up" and slope <= 0:
                    continue
                if direction == "down" and slope >= 0:
                    continue
                
                # 计算触碰次数（其他点接近趋势线）
                touches = 2  # 两个定义点
                for k, (pt_idx, pt_price) in enumerate(swing_points):
                    if k == i or k == j:
                        continue
                    expected = slope * pt_idx + intercept
                    tolerance = abs(expected) * 0.005  # 0.5% 容差
                    if abs(pt_price - expected) <= tolerance:
                        touches += 1
                
                if touches > best_touches:
                    best_touches = touches
                    best_line = (slope, intercept, p1_idx, p2_idx)
        
        return best_line
    
    @staticmethod
    def _is_trendline_break(
        df: pd.DataFrame,
        bar_idx: int,
        trendline: Tuple[float, float, int, int],
        direction: str,
        atr: Optional[float] = None,
    ) -> Tuple[bool, float]:
        """
        检测趋势线突破（Al Brooks 修正版）
        
        Al Brooks: "趋势线突破需要有意义的跟随"
        仅收盘价穿越趋势线不足以确认突破
        
        Al Brooks 修正：提高突破阈值
        - 至少突破趋势线 0.8%（从 0.5% 提高）
        - 或 1.0×ATR（从 0.8×ATR 提高）
        - 增加突破棒收盘位置检查
        
        Args:
            df: K线数据
            bar_idx: 当前 K 线索引
            trendline: (slope, intercept, start_idx, end_idx)
            direction: 原趋势方向 "up" / "down"
            atr: ATR 值（用于计算突破幅度）
        
        Returns:
            (is_break, break_magnitude): 是否突破及突破幅度
        """
        slope, intercept, _, _ = trendline
        bar = df.iloc[bar_idx]
        bar_close = float(bar["close"])
        bar_low = float(bar["low"])
        bar_high = float(bar["high"])
        bar_open = float(bar["open"])
        
        # 计算趋势线在当前位置的预期价格
        trendline_price = slope * bar_idx + intercept
        
        # Al Brooks 修正：检查突破棒收盘位置
        # 突破棒收盘应在极端位置（>75%）才是有效突破
        kline_range = bar_high - bar_low
        if kline_range > 0:
            if direction == "up":
                # 跌破支撑线：阴线，收盘在下方 75% 区域
                close_position = (bar_high - bar_close) / kline_range
                if close_position < 0.75 and bar_close >= bar_open:  # 非强势阴线
                    return (False, 0.0)
            else:
                # 突破压力线：阳线，收盘在上方 75% 区域
                close_position = (bar_close - bar_low) / kline_range
                if close_position < 0.75 and bar_close <= bar_open:  # 非强势阳线
                    return (False, 0.0)
        
        if direction == "up":
            # 上升趋势，检测跌破支撑线
            # 收盘价必须在趋势线下方
            if bar_close >= trendline_price:
                return (False, 0.0)
            break_magnitude = (trendline_price - bar_close) / trendline_price
            # Al Brooks 修正：趋势线突破需要更显著的幅度
            # 至少突破趋势线 0.8%（从 0.5% 提高），或 1.0×ATR（从 0.8×ATR 提高）
            min_break = 0.008  # 从 0.005 提高到 0.008
            if atr and atr > 0:
                min_break = max(min_break, 1.0 * atr / trendline_price)  # 从 0.8 提高到 1.0
            if break_magnitude >= min_break:
                return (True, break_magnitude)
        
        elif direction == "down":
            # 下降趋势，检测突破压力线
            if bar_close <= trendline_price:
                return (False, 0.0)
            break_magnitude = (bar_close - trendline_price) / trendline_price
            # Al Brooks 修正：趋势线突破需要更显著的幅度
            min_break = 0.008  # 从 0.005 提高到 0.008
            if atr and atr > 0:
                min_break = max(min_break, 1.0 * atr / trendline_price)  # 从 0.8 提高到 1.0
            if break_magnitude >= min_break:
                return (True, break_magnitude)
        
        return (False, 0.0)
    
    @staticmethod
    def _is_strong_breakout_bar(
        df: pd.DataFrame,
        bar_idx: int,
        direction: str,
        min_body_ratio: float = 0.55,
    ) -> Tuple[bool, str]:
        """
        验证突破棒是否为强趋势棒
        
        Al Brooks: 强突破棒的特征 - 实体大、影线小、收盘价在极端位置
        
        Args:
            bar_idx: K 线索引
            direction: 突破方向 "up" / "down"（与原趋势相反）
            min_body_ratio: 最小实体占比
        
        Returns:
            (is_strong, reason)
        """
        bar = df.iloc[bar_idx]
        high = float(bar["high"])
        low = float(bar["low"])
        open_price = float(bar["open"])
        close = float(bar["close"])
        
        kline_range = high - low
        if kline_range == 0:
            return (False, "K线范围为0")
        
        body = abs(close - open_price)
        body_ratio = body / kline_range
        
        # 实体占比检查
        if body_ratio < min_body_ratio:
            return (False, f"实体占比不足({body_ratio:.1%}<{min_body_ratio:.0%})")
        
        # 方向检查
        is_bullish = close > open_price
        is_bearish = close < open_price
        
        if direction == "up" and not is_bullish:
            return (False, "向上突破需要阳线")
        if direction == "down" and not is_bearish:
            return (False, "向下突破需要阴线")
        
        # 收盘位置检查（收盘价应在趋势方向的极端位置）
        if direction == "up":
            close_position = (close - low) / kline_range
            if close_position < 0.70:
                return (False, f"收盘位置不够高({close_position:.1%})")
        else:
            close_position = (high - close) / kline_range
            if close_position < 0.70:
                return (False, f"收盘位置不够低({close_position:.1%})")
        
        return (True, "强突破棒")
    
    @staticmethod
    def _count_overlapping_bars(
        df: pd.DataFrame,
        start_idx: int,
        end_idx: int,
        overlap_threshold: float = 0.5,
    ) -> int:
        """
        计算重叠棒数量（用于过滤弱突破）
        
        Al Brooks: 多根重叠棒表示市场犹豫，不是真正的突破
        
        Args:
            start_idx, end_idx: 检测范围
            overlap_threshold: 重叠比例阈值
        
        Returns:
            重叠棒数量
        """
        if end_idx <= start_idx:
            return 0
        
        overlap_count = 0
        for j in range(start_idx + 1, end_idx + 1):
            if j >= len(df):
                break
            curr = df.iloc[j]
            prev = df.iloc[j - 1]
            
            curr_high = float(curr["high"])
            curr_low = float(curr["low"])
            prev_high = float(prev["high"])
            prev_low = float(prev["low"])
            
            # 计算重叠区域
            overlap_high = min(curr_high, prev_high)
            overlap_low = max(curr_low, prev_low)
            
            if overlap_high > overlap_low:
                overlap_range = overlap_high - overlap_low
                curr_range = curr_high - curr_low
                if curr_range > 0 and overlap_range / curr_range >= overlap_threshold:
                    overlap_count += 1
        
        return overlap_count
    
    @staticmethod
    def _detect_double_top_bottom(
        df: pd.DataFrame,
        i: int,
        extreme_price: float,
        trend_direction: str,
        atr: Optional[float] = None,
        lookback: int = 30,
        min_bar_gap: int = 5,
    ) -> Tuple[bool, int]:
        """
        检测双顶/双底结构 - Al Brooks MTR 增强验证
        
        Al Brooks: 双顶/双底是 MTR 的核心结构，两个接近的极值点
        形成了"磁吸位"，增加了反转的可信度。
        
        检测逻辑：
        1. 在回看期内找到所有接近 extreme_price 的极值点
        2. 验证至少有 2 个极值点（间隔 >= min_bar_gap 根 K 线）
        3. 返回是否形成双顶/双底 + 第一个极值点的索引
        
        Args:
            df: K线数据
            i: 当前 K 线索引
            extreme_price: 极值价格
            trend_direction: 趋势方向 ("up" = 双顶, "down" = 双底)
            atr: ATR 值（用于计算容差）
            lookback: 回看周期
            min_bar_gap: 两个极值点之间的最小 K 线间隔
        
        Returns:
            (is_double_pattern, first_extreme_idx)
        """
        # 动态容差：0.5 * ATR 或 0.5% 价格
        if atr and atr > 0:
            tolerance = atr * 0.5
        else:
            tolerance = extreme_price * 0.005
        
        extremes: List[Tuple[int, float]] = []
        
        for j in range(max(0, i - lookback), i + 1):
            bar = df.iloc[j]
            if trend_direction == "up":
                # 双顶：找接近 extreme_price 的高点
                bar_high = float(bar["high"])
                if bar_high >= extreme_price - tolerance:
                    extremes.append((j, bar_high))
            else:
                # 双底：找接近 extreme_price 的低点
                bar_low = float(bar["low"])
                if bar_low <= extreme_price + tolerance:
                    extremes.append((j, bar_low))
        
        # 合并相邻的极值点（同一波动中的连续极值只算一个）
        merged_extremes: List[Tuple[int, float]] = []
        for idx, price in extremes:
            if not merged_extremes or idx - merged_extremes[-1][0] >= 2:
                merged_extremes.append((idx, price))
            else:
                # 更新为更极端的值
                if trend_direction == "up" and price > merged_extremes[-1][1]:
                    merged_extremes[-1] = (idx, price)
                elif trend_direction == "down" and price < merged_extremes[-1][1]:
                    merged_extremes[-1] = (idx, price)
        
        # 验证：至少需要 2 个极值点，且间隔 >= min_bar_gap
        if len(merged_extremes) >= 2:
            first_idx, first_price = merged_extremes[-2]
            second_idx, second_price = merged_extremes[-1]
            
            if second_idx - first_idx >= min_bar_gap:
                logging.debug(
                    f"✅ 双{'顶' if trend_direction == 'up' else '底'}检测: "
                    f"第一极值@{first_idx}={first_price:.2f}, "
                    f"第二极值@{second_idx}={second_price:.2f}, "
                    f"间隔={second_idx - first_idx}根"
                )
                return (True, first_idx)
        
        return (False, -1)
    
    @staticmethod
    def _detect_retest_with_false_breakout(
        df: pd.DataFrame,
        current_idx: int,
        extreme_price: float,
        trend_direction: str,
        atr: Optional[float] = None,
        fallback_tolerance: float = 0.003,
    ) -> Tuple[bool, bool, int]:
        """
        检测回测（允许假突破）- 动态 ATR 容差版
        
        Al Brooks: MTR 回测时常出现 Higher High（上升趋势）或 Lower Low（下降趋势）
        的假突破，这反而增加了反转的可信度。
        
        Al Brooks 修正：
        - BTC 等高波动资产使用固定百分比容差太窄（0.3% ≈ $300），容易被插针过滤
        - 改用 0.5 * ATR 作为动态容差，更适应市场波动
        
        Args:
            current_idx: 当前 K 线索引
            extreme_price: 原趋势的极值价格
            trend_direction: 原趋势方向
            atr: ATR 值（用于计算动态容差）
            fallback_tolerance: ATR 不可用时的回退容差比例
        
        Returns:
            (is_at_retest, is_false_breakout, retest_bar_idx)
        """
        bar = df.iloc[current_idx]
        bar_high = float(bar["high"])
        bar_low = float(bar["low"])
        
        # 检查前一根 K 线
        prev_bar = df.iloc[current_idx - 1] if current_idx > 0 else None
        prev_high = float(prev_bar["high"]) if prev_bar is not None else 0
        prev_low = float(prev_bar["low"]) if prev_bar is not None else float("inf")
        
        # 动态 ATR 容差：0.5 * ATR，回退到固定百分比
        # Al Brooks: 对于 BTC 这种高波动资产，ATR 容差更合理
        if atr and atr > 0:
            atr_tolerance = atr * 0.5
        else:
            atr_tolerance = extreme_price * fallback_tolerance
        
        if trend_direction == "up":
            # 上升趋势回测前高
            retest_zone = extreme_price - atr_tolerance
            at_retest = bar_high >= retest_zone or prev_high >= retest_zone
            # 假突破：创出更高高点（超过极值 + 0.25 * ATR）
            false_breakout_threshold = extreme_price + (atr_tolerance * 0.5)
            false_breakout = bar_high > false_breakout_threshold
            retest_bar = current_idx if bar_high >= retest_zone else (current_idx - 1 if prev_high >= retest_zone else -1)
        else:
            # 下降趋势回测前低
            retest_zone = extreme_price + atr_tolerance
            at_retest = bar_low <= retest_zone or prev_low <= retest_zone
            # 假突破：创出更低低点（低于极值 - 0.25 * ATR）
            false_breakout_threshold = extreme_price - (atr_tolerance * 0.5)
            false_breakout = bar_low < false_breakout_threshold
            retest_bar = current_idx if bar_low <= retest_zone else (current_idx - 1 if prev_low <= retest_zone else -1)
        
        return (at_retest, false_breakout, retest_bar)
    
    def detect_mtr_reversal(
        self,
        df: pd.DataFrame,
        i: int,
        ema: float,
        atr: Optional[float] = None,
        market_state: Optional[MarketState] = None,
        *,
        mtr_lookback: int = 60,
        min_trend_bars: int = 8,
        retest_tolerance: float = 0.003,
        max_overlapping_bars: int = 3,
    ) -> Optional[Tuple[str, str, float, float]]:
        """
        检测 MTR（Major Trend Reversal）主要趋势反转 - 纯价格行为版
        
        Al Brooks MTR 核心逻辑（不依赖 EMA 交叉）：
        1. 趋势定义：回看 60 根 K 线，识别显著的上升/下降趋势
           - 上升趋势：连续的 Higher High + Higher Low
           - 下降趋势：连续的 Lower High + Lower Low
        
        2. 趋势线突破：连接趋势中的主要高/低点
           - 上升趋势：连接 swing lows 形成支撑线，价格跌破
           - 下降趋势：连接 swing highs 形成压力线，价格突破
        
        3. 强力突破棒：突破棒必须是强趋势棒
           - 实体占比 >= 55%
           - 收盘在极端位置（顶部/底部 30%）
        
        4. 回测（允许假突破）：价格回到前极值附近
           - 允许 Higher High（上升趋势）或 Lower Low（下降趋势）
           - 在假突破后寻找反转信号棒
        
        5. 弱突破过滤：如果突破区域有太多重叠棒，取消信号
        
        Args:
            df: K线数据
            i: 当前 K 线索引
            ema: EMA20（仅用于止盈参考）
            atr: ATR 值
            market_state: 市场状态
            mtr_lookback: 趋势识别回看周期（默认 60）
            min_trend_bars: 最少趋势 K 线数量
            retest_tolerance: 回测容差比例
            max_overlapping_bars: 最大允许重叠棒数量
        
        Returns:
            (signal_type, side, stop_loss, base_height) 或 None
        """
        if i < mtr_lookback + 5:
            return None
        
        current_bar = df.iloc[i]
        current_high = float(current_bar["high"])
        current_low = float(current_bar["low"])
        current_close = float(current_bar["close"])
        current_open = float(current_bar["open"])
        
        # ========== Step 1: 识别显著趋势 ==========
        trend_result = self._identify_significant_trend(
            df, i, lookback=mtr_lookback, min_swing_points=3
        )
        if trend_result is None:
            return None
        
        trend_direction, swing_highs, swing_lows, trend_strength = trend_result
        
        # 趋势强度过滤：至少 40% 才算显著趋势
        if trend_strength < 0.4:
            logging.debug(f"MTR 跳过: 趋势强度不足 ({trend_strength:.1%} < 40%)")
            return None
        
        # ========== Step 2: 计算趋势线 ==========
        if trend_direction == "up":
            # 上升趋势：连接 swing lows 形成支撑线
            trendline = self._calculate_trendline(swing_lows, "up")
            extreme_price = max(h for _, h in swing_highs[-3:])  # 趋势最高点
        else:
            # 下降趋势：连接 swing highs 形成压力线
            trendline = self._calculate_trendline(swing_highs, "down")
            extreme_price = min(l for _, l in swing_lows[-3:])  # 趋势最低点
        
        if trendline is None:
            logging.debug("MTR 跳过: 无法计算有效趋势线")
            return None
        
        # ========== Step 3: 检测趋势线突破 ==========
        # 在最近 20 根 K 线内寻找突破点
        break_bar_idx = None
        for check_idx in range(max(0, i - 20), i + 1):
            is_break, break_mag = self._is_trendline_break(
                df, check_idx, trendline, trend_direction, atr
            )
            if is_break:
                break_bar_idx = check_idx
                break
        
        if break_bar_idx is None:
            return None
        
        # ========== Step 4: 验证突破棒强度 ==========
        # 突破方向与原趋势相反
        breakout_direction = "down" if trend_direction == "up" else "up"
        is_strong, reason = self._is_strong_breakout_bar(
            df, break_bar_idx, breakout_direction, min_body_ratio=0.55
        )
        if not is_strong:
            logging.debug(f"MTR 跳过: 突破棒不够强 - {reason}")
            return None
        
        # ========== Step 5: 检测弱突破（重叠棒过滤）==========
        overlap_count = self._count_overlapping_bars(
            df, break_bar_idx, min(break_bar_idx + 5, i), overlap_threshold=0.5
        )
        if overlap_count > max_overlapping_bars:
            logging.debug(f"MTR 跳过: 突破后重叠棒过多 ({overlap_count} > {max_overlapping_bars})")
            return None
        
        # ========== Step 6: 检测回测（允许假突破）==========
        # Al Brooks 修正：使用动态 ATR 容差（0.5 * ATR），而非固定百分比
        at_retest, is_false_bo, retest_bar = self._detect_retest_with_false_breakout(
            df, i, extreme_price, trend_direction, atr=atr, fallback_tolerance=retest_tolerance
        )
        
        if not at_retest:
            return None
        
        # ========== Step 6.5: 双顶/双底验证（增强 MTR 可信度）==========
        # Al Brooks: 双顶/双底是 MTR 的核心结构，增加反转可信度
        is_double_pattern, first_extreme_idx = self._detect_double_top_bottom(
            df, i, extreme_price, trend_direction, atr=atr, lookback=30, min_bar_gap=5
        )
        
        # 如果有双顶/双底 + 假突破，信号更强
        has_strong_structure = is_double_pattern or is_false_bo
        
        # ========== Step 7: 验证反转信号棒 ==========
        if trend_direction == "up":
            # 上升趋势反转 → 做空
            valid_signal, signal_reason = self.validate_btc_signal_bar(
                current_bar, "sell", df=df, i=i, signal_type="MTR_Sell"
            )
            # Al Brooks 修正：双顶 + 假突破后的阴线更有说服力
            if valid_signal or (has_strong_structure and current_close < current_open):
                stop_loss = self.calculate_unified_stop_loss(df, i, "sell", current_close, atr)
                if stop_loss is None:
                    logging.debug(f"MTR_Sell 被跳过: High Risk Filter 止损距离过大")
                    return None
                # 如果有假突破，止损设在假突破高点上方
                # Al Brooks: 结构止损应设在假突破极值 + 0.5 ATR
                if is_false_bo:
                    stop_loss = max(stop_loss, current_high + (0.5 * atr if atr else current_high * 0.005))
                
                base_height = extreme_price - current_close
                if atr and atr > 0 and base_height < atr * 0.5:
                    base_height = atr * 2.0
                
                # 构建信号详情
                signal_details = []
                if is_double_pattern:
                    signal_details.append("双顶")
                if is_false_bo:
                    signal_details.append("假突破")
                signal_detail = f"({'+'.join(signal_details)})" if signal_details else ""
                
                logging.debug(
                    f"✅ MTR_Sell{signal_detail} 触发: "
                    f"趋势={trend_direction}, 前高={extreme_price:.2f}, "
                    f"趋势线突破@{break_bar_idx}, 趋势强度={trend_strength:.1%}"
                )
                return ("MTR_Sell", "sell", stop_loss, base_height)
            
            # 二阶段入场（H2 风格）
            if i >= 2:
                bar_before = df.iloc[i - 2]
                high_before = float(bar_before["high"])
                # Al Brooks 修正：使用动态 ATR 容差
                secondary_tolerance = (atr * 0.5) if atr and atr > 0 else extreme_price * retest_tolerance
                if high_before >= extreme_price - secondary_tolerance:
                    # 前一根也接近高点，当前是第二次测试
                    if current_close < current_open:  # 阴线
                        stop_loss = self.calculate_unified_stop_loss(df, i, "sell", current_close, atr)
                        if stop_loss is None:
                            logging.debug(f"MTR_Sell(二阶段) 被跳过: High Risk Filter 止损距离过大")
                        else:
                            base_height = extreme_price - current_close
                            if atr and atr > 0 and base_height < atr * 0.5:
                                base_height = atr * 2.0
                            logging.debug(f"✅ MTR_Sell(二阶段) 触发: 前高={extreme_price:.2f} 二次回测反转")
                            return ("MTR_Sell", "sell", stop_loss, base_height)
        
        else:
            # 下降趋势反转 → 做多
            valid_signal, signal_reason = self.validate_btc_signal_bar(
                current_bar, "buy", df=df, i=i, signal_type="MTR_Buy"
            )
            # Al Brooks 修正：双底 + 假突破后的阳线更有说服力
            if valid_signal or (has_strong_structure and current_close > current_open):
                stop_loss = self.calculate_unified_stop_loss(df, i, "buy", current_close, atr)
                if stop_loss is None:
                    logging.debug(f"MTR_Buy 被跳过: High Risk Filter 止损距离过大")
                    return None
                # 如果有假突破，止损设在假突破低点下方
                # Al Brooks: 结构止损应设在假突破极值 + 0.5 ATR
                if is_false_bo:
                    stop_loss = min(stop_loss, current_low - (0.5 * atr if atr else current_low * 0.005))
                
                base_height = current_close - extreme_price
                if atr and atr > 0 and base_height < atr * 0.5:
                    base_height = atr * 2.0
                
                # 构建信号详情
                signal_details = []
                if is_double_pattern:
                    signal_details.append("双底")
                if is_false_bo:
                    signal_details.append("假突破")
                signal_detail = f"({'+'.join(signal_details)})" if signal_details else ""
                
                logging.debug(
                    f"✅ MTR_Buy{signal_detail} 触发: "
                    f"趋势={trend_direction}, 前低={extreme_price:.2f}, "
                    f"趋势线突破@{break_bar_idx}, 趋势强度={trend_strength:.1%}"
                )
                return ("MTR_Buy", "buy", stop_loss, base_height)
            
            # 二阶段入场（L2 风格）
            if i >= 2:
                bar_before = df.iloc[i - 2]
                low_before = float(bar_before["low"])
                # Al Brooks 修正：使用动态 ATR 容差
                secondary_tolerance = (atr * 0.5) if atr and atr > 0 else extreme_price * retest_tolerance
                if low_before <= extreme_price + secondary_tolerance:
                    if current_close > current_open:  # 阳线
                        stop_loss = self.calculate_unified_stop_loss(df, i, "buy", current_close, atr)
                        if stop_loss is None:
                            logging.debug(f"MTR_Buy(二阶段) 被跳过: High Risk Filter 止损距离过大")
                        else:
                            base_height = current_close - extreme_price
                            if atr and atr > 0 and base_height < atr * 0.5:
                                base_height = atr * 2.0
                            logging.debug(f"✅ MTR_Buy(二阶段) 触发: 前低={extreme_price:.2f} 二次回测反转")
                            return ("MTR_Buy", "buy", stop_loss, base_height)
        
        return None
    
    def detect_final_flag_reversal(
        self,
        df: pd.DataFrame,
        i: int,
        ema: float,
        atr: Optional[float] = None,
        market_state: Optional[MarketState] = None,
        final_flag_info: Optional[dict] = None,
    ) -> Optional[Tuple[str, str, float, float]]:
        """
        检测 Final Flag Reversal（终极旗形反转）- Al Brooks 高胜率反转
        
        调用入口 - 实际逻辑已提取到 final_flag_reversal.py
        
        Al Brooks: "Final Flag 是趋势耗尽的最后挣扎。当价格突破旗形后迅速失败，
        这是高胜率的反转入场点，因为趋势已经耗尽了所有动能。"
        
        Args:
            df: K线数据
            i: 当前 K 线索引
            ema: EMA20 值
            atr: ATR 值
            market_state: 市场状态（必须是 FINAL_FLAG）
            final_flag_info: Final Flag 信息（来自 MarketAnalyzer）
        
        返回: (signal_type, side, stop_loss, base_height) 或 None
        """
        return detect_final_flag_reversal_impl(
            df=df,
            i=i,
            ema=ema,
            atr=atr,
            market_state=market_state,
            final_flag_info=final_flag_info,
            validate_btc_signal_bar_func=self.validate_btc_signal_bar,
        )
