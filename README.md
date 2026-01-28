# BrooksQuant

基于 **Al Brooks 价格行为理论** 的 Binance 永续合约量化交易系统。

## 概述

BrooksQuant 是一个全自动化的量化交易系统，实现了 Al Brooks 价格行为交易策略。系统支持观察模式（模拟交易）和实盘模式，具备完整的风险管理、订单流分析和多时间框架过滤功能。

### 核心特性

- **Al Brooks 策略**：Spike、H2/L2、Failed Breakout、Wedge、Climax 等经典形态
- **订单流分析**：基于 aggTrade 的 Delta 分析，识别吸收、突破、流动性撤离
- **双止损保护**：软止损（程序监控）+ 硬止损（交易所挂单）双重保障
- **HTF 过滤器**：高时间框架趋势过滤，避免逆势交易
- **多周期自适应**：根据 K 线周期自动调整策略参数
- **智能止盈**：分批止盈（TP1/TP2）+ 追踪止损 + 动态保本
- **实际盈亏**：查询币安真实成交价和手续费，准确计算盈亏

## 系统架构

```
BrooksQuant/
├── main.py                 # 主入口，启动所有异步任务
├── strategy.py             # 策略核心，信号生成
├── order_executor.py       # 订单执行，处理开平仓请求
├── trade_logger.py         # 交易日志，数据库持久化
├── user_manager.py         # 用户管理，Binance API 封装
├── delta_flow.py           # 订单流分析（Delta）
├── config.py               # 配置管理
│
├── workers/                # 异步工作者
│   ├── kline_producer.py   # K线数据流生产者
│   ├── user_worker.py      # 用户信号处理
│   ├── stats_worker.py     # 统计打印
│   └── helpers.py          # 辅助函数
│
├── logic/                  # 策略逻辑
│   ├── patterns.py         # 形态检测（Spike/Wedge/Climax）
│   ├── state_machines.py   # H2/L2 状态机
│   ├── market_analyzer.py  # 市场状态分析
│   ├── htf_filter.py       # 高时间框架过滤器
│   ├── interval_params.py  # 周期自适应参数
│   ├── talib_indicators.py # TA-Lib 指标计算
│   └── talib_patterns.py   # TA-Lib 形态识别
│
├── .env                    # 环境变量配置
├── brooksquant.service     # Systemd 服务文件
└── requirements.txt        # Python 依赖
```

## 快速开始

### 环境要求

- Python 3.10+
- PostgreSQL 14+
- Redis 6+
- Ubuntu 20.04+

### 安装步骤

```bash
# 1. 克隆项目
git clone https://github.com/your-repo/BrooksQuant.git
cd BrooksQuant

# 2. 创建虚拟环境
python3 -m venv venv
source venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置环境变量
cp .env.example .env
# 编辑 .env 填入实际配置

# 5. 初始化数据库
sudo -u postgres createdb brooksquant
sudo -u postgres psql -d brooksquant -c "CREATE USER quantuser WITH PASSWORD 'your_password';"
sudo -u postgres psql -d brooksquant -c "GRANT ALL PRIVILEGES ON DATABASE brooksquant TO quantuser;"

# 6. 启动系统
python main.py
```

### Systemd 服务部署

```bash
# 复制服务文件
sudo cp brooksquant.service /etc/systemd/system/

# 启用并启动
sudo systemctl daemon-reload
sudo systemctl enable brooksquant
sudo systemctl start brooksquant

# 查看状态
sudo systemctl status brooksquant
sudo journalctl -u brooksquant -f
```

## 配置说明

### 环境变量 (.env)

```bash
# ============ 数据库 ============
DATABASE_URL=postgresql://user:pass@localhost:5432/brooksquant

# ============ Redis ============
REDIS_URL=redis://localhost:6379/0

# ============ Binance API ============
USER1_API_KEY=your_api_key
USER1_API_SECRET=your_api_secret
USER2_API_KEY=your_api_key_2        # 可选
USER2_API_SECRET=your_api_secret_2  # 可选

# ============ 运行模式 ============
OBSERVE_MODE=true                    # true=观察模式, false=实盘

# ============ 交易参数 ============
SYMBOL=BTCUSDT                       # 交易对
INTERVAL=5m                          # K线周期
LEVERAGE=20                          # 杠杆倍数
POSITION_SIZE_PERCENT=100            # 仓位百分比（小资金）
LARGE_BALANCE_THRESHOLD=1000         # 大资金阈值
LARGE_BALANCE_POSITION_PCT=50        # 大资金仓位百分比

# ============ 双止损 ============
HARD_STOP_BUFFER_PCT=0.15            # 硬止损缓冲（%）
```

