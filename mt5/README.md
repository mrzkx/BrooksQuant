# BrooksQuant EA for MetaTrader 5

基于 Al Brooks 价格行为理论的智能交易系统，从 Python 版本移植到 MT5 平台。

## 功能特性

### 核心策略（与 Python 版本对齐）

1. **市场状态检测**
   - Strong Trend（强趋势）- 禁止逆势交易
   - Tight Channel（紧凑通道）- 禁止反转交易
   - Trading Range（交易区间）- BLSH 策略
   - Breakout（突破）- Always In 模式
   - Final Flag（终极旗形）- 高胜率反转

2. **信号类型**
   - **Spike**：强突破直接入场
   - **H2/L2**：二次回调/反弹入场（核心信号）
   - **Wedge**：楔形反转（三推形态）
   - **Climax**：极端反转
   - **MTR**：主要趋势反转
   - **Failed Breakout**：区间假突破
   - **Final Flag**：终极旗形反转

3. **风险管理**
   - 动态止损计算（基于 Signal Bar + Entry Bar）
   - TP1/TP2 分批止盈
   - 信号冷却期防止过度交易
   - 最大止损 ATR 倍数过滤

## 安装说明

### 方法一：直接复制文件

1. 找到您的 MT5 数据目录：
   - 在 MT5 中点击 **文件 → 打开数据文件夹**
   
2. 将 `BrooksQuant_EA.mq5` 复制到：
   ```
   MQL5/Experts/
   ```

3. 在 MT5 中编译 EA：
   - 打开 **MetaEditor**（按 F4）
   - 找到并打开 `BrooksQuant_EA.mq5`
   - 按 **F7** 编译

4. 将编译后的 EA 拖放到图表上

### 方法二：通过 MetaEditor

1. 在 MT5 中按 **F4** 打开 MetaEditor
2. **文件 → 新建 → Expert Advisor**
3. 粘贴代码并保存为 `BrooksQuant_EA.mq5`
4. 按 **F7** 编译

## 输入参数说明

### 基础设置
| 参数 | 默认值 | 说明 |
|------|--------|------|
| InpLotSize | 0.1 | 基础手数 |
| InpMagicNumber | 20260203 | EA 标识号 |
| InpMaxPositions | 1 | 最大持仓数 |
| InpEnableTrading | true | 启用实盘交易 |

### Al Brooks 参数
| 参数 | 默认值 | 说明 |
|------|--------|------|
| InpEMAPeriod | 20 | EMA 周期（趋势过滤器） |
| InpATRPeriod | 20 | ATR 周期（波动率） |
| InpLookbackPeriod | 20 | 回看周期 |

### 信号棒质量参数
| 参数 | 默认值 | 说明 |
|------|--------|------|
| InpMinBodyRatio | 0.50 | 最小实体占比 |
| InpClosePositionPct | 0.25 | 收盘位置要求 |

### 趋势检测参数
| 参数 | 默认值 | 说明 |
|------|--------|------|
| InpSlopeThreshold | 0.008 | 强斜率阈值 (0.8%) |
| InpStrongTrendScore | 0.50 | 强趋势得分阈值 |

### 信号控制
| 参数 | 默认值 | 说明 |
|------|--------|------|
| InpSignalCooldown | 3 | 信号冷却期（K线数） |
| InpEnableSpike | true | 启用 Spike 信号 |
| InpEnableH2L2 | true | 启用 H2/L2 信号 |
| InpEnableWedge | true | 启用 Wedge 信号 |
| InpEnableClimax | true | 启用 Climax 信号 |
| InpEnableMTR | true | 启用 MTR 信号 |
| InpEnableFailedBO | true | 启用 Failed Breakout |

