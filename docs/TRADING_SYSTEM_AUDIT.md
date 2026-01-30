# BrooksQuant 交易系统代码审计报告

> 审计日期: 2026-01-30  
> 审计范围: 信号触发、交易场景、止盈止损决策  
> 对照标准: Al Brooks Price Action 理论

---

## 1. 市场状态识别 (MarketAnalyzer)

### 1.1 状态分类

系统定义了 6 种市场状态（`market_analyzer.py` 第 28-35 行）：

| 状态 | 代码值 | Al Brooks 对应概念 | 允许的信号类型 |
|------|--------|-------------------|---------------|
| `STRONG_TREND` | 强趋势 | "Always In" | 仅顺势 Spike |
| `BREAKOUT` | 突破 | Spike 阶段 | Spike |
| `CHANNEL` | 通道 | 有序趋势 | H2/L2、Spike |
| `TRADING_RANGE` | 区间 | BLSH (Buy Low Sell High) | Failed Breakout、Wedge |
| `TIGHT_CHANNEL` | 紧通道 | 单边强趋势 | 禁止反转 |
| `FINAL_FLAG` | 终旗 | 趋势耗尽 | 高胜率反转 |

### 1.2 强趋势检测逻辑

```python
# market_analyzer.py 第 243-477 行
def _detect_strong_trend(df, i, ema):
    # 评分因子：
    # 1. 连续同向K线 ≥3: +0.25, ≥5: +0.50
    # 2. 连续创新高/低 ≥4: +0.20
    # 3. 持续远离EMA (8根以上): +0.15
    # 4. 5根K线涨跌幅 >0.8%: +0.15
    # 5. Gap检测（Bar Gap/Body Gap）: +0.25~0.40
    # 6. 回调幅度惩罚：回调>30%整体走势时减分
    
    if trend_strength >= 0.5:  # 阈值
        return MarketState.STRONG_TREND
```

**Al Brooks 符合度: ✅ 高**
- Gap 检测正确："A gap is the strongest form of urgency"
- 回调幅度惩罚正确："强趋势没有任何有意义的回调"

### 1.3 市场周期状态机

```python
# market_analyzer.py 第 212-241 行
class MarketCycle(Enum):
    SPIKE = "Spike"           # Always In，忽略小回调
    CHANNEL = "Channel"       # 趋势延续
    TRADING_RANGE = "TradingRange"  # BLSH
```

**关键逻辑**：SPIKE 周期后保持 2 根 K 线滞后，避免频繁切换。

---

## 2. 信号触发场景

### 2.1 Spike（强突破）

**触发条件**（`patterns.py` 第 462-603 行）：

```python
def detect_strong_spike(df, i, ema, atr, market_state):
    # 前置：市场状态必须是 BREAKOUT/CHANNEL/STRONG_TREND
    
    # Signal Bar (i-1) 条件：
    # 1. 实体占比 > 65%
    # 2. 突破过去10根K线的高点/低点
    
    # Entry Bar (i) 条件：
    # 1. 同向强K线，实体 > 50%
    # 2. 价格在EMA正确一侧
    
    # 入场模式决策：
    if ema_deviation > 1.5 * atr:
        entry_mode = "Limit_Entry"  # 限价入场
        limit_price = Signal_Bar 实体50%处
    else:
        entry_mode = "Market_Entry"  # 市价入场
```

**Al Brooks 符合度: ✅ 高**
- 双 K 线确认（Signal + Entry Bar）正确
- 限价入场回撤机制符合 "等待 Spike 回测" 原则

### 2.2 H2/L2（回调交易）

**状态机逻辑**（`state_machines.py` 第 115-358 行）：

```python
class H2StateMachine:
    # 状态流转：
    # WAITING_FOR_PULLBACK → IN_PULLBACK → H1_DETECTED → WAITING_FOR_H2
    
    # H2 触发条件：
    def update(close, high, low, ema, atr, ...):
        # 1. 价格在 EMA 上方（带 0.3% 容差）
        is_in_uptrend = close >= ema * (1 - 0.003)
        
        # 2. 状态 = WAITING_FOR_H2 且 high > h1_high
        if state == WAITING_FOR_H2 and high > self.h1_high:
            return H2Signal("H2_Buy", "buy", stop_loss, base_height)
```

**Al Brooks 符合度: ⚠️ 中等**
- ✅ H2 定义正确："第二次突破 H1 高点"
- ✅ EMA 容差正确："EMA 作为过滤器，非刚性边界"
- ⚠️ 缺少 "Counting Bars" 概念（应统计回调 K 线数量）

