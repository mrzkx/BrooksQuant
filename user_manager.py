import asyncio
import logging
import math
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, Optional

from binance import AsyncClient

from config import (
    UserCredentials, 
    create_async_client_for_user,
    LARGE_BALANCE_THRESHOLD,
    LARGE_BALANCE_POSITION_PCT,
    POSITION_SIZE_PERCENT,
    LEVERAGE,
)


class TradingUser:
    """
    为每个交易用户维护独立的 AsyncClient，并提供线程安全的连接与下单方法。
    
    实盘功能：
    - 获取合约账户余额
    - 设置杠杆倍数
    - 动态仓位计算（基于 .env 配置）
    """

    # 仓位配置从 config.py 导入（可通过 .env 配置）
    SMALL_ACCOUNT_THRESHOLD = LARGE_BALANCE_THRESHOLD  # 小资金阈值（USDT）
    SMALL_ACCOUNT_POSITION_PCT = POSITION_SIZE_PERCENT  # 小资金仓位比例
    LARGE_ACCOUNT_POSITION_PCT = LARGE_BALANCE_POSITION_PCT  # 大资金仓位比例
    DEFAULT_LEVERAGE = LEVERAGE  # 默认杠杆倍数

    def __init__(self, name: str, credentials: UserCredentials):
        self.name = name
        self.credentials = credentials
        self.client: Optional[AsyncClient] = None
        self._lock = asyncio.Lock()
        self._leverage_set: Dict[str, bool] = {}  # 记录已设置杠杆的交易对
        self._cached_balance: Optional[float] = None  # 缓存的余额
        self._balance_cache_time: float = 0  # 余额缓存时间
        
        # 交易规则缓存（stepSize, minQty, tickSize）
        self._symbol_filters: Dict[str, Dict[str, Any]] = {}

    async def connect(self) -> AsyncClient:
        """初始化客户端（避免重复创建）。"""
        async with self._lock:
            if self.client is None:
                if not self.credentials.api_key or not self.credentials.api_secret:
                    raise ValueError(f"{self.name} 缺少 API_KEY 或 API_SECRET")
                self.client = await create_async_client_for_user(self.credentials)
                logging.info("用户 %s 已连接 Binance API", self.name)
                
                # 确保持仓模式为单向模式（One-Way Mode）
                await self._ensure_one_way_position_mode()
                
        assert self.client is not None
        return self.client
    
    async def _ensure_one_way_position_mode(self) -> bool:
        """
        确保账户使用单向持仓模式（One-Way Mode）
        
        Binance 有两种持仓模式：
        - 单向模式（One-Way）：一个交易对只能有一个方向的仓位
        - 双向模式（Hedge Mode）：可以同时持有多仓和空仓
        
        本策略使用单向模式，更简单且符合 Al Brooks 理念
        """
        if self.client is None:
            return False
        
        try:
            # 获取当前持仓模式
            position_mode = await self.client.futures_get_position_mode()
            is_hedge_mode = position_mode.get("dualSidePosition", False)
            
            if is_hedge_mode:
                logging.info(f"[{self.name}] 检测到双向持仓模式，正在切换为单向模式...")
                try:
                    await self.client.futures_change_position_mode(dualSidePosition=False)
                    logging.info(f"[{self.name}] ✅ 已切换为单向持仓模式")
                except Exception as e:
                    error_msg = str(e)
                    if "No need to change" in error_msg:
                        logging.info(f"[{self.name}] 持仓模式已是单向模式")
                    elif "position" in error_msg.lower():
                        # 如果有持仓无法切换，需要先平仓
                        logging.error(
                            f"[{self.name}] ⚠️ 无法切换持仓模式（可能有未平仓位置）。"
                            f"请在 Binance 手动切换为单向模式，或先平掉所有仓位。"
                        )
                        return False
                    else:
                        logging.error(f"[{self.name}] 切换持仓模式失败: {e}")
                        return False
            else:
                logging.info(f"[{self.name}] 持仓模式: 单向模式 ✓")
            
            return True
            
        except Exception as e:
            logging.error(f"[{self.name}] 获取持仓模式失败: {e}", exc_info=True)
            return False

    async def close(self) -> None:
        async with self._lock:
            if self.client is not None:
                await self.client.close_connection()
                logging.info("用户 %s 已断开 Binance API", self.name)
                self.client = None

    async def get_futures_balance(self, force_refresh: bool = False) -> float:
        """
        获取合约账户 USDT 余额（可用余额）
        
        Args:
            force_refresh: 是否强制刷新（默认使用 60 秒缓存）
        
        Returns:
            float: USDT 可用余额
        """
        import time
        
        if self.client is None:
            raise RuntimeError(f"用户 {self.name} 尚未连接客户端")
        
        # 使用缓存（60秒内有效）
        current_time = time.time()
        if not force_refresh and self._cached_balance is not None:
            if current_time - self._balance_cache_time < 60:
                return self._cached_balance
        
        try:
            # 获取合约账户信息
            account_info = await self.client.futures_account_balance()
            
            # 查找 USDT 余额
            for asset in account_info:
                if asset.get("asset") == "USDT":
                    balance = float(asset.get("availableBalance", 0))
                    self._cached_balance = balance
                    self._balance_cache_time = current_time
                    logging.info(f"[{self.name}] 合约账户 USDT 余额: {balance:.2f}")
                    return balance
            
            logging.warning(f"[{self.name}] 未找到 USDT 余额信息")
            return 0.0
            
        except Exception as e:
            logging.error(f"[{self.name}] 获取账户余额失败: {e}", exc_info=True)
            # 如果有缓存，返回缓存值
            if self._cached_balance is not None:
                logging.warning(f"[{self.name}] 使用缓存余额: {self._cached_balance:.2f}")
                return self._cached_balance
            raise

    async def get_symbol_filters(self, symbol: str, force_refresh: bool = False) -> Dict[str, Any]:
        """
        获取交易对的过滤器规则（stepSize, minQty, tickSize）
        
        Args:
            symbol: 交易对（如 "BTCUSDT"）
            force_refresh: 是否强制刷新
        
        Returns:
            Dict: {
                "stepSize": 数量精度步长,
                "minQty": 最小数量,
                "maxQty": 最大数量,
                "tickSize": 价格精度步长,
                "minNotional": 最小名义价值
            }
        """
        if self.client is None:
            raise RuntimeError(f"用户 {self.name} 尚未连接客户端")
        
        # 使用缓存
        if not force_refresh and symbol in self._symbol_filters:
            return self._symbol_filters[symbol]
        
        try:
            exchange_info = await self.client.futures_exchange_info()
            
            for sym_info in exchange_info.get("symbols", []):
                if sym_info.get("symbol") == symbol:
                    filters = {}
                    
                    for f in sym_info.get("filters", []):
                        filter_type = f.get("filterType")
                        
                        if filter_type == "LOT_SIZE":
                            filters["stepSize"] = float(f.get("stepSize", "0.001"))
                            filters["minQty"] = float(f.get("minQty", "0.001"))
                            filters["maxQty"] = float(f.get("maxQty", "10000"))
                        
                        elif filter_type == "PRICE_FILTER":
                            filters["tickSize"] = float(f.get("tickSize", "0.01"))
                        
                        elif filter_type == "MIN_NOTIONAL":
                            filters["minNotional"] = float(f.get("notional", "5"))
                    
                    # 设置默认值
                    filters.setdefault("stepSize", 0.001)
                    filters.setdefault("minQty", 0.001)
                    filters.setdefault("maxQty", 10000)
                    filters.setdefault("tickSize", 0.01)
                    filters.setdefault("minNotional", 5)
                    
                    self._symbol_filters[symbol] = filters
                    logging.info(
                        f"[{self.name}] 获取 {symbol} 过滤器: "
                        f"stepSize={filters['stepSize']}, minQty={filters['minQty']}, "
                        f"tickSize={filters['tickSize']}"
                    )
                    return filters
            
            # 未找到交易对，使用默认值
            logging.warning(f"[{self.name}] 未找到 {symbol} 的过滤器，使用默认值")
            default_filters = {
                "stepSize": 0.001,
                "minQty": 0.001,
                "maxQty": 10000,
                "tickSize": 0.01,
                "minNotional": 5,
            }
            self._symbol_filters[symbol] = default_filters
            return default_filters
            
        except Exception as e:
            logging.error(f"[{self.name}] 获取交易规则失败: {e}", exc_info=True)
            # 返回 BTCUSDT 的默认值
            return {
                "stepSize": 0.001,
                "minQty": 0.001,
                "maxQty": 10000,
                "tickSize": 0.01,
                "minNotional": 5,
            }

    def round_step_size(self, quantity: float, step_size: float) -> float:
        """
        按照 stepSize 向下取整数量
        
        Args:
            quantity: 原始数量
            step_size: 步长（如 0.001）
        
        Returns:
            float: 取整后的数量
        """
        if step_size <= 0:
            return quantity
        
        # 使用 Decimal 避免浮点数精度问题
        qty_decimal = Decimal(str(quantity))
        step_decimal = Decimal(str(step_size))
        
        # 向下取整到 stepSize 的倍数
        rounded = (qty_decimal // step_decimal) * step_decimal
        
        return float(rounded)

    def round_tick_size(self, price: float, tick_size: float) -> float:
        """
        按照 tickSize 取整价格
        
        Args:
            price: 原始价格
            tick_size: 步长（如 0.01）
        
        Returns:
            float: 取整后的价格
        """
        if tick_size <= 0:
            return price
        
        # 使用 Decimal 避免浮点数精度问题
        price_decimal = Decimal(str(price))
        tick_decimal = Decimal(str(tick_size))
        
        # 四舍五入到 tickSize 的倍数
        rounded = round(price_decimal / tick_decimal) * tick_decimal
        
        return float(rounded)

    async def set_leverage(self, symbol: str, leverage: int = DEFAULT_LEVERAGE) -> bool:
        """
        设置交易对的杠杆倍数
        
        Args:
            symbol: 交易对（如 "BTCUSDT"）
            leverage: 杠杆倍数（默认 20）
        
        Returns:
            bool: 是否设置成功
        """
        if self.client is None:
            raise RuntimeError(f"用户 {self.name} 尚未连接客户端")
        
        # 检查是否已设置过
        if self._leverage_set.get(symbol):
            return True
        
        try:
            response = await self.client.futures_change_leverage(
                symbol=symbol,
                leverage=leverage
            )
            actual_leverage = response.get("leverage", leverage)
            logging.info(f"[{self.name}] 设置 {symbol} 杠杆: {actual_leverage}x")
            self._leverage_set[symbol] = True
            return True
            
        except Exception as e:
            error_msg = str(e)
            # 如果是"已经是该杠杆"的错误，视为成功
            if "No need to change" in error_msg or "leverage not changed" in error_msg.lower():
                logging.info(f"[{self.name}] {symbol} 杠杆已是 {leverage}x，无需更改")
                self._leverage_set[symbol] = True
                return True
            
            logging.error(f"[{self.name}] 设置杠杆失败: {e}", exc_info=True)
            return False

    def calculate_position_size_percent(self, balance: float) -> float:
        """
        根据账户余额计算仓位百分比
        
        规则：
        - 余额 <= 1000 USDT: 100% 仓位（全仓）
        - 余额 > 1000 USDT: 20% 仓位
        
        Args:
            balance: 账户 USDT 余额
        
        Returns:
            float: 仓位百分比（0-100）
        """
        if balance <= self.SMALL_ACCOUNT_THRESHOLD:
            return self.SMALL_ACCOUNT_POSITION_PCT
        else:
            return self.LARGE_ACCOUNT_POSITION_PCT

    def calculate_order_quantity(
        self, 
        balance: float, 
        current_price: float, 
        leverage: int = DEFAULT_LEVERAGE,
        symbol: str = "BTCUSDT"
    ) -> float:
        """
        计算下单数量（实盘版本，符合交易所 stepSize 规则）
        
        公式: 下单数量 = (余额 × 仓位百分比 × 杠杆) / 当前价格
        
        Args:
            balance: 账户 USDT 余额
            current_price: 当前价格
            leverage: 杠杆倍数（默认 20）
            symbol: 交易对（用于获取 stepSize）
        
        Returns:
            float: 符合 stepSize 规则的数量
        """
        if current_price <= 0 or balance <= 0:
            logging.warning(f"[{self.name}] 无效参数: 余额={balance}, 价格={current_price}")
            return 0.001  # 最小值
        
        # 获取交易规则（使用缓存）
        filters = self._symbol_filters.get(symbol, {
            "stepSize": 0.001,
            "minQty": 0.001,
            "minNotional": 5,
        })
        step_size = filters.get("stepSize", 0.001)
        min_qty = filters.get("minQty", 0.001)
        min_notional = filters.get("minNotional", 5)
        
        # 计算仓位百分比
        position_pct = self.calculate_position_size_percent(balance)
        
        # 开仓金额 = 余额 × 仓位百分比
        position_value = balance * (position_pct / 100)
        
        # 实际购买力 = 开仓金额 × 杠杆
        buying_power = position_value * leverage
        
        # 下单数量 = 购买力 / 当前价格
        quantity = buying_power / current_price
        
        # 按 stepSize 向下取整
        quantity = self.round_step_size(quantity, step_size)
        
        # 确保不低于最小交易量
        quantity = max(quantity, min_qty)
        
        # 检查最小名义价值（防止订单被拒绝）
        notional_value = quantity * current_price
        if notional_value < min_notional:
            # 调整数量以满足最小名义价值
            quantity = min_notional / current_price
            quantity = self.round_step_size(quantity, step_size)
            # 向上取整确保满足最小值
            if quantity * current_price < min_notional:
                quantity += step_size
            logging.warning(
                f"[{self.name}] 调整数量以满足最小名义价值: "
                f"{notional_value:.2f} < {min_notional}, 新数量={quantity:.6f}"
            )
        
        logging.info(
            f"[{self.name}] 仓位计算: 余额={balance:.2f} USDT, "
            f"仓位比例={position_pct:.0f}%, 杠杆={leverage}x, "
            f"下单数量={quantity:.6f} BTC (≈{quantity * current_price:.2f} USDT), "
            f"stepSize={step_size}"
        )
        
        return quantity
    
    async def calculate_order_quantity_async(
        self, 
        balance: float, 
        current_price: float, 
        leverage: int = DEFAULT_LEVERAGE,
        symbol: str = "BTCUSDT"
    ) -> float:
        """
        异步计算下单数量（首次调用时获取交易规则）
        
        建议在实盘下单前使用此方法，确保获取最新的 stepSize
        """
        # 确保已获取交易规则
        if symbol not in self._symbol_filters:
            await self.get_symbol_filters(symbol)
        
        return self.calculate_order_quantity(balance, current_price, leverage, symbol)

    async def create_order(self, **order_params: Any) -> Dict[str, Any]:
        """
        调用 Binance 下单接口（通用方法）。需要先调用 connect() 初始化客户端。
        """
        if self.client is None:
            raise RuntimeError(f"用户 {self.name} 尚未连接客户端，无法下单。")
        logging.info("用户 %s 下单参数: %s", self.name, order_params)
        response = await self.client.futures_create_order(**order_params)
        logging.info("用户 %s 下单返回: %s", self.name, response)
        return response

    async def create_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        time_in_force: str = "GTC",
        reduce_only: bool = False,
    ) -> Dict[str, Any]:
        """
        创建限价单（开仓用）
        
        Args:
            symbol: 交易对（如 "BTCUSDT"）
            side: 方向（"BUY" 或 "SELL"）
            quantity: 数量
            price: 限价
            time_in_force: 有效期（GTC=撤销前有效, IOC=立即成交或取消, FOK=全部成交或取消）
            reduce_only: 是否只减仓
        
        Returns:
            订单响应
        """
        if self.client is None:
            raise RuntimeError(f"用户 {self.name} 尚未连接客户端，无法下单。")
        
        order_params = {
            "symbol": symbol,
            "side": side.upper(),
            "type": "LIMIT",
            "quantity": quantity,
            "price": str(price),  # Binance 要求字符串
            "timeInForce": time_in_force,
        }
        
        if reduce_only:
            order_params["reduceOnly"] = "true"
        
        logging.info(f"[{self.name}] 创建限价单: {order_params}")
        response = await self.client.futures_create_order(**order_params)
        logging.info(f"[{self.name}] 限价单响应: {response}")
        return response

    async def create_stop_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        stop_price: float,
        reduce_only: bool = True,
    ) -> Dict[str, Any]:
        """
        创建止损市价单（平仓用）
        
        Args:
            symbol: 交易对（如 "BTCUSDT"）
            side: 方向（"BUY" 平空仓 或 "SELL" 平多仓）
            quantity: 数量
            stop_price: 触发价格
            reduce_only: 是否只减仓（止损通常设为 True）
        
        Returns:
            订单响应
        """
        if self.client is None:
            raise RuntimeError(f"用户 {self.name} 尚未连接客户端，无法下单。")
        
        order_params = {
            "symbol": symbol,
            "side": side.upper(),
            "type": "STOP_MARKET",
            "quantity": quantity,
            "stopPrice": str(stop_price),  # Binance 要求字符串
            "reduceOnly": "true" if reduce_only else "false",
        }
        
        logging.info(f"[{self.name}] 创建止损市价单: {order_params}")
        response = await self.client.futures_create_order(**order_params)
        logging.info(f"[{self.name}] 止损市价单响应: {response}")
        return response

    async def create_take_profit_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        stop_price: float,
        reduce_only: bool = True,
    ) -> Dict[str, Any]:
        """
        创建止盈市价单（平仓用）
        
        Args:
            symbol: 交易对（如 "BTCUSDT"）
            side: 方向（"BUY" 平空仓 或 "SELL" 平多仓）
            quantity: 数量
            stop_price: 触发价格
            reduce_only: 是否只减仓
        
        Returns:
            订单响应
        """
        if self.client is None:
            raise RuntimeError(f"用户 {self.name} 尚未连接客户端，无法下单。")
        
        order_params = {
            "symbol": symbol,
            "side": side.upper(),
            "type": "TAKE_PROFIT_MARKET",
            "quantity": quantity,
            "stopPrice": str(stop_price),
            "reduceOnly": "true" if reduce_only else "false",
        }
        
        logging.info(f"[{self.name}] 创建止盈市价单: {order_params}")
        response = await self.client.futures_create_order(**order_params)
        logging.info(f"[{self.name}] 止盈市价单响应: {response}")
        return response

    async def cancel_all_orders(self, symbol: str) -> bool:
        """
        取消指定交易对的所有挂单
        
        Args:
            symbol: 交易对（如 "BTCUSDT"）
        
        Returns:
            bool: 是否成功
        """
        if self.client is None:
            raise RuntimeError(f"用户 {self.name} 尚未连接客户端")
        
        try:
            response = await self.client.futures_cancel_all_open_orders(symbol=symbol)
            logging.info(f"[{self.name}] 已取消 {symbol} 所有挂单: {response}")
            return True
        except Exception as e:
            logging.error(f"[{self.name}] 取消挂单失败: {e}", exc_info=True)
            return False

    async def create_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        reduce_only: bool = False,
    ) -> Dict[str, Any]:
        """
        创建市价单（入场或平仓）
        
        Al Brooks 理念：突破型信号需要快速入场，使用市价单确保成交
        
        Args:
            symbol: 交易对（如 "BTCUSDT"）
            side: 方向（"BUY" 或 "SELL"）
            quantity: 数量
            reduce_only: 是否只减仓（平仓时使用）
        
        Returns:
            订单响应
        """
        if self.client is None:
            raise RuntimeError(f"用户 {self.name} 尚未连接客户端，无法下单。")
        
        order_params = {
            "symbol": symbol,
            "side": side.upper(),
            "type": "MARKET",
            "quantity": quantity,
        }
        
        if reduce_only:
            order_params["reduceOnly"] = "true"
        
        logging.info(f"[{self.name}] 创建市价单: {order_params}")
        response = await self.client.futures_create_order(**order_params)
        logging.info(f"[{self.name}] 市价单响应: {response}")
        return response

    async def close_position_market(
        self,
        symbol: str,
        side: str,
        quantity: float,
    ) -> Dict[str, Any]:
        """
        市价平仓（Al Brooks 动态退出）
        
        当 K 线监控检测到退出信号时调用，确保立即成交
        
        Args:
            symbol: 交易对
            side: 持仓方向（"buy" 做多仓 或 "sell" 做空仓）
            quantity: 平仓数量
        
        Returns:
            订单响应
        """
        # 平仓方向与持仓方向相反
        close_side = "SELL" if side.lower() == "buy" else "BUY"
        
        return await self.create_market_order(
            symbol=symbol,
            side=close_side,
            quantity=quantity,
            reduce_only=True,
        )

    async def has_open_position(self, symbol: str) -> bool:
        """
        检查是否有未平仓的仓位（双止损优化）
        
        用于在程序执行止损前检查仓位是否已被硬止损单平掉
        
        Args:
            symbol: 交易对
        
        Returns:
            bool: True 表示有仓位，False 表示无仓位
        """
        if self.client is None:
            raise RuntimeError(f"用户 {self.name} 尚未连接客户端")
        
        try:
            positions = await self.client.futures_position_information(symbol=symbol)
            for pos in positions:
                amt = float(pos.get("positionAmt", 0))
                if amt != 0:
                    logging.debug(f"[{self.name}] 检测到仓位: {symbol} {amt}")
                    return True
            
            logging.debug(f"[{self.name}] 无仓位: {symbol}")
            return False
            
        except Exception as e:
            logging.error(f"[{self.name}] 检查仓位失败: {e}")
            raise

    async def get_recent_trades(self, symbol: str, limit: int = 10) -> list:
        """
        获取最近的成交记录（用于计算实际盈亏）
        
        Args:
            symbol: 交易对
            limit: 返回记录数
        
        Returns:
            成交记录列表，包含 price, qty, commission, commissionAsset 等
        """
        if self.client is None:
            raise RuntimeError(f"用户 {self.name} 尚未连接客户端")
        
        try:
            trades = await self.client.futures_account_trades(symbol=symbol, limit=limit)
            return trades
        except Exception as e:
            logging.error(f"[{self.name}] 获取成交记录失败: {e}")
            return []

    async def get_trade_details(self, symbol: str, quantity: float) -> dict:
        """
        获取最近平仓的实际成交详情（价格和手续费）
        
        通过匹配数量找到对应的成交记录
        
        Args:
            symbol: 交易对
            quantity: 平仓数量
        
        Returns:
            {
                "avg_price": 实际成交均价,
                "commission": 手续费,
                "commission_asset": 手续费资产
            }
        """
        if self.client is None:
            raise RuntimeError(f"用户 {self.name} 尚未连接客户端")
        
        try:
            # 获取最近 20 条成交记录
            trades = await self.client.futures_account_trades(symbol=symbol, limit=20)
            
            if not trades:
                return {"avg_price": 0, "commission": 0, "commission_asset": "USDT"}
            
            # 按时间倒序（最新的在前）
            trades.sort(key=lambda x: x.get("time", 0), reverse=True)
            
            # 查找匹配数量的成交（可能分多笔成交）
            target_qty = abs(float(quantity))
            matched_trades = []
            accumulated_qty = 0
            
            for trade in trades:
                trade_qty = abs(float(trade.get("qty", 0)))
                accumulated_qty += trade_qty
                matched_trades.append(trade)
                
                # 累计数量达到目标（允许 1% 误差）
                if accumulated_qty >= target_qty * 0.99:
                    break
            
            if not matched_trades:
                return {"avg_price": 0, "commission": 0, "commission_asset": "USDT"}
            
            # 计算加权平均价格和总手续费
            total_value = 0
            total_qty = 0
            total_commission = 0
            commission_asset = "USDT"
            
            for trade in matched_trades:
                price = float(trade.get("price", 0))
                qty = abs(float(trade.get("qty", 0)))
                commission = float(trade.get("commission", 0))
                commission_asset = trade.get("commissionAsset", "USDT")
                
                total_value += price * qty
                total_qty += qty
                total_commission += commission
            
            avg_price = total_value / total_qty if total_qty > 0 else 0
            
            logging.info(
                f"[{self.name}] 成交详情: 均价={avg_price:.2f}, "
                f"手续费={total_commission:.4f} {commission_asset}"
            )
            
            return {
                "avg_price": avg_price,
                "commission": total_commission,
                "commission_asset": commission_asset,
            }
            
        except Exception as e:
            logging.error(f"[{self.name}] 获取成交详情失败: {e}")
            return {"avg_price": 0, "commission": 0, "commission_asset": "USDT"}

    def calculate_limit_price(
        self, 
        current_price: float, 
        side: str, 
        slippage_pct: float = 0.05,
        symbol: str = "BTCUSDT",
        atr: Optional[float] = None,
    ) -> float:
        """
        计算限价单价格（带滑点容忍度，符合 tickSize 规则）
        
        Args:
            current_price: 当前价格
            side: 方向（"buy" 或 "sell"）
            slippage_pct: 滑点百分比（默认 0.05%）
            symbol: 交易对（用于获取 tickSize）
            atr: ATR值（可选，用于动态调整滑点）
        
        Returns:
            float: 符合 tickSize 规则的限价
        """
        # 获取 tickSize（使用缓存）
        filters = self._symbol_filters.get(symbol, {"tickSize": 0.01})
        tick_size = filters.get("tickSize", 0.01)
        
        # 动态滑点计算（问题6修复）
        # 如果提供了ATR，根据市场波动调整滑点
        if atr is not None and atr > 0:
            # ATR 相对于价格的百分比
            atr_pct = (atr / current_price) * 100
            # 滑点至少为 ATR 的 10%，但不超过 0.3%
            dynamic_slippage = max(slippage_pct, min(atr_pct * 0.1, 0.3))
            slippage_pct = dynamic_slippage
        
        slippage = current_price * (slippage_pct / 100)
        
        if side.lower() == "buy":
            # 买入：略高于当前价
            limit_price = current_price + slippage
        else:
            # 卖出：略低于当前价
            limit_price = current_price - slippage
        
        # 按 tickSize 取整
        return self.round_tick_size(limit_price, tick_size)
    
    async def get_order_status(self, symbol: str, order_id: int) -> Dict[str, Any]:
        """
        查询订单状态
        
        Args:
            symbol: 交易对
            order_id: 订单ID
        
        Returns:
            订单详情
        """
        if self.client is None:
            raise RuntimeError(f"用户 {self.name} 尚未连接客户端")
        
        response = await self.client.futures_get_order(symbol=symbol, orderId=order_id)
        return response
    
    async def wait_for_order_fill(
        self,
        symbol: str,
        order_id: int,
        timeout_seconds: float = 60.0,
        poll_interval: float = 2.0,
    ) -> Dict[str, Any]:
        """
        等待限价单成交（带超时）
        
        问题1修复：添加订单状态轮询和超时取消逻辑
        
        Args:
            symbol: 交易对
            order_id: 订单ID
            timeout_seconds: 超时时间（秒）
            poll_interval: 轮询间隔（秒）
        
        Returns:
            订单详情（如果成交）
        
        Raises:
            TimeoutError: 如果超时未成交
        """
        import asyncio
        start_time = asyncio.get_event_loop().time()
        
        while True:
            try:
                order = await self.get_order_status(symbol, order_id)
                status = order.get("status", "")
                
                if status == "FILLED":
                    logging.info(f"[{self.name}] ✅ 限价单 {order_id} 已成交: avgPrice={order.get('avgPrice')}")
                    return order
                elif status == "PARTIALLY_FILLED":
                    executed_qty = float(order.get("executedQty", 0))
                    logging.info(f"[{self.name}] ⏳ 限价单 {order_id} 部分成交: {executed_qty}")
                elif status in ["CANCELED", "EXPIRED", "REJECTED"]:
                    logging.warning(f"[{self.name}] ⚠️ 限价单 {order_id} 状态异常: {status}")
                    return order
                
                # 检查超时
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed >= timeout_seconds:
                    logging.warning(f"[{self.name}] ⏰ 限价单 {order_id} 等待超时 ({timeout_seconds}s)，取消订单")
                    # 尝试取消订单
                    try:
                        await self.client.futures_cancel_order(symbol=symbol, orderId=order_id)
                        logging.info(f"[{self.name}] 已取消未成交限价单 {order_id}")
                    except Exception as cancel_err:
                        logging.warning(f"[{self.name}] 取消订单失败（可能已成交）: {cancel_err}")
                    
                    # 再次查询最终状态
                    final_order = await self.get_order_status(symbol, order_id)
                    if final_order.get("status") == "FILLED":
                        return final_order
                    
                    raise TimeoutError(f"限价单 {order_id} 超时未成交")
                
                await asyncio.sleep(poll_interval)
                
            except Exception as e:
                if "TimeoutError" in str(type(e).__name__):
                    raise
                logging.error(f"[{self.name}] 查询订单状态失败: {e}")
                await asyncio.sleep(poll_interval)
