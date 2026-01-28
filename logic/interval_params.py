"""
K 线周期自适应参数配置

Al Brooks 价格行为理论在不同时间周期下的参数优化

核心原理：
1. 短周期（1m, 3m）：噪音多，需要更严格的过滤条件
2. 中周期（5m, 15m）：平衡点，标准参数
3. 长周期（30m, 1h+）：信号更可靠，可适当放宽条件

BTC 特性：
- 24/7 交易，无开盘缺口
- 波动率在不同时段差异大（亚洲时段 < 欧美时段）
- 长影线频繁，需要更严格的实体过滤
"""

import logging
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class IntervalParams:
    """
    单个周期的 Al Brooks 参数配置
    
    所有参数都经过 BTC 高波动特性优化
    """
    
    # ========== 信号棒质量参数 ==========
    min_body_ratio: float           # 最小实体占比（过滤长影线）
    close_position_pct: float       # 收盘价位置要求（顶部/底部 x%）
    
    # ========== 趋势检测参数 ==========
    slope_threshold_pct: float      # 强斜率阈值（Tight Channel 检测）
    strong_trend_threshold: float   # 强趋势得分阈值
    trend_lookback_bars: int        # 趋势检测回看 K 线数
    
    # ========== 信号冷却参数 ==========
    signal_cooldown_bars: int       # 同类型信号最小间隔 K 线数
    
    # ========== ATR 倍数参数 ==========
    atr_stop_min_mult: float        # 止损最小 ATR 倍数
    atr_stop_max_mult: float        # 止损最大 ATR 倍数
    atr_climax_mult: float          # Climax 检测 ATR 倍数
    atr_spike_filter_mult: float    # Spike 过滤 ATR 倍数（避免追涨）
    
    # ========== Wedge 检测参数 ==========
    wedge_min_total_span: int       # 楔形最小跨度（第1到第3推进）
    wedge_min_leg_span: int         # 楔形相邻推进最小间隔
    
    # ========== 盈亏比参数 ==========
    default_tp1_r: float            # 默认 TP1 风险倍数
    default_tp2_r: float            # 默认 TP2 风险倍数
    
    # ========== H2/L2 状态机参数 ==========
    ema_touch_tolerance: float      # EMA 触碰容差（%）


# ============================================================================
# Al Brooks + BTC 优化参数表
# ============================================================================

