# Al Brooks 价格行为策略 - TradingView 版

本目录为 BrooksQuant 策略的 TradingView Pine Script v5 实现，便于在 TradingView 上回测与看图。

## 文件

- **AlBrooksStrategy.pine** — 主策略脚本（含入场、出场、市场状态与形态检测）

## 使用方法

1. 打开 [TradingView](https://www.tradingview.com)，进入任意品种的图表。
2. 底部点击「Pine 编辑器」→ 新建空白脚本。
3. 将 `AlBrooksStrategy.pine` 全部内容粘贴进去并保存。
4. 图表会加载策略：显示 EMA、多空信号三角、右上角市场状态表。
5. 在「策略测试器」中查看回测结果，在「属性」中调整参数。

## 策略逻辑概要（与 BrooksQuant 对应）

| 模块 | 说明 |
|------|------|
| **市场状态** | StrongTrend / Breakout / Channel / TradingRange / TightChannel；强趋势下禁止逆势。 |
| **Failed Breakout** | 仅在 TradingRange：创新高/新低后出现反向 K 线（阴线/阳线 + 收盘位置）则反向入场。 |
| **Strong Spike** | Breakout/Channel/StrongTrend：连续 2 根同向、实体 ≥ 2×10 根平均实体、突破 10 根高/低点；过滤 3×ATR 以上“高潮”棒。 |
| **Climax 反转** | 前一根 K 线波幅 > 2.5×ATR，当前根反转且带尾影线（上影/下影 ≥15%）；非强趋势下才触发。 |
| **Wedge 反转** | 三高/三低收敛（楔形），第三根疲软且收盘突破第三点；非强趋势下才触发。 |
| **H2/L2** | 上升趋势中回调再创新高 = H2 多；下降趋势中反弹再创新低 = L2 空；需满足信号棒质量（实体占比、收盘位置）。 |
| **信号冷却** | 同方向信号间隔至少 N 根 K 线（默认 5），可调。 |
| **止盈止损** | 止损：前两根 K 线极值 ± ATR 缓冲，并限制在 ATR 倍数区间内；TP1=风险×R1 平 50% 仓，TP2=风险×R2 平剩余。 |

## 与 Python 版的差异

- **无订单流/Delta**：TradingView 无逐笔 Delta，策略仅基于 OHLC + EMA/ATR。
- **无 TA-Lib 形态**：未做 candlestick 形态加成，仅保留 PA 条件。
- **无 HTF 过滤**：未使用 `request.security` 的高周期 EMA 过滤，可在脚本中自行加“更高周期趋势”过滤。
- **周期参数**：Pine 中为统一输入参数，未按 1m/5m/1h 自动切换；建议按所用周期在界面里微调（如冷却、斜率阈值、实体占比）。

## 主要输入参数

- **指标**：EMA 周期、ATR 周期、回看周期、短期回看。
- **信号**：冷却 K 线数、最小实体占比、收盘位置%、强趋势阈值、紧凑通道斜率阈值。
- **形态开关**：Failed Breakout / Spike / Climax / Wedge / H2-L2 可单独开关。
- **止盈止损**：TP1/TP2 风险倍数、止损最小/最大 ATR 倍数。

建议先在 5m/15m 回测，再根据品种与周期细调参数。
