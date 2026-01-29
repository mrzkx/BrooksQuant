"""
模式检测器

负责 Wedge、Failed Breakout、Spike、Climax 的检测逻辑

Al Brooks 核心模式：
- Strong Spike: 强突破直接入场
- Failed Breakout: 失败突破反转
- Wedge Reversal: 楔形反转（三次推进）
- Climax Reversal: 高潮竭尽反转
"""

import logging
import pandas as pd
from typing import Optional, Tuple
from .market_analyzer import MarketState
from .interval_params import get_interval_params, IntervalParams


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
    
    def __init__(self, lookback_period: int = 20, kline_interval: str = "5m"):
        self.lookback_period = lookback_period
        self.kline_interval = kline_interval
        
        # 加载周期自适应参数
        self._params: IntervalParams = get_interval_params(kline_interval)
        
        # 更新类属性为周期参数
        self.BTC_MIN_BODY_RATIO = self._params.min_body_ratio
        self.BTC_CLOSE_POSITION_PCT = self._params.close_position_pct
        
        logging.info(
            f"📐 PatternDetector 初始化: 周期={kline_interval}, "
            f"实体占比≥{self._params.min_body_ratio:.0%}, "
            f"收盘位置≤{self._params.close_position_pct:.0%}"
        )
    
    @staticmethod
    def validate_signal_close(row: pd.Series, side: str) -> bool:
        """
        验证K线收盘价位置是否符合信号要求（通用版）
        
        买入信号：收盘价必须在K线顶部25%区域
        卖出信号：收盘价必须在K线底部25%区域
        """
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        
        kline_range = high - low
        if kline_range == 0:
            return False
        
        if side == "buy":
            return bool((close - low) / kline_range >= 0.75)
        else:
            return bool((high - close) / kline_range >= 0.75)
    
    @classmethod
    def validate_btc_signal_bar(
        cls, 
        row: pd.Series, 
        side: str,
        min_body_ratio: Optional[float] = None,
        close_position_pct: Optional[float] = None
    ) -> tuple[bool, str]:
        """
        BTC 专用信号棒质量验证（针对高波动长影线特性）
        
        Al Brooks: "信号棒的质量决定了交易的成功率"
        
        BTC 特殊要求：
        1. 实体必须占全长的 60% 以上（过滤长影线噪音）
        2. 买入信号：收盘价必须在最高 20% 区域（强势收盘）
        3. 卖出信号：收盘价必须在最低 20% 区域（弱势收盘）
        4. 信号棒方向必须与交易方向一致（买=阳线，卖=阴线）
        
        Args:
            row: K线数据
            side: 交易方向 ("buy" 或 "sell")
            min_body_ratio: 最小实体占比（默认 0.60）
            close_position_pct: 收盘位置要求（默认 0.20，即顶部/底部 20%）
        
        Returns:
            (is_valid, reason): 是否有效及原因
        """
        if min_body_ratio is None:
            min_body_ratio = cls.BTC_MIN_BODY_RATIO
        if close_position_pct is None:
            close_position_pct = cls.BTC_CLOSE_POSITION_PCT
        
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        open_price = float(row["open"])
        
        kline_range = high - low
        if kline_range == 0:
            return (False, "K线范围为0")
        
        body_size = abs(close - open_price)
        body_ratio = body_size / kline_range
        
        # ========== 条件1: 实体占比检查 ==========
        if body_ratio < min_body_ratio:
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
        
        return (True, "信号棒质量合格")
    
    def calculate_unified_stop_loss(
        self, df: pd.DataFrame, i: int, side: str, entry_price: float, atr: Optional[float] = None
    ) -> float:
        """
        Al Brooks 风格止损计算（周期自适应版）
        
        核心原则：止损放在 Signal Bar（前一根K线）的极值外
        
        Al Brooks: "如果市场回到 Signal Bar 之外，说明你的判断错了"
        
        周期自适应优化：
        1. 止损最小 ATR 倍数根据周期调整（短周期更宽）
        2. 止损最大 ATR 倍数根据周期调整
        3. 使用前两根 K 线的极值（提供更多缓冲）
        """
        if i < 2:
            return entry_price * (0.98 if side == "buy" else 1.02)
        
        signal_bar = df.iloc[i - 1]  # Signal Bar = 前一根 K 线
        prev_bar = df.iloc[i - 2]    # 前两根 K 线
        
        # Buffer = 0.15 * ATR 或最小 0.15%
        if atr and atr > 0:
            buffer = atr * 0.15
        else:
            buffer = entry_price * 0.0015
        
        # 使用周期自适应 ATR 倍数
        atr_stop_min = self._params.atr_stop_min_mult
        atr_stop_max = self._params.atr_stop_max_mult
        
        if side == "buy":
            # 买入：止损在前两根 K 线低点下方（取较低者）
            two_bar_low = min(signal_bar["low"], prev_bar["low"])
            signal_bar_stop = two_bar_low - buffer
            
            if atr and atr > 0:
                # 最小距离：周期自适应
                min_stop_distance = atr * atr_stop_min
                min_stop = entry_price - min_stop_distance
                if signal_bar_stop > min_stop:
                    signal_bar_stop = min_stop
                
                # 最大距离：周期自适应
                max_stop_distance = atr * atr_stop_max
                floor_stop = entry_price - max_stop_distance
                signal_bar_stop = max(signal_bar_stop, floor_stop)
            
            return signal_bar_stop
        else:
            # 卖出：止损在前两根 K 线高点上方（取较高者）
            two_bar_high = max(signal_bar["high"], prev_bar["high"])
            signal_bar_stop = two_bar_high + buffer
            
            if atr and atr > 0:
                # 最小距离：周期自适应
                min_stop_distance = atr * atr_stop_min
                max_stop = entry_price + min_stop_distance
                if signal_bar_stop < max_stop:
                    signal_bar_stop = max_stop
                
                # 最大距离：周期自适应
                max_stop_distance = atr * atr_stop_max
                ceiling_stop = entry_price + max_stop_distance
                signal_bar_stop = min(signal_bar_stop, ceiling_stop)
            
            return signal_bar_stop
    
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
        检测 Strong Spike（强突破入场）- Al Brooks Spike & Channel 对齐版
        
        增强突破定义：
        1. Signal Bar（前一根 i-1）实体占比 > 70%，且必须突破过去 10 根 K 线的极值
        2. Entry Bar（当前 Bar i）续延性验证：同向强 K 线，实体 > 50%
        
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
        
        # ---------- 向上突破 ----------
        if s_close > s_open and e_close > e_open:
            # Signal Bar: 实体占比 > 70%，且突破过去 10 根最高点
            if s_range <= 0:
                return None
            signal_body_ratio = s_body / s_range
            if signal_body_ratio <= 0.70:
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
            if signal_body_ratio <= 0.70:
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
    
    def detect_climax_reversal(
        self, df: pd.DataFrame, i: int, ema: float, atr: Optional[float] = None
    ) -> Optional[Tuple[str, str, float, float]]:
        """
        检测 Climax 反转信号
        
        当检测到 Climax（Spike 长度超过 2.5 倍 ATR）后，寻找反转信号
        
        优化增强：
        1. 尾部影线检查 - Al Brooks 强调真正的 Climax 有明显的"拒绝影线"
        2. 前期走势深度检查 - 确保是真正的超卖/超买
        
        返回: (signal_type, side, stop_loss, base_height) 或 None
        """
        if i < 3 or atr is None or atr <= 0:  # 需要至少3根K线来检查前期走势
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
                
                # ⭐ 新增：尾部影线检查（上影线 = 拒绝更高价格）
                upper_tail = high - max(open_price, close)
                tail_ratio = upper_tail / current_range
                if tail_ratio < 0.15:  # 上影线至少占 K 线的 15%
                    logging.debug(f"Climax_Sell 被跳过: 上影线不足 ({tail_ratio:.1%} < 15%)")
                    return None
                
                # ⭐ 新增：前期走势深度检查（确保是真正的超买）
                # 检查前 3 根 K 线的整体涨幅
                prior_bar = df.iloc[i - 3]
                prior_move = prev_high - prior_bar["low"]  # 从前3根的低点到Climax高点
                if prior_move < atr * 1.5:  # 之前涨幅不够深
                    logging.debug(f"Climax_Sell 被跳过: 前期涨幅不足 ({prior_move:.2f} < {atr * 1.5:.2f})")
                    return None
                
                stop_loss = self.calculate_unified_stop_loss(df, i, "sell", close, atr)
                logging.debug(f"✅ Climax_Sell 触发: 上影线={tail_ratio:.1%}, 前期涨幅={prior_move:.2f}")
                return ("Climax_Sell", "sell", stop_loss, prev_range)
        
        # 向下 Climax -> Climax_Buy（做多反转）
        if prev_range > atr * CLIMAX_ATR_MULTIPLIER and prev_close < prev_open:
            if close > open_price and close > prev_close:
                if not self.validate_signal_close(current_bar, "buy"):
                    return None
                
                # ⭐ 新增：尾部影线检查（下影线 = 拒绝更低价格）
                lower_tail = min(open_price, close) - low
                tail_ratio = lower_tail / current_range
                if tail_ratio < 0.15:  # 下影线至少占 K 线的 15%
                    logging.debug(f"Climax_Buy 被跳过: 下影线不足 ({tail_ratio:.1%} < 15%)")
                    return None
                
                # ⭐ 新增：前期走势深度检查（确保是真正的超卖）
                # 检查前 3 根 K 线的整体跌幅
                prior_bar = df.iloc[i - 3]
                prior_move = prior_bar["high"] - prev_low  # 从前3根的高点到Climax低点
                if prior_move < atr * 1.5:  # 之前跌幅不够深
                    logging.debug(f"Climax_Buy 被跳过: 前期跌幅不足 ({prior_move:.2f} < {atr * 1.5:.2f})")
                    return None
                
                stop_loss = self.calculate_unified_stop_loss(df, i, "buy", close, atr)
                logging.debug(f"✅ Climax_Buy 触发: 下影线={tail_ratio:.1%}, 前期跌幅={prior_move:.2f}")
                return ("Climax_Buy", "buy", stop_loss, prev_range)
        
        return None
    
    def detect_failed_breakout(
        self, df: pd.DataFrame, i: int, ema: float, atr: Optional[float] = None,
        market_state: Optional[MarketState] = None
    ) -> Optional[Tuple[str, str, float, float]]:
        """
        检测 Failed Breakout（失败突破反转）
        
        优化：
        1. 使用更短期的高/低点（10根而非20根），更敏感
        2. 放宽收盘价验证（从75%降到60%）
        3. 仅在 TRADING_RANGE 状态下激活
        4. ⭐ 新增：检查之前是否已有突破（防止把真突破当假突破）
        5. ⭐ 新增：要求当根K线是"第一根"创新高/新低的K线
        
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
                # 优化：收盘价验证从75%放宽到60%（收盘在K线下半部分即可）
                close_position = (high - close) / kline_range
                if close_position >= 0.6:  # 收盘在K线60%以下位置
                    stop_loss = self.calculate_unified_stop_loss(df, i, "sell", close, atr)
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
                # 优化：收盘价验证从75%放宽到60%（收盘在K线上半部分即可）
                close_position = (close - low) / kline_range
                if close_position >= 0.6:  # 收盘在K线60%以上位置
                    stop_loss = self.calculate_unified_stop_loss(df, i, "buy", close, atr)
                    logging.debug(f"✅ FailedBreakout_Buy 触发: 创新低{current_low:.2f}后反转，收盘位置={close_position:.1%}")
                    return ("FailedBreakout_Buy", "buy", stop_loss, range_width)
        
        return None
    
    def detect_wedge_reversal(
        self,
        df: pd.DataFrame,
        i: int,
        ema: float,
        atr: Optional[float] = None,
        market_state: Optional[MarketState] = None,
    ) -> Optional[Tuple[str, str, float, float, float, float, bool]]:
        """
        检测 Wedge Reversal（楔形反转，三次推进）- Al Brooks 加固版
        
        三推验证：
        - 指数间隔：idx2 - idx1 >= 3 且 idx3 - idx2 >= 3
        - 动能递减：(Price3 - Price2) < (Price2 - Price1)
        
        上下文过滤：
        - 禁止在 TIGHT_CHANNEL 中触发反转
        - 必须在价格偏离 EMA 超过 1.2 * ATR 时才考虑反转
        
        止损与止盈：
        - SL = 第三推极值 +/- 0.5 * ATR
        - TP1 = EMA20 位置
        - TP2 = 楔形起点（Measured Move 目标）
        
        返回: (signal_type, side, stop_loss, base_height, wedge_tp1, wedge_tp2, is_strong_reversal_bar) 或 None
        """
        # 上下文过滤：禁止在紧凑通道中反转
        if market_state == MarketState.TIGHT_CHANNEL:
            return None
        
        # 必须在价格偏离 EMA 超过 1.2 * ATR 时才考虑反转
        if atr is not None and atr > 0:
            current_close = float(df.iloc[i]["close"])
            if abs(current_close - ema) < 1.2 * atr:
                return None
        
        # 三推指数间隔：至少 3 根 K 线
        LEG_SPAN_MIN = 3
        min_total_span = self._params.wedge_min_total_span
        
        if atr and atr > 0:
            dynamic_span = max(
                int(min_total_span * 0.6),
                min(min_total_span, int(300 / atr))
            )
            min_total_span = dynamic_span
        
        if i < 15:
            return None
        
        lookback_start = max(0, i - 25)
        recent_data = df.iloc[lookback_start : i + 1]
        
        # 检测 High 3（上升楔形）
        recent_highs = [recent_data.iloc[j]["high"] for j in range(len(recent_data))]
        if len(recent_highs) >= 10:
            peaks = []
            for j in range(1, len(recent_highs) - 1):
                if recent_highs[j] > recent_highs[j - 1] and recent_highs[j] > recent_highs[j + 1]:
                    actual_idx = lookback_start + j
                    peaks.append((actual_idx, recent_highs[j]))
            
            if len(peaks) >= 3:
                last_3_peaks = peaks[-3:]
                peak_indices = [p[0] for p in last_3_peaks]
                peak_values = [p[1] for p in last_3_peaks]
                
                if (peak_values[0] < peak_values[1] < peak_values[2] and 
                    (peak_values[1] - peak_values[0]) > (peak_values[2] - peak_values[1])):
                    
                    # 纵向距离：第一推 (P1→P2) vs 第三推 (P2→P3)。若第三推 > 第一推的 120% 说明趋势在加速非衰减，跳过
                    first_push = peak_values[1] - peak_values[0]
                    third_push = peak_values[2] - peak_values[1]
                    if first_push > 0 and third_push > 1.2 * first_push:
                        logging.debug(
                            f"Wedge_Sell 跳过: 第三推纵向({third_push:.2f}) > 第一推120%({1.2*first_push:.2f})，趋势加速"
                        )
                    else:
                        # 三推指数间隔：idx2-idx1>=3 且 idx3-idx2>=3
                        if peak_indices[2] - peak_indices[0] < min_total_span:
                            pass
                        elif (peak_indices[1] - peak_indices[0] < LEG_SPAN_MIN
                              or peak_indices[2] - peak_indices[1] < LEG_SPAN_MIN):
                            pass
                        elif df.iloc[peak_indices[2]]["body_size"] >= df.iloc[peak_indices[0]]["body_size"]:
                            pass
                        else:
                            third_bar = df.iloc[peak_indices[2]]
                            is_bearish = third_bar["close"] < third_bar["open"]
                            upper_shadow = third_bar["high"] - max(third_bar["open"], third_bar["close"])
                            body_size = abs(third_bar["close"] - third_bar["open"])
                            has_long_upper = upper_shadow > body_size * 2 if body_size > 0 else upper_shadow > (third_bar["high"] - third_bar["low"]) * 0.3
                            
                            if is_bearish or has_long_upper:
                                current_close = float(df.iloc[i]["close"])
                                if current_close < peak_values[2] * 0.98:
                                    current_bar = df.iloc[i]
                                    if self.validate_signal_close(current_bar, "sell"):
                                        third_high = peak_values[2]
                                        # SL = 极值 + 0.5 * ATR
                                        stop_loss = third_high + (0.5 * atr) if atr and atr > 0 else third_high * 1.001
                                        wedge_height = peak_values[2] - peak_values[0]
                                        wedge_tp1 = ema  # TP1 = EMA20
                                        wedge_tp2 = float(df.iloc[peak_indices[0]]["low"])  # TP2 = 楔形起点
                                        # Signal Bar 强反转棒：上影线占比 > 30%
                                        sb_range = float(current_bar["high"]) - float(current_bar["low"])
                                        sb_upper = float(current_bar["high"]) - max(float(current_bar["open"]), float(current_bar["close"]))
                                        is_strong_reversal_bar = sb_range > 0 and (sb_upper / sb_range) > 0.3
                                        return ("Wedge_Sell", "sell", stop_loss, wedge_height, wedge_tp1, wedge_tp2, is_strong_reversal_bar)
        
        # 检测 Low 3（下降楔形）
        recent_lows = [recent_data.iloc[j]["low"] for j in range(len(recent_data))]
        if len(recent_lows) >= 10:
            troughs = []
            for j in range(1, len(recent_lows) - 1):
                if recent_lows[j] < recent_lows[j - 1] and recent_lows[j] < recent_lows[j + 1]:
                    actual_idx = lookback_start + j
                    troughs.append((actual_idx, recent_lows[j]))
            
            if len(troughs) >= 3:
                last_3_troughs = troughs[-3:]
                trough_indices = [t[0] for t in last_3_troughs]
                trough_values = [t[1] for t in last_3_troughs]
                
                if (trough_values[0] > trough_values[1] > trough_values[2] and 
                    (trough_values[0] - trough_values[1]) > (trough_values[1] - trough_values[2])):
                    
                    # 纵向距离：第一推 (P1→P2) vs 第三推 (P2→P3)。若第三推 > 第一推的 120% 说明趋势在加速非衰减，跳过
                    first_push = trough_values[0] - trough_values[1]
                    third_push = trough_values[1] - trough_values[2]
                    if first_push > 0 and third_push > 1.2 * first_push:
                        logging.debug(
                            f"Wedge_Buy 跳过: 第三推纵向({third_push:.2f}) > 第一推120%({1.2*first_push:.2f})，趋势加速"
                        )
                    else:
                        # 三推指数间隔：idx2-idx1>=3 且 idx3-idx2>=3
                        if trough_indices[2] - trough_indices[0] < min_total_span:
                            pass
                        elif (trough_indices[1] - trough_indices[0] < LEG_SPAN_MIN
                              or trough_indices[2] - trough_indices[1] < LEG_SPAN_MIN):
                            pass
                        elif df.iloc[trough_indices[2]]["body_size"] >= df.iloc[trough_indices[0]]["body_size"]:
                            pass
                        else:
                            third_bar = df.iloc[trough_indices[2]]
                            is_bullish = third_bar["close"] > third_bar["open"]
                            lower_shadow = min(third_bar["open"], third_bar["close"]) - third_bar["low"]
                            body_size = abs(third_bar["close"] - third_bar["open"])
                            has_long_lower = lower_shadow > body_size * 2 if body_size > 0 else lower_shadow > (third_bar["high"] - third_bar["low"]) * 0.3
                            
                            if is_bullish or has_long_lower:
                                current_close = float(df.iloc[i]["close"])
                                if current_close > trough_values[2] * 1.02:
                                    current_bar = df.iloc[i]
                                    if not self.validate_signal_close(current_bar, "buy"):
                                        logging.debug("Wedge_Buy 跳过: Signal Bar 收盘未在全长前25%区域")
                                        pass
                                    else:
                                        sb_high = float(current_bar["high"])
                                        sb_low = float(current_bar["low"])
                                        sb_open = float(current_bar["open"])
                                        sb_close = float(current_bar["close"])
                                        sb_body = abs(sb_close - sb_open)
                                        sb_lower_shadow = min(sb_open, sb_close) - sb_low
                                        if sb_body > 0 and sb_lower_shadow <= 1.5 * sb_body:
                                            logging.debug(
                                                f"Wedge_Buy 跳过: Signal Bar 下影线未大于实体1.5倍，非探底回升"
                                            )
                                        elif sb_body == 0 and sb_lower_shadow <= 0:
                                            logging.debug("Wedge_Buy 跳过: Signal Bar 无实体且无下影线")
                                        else:
                                            third_low = trough_values[2]
                                            # SL = 极值 - 0.5 * ATR
                                            stop_loss = third_low - (0.5 * atr) if atr and atr > 0 else third_low * 0.999
                                            wedge_height = trough_values[0] - trough_values[2]
                                            wedge_tp1 = ema  # TP1 = EMA20
                                            wedge_tp2 = float(df.iloc[trough_indices[0]]["high"])  # TP2 = 楔形起点
                                            # Signal Bar 强反转棒：下影线占比 > 30%
                                            sb_range = sb_high - sb_low
                                            is_strong_reversal_bar = sb_range > 0 and (sb_lower_shadow / sb_range) > 0.3
                                            return ("Wedge_Buy", "buy", stop_loss, wedge_height, wedge_tp1, wedge_tp2, is_strong_reversal_bar)
        
        return None