### Context Bypass 应急入场
| 参数 | 默认值 | 说明 |
|------|--------|------|
| InpEnableSpikeMarket | true | 启用 Spike Market Entry |
| InpEnableMicroChH1 | true | 启用 Micro Channel H1 |
| InpGapCountThreshold | 3 | Micro Channel H1 GapCount 阈值 |
| InpHTFBypassGapCount | 5 | HTF过滤失效的 GapCount 阈值 |

### HTF 过滤
| 参数 | 默认值 | 说明 |
|------|--------|------|
| InpHTFTimeframe | PERIOD_H1 | HTF 周期 |
| InpHTFEMAPeriod | 20 | HTF EMA 周期 |
| InpEnableHTFFilter | true | 启用 HTF 过滤 |

### 风险管理
| 参数 | 默认值 | 说明 |
|------|--------|------|
| InpRiskRewardRatio | 2.0 | 风险回报比 |
| InpTP1RiskMultiple | 0.8 | TP1 风险倍数 |
| InpTP2RiskMultiple | 2.0 | TP2 风险倍数 |
| InpTP1ClosePercent | 50.0 | TP1 平仓比例 |
| InpMaxStopATRMult | 3.0 | 最大止损 ATR 倍数 |

## 信号优先级（严格执行）

按照 Al Brooks 价格行为理论，实现严格的信号优先级：

### 优先级 1: Context_Bypass（应急入场）

**1A. Spike_Market_Entry（SPIKE 周期市价入场）**
- 条件：`MarketCycle == SPIKE`
- 逻辑：只要当前是强趋势棒（实体 > 60%），立即在收盘时市价入场
- 无需等待确认棒

**1B. Micro_Channel_H1（Tight Channel 快速入场）**
- 条件：`MarketState == TIGHT_CHANNEL` 且 `GapCount >= 3`
- 逻辑：一旦价格突破前一棒高点（H1），立即入场
- **忽略 H2 状态机的阴线计数要求**

### 优先级 2: 标准 Spike
- 在非 SPIKE 周期的 Spike 检测

### 优先级 3: H2/L2 状态机
- 状态转换：`WAITING_FOR_PULLBACK -> IN_PULLBACK -> H1 -> H2`
- **重要**：在 StrongTrend 模式下，当 `GapCount >= 5` 时，HTF（1h EMA）的反向过滤**失效**

### 优先级 4: 反转信号（限制条件）
- **仅在 `TRADING_RANGE` 或 `FINAL_FLAG` 状态下激活**
- Climax - 极端反转
- Wedge - 楔形三推反转
- MTR - 主要趋势反转
- Failed Breakout - 区间假突破
- Final Flag - 终极旗形反转

## GapCount 与 HTF 过滤

### GapCount（EMA Gap 计数）
GapCount 表示连续远离 EMA 的 K 线数量：
- 向上 Gap：整根 K 线（包括低点）都在 EMA 上方
- 向下 Gap：整根 K 线（包括高点）都在 EMA 下方

**GapCount 阈值**：
| 阈值 | 意义 |
|------|------|
| >= 3 | 激活 Micro_Channel_H1（Tight Channel 快速入场）|
| >= 5 | HTF 反向过滤失效（Strong Trend 模式下）|

### HTF 过滤规则
默认使用 H1（1小时）周期的 EMA 作为 HTF 参考：

| 信号方向 | HTF 趋势 | 结果 |
|----------|----------|------|
| H2 Buy   | HTF = Down | **阻止**（除非 GapCount >= 5）|
| L2 Sell  | HTF = Up   | **阻止**（除非 GapCount >= 5）|
| 其他     | -          | 不受影响 |

**HTF 过滤失效条件**：
```
StrongTrend + GapCount >= 5 → HTF 过滤失效
```

这意味着在强劲趋势中，当价格持续远离 EMA（GapCount >= 5），即使 HTF 指向相反方向，H2/L2 信号仍会触发。

## 市场状态说明

### Strong Trend（强趋势）
- 连续 3+ 根同向 K 线
- 连续创新高/新低
- 价格持续远离 EMA
- **规则**：禁止逆势交易

