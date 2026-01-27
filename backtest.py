"""
BrooksQuant 回测系统

与实盘策略完全一致（不使用动态订单流），支持：
- 历史K线数据下载
- Al Brooks 价格行为策略回测
- 分批止盈止损模拟
- 追踪止损（Trailing Stop）
- 详细统计报告

使用方法:
    python backtest.py --symbol BTCUSDT --interval 5m --start 2024-01-01 --end 2024-12-31
"""

import asyncio
import argparse
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from decimal import Decimal, ROUND_DOWN

import pandas as pd
from binance import AsyncClient

# 导入策略模块
from logic.market_analyzer import MarketState, MarketAnalyzer
from logic.patterns import PatternDetector
from logic.state_machines import H2StateMachine, L2StateMachine
from logic.talib_indicators import compute_ema, compute_atr


# ============================================================================
# 配置
# ============================================================================

@dataclass
class BacktestConfig:
    """回测配置"""
    symbol: str = "BTCUSDT"
    interval: str = "5m"
    start_date: str = "2024-01-01"
    end_date: str = "2024-12-31"
    
    # 资金管理
    initial_capital: float = 1000.0  # 初始资金 (USDT)
    leverage: int = 20  # 杠杆倍数
    position_size_pct: float = 1.0  # 仓位比例 (1.0 = 100%)
    
    # 手续费
    maker_fee: float = 0.0002  # 限价单手续费 0.02%
    taker_fee: float = 0.0004  # 市价单手续费 0.04%
    
    # 策略参数
    ema_period: int = 20
    lookback_period: int = 20
    stop_loss_atr_multiplier: float = 2.0  # 止损 ATR 乘数
    
    # 风控参数
    cooldown_bars: int = 3  # 止损后冷却K线数


# ============================================================================
# 数据结构
# ============================================================================

@dataclass
class BacktestTrade:
    """回测交易记录"""
    id: int
    signal: str
    side: str  # "buy" / "sell"
    entry_price: float
    quantity: float
    stop_loss: float
    take_profit: float
    tp1_price: float
    tp2_price: float
    entry_bar: int
    entry_time: datetime
    
    # 出场信息
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    exit_bar: Optional[int] = None
    exit_time: Optional[datetime] = None
    
    # 盈亏
    pnl: Optional[float] = None
    pnl_percent: Optional[float] = None
    
    # 分批止盈
    exit_stage: int = 0  # 0:未出场, 1:半仓, 2:全仓
    tp1_price_filled: Optional[float] = None
    remaining_quantity: Optional[float] = None
    
    # 追踪止损
    trailing_stop_activated: bool = False
    trailing_stop_price: Optional[float] = None
    trailing_max_profit_r: float = 0.0
    original_stop_loss: Optional[float] = None
    
    # 手续费
    entry_fee: float = 0.0
    exit_fee: float = 0.0
    
    # 市场上下文
    market_state: Optional[str] = None


@dataclass
class BacktestPosition:
    """当前持仓"""
    trade: BacktestTrade
    current_stop_loss: float
    
    # 追踪止损状态
    trailing_stop_activated: bool = False
    trailing_stop_price: Optional[float] = None
    trailing_max_profit_r: float = 0.0


@dataclass 
class BacktestResult:
    """回测结果"""
    config: BacktestConfig
    trades: List[BacktestTrade]
    equity_curve: List[float]
    
    # 统计指标
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    
    total_pnl: float = 0.0
    total_pnl_percent: float = 0.0
    
    max_drawdown: float = 0.0
    max_drawdown_percent: float = 0.0
    
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0
    
    avg_win: float = 0.0
    avg_loss: float = 0.0
    avg_holding_bars: float = 0.0
    
    # 按信号类型统计
    stats_by_signal: Dict[str, Dict] = field(default_factory=dict)


# ============================================================================
# 回测策略（同步版本，不使用动态订单流）
# ============================================================================