### 仓位管理规则

| 账户余额 | 仓位比例 | 杠杆 | 说明 |
|---------|---------|------|------|
| ≤ 1000 USDT | 100% | 20x | 小资金全仓 |
| > 1000 USDT | 50% | 20x | 大资金分散风险 |

## 交易策略

### 信号类型

| 信号 | 类型 | 入场方式 | 说明 |
|------|------|---------|------|
| **Spike** | 突破型 | 市价单 | 强势突破，实体 > 1.8倍平均 |
| **H2/L2** | 回撤型 | 限价单 | 趋势回调第二次入场 |
| **Failed_Breakout** | 反转型 | 限价单 | 假突破反转 |
| **Wedge** | 反转型 | 限价单 | 楔形三推反转 |
| **Climax** | 反转型 | 市价单 | 高潮反转 |

### HTF 趋势过滤

系统使用 1 小时 EMA20 作为高时间框架过滤器：

| HTF 趋势 | 允许信号 | 禁止信号 |
|----------|---------|---------|
| Bullish（EMA 上升）| 买入信号 | 卖出信号 |
| Bearish（EMA 下降）| 卖出信号 | 买入信号 |
| Neutral（EMA 横盘）| 双向交易 | 无 |

### 市场状态过滤

| 市场模式 | 允许的信号 | 禁止的信号 |
|---------|-----------|-----------|
| **StrongTrend** | 顺势信号 | 所有反转信号 |
| **TightChannel** | 顺势信号 | 所有反转信号 |
| **Channel** | 全部 | 无 |
| **TradingRange** | 反转信号优先 | 无 |
| **Breakout** | Spike 优先 | 无 |

## 双止损策略

BrooksQuant 采用双重止损保护机制：

```
┌─────────────────────────────────────────────────────────┐
│                      做空示例                           │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  硬止损 (交易所挂单) ─────── 89,621.40  (+0.15% 缓冲)   │
│                              ▲                          │
│                              │  缓冲区                  │
│                              ▼                          │
│  软止损 (程序监控)   ─────── 89,487.00  (K线收盘价)    │
│                                                         │
│  入场价             ─────── 89,304.20                  │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

### 止损触发逻辑

1. **正常情况**：K 线收盘价触及软止损 → 程序执行平仓 → 取消硬止损挂单
2. **极端情况**：程序崩溃或网络断开 → 硬止损挂单自动触发保护

### 智能平仓检测

平仓前检查仓位是否已被硬止损平掉，避免重复下单：

```python
if await user.has_open_position(SYMBOL):
    # 仓位存在，执行软止损平仓
    await user.close_position_market(...)
else:
    # 仓位已被硬止损平掉，跳过平仓
    logging.info("仓位已被硬止损单平掉")
```

## 止盈止损策略

```
入场 ──────────────────────────────────────────────────────────►
  │
  │  TP1 (1R, 50%仓位)
  │  ├─ 触发后止损移至入场价（保本）
  │  │
  │  TP2 (Measured Move, 50%仓位)
  │  ├─ 或追踪止损退出
  │
  ▼
止损 (市价单确保执行)
```

**追踪止损规则：**
- 激活条件：盈利 ≥ 1R
- 追踪距离：0.5R
- 只向有利方向移动

## 盈亏计算

系统在平仓后查询币安实际成交记录，计算真实盈亏：

```
实际盈亏 = 价差盈亏 - 开仓手续费 - 平仓手续费

