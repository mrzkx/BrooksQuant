# BrooksQuant

基于 **Al Brooks 价格行为理论** 的加密货币量化交易系统。

## 📋 概述

BrooksQuant 是一个全自动化的量化交易系统，实现了 Al Brooks 价格行为交易策略，专注于 Binance 永续合约市场。系统支持观察模式（模拟交易）和实盘模式，具备完整的风险管理和订单流分析功能。

### 核心特性

- **Al Brooks 策略实现**：Spike、H2/L2、Failed Breakout、Wedge、Climax 等经典形态
- **实时订单流分析**：基于 aggTrade 数据的 Delta 分析，识别吸收、突破、流动性撤离
- **智能止盈止损**：分批止盈（TP1/TP2）、追踪止损、动态保本
- **市场状态识别**：自动识别趋势、通道、区间等市场模式
- **双模式运行**：观察模式（模拟）和实盘模式无缝切换
- **高可用架构**：自动重连、指数退避、数据持久化

## 🏗️ 系统架构

```
BrooksQuant/
├── main.py                 # 主入口（177行）
├── order_executor.py       # 订单执行逻辑（371行）
├── strategy.py             # 策略核心（524行）
├── trade_logger.py         # 交易日志/持仓管理（740行）
├── user_manager.py         # 用户/API管理（782行）
├── delta_flow.py           # 订单流分析（928行）
├── config.py               # 配置加载（411行）
│
├── workers/                # 异步工作者模块
│   ├── kline_producer.py   # K线数据流生产者
│   ├── user_worker.py      # 用户信号处理
│   ├── stats_worker.py     # 统计打印
│   └── helpers.py          # 辅助函数
│
├── logic/                  # 策略逻辑模块
│   ├── patterns.py         # 形态检测（Spike/Wedge/Climax等）
│   ├── state_machines.py   # H2/L2 状态机
│   └── market_analyzer.py  # 市场状态分析
│
├── .env                    # 环境变量配置
├── brooksquant.service     # Systemd 服务文件
└── requirements.txt        # Python 依赖
```

## 🚀 快速开始

### 环境要求

- Python 3.10+
- PostgreSQL 14+
- Redis 6+
- Ubuntu 20.04+ (推荐)

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

# 启用并启动服务
sudo systemctl daemon-reload
sudo systemctl enable brooksquant
sudo systemctl start brooksquant

# 查看状态
sudo systemctl status brooksquant
sudo journalctl -u brooksquant -f
```

## ⚙️ 配置说明

### 环境变量 (.env)

```bash
# ============ 交易参数 ============
SYMBOL=BTCUSDT              # 交易对
INTERVAL=5m                 # K线周期
OBSERVE_BALANCE=10000       # 观察模式模拟资金
POSITION_SIZE_PERCENT=20    # 仓位百分比
LEVERAGE=20                 # 杠杆倍数

# ============ 运行模式 ============
OBSERVE_MODE=true           # true=观察模式, false=实盘模式

# ============ 数据库 ============
DATABASE_URL=postgresql://user:pass@localhost:5432/brooksquant

# ============ Redis ============
REDIS_URL=redis://localhost:6379/0

