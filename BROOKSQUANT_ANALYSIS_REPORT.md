# BrooksQuant 项目分析报告

**说明**：仅分析，未做任何代码修改。

---

## 1) 文件树结构

```
BrooksQuant/
├── .cursorrules
├── .env.example
├── .gitignore
├── brooksquant.service
├── config.py
├── delta_flow.py
├── journald-brooksquant.conf
├── main.py
├── order_executor.py
├── README.md
├── requirements.txt
├── strategy.py
├── trade_logger.py
├── user_manager.py
├── utils.py
├── logic/
│   ├── __init__.py
│   ├── htf_filter.py
│   ├── interval_params.py
│   ├── market_analyzer.py
│   ├── patterns.py
│   ├── state_machines.py
│   ├── talib_indicators.py
│   ├── talib_patterns.py
│   └── trader_equation.py
└── workers/
    ├── __init__.py
    ├── helpers.py
    ├── kline_producer.py
    ├── stats_worker.py
    └── user_worker.py
```

（未包含：`logs/`、`data/`、`.git/`、`venv/` 等非源码目录。）

---

## 2) 文件职责与行数一览

| 文件 | 行数 | 角色 | 优化建议 |
|------|------|------|----------|
| **根目录** | | | |
| `main.py` | 162 | 生产逻辑（入口） | 无 |
| `config.py` | 473 | 工具/配置 | ⚠️ >300 行，可拆分为 DB/Redis/交易/日志 配置子模块 |
| `strategy.py` | 1611 | 生产逻辑（策略核心） | ⚠️ 明显过长，建议按信号类型拆分为子模块（Spike/H2L2/Wedge/FB 等） |
| `order_executor.py` | 438 | 生产逻辑（下单与平仓） | ⚠️ >300 行，可拆出观察/实盘两套执行逻辑 |
| `trade_logger.py` | 1259 | 生产逻辑（交易记录与统计） | ⚠️ 明显过长，建议拆出 Trade 模型、统计查询、内存缓存 |
| `user_manager.py` | 1088 | 生产逻辑（用户与仓位） | ⚠️ 明显过长，可拆出仓位计算、订单/持仓查询、杠杆与模式 |
| `delta_flow.py` | 1015 | 生产逻辑（订单流 Delta） | ⚠️ 明显过长，可拆出 DeltaAnalyzer、DeltaSignalModifier、aggtrade_worker |
| `utils.py` | 24 | 工具 | 无 |
| `.cursorrules` | - | 实例/规范 | - |
| `.env.example` | - | 实例/配置 | - |
| `.gitignore` | - | 实例/配置 | - |
| `brooksquant.service` | - | 实例/部署 | - |
| `journald-brooksquant.conf` | - | 实例/部署 | - |
| `README.md` | - | 文档 | - |
| `requirements.txt` | - | 依赖 | - |
| **logic/** | | | |
| `logic/__init__.py` | 23 | 生产逻辑（包导出） | 无 |
| `logic/market_analyzer.py` | 553 | 生产逻辑（市场状态） | ⚠️ >300 行，可考虑拆出 StrongTrend/TightChannel 检测 |
| `logic/patterns.py` | 1088 | 生产逻辑（形态检测） | ⚠️ 明显过长，建议按形态拆为 Wedge/FB/Spike/Climax 等子模块 |
| `logic/htf_filter.py` | 467 | 生产逻辑（HTF 过滤） | ⚠️ >300 行，可接受；`htf_updater_worker` 未被引用 |
| `logic/interval_params.py` | 339 | 生产逻辑（周期参数） | ⚠️ >300 行，多为配置表，可接受 |
| `logic/state_machines.py` | 478 | 生产逻辑（H2/L2 状态机） | ⚠️ >300 行，可接受 |
| `logic/talib_indicators.py` | 208 | 生产逻辑（TA-Lib 指标） | 无 |
| `logic/talib_patterns.py` | 365 | 生产逻辑（TA-Lib 形态） | ⚠️ >300 行，可接受 |
| `logic/trader_equation.py` | 55 | 工具/策略（交易者方程） | 无 |
| **workers/** | | | |
| `workers/__init__.py` | 15 | 生产逻辑（包导出） | 无 |
| `workers/helpers.py` | 235 | 生产逻辑（K 线/仓位辅助） | 无 |
| `workers/kline_producer.py` | 424 | 生产逻辑（K 线与信号） | ⚠️ >300 行；存在未使用 import `os` |
| `workers/stats_worker.py` | 59 | 生产逻辑（定期统计） | 存在未使用 import `os` |
| `workers/user_worker.py` | 334 | 生产逻辑（用户信号执行） | ⚠️ >300 行；存在未使用 import `List` |

**角色说明**  
- **生产逻辑**：实盘/观察模式下的核心业务代码。  
- **工具**：通用工具函数或配置加载，被多处复用。  
- **实例/配置/文档**：部署、环境示例、文档、依赖列表。

---

## 3) 明显过长文件（行数 > 300）

| 文件 | 行数 |
|------|------|
| `strategy.py` | 1611 |
| `trade_logger.py` | 1259 |
| `user_manager.py` | 1088 |
| `logic/patterns.py` | 1088 |
| `delta_flow.py` | 1015 |
| `logic/market_analyzer.py` | 553 |
| `logic/state_machines.py` | 478 |
| `logic/htf_filter.py` | 467 |
| `config.py` | 473 |
| `order_executor.py` | 438 |
| `workers/kline_producer.py` | 424 |
| `logic/talib_patterns.py` | 365 |
| `logic/interval_params.py` | 339 |
| `workers/user_worker.py` | 334 |

共 **14** 个文件超过 300 行；其中 **5** 个超过 1000 行（strategy、trade_logger、user_manager、patterns、delta_flow）。

---

## 4) 未使用的 import 与未被引用的符号

### 4.1 未使用的 import

| 文件 | 未使用的 import |
|------|------------------|
| `workers/stats_worker.py` | `os` |
| `workers/user_worker.py` | `List`（来自 `typing`） |
| `workers/kline_producer.py` | `os` |

### 4.2 未被引用的函数 / 类 / 变量

| 文件 | 类型 | 符号 | 说明 |
|------|------|------|------|
| `delta_flow.py` | 函数 | `reset_delta_analyzer` | 仅定义，项目内无调用；可用于测试或手动重置 Delta 单例 |
| `logic/interval_params.py` | 变量 | `INTERVAL_PARAMS_DOC` | 文档字符串常量，未被引用；可保留作文档或移至 README |
| `logic/htf_filter.py` | 函数 | `htf_updater_worker` | 仅定义，未被调用；当前 HTF 在 kline_producer 内按 K 线周期更新，若需独立后台更新可接入此处 |

**说明**：`logic/__init__.py` 中的 `MarketState`、`MarketCycle`、`MarketAnalyzer`、`PatternDetector`、`HState`、`LState`、`H2StateMachine`、`L2StateMachine` 当前无 `from logic import ...` 的用法，外部均通过 `logic.market_analyzer`、`logic.patterns` 等子模块直接引用。它们作为包级公共 API 保留是合理的，不记为“未引用”。

---

## 5) 依赖关系（模块 import 关系）

箭头表示「被 import」：`A → B` = A 依赖 B。

### 5.1 顶层与根目录

```
main.py
  → config, strategy, trade_logger, user_manager
  → delta_flow (aggtrade_worker)
  → workers (kline_producer, user_worker, print_stats_periodically)

config.py
  → logging, os, datetime, dataclasses, typing, urllib.parse
  → dotenv, binance (AsyncClient)

strategy.py
  → json, logging, pandas, typing, dataclasses
  → redis.asyncio (aioredis)
  → logic.market_analyzer (MarketState, MarketCycle, MarketAnalyzer)
  → logic.patterns (PatternDetector)
  → logic.state_machines (H2StateMachine, L2StateMachine)
  → logic.interval_params (get_interval_params, IntervalParams)
  → logic.htf_filter (get_htf_filter, HTFFilter, HTFTrend)
  → logic.talib_patterns (get_talib_detector, calculate_talib_boost, TALibPatternDetector, TALIB_AVAILABLE)
  → logic.talib_indicators (compute_ema, compute_atr, compute_ema_adaptive)
  → logic.trader_equation (satisfies_trader_equation)
  → delta_flow (DeltaAnalyzer, DeltaSnapshot, DeltaTrend, DeltaSignalModifier, get_delta_analyzer, compute_wedge_buy_delta_boost)

order_executor.py
  → asyncio, logging, typing (Dict)
  → config (SYMBOL, ORDER_PRICE_OFFSET_PCT, ORDER_PRICE_OFFSET_TICKS)
  → logic.trader_equation (satisfies_trader_equation)
  → trade_logger, user_manager, utils (round_quantity_to_step_size)

trade_logger.py
  → logging, threading, datetime, typing, contextlib
  → sqlalchemy (create_engine, Column, Integer, String, Float, Boolean, DateTime, func, case; sessionmaker, declarative_base)
  → config (DATABASE_URL, SAVE_TRADES_TO_DB)

user_manager.py
  → asyncio, logging, decimal (Decimal), typing
  → utils (round_quantity_to_step_size)
  → binance (AsyncClient)
  → config (UserCredentials, create_async_client_for_user, LARGE_BALANCE_THRESHOLD, LARGE_BALANCE_POSITION_PCT, POSITION_SIZE_PERCENT, LEVERAGE)

utils.py
  → decimal (Decimal, ROUND_DOWN)

delta_flow.py
  → asyncio, logging, json, time, collections.deque, dataclasses, typing, enum
  → numpy, redis.asyncio, binance (AsyncClient, BinanceSocketManager), binance.exceptions (ReadLoopClosed)
  → 可选: websockets.exceptions (ConnectionClosed)
```

### 5.2 logic 包

```
logic/__init__.py
  → logic.market_analyzer (MarketState, MarketCycle, MarketAnalyzer)
  → logic.patterns (PatternDetector)
  → logic.state_machines (HState, LState, H2StateMachine, L2StateMachine)

logic/market_analyzer.py
  → logging, pandas, enum, typing
  → logic.interval_params (get_interval_params, IntervalParams)

logic/patterns.py
  → logging, pandas, typing
  → logic.market_analyzer (MarketState)
  → logic.interval_params (get_interval_params, IntervalParams)

logic/htf_filter.py
  → asyncio, logging, time, dataclasses, enum, typing
  → pandas, binance (AsyncClient)
  → logic.talib_indicators (compute_ema)

logic/interval_params.py
  → logging, dataclasses, typing

logic/state_machines.py
  → logging, pandas, enum, typing, dataclasses

logic/talib_indicators.py
  → logging, typing, numpy, pandas
  → 可选: talib

logic/talib_patterns.py
  → logging, dataclasses, enum, typing, numpy, pandas
  → 可选: talib

logic/trader_equation.py
  → typing (Optional)
  → config (延迟 import：TRADER_EQUATION_ENABLED, TRADER_EQUATION_WIN_RATE)
```

### 5.3 workers 包

```
workers/__init__.py
  → workers.kline_producer (kline_producer)
  → workers.user_worker (user_worker)
  → workers.stats_worker (print_stats_periodically)

workers/kline_producer.py
  → asyncio, logging, os, typing, pandas
  → binance (BinanceSocketManager, AsyncClient), binance.exceptions (ReadLoopClosed)
  → config (SYMBOL, OBSERVE_MODE)
  → strategy (AlBrooksStrategy), trade_logger (TradeLogger)
  → workers.helpers (load_historical_klines, fill_missing_klines)
  → logic.htf_filter (get_htf_filter)
  → 可选: websockets.exceptions (ConnectionClosed)

workers/helpers.py
  → logging, typing
  → binance (AsyncClient)
  → config (OBSERVE_BALANCE, POSITION_SIZE_PERCENT, LEVERAGE, SYMBOL, KLINE_INTERVAL, LARGE_BALANCE_THRESHOLD, LARGE_BALANCE_POSITION_PCT)

workers/stats_worker.py
  → asyncio, logging, os, typing (List)
  → config (OBSERVE_MODE)
  → trade_logger (TradeLogger), user_manager (TradingUser)

workers/user_worker.py
  → asyncio, logging, os, typing (Dict, List)
  → config (LEVERAGE, SYMBOL, OBSERVE_MODE)
  → trade_logger (TradeLogger), user_manager (TradingUser)
  → order_executor (execute_observe_order, execute_live_order, handle_close_request)
  → workers.helpers (calculate_order_quantity)
```

### 5.4 依赖小结（谁被谁用）

- **config**：被 main, order_executor, trade_logger, user_manager, workers.*, logic.trader_equation 使用。  
- **strategy**：被 main, workers.kline_producer 使用。  
- **trade_logger**：被 main, order_executor, user_manager, workers.kline_producer, workers.stats_worker, workers.user_worker 使用。  
- **user_manager**：被 main, order_executor, workers.stats_worker, workers.user_worker 使用。  
- **order_executor**：被 workers.user_worker 使用。  
- **utils**：被 order_executor, user_manager 使用。  
- **delta_flow**：被 main, strategy 使用。  
- **logic.***：主要被 strategy 使用；logic.htf_filter 另被 workers.kline_producer 使用。

---

## 6) 报告汇总

- **文件树**：见第 1 节。  
- **职责与行数**：见第 2 节表格（含角色与优化建议）。  
- **过长文件**：14 个 >300 行，5 个 >1000 行，见第 3 节。  
- **未使用 import**：3 处（`workers/stats_worker.py` 的 `os`，`workers/user_worker.py` 的 `List`，`workers/kline_producer.py` 的 `os`）。  
- **未引用符号**：`reset_delta_analyzer`、`INTERVAL_PARAMS_DOC`、`htf_updater_worker`，见第 4 节。  
- **依赖关系**：见第 5 节（根目录 → logic → workers，及小结）。

如需对某一文件或依赖做更细的拆分/重构方案，可以指定模块名继续细化。