INTERVAL_PARAMS: Dict[str, IntervalParams] = {
    
    # ========== 1分钟：高噪音，最严格过滤 ==========
    "1m": IntervalParams(
        # 信号棒质量：极严格（1分钟噪音太多）
        min_body_ratio=0.70,            # 实体至少 70%（过滤大量假信号）
        close_position_pct=0.15,        # 收盘必须在顶部/底部 15%
        
        # 趋势检测：更敏感（短周期趋势变化快）
        slope_threshold_pct=0.003,      # 0.3%（10根=10分钟）
        strong_trend_threshold=0.55,    # 略高阈值
        trend_lookback_bars=8,          # 减少回看（1分钟历史太短意义不大）
        
        # 信号冷却：更长（避免过度交易）
        signal_cooldown_bars=10,        # 至少 10 分钟
        
        # ATR 倍数：更宽止损（1分钟波动噪音大）
        atr_stop_min_mult=2.0,          # 最小 2x ATR
        atr_stop_max_mult=4.0,          # 最大 4x ATR
        atr_climax_mult=3.0,            # Climax 需要更大的 ATR 倍数
        atr_spike_filter_mult=2.0,      # Spike 过滤更敏感
        
        # Wedge：缩短周期（1分钟形态更紧凑）
        wedge_min_total_span=6,
        wedge_min_leg_span=2,
        
        # 盈亏比：需要更高盈亏比弥补低胜率
        default_tp1_r=1.2,
        default_tp2_r=3.0,
        
        # EMA 容差
        ema_touch_tolerance=0.002,      # 0.2%
    ),
    
    # ========== 3分钟：中高噪音 ==========
    "3m": IntervalParams(
        min_body_ratio=0.65,
        close_position_pct=0.18,
        
        slope_threshold_pct=0.005,      # 0.5%（10根=30分钟）
        strong_trend_threshold=0.52,
        trend_lookback_bars=10,
        
        signal_cooldown_bars=7,         # 21 分钟
        
        atr_stop_min_mult=1.8,
        atr_stop_max_mult=3.5,
        atr_climax_mult=2.8,
        atr_spike_filter_mult=2.2,
        
        wedge_min_total_span=7,
        wedge_min_leg_span=2,
        
        default_tp1_r=1.0,
        default_tp2_r=2.5,
        
        ema_touch_tolerance=0.0015,
    ),
    
    # ========== 5分钟：标准周期（当前默认）==========
    # ⭐ 优化：放宽信号棒质量要求，增加信号频率
    "5m": IntervalParams(
        min_body_ratio=0.50,            # ⭐ 从 0.60 降到 0.50（Al Brooks 允许更多形态）
        close_position_pct=0.25,        # ⭐ 从 0.20 放宽到 0.25
        
        slope_threshold_pct=0.008,      # 0.8%（10根=50分钟）
        strong_trend_threshold=0.50,    # 当前值
        trend_lookback_bars=10,
        
        signal_cooldown_bars=3,         # ⭐ 从 5 降到 3（15 分钟，更灵活）
        
        atr_stop_min_mult=1.5,          # 当前值
        atr_stop_max_mult=3.0,          # 当前值
        atr_climax_mult=2.5,            # 当前值
        atr_spike_filter_mult=3.0,      # ⭐ 从 2.5 提高到 3.0（允许更大的突破）
        
        wedge_min_total_span=8,
        wedge_min_leg_span=2,
        
        default_tp1_r=0.8,
        default_tp2_r=2.0,
        
        ema_touch_tolerance=0.001,
    ),
    
    # ========== 15分钟：信号更可靠 ==========
    "15m": IntervalParams(
        min_body_ratio=0.55,            # 可适当放宽
        close_position_pct=0.22,        # 略放宽
        
        slope_threshold_pct=0.012,      # 1.2%（10根=2.5小时）
        strong_trend_threshold=0.48,
        trend_lookback_bars=12,
        
        signal_cooldown_bars=4,         # 1 小时
        
        atr_stop_min_mult=1.3,
        atr_stop_max_mult=2.8,
        atr_climax_mult=2.3,
        atr_spike_filter_mult=2.8,
        
        wedge_min_total_span=10,
        wedge_min_leg_span=3,
        
        default_tp1_r=0.8,
        default_tp2_r=2.0,
        
        ema_touch_tolerance=0.001,
    ),
    
    # ========== 30分钟：中长周期 ==========
    "30m": IntervalParams(
        min_body_ratio=0.52,
        close_position_pct=0.25,
        
        slope_threshold_pct=0.015,      # 1.5%（10根=5小时）
        strong_trend_threshold=0.45,
        trend_lookback_bars=15,
        
        signal_cooldown_bars=3,         # 1.5 小时
        
        atr_stop_min_mult=1.2,
        atr_stop_max_mult=2.5,
        atr_climax_mult=2.2,
        atr_spike_filter_mult=3.0,
        
        wedge_min_total_span=12,
        wedge_min_leg_span=3,
        
        default_tp1_r=0.8,
        default_tp2_r=2.0,
        
        ema_touch_tolerance=0.0008,
    ),
    
    # ========== 1小时：长周期，最可靠 ==========
    "1h": IntervalParams(
        min_body_ratio=0.50,            # 长周期信号更可靠，可放宽
        close_position_pct=0.28,
        
        slope_threshold_pct=0.020,      # 2%（10根=10小时）
        strong_trend_threshold=0.42,
        trend_lookback_bars=20,
        
        signal_cooldown_bars=2,         # 2 小时
        
        atr_stop_min_mult=1.0,
        atr_stop_max_mult=2.2,
        atr_climax_mult=2.0,
        atr_spike_filter_mult=3.5,
        
        wedge_min_total_span=15,
        wedge_min_leg_span=4,
        
        default_tp1_r=0.8,
        default_tp2_r=2.0,
        
        ema_touch_tolerance=0.0005,
    ),
    
    # ========== 4小时：日内波段 ==========
    "4h": IntervalParams(
        min_body_ratio=0.48,
        close_position_pct=0.30,
        
        slope_threshold_pct=0.030,      # 3%（10根=40小时）
        strong_trend_threshold=0.40,
        trend_lookback_bars=20,
        
        signal_cooldown_bars=2,         # 8 小时
        
        atr_stop_min_mult=0.8,
        atr_stop_max_mult=2.0,
        atr_climax_mult=1.8,
        atr_spike_filter_mult=4.0,
        
        wedge_min_total_span=15,
        wedge_min_leg_span=4,
        
        default_tp1_r=0.8,
        default_tp2_r=2.5,
        
        ema_touch_tolerance=0.0005,
    ),
    
    # ========== 日线：中长期趋势 ==========
    "1d": IntervalParams(
        min_body_ratio=0.45,
        close_position_pct=0.32,
        
        slope_threshold_pct=0.050,      # 5%（10根=10天）
        strong_trend_threshold=0.38,
        trend_lookback_bars=20,
        
        signal_cooldown_bars=1,         # 1 天
        
        atr_stop_min_mult=0.8,
        atr_stop_max_mult=1.8,
        atr_climax_mult=1.5,
        atr_spike_filter_mult=5.0,
        
        wedge_min_total_span=15,
        wedge_min_leg_span=5,
        
        default_tp1_r=1.0,
        default_tp2_r=3.0,
        
        ema_touch_tolerance=0.0005,
    ),
}


