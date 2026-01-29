"""
BrooksQuant 配置管理模块

所有敏感配置通过环境变量或 .env 文件管理，不在代码中硬编码。

环境变量优先级：
1. 系统环境变量
2. .env 文件
3. 无默认值（强制配置）

必需的环境变量（生产环境强制）：
- REDIS_URL 或 (REDIS_HOST, REDIS_PORT)
- USER1_API_KEY, USER1_API_SECRET (Binance 凭证，观察模式可选)

说明：交易记录不再持久化到 PostgreSQL，持仓状态根据币安真实持仓恢复与更新。
"""

import logging
import os
from datetime import datetime
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlparse, urlunparse

from dotenv import load_dotenv
from binance import AsyncClient


# 加载 .env 文件
load_dotenv()


# ============================================================================
# 运行模式检测
# ============================================================================

# 观察模式：设置为 True 时只模拟交易，不实际下单
OBSERVE_MODE = os.getenv("OBSERVE_MODE", "true").lower() == "true"


# ============================================================================
# 环境变量验证
# ============================================================================

def _mask_url_password(url: str) -> str:
    """
    安全地脱敏 URL 中的密码
    
    例如: postgresql://user:secret@host:5432/db -> postgresql://user:***@host:5432/db
    """
    try:
        parsed = urlparse(url)
        if parsed.password:
            # 替换密码为 ***
            netloc = f"{parsed.username}:***@{parsed.hostname}"
            if parsed.port:
                netloc += f":{parsed.port}"
            masked = parsed._replace(netloc=netloc)
            return urlunparse(masked)
        return url
    except Exception:
        # 解析失败时，使用简单的 @ 分割
        return url.split("@")[-1] if "@" in url else url


# ============================================================================
# Redis 配置（生产环境强制）
# ============================================================================

def get_redis_url() -> str:
    """
    获取 Redis 连接 URL
    
    优先级：
    1. REDIS_URL 环境变量（完整连接字符串）
    2. 从独立环境变量组装：REDIS_HOST, REDIS_PORT, REDIS_PASSWORD, REDIS_DB
    
    生产环境强制配置：未配置时抛出异常，Delta 订单流分析依赖 Redis
    """
    redis_url = os.getenv("REDIS_URL")
    
    if redis_url:
        safe_url = _mask_url_password(redis_url)
        logging.info(f"Redis 配置: {safe_url}")
        return redis_url
    
    # 从独立环境变量组装
    redis_host = os.getenv("REDIS_HOST")
    redis_port = os.getenv("REDIS_PORT", "6379")
    redis_db = os.getenv("REDIS_DB", "0")
    redis_password = os.getenv("REDIS_PASSWORD")
    
    if redis_host:
        if redis_password:
            redis_url = f"redis://:{redis_password}@{redis_host}:{redis_port}/{redis_db}"
        else:
            redis_url = f"redis://{redis_host}:{redis_port}/{redis_db}"
        
        logging.info(f"Redis 配置: {redis_host}:{redis_port}/{redis_db}")
        return redis_url
    
    # 生产环境强制配置：未配置 Redis 时抛出异常
    error_msg = (
        "❌ Redis 未配置！生产环境强制要求配置 Redis。\n"
        "Delta 订单流分析功能依赖 Redis 进行数据缓存。\n"
        "请设置以下环境变量之一：\n"
        "  方式1: REDIS_URL=redis://host:6379/0\n"
        "  方式2: REDIS_HOST (可选: REDIS_PORT, REDIS_DB, REDIS_PASSWORD)"
    )
    logging.critical(error_msg)
    raise RuntimeError(error_msg)


# ============================================================================
# 导出配置常量
# ============================================================================

REDIS_URL = get_redis_url()


# ============================================================================
# 交易参数配置
# ============================================================================