class BacktestStrategy:
    """
    回测专用策略类
    
    与实盘 AlBrooksStrategy 逻辑一致，但：
    - 同步执行（不使用 async）
    - 不使用动态订单流 Delta 分析
    - 所有信号强度设为 1.0
    - 可配置止损 ATR 乘数
    """
    
    def __init__(self, ema_period: int = 20, lookback_period: int = 20, stop_loss_atr_multiplier: float = 2.0):
        self.ema_period = ema_period
        self.lookback_period = lookback_period
        self.stop_loss_atr_multiplier = stop_loss_atr_multiplier  # 止损 ATR 乘数
        
        # 初始化模块化组件
        self.market_analyzer = MarketAnalyzer(ema_period=ema_period)
        self.pattern_detector = PatternDetector(lookback_period=lookback_period)
        
        # 覆盖 pattern_detector 的止损计算方法
        self._override_stop_loss_calculation()
    
    def _override_stop_loss_calculation(self):
        """
        Al Brooks 风格止损计算（优化版）
        
        核心原则：止损放在 Signal Bar（前一根K线）的极值外
        最小距离：1.5 * ATR（防止太紧）
        最大距离：multiplier * ATR（防止太宽）
        """
        multiplier = self.stop_loss_atr_multiplier  # 用作最大安全边界的 ATR 倍数
        
        def custom_unified_stop_loss(df, i, side, entry_price, atr=None):
            if i < 2:
                return entry_price * (0.98 if side == "buy" else 1.02)
            
            signal_bar = df.iloc[i - 1]  # Signal Bar = 前一根 K 线
            prev_bar = df.iloc[i - 2]    # 前两根 K 线
            
            # Buffer = 0.15 * ATR 或最小 0.15%
            if atr and atr > 0:
                buffer = atr * 0.15
            else:
                buffer = entry_price * 0.0015
            
            if side == "buy":
                # 买入：止损在前两根 K 线低点下方（取较低者）
                two_bar_low = min(signal_bar["low"], prev_bar["low"])
                signal_bar_stop = two_bar_low - buffer
                
                if atr and atr > 0:
                    # 最小距离：至少 1.5 * ATR
                    min_stop_distance = atr * 1.5
                    min_stop = entry_price - min_stop_distance
                    if signal_bar_stop > min_stop:
                        signal_bar_stop = min_stop
                    
                    # 最大距离：不超过 multiplier * ATR
                    max_stop_distance = atr * multiplier
                    floor_stop = entry_price - max_stop_distance
                    signal_bar_stop = max(signal_bar_stop, floor_stop)
                
                return signal_bar_stop
            else:
                # 卖出：止损在前两根 K 线高点上方（取较高者）
                two_bar_high = max(signal_bar["high"], prev_bar["high"])
                signal_bar_stop = two_bar_high + buffer
                
                if atr and atr > 0:
                    # 最小距离：至少 1.5 * ATR
                    min_stop_distance = atr * 1.5
                    max_stop = entry_price + min_stop_distance
                    if signal_bar_stop < max_stop:
                        signal_bar_stop = max_stop
                    
                    # 最大距离：不超过 multiplier * ATR
                    max_stop_distance = atr * multiplier
                    ceiling_stop = entry_price + max_stop_distance
                    signal_bar_stop = min(signal_bar_stop, ceiling_stop)
                
                return signal_bar_stop
        
        # 替换 pattern_detector 的方法
        self.pattern_detector.calculate_unified_stop_loss = staticmethod(custom_unified_stop_loss)
    
    def _compute_ema(self, df: pd.DataFrame) -> pd.Series:
        """计算 EMA (使用 TA-Lib)"""
        return compute_ema(df["close"], self.ema_period)
    
    def _compute_atr(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        """计算 ATR (使用 TA-Lib)"""
        return compute_atr(df["high"], df["low"], df["close"], period)
    
    def _calculate_tp1_tp2(
        self, entry_price: float, stop_loss: float, side: str, 
        base_height: float, atr: Optional[float] = None
    ) -> Tuple[float, float]:
        """计算分批止盈目标位"""
        risk = abs(entry_price - stop_loss)
        
        if side == "buy":
            tp1 = entry_price + risk
            measured_move = entry_price + base_height if base_height > 0 else entry_price + (risk * 2)
            tp2 = max(measured_move, entry_price + (risk * 2))
            if base_height < risk * 1.5:
                tp2 = max(tp2, entry_price + (risk * 3))
        else:
            tp1 = entry_price - risk
            measured_move = entry_price - base_height if base_height > 0 else entry_price - (risk * 2)
            tp2 = min(measured_move, entry_price - (risk * 2))
            if base_height < risk * 1.5:
                tp2 = min(tp2, entry_price - (risk * 3))
        
        return (tp1, tp2)
    
    def generate_signals(self, df: pd.DataFrame, show_progress: bool = True) -> pd.DataFrame:
        """
        生成交易信号（同步版本）
        
        与实盘 AlBrooksStrategy.generate_signals 逻辑一致
        """
        data = df.copy()
        data["ema"] = self._compute_ema(data)
        
        if len(data) >= 20:
            data["atr"] = self._compute_atr(data, period=20)
        else:
            data["atr"] = None
        
        total_bars = len(data)
        progress_interval = max(total_bars // 20, 1000)  # 每5%或1000根打印一次
        
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
        
        # Spike 回撤入场状态
        pending_spike: Optional[Tuple[str, str, float, float, float, int]] = None
        
        # H2/L2 状态机
        h2_machine = H2StateMachine()
        l2_machine = L2StateMachine()
        
        for i in range(1, len(data)):
            # 进度显示
            if show_progress and i % progress_interval == 0:
                pct = i / total_bars * 100
                logging.info(f"信号生成进度: {pct:.1f}% ({i}/{total_bars})")
            
            row = data.iloc[i]
            close, high, low = row["close"], row["high"], row["low"]
            ema = row["ema"]
            atr = row["atr"] if "atr" in data.columns else None
            
            # 检测市场状态
            market_state = self.market_analyzer.detect_market_state(data, i, ema)
            market_states[i] = market_state.value
            
            # 获取趋势方向和强度
            trend_direction = self.market_analyzer.get_trend_direction()
            trend_strength = self.market_analyzer.get_trend_strength()
            
            # 计算紧凑通道评分
            tight_channel_scores[i] = self.market_analyzer.calculate_tight_channel_score(data, i, ema)
            
            # 紧凑通道方向
            tight_channel_direction = None
            if market_state == MarketState.TIGHT_CHANNEL:
                tight_channel_direction = self.market_analyzer.get_tight_channel_direction(data, i)
            
            # 强趋势模式判断
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
            
            # 处理待处理的 Spike 回撤入场
            if pending_spike is not None:
                signal_type, side, stop_loss, limit_price, base_height, spike_idx = pending_spike
                
                if side == "buy" and low <= limit_price:
                    signals[i] = signal_type
                    sides[i] = side
                    stops[i] = stop_loss
                    base_heights[i] = base_height
                    risk_reward_ratios[i] = 2.0
                    tp1, tp2 = self._calculate_tp1_tp2(limit_price, stop_loss, side, base_height, atr)
                    tp1_prices[i], tp2_prices[i] = tp1, tp2
                    pending_spike = None
                    h2_machine.set_strong_trend()
                    continue
                elif side == "sell" and high >= limit_price:
                    signals[i] = signal_type
                    sides[i] = side
                    stops[i] = stop_loss
                    base_heights[i] = base_height
                    risk_reward_ratios[i] = 2.0
                    tp1, tp2 = self._calculate_tp1_tp2(limit_price, stop_loss, side, base_height, atr)
                    tp1_prices[i], tp2_prices[i] = tp1, tp2
                    pending_spike = None
                    l2_machine.set_strong_trend()
                    continue
                elif (side == "buy" and close > limit_price * 1.05) or (side == "sell" and close < limit_price * 0.95):
                    pending_spike = None
                elif i - spike_idx > 5:
                    pending_spike = None
            
            # 优先级1: Failed Breakout
            if market_state == MarketState.TRADING_RANGE and not is_strong_trend_mode:
                result = self.pattern_detector.detect_failed_breakout(data, i, ema, atr, market_state)
                if result:
                    signal_type, side, stop_loss, base_height = result
                    
                    if allowed_side is not None and side != allowed_side:
                        continue
                    
                    signals[i] = signal_type
                    sides[i] = side
                    stops[i] = stop_loss
                    base_heights[i] = base_height
                    risk_reward_ratios[i] = 1.0
                    tp1, tp2 = self._calculate_tp1_tp2(close, stop_loss, side, base_height, atr)
                    tp1_prices[i], tp2_prices[i] = tp1, tp2
                    continue
            
            # 优先级2: Strong Spike
            spike_result = self.pattern_detector.detect_strong_spike(data, i, ema, atr, market_state)
            if spike_result:
                signal_type, side, stop_loss, limit_price, base_height = spike_result
                
                if allowed_side is not None and side != allowed_side:
                    continue
                
                if limit_price is not None:
                    pending_spike = (signal_type, side, stop_loss, limit_price, base_height, i)
                else:
                    # 回测中不使用 Delta 过滤，直接入场
                    signals[i] = signal_type
                    sides[i] = side
                    stops[i] = stop_loss
                    base_heights[i] = base_height
                    risk_reward_ratios[i] = 2.0
                    tp1, tp2 = self._calculate_tp1_tp2(close, stop_loss, side, base_height, atr)
                    tp1_prices[i], tp2_prices[i] = tp1, tp2
                    if side == "buy":
                        h2_machine.set_strong_trend()
                    else:
                        l2_machine.set_strong_trend()
                continue
            
            # 优先级3: Climax 反转
            if not is_strong_trend_mode:
                climax_result = self.pattern_detector.detect_climax_reversal(data, i, ema, atr)
                if climax_result:
                    signal_type, side, stop_loss, base_height = climax_result
                    
                    if allowed_side is not None and side != allowed_side:
                        continue
                    
                    signals[i] = signal_type
                    sides[i] = side
                    stops[i] = stop_loss
                    base_heights[i] = base_height
                    risk_reward_ratios[i] = 2.0
                    tp1, tp2 = self._calculate_tp1_tp2(close, stop_loss, side, base_height, atr)
                    tp1_prices[i], tp2_prices[i] = tp1, tp2
                    continue
            
            # 优先级4: Wedge 反转
            if not is_strong_trend_mode:
                wedge_result = self.pattern_detector.detect_wedge_reversal(data, i, ema, atr)
                if wedge_result:
                    signal_type, side, stop_loss, base_height = wedge_result
                    
                    if allowed_side is not None and side != allowed_side:
                        continue
                    
                    signals[i] = signal_type
                    sides[i] = side
                    stops[i] = stop_loss
                    base_heights[i] = base_height
                    risk_reward_ratios[i] = 2.0
                    tp1, tp2 = self._calculate_tp1_tp2(close, stop_loss, side, base_height, atr)
                    tp1_prices[i], tp2_prices[i] = tp1, tp2
                    continue
            
            # H2 状态机
            if allowed_side is None or allowed_side == "buy":
                h2_signal = h2_machine.update(
                    close, high, low, ema, atr, data, i,
                    self.pattern_detector.calculate_unified_stop_loss
                )
                if h2_signal:
                    signals[i] = h2_signal.signal_type
                    sides[i] = h2_signal.side
                    stops[i] = h2_signal.stop_loss
                    base_heights[i] = h2_signal.base_height
                    risk_reward_ratios[i] = 2.0
                    tp1, tp2 = self._calculate_tp1_tp2(close, h2_signal.stop_loss, h2_signal.side, h2_signal.base_height, atr)
                    tp1_prices[i], tp2_prices[i] = tp1, tp2
            
            # L2 状态机
            if allowed_side is None or allowed_side == "sell":
                l2_signal = l2_machine.update(
                    close, high, low, ema, atr, data, i,
                    self.pattern_detector.calculate_unified_stop_loss
                )
                if l2_signal:
                    signals[i] = l2_signal.signal_type
                    sides[i] = l2_signal.side
                    stops[i] = l2_signal.stop_loss
                    base_heights[i] = l2_signal.base_height
                    risk_reward_ratios[i] = 2.0
                    tp1, tp2 = self._calculate_tp1_tp2(close, l2_signal.stop_loss, l2_signal.side, l2_signal.base_height, atr)
                    tp1_prices[i], tp2_prices[i] = tp1, tp2
        
        # 写入结果
        data["market_state"] = market_states
        data["signal"] = signals
        data["side"] = sides
        data["stop_loss"] = stops
        data["risk_reward_ratio"] = risk_reward_ratios
        data["base_height"] = base_heights
        data["tp1_price"] = tp1_prices
        data["tp2_price"] = tp2_prices
        data["tight_channel_score"] = tight_channel_scores
        
        return data


# ============================================================================
# 回测引擎
# ============================================================================

class BacktestEngine:
    """回测引擎"""
    
    def __init__(self, config: BacktestConfig):
        self.config = config
        self.strategy = BacktestStrategy(
            ema_period=config.ema_period,
            lookback_period=config.lookback_period,
            stop_loss_atr_multiplier=config.stop_loss_atr_multiplier
        )
        
        # 交易状态
        self.trades: List[BacktestTrade] = []
        self.position: Optional[BacktestPosition] = None
        self.trade_counter = 0
        
        # 资金状态
        self.capital = config.initial_capital
        self.equity_curve: List[float] = []
        
        # 冷却状态
        self.cooldown_until: int = 0
    
    async def load_data(self) -> pd.DataFrame:
        """从 Binance 下载历史K线数据"""
        client = await AsyncClient.create()
        
        try:
            start_dt = datetime.strptime(self.config.start_date, "%Y-%m-%d")
            end_dt = datetime.strptime(self.config.end_date, "%Y-%m-%d")
            
            # 多下载一些数据用于预热指标
            warmup_days = 30
            warmup_start = start_dt - timedelta(days=warmup_days)
            
            logging.info(f"正在下载 {self.config.symbol} {self.config.interval} 数据...")
            logging.info(f"时间范围: {warmup_start.date()} 到 {end_dt.date()}")
            
            klines = await client.get_historical_klines(
                symbol=self.config.symbol,
                interval=self.config.interval,
                start_str=warmup_start.strftime("%Y-%m-%d"),
                end_str=end_dt.strftime("%Y-%m-%d")
            )
            
            if not klines:
                raise ValueError("未能下载到K线数据")
            
            # 转换为 DataFrame
            df = pd.DataFrame(klines, columns=[
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "quote_volume", "trades", "taker_buy_base",
                "taker_buy_quote", "ignore"
            ])
            
            # 数据类型转换
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
            df["close_time"] = pd.to_datetime(df["close_time"], unit="ms")
            
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)
            
            df = df.set_index("open_time")
            
            logging.info(f"✅ 成功下载 {len(df)} 根K线数据")
            
            return df
            
        finally:
            await client.close_connection()
    
    def _calculate_quantity(self, price: float) -> float:
        """计算下单数量"""
        position_value = self.capital * self.config.position_size_pct * self.config.leverage
        quantity = position_value / price
        
        # 精度处理（3位小数）
        quantity = float(Decimal(str(quantity)).quantize(Decimal("0.001"), rounding=ROUND_DOWN))
        
        return max(quantity, 0.001)
    
    def _open_position(self, bar_idx: int, row: pd.Series, signal_data: pd.Series) -> None:
        """开仓"""
        self.trade_counter += 1
        
        entry_price = row["close"]
        quantity = self._calculate_quantity(entry_price)
        
        # 计算手续费（市价单）
        entry_fee = entry_price * quantity * self.config.taker_fee
        
        trade = BacktestTrade(
            id=self.trade_counter,
            signal=signal_data["signal"],
            side=signal_data["side"],
            entry_price=entry_price,
            quantity=quantity,
            stop_loss=signal_data["stop_loss"],
            take_profit=signal_data["tp2_price"],  # TP2 作为最终目标
            tp1_price=signal_data["tp1_price"],
            tp2_price=signal_data["tp2_price"],
            entry_bar=bar_idx,
            entry_time=row.name if hasattr(row, 'name') else datetime.now(),
            entry_fee=entry_fee,
            market_state=signal_data.get("market_state"),
            remaining_quantity=quantity,
            original_stop_loss=signal_data["stop_loss"]
        )
        
        self.position = BacktestPosition(
            trade=trade,
            current_stop_loss=signal_data["stop_loss"]
        )
        
        # 扣除手续费
        self.capital -= entry_fee
    
    def _check_exit(self, bar_idx: int, row: pd.Series) -> Optional[str]:
        """
        检查是否触发出场条件
        
        返回出场原因或 None
        """
        if self.position is None:
            return None
        
        trade = self.position.trade
        high = row["high"]
        low = row["low"]
        close = row["close"]
        
        side = trade.side
        entry_price = trade.entry_price
        stop_loss = self.position.current_stop_loss
        original_stop_loss = trade.original_stop_loss or stop_loss
        
        # 计算当前盈利（以 R 为单位）
        risk = abs(entry_price - original_stop_loss)
        if risk <= 0:
            risk = entry_price * 0.01  # 防止除零
        
        if side == "buy":
            current_profit = close - entry_price
        else:
            current_profit = entry_price - close
        
        current_profit_r = current_profit / risk
        
        # ========== 1. 止损检查（最高优先级）==========
        if side == "buy" and low <= stop_loss:
            return "stop_loss"
        elif side == "sell" and high >= stop_loss:
            return "stop_loss"
        
        # ========== 2. TP1 检查（半仓止盈）==========
        if trade.exit_stage == 0:
            if side == "buy" and high >= trade.tp1_price:
                return "tp1"
            elif side == "sell" and low <= trade.tp1_price:
                return "tp1"
        
        # ========== 3. TP2 检查（全部止盈）==========
        if trade.exit_stage == 1:
            if side == "buy" and high >= trade.tp2_price:
                return "tp2"
            elif side == "sell" and low <= trade.tp2_price:
                return "tp2"
        
        # ========== 4. 追踪止损逻辑（Al Brooks 动态退出）==========
        # 在盈利 0.8R 后激活追踪止损（提前保护利润）
        if current_profit_r >= 0.8 and not self.position.trailing_stop_activated:
            self.position.trailing_stop_activated = True
            self.position.trailing_max_profit_r = current_profit_r
            
            # 初始追踪止损：盈利的 50% 位置
            if side == "buy":
                self.position.trailing_stop_price = entry_price + (current_profit * 0.5)
            else:
                self.position.trailing_stop_price = entry_price - (current_profit * 0.5)
        
        # 更新追踪止损
        if self.position.trailing_stop_activated and current_profit_r > self.position.trailing_max_profit_r:
            self.position.trailing_max_profit_r = current_profit_r
            
            # 追踪止损跟随：保护 50% 的盈利
            if side == "buy":
                new_trailing_stop = entry_price + (current_profit * 0.5)
                if new_trailing_stop > (self.position.trailing_stop_price or 0):
                    self.position.trailing_stop_price = new_trailing_stop
                    self.position.current_stop_loss = new_trailing_stop
            else:
                new_trailing_stop = entry_price - (current_profit * 0.5)
                if new_trailing_stop < (self.position.trailing_stop_price or float('inf')):
                    self.position.trailing_stop_price = new_trailing_stop
                    self.position.current_stop_loss = new_trailing_stop
        
        # 检查追踪止损触发
        if self.position.trailing_stop_activated and self.position.trailing_stop_price:
            if side == "buy" and low <= self.position.trailing_stop_price:
                return "trailing_stop"
            elif side == "sell" and high >= self.position.trailing_stop_price:
                return "trailing_stop"
        
        return None
    
    def _close_position(self, bar_idx: int, row: pd.Series, reason: str) -> None:
        """平仓"""
        if self.position is None:
            return
        
        trade = self.position.trade
        side = trade.side
        
        # 确定出场价格
        if reason == "stop_loss":
            exit_price = self.position.current_stop_loss
        elif reason == "trailing_stop":
            exit_price = self.position.trailing_stop_price or row["close"]
        elif reason == "tp1":
            exit_price = trade.tp1_price
        elif reason == "tp2":
            exit_price = trade.tp2_price
        else:
            exit_price = row["close"]
        
        # 处理 TP1（半仓平仓）
        if reason == "tp1":
            half_qty = trade.remaining_quantity / 2
            
            # 计算 TP1 盈亏
            if side == "buy":
                tp1_pnl = (exit_price - trade.entry_price) * half_qty
            else:
                tp1_pnl = (trade.entry_price - exit_price) * half_qty
            
            # 手续费
            exit_fee = exit_price * half_qty * self.config.taker_fee
            tp1_pnl -= exit_fee
            
            # 更新资金
            self.capital += tp1_pnl
            
            # 更新交易记录
            trade.exit_stage = 1
            trade.tp1_price_filled = exit_price
            trade.remaining_quantity = half_qty
            trade.exit_fee += exit_fee
            
            # 移动止损到入场价（保本）
            self.position.current_stop_loss = trade.entry_price
            
            return  # 不完全平仓
        
        # 完全平仓
        close_qty = trade.remaining_quantity or trade.quantity
        
        # 计算最终盈亏
        if side == "buy":
            final_pnl = (exit_price - trade.entry_price) * close_qty
        else:
            final_pnl = (trade.entry_price - exit_price) * close_qty
        
        # 手续费
        exit_fee = exit_price * close_qty * self.config.taker_fee
        final_pnl -= exit_fee
        
        # 如果有 TP1，加上 TP1 的盈亏
        if trade.exit_stage == 1 and trade.tp1_price_filled:
            half_qty = trade.quantity / 2
            if side == "buy":
                tp1_pnl = (trade.tp1_price_filled - trade.entry_price) * half_qty
            else:
                tp1_pnl = (trade.entry_price - trade.tp1_price_filled) * half_qty
            total_pnl = tp1_pnl + final_pnl - trade.entry_fee
        else:
            total_pnl = final_pnl - trade.entry_fee
        
        # 更新交易记录
        trade.exit_price = exit_price
        trade.exit_reason = reason
        trade.exit_bar = bar_idx
        trade.exit_time = row.name if hasattr(row, 'name') else datetime.now()
        trade.exit_fee += exit_fee
        trade.exit_stage = 2
        trade.pnl = total_pnl
        
        cost_basis = trade.entry_price * trade.quantity
        if cost_basis > 0:
            trade.pnl_percent = (total_pnl / cost_basis) * 100
        
        # 追踪止损状态
        trade.trailing_stop_activated = self.position.trailing_stop_activated
        trade.trailing_stop_price = self.position.trailing_stop_price
        trade.trailing_max_profit_r = self.position.trailing_max_profit_r
        
        # 更新资金
        self.capital += final_pnl
        
        # 保存交易
        self.trades.append(trade)
        
        # 清空持仓
        self.position = None
        
        # 止损后设置冷却期
        if reason == "stop_loss" and total_pnl < 0:
            self.cooldown_until = bar_idx + self.config.cooldown_bars
    
    def run(self, df: pd.DataFrame) -> BacktestResult:
        """运行回测"""
        logging.info("正在生成交易信号...")
        
        # 生成信号
        df_with_signals = self.strategy.generate_signals(df)
        
        # 过滤到回测时间范围
        start_dt = datetime.strptime(self.config.start_date, "%Y-%m-%d")
        df_backtest = df_with_signals[df_with_signals.index >= start_dt]
        
        logging.info(f"开始回测，共 {len(df_backtest)} 根K线...")
        
        # 遍历每根K线
        for bar_idx, (timestamp, row) in enumerate(df_backtest.iterrows()):
            # 记录权益
            if self.position:
                # 浮动盈亏
                trade = self.position.trade
                if trade.side == "buy":
                    unrealized_pnl = (row["close"] - trade.entry_price) * (trade.remaining_quantity or trade.quantity)
                else:
                    unrealized_pnl = (trade.entry_price - row["close"]) * (trade.remaining_quantity or trade.quantity)
                self.equity_curve.append(self.capital + unrealized_pnl)
            else:
                self.equity_curve.append(self.capital)
            
            # 检查出场条件
            if self.position:
                exit_reason = self._check_exit(bar_idx, row)
                if exit_reason:
                    self._close_position(bar_idx, row, exit_reason)
            
            # 检查入场条件
            if self.position is None and row.get("signal") is not None:
                # 检查冷却期
                if bar_idx < self.cooldown_until:
                    continue
                
                # 开仓
                self._open_position(bar_idx, row, row)
        
        # 如果还有未平仓的持仓，强制平仓
        if self.position:
            last_row = df_backtest.iloc[-1]
            self._close_position(len(df_backtest) - 1, last_row, "end_of_backtest")
        
        # 计算统计指标
        result = self._calculate_statistics()
        
        return result
    
    def _calculate_statistics(self) -> BacktestResult:
        """计算回测统计指标"""
        result = BacktestResult(
            config=self.config,
            trades=self.trades,
            equity_curve=self.equity_curve
        )
        
        if not self.trades:
            return result
        
        # 基础统计
        result.total_trades = len(self.trades)
        result.winning_trades = sum(1 for t in self.trades if t.pnl and t.pnl > 0)
        result.losing_trades = sum(1 for t in self.trades if t.pnl and t.pnl <= 0)
        result.win_rate = result.winning_trades / result.total_trades * 100 if result.total_trades > 0 else 0
        
        # 盈亏统计
        result.total_pnl = sum(t.pnl for t in self.trades if t.pnl)
        result.total_pnl_percent = (result.total_pnl / self.config.initial_capital) * 100
        
        # 平均盈亏
        winning_pnls = [t.pnl for t in self.trades if t.pnl and t.pnl > 0]
        losing_pnls = [t.pnl for t in self.trades if t.pnl and t.pnl <= 0]
        
        result.avg_win = sum(winning_pnls) / len(winning_pnls) if winning_pnls else 0
        result.avg_loss = sum(losing_pnls) / len(losing_pnls) if losing_pnls else 0
        
        # 盈利因子
        gross_profit = sum(winning_pnls)
        gross_loss = abs(sum(losing_pnls))
        result.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        
        # 最大回撤
        peak = self.config.initial_capital
        max_dd = 0
        max_dd_pct = 0
        
        for equity in self.equity_curve:
            if equity > peak:
                peak = equity
            dd = peak - equity
            dd_pct = (dd / peak) * 100 if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
                max_dd_pct = dd_pct
        
        result.max_drawdown = max_dd
        result.max_drawdown_percent = max_dd_pct
        
        # 平均持仓时间
        holding_bars = []
        for t in self.trades:
            if t.exit_bar is not None:
                holding_bars.append(t.exit_bar - t.entry_bar)
        result.avg_holding_bars = sum(holding_bars) / len(holding_bars) if holding_bars else 0
        
        # 按信号类型统计
        signal_stats: Dict[str, Dict] = {}
        for t in self.trades:
            signal = t.signal
            if signal not in signal_stats:
                signal_stats[signal] = {
                    "count": 0,
                    "wins": 0,
                    "losses": 0,
                    "total_pnl": 0.0
                }
            signal_stats[signal]["count"] += 1
            if t.pnl and t.pnl > 0:
                signal_stats[signal]["wins"] += 1
            else:
                signal_stats[signal]["losses"] += 1
            signal_stats[signal]["total_pnl"] += t.pnl or 0
        
        # 计算每个信号的胜率
        for signal, stats in signal_stats.items():
            stats["win_rate"] = stats["wins"] / stats["count"] * 100 if stats["count"] > 0 else 0
        
        result.stats_by_signal = signal_stats
        
        # 简化的 Sharpe Ratio（假设无风险利率为0）
        if len(self.equity_curve) > 1:
            returns = []
            for i in range(1, len(self.equity_curve)):
                ret = (self.equity_curve[i] - self.equity_curve[i-1]) / self.equity_curve[i-1] if self.equity_curve[i-1] > 0 else 0
                returns.append(ret)
            
            if returns:
                import statistics
                avg_return = statistics.mean(returns)
                std_return = statistics.stdev(returns) if len(returns) > 1 else 0
                
                # 年化（假设5分钟K线，一年约 105,120 根）
                annualization_factor = (105120 / len(returns)) ** 0.5
                result.sharpe_ratio = (avg_return / std_return * annualization_factor) if std_return > 0 else 0
        
        return result


# ============================================================================
# 报告生成
# ============================================================================

def print_report(result: BacktestResult) -> None:
    """打印回测报告"""
    print("\n" + "=" * 70)
    print("                       BrooksQuant 回测报告")
    print("=" * 70)
    
    config = result.config
    print(f"\n【回测配置】")
    print(f"  交易对: {config.symbol}")
    print(f"  K线周期: {config.interval}")
    print(f"  时间范围: {config.start_date} ~ {config.end_date}")
    print(f"  初始资金: {config.initial_capital:,.2f} USDT")
    print(f"  杠杆: {config.leverage}x")
    print(f"  仓位比例: {config.position_size_pct * 100:.0f}%")
    
    print(f"\n【整体表现】")
    print(f"  总交易数: {result.total_trades}")
    print(f"  盈利次数: {result.winning_trades}")
    print(f"  亏损次数: {result.losing_trades}")
    print(f"  胜率: {result.win_rate:.2f}%")
    print(f"  总盈亏: {result.total_pnl:,.2f} USDT ({result.total_pnl_percent:+.2f}%)")
    print(f"  最终权益: {config.initial_capital + result.total_pnl:,.2f} USDT")
    
    print(f"\n【风险指标】")
    print(f"  最大回撤: {result.max_drawdown:,.2f} USDT ({result.max_drawdown_percent:.2f}%)")
    print(f"  盈利因子: {result.profit_factor:.2f}")
    print(f"  Sharpe Ratio: {result.sharpe_ratio:.2f}")
    
    print(f"\n【交易分析】")
    print(f"  平均盈利: {result.avg_win:,.2f} USDT")
    print(f"  平均亏损: {result.avg_loss:,.2f} USDT")
    print(f"  盈亏比: {abs(result.avg_win / result.avg_loss):.2f}" if result.avg_loss != 0 else "  盈亏比: N/A")
    print(f"  平均持仓: {result.avg_holding_bars:.1f} 根K线")
    
    print(f"\n【按信号类型统计】")
    print(f"  {'信号类型':<20} {'次数':>6} {'胜率':>8} {'盈亏':>12}")
    print(f"  {'-' * 48}")
    
    for signal, stats in sorted(result.stats_by_signal.items(), key=lambda x: -x[1]["count"]):
        print(f"  {signal:<20} {stats['count']:>6} {stats['win_rate']:>7.1f}% {stats['total_pnl']:>+11.2f}")
    
    # 出场原因统计
    exit_reasons: Dict[str, int] = {}
    for t in result.trades:
        reason = t.exit_reason or "unknown"
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
    
    print(f"\n【出场原因统计】")
    for reason, count in sorted(exit_reasons.items(), key=lambda x: -x[1]):
        pct = count / result.total_trades * 100 if result.total_trades > 0 else 0
        print(f"  {reason:<20}: {count:>4} ({pct:.1f}%)")
    
    print("\n" + "=" * 70)


def plot_equity_curve(result: BacktestResult, filename: str = "backtest_equity.png") -> None:
    """绘制权益曲线"""
    try:
        import matplotlib.pyplot as plt
        
        fig, axes = plt.subplots(2, 1, figsize=(14, 10), gridspec_kw={'height_ratios': [3, 1]})
        
        # 权益曲线
        ax1 = axes[0]
        equity = result.equity_curve
        ax1.plot(equity, color='blue', linewidth=1)
        ax1.axhline(y=result.config.initial_capital, color='gray', linestyle='--', alpha=0.5)
        ax1.fill_between(range(len(equity)), result.config.initial_capital, equity, 
                        where=[e >= result.config.initial_capital for e in equity],
                        color='green', alpha=0.3)
        ax1.fill_between(range(len(equity)), result.config.initial_capital, equity,
                        where=[e < result.config.initial_capital for e in equity],
                        color='red', alpha=0.3)
        ax1.set_title(f'BrooksQuant 回测权益曲线 ({result.config.symbol} {result.config.interval})', fontsize=14)
        ax1.set_ylabel('权益 (USDT)')
        ax1.grid(True, alpha=0.3)
        
        # 标注关键信息
        final_equity = result.config.initial_capital + result.total_pnl
        ax1.annotate(f'最终: {final_equity:,.2f} USDT\n盈亏: {result.total_pnl_percent:+.2f}%',
                    xy=(len(equity)-1, equity[-1] if equity else 0),
                    xytext=(10, 10), textcoords='offset points',
                    fontsize=10, ha='left')
        
        # 回撤曲线
        ax2 = axes[1]
        drawdowns = []
        peak = result.config.initial_capital
        for e in equity:
            if e > peak:
                peak = e
            dd_pct = ((peak - e) / peak * 100) if peak > 0 else 0
            drawdowns.append(dd_pct)
        
        ax2.fill_between(range(len(drawdowns)), 0, drawdowns, color='red', alpha=0.5)
        ax2.set_ylabel('回撤 (%)')
        ax2.set_xlabel('K线数量')
        ax2.invert_yaxis()
        ax2.grid(True, alpha=0.3)
        ax2.annotate(f'最大回撤: {result.max_drawdown_percent:.2f}%',
                    xy=(drawdowns.index(max(drawdowns)) if drawdowns else 0, max(drawdowns) if drawdowns else 0),
                    xytext=(10, -10), textcoords='offset points',
                    fontsize=10)
        
        plt.tight_layout()
        plt.savefig(filename, dpi=150)
        plt.close()
        
        print(f"\n✅ 权益曲线已保存到: {filename}")
        
    except ImportError:
        print("\n⚠️ 未安装 matplotlib，跳过图表生成")
    except Exception as e:
        print(f"\n⚠️ 生成图表失败: {e}")


def export_trades_csv(result: BacktestResult, filename: str = "backtest_trades.csv") -> None:
    """导出交易记录到 CSV"""
    import csv
    
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "ID", "信号", "方向", "入场价", "数量", "止损", "止盈",
            "出场价", "出场原因", "盈亏", "盈亏%", "入场时间", "出场时间",
            "持仓K线", "追踪止损", "市场状态"
        ])
        
        for t in result.trades:
            holding_bars = (t.exit_bar - t.entry_bar) if t.exit_bar else 0
            writer.writerow([
                t.id, t.signal, t.side, f"{t.entry_price:.2f}", f"{t.quantity:.4f}",
                f"{t.stop_loss:.2f}", f"{t.take_profit:.2f}",
                f"{t.exit_price:.2f}" if t.exit_price else "",
                t.exit_reason or "",
                f"{t.pnl:.2f}" if t.pnl else "",
                f"{t.pnl_percent:.2f}" if t.pnl_percent else "",
                t.entry_time.strftime("%Y-%m-%d %H:%M") if t.entry_time else "",
                t.exit_time.strftime("%Y-%m-%d %H:%M") if t.exit_time else "",
                holding_bars,
                "是" if t.trailing_stop_activated else "否",
                t.market_state or ""
            ])
    
    print(f"\n✅ 交易记录已导出到: {filename}")