# ============ Binance API ============
USER1_API_KEY=your_api_key
USER1_API_SECRET=your_api_secret
```

### 仓位管理规则

| 账户余额 | 仓位比例 | 杠杆 | 说明 |
|---------|---------|------|------|
| ≤ 1000 USDT | 100% | 20x | 小资金全仓模式 |
| > 1000 USDT | 20% | 20x | 大资金分散风险 |

## 📈 交易策略

### 信号类型

| 信号 | 类型 | 入场方式 | 说明 |
|------|------|---------|------|
| **Spike_Buy/Sell** | 突破型 | 市价单 | 强势突破，实体 > 1.8倍平均 |
| **H2_Buy/H2_Sell** | 回撤型 | 限价单 | 趋势回调第二次入场点 |
| **L2_Buy/L2_Sell** | 回撤型 | 限价单 | 反弹第二次入场点 |
| **Failed_Breakout** | 反转型 | 限价单 | 假突破反转 |
| **Wedge_Buy/Sell** | 反转型 | 限价单 | 楔形三推反转 |
| **Climax_Buy/Sell** | 反转型 | 市价单 | 高潮反转 |

### 止盈止损策略 (Al Brooks 理念)

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

### 市场状态过滤

| 市场模式 | 允许的信号 | 禁止的信号 |
|---------|-----------|-----------|
| **StrongTrend** | 顺势信号 | 所有反转信号 |
| **TightChannel** | 顺势信号 | 所有反转信号 |
| **Channel** | 全部 | 无 |
| **TradingRange** | 反转信号优先 | 无 |
| **Breakout** | Spike 优先 | 无 |

## 🔄 订单流分析 (Delta)

系统通过 Binance aggTrade WebSocket 实时分析订单流：

- **累计 Delta**：买方成交量 - 卖方成交量
- **Delta 比率**：多空力量对比
- **Delta 加速度**：动量变化率
- **吸收检测**：价格不动但 Delta 大幅变化
- **Climax 检测**：极端成交量 + 单边 Delta
- **流动性撤离**：价格变化但成交量萎缩

## 📊 数据库模型

### trades 表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | SERIAL | 主键 |
| user | VARCHAR | 用户名 |
| signal | VARCHAR | 信号类型 |
| side | VARCHAR | 方向 (buy/sell) |
| entry_price | FLOAT | 入场价格 |
| quantity | FLOAT | 数量 |
| stop_loss | FLOAT | 止损价 |
| take_profit | FLOAT | 止盈价 |
| tp1_price | FLOAT | 第一目标位 |
| tp2_price | FLOAT | 第二目标位 |
| exit_price | FLOAT | 出场价格 |
| exit_reason | VARCHAR | 出场原因 |
| pnl | FLOAT | 盈亏 (USDT) |
| status | VARCHAR | 状态 (open/partial/closed) |
| is_observe | BOOLEAN | 是否观察模式 |
| trailing_stop_price | FLOAT | 追踪止损价 |
| trailing_stop_activated | BOOLEAN | 追踪止损是否激活 |

## 🛡️ 风险控制

1. **冷却期机制**：止损后等待 3 根 K 线（15分钟）
2. **反手阈值**：新信号强度需超过当前信号的 1.2-1.5 倍
3. **动态仓位**：根据账户余额自动调整仓位比例
4. **止损保护**：所有止损使用市价单确保成交
5. **限价单超时**：60秒未成交自动取消

## 📝 日志说明

```bash
# 查看实时日志
sudo journalctl -u brooksquant -f

# 查看文件日志
tail -f /opt/BrooksQuant/logs/brooksquant_*.log

# 日志级别说明
[INFO]    # 正常操作信息
[WARNING] # 警告（如重连、超时）
[ERROR]   # 错误（需关注）
```

### 关键日志标识

| 标识 | 含义 |
|------|------|
| 🎯 | 触发交易信号 |
| ✅ | 操作成功 |
| ❌ | 操作失败 |
| 📊 | K线数据/统计 |
| 📈 | 追踪止损激活 |
| ⏳ | 冷却期 |
| 🔄 | 重连/恢复 |

## 🔧 常见问题

### Q: 如何切换到实盘模式？

```bash
# 方式1：修改 .env 文件
OBSERVE_MODE=false

# 方式2：修改 systemd 服务
sudo nano /etc/systemd/system/brooksquant.service
# 将 Environment=OBSERVE_MODE=true 改为 false
sudo systemctl daemon-reload
sudo systemctl restart brooksquant
```

### Q: 如何添加多用户？

在 `.env` 中添加：
```bash
USER2_API_KEY=your_second_api_key
USER2_API_SECRET=your_second_api_secret
```

### Q: WebSocket 频繁断开？

检查网络连接和 Binance API 状态。系统会自动重连，最多 10 次尝试。

### Q: 如何查看交易统计？

```bash
# 连接数据库查询
sudo -u postgres psql -d brooksquant

# 查看所有交易
SELECT * FROM trades ORDER BY created_at DESC LIMIT 20;

# 查看统计
SELECT 
  COUNT(*) as total,
  SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
  SUM(pnl) as total_pnl
FROM trades 
WHERE status = 'closed' AND is_observe = false;
```

## 📄 许可证

MIT License

## ⚠️ 免责声明

本软件仅供学习和研究目的。加密货币交易具有高风险，可能导致全部本金损失。使用本系统进行实盘交易的风险由用户自行承担。作者不对任何交易损失负责。

---

**BrooksQuant** - *Trade with Price Action, Not with Emotion*
