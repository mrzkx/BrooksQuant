import asyncio
import logging
from typing import Any, Dict, Optional

from binance import AsyncClient

from config import UserCredentials, create_async_client_for_user


class TradingUser:
    """
    为每个交易用户维护独立的 AsyncClient，并提供线程安全的连接与下单方法。
    """

    def __init__(self, name: str, credentials: UserCredentials):
        self.name = name
        self.credentials = credentials
        self.client: Optional[AsyncClient] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> AsyncClient:
        """初始化客户端（避免重复创建）。"""
        async with self._lock:
            if self.client is None:
                if not self.credentials.api_key or not self.credentials.api_secret:
                    raise ValueError(f"{self.name} 缺少 API_KEY 或 API_SECRET")
                self.client = await create_async_client_for_user(self.credentials)
                logging.info("用户 %s 已连接 Binance API", self.name)
        assert self.client is not None
        return self.client

    async def close(self) -> None:
        async with self._lock:
            if self.client is not None:
                await self.client.close_connection()
                logging.info("用户 %s 已断开 Binance API", self.name)
                self.client = None

    async def create_order(self, **order_params: Any) -> Dict[str, Any]:
        """
        调用 Binance 下单接口。需要先调用 connect() 初始化客户端。
        """
        if self.client is None:
            raise RuntimeError(f"用户 {self.name} 尚未连接客户端，无法下单。")
        logging.info("用户 %s 下单参数: %s", self.name, order_params)
        response = await self.client.create_order(**order_params)
        logging.info("用户 %s 下单返回: %s", self.name, response)
        return response