# ============================================================================
# 主函数
# ============================================================================

async def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="BrooksQuant 回测系统")
    parser.add_argument("--symbol", type=str, default="BTCUSDT", help="交易对")
    parser.add_argument("--interval", type=str, default="5m", help="K线周期")
    parser.add_argument("--start", type=str, default="2024-01-01", help="开始日期")
    parser.add_argument("--end", type=str, default="2024-12-31", help="结束日期")
    parser.add_argument("--capital", type=float, default=1000.0, help="初始资金")
    parser.add_argument("--leverage", type=int, default=20, help="杠杆倍数")
    parser.add_argument("--sl-atr", type=float, default=2.0, help="止损ATR乘数（默认2.0）")
    parser.add_argument("--export", action="store_true", help="导出交易记录到CSV")
    parser.add_argument("--plot", action="store_true", help="生成权益曲线图")
    parser.add_argument("--verbose", action="store_true", help="详细日志")
    
    args = parser.parse_args()
    
    # 配置日志
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    
    # 创建配置
    config = BacktestConfig(
        symbol=args.symbol,
        interval=args.interval,
        start_date=args.start,
        end_date=args.end,
        initial_capital=args.capital,
        leverage=args.leverage,
        stop_loss_atr_multiplier=args.sl_atr
    )
    
    # 创建回测引擎
    engine = BacktestEngine(config)
    
    # 下载数据
    df = await engine.load_data()
    
    # 运行回测
    result = engine.run(df)
    
    # 打印报告
    print_report(result)
    
    # 导出 CSV
    if args.export:
        export_trades_csv(result)
    
    # 生成图表
    if args.plot:
        plot_equity_curve(result)


if __name__ == "__main__":
    asyncio.run(main())