其中:
- 价差盈亏 = (入场价 - 出场价) × 数量  (做空)
- 手续费 = 成交金额 × 0.05% (Taker)
```

日志示例：
```
[User1] 成交详情: 均价=89486.00, 手续费=1.61 USDT
[User1] 更新实际盈亏: 出场价 89477.22 → 89486.00, 手续费=1.61, 实际盈亏=-8.15 USDT
```

## 多周期自适应参数

系统根据 K 线周期自动调整策略参数：

| 周期 | 实体比例要求 | 止损 ATR 倍数 | 信号冷却 | 说明 |
|------|-------------|--------------|---------|------|
| 1m | 70% | 2.0-4.0x | 10根 | 噪音多，严格过滤 |
| 5m | 50% | 1.5-3.0x | 3根 | 标准周期 |
| 15m | 55% | 1.3-2.8x | 4根 | 信号更可靠 |
| 1h | 50% | 1.0-2.2x | 2根 | 长周期最可靠 |

## 订单流分析 (Delta)

通过 Binance aggTrade WebSocket 实时分析订单流：

- **累计 Delta**：买方成交量 - 卖方成交量
- **Delta 比率**：多空力量对比
- **Delta 加速度**：动量变化率
- **吸收检测**：价格不动但 Delta 大幅变化
- **Climax 检测**：极端成交量 + 单边 Delta
- **流动性撤离**：价格变化但成交量萎缩

## 数据库模型

### trades 表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | SERIAL | 主键 |
| user | VARCHAR | 用户名 |
| signal | VARCHAR | 信号类型 |
| side | VARCHAR | 方向 (buy/sell) |
| entry_price | FLOAT | 入场价格 |
| quantity | FLOAT | 数量 |
| stop_loss | FLOAT | 软止损价 |
| hard_stop_loss | FLOAT | 硬止损价 |
| take_profit | FLOAT | 止盈价 |
| tp1_price | FLOAT | 第一目标位 |
| tp2_price | FLOAT | 第二目标位 |
| exit_price | FLOAT | 实际出场价 |
| exit_reason | VARCHAR | 出场原因 |
| pnl | FLOAT | 实际盈亏 (USDT) |
| status | VARCHAR | 状态 (open/partial/closed) |
| is_observe | BOOLEAN | 是否观察模式 |

## 风险控制

1. **冷却期机制**：止损后等待 3 根 K 线
2. **HTF 趋势过滤**：只做顺势交易
3. **双止损保护**：程序 + 交易所双重保障
4. **动态仓位**：根据账户余额自动调整
5. **限价单超时**：60 秒未成交自动取消

## 日志说明

```bash
# 查看实时日志
sudo journalctl -u brooksquant -f

# 查看文件日志
tail -f /opt/BrooksQuant/logs/brooksquant_*.log
```

### 关键日志标识

| 标识 | 含义 |
|------|------|
| 🎯 | 触发交易信号 |
| ✅ | 操作成功 |
| ❌ | 操作失败 |
| 📊 | K线数据/统计 |
| 🟢 | HTF 趋势更新 |
| 📈 | 追踪止损激活 |
| ⏳ | 冷却期 |
| 🔄 | 重连/恢复 |

## 常见问题

### 如何切换到实盘模式？

```bash
# 修改 systemd 服务
sudo systemctl edit brooksquant --force

# 添加以下内容
[Service]
Environment=OBSERVE_MODE=false

# 重启服务
sudo systemctl daemon-reload
sudo systemctl restart brooksquant
```

### 如何添加多用户？

在 `.env` 中添加：
```bash
USER2_API_KEY=your_second_api_key
USER2_API_SECRET=your_second_api_secret
```

### 如何查看交易统计？

```sql
-- 连接数据库
sudo -u postgres psql -d brooksquant

-- 查看所有交易
SELECT * FROM trades ORDER BY created_at DESC LIMIT 20;

-- 查看统计
SELECT 
  COUNT(*) as total,
  SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
  SUM(pnl) as total_pnl
FROM trades 
WHERE status = 'closed' AND is_observe = false;
```

## 许可证

MIT License

## 免责声明

本软件仅供学习和研究目的。加密货币交易具有高风险，可能导致全部本金损失。使用本系统进行实盘交易的风险由用户自行承担。作者不对任何交易损失负责。

---

**BrooksQuant** - *Trade with Price Action, Not with Emotion*