def get_trading_config() -> dict:
    """
    获取交易参数配置
    
    环境变量：
    - OBSERVE_BALANCE: 观察模式的模拟总资金（USDT），默认 10000
    - POSITION_SIZE_PERCENT: 开仓使用资金百分比（小资金时），默认 100（即 100%）
    - LEVERAGE: 杠杆倍数，默认 20
    - SYMBOL: 交易对，默认 BTCUSDT
    - INTERVAL: K线周期，默认 5m
    - TICK_SIZE: 合约最小价格步长（用于动态止损 SignalBar±TickSize），默认 0.01
    - USE_SIGNAL_BAR_ONLY_STOP: 是否使用纯信号棒止损（true=SignalBar极值±TickSize），默认 true
    - TRADER_EQUATION_ENABLED: 是否启用交易者方程过滤（WinRate×Reward>Risk），默认 true
    - TRADER_EQUATION_WIN_RATE: 交易者方程默认胜率（0~1），默认 0.4
    - VOLUME_BREAKOUT_CONFIRM_ENABLED: 是否启用成交量确认突破（有效突破类信号需放量），默认 false
    - VOLUME_BREAKOUT_MULTIPLIER: 成交量确认倍数（当根或近期量 > 均量×此值），默认 1.2
    - OPEN_INTEREST_ENABLED: 是否启用持仓量确认（需数据源支持，当前为预留），默认 false
    - ORDER_PRICE_OFFSET_PCT: 追价限价单偏移百分比（买 Ask+偏移/卖 Bid-偏移），默认 0.05
    - ORDER_PRICE_OFFSET_TICKS: 追价限价单偏移 tick 数（与 ORDER_PRICE_OFFSET_PCT 二选一），默认 0
    
    动态仓位配置（资金管理）：
    - LARGE_BALANCE_THRESHOLD: 大资金阈值（USDT），默认 1000
    - LARGE_BALANCE_POSITION_PCT: 大资金时的仓位百分比，默认 50（即 50%）
    
    规则：
    - 余额 <= LARGE_BALANCE_THRESHOLD: 使用 POSITION_SIZE_PERCENT
    - 余额 > LARGE_BALANCE_THRESHOLD: 使用 LARGE_BALANCE_POSITION_PCT
    """
    # 使用默认值避免空字符串导致的转换异常
    observe_balance_str = os.getenv("OBSERVE_BALANCE", "10000")
    position_size_str = os.getenv("POSITION_SIZE_PERCENT", "100")  # 小资金默认 100%
    leverage_str = os.getenv("LEVERAGE", "20")
    symbol_str = os.getenv("SYMBOL", "BTCUSDT")
    interval_str = os.getenv("INTERVAL", "5m")
    tick_size_str = os.getenv("TICK_SIZE", "0.01")
    use_signal_bar_only_stop_str = os.getenv("USE_SIGNAL_BAR_ONLY_STOP", "true")
    trader_equation_enabled_str = os.getenv("TRADER_EQUATION_ENABLED", "true")
    trader_equation_win_rate_str = os.getenv("TRADER_EQUATION_WIN_RATE", "0.4")
    volume_breakout_confirm_str = os.getenv("VOLUME_BREAKOUT_CONFIRM_ENABLED", "false")
    volume_breakout_multiplier_str = os.getenv("VOLUME_BREAKOUT_MULTIPLIER", "1.2")
    open_interest_enabled_str = os.getenv("OPEN_INTEREST_ENABLED", "false")
    order_price_offset_pct_str = os.getenv("ORDER_PRICE_OFFSET_PCT", "0.05")
    order_price_offset_ticks_str = os.getenv("ORDER_PRICE_OFFSET_TICKS", "0")
    
    # 动态仓位配置
    large_balance_threshold_str = os.getenv("LARGE_BALANCE_THRESHOLD", "1000")
    large_balance_position_pct_str = os.getenv("LARGE_BALANCE_POSITION_PCT", "50")
    
    try:
        observe_balance = float(observe_balance_str) if observe_balance_str else 10000.0
        position_size_percent = float(position_size_str) if position_size_str else 100.0
        leverage = int(leverage_str) if leverage_str else 20
        large_balance_threshold = float(large_balance_threshold_str) if large_balance_threshold_str else 1000.0
        large_balance_position_pct = float(large_balance_position_pct_str) if large_balance_position_pct_str else 50.0
        tick_size = float(tick_size_str) if tick_size_str else 0.01
        use_signal_bar_only_stop = use_signal_bar_only_stop_str.lower() in ("true", "1", "yes")
        trader_equation_enabled = trader_equation_enabled_str.lower() in ("true", "1", "yes")
        trader_equation_win_rate = float(trader_equation_win_rate_str) if trader_equation_win_rate_str else 0.4
        volume_breakout_confirm = volume_breakout_confirm_str.lower() in ("true", "1", "yes")
        volume_breakout_multiplier = float(volume_breakout_multiplier_str) if volume_breakout_multiplier_str else 1.2
        open_interest_enabled = open_interest_enabled_str.lower() in ("true", "1", "yes")
        order_price_offset_pct = float(order_price_offset_pct_str) if order_price_offset_pct_str else 0.0
        order_price_offset_ticks = int(order_price_offset_ticks_str) if order_price_offset_ticks_str else 0
    except ValueError as e:
        logging.error(f"交易参数配置格式错误: {e}")
        raise RuntimeError(f"交易参数配置格式错误: {e}")
    
    # 参数范围验证
    if observe_balance <= 0:
        raise RuntimeError(f"OBSERVE_BALANCE 必须大于 0，当前值: {observe_balance}")
    if not (0 < position_size_percent <= 100):
        raise RuntimeError(f"POSITION_SIZE_PERCENT 必须在 (0, 100] 范围内，当前值: {position_size_percent}")
    if not (1 <= leverage <= 125):
        raise RuntimeError(f"LEVERAGE 必须在 [1, 125] 范围内，当前值: {leverage}")
    if large_balance_threshold < 0:
        raise RuntimeError(f"LARGE_BALANCE_THRESHOLD 必须 >= 0，当前值: {large_balance_threshold}")
    if not (0 < large_balance_position_pct <= 100):
        raise RuntimeError(f"LARGE_BALANCE_POSITION_PCT 必须在 (0, 100] 范围内，当前值: {large_balance_position_pct}")
    if tick_size <= 0:
        raise RuntimeError(f"TICK_SIZE 必须大于 0，当前值: {tick_size}")
    if not (0 <= trader_equation_win_rate <= 1):
        raise RuntimeError(f"TRADER_EQUATION_WIN_RATE 必须在 [0, 1]，当前值: {trader_equation_win_rate}")
    
    config = {
        "observe_balance": observe_balance,
        "position_size_percent": position_size_percent,  # 小资金仓位
        "leverage": leverage,
        "symbol": symbol_str or "BTCUSDT",
        "interval": interval_str or "5m",
        "tick_size": tick_size,
        "use_signal_bar_only_stop": use_signal_bar_only_stop,
        "trader_equation_enabled": trader_equation_enabled,
        "trader_equation_win_rate": trader_equation_win_rate,
        "volume_breakout_confirm_enabled": volume_breakout_confirm,
        "volume_breakout_multiplier": volume_breakout_multiplier,
        "open_interest_enabled": open_interest_enabled,
        "order_price_offset_pct": order_price_offset_pct,
        "order_price_offset_ticks": order_price_offset_ticks,
        "large_balance_threshold": large_balance_threshold,
        "large_balance_position_pct": large_balance_position_pct,
    }
    
    logging.info(
        f"交易参数: 模拟资金={config['observe_balance']} USDT, "
        f"小资金仓位={config['position_size_percent']}%, "
        f"大资金阈值={config['large_balance_threshold']} USDT, "
        f"大资金仓位={config['large_balance_position_pct']}%, "
        f"杠杆={config['leverage']}x, "
        f"交易对={config['symbol']}, K线周期={config['interval']}, "
        f"止损模式={'信号棒极值+TickSize' if config['use_signal_bar_only_stop'] else '两棒+ATR'}, tick_size={config['tick_size']}"
    )
    
    return config