### Tight Channel（紧凑通道）
- 所有 K 线在 EMA 一侧
- 方向高度一致
- 强斜率（> 0.8%）
- **规则**：禁止反转交易

### Trading Range（交易区间）
- EMA 穿越 6 次以上
- 价格震荡
- **规则**：高空低多 (BLSH)

### Final Flag（终极旗形）
- 从 Tight Channel 退出
- 价格仍远离 EMA（> 1%）
- 横盘 3-8 根 K 线
- **规则**：高胜率反转入场

## H2/L2 状态机

### H2 买入（上升趋势）
```
等待回调 → 回调中 → H1检测 → 等待H2 → 触发
           ↓         ↓          ↓
        价格<EMA   突破高点    再次回调后突破
```

### L2 卖出（下降趋势）
```
等待反弹 → 反弹中 → L1检测 → 等待L2 → 触发
           ↓         ↓          ↓
        价格>EMA   跌破低点    再次反弹后跌破
```

## 风险管理（CTrade 下单）

### 止损计算 (CalculateUnifiedStopLoss)

基于前两根 K 线极值 ± ATR Buffer：

```
买入止损 = min(SignalBar.Low, EntryBar.Low) - ATR_Buffer
卖出止损 = max(SignalBar.High, EntryBar.High) + ATR_Buffer

ATR_Buffer = max(0.3 × ATR, 0.2% × Price)
```

**硬性约束**：止损距离不得超过 3×ATR，否则信号被拒绝。

### 仓位管理

**TP1 触发条件**：当前 R-Multiple >= 0.8R

**TP1 操作**：
1. 平仓 50% 仓位
2. 将止损移动至保本位（入场价 + 10 点缓冲）

**TP2**：剩余仓位在 2.0R 时全部平仓

### 追踪止损

当已触发 TP1 且当前 R > 1.5R 时，启用追踪止损：
- 买入：追踪止损 = 当前价 - 0.5×ATR
- 卖出：追踪止损 = 当前价 + 0.5×ATR

### 动态订单类型分配（核心）

**Spike 模式（Urgency - 入场比价格更重要）**：
- 条件：`MarketCycle == SPIKE` 或 `Spike_Market_Entry`
- 订单类型：`ORDER_TYPE_BUY` / `ORDER_TYPE_SELL`（市价单）
- 原因：强势突破时，入场时机比成本更重要

**Pullback 模式（Value - 限价单抵消点差成本）**：
- 条件：`H2/L2` 或 `Micro_Channel_H1` 信号
- 订单类型：`ORDER_TYPE_BUY_LIMIT` / `ORDER_TYPE_SELL_LIMIT`（限价单）
- 限价位置：前一根 K 线的高点/低点（信号棒极值）
- 原因：回调入场时，使用限价单可以抵消点差成本

### 下单流程

使用 MQL5 `CTrade` 类进行所有交易操作：

```mql5
// 市价单（Spike 模式）
trade.Buy(InpLotSize, _Symbol, 0, stopLoss, tp2, comment);
trade.Sell(InpLotSize, _Symbol, 0, stopLoss, tp2, comment);

// 限价单（Pullback 模式）
trade.BuyLimit(InpLotSize, limitPrice, _Symbol, stopLoss, tp2, 
               ORDER_TIME_SPECIFIED, expiration, comment);
trade.SellLimit(InpLotSize, limitPrice, _Symbol, stopLoss, tp2,
                ORDER_TIME_SPECIFIED, expiration, comment);

// 部分平仓
trade.PositionClosePartial(ticket, closeVolume);

// 修改止损
trade.PositionModify(ticket, newSL, positionTP);
```

### 风险检查流程

```
1. 验证止损 > 0
2. 计算风险 = |入场价 - 止损|
3. 验证风险 > 0
4. 硬性约束：风险 <= 3×ATR
5. 检查 broker 最小止损距离
6. 下单
```

