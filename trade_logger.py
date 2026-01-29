"""
交易日志记录器 - 内存 + 可选 Redis 当前状态持久化

- 持仓与辅助状态存内存，可选写入 Redis（trade:position:{user}、trade:aux:{user}）
- 启动/恢复时：先查币安，再查 Redis；Redis 有且与币安一致则用 Redis 恢复，否则用币安覆盖
- 不落历史库：已完成交易不写入任何 DB
"""

import json
import logging
import threading
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from typing import Dict, List, Optional, Any

try:
    import redis
except ImportError:
    redis = None



@dataclass
class Trade:
    """
    内存交易记录（与原先 DB 字段兼容，供持仓与统计使用）
    
    订单 ID 字段（实盘用于 OCO 风格止盈止损管理）：
    - tp1_order_id: TP1 止盈单订单 ID
    - tp2_order_id: TP2 止盈单订单 ID（TP1 触发后挂单）
    - sl_order_id: 止损单订单 ID（TP1 触发后挂单）
    """
    id: int = 0
    user: str = ""
    signal: str = ""
    side: str = ""
    entry_price: float = 0.0
    quantity: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    exit_timestamp: Optional[datetime] = None
    pnl: Optional[float] = None
    pnl_percent: Optional[float] = None
    status: str = "open"
    exit_stage: int = 0
    tp1_price: Optional[float] = None
    tp2_price: Optional[float] = None
    remaining_quantity: Optional[float] = None
    breakeven_moved: bool = False
    original_stop_loss: Optional[float] = None
    trailing_stop_price: Optional[float] = None
    trailing_stop_activated: bool = False
    trailing_max_profit_r: Optional[float] = None
    tp1_close_ratio: float = 0.5
    is_climax_bar: bool = False
    hard_stop_loss: Optional[float] = None
    market_state: Optional[str] = None
    tight_channel_score: Optional[float] = None
    signal_strength: Optional[float] = None
    is_observe: bool = True
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    # OCO 风格订单 ID（实盘用）
    tp1_order_id: Optional[int] = None
    tp2_order_id: Optional[int] = None
    sl_order_id: Optional[int] = None

    def __repr__(self):
        return f"<Trade(id={self.id}, user='{self.user}', signal='{self.signal}', status='{self.status}')>"


# ---------------------------------------------------------------------------
# 序列化（供 Redis 读写）
# ---------------------------------------------------------------------------

_DATETIME_FIELDS = {"created_at", "updated_at", "exit_timestamp"}


def _trade_to_dict(trade: Trade) -> Dict[str, Any]:
    """Trade -> 可 JSON 序列化的 dict（datetime 转 ISO 字符串）"""
    d = asdict(trade)
    for k in _DATETIME_FIELDS:
        if k in d and d[k] is not None:
            v = d[k]
            d[k] = v.isoformat() if hasattr(v, "isoformat") else v
    return d


def _dict_to_trade(d: Dict[str, Any]) -> Trade:
    """dict -> Trade（ISO 字符串转 datetime）"""
    d = dict(d)
    for k in _DATETIME_FIELDS:
        if k in d and d[k] is not None and isinstance(d[k], str):
            try:
                d[k] = datetime.fromisoformat(d[k].replace("Z", "+00:00"))
            except Exception:
                d[k] = None
    field_names = {f.name for f in fields(Trade)}
    filtered = {k: v for k, v in d.items() if k in field_names}
    return Trade(**filtered)