# 导出交易配置
TRADING_CONFIG = get_trading_config()

# 便捷常量
OBSERVE_BALANCE = TRADING_CONFIG["observe_balance"]
POSITION_SIZE_PERCENT = TRADING_CONFIG["position_size_percent"]
LEVERAGE = TRADING_CONFIG["leverage"]
SYMBOL = TRADING_CONFIG["symbol"]
KLINE_INTERVAL = TRADING_CONFIG["interval"]
TICK_SIZE = TRADING_CONFIG["tick_size"]
USE_SIGNAL_BAR_ONLY_STOP = TRADING_CONFIG["use_signal_bar_only_stop"]
TRADER_EQUATION_ENABLED = TRADING_CONFIG["trader_equation_enabled"]
TRADER_EQUATION_WIN_RATE = TRADING_CONFIG["trader_equation_win_rate"]
VOLUME_BREAKOUT_CONFIRM_ENABLED = TRADING_CONFIG["volume_breakout_confirm_enabled"]
VOLUME_BREAKOUT_MULTIPLIER = TRADING_CONFIG["volume_breakout_multiplier"]
OPEN_INTEREST_ENABLED = TRADING_CONFIG["open_interest_enabled"]
ORDER_PRICE_OFFSET_PCT = TRADING_CONFIG["order_price_offset_pct"]
ORDER_PRICE_OFFSET_TICKS = TRADING_CONFIG["order_price_offset_ticks"]
LARGE_BALANCE_THRESHOLD = TRADING_CONFIG["large_balance_threshold"]
LARGE_BALANCE_POSITION_PCT = TRADING_CONFIG["large_balance_position_pct"]