## 信息面板

EA 在图表左上角显示实时信息：
- 当前市场状态
- 趋势方向和强度
- H2/L2 状态机状态
- EMA 和 ATR 值
- 最后检测到的信号
- 当前持仓数量

## 黄金专用功能 (XAUUSD)

### 点差保护

当当前点差超过过去 20 根 K 线平均点差的 2 倍时，自动暂停 `Spike_Market_Entry`：

```
如果 当前点差 > 平均点差 × 2.0：
    ⛔ 阻止 Spike_Market_Entry 执行
    ✅ 其他信号类型不受影响
```

### 止损计算（包含点差）

止损 Buffer 包含当前实时点差：

```
SL_Buy = min(SignalBar.Low, EntryBar.Low) - (ATR × 0.5 + 实时点差)
SL_Sell = max(SignalBar.High, EntryBar.High) + (ATR × 0.5 + 实时点差)
```

### 时段权重

| 时段 | GMT 时间 | 优先信号 | 说明 |
|------|----------|----------|------|
| 美盘 | 14:00-22:00 | Spike | 波动大，突破多 |
| 亚盘 | 00:00-08:00 | TradingRange, FailedBO | 波动小，区间震荡 |
| 欧盘 | 08:00-14:00 | 均衡 | 两者兼顾 |

### 小数位数适配

自动识别黄金的小数位数（通常 2-3 位）：
- `g_SymbolDigits`：品种小数位数
- `g_SymbolPoint`：最小价格单位
- `g_SymbolTickSize`：Tick 大小

### 黄金参数设置

| 参数 | 默认值 | 说明 |
|------|--------|------|
| InpEnableSpreadFilter | true | 启用点差过滤 |
| InpMaxSpreadMult | 2.0 | 最大点差倍数 |
| InpSpreadLookback | 20 | 点差回看周期 |
| InpEnableSessionWeight | true | 启用时段权重 |
| InpUSSessionStart | 14 | 美盘开始 (GMT) |
| InpUSSessionEnd | 22 | 美盘结束 (GMT) |
| InpUseLimitOrders | true | H2/L2 使用限价单 |

## 推荐设置

### 外汇市场
- 周期：M15, M30, H1
- EMA：20
- 止损：2-3 ATR
- TP：1:2 风险回报

### 加密货币
- 周期：M5, M15
- EMA：20
- 止损：1.5-2.5 ATR
- TP：1:2-1:3 风险回报

### 指数期货
- 周期：M15, H1
- EMA：20
- 止损：2-3 ATR
- TP：1:2 风险回报

## 回测建议

1. **选择足够的历史数据**
   - 至少 6 个月以上
   - 包含不同市场状态

2. **使用 Tick 数据**
   - 选择"每个报价"模式
   - 获得更准确的结果

3. **优化参数**
   - EMA 周期：10-30
   - 止损倍数：1.5-3.0
   - 信号冷却：2-5

## 注意事项

1. **风险警告**
   - 所有交易都有风险
   - 过去表现不代表未来
   - 请使用合适的仓位

2. **建议做法**
   - 先在模拟账户测试
   - 充分回测
   - 监控实盘表现

3. **不建议做法**
   - 盲目信任信号
   - 使用过大仓位
   - 忽略市场状态

## 版本历史

### v2.0 (2026-02-03)
- 从 Python BrooksQuant 移植
- 完整实现所有信号类型
- 添加市场状态检测
- 添加 H2/L2 状态机
- 添加信息面板

## 技术支持

如有问题，请检查：
1. EA 是否正确编译
2. 交易是否已启用
3. 参数设置是否合理
4. 市场是否开放

## 许可证

本 EA 基于 Al Brooks 价格行为理论开发，仅供学习和研究使用。

---

*Al Brooks: "交易的艺术在于阅读 K 线，而不是预测未来。"*