### 2.3 Failed Breakout（失败突破）

**触发条件**（`patterns.py` 第 776-881 行）：

```python
def detect_failed_breakout(df, i, ema, atr, market_state):
    # 前置：市场状态必须是 TRADING_RANGE
    
    # 创新高后反转：
    if current_high > max_lookback_high:
        # 防误判1：前2根已在高位 → 趋势延续，非假突破
        # 防误判2：前1根阳线收盘在高位 → 趋势可能延续
        
        if close < open:  # 阴线
            close_position = (high - close) / kline_range
            if close_position >= 0.6:  # 收盘远离高点
                return ("FailedBreakout_Sell", ...)
```

**Al Brooks 符合度: ✅ 高**
- 正确识别 "假突破反转"
- 添加了趋势延续过滤（防止追涨杀跌）

### 2.4 Wedge Reversal（楔形反转）

**触发条件**（`wedge_reversal.py`）：

```python
def detect_wedge_reversal_impl(...):
    # 1. 识别三推结构（三个递降高点或三个递升低点）
    # 2. 检测收敛：第三推与第一推的间距 < 第一推与第二推
    # 3. 检测动能递减：每次推进的 K 线数量递增
    
    # 信号棒验证：
    # - 实体占比 > 60%（宽松模式 40%）
    # - 收盘在有利位置 > 35%
```

**Al Brooks 符合度: ✅ 高**
- 三推结构识别正确
- 收敛 + 动能递减检测符合 "动能耗尽" 原则

### 2.5 Climax Reversal（高潮反转）

**触发条件**（`patterns.py` 第 605-722 行）：

```python
def detect_climax_reversal(df, i, ema, atr):
    # Climax 定义：前一根K线范围 > 2.5 * ATR
    if prev_range > atr * 2.5 and prev_close > prev_open:
        # 验证1：尾部影线 ≥ 15%（拒绝更高价格）
        # 验证2：前5-8根走势深度 > 2.0 * ATR
        # 验证3：至少5根在EMA同一侧（真正的超买/超卖）
        
        if close < open and close < prev_close:
            return ("Climax_Sell", ...)
```

**Al Brooks 符合度: ✅ 高**
- "拒绝影线" 检测正确
- 趋势持续性检测符合 "极端位置" 原则

### 2.6 MTR（主趋势反转）

**触发条件**（`patterns.py` 第 992+ 行）：

```python
def detect_mtr_reversal(df, i, ema, atr, market_state):
    # MTR 四阶段：
    # 1. 识别显著趋势（至少3个swing点）
    # 2. 突破 EMA（趋势线突破）
    # 3. 回测极值（Retest）- 容差 0.1%
    # 4. 强反转棒或 H2/L2
```

**Al Brooks 符合度: ⚠️ 中等**
- ✅ 四阶段识别正确
- ⚠️ 缺少 "双顶/双底" 结构验证
- ⚠️ 回测容差可能过严（0.1%）

### 2.7 Final Flag（终旗反转）

**触发条件**（`patterns.py` 第 691-777 行 + `signal_checks.py`）：

```python
def _detect_final_flag(df, i, ema):
    # 条件1：之前至少5根连续 TightChannel
    # 条件2：价格仍远离 EMA（> 1% = Climax 区域）
    # 条件3：当前处于横盘（旗形波幅 < TightChannel 的 50%）
    # 条件4：旗形内无强力K线（保持横盘特征）
```

**Al Brooks 符合度: ✅ 高**
- 正确识别 "趋势耗尽后的最后挣扎"
- 高胜率反转点标记正确

---

## 3. 信号过滤机制

### 3.1 强趋势禁止逆势交易

```python
# signal_checks.py 第 92-107 行
if ctx.market_state == MarketState.STRONG_TREND:
    if ctx.trend_direction == "up" and side == "sell":
        return None  # 禁止做空
```

**Al Brooks 符合度: ✅ 完全正确**
- "在强趋势中逆势交易是自杀行为"

### 3.2 SPIKE 周期禁止反转

```python
# signal_checks.py 第 132-134 行
if ctx.market_cycle == MarketCycle.SPIKE:
    return None  # Climax/Wedge/MTR 全部禁止
```

**Al Brooks 符合度: ✅ 完全正确**
- "Always In 阶段不做反转"