class TradeLogger:
    """
    交易日志记录器（内存 + 可选 Redis 当前状态持久化）

    - 持仓与辅助状态存内存，可选写入 Redis（开/平仓、TP1、追踪、冷却时写）
    - 启动/恢复时：先查币安，再查 Redis；Redis 有且与币安一致则用 Redis 恢复
    - 不落历史库：已完成交易不写入任何 DB
    """

    REDIS_KEY_POSITION = "trade:position:{user}"
    REDIS_KEY_AUX = "trade:aux:{user}"

    def __init__(self, redis_url: Optional[str] = None):
        self._lock = threading.RLock()
        self.positions: Dict[str, Optional[Trade]] = {}
        self._closed_trades: List[Trade] = []  # 当前进程已平仓记录，用于统计（不落库）
        self._tp2_order_placed: Dict[str, bool] = {}
        self._tp1_order_placed: Dict[str, bool] = {}
        self._trailing_stop: Dict[str, Dict] = {}
        self.TRAILING_ACTIVATION_R = 0.8
        self.TRAILING_DISTANCE_R = 0.5
        self.cooldown_until: Dict[str, Optional[float]] = {}
        self.kline_count: int = 0
        self._trade_id_counter = 0
        self._redis_url: Optional[str] = redis_url if redis_url else None
        self._redis_client: Optional[Any] = None
        if self._redis_url and redis:
            try:
                self._redis_client = redis.Redis.from_url(self._redis_url, decode_responses=True)
                self._redis_client.ping()
                logging.info("📊 交易日志器已初始化（内存 + Redis 当前状态持久化）")
            except Exception as e:
                logging.warning(f"📊 Redis 连接失败，仅使用内存: {e}")
                self._redis_client = None
        else:
            logging.info("📊 交易日志器已初始化（内存版），持仓将根据币安真实持仓恢复与更新")

    async def close(self) -> None:
        """关闭 Redis 连接"""
        if self._redis_client is not None:
            try:
                self._redis_client.close()
                logging.info("📊 交易日志器 Redis 连接已关闭")
            except Exception as e:
                logging.warning(f"关闭交易日志器 Redis 连接时出错: {e}")
            finally:
                self._redis_client = None

    def _redis(self) -> Optional[Any]:
        """获取 Redis 客户端（懒连接）"""
        if self._redis_client is None:
            return None
        try:
            self._redis_client.ping()
            return self._redis_client
        except Exception:
            self._redis_client = None
            return None

    def _redis_save_position(self, user: str, trade: Optional[Trade]) -> None:
        """写入当前持仓到 Redis trade:position:{user}"""
        r = self._redis()
        if not r:
            return
        try:
            key = self.REDIS_KEY_POSITION.format(user=user)
            if trade is None:
                r.delete(key)
            else:
                r.set(key, json.dumps(_trade_to_dict(trade)))
        except Exception as e:
            logging.debug(f"[{user}] Redis 写入 position 失败: {e}")

    def _redis_load_position(self, user: str) -> Optional[Trade]:
        """从 Redis 读取 trade:position:{user}"""
        r = self._redis()
        if not r:
            return None
        try:
            key = self.REDIS_KEY_POSITION.format(user=user)
            raw = r.get(key)
            if not raw:
                return None
            return _dict_to_trade(json.loads(raw))
        except Exception as e:
            logging.debug(f"[{user}] Redis 读取 position 失败: {e}")
            return None

    def _redis_save_aux(self, user: str) -> None:
        """写入辅助状态到 Redis trade:aux:{user}（tp1_placed, tp2_placed, trailing, cooldown_until）"""
        r = self._redis()
        if not r:
            return
        try:
            key = self.REDIS_KEY_AUX.format(user=user)
            aux = {
                "tp1_placed": self._tp1_order_placed.get(user, False),
                "tp2_placed": self._tp2_order_placed.get(user, False),
                "trailing": self._trailing_stop.get(user),
                "cooldown_until": self.cooldown_until.get(user),
            }
            r.set(key, json.dumps(aux))
        except Exception as e:
            logging.debug(f"[{user}] Redis 写入 aux 失败: {e}")

    def _redis_load_aux(self, user: str) -> Optional[Dict[str, Any]]:
        """从 Redis 读取 trade:aux:{user}"""
        r = self._redis()
        if not r:
            return None
        try:
            key = self.REDIS_KEY_AUX.format(user=user)
            raw = r.get(key)
            if not raw:
                return None
            return json.loads(raw)
        except Exception as e:
            logging.debug(f"[{user}] Redis 读取 aux 失败: {e}")
            return None

    def _redis_del_user(self, user: str) -> None:
        """删除该用户的 position 与 aux 键"""
        r = self._redis()
        if not r:
            return
        try:
            r.delete(self.REDIS_KEY_POSITION.format(user=user))
            r.delete(self.REDIS_KEY_AUX.format(user=user))
        except Exception as e:
            logging.debug(f"[{user}] Redis 删除键失败: {e}")

    def _next_id(self) -> int:
        self._trade_id_counter += 1
        return self._trade_id_counter

    def recover_from_binance_position(
        self,
        user: str,
        position_info: Dict,
        current_price: float,
        atr: Optional[float] = None,
    ) -> Optional[Trade]:
        """
        恢复交易状态：先查币安，再查 Redis；Redis 有且与币安一致则用 Redis 恢复，否则用币安推断。
        """
        with self._lock:
            if self.positions.get(user):
                logging.warning(f"[{user}] 已有持仓记录，将被币安真实持仓覆盖")
                self.positions[user] = None

            try:
                position_amt = position_info["positionAmt"]
                entry_price = float(position_info["entryPrice"])

                if position_amt > 0:
                    side = "buy"
                    quantity = float(position_amt)
                else:
                    side = "sell"
                    quantity = abs(float(position_amt))

                # 币安无仓位：清理 Redis 并返回
                if quantity <= 0:
                    self._redis_del_user(user)
                    return None

                # 先尝试从 Redis 恢复（与币安一致则用 Redis 状态）
                trade_redis = self._redis_load_position(user)
                if trade_redis is not None:
                    redis_side = getattr(trade_redis, "side", "")
                    redis_qty = float(getattr(trade_redis, "quantity", 0) or 0)
                    if redis_side == side and redis_qty > 0 and abs(redis_qty - quantity) / quantity <= 0.01:
                        trade_redis.quantity = quantity
                        trade_redis.entry_price = entry_price
                        trade_redis.remaining_quantity = quantity
                        self.positions[user] = trade_redis
                        aux = self._redis_load_aux(user) or {}
                        self._tp1_order_placed[user] = bool(aux.get("tp1_placed", False))
                        self._tp2_order_placed[user] = bool(aux.get("tp2_placed", False))
                        if aux.get("trailing"):
                            self._trailing_stop[user] = aux["trailing"]
                        else:
                            orig_sl = float(getattr(trade_redis, "original_stop_loss", None) or trade_redis.stop_loss)
                            self._trailing_stop[user] = {
                                "trailing_stop": float(trade_redis.stop_loss),
                                "original_stop_loss": orig_sl,
                                "max_profit": float(getattr(trade_redis, "trailing_max_profit_r", 0) or 0),
                                "activated": bool(getattr(trade_redis, "trailing_stop_activated", False)),
                            }
                        co = aux.get("cooldown_until")
                        if co is not None:
                            self.cooldown_until[user] = float(co)
                        logging.info(
                            f"🔄 [{user}] 从 Redis 恢复持仓: {side.upper()} {quantity:.6f} @ {entry_price:.2f}, "
                            f"已与币安对齐"
                        )
                        return trade_redis

                # Redis 无或与币安不一致：用币安推断
                if atr and atr > 0:
                    stop_distance = atr * 1.5
                else:
                    stop_distance = entry_price * 0.01

                if side == "buy":
                    stop_loss = entry_price - stop_distance
                else:
                    stop_loss = entry_price + stop_distance

                risk = stop_distance
                tp1_multiplier, tp2_multiplier = 1.0, 2.0
                if side == "buy":
                    tp1_price = entry_price + risk * tp1_multiplier
                    tp2_price = entry_price + risk * tp2_multiplier
                    take_profit = tp2_price
                else:
                    tp1_price = entry_price - risk * tp1_multiplier
                    tp2_price = entry_price - risk * tp2_multiplier
                    take_profit = tp2_price

                trade = Trade(
                    id=self._next_id(),
                    user=user,
                    signal="Recovered",
                    side=side,
                    entry_price=entry_price,
                    quantity=quantity,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    status="open",
                    exit_stage=0,
                    tp1_price=tp1_price,
                    tp2_price=tp2_price,
                    remaining_quantity=quantity,
                    breakeven_moved=False,
                    original_stop_loss=stop_loss,
                    trailing_stop_price=None,
                    trailing_stop_activated=False,
                    trailing_max_profit_r=None,
                    is_observe=False,
                    tp1_close_ratio=0.5,
                    is_climax_bar=False,
                    hard_stop_loss=None,
                )

                self.positions[user] = trade
                self._tp2_order_placed[user] = False
                self._redis_save_position(user, trade)
                self._redis_save_aux(user)

                tp1_already_hit = (side == "buy" and current_price >= tp1_price) or (
                    side == "sell" and current_price <= tp1_price
                )
                if tp1_already_hit:
                    logging.info(
                        f"🔄 从币安恢复持仓 [{user}]: {side.upper()} {quantity:.6f} @ {entry_price:.2f}, "
                        f"止损={stop_loss:.2f}, TP1={tp1_price:.2f}, TP2={tp2_price:.2f}, "
                        f"当前价={current_price:.2f} 已超过 TP1，将在下个周期触发止盈"
                    )
                else:
                    logging.info(
                        f"🔄 从币安恢复持仓 [{user}]: {side.upper()} {quantity:.6f} @ {entry_price:.2f}, "
                        f"止损={stop_loss:.2f}, TP1={tp1_price:.2f}, TP2={tp2_price:.2f}, "
                        f"当前价={current_price:.2f}, 未实现盈亏={position_info.get('unRealizedProfit', 0):.2f} USDT"
                    )
                return trade
            except Exception as e:
                logging.error(f"❌ 从币安恢复持仓失败 [{user}]: {e}", exc_info=True)
                return None

    def open_position(
        self,
        user: str,
        signal: str,
        side: str,
        entry_price: float,
        quantity: float,
        stop_loss: float,
        take_profit: float,
        signal_strength: Optional[float] = None,
        tp1_price: Optional[float] = None,
        tp2_price: Optional[float] = None,
        market_state: Optional[str] = None,
        tight_channel_score: Optional[float] = None,
        is_observe: bool = True,
        tp1_close_ratio: float = 0.5,
        is_climax_bar: bool = False,
        hard_stop_loss: Optional[float] = None,
    ) -> Trade:
        """开仓（线程安全，仅内存）"""
        entry_price = float(entry_price)
        quantity = float(quantity)
        stop_loss = float(stop_loss)
        take_profit = float(take_profit)
        signal_strength = float(signal_strength) if signal_strength is not None else None
        tp1_price = float(tp1_price) if tp1_price is not None else None
        tp2_price = float(tp2_price) if tp2_price is not None else None
        tight_channel_score = float(tight_channel_score) if tight_channel_score is not None else None
        tp1_close_ratio = float(tp1_close_ratio) if tp1_close_ratio is not None else 0.5
        hard_stop_loss = float(hard_stop_loss) if hard_stop_loss is not None else None

        with self._lock:
            if self.positions.get(user):
                self._close_position_unlocked(user, entry_price, "manual", "新信号开仓")

            trade = Trade(
                id=self._next_id(),
                user=user,
                signal=signal,
                side=side,
                entry_price=entry_price,
                quantity=quantity,
                stop_loss=stop_loss,
                take_profit=take_profit,
                status="open",
                exit_stage=0,
                tp1_price=tp1_price,
                tp2_price=tp2_price,
                remaining_quantity=quantity,
                breakeven_moved=False,
                original_stop_loss=stop_loss,
                trailing_stop_price=None,
                trailing_stop_activated=False,
                trailing_max_profit_r=None,
                market_state=market_state,
                tight_channel_score=tight_channel_score,
                signal_strength=signal_strength,
                is_observe=is_observe,
                tp1_close_ratio=tp1_close_ratio,
                is_climax_bar=is_climax_bar,
                hard_stop_loss=hard_stop_loss,
            )
            self.positions[user] = trade
            self._tp2_order_placed[user] = False
            self._tp1_order_placed[user] = False
            self._redis_save_position(user, trade)
            self._redis_save_aux(user)
            logging.info(
                f"用户 {user} 开仓: {signal} {side} @ {entry_price:.2f}, "
                f"止损={stop_loss:.2f}, TP1={tp1_price or take_profit:.2f}, TP2={tp2_price or take_profit:.2f}"
            )
            return trade

    def close_position(
        self, user: str, exit_price: float, exit_reason: str, note: str = ""
    ) -> Optional[Trade]:
        """平仓（线程安全）"""
        with self._lock:
            return self._close_position_unlocked(user, exit_price, exit_reason, note)

    def _close_position_unlocked(
        self, user: str, exit_price: float, exit_reason: str, note: str = ""
    ) -> Optional[Trade]:
        """平仓内部（需在持有锁时调用）"""
        trade = self.positions.get(user)
        if not trade:
            return None

        exit_price = float(exit_price)
        had_tp1 = (getattr(trade, "exit_stage", 0) >= 1 and trade.tp1_price)
        trade.exit_price = exit_price
        trade.exit_reason = exit_reason
        trade.exit_timestamp = datetime.utcnow()
        trade.status = "closed"
        trade.exit_stage = 2

        qty = trade.remaining_quantity or trade.quantity
        if trade.side == "buy":
            final_pnl = (exit_price - trade.entry_price) * qty
        else:
            final_pnl = (trade.entry_price - exit_price) * qty

        if had_tp1 and trade.tp1_price is not None:
            half_qty = trade.quantity * 0.5
            tp1_val = float(trade.tp1_price)
            if trade.side == "buy":
                tp1_pnl = (tp1_val - trade.entry_price) * half_qty
            else:
                tp1_pnl = (trade.entry_price - tp1_val) * half_qty
            trade.pnl = tp1_pnl + final_pnl
        else:
            trade.pnl = final_pnl

        cost_basis = (trade.entry_price or 0) * (trade.quantity or 0)
        if cost_basis > 0:
            trade.pnl_percent = (trade.pnl / cost_basis) * 100
        else:
            trade.pnl_percent = 0.0
            logging.warning(f"用户 {user} 交易 [ID={trade.id}] 成本为零，无法计算百分比盈亏")

        self._closed_trades.append(trade)
        if exit_reason == "stop_loss" and trade.pnl and trade.pnl < 0:
            self.set_cooldown(user, cooldown_bars=3)

        self.positions[user] = None
        self._redis_del_user(user)
        self._redis_save_aux(user)
        logging.info(
            f"用户 {user} 平仓: {exit_reason} @ {exit_price:.2f}, "
            f"盈亏={trade.pnl:.4f} USDT ({trade.pnl_percent:.2f}%) {note}"
        )
        return trade

    def force_close_position(
        self, user: str, exit_price: float, reason: str = "externally_closed"
    ) -> Optional[Trade]:
        """
        强制将本地持仓标记为已平仓（实盘对齐用）。

        当币安已无仓位但本地仍有记录时调用，表示被外部平仓（手动平仓、强平等）。
        不向交易所下单，仅同步本地状态，避免逻辑继续对该仓位计算止盈。
        """
        with self._lock:
            trade = self.positions.get(user)
            if not trade:
                return None
            if user in self._trailing_stop:
                del self._trailing_stop[user]
            return self._close_position_unlocked(
                user, float(exit_price), reason, note="实盘对齐：交易所已无仓位"
            )

    def update_trade_with_actual_pnl(
        self,
        user: str,
        actual_exit_price: float,
        commission: float = 0.0,
        open_commission: float = 0.0,
    ) -> bool:
        """
        使用币安真实成交更新最近一次已平仓记录的实际盈亏（仅更新内存统计）
        """
        actual_exit_price = float(actual_exit_price)
        commission = float(commission)
        open_commission = float(open_commission)

        with self._lock:
            for i in range(len(self._closed_trades) - 1, -1, -1):
                t = self._closed_trades[i]
                if t.user == user and t.status == "closed":
                    qty = t.remaining_quantity or t.quantity
                    if t.side == "buy":
                        raw_pnl = (actual_exit_price - float(t.entry_price)) * float(qty)
                    else:
                        raw_pnl = (float(t.entry_price) - actual_exit_price) * float(qty)
                    total_commission = commission + open_commission
                    t.pnl = raw_pnl - total_commission
                    cost_basis = float(t.entry_price) * float(t.quantity)
                    if cost_basis > 0:
                        t.pnl_percent = (t.pnl / cost_basis) * 100
                    t.exit_price = actual_exit_price
                    logging.info(
                        f"[{user}] 更新实际盈亏: 手续费={total_commission:.4f}, "
                        f"实际盈亏={t.pnl:.4f} USDT ({t.pnl_percent:.2f}%)"
                    )
                    return True
            logging.warning(f"[{user}] 未找到最近的已关闭交易，无法更新盈亏")
            return False

    def check_stop_loss_take_profit(self, user: str, current_price: float) -> Optional[Any]:
        """检查止损止盈（Al Brooks 动态退出，仅内存）"""
        current_price = float(current_price)

        with self._lock:
            trade = self.positions.get(user)
            if not trade:
                return None

            initial_risk = abs(float(trade.entry_price) - float(trade.stop_loss))
            if initial_risk == 0:
                initial_risk = float(trade.entry_price) * 0.01

            if trade.side == "buy":
                current_profit = current_price - float(trade.entry_price)
            else:
                current_profit = float(trade.entry_price) - current_price
            profit_in_r = current_profit / initial_risk if initial_risk > 0 else 0

            if user not in self._trailing_stop:
                if getattr(trade, "trailing_stop_activated", False) and getattr(
                    trade, "trailing_stop_price", None
                ):
                    self._trailing_stop[user] = {
                        "trailing_stop": float(trade.trailing_stop_price or 0),
                        "original_stop_loss": float(trade.original_stop_loss or trade.stop_loss),
                        "max_profit": float(getattr(trade, "trailing_max_profit_r", 0) or 0),
                        "activated": True,
                    }
                    logging.info(f"[{user}] 从内存恢复追踪止损状态: {self._trailing_stop[user]}")
                else:
                    original_sl = float(trade.original_stop_loss or trade.stop_loss)
                    self._trailing_stop[user] = {
                        "trailing_stop": float(trade.stop_loss),
                        "original_stop_loss": original_sl,
                        "max_profit": 0.0,
                        "activated": False,
                    }
                    if not trade.original_stop_loss:
                        trade.original_stop_loss = original_sl

            ts_state = self._trailing_stop[user]
            original_risk = abs(float(trade.entry_price) - ts_state["original_stop_loss"])
            if original_risk == 0:
                original_risk = initial_risk
            profit_in_r = current_profit / original_risk if original_risk > 0 else 0

            if profit_in_r > ts_state["max_profit"]:
                ts_state["max_profit"] = profit_in_r

            if not ts_state["activated"] and profit_in_r >= self.TRAILING_ACTIVATION_R:
                ts_state["activated"] = True
                trailing_distance = original_risk * self.TRAILING_DISTANCE_R
                if trade.side == "buy":
                    ts_state["trailing_stop"] = float(trade.entry_price) + current_profit - trailing_distance
                else:
                    ts_state["trailing_stop"] = float(trade.entry_price) - current_profit + trailing_distance
                trade.trailing_stop_activated = True
                trade.trailing_stop_price = ts_state["trailing_stop"]
                trade.trailing_max_profit_r = profit_in_r
                self._redis_save_position(user, trade)
                self._redis_save_aux(user)
                logging.info(
                    f"📈 [{user}] 追踪止损已激活！盈利={profit_in_r:.2f}R, "
                    f"追踪止损={ts_state['trailing_stop']:.2f}"
                )

            if ts_state["activated"]:
                trailing_distance = original_risk * self.TRAILING_DISTANCE_R
                ts_updated = False
                if trade.side == "buy":
                    new_ts = current_price - trailing_distance
                    if new_ts > ts_state["trailing_stop"]:
                        ts_state["trailing_stop"] = new_ts
                        ts_updated = True
                else:
                    new_ts = current_price + trailing_distance
                    if new_ts < ts_state["trailing_stop"]:
                        ts_state["trailing_stop"] = new_ts
                        ts_updated = True
                if profit_in_r > ts_state["max_profit"]:
                    ts_state["max_profit"] = profit_in_r
                    ts_updated = True
                if ts_updated:
                    trade.stop_loss = ts_state["trailing_stop"]
                    trade.trailing_stop_price = ts_state["trailing_stop"]
                    trade.trailing_max_profit_r = ts_state["max_profit"]
                    self._redis_save_position(user, trade)
                    self._redis_save_aux(user)

            if trade.exit_stage == 0 and trade.tp1_price:
                tp1_hit = (trade.side == "buy" and current_price >= float(trade.tp1_price)) or (
                    trade.side == "sell" and current_price <= float(trade.tp1_price)
                )
                if tp1_hit and self._tp1_order_placed.get(user, False):
                    return None
                if tp1_hit:
                    close_ratio = float(trade.tp1_close_ratio or 0.5)
                    close_qty = float(trade.quantity) * close_ratio
                    trade.remaining_quantity = float(trade.quantity) - close_qty
                    trade.exit_stage = 1
                    trade.status = "partial"
                    entry_price = float(trade.entry_price)
                    fee_buffer = entry_price * 0.001
                    if trade.side == "buy":
                        breakeven_stop = entry_price + fee_buffer
                    else:
                        breakeven_stop = entry_price - fee_buffer
                    trade.stop_loss = breakeven_stop
                    trade.breakeven_moved = True
                    if trade.side == "buy":
                        ts_state["trailing_stop"] = max(ts_state["trailing_stop"], breakeven_stop)
                    else:
                        ts_state["trailing_stop"] = min(ts_state["trailing_stop"], breakeven_stop)
                    ts_state["activated"] = True
                    if trade.tp2_price:
                        self._tp2_order_placed[user] = False
                    self._redis_save_position(user, trade)
                    self._redis_save_aux(user)
                    logging.info(
                        f"🎯 [{user}] TP1触发！平仓{int(close_ratio*100)}% @ {float(trade.tp1_price):.2f}, "
                        f"保本止损={breakeven_stop:.2f}"
                        + (f" [Climax信号棒]" if trade.is_climax_bar else "")
                    )
                    return {
                        "action": "tp1",
                        "trade": trade,
                        "close_quantity": close_qty,
                        "close_price": float(trade.tp1_price),
                        "new_stop_loss": breakeven_stop,
                        "tp2_price": float(trade.tp2_price) if trade.tp2_price else None,
                        # OCO 风格订单所需字段
                        "entry_price": float(trade.entry_price),
                        "position_side": trade.side,
                        "remaining_quantity": trade.remaining_quantity,
                        "side": "SELL" if trade.side.lower() == "buy" else "BUY",  # 平仓方向
                    }

            if trade.exit_stage == 1 and trade.tp2_price:
                tp2_hit = (trade.side == "buy" and current_price >= float(trade.tp2_price)) or (
                    trade.side == "sell" and current_price <= float(trade.tp2_price)
                )
                if tp2_hit:
                    if user in self._trailing_stop:
                        del self._trailing_stop[user]
                    return self._close_position_unlocked(user, float(trade.tp2_price), "tp2")

            if not trade.tp1_price and not trade.breakeven_moved:
                breakeven_hit = (
                    trade.side == "buy"
                    and current_price >= float(trade.entry_price) + initial_risk
                ) or (
                    trade.side == "sell"
                    and current_price <= float(trade.entry_price) - initial_risk
                )
                if breakeven_hit:
                    trade.stop_loss = float(trade.entry_price)
                    trade.breakeven_moved = True
                    ts_state["trailing_stop"] = float(trade.entry_price)
                    self._redis_save_position(user, trade)
                    self._redis_save_aux(user)
                    logging.info(f"💡 [{user}] Breakeven触发！止损移至入场价: {float(trade.entry_price):.2f}")

            effective_stop = ts_state["trailing_stop"] if ts_state["activated"] else float(trade.stop_loss)
            stop_hit = (trade.side == "buy" and current_price <= effective_stop) or (
                trade.side == "sell" and current_price >= effective_stop
            )
            if stop_hit:
                if ts_state["activated"] and ts_state["max_profit"] > 0:
                    reason = "trailing_stop"
                elif trade.breakeven_moved and float(trade.stop_loss) == float(trade.entry_price):
                    reason = "breakeven_stop"
                else:
                    reason = "stop_loss"
                if user in self._trailing_stop:
                    del self._trailing_stop[user]
                return self._close_position_unlocked(user, effective_stop, reason)

            if not trade.tp1_price:
                tp_hit = (trade.side == "buy" and current_price >= float(trade.take_profit)) or (
                    trade.side == "sell" and current_price <= float(trade.take_profit)
                )
                if tp_hit:
                    if user in self._trailing_stop:
                        del self._trailing_stop[user]
                    return self._close_position_unlocked(user, float(trade.take_profit), "take_profit")

            return None

    def mark_tp1_order_placed(self, user: str, order_id: Optional[int] = None):
        with self._lock:
            self._tp1_order_placed[user] = True
            trade = self.positions.get(user)
            if trade and order_id:
                trade.tp1_order_id = order_id
                self._redis_save_position(user, trade)
            self._redis_save_aux(user)

    def tp1_order_placed(self, user: str) -> bool:
        with self._lock:
            return bool(self._tp1_order_placed.get(user, False))

    def update_tp2_sl_order_ids(
        self, user: str, tp2_order_id: Optional[int] = None, sl_order_id: Optional[int] = None
    ) -> bool:
        """
        更新 TP2 和止损单的订单 ID（TP1 触发后挂单时调用）
        
        Args:
            user: 用户名
            tp2_order_id: TP2 限价止盈单订单 ID
            sl_order_id: 止损单订单 ID
        
        Returns:
            bool: 是否成功更新
        """
        with self._lock:
            trade = self.positions.get(user)
            if not trade:
                return False
            if tp2_order_id is not None:
                trade.tp2_order_id = tp2_order_id
            if sl_order_id is not None:
                trade.sl_order_id = sl_order_id
            trade.updated_at = datetime.utcnow()
            self._redis_save_position(user, trade)
            logging.info(
                f"[{user}] 已更新订单 ID: TP2={tp2_order_id}, SL={sl_order_id}"
            )
            return True

    def get_pending_order_ids(self, user: str) -> Dict[str, Optional[int]]:
        """
        获取当前持仓的挂单 ID（用于平仓前撤单）
        
        Returns:
            {'tp1_order_id': ..., 'tp2_order_id': ..., 'sl_order_id': ...}
        """
        with self._lock:
            trade = self.positions.get(user)
            if not trade:
                return {'tp1_order_id': None, 'tp2_order_id': None, 'sl_order_id': None}
            return {
                'tp1_order_id': trade.tp1_order_id,
                'tp2_order_id': trade.tp2_order_id,
                'sl_order_id': trade.sl_order_id,
            }

    def clear_order_ids(self, user: str) -> None:
        """
        清除订单 ID（平仓或撤单后调用）
        """
        with self._lock:
            trade = self.positions.get(user)
            if trade:
                trade.tp1_order_id = None
                trade.tp2_order_id = None
                trade.sl_order_id = None
                trade.updated_at = datetime.utcnow()
                self._redis_save_position(user, trade)

    def update_position_from_binance(
        self, user: str, quantity: float, entry_price: float
    ) -> bool:
        quantity = float(quantity)
        entry_price = float(entry_price)
        with self._lock:
            trade = self.positions.get(user)
            if not trade:
                return False
            trade.quantity = quantity
            trade.entry_price = entry_price
            trade.remaining_quantity = quantity
            logging.info(
                f"[{user}] 已用币安真实持仓更新: 数量={quantity:.4f}, 入场价={entry_price:.2f}"
            )
            return True

    def needs_tp1_fill_sync(self, user: str) -> bool:
        with self._lock:
            trade = self.positions.get(user)
            if not trade:
                return False
            return bool(self._tp1_order_placed.get(user, False))

    def sync_after_tp1_filled(
        self, user: str, remaining_quantity: float, entry_price: float
    ) -> bool:
        remaining_quantity = float(remaining_quantity)
        entry_price = float(entry_price)
        with self._lock:
            trade = self.positions.get(user)
            if not trade:
                return False
            close_ratio = float(trade.tp1_close_ratio or 0.5)
            trade.remaining_quantity = remaining_quantity
            trade.exit_stage = 1
            trade.status = "partial"
            fee_buffer = entry_price * 0.001
            if trade.side == "buy":
                breakeven_stop = entry_price + fee_buffer
            else:
                breakeven_stop = entry_price - fee_buffer
            trade.stop_loss = breakeven_stop
            trade.breakeven_moved = True
            if user in self._trailing_stop:
                ts = self._trailing_stop[user]
                if trade.side == "buy":
                    ts["trailing_stop"] = max(ts.get("trailing_stop", 0), breakeven_stop)
                else:
                    ts["trailing_stop"] = min(ts.get("trailing_stop", float("inf")), breakeven_stop)
                ts["activated"] = True
            self._tp1_order_placed[user] = False
            self._redis_save_position(user, trade)
            self._redis_save_aux(user)
            logging.info(
                f"[{user}] TP1 已由交易所触发，已同步: 剩余={remaining_quantity:.4f}, "
                f"保本止损={breakeven_stop:.2f}"
            )
            return True

    def increment_kline(self):
        self.kline_count += 1

    def is_in_cooldown(self, user: str) -> bool:
        import time
        cooldown_end = self.cooldown_until.get(user)
        if not cooldown_end:
            return False
        current_time = time.time()
        if current_time < cooldown_end:
            return True
        self.cooldown_until[user] = None
        return False

    def set_cooldown(self, user: str, cooldown_bars: int = 3, kline_interval_seconds: int = 300):
        import time
        cooldown_seconds = cooldown_bars * kline_interval_seconds
        self.cooldown_until[user] = time.time() + cooldown_seconds
        self._redis_save_aux(user)
        logging.info(f"⏳ [{user}] 启动冷却期: {cooldown_bars} 根K线 ({cooldown_seconds}秒)")

    def should_allow_reversal(
        self, user: str, new_signal_strength: float, reversal_threshold: float = 1.2
    ) -> bool:
        trade = self.positions.get(user)
        if not trade:
            return True
        current_strength = getattr(trade, "signal_strength", None) or 0.0
        if current_strength == 0.0:
            return True
        return new_signal_strength >= current_strength * reversal_threshold

    def _stats_from_closed(
        self, user: Optional[str] = None, is_observe: Optional[bool] = None
    ) -> Dict[str, Any]:
        """从内存已平仓记录计算统计"""
        with self._lock:
            items = [
                t
                for t in self._closed_trades
                if t.status == "closed" and (t.pnl is not None)
            ]
            if user is not None:
                items = [t for t in items if t.user == user]
            if is_observe is not None:
                items = [t for t in items if getattr(t, "is_observe", True) == is_observe]

        if not items:
            return {
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "avg_pnl": 0.0,
                "max_profit": 0.0,
                "max_loss": 0.0,
            }
        total = len(items)
        winning = sum(1 for t in items if (t.pnl or 0) > 0)
        losing = sum(1 for t in items if (t.pnl or 0) <= 0)
        pnls = [t.pnl for t in items if t.pnl is not None]
        total_pnl = sum(pnls)
        return {
            "total_trades": total,
            "winning_trades": winning,
            "losing_trades": losing,
            "win_rate": (winning / total * 100) if total > 0 else 0.0,
            "total_pnl": float(total_pnl),
            "avg_pnl": float(total_pnl / total) if total > 0 else 0.0,
            "max_profit": float(max(pnls)) if pnls else 0.0,
            "max_loss": float(min(pnls)) if pnls else 0.0,
        }

    def get_statistics(
        self, user: Optional[str] = None, is_observe: Optional[bool] = None
    ) -> Dict[str, Any]:
        """获取交易统计（基于当前进程已平仓记录）"""
        return self._stats_from_closed(user=user, is_observe=is_observe)

    def get_user_stats(
        self, user: str, is_observe: Optional[bool] = None
    ) -> Dict[str, Any]:
        return self.get_statistics(user=user, is_observe=is_observe)
