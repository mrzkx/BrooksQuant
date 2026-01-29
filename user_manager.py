import asyncio
import logging
from typing import Any, Dict, List, Optional

from binance import AsyncClient

from config import (
    UserCredentials,
    create_async_client_for_user,
    LARGE_BALANCE_THRESHOLD,
    LARGE_BALANCE_POSITION_PCT,
    POSITION_SIZE_PERCENT,
    LEVERAGE,
)
from utils import round_quantity_to_step_size as _round_quantity_to_step_size, round_tick_size as _round_tick_size
from user_filters import parse_symbol_filters_from_exchange_info, DEFAULT_FILTERS
from user_position_sizing import (
    get_position_size_percent as _get_position_size_percent,
    compute_order_quantity as _compute_order_quantity,
    compute_limit_price as _compute_limit_price,
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
        获取交易对的过滤器规则（stepSize, minQty, tickSize）。
        委托 user_filters.parse_symbol_filters_from_exchange_info。
        """
        if self.client is None:
            raise RuntimeError(f"用户 {self.name} 尚未连接客户端")
        if not force_refresh and symbol in self._symbol_filters:
            return self._symbol_filters[symbol]
        try:
            exchange_info = await self.client.futures_exchange_info()
            filters = parse_symbol_filters_from_exchange_info(
                exchange_info, symbol, log_prefix=f"[{self.name}]"
            )
            self._symbol_filters[symbol] = filters
            return filters
        except Exception as e:
            logging.error(f"[{self.name}] 获取交易规则失败: {e}", exc_info=True)
            return dict(DEFAULT_FILTERS)

    def round_step_size(self, quantity: float, step_size: float) -> float:
        """按照 stepSize 向下取整数量（委托 utils）。"""
        if step_size <= 0:
            return quantity
        return _round_quantity_to_step_size(quantity, step_size)

    def round_tick_size(self, price: float, tick_size: float) -> float:
        """按照 tickSize 取整价格（委托 utils）。"""
        if tick_size <= 0:
            return price
        return _round_tick_size(price, tick_size)

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
        """根据账户余额计算仓位百分比（委托 user_position_sizing）。"""
        return _get_position_size_percent(
            balance,
            self.SMALL_ACCOUNT_THRESHOLD,
            self.SMALL_ACCOUNT_POSITION_PCT,
            self.LARGE_ACCOUNT_POSITION_PCT,
        )

    def calculate_order_quantity(
        self,
        balance: float,
        current_price: float,
        leverage: int = DEFAULT_LEVERAGE,
        symbol: str = "BTCUSDT",
    ) -> float:
        """
        计算下单数量（实盘版本，符合交易所 stepSize 规则）。
        委托 user_position_sizing.compute_order_quantity。
        """
        filters = self._symbol_filters.get(symbol, dict(DEFAULT_FILTERS))
        step_size = filters.get("stepSize", 0.001)
        min_qty = filters.get("minQty", 0.001)
        min_notional = filters.get("minNotional", 5.0)
        position_pct = self.calculate_position_size_percent(balance)
        return _compute_order_quantity(
            balance,
            current_price,
            leverage,
            position_pct,
            step_size,
            min_qty,
            min_notional,
            self.round_step_size,
            log_prefix=f"[{self.name}]",
        )
    
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

    async def cancel_order(self, symbol: str, order_id: int) -> bool:
        """
        取消单个挂单
        
        Args:
            symbol: 交易对（如 "BTCUSDT"）
            order_id: 订单 ID
        
        Returns:
            bool: 是否成功
        """
        if self.client is None:
            raise RuntimeError(f"用户 {self.name} 尚未连接客户端")
        
        try:
            response = await self.client.futures_cancel_order(symbol=symbol, orderId=order_id)
            logging.info(f"[{self.name}] 已取消订单 {order_id}: {response}")
            return True
        except Exception as e:
            # 订单可能已成交或已取消，不视为错误
            if "Unknown order" in str(e) or "Order does not exist" in str(e):
                logging.info(f"[{self.name}] 订单 {order_id} 不存在或已成交")
                return True
            logging.error(f"[{self.name}] 取消订单 {order_id} 失败: {e}", exc_info=True)
            return False

    async def get_open_orders(self, symbol: str) -> List[Dict[str, Any]]:
        """
        获取指定交易对的所有挂单
        
        Args:
            symbol: 交易对（如 "BTCUSDT"）
        
        Returns:
            挂单列表
        """
        if self.client is None:
            raise RuntimeError(f"用户 {self.name} 尚未连接客户端")
        
        try:
            orders = await self.client.futures_get_open_orders(symbol=symbol)
            logging.debug(f"[{self.name}] {symbol} 挂单列表: {len(orders)} 个")
            return orders
        except Exception as e:
            logging.error(f"[{self.name}] 获取挂单列表失败: {e}", exc_info=True)
            return []

    async def create_take_profit_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        stop_price: float,
        reduce_only: bool = True,
    ) -> Dict[str, Any]:
        """
        创建限价止盈单（TP2 用，避免市价滑点）
        
        使用 TAKE_PROFIT 类型（限价止盈），当价格触及 stopPrice 后挂限价单
        
        Args:
            symbol: 交易对（如 "BTCUSDT"）
            side: 方向（"BUY" 平空仓 或 "SELL" 平多仓）
            quantity: 数量
            price: 限价（成交价格）
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
            "type": "TAKE_PROFIT",  # 限价止盈
            "quantity": quantity,
            "price": str(price),  # 限价
            "stopPrice": str(stop_price),  # 触发价
            "reduceOnly": "true" if reduce_only else "false",
            "timeInForce": "GTC",
        }
        
        logging.info(f"[{self.name}] 创建限价止盈单: {order_params}")
        response = await self.client.futures_create_order(**order_params)
        logging.info(f"[{self.name}] 限价止盈单响应: {response}")
        return response

    async def get_order_book_best_prices(self, symbol: str, limit: int = 5) -> tuple[float, float]:
        """
        获取订单簿最优买卖价（用于限价开仓）
        
        Args:
            symbol: 交易对（如 "BTCUSDT"）
            limit: 深度档位（5/10/20/50/100，默认 5 档即可取最优价）
        
        Returns:
            (best_bid, best_ask): 最优买一价、最优卖一价
        
        Raises:
            RuntimeError: 客户端未连接或订单簿为空
        """
        if self.client is None:
            raise RuntimeError(f"用户 {self.name} 尚未连接客户端")
        
        try:
            depth = await self.client.futures_order_book(symbol=symbol, limit=limit)
            bids = depth.get("bids", [])
            asks = depth.get("asks", [])
            if not bids or not asks:
                raise RuntimeError(f"[{self.name}] 订单簿为空: {symbol}")
            best_bid = float(bids[0][0])
            best_ask = float(asks[0][0])
            logging.debug(f"[{self.name}] 订单簿最优价 {symbol}: bid={best_bid}, ask={best_ask}")
            return (best_bid, best_ask)
        except Exception as e:
            logging.error(f"[{self.name}] 获取订单簿失败: {e}", exc_info=True)
            raise

    async def get_limit_price_from_order_book(
        self,
        symbol: str,
        side: str,
        offset_pct: float = 0.0,
        offset_ticks: int = 0,
    ) -> float:
        """
        根据订单簿最优价得到开仓限价（并符合 tickSize）。
        支持追价限价单（Marketable Limit）：买 Ask+偏移、卖 Bid-偏移，提高成交优先级并限制滑点。
        
        - 买入：best_ask + offset（偏移为正时更易成交）
        - 卖出：best_bid - offset
        
        Args:
            symbol: 交易对
            side: "BUY" 或 "SELL"
            offset_pct: 偏移百分比（如 0.05 表示 0.05%），与 offset_ticks 二选一
            offset_ticks: 偏移 tick 数（与 offset_pct 二选一）
        
        Returns:
            符合 tickSize 的限价
        """
        best_bid, best_ask = await self.get_order_book_best_prices(symbol)
        filters = self._symbol_filters.get(symbol, {"tickSize": 0.01})
        tick_size = float(filters.get("tickSize", 0.01))
        if side.upper() == "BUY":
            price = best_ask
            if offset_ticks > 0:
                price = price + tick_size * offset_ticks
            elif offset_pct > 0:
                price = price * (1.0 + offset_pct / 100.0)
        else:
            price = best_bid
            if offset_ticks > 0:
                price = price - tick_size * offset_ticks
            elif offset_pct > 0:
                price = price * (1.0 - offset_pct / 100.0)
        return self.round_tick_size(price, tick_size)

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

    async def get_used_margin(self, symbol: str) -> float:
        """
        获取当前已占用的保证金（用于仓位计算）
        
        Args:
            symbol: 交易对
        
        Returns:
            float: 已占用的保证金（USDT）
        """
        if self.client is None:
            raise RuntimeError(f"用户 {self.name} 尚未连接客户端")
        
        try:
            positions = await self.client.futures_position_information(symbol=symbol)
            total_used_margin = 0.0
            
            for pos in positions:
                amt = float(pos.get("positionAmt", 0))
                if amt != 0:
                    # 获取持仓的保证金
                    position_margin = float(pos.get("positionInitialMargin", 0))
                    total_used_margin += position_margin
            
            return total_used_margin
            
        except Exception as e:
            logging.warning(f"[{self.name}] 获取已占用保证金失败: {e}，假设为 0")
            return 0.0

    async def get_position_info(self, symbol: str) -> Optional[Dict]:
        """
        获取币安真实持仓信息（用于恢复持仓状态）
        
        Args:
            symbol: 交易对
        
        Returns:
            Dict 包含持仓信息，如果没有持仓则返回 None
            {
                "positionAmt": 持仓数量（正数=做多，负数=做空）,
                "entryPrice": 入场均价,
                "markPrice": 标记价格,
                "unRealizedProfit": 未实现盈亏,
                "leverage": 杠杆倍数,
                "positionSide": 持仓方向（LONG/SHORT）,
                "notional": 持仓名义价值
            }
        """
        if self.client is None:
            raise RuntimeError(f"用户 {self.name} 尚未连接客户端")
        
        try:
            positions = await self.client.futures_position_information(symbol=symbol)
            
            for pos in positions:
                amt = float(pos.get("positionAmt", 0))
                if amt != 0:
                    # 找到有持仓的仓位
                    return {
                        "positionAmt": amt,
                        "entryPrice": float(pos.get("entryPrice", 0)),
                        "markPrice": float(pos.get("markPrice", 0)),
                        "unRealizedProfit": float(pos.get("unRealizedProfit", 0)),
                        "leverage": int(pos.get("leverage", 20)),
                        "positionSide": pos.get("positionSide", "BOTH"),
                        "notional": float(pos.get("notional", 0)),
                        "isolatedMargin": float(pos.get("isolatedMargin", 0)),
                        "isolated": pos.get("isolated", False),
                    }
            
            return None
            
        except Exception as e:
            logging.error(f"[{self.name}] 获取持仓信息失败: {e}")
            return None

    async def sync_real_position(self, symbol: str) -> Dict[str, Any]:
        """
        同步币安真实持仓，返回标准化字典供策略引擎对比。

        调用 Binance futures_position_information，提取数量、开仓均价与浮盈。

        Args:
            symbol: 交易对，如 BTCUSDT

        Returns:
            标准化字典，始终返回（无持仓时数量为 0）:
            {
                "symbol": str,
                "position_amt": float,      # 有符号：正=多，负=空
                "quantity": float,           # 数量绝对值
                "side": str,                 # "buy" | "sell"
                "entry_price": float,       # 开仓均价
                "unrealized_profit": float, # 未实现盈亏（USDT）
                "has_position": bool,
            }
        """
        if self.client is None:
            raise RuntimeError(f"用户 {self.name} 尚未连接客户端")

        empty: Dict[str, Any] = {
            "symbol": symbol,
            "position_amt": 0.0,
            "quantity": 0.0,
            "side": "buy",
            "entry_price": 0.0,
            "unrealized_profit": 0.0,
            "has_position": False,
            "api_error": False,
        }

        try:
            positions = await self.client.futures_position_information(symbol=symbol)
            for pos in positions:
                amt = float(pos.get("positionAmt", 0))
                if amt == 0:
                    continue
                entry_price = float(pos.get("entryPrice", 0))
                un_realized = float(pos.get("unRealizedProfit", 0))
                quantity = abs(amt)
                side = "buy" if amt > 0 else "sell"
                return {
                    "symbol": symbol,
                    "position_amt": amt,
                    "quantity": quantity,
                    "side": side,
                    "entry_price": entry_price,
                    "unrealized_profit": un_realized,
                    "has_position": True,
                    "api_error": False,
                }
            return empty
        except Exception as e:
            logging.error(f"[{self.name}] sync_real_position({symbol}) 失败: {e}")
            # 返回带错误标记的空字典，避免误判为外部平仓
            return {**empty, "api_error": True}

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
            raw = await self.client.futures_account_trades(symbol=symbol, limit=limit)
            return list(raw) if isinstance(raw, list) else []
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
            raw = await self.client.futures_account_trades(symbol=symbol, limit=20)
            trades = list(raw) if isinstance(raw, list) else []
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
        """计算限价单价格（带滑点，符合 tickSize）。委托 user_position_sizing.compute_limit_price。"""
        filters = self._symbol_filters.get(symbol, dict(DEFAULT_FILTERS))
        tick_size = filters.get("tickSize", 0.01)
        return _compute_limit_price(
            current_price,
            side,
            slippage_pct,
            tick_size,
            self.round_tick_size,
            atr=atr,
        )
    
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
