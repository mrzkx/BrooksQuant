"""
BrooksQuant 策略主控 — EA OnTick 等价物

每根新 K 线收盘后调用 on_new_bar(df)，返回 Optional[SignalResult]。
内部管理所有有状态追踪器（SwingTracker / HLCounter / MarketStateTracker / 各种 Filter）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from logic.constants import (
    SignalResult, SignalType, DIR_LONG, DIR_SHORT,
    EMA_PERIOD, ATR_PERIOD, is_spike_signal, signal_side,
    ENABLE_BREAKOUT_MODE, BREAKOUT_MODE_ATR_MULT,
)
from logic.indicators import compute_ema, compute_atr
from logic.swing_tracker import SwingTracker
from logic.hl_counter import HLCounter
from logic.market_state import MarketStateTracker
from logic.filters import (
    BarbWireFilter, GapBar20Rule, HTFFilter, SpreadFilter,
    SignalCooldownTracker, MeasuringGapTracker, BreakoutModeTracker,
)
from logic.signals import SignalContext
from logic.scan_market import scan_market
from logic.stop_loss import calculate_unified_stop_loss
from logic.take_profit import get_scalp_tp1, get_measured_move_tp2

logger = logging.getLogger(__name__)


@dataclass
class BrooksStrategy:
    """
    有状态策略。kline_producer 每收到新 K 线收盘后调用 on_new_bar(df)。
    """
    ema_period: int = EMA_PERIOD
    atr_period: int = ATR_PERIOD

    # 状态追踪器
    swings: SwingTracker = field(default_factory=SwingTracker)
    hl: HLCounter = field(default_factory=HLCounter)
    mstate: MarketStateTracker = field(default_factory=MarketStateTracker)
    barb_wire: BarbWireFilter = field(default_factory=BarbWireFilter)
    gap20: GapBar20Rule = field(default_factory=GapBar20Rule)
    htf: HTFFilter = field(default_factory=HTFFilter)
    spread_filter: SpreadFilter = field(default_factory=SpreadFilter)
    cooldown: SignalCooldownTracker = field(default_factory=SignalCooldownTracker)
    measuring_gap: MeasuringGapTracker = field(default_factory=MeasuringGapTracker)
    breakout_mode: BreakoutModeTracker = field(default_factory=BreakoutModeTracker)

    # 趋势线 & 突破追踪（简化版）
    trend_line_broken: bool = False
    trend_line_price: float = 0.0
    trend_line_break_price: float = 0.0
    recent_breakout: bool = False
    breakout_dir: str = ""
    breakout_level: float = 0.0
    breakout_bar_age: int = 0

    _bar_count: int = 0

    # ── 主入口 ─────────────────────────────────────────────────────

    def on_new_bar(self, df: pd.DataFrame) -> Optional[SignalResult]:
        """
        新 K 线收盘后调用。df 需包含列 open/high/low/close。
        返回 SignalResult（含 tp1/tp2）或 None。
        """
        n = len(df)
        if n < 30:
            return None

        highs = df["high"]
        lows = df["low"]
        opens = df["open"]
        closes = df["close"]

        ema = compute_ema(closes, self.ema_period)
        atr_s = compute_atr(highs, lows, closes, self.atr_period)
        atr_val = float(atr_s.iloc[-2]) if len(atr_s) >= 2 else 0.0
        if atr_val <= 0:
            return None

        self._bar_count += 1
        self.cooldown.tick()

        # 1. 更新追踪系统
        self.swings.update(highs, lows)
        self.hl.update(highs, lows, opens, closes, atr_val, self.swings)
        self.mstate.update(highs, lows, opens, closes, ema, atr_val, self.swings)
        self.gap20.calculate_gap_count(closes, lows, highs, ema, atr_val)
        self.gap20.update(closes, highs, lows, opens, ema, atr_val)
        self.barb_wire.update(highs, lows, opens, closes, atr_val)
        self.measuring_gap.update(highs, lows, opens, closes, atr_val)
        self._update_trend_line(atr_val)
        self._update_breakout_pullback_tracking()

        # BarbWire 突破 → Breakout Mode
        if self.barb_wire.breakout_direction and ENABLE_BREAKOUT_MODE:
            bd = self.barb_wire.breakout_direction
            self.breakout_mode.activate(
                bd, float(closes.iloc[-2]),
                float(highs.iloc[-2]) if bd == "up" else float(lows.iloc[-2]),
            )
        self.breakout_mode.tick(highs, lows, atr_val)

        # 2. BarbWire 活跃 → 跳过信号
        if self.barb_wire.active:
            return None

        # 3. 构建上下文
        ctx = SignalContext(
            swings=self.swings,
            hl=self.hl,
            mstate=self.mstate,
            cooldown=self.cooldown,
            gap20=self.gap20,
            htf=self.htf,
            trend_line_broken=self.trend_line_broken,
            trend_line_price=self.trend_line_price,
            trend_line_break_price=self.trend_line_break_price,
            recent_breakout=self.recent_breakout,
            breakout_dir=self.breakout_dir,
            breakout_level=self.breakout_level,
            breakout_bar_age=self.breakout_bar_age,
        )

        is_ttr = self.mstate.is_ttr(highs, lows, atr_val)

        # 4. 扫描信号 — EA 先多后空
        result: Optional[SignalResult] = None
        for direction in (DIR_LONG, DIR_SHORT):
            r = scan_market(direction, highs, lows, opens, closes, atr_val, is_ttr, ctx)
            if r is not None:
                result = r
                break

        # 同步 ctx 中可能被修改的突破追踪状态
        self.recent_breakout = ctx.recent_breakout
        self.breakout_dir = ctx.breakout_dir
        self.breakout_level = ctx.breakout_level
        self.breakout_bar_age = ctx.breakout_bar_age
        self.trend_line_broken = ctx.trend_line_broken

        if result is None:
            return None

        # 5. SpreadFilter
        if self.spread_filter.active:
            return None

        # 6. 计算 TP
        h1 = float(highs.iloc[-2])
        l1 = float(lows.iloc[-2])
        h2 = float(highs.iloc[-3]) if n >= 3 else h1
        l2 = float(lows.iloc[-3]) if n >= 3 else l1
        side = signal_side(result.signal_type)

        if result.stop_loss == 0:
            result.stop_loss = calculate_unified_stop_loss(
                side, atr_val, result.entry_price,
                self.mstate.state, self.swings,
                h1, l1, h2, l2,
            )
        if result.stop_loss == 0:
            return None

        result.tp1 = get_scalp_tp1(side, result.entry_price, result.stop_loss)
        result.tp2 = get_measured_move_tp2(
            side, result.entry_price, atr_val,
            h1, l1, h2, l2,
            self.mstate.state,
            self.mstate.tight_channel_dir,
            self.mstate.tight_channel_extreme,
        )

        # 非 Spike 信号: entry_price 改为信号棒极值（限价单挂单位）
        if not is_spike_signal(result.signal_type):
            if side == "buy":
                result.entry_price = h1
            else:
                result.entry_price = l1

        logger.info(
            f"信号: {result.signal_type.name} {side} "
            f"entry={result.entry_price:.2f} sl={result.stop_loss:.2f} "
            f"tp1={result.tp1:.2f} tp2={result.tp2:.2f} "
            f"state={self.mstate.state.value} AI={self.mstate.always_in.name}"
        )
        return result

    # ── 软止损检查（每根 K 线收盘调用）────────────────────────────

    def check_soft_stop(
        self, side: str, technical_sl: float,
        close: float, recent_closes: list[float] | None = None,
    ) -> bool:
        from logic.stop_loss import check_soft_stop
        return check_soft_stop(side, technical_sl, close, recent_closes)

    # ── 高潮退出检查 ─────────────────────────────────────────────

    def check_climax_exit(self, df: pd.DataFrame, side: str) -> bool:
        n = len(df)
        if n < 7 or self.mstate.state != MarketState.TIGHT_CHANNEL:
            return False
        if self.mstate.tight_channel_extreme <= 0:
            return False
        c1 = float(df["close"].iloc[-2])
        o1 = float(df["open"].iloc[-2])
        h1 = float(df["high"].iloc[-2])
        l1 = float(df["low"].iloc[-2])
        body = abs(c1 - o1)
        avg_body = sum(
            abs(float(df["close"].iloc[-1 - i]) - float(df["open"].iloc[-1 - i]))
            for i in range(2, 7)
        ) / 5.0
        if avg_body <= 0 or body < avg_body * 3.0:
            return False
        tc = self.mstate
        if side == "buy" and c1 > o1 and tc.tight_channel_dir == "up" and h1 >= tc.tight_channel_extreme:
            return True
        if side == "sell" and c1 < o1 and tc.tight_channel_dir == "down" and l1 <= tc.tight_channel_extreme:
            return True
        return False

    # ── HTF 更新 ──────────────────────────────────────────────────

    def update_htf(self, current_close: float, htf_ema: float, atr: float) -> None:
        self.htf.update(current_close, htf_ema, atr)

    def update_spread(self, spread: float) -> None:
        self.spread_filter.update(spread)

    # ── 内部 ──────────────────────────────────────────────────────

    def _update_trend_line(self, atr: float) -> None:
        if len(self.swings.swings) < 4 or atr <= 0:
            return
        ai = self.mstate.always_in
        td = self.mstate.trend_direction
        sp = self.swings.swings

        if ai == AlwaysIn.LONG or td == "up":
            lows = [(s.price, s.bar_index) for s in sp if not s.is_high][:2]
            if len(lows) >= 2 and lows[1][0] < lows[0][0] and lows[1][1] > lows[0][1]:
                sl_end, sl_start = lows[0], lows[1]
                if sl_start[1] != sl_end[1]:
                    slope = (sl_end[0] - sl_start[0]) / (sl_start[1] - sl_end[1])
                    tl_now = sl_end[0] + slope * (sl_end[1] - 1)
                    self.trend_line_price = tl_now
                    # 检测突破（使用最近 close）
                    if not self.trend_line_broken and tl_now > 0:
                        pass  # 简化：通过 swing 结构判断 MTR

        if ai == AlwaysIn.SHORT or td == "down":
            highs = [(s.price, s.bar_index) for s in sp if s.is_high][:2]
            if len(highs) >= 2 and highs[1][0] > highs[0][0] and highs[1][1] > highs[0][1]:
                pass

    def _update_breakout_pullback_tracking(self) -> None:
        if not self.recent_breakout:
            return
        self.breakout_bar_age += 1
        max_age = 10
        if self.mstate.state in (MarketState.STRONG_TREND, MarketState.BREAKOUT):
            max_age = 15
        elif self.mstate.state == MarketState.TRADING_RANGE:
            max_age = 8
        if self.breakout_bar_age > max_age:
            self.recent_breakout = False
            self.breakout_dir = ""
            self.breakout_level = 0.0
            self.breakout_bar_age = 0


# 需要导入 — 放在方法内以避免循环引用
from logic.constants import MarketState
from logic.constants import AlwaysIn