### 3.3 Delta 订单流过滤

```python
# delta_flow.py 第 80-103 行
class DeltaAnalyzer:
    # 检测：
    # - 强 Delta 确认（同向订单流）
    # - 吸收信号（大量成交无价格变化）
    # - Climax 买/卖（大量买入/卖出但价格不动）
    
    # 信号调节：
    # 顺势信号 + 强 Delta → modifier = 1.2（加强）
    # 逆势信号 + 强反向 Delta → modifier = 0.3（强烈减弱）
```

**Al Brooks 符合度: ⚠️ 扩展**
- Al Brooks 原理中不包含订单流分析
- 但作为现代量化扩展是合理的

### 3.4 HTF（高时间周期）过滤

```python
# htf_filter.py
# 1小时 EMA20 方向和斜率过滤：
# - 顺势信号在 HTF 顺势时加权
# - 逆势信号在 HTF 逆势时减权
```

**Al Brooks 符合度: ✅ 高**
- "多时间周期一致性" 原则

---

## 4. 止损决策

### 4.1 统一止损计算

```python
# patterns.py 第 309-372 行
def calculate_unified_stop_loss(df, i, side, entry_price, atr):
    signal_bar = df.iloc[i - 1]
    entry_bar = df.iloc[i]
    
    buffer = atr * 0.1  # ATR 缓冲
    
    if side == "buy":
        stop_loss = min(signal_bar["low"], entry_bar["low"]) - buffer
    else:
        stop_loss = max(signal_bar["high"], entry_bar["high"]) + buffer
    
    # High Risk Filter：止损距离 > 3×ATR 则放弃信号
    if stop_distance > atr * 3.0:
        return None
```

**Al Brooks 符合度: ✅ 高**
- "止损在 Signal Bar 极值外" 正确
- High Risk Filter 合理

### 4.2 Spike 专用止损

```python
# patterns.py 第 441-460 行
def _spike_stop_at_signal_bar_extreme(signal_high, signal_low, side):
    # 止损 = Signal Bar 极值外 0.1%
    if side == "buy":
        return signal_low * 0.999
    else:
        return signal_high * 1.001
```

**Al Brooks 符合度: ✅ 正确**
- Spike 止损在 Signal Bar 极值

---

## 5. 止盈决策

### 5.1 分批止盈架构

```python
# signal_tp.py 第 92-294 行
def calculate_tp1_tp2(params, entry_price, stop_loss, side, base_height, ...):
    risk = abs(entry_price - stop_loss)
    
    # Spike 信号：
    tp1 = entry_price + risk * 1.0  # 1R
    tp2 = entry_price + base_height  # Measured Move
    
    # Wedge/FailedBreakout 信号：
    tp1 = ema  # 均线回归
    tp2 = pattern_origin  # 形态起点
    
    # H2/L2 信号：
    # Channel 状态：tp1 = 1.2R
    # TradingRange 状态：tp1 = 0.8R
    tp2 = 2.0R
    
    # Climax 信号棒：
    if is_climax:
        tp1_close_ratio = 0.75  # 提高平仓比例
        tp2 = min(tp2, 1.5R)    # 限制 TP2
```

**Al Brooks 符合度: ⚠️ 中等**
- ✅ Measured Move 概念正确
- ✅ 均线回归作为 TP1 正确
- ⚠️ 固定 R 倍数过于量化（Al Brooks 更强调结构目标）

### 5.2 信号类型 R 倍数配置

| 信号类型 | TP1 R 倍数 | TP2 R 倍数 | Al Brooks 评估 |
|---------|-----------|-----------|---------------|
| Spike | 1.0 | 2.0 | ✅ 合理 |
| H2/L2 | 1.0 | 2.0 | ✅ 顺势可更激进 |
| Wedge | 0.8 | 2.0 | ✅ 反转保守 |
| MTR | 0.8 | 2.5 | ✅ 低胜率高盈亏比 |
| Climax | 1.2 | 2.0 | ⚠️ 应更保守 |

---

## 6. 持仓管理

### 6.1 TP1 后保本机制

```python
# trade_logger.py 第 654-700 行
if tp1_hit:
    close_qty = quantity * 0.5  # 平仓 50%
    
    # 止损移至入场价 + 手续费缓冲
    fee_buffer = entry_price * 0.001
    if side == "buy":
        breakeven_stop = entry_price + fee_buffer
    else:
        breakeven_stop = entry_price - fee_buffer
    
    trade.stop_loss = breakeven_stop
    trade.breakeven_moved = True
```

