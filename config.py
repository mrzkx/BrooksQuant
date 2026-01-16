"""
BrooksQuant 配置管理模块

所有敏感配置通过环境变量或 .env 文件管理，不在代码中硬编码。

环境变量优先级：
1. 系统环境变量
2. .env 文件
3. 无默认值（强制配置）

必需的环境变量：
- DATABASE_URL 或 (DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)
- REDIS_URL 或 (REDIS_HOST, REDIS_PORT)
- USER1_API_KEY, USER1_API_SECRET (Binance 凭证)
"""

import logging
import os
import sys
from datetime import datetime
from dataclasses import dataclass
from typing import List, Optional

from dotenv import load_dotenv
from binance import AsyncClient


# 加载 .env 文件
load_dotenv()


# ============================================================================
# 环境变量验证
# ============================================================================

def _get_required_env(key: str, fallback_keys: Optional[List[str]] = None) -> str:
    """
    获取必需的环境变量
    
    如果未设置，记录警告但不终止程序（允许观察模式下运行）
    """
    value = os.getenv(key)
    
    if not value and fallback_keys:
        for fallback in fallback_keys:
            value = os.getenv(fallback)
            if value:
                break
    
    return value or ""


def _get_env(key: str, default: str = "") -> str:
    """获取环境变量，带默认值"""
    return os.getenv(key, default)


# ============================================================================
# 数据库配置
# ============================================================================

def get_database_url() -> str:
    """
    获取数据库连接 URL
    
    优先级：
    1. DATABASE_URL 环境变量（完整连接字符串）
    2. DB_URL 环境变量
    3. 从独立环境变量组装：DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
    
    注意：不再提供硬编码的 localhost 默认值
    """
    db_url = os.getenv("DATABASE_URL") or os.getenv("DB_URL")
    
    if db_url:
        # 隐藏密码用于日志
        safe_url = db_url.split("@")[-1] if "@" in db_url else db_url
        logging.info(f"数据库配置: {safe_url}")
        return db_url
    
    # 从独立环境变量组装
    db_host = os.getenv("DB_HOST")
    db_port = os.getenv("DB_PORT", "5432")
    db_name = os.getenv("DB_NAME")
    db_user = os.getenv("DB_USER")
    db_password = os.getenv("DB_PASSWORD")
    
    if db_host and db_name and db_user:
        db_url = f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"
        logging.info(f"数据库配置: {db_host}:{db_port}/{db_name}")
        return db_url
    
    # 未配置数据库，使用内存占位（仅用于测试）
    logging.warning(
        "⚠️ 数据库未配置！请设置 DATABASE_URL 或 DB_HOST/DB_NAME/DB_USER 环境变量。"
        "当前使用 SQLite 内存数据库（数据不会持久化）"
    )
    return "sqlite:///:memory:"


# ============================================================================
# Redis 配置
# ============================================================================

def get_redis_url() -> str:
    """
    获取 Redis 连接 URL
    
    优先级：
    1. REDIS_URL 环境变量（完整连接字符串）
    2. 从独立环境变量组装：REDIS_HOST, REDIS_PORT, REDIS_PASSWORD, REDIS_DB
    
    注意：不再提供硬编码的 localhost 默认值
    """
    redis_url = os.getenv("REDIS_URL")
    
    if redis_url:
        safe_url = redis_url.split("@")[-1] if "@" in redis_url else redis_url
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
    
    # 未配置 Redis，OBI 过滤将被禁用
    logging.warning(
        "⚠️ Redis 未配置！请设置 REDIS_URL 或 REDIS_HOST 环境变量。"
        "OBI（订单簿不平衡）过滤功能将被禁用。"
    )
    return ""


# ============================================================================
# 导出配置常量
# ============================================================================

DATABASE_URL = get_database_url()
REDIS_URL = get_redis_url()


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
            logging.warning(f"USER{idx} 凭证未配置或不完整")
        
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
    """验证关键配置是否已设置"""
    warnings = []
    
    if DATABASE_URL == "sqlite:///:memory:":
        warnings.append("数据库未配置，交易记录不会持久化")
    
    if not REDIS_URL:
        warnings.append("Redis 未配置，OBI 过滤将被禁用")
    
    creds = load_user_credentials()
    if not any(c.is_valid for c in creds):
        warnings.append("无有效的 Binance API 凭证")
    
    if warnings:
        logging.warning("=" * 60)
        logging.warning("⚠️ 配置警告:")
        for w in warnings:
            logging.warning(f"  - {w}")
        logging.warning("=" * 60)
    
    return len(warnings) == 0


# 启动时验证配置
validate_config()