def get_interval_params(interval: str) -> IntervalParams:
    """
    获取指定 K 线周期的参数配置
    
    Args:
        interval: K 线周期（如 "1m", "5m", "15m", "1h"）
    
    Returns:
        IntervalParams: 该周期的参数配置
        
    如果周期不在预设列表中，返回 5m 的默认参数
    """
    params = INTERVAL_PARAMS.get(interval)
    
    if params is None:
        logging.warning(
            f"⚠️ 未知的 K 线周期 '{interval}'，使用 5m 默认参数"
        )
        return INTERVAL_PARAMS["5m"]
    
    return params


# ============================================================================
# 周期参数说明文档
# ============================================================================

INTERVAL_PARAMS_DOC = """
Al Brooks BTC 周期自适应参数说明
================================

核心原理：
- 短周期（1m-3m）：噪音多，假信号多 → 严格过滤，宽止损，高盈亏比
- 中周期（5m-15m）：平衡点 → 标准参数
- 长周期（30m-1d）：信号可靠 → 放宽过滤，窄止损，标准盈亏比

参数说明：

1. 信号棒质量
   - min_body_ratio: 实体/全长比例，过滤长影线假信号
   - close_position_pct: 收盘价必须在K线顶部/底部x%区域

2. 趋势检测
   - slope_threshold_pct: Tight Channel 斜率阈值
   - strong_trend_threshold: STRONG_TREND 得分阈值
   - trend_lookback_bars: 趋势检测回看K线数

3. 信号冷却
   - signal_cooldown_bars: 同类型信号最小间隔

4. ATR 倍数
   - atr_stop_min_mult: 止损最小距离
   - atr_stop_max_mult: 止损最大距离
   - atr_climax_mult: Climax 检测阈值
   - atr_spike_filter_mult: Spike 追涨过滤阈值

5. Wedge 检测
   - wedge_min_total_span: 楔形三推最小跨度
   - wedge_min_leg_span: 相邻推进最小间隔

6. 盈亏比
   - default_tp1_r: 第一止盈目标风险倍数
   - default_tp2_r: 第二止盈目标风险倍数
"""