**Al Brooks 符合度: ✅ 高**
- "部分止盈后保本" 是标准做法

### 6.2 追踪止损

```python
# trade_logger.py 第 600-652 行
# 激活条件：盈利 ≥ 1R
if profit_in_r >= 1.0 and not ts_state["activated"]:
    ts_state["activated"] = True
    trailing_stop = entry_price ± (current_profit - 0.5R)

# 更新逻辑：只向有利方向移动
if side == "buy":
    new_ts = current_price - 0.5R
    if new_ts > ts_state["trailing_stop"]:
        ts_state["trailing_stop"] = new_ts
```

**Al Brooks 符合度: ⚠️ 中等**
- ✅ 追踪止损概念正确
- ⚠️ Al Brooks 更强调 "结构止损"（如 swing low/high）

---

## 7. 交易执行

### 7.1 入场订单

```python
# order_executor.py 第 204-224 行
# 追价限价单：订单簿最优价 + 偏移
limit_price = await user.get_limit_price_from_order_book(
    SYMBOL, side, offset_pct, offset_ticks
)
await user.create_limit_order(symbol, side, quantity, price=limit_price)

# 等待成交（超时 60 秒）
await user.wait_for_order_fill(symbol, order_id, timeout_seconds=60.0)
```

### 7.2 止损监控

```python
# 软止损：K 线收盘价检查（非盘中触碰）
# trade_logger.py 第 727-740 行
effective_stop = ts_state["trailing_stop"] if ts_state["activated"] else trade.stop_loss
stop_hit = (side == "buy" and current_price <= effective_stop)
```

---

## 8. Al Brooks 理论符合度总评

### 8.1 核心原则符合度

| 原则 | 系统实现 | 符合度 |
|------|---------|-------|
| Context is King | ✅ 6 种市场状态 + 3 种周期 | ✅ 高 |
| Signal Bar 验证 | ✅ 实体占比 + 收盘位置 | ✅ 高 |
| 强趋势禁逆势 | ✅ STRONG_TREND/SPIKE 过滤 | ✅ 高 |
| 结构止损 | ⚠️ Signal Bar 极值 | 中 |
| 结构止盈 | ⚠️ 部分使用 EMA/形态起点 | 中 |
| Measured Move | ✅ Spike base_height | ✅ 高 |
| 三推楔形 | ✅ 收敛 + 动能递减 | ✅ 高 |
| BLSH 区间交易 | ✅ TradingRange 失败突破 | ✅ 高 |

### 8.2 主要偏差

1. **固定 R 倍数止盈**：Al Brooks 更强调结构目标（如前一 swing、EMA），而非固定倍数
2. **止损缓冲过小**：当前 `0.1 * ATR` 对 BTC 波动性不足
3. **缺少 Counting Bars**：H2/L2 应统计回调 K 线数量
4. **MTR 回测容差严格**：0.1% 可能过于严格

### 8.3 合理扩展

1. **Delta 订单流**：现代量化合理扩展
2. **HTF 过滤**：多时间周期验证
3. **交易者方程**：Risk/Reward 量化检验

---

## 9. 优化建议

### 9.1 止损优化
```python
# 建议：最小止损距离 = max(0.3 * ATR, 0.5% * entry_price)
buffer = max(atr * 0.3, entry_price * 0.005)
```

### 9.2 止盈优化
```python
# 建议：优先使用结构目标
tp2_priority = [
    wedge_extreme,      # 1. 楔形极值
    swing_high_low,     # 2. 前一 swing
    measured_move,      # 3. Measured Move
    2.0 * risk          # 4. 回退
]
```

### 9.3 H2/L2 Counting Bars
```python
# 建议：添加回调 K 线计数
pullback_bars = i - pullback_start_idx
if pullback_bars < 3:
    return None  # 回调太短，不是有效 H2/L2
```

---

## 10. 总结

**系统整体符合 Al Brooks 价格行为理论约 80%**，主要体现在：

✅ **优势**：
- 市场状态分类完整
- 信号过滤逻辑严谨
- 三推楔形识别精确
- 强趋势禁逆势正确

⚠️ **改进空间**：
- 止损/止盈应更多使用结构目标
- 回调交易需要 Counting Bars
- BTC 高波动需要更宽止损

---

*文档生成：BrooksQuant 代码审计工具*  
*审计版本：v1.0*
