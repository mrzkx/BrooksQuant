"""
策略 H2/L2 状态机信号处理

职责（关注点分离）：
- 形态识别：通过 H2/L2 状态机检测回调信号
- 信号棒校验：验证 Signal Bar 质量
- Delta 基础过滤：基于订单流过滤明显逆势信号

不负责（由 strategy.py 统一处理）：
- HTF 过滤：是否允许 H2/L2 入场
- HTF 权重调节：信号强度加权
"""

import logging
from typing import Optional, Tuple, Callable

import pandas as pd

from .market_analyzer import MarketCycle
from .patterns import PatternDetector
from .signal_models import BarContext, SignalResult
from .state_machines import H2StateMachine, L2StateMachine


# DeltaSnapshot 类型由调用方传入，此处用 type hint 字符串避免循环依赖 delta_flow
def _noop_cooldown(_st: str, _side: str, _bar: int, _latest: bool) -> bool:
    return False


def _default_delta_modifier(_snapshot, _side: str, _pct: float) -> Tuple[float, str]:
    return (1.0, "")


class H2L2Processor:
    """
    H2/L2 状态机信号处理器（纯形态识别 + Delta 基础过滤）
    
    关注点分离：
    - 本类只负责形态识别和 Delta 过滤
    - HTF 过滤和权重调节由 strategy.py 统一处理
    """

    def __init__(
        self,
        pattern_detector: PatternDetector,
        check_signal_cooldown: Optional[Callable[[str, str, int, bool], bool]] = None,
        calculate_delta_modifier: Optional[Callable[..., Tuple[float, str]]] = None,
    ):
        self.pattern_detector = pattern_detector
        self._check_cooldown = check_signal_cooldown or _noop_cooldown
        self._calc_delta = calculate_delta_modifier or _default_delta_modifier

    def validate_h2l2_signal_bar(
        self, ctx: BarContext, data: pd.DataFrame, signal_side: str, row_index: int
    ) -> Tuple[bool, str]:
        """H2/L2 信号棒质量校验：TradingRange 时放宽参数。返回 (bar_valid, bar_reason)。"""
        # H2/L2 是趋势延续信号，不需要传递 signal_type（不检查反转棒影线要求）
        # 但仍传递 df 和 i 以启用相对大小和低重叠度检查
        if ctx.market_cycle == MarketCycle.TRADING_RANGE:
            return self.pattern_detector.validate_btc_signal_bar(
                data.iloc[row_index], signal_side, min_body_ratio=0.40, close_position_pct=0.35,
                df=data, i=row_index
            )
        return self.pattern_detector.validate_btc_signal_bar(
            data.iloc[row_index], signal_side, df=data, i=row_index
        )

    async def process_h2_signal(
        self,
        h2_machine: H2StateMachine,
        data: pd.DataFrame,
        ctx: BarContext,
        cached_delta_snapshot: Optional[object],
    ) -> Optional[SignalResult]:
        """
        处理 H2 状态机信号（纯形态识别 + Delta 基础过滤）
        
        注意：HTF 过滤由 strategy.py 在调用此方法前完成
        
        Args:
            h2_machine: H2 状态机
            data: K线数据
            ctx: 当前 K 线上下文
            cached_delta_snapshot: Delta 快照（可选）
        
        Returns:
            SignalResult 或 None
        """
        # ========== 形态识别：H2 状态机 ==========
        h2_signal = h2_machine.update(
            ctx.close, ctx.high, ctx.low, ctx.ema, ctx.atr, data, ctx.i,
            self.pattern_detector.calculate_unified_stop_loss,
            market_state=ctx.market_state,
        )
        if not h2_signal:
            return None
        
        # ========== 冷却期检查 ==========
        if self._check_cooldown(h2_signal.signal_type, h2_signal.side, ctx.i, ctx.is_latest_bar):
            return None
        
        # ========== 信号棒质量校验 ==========
        bar_valid, bar_reason = self.validate_h2l2_signal_bar(ctx, data, h2_signal.side, ctx.i)
        if not bar_valid:
            if ctx.is_latest_bar:
                logging.info(f"🚫 H2信号棒质量不合格: {h2_signal.signal_type} - {bar_reason}")
            return None
        
        # ========== Delta 基础过滤（订单流）==========
        delta_modifier = 1.0
        if cached_delta_snapshot is not None and getattr(cached_delta_snapshot, "trade_count", 0) > 0:
            delta_ratio = getattr(cached_delta_snapshot, "delta_ratio", 0.0)
            # 强烈反向：买入信号但 Delta < -0.3（强卖压）→ 阻止
            if delta_ratio < -0.3:
                if ctx.is_latest_bar:
                    logging.info(
                        f"🚫 H2 Delta强烈反向: {h2_signal.signal_type} - "
                        f"买入信号但Delta={delta_ratio:.2f}<-0.3，强卖压"
                    )
                return None
            # 轻微反向：Delta < 0 → 信号减弱
            elif delta_ratio < 0:
                delta_modifier = 0.7
                if ctx.is_latest_bar:
                    logging.info(
                        f"⚠️ H2 Delta轻微反向: {h2_signal.signal_type} - "
                        f"Delta={delta_ratio:.2f}，信号减弱"
                    )
            # 顺向：使用回调计算调节因子
            else:
                kline_open = data.iloc[ctx.i]["open"]
                price_change_pct = ((ctx.close - kline_open) / kline_open * 100) if kline_open > 0 else 0.0
                delta_modifier, delta_reason = self._calc_delta(
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
        
        # 返回纯形态信号（HTF 权重由 strategy.py 统一应用）
        # Al Brooks: Tight Channel 中 H1 标记为高风险
        return SignalResult(
            signal_type=h2_signal.signal_type,
            side=h2_signal.side,
            stop_loss=h2_signal.stop_loss,
            base_height=h2_signal.base_height,
            delta_modifier=delta_modifier,
            risk_reward=2.0,
            is_high_risk=h2_signal.is_high_risk,
        )

    async def process_l2_signal(
        self,
        l2_machine: L2StateMachine,
        data: pd.DataFrame,
        ctx: BarContext,
        cached_delta_snapshot: Optional[object],
    ) -> Optional[SignalResult]:
        """
        处理 L2 状态机信号（纯形态识别 + Delta 基础过滤）
        
        注意：HTF 过滤由 strategy.py 在调用此方法前完成
        
        Args:
            l2_machine: L2 状态机
            data: K线数据
            ctx: 当前 K 线上下文
            cached_delta_snapshot: Delta 快照（可选）
        
        Returns:
            SignalResult 或 None
        """
        # ========== 形态识别：L2 状态机 ==========
        l2_signal = l2_machine.update(
            ctx.close, ctx.high, ctx.low, ctx.ema, ctx.atr, data, ctx.i,
            self.pattern_detector.calculate_unified_stop_loss,
            market_state=ctx.market_state,
        )
        if not l2_signal:
            return None
        
        # ========== 冷却期检查 ==========
        if self._check_cooldown(l2_signal.signal_type, l2_signal.side, ctx.i, ctx.is_latest_bar):
            return None
        
        # ========== 信号棒质量校验 ==========
        bar_valid, bar_reason = self.validate_h2l2_signal_bar(ctx, data, l2_signal.side, ctx.i)
        if not bar_valid:
            if ctx.is_latest_bar:
                logging.info(f"🚫 L2信号棒质量不合格: {l2_signal.signal_type} - {bar_reason}")
            return None
        
        # ========== Delta 基础过滤（订单流）==========
        delta_modifier = 1.0
        if cached_delta_snapshot is not None and getattr(cached_delta_snapshot, "trade_count", 0) > 0:
            delta_ratio = getattr(cached_delta_snapshot, "delta_ratio", 0.0)
            # 强烈反向：卖出信号但 Delta > 0.3（强买压）→ 阻止
            if delta_ratio > 0.3:
                if ctx.is_latest_bar:
                    logging.info(
                        f"🚫 L2 Delta强烈反向: {l2_signal.signal_type} - "
                        f"卖出信号但Delta={delta_ratio:.2f}>0.3，强买压"
                    )
                return None
            # 轻微反向：Delta > 0 → 信号减弱
            elif delta_ratio > 0:
                delta_modifier = 0.7
                if ctx.is_latest_bar:
                    logging.info(
                        f"⚠️ L2 Delta轻微反向: {l2_signal.signal_type} - "
                        f"Delta={delta_ratio:.2f}，信号减弱"
                    )
            # 顺向：使用回调计算调节因子
            else:
                kline_open = data.iloc[ctx.i]["open"]
                price_change_pct = ((ctx.close - kline_open) / kline_open * 100) if kline_open > 0 else 0.0
                delta_modifier, delta_reason = self._calc_delta(
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
        
        # 返回纯形态信号（HTF 权重由 strategy.py 统一应用）
        # Al Brooks: Tight Channel 中 L1 标记为高风险
        return SignalResult(
            signal_type=l2_signal.signal_type,
            side=l2_signal.side,
            stop_loss=l2_signal.stop_loss,
            base_height=l2_signal.base_height,
            delta_modifier=delta_modifier,
            risk_reward=2.0,
            is_high_risk=l2_signal.is_high_risk,
        )