# ============================================================================
# 止盈止损执行方式（已改为程序执行，不挂委托）
# ============================================================================
# 止损、TP1、TP2 均由程序根据 K 线监控判断后市价平仓，不在交易所挂 STOP_MARKET/TAKE_PROFIT_MARKET。
# 以下配置已废弃，仅保留兼容（不再使用）：
# HARD_STOP_BUFFER_PCT = 硬止损缓冲百分比（原用于挂硬止损单）
HARD_STOP_BUFFER_PCT = float(os.getenv("HARD_STOP_BUFFER_PCT", "0.15")) / 100


# ============================================================================
# 日志配置
# ============================================================================

def _get_log_level() -> int:
    """从环境变量获取日志级别"""
    level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    return getattr(logging, level_str, logging.INFO)


def setup_logging() -> str:
    """
    配置日志系统
    
    特性：
    - 同时输出到控制台和文件
    - 按日期自动生成日志文件
    - 支持通过 LOG_LEVEL 环境变量调整级别
    """
    log_level = _get_log_level()
    
    # 创建日志目录
    log_dir = os.getenv("LOG_DIR", "logs")
    os.makedirs(log_dir, exist_ok=True)
    
    # 日志文件名（按日期）
    log_file = os.path.join(log_dir, f"brooksquant_{datetime.now().strftime('%Y%m%d')}.log")
    
    # 日志格式
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"
    
    # 配置根日志记录器
    logger = logging.getLogger()
    logger.setLevel(log_level)
    logger.handlers.clear()
    
    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(logging.Formatter(log_format, date_format))
    logger.addHandler(console_handler)
    
    # 文件处理器
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(log_level)
    file_handler.setFormatter(logging.Formatter(log_format, date_format))
    logger.addHandler(file_handler)
    
    logging.info(f"日志系统已初始化: {log_file}")
    return log_file


# 初始化日志系统
setup_logging()


# ============================================================================
# Binance 用户凭证
# ============================================================================

@dataclass
class UserCredentials:
    """用户 API 凭证"""
    api_key: str
    api_secret: str
    
    @property
    def is_valid(self) -> bool:
        """检查凭证是否有效"""
        return bool(self.api_key and self.api_secret)


def load_user_credentials() -> List[UserCredentials]:
    """
    从环境变量加载用户凭证
    
    支持的环境变量：
    - USER1_API_KEY, USER1_API_SECRET
    - USER2_API_KEY, USER2_API_SECRET
    """
    creds = []
    
    for idx in (1, 2):
        key = os.getenv(f"USER{idx}_API_KEY", "").strip()
        secret = os.getenv(f"USER{idx}_API_SECRET", "").strip()
        
        cred = UserCredentials(api_key=key, api_secret=secret)
        
        if not cred.is_valid:
            logging.warning(f"USER{idx} 凭证未配置或不完整，跳过该用户")
            continue  # 跳过无效凭证，不添加到列表
        
        creds.append(cred)
    
    return creds


async def create_async_client_for_user(user: UserCredentials) -> AsyncClient:
    """为用户创建 Binance 异步客户端"""
    if not user.is_valid:
        raise ValueError("API 凭证无效，无法创建客户端")
    
    return await AsyncClient.create(
        api_key=user.api_key, 
        api_secret=user.api_secret
    )


# ============================================================================
# 配置验证（启动时执行）
# ============================================================================

def validate_config():
    """
    验证关键配置是否已设置
    
    生产环境强制要求：
    - Redis（已在 get_redis_url 中强制）
    - Binance API 凭证（非观察模式下强制）
    """
    warnings = []
    errors = []
    
    # 检查 Binance API 凭证
    creds = load_user_credentials()
    valid_creds = [c for c in creds if c.is_valid]
    
    if not valid_creds:
        if OBSERVE_MODE:
            warnings.append("无有效的 Binance API 凭证（观察模式下可选）")
        else:
            errors.append("实盘模式需要有效的 Binance API 凭证")
    
    # 打印警告
    if warnings:
        logging.warning("=" * 60)
        logging.warning("⚠️ 配置警告:")
        for w in warnings:
            logging.warning(f"  - {w}")
        logging.warning("=" * 60)
    
    # 如果有错误，抛出异常
    if errors:
        error_msg = "配置验证失败:\n" + "\n".join(f"  - {e}" for e in errors)
        logging.critical(error_msg)
        raise RuntimeError(error_msg)
    
    logging.info("✅ 配置验证通过")
    return True


# 启动时验证配置
validate_config()
