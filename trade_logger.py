"""
交易日志记录器 - PostgreSQL 持久化版本（优化版 v3）

优化内容：
- 清理 Trade 模型中的冗余字段
- 统一使用 SQLAlchemy 处理数据
- 使用 SQL 聚合函数优化统计查询
- 移除废弃的 dataclass 转换逻辑
- 添加并发锁保护内存缓存
"""

import logging
import threading
from datetime import datetime
from typing import Dict, Optional
from contextlib import contextmanager, nullcontext

from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean, DateTime,
    func, case
)
from sqlalchemy.orm import sessionmaker, declarative_base
from config import DATABASE_URL, SAVE_TRADES_TO_DB


Base = declarative_base()


class Trade(Base):
    """
    交易记录数据库模型（精简版）
    
    字段说明：
    - 基础字段：id, user, signal, side
    - 价格字段：entry_price, quantity, stop_loss, take_profit
    - 出场字段：exit_price, exit_reason, exit_timestamp
    - 盈亏字段：pnl, pnl_percent
    - 状态字段：status, exit_stage
    - 分批止盈：tp1_price, tp2_price, remaining_quantity
    - 上下文字段：market_state, tight_channel_score, signal_strength
    - 时间戳：created_at, updated_at
    """
    __tablename__ = 'trades'
    
    # 主键
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # 用户和信号
    user = Column(String(50), nullable=False, index=True)
    signal = Column(String(50), nullable=False)
    side = Column(String(10), nullable=False)
    
    # 价格和数量
    entry_price = Column(Float, nullable=False)
    quantity = Column(Float, nullable=False)
    stop_loss = Column(Float, nullable=False)
    take_profit = Column(Float, nullable=False)
    
    # 出场信息
    exit_price = Column(Float, nullable=True)
    exit_reason = Column(String(50), nullable=True)
    exit_timestamp = Column(DateTime, nullable=True)
    
    # 盈亏
    pnl = Column(Float, nullable=True)
    pnl_percent = Column(Float, nullable=True)
    
    # 状态: 'open', 'partial', 'closed'
    status = Column(String(20), nullable=False, default="open", index=True)
    
    # 分批止盈
    exit_stage = Column(Integer, default=0)  # 0:未出场, 1:半仓, 2:全仓
    tp1_price = Column(Float, nullable=True)
    tp2_price = Column(Float, nullable=True)
    remaining_quantity = Column(Float, nullable=True)
    
    # Breakeven 状态
    breakeven_moved = Column(Boolean, default=False)
    
    # 追踪止损持久化（问题3修复）
    trailing_stop_price = Column(Float, nullable=True)  # 当前追踪止损价格
    trailing_stop_activated = Column(Boolean, default=False)  # 是否已激活追踪止损
    trailing_max_profit_r = Column(Float, nullable=True)  # 最大盈利（以R为单位）
    original_stop_loss = Column(Float, nullable=True)  # 原始止损价（用于计算R）
    
    # 动态分批出场参数（Al Brooks 优化）
    tp1_close_ratio = Column(Float, default=0.5)  # TP1 平仓比例（默认50%，Climax时75%）
    is_climax_bar = Column(Boolean, default=False)  # 是否是 Climax 信号棒
    
    # 双止损配置（Al Brooks 风险管理）
    # soft_stop_loss = stop_loss（基于 K 线收盘价判断）
    # hard_stop_loss = 挂在币安的止损单价格（比 soft_stop_loss 更宽松，作为保险）
    hard_stop_loss = Column(Float, nullable=True)  # 硬止损价格（挂单价格）
    
    # 市场上下文
    market_state = Column(String(50), nullable=True)
    tight_channel_score = Column(Float, nullable=True)
    signal_strength = Column(Float, nullable=True)
    
    # 交易模式：区分观察模式和实盘模式
    is_observe = Column(Boolean, nullable=False, default=True, index=True)
    
    # 时间戳（使用 DateTime，无冗余字符串字段）
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def __repr__(self):
        return f"<Trade(id={self.id}, user='{self.user}', signal='{self.signal}', status='{self.status}')>"


class TradeLogger:
    """
    交易日志记录器
    
    特性：
    - PostgreSQL 实时持久化
    - 程序重启自动恢复持仓
    - 分批止盈和保本止损
    - SQL 聚合函数优化统计
    - 上下文管理器防止连接泄露
    """

    def __init__(self, db_url: str = None):
        self.db_url = db_url or DATABASE_URL
        self.engine = create_engine(
            self.db_url,
            echo=False,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            pool_recycle=3600,
        )
        self.SessionLocal = sessionmaker(bind=self.engine)
        
        # 创建表
        Base.metadata.create_all(self.engine)
        
        db_display = self.db_url.split('@')[-1] if '@' in self.db_url else 'localhost'
        logging.info(f"✅ 数据库连接成功: {db_display}")
        
        # 并发锁（保护内存缓存）
        self._lock = threading.RLock()
        
        # 内存缓存
        self.positions: Dict[str, Optional[Trade]] = {}
        
        # ========== 延迟写入优化 ==========
        # 避免每次价格检查都写入数据库，只在状态变化时写入
        self._dirty_trades: Dict[str, bool] = {}  # 标记需要持久化的交易
        self._last_db_sync: float = 0  # 上次数据库同步时间
        self.DB_SYNC_INTERVAL = 5.0  # 最小同步间隔（秒）
        
        # TP2 订单状态跟踪（实盘模式下，TP1 触发后需要挂 TP2 订单）
        self._tp2_order_placed: Dict[str, bool] = {}
        # TP1 订单状态：开仓后挂 TP1 止盈单，TP1 触发后由程序决定移动止盈止损
        self._tp1_order_placed: Dict[str, bool] = {}
        
        # Al Brooks 追踪止损状态
        # 格式: {user: {"trailing_stop": float, "max_profit": float, "activated": bool}}
        self._trailing_stop: Dict[str, Dict] = {}
        
        # 追踪止损参数（Al Brooks 理念优化）
        # Al Brooks: "让利润奔跑，但要保护已有利润"
        self.TRAILING_ACTIVATION_R = 0.8  # 激活阈值：盈利 0.8R 时激活（提前保护）
        self.TRAILING_DISTANCE_R = 0.5    # 追踪距离：保护盈利的一半（0.5R）
        
        # 冷却期管理
        self.cooldown_until: Dict[str, Optional[int]] = {}
        self.kline_count: int = 0
        
        # 从数据库恢复持仓
        self.sync_from_db()

    @contextmanager
    def session_scope(self):
        """数据库会话上下文管理器"""
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            logging.error(f"数据库操作失败，已回滚: {e}")
            raise
        finally:
            session.close()

    def sync_from_db(self):
        """从数据库恢复未平仓持仓"""
        if not SAVE_TRADES_TO_DB:
            logging.info("📊 数据库保存已禁用，跳过数据库恢复")
            return
            
        with self.session_scope() as session:
            try:
                open_trades = session.query(Trade).filter(
                    Trade.status.in_(['open', 'partial'])
                ).all()
                
                if not open_trades:
                    logging.info("📊 数据库中没有未平仓的持仓")
                    return
                
                for trade in open_trades:
                    session.expunge(trade)
                    self.positions[trade.user] = trade
                    
                    tp1_str = f"{trade.tp1_price:.2f}" if trade.tp1_price else "N/A"
                    tp2_str = f"{trade.tp2_price:.2f}" if trade.tp2_price else "N/A"
                    
                    logging.info(
                        f"🔄 恢复持仓 [{trade.user}]: {trade.signal} {trade.side} @ {trade.entry_price:.2f}, "
                        f"TP1={tp1_str}, TP2={tp2_str}, 状态={trade.status}"
                    )
                
                logging.info(f"✅ 成功恢复 {len(open_trades)} 个持仓")
                
            except Exception as e:
                logging.error(f"❌ 从数据库恢复持仓失败: {e}", exc_info=True)

    def recover_from_binance_position(
        self,
        user: str,
        position_info: Dict,
        current_price: float,
        atr: Optional[float] = None,
    ) -> Optional[Trade]:
        """
        根据币安真实持仓恢复交易状态（网络断开重启时使用）
        
        Args:
            user: 用户名
            position_info: 币安持仓信息（从 get_position_info 获取）
            current_price: 当前价格（用于计算止损止盈）
            atr: ATR 值（可选，用于计算止损距离）
        
        Returns:
            Trade: 恢复的交易记录，如果失败返回 None
        """
        with self._lock:
            # 如果已有持仓记录，先清除
            if self.positions.get(user):
                logging.warning(f"[{user}] 已有持仓记录，将被币安真实持仓覆盖")
                self.positions[user] = None
            
            try:
                position_amt = position_info["positionAmt"]
                entry_price = position_info["entryPrice"]
                
                # 确定方向
                if position_amt > 0:
                    side = "buy"
                    quantity = position_amt
                else:
                    side = "sell"
                    quantity = abs(position_amt)
                
                # 计算止损距离（使用策略标准）
                if atr and atr > 0:
                    # 使用 ATR 计算止损距离（策略标准：1.5-3.0x ATR）
                    stop_distance = atr * 1.5  # 保守使用 1.5x ATR
                else:
                    # 使用入场价的 1% 作为止损距离
                    stop_distance = entry_price * 0.01
                
                # 计算止损价格
                if side == "buy":
                    stop_loss = entry_price - stop_distance
                else:
                    stop_loss = entry_price + stop_distance
                
                # 计算风险（R）= 入场价到止损的距离
                risk = stop_distance
                
                # 使用策略标准计算 TP1 和 TP2（Al Brooks 风格）
                # TP1 = 1R（大多数信号类型的默认值）
                # TP2 = 2R（保守估计）
                tp1_multiplier = 1.0  # 1R
                tp2_multiplier = 2.0  # 2R
                
                if side == "buy":
                    tp1_price = entry_price + risk * tp1_multiplier
                    tp2_price = entry_price + risk * tp2_multiplier
                    take_profit = tp2_price  # 总止盈目标
                else:
                    tp1_price = entry_price - risk * tp1_multiplier
                    tp2_price = entry_price - risk * tp2_multiplier
                    take_profit = tp2_price  # 总止盈目标
                
                # 创建内存交易记录
                from dataclasses import dataclass
                from typing import Optional as Opt
                
                @dataclass
                class MemoryTrade:
                    id: int = 0
                    user: str = ""
                    signal: str = "Recovered"
                    side: str = ""
                    entry_price: float = 0.0
                    quantity: float = 0.0
                    stop_loss: float = 0.0
                    take_profit: float = 0.0
                    status: str = "open"
                    exit_stage: int = 0
                    tp1_price: Opt[float] = None
                    tp2_price: Opt[float] = None
                    remaining_quantity: float = 0.0
                    breakeven_moved: bool = False
                    original_stop_loss: float = 0.0
                    trailing_stop_price: Opt[float] = None
                    trailing_stop_activated: bool = False
                    trailing_max_profit_r: Opt[float] = None
                    market_state: Opt[str] = None
                    tight_channel_score: Opt[float] = None
                    signal_strength: Opt[float] = None
                    is_observe: bool = False
                    tp1_close_ratio: float = 0.5  # 默认 50% 止盈
                    is_climax_bar: bool = False
                    hard_stop_loss: Opt[float] = None
                
                trade = MemoryTrade(
                    user=user,
                    signal="Recovered",
                    side=side,
                    entry_price=entry_price,
                    quantity=quantity,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    status="open",
                    exit_stage=0,
                    tp1_price=tp1_price,  # 使用策略标准 1R
                    tp2_price=tp2_price,  # 使用策略标准 2R
                    remaining_quantity=quantity,
                    breakeven_moved=False,
                    original_stop_loss=stop_loss,
                    trailing_stop_price=None,
                    trailing_stop_activated=False,
                    trailing_max_profit_r=None,
                    market_state=None,
                    tight_channel_score=None,
                    signal_strength=None,
                    is_observe=False,
                    tp1_close_ratio=0.5,  # 默认 50% 止盈
                    is_climax_bar=False,
                    hard_stop_loss=None,
                )
                
                self.positions[user] = trade
                self._tp2_order_placed[user] = False
                
                # 检查当前价格是否已经达到 TP1（恢复时立即检查）
                tp1_already_hit = False
                if side == "buy":
                    tp1_already_hit = current_price >= tp1_price
                else:
                    tp1_already_hit = current_price <= tp1_price
                
                if tp1_already_hit:
                    logging.info(
                        f"🔄 从币安恢复持仓 [{user}]: {side.upper()} {quantity:.6f} BTC @ {entry_price:.2f}, "
                        f"止损={stop_loss:.2f}, TP1={tp1_price:.2f}(1R), TP2={tp2_price:.2f}(2R), "
                        f"当前价={current_price:.2f} 已超过 TP1，将在下个周期触发 50% 止盈"
                    )
                else:
                    logging.info(
                        f"🔄 从币安恢复持仓 [{user}]: {side.upper()} {quantity:.6f} BTC @ {entry_price:.2f}, "
                        f"止损={stop_loss:.2f}, TP1={tp1_price:.2f}(1R), TP2={tp2_price:.2f}(2R), "
                        f"当前价={current_price:.2f}, 未实现盈亏={position_info.get('unRealizedProfit', 0):.2f} USDT"
                    )
                
                return trade
                
            except Exception as e:
                logging.error(f"❌ 从币安恢复持仓失败 [{user}]: {e}", exc_info=True)
                return None
    
    def sync_dirty_trades(self, force: bool = False) -> int:
        """
        批量同步脏数据到数据库（延迟写入优化）
        
        Args:
            force: 是否强制同步（忽略时间间隔）
        
        Returns:
            int: 同步的交易数量
        """
        import time
        current_time = time.time()
        
        # 检查同步间隔
        if not force and (current_time - self._last_db_sync) < self.DB_SYNC_INTERVAL:
            return 0
        
        with self._lock:
            dirty_users = [u for u, dirty in self._dirty_trades.items() if dirty]
            if not dirty_users:
                return 0
            
            synced = 0
            with self.session_scope() as session:
                for user in dirty_users:
                    trade = self.positions.get(user)
                    if trade:
                        session.merge(trade)
                        self._dirty_trades[user] = False
                        synced += 1
            
            self._last_db_sync = current_time
            if synced > 0:
                logging.debug(f"📊 批量同步 {synced} 个交易到数据库")
            
            return synced
    
    def mark_dirty(self, user: str):
        """标记交易为脏数据（需要同步）"""
        self._dirty_trades[user] = True

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
        is_observe: bool = True,  # 默认为观察模式
        tp1_close_ratio: float = 0.5,  # TP1 平仓比例（默认50%，Climax时75%）
        is_climax_bar: bool = False,  # 是否是 Climax 信号棒
        hard_stop_loss: Optional[float] = None,  # 硬止损价格（挂单价格，比 stop_loss 更宽松）
    ) -> Trade:
        """开仓并持久化（线程安全）"""
        # 将 numpy 类型转换为 Python 原生类型（PostgreSQL 不支持 np.float64）
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
            # 已有持仓则先平仓（注意：close_position 内部也会获取锁，使用 RLock 避免死锁）
            if self.positions.get(user):
                self._close_position_unlocked(user, entry_price, "manual", "新信号开仓")

            if SAVE_TRADES_TO_DB:
                # 保存到数据库
                with self.session_scope() as session:
                    trade = Trade(
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
                        # 追踪止损初始化（问题3修复）
                        original_stop_loss=stop_loss,  # 保存原始止损（软止损）
                        trailing_stop_price=None,
                        trailing_stop_activated=False,
                        trailing_max_profit_r=None,
                        # 市场上下文
                        market_state=market_state,
                        tight_channel_score=tight_channel_score,
                        signal_strength=signal_strength,
                        is_observe=is_observe,  # 记录交易模式
                        # 动态分批出场参数
                        tp1_close_ratio=tp1_close_ratio,
                        is_climax_bar=is_climax_bar,
                        # 双止损配置
                        hard_stop_loss=hard_stop_loss,  # 硬止损价格（挂单价格）
                    )

                    session.add(trade)
                    session.flush()
                    session.expunge(trade)

                    self.positions[user] = trade
                    
                    # 重置 TP2/TP1 订单标记（新开仓）
                    self._tp2_order_placed[user] = False
                    self._tp1_order_placed[user] = False

                    logging.info(
                        f"用户 {user} 开仓 [ID={trade.id}]: {signal} {side} @ {entry_price:.2f}, "
                        f"止损={stop_loss:.2f}, TP1={tp1_price or take_profit:.2f}, TP2={tp2_price or take_profit:.2f}"
                    )
                    
                    return trade
            else:
                # 不保存到数据库，只创建内存对象
                from dataclasses import dataclass
                from typing import Optional as Opt
                
                @dataclass
                class MemoryTrade:
                    id: int = 0
                    user: str = ""
                    signal: str = ""
                    side: str = ""
                    entry_price: float = 0.0
                    quantity: float = 0.0
                    stop_loss: float = 0.0
                    take_profit: float = 0.0
                    status: str = "open"
                    exit_stage: int = 0
                    tp1_price: Opt[float] = None
                    tp2_price: Opt[float] = None
                    remaining_quantity: float = 0.0
                    breakeven_moved: bool = False
                    original_stop_loss: float = 0.0
                    trailing_stop_price: Opt[float] = None
                    trailing_stop_activated: bool = False
                    trailing_max_profit_r: Opt[float] = None
                    market_state: Opt[str] = None
                    tight_channel_score: Opt[float] = None
                    signal_strength: Opt[float] = None
                    is_observe: bool = True
                    tp1_close_ratio: float = 0.5
                    is_climax_bar: bool = False
                    hard_stop_loss: Opt[float] = None
                
                trade = MemoryTrade(
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
                
                # 重置 TP2/TP1 订单标记（新开仓）
                self._tp2_order_placed[user] = False
                self._tp1_order_placed[user] = False

                logging.info(
                    f"用户 {user} 开仓（内存）: {signal} {side} @ {entry_price:.2f}, "
                    f"止损={stop_loss:.2f}, TP1={tp1_price or take_profit:.2f}, TP2={tp2_price or take_profit:.2f}"
                )
                
                return trade

    def close_position(
        self, user: str, exit_price: float, exit_reason: str, note: str = ""
    ) -> Optional[Trade]:
        """平仓并持久化（线程安全）"""
        with self._lock:
            return self._close_position_unlocked(user, exit_price, exit_reason, note)
    
    def _close_position_unlocked(
        self, user: str, exit_price: float, exit_reason: str, note: str = ""
    ) -> Optional[Trade]:
        """平仓内部方法（需在持有锁的情况下调用）"""
        trade = self.positions.get(user)
        if not trade:
            return None

        # 将 numpy 类型转换为 Python 原生类型
        exit_price = float(exit_price)

        # 更新交易对象
        trade.exit_price = exit_price
        trade.exit_reason = exit_reason
        trade.exit_timestamp = datetime.utcnow()
        trade.status = "closed"
        trade.exit_stage = 2

        # 计算盈亏
        qty = trade.remaining_quantity or trade.quantity
        
        if trade.side == "buy":
            final_pnl = (exit_price - trade.entry_price) * qty
        else:
            final_pnl = (trade.entry_price - exit_price) * qty

        # 累加 TP1 盈利（如果有）
        if trade.exit_stage >= 1 and trade.tp1_price:
            half_qty = trade.quantity * 0.5
            if trade.side == "buy":
                tp1_pnl = (trade.tp1_price - trade.entry_price) * half_qty
            else:
                tp1_pnl = (trade.entry_price - trade.tp1_price) * half_qty
            
            trade.pnl = tp1_pnl + final_pnl
        else:
            trade.pnl = final_pnl
        
        # 防止除以零
        cost_basis = (trade.entry_price or 0) * (trade.quantity or 0)
        if cost_basis > 0:
            trade.pnl_percent = (trade.pnl / cost_basis) * 100
        else:
            trade.pnl_percent = 0.0
            trade_id = getattr(trade, 'id', 'N/A')
            logging.warning(f"用户 {user} 交易 [ID={trade_id}] 成本为零，无法计算百分比盈亏")

        # 如果启用数据库保存，则保存到数据库
        if SAVE_TRADES_TO_DB:
            with self.session_scope() as session:
                session.merge(trade)
                trade_id = getattr(trade, 'id', 'N/A')
                logging.info(
                    f"用户 {user} 平仓 [ID={trade_id}]: {exit_reason} @ {exit_price:.2f}, "
                    f"盈亏={trade.pnl:.4f} USDT ({trade.pnl_percent:.2f}%) {note}"
                )
        else:
            trade_id = getattr(trade, 'id', 'N/A')
            logging.info(
                f"用户 {user} 平仓（内存）[ID={trade_id}]: {exit_reason} @ {exit_price:.2f}, "
                f"盈亏={trade.pnl:.4f} USDT ({trade.pnl_percent:.2f}%) {note}"
            )

        # 止损亏损启动冷却期
        if exit_reason == "stop_loss" and trade.pnl and trade.pnl < 0:
            self.set_cooldown(user, cooldown_bars=3)

        self.positions[user] = None
        return trade

    def update_trade_with_actual_pnl(
        self,
        user: str,
        actual_exit_price: float,
        commission: float = 0.0,
        open_commission: float = 0.0,
    ) -> bool:
        """
        更新交易记录的实际盈亏（使用币安真实成交数据）
        
        在平仓后调用，用真实成交价和手续费更新数据库记录
        
        Args:
            user: 用户名
            actual_exit_price: 实际成交价
            commission: 平仓手续费
            open_commission: 开仓手续费（可选，如果已知）
        
        Returns:
            bool: 是否更新成功
        """
        # 如果未启用数据库保存，直接返回
        if not SAVE_TRADES_TO_DB:
            return True
        
        # 将 numpy 类型转换为 Python 原生类型
        actual_exit_price = float(actual_exit_price)
        commission = float(commission)
        open_commission = float(open_commission)
        
        with self.session_scope() as session:
            try:
                # 查找最近关闭的交易
                trade = session.query(Trade).filter(
                    Trade.user == user,
                    Trade.status == "closed"
                ).order_by(Trade.updated_at.desc()).first()
                
                if not trade:
                    logging.warning(f"[{user}] 未找到最近的已关闭交易，无法更新盈亏")
                    return False
                
                # 更新实际出场价
                old_exit_price = trade.exit_price
                trade.exit_price = actual_exit_price
                
                # 重新计算盈亏（包含手续费）
                qty = trade.remaining_quantity or trade.quantity
                
                if trade.side == "buy":
                    raw_pnl = (actual_exit_price - float(trade.entry_price)) * float(qty)
                else:
                    raw_pnl = (float(trade.entry_price) - actual_exit_price) * float(qty)
                
                # 扣除手续费（开仓+平仓）
                total_commission = commission + open_commission
                trade.pnl = raw_pnl - total_commission
                
                # 重新计算百分比
                cost_basis = float(trade.entry_price) * float(trade.quantity)
                if cost_basis > 0:
                    trade.pnl_percent = (trade.pnl / cost_basis) * 100
                
                session.merge(trade)
                
                logging.info(
                    f"[{user}] 更新实际盈亏: 出场价 {old_exit_price:.2f} → {actual_exit_price:.2f}, "
                    f"手续费={total_commission:.4f}, 实际盈亏={trade.pnl:.4f} USDT ({trade.pnl_percent:.2f}%)"
                )
                
                return True
                
            except Exception as e:
                logging.error(f"[{user}] 更新实际盈亏失败: {e}")
                return False

    def check_stop_loss_take_profit(self, user: str, current_price: float) -> Optional[Trade]:
        """
        检查止损止盈（Al Brooks 动态退出模式）
        
        Al Brooks 理念：
        1. 不预挂固定止盈单，通过 K 线监控动态退出
        2. 使用追踪止损保护利润
        3. 盈利 1R 后激活追踪止损
        4. TP1 触发后止损移至入场价（保本）
        """
        # 将 numpy 类型转换为 Python 原生类型
        current_price = float(current_price)
        
        with self._lock:
            trade = self.positions.get(user)
            if not trade:
                return None

            # 检查是否是 MemoryTrade（禁用数据库保存时）
            is_memory_trade = not hasattr(trade, '__table__') or trade.__class__.__name__ == 'MemoryTrade'
            
            # 如果禁用数据库保存，使用空上下文管理器
            if is_memory_trade or not SAVE_TRADES_TO_DB:
                session_context = nullcontext()
            else:
                session_context = self.session_scope()
            
            with session_context as session:
                # 计算风险（R）= 入场价到止损的距离
                initial_risk = abs(float(trade.entry_price) - float(trade.stop_loss))
                if initial_risk == 0:
                    initial_risk = float(trade.entry_price) * 0.01  # 默认 1%
                
                # 计算当前盈利（以 R 为单位）
                if trade.side == "buy":
                    current_profit = current_price - float(trade.entry_price)
                else:
                    current_profit = float(trade.entry_price) - current_price
                
                profit_in_r = current_profit / initial_risk if initial_risk > 0 else 0
                
                # ========== Al Brooks 追踪止损逻辑（问题3修复：持久化）==========
                if user not in self._trailing_stop:
                    # 优先从数据库恢复追踪止损状态
                    if trade.trailing_stop_activated and trade.trailing_stop_price:
                        self._trailing_stop[user] = {
                            "trailing_stop": float(trade.trailing_stop_price),
                            "original_stop_loss": float(trade.original_stop_loss or trade.stop_loss),
                            "max_profit": float(trade.trailing_max_profit_r or 0.0),
                            "activated": True
                        }
                        logging.info(f"[{user}] 从数据库恢复追踪止损状态: {self._trailing_stop[user]}")
                    else:
                        # 初始化新的追踪止损状态
                        original_sl = float(trade.original_stop_loss or trade.stop_loss)
                        self._trailing_stop[user] = {
                            "trailing_stop": float(trade.stop_loss),
                            "original_stop_loss": original_sl,
                            "max_profit": 0.0,
                            "activated": False
                        }
                        # 保存原始止损到数据库
                        if not trade.original_stop_loss:
                            trade.original_stop_loss = original_sl
                            if session and SAVE_TRADES_TO_DB and not is_memory_trade:
                                session.merge(trade)
                
                ts_state = self._trailing_stop[user]
                
                # 使用原始止损计算风险，避免追踪止损更新导致的不一致
                original_risk = abs(float(trade.entry_price) - ts_state["original_stop_loss"])
                if original_risk == 0:
                    original_risk = initial_risk  # 回退到默认值
                
                # 重新计算盈利（使用原始风险）
                profit_in_r = current_profit / original_risk if original_risk > 0 else 0
                
                # 更新最大盈利
                if profit_in_r > ts_state["max_profit"]:
                    ts_state["max_profit"] = profit_in_r
                
                # 激活追踪止损条件：盈利超过 TRAILING_ACTIVATION_R 倍风险
                if not ts_state["activated"] and profit_in_r >= self.TRAILING_ACTIVATION_R:
                    ts_state["activated"] = True
                    # 初始追踪止损 = 入场价 + (当前盈利 - 追踪距离)
                    trailing_distance = original_risk * self.TRAILING_DISTANCE_R
                    if trade.side == "buy":
                        ts_state["trailing_stop"] = float(trade.entry_price) + current_profit - trailing_distance
                    else:
                        ts_state["trailing_stop"] = float(trade.entry_price) - current_profit + trailing_distance
                    
                    # 问题3修复：持久化追踪止损状态
                    trade.trailing_stop_activated = True
                    trade.trailing_stop_price = ts_state["trailing_stop"]
                    trade.trailing_max_profit_r = profit_in_r
                    if session and SAVE_TRADES_TO_DB and not is_memory_trade:
                        session.merge(trade)
                    
                    logging.info(
                        f"📈 [{user}] 追踪止损已激活！盈利={profit_in_r:.2f}R, "
                        f"追踪止损={ts_state['trailing_stop']:.2f}"
                    )
                
                # 更新追踪止损（只向有利方向移动）
                if ts_state["activated"]:
                    trailing_distance = original_risk * self.TRAILING_DISTANCE_R
                    ts_updated = False
                    
                    if trade.side == "buy":
                        new_trailing_stop = current_price - trailing_distance
                        if new_trailing_stop > ts_state["trailing_stop"]:
                            ts_state["trailing_stop"] = new_trailing_stop
                            ts_updated = True
                    else:
                        new_trailing_stop = current_price + trailing_distance
                        if new_trailing_stop < ts_state["trailing_stop"]:
                            ts_state["trailing_stop"] = new_trailing_stop
                            ts_updated = True
                    
                    # 更新最大盈利
                    if profit_in_r > ts_state["max_profit"]:
                        ts_state["max_profit"] = profit_in_r
                        ts_updated = True
                    
                    # 延迟写入优化：只更新内存，标记为脏数据
                    # 数据库写入由 sync_dirty_trades() 批量处理
                    if ts_updated:
                        trade.stop_loss = ts_state["trailing_stop"]
                        trade.trailing_stop_price = ts_state["trailing_stop"]
                        trade.trailing_max_profit_r = ts_state["max_profit"]
                        self.mark_dirty(user)  # 延迟写入
                
                # ========== TP1 触发（阶段0 → 1）==========
                if trade.exit_stage == 0 and trade.tp1_price:
                    tp1_hit = (trade.side == "buy" and current_price >= float(trade.tp1_price)) or \
                              (trade.side == "sell" and current_price <= float(trade.tp1_price))
                    
                    # 若已挂 TP1 止盈单，由交易所执行 TP1，不在此触发程序平仓；user_worker 会检测成交并 sync_after_tp1_filled
                    if tp1_hit and self._tp1_order_placed.get(user, False):
                        return None
                    
                    if tp1_hit:
                        # 使用动态平仓比例（默认 50%，Climax 时 75%）
                        close_ratio = float(trade.tp1_close_ratio or 0.5)
                        close_qty = float(trade.quantity) * close_ratio
                        trade.remaining_quantity = float(trade.quantity) - close_qty
                        trade.exit_stage = 1
                        trade.status = "partial"
                        
                        # 动态保本：止损移至入场价 + 手续费覆盖（0.04% × 2 = 0.08%）
                        entry_price = float(trade.entry_price)
                        fee_buffer = entry_price * 0.001  # 0.1% 缓冲（覆盖手续费+滑点）
                        if trade.side == "buy":
                            breakeven_stop = entry_price + fee_buffer  # 做多：入场价上方
                        else:
                            breakeven_stop = entry_price - fee_buffer  # 做空：入场价下方
                        
                        trade.stop_loss = breakeven_stop
                        trade.breakeven_moved = True
                        
                        if session and SAVE_TRADES_TO_DB and not is_memory_trade:
                            session.merge(trade)
                        
                        # 更新追踪止损状态（不允许后退）
                        if trade.side == "buy":
                            # 做多：取追踪止损和保本价中的较大值
                            ts_state["trailing_stop"] = max(ts_state["trailing_stop"], breakeven_stop)
                        else:
                            # 做空：取追踪止损和保本价中的较小值
                            ts_state["trailing_stop"] = min(ts_state["trailing_stop"], breakeven_stop)
                        ts_state["activated"] = True
                        
                        if trade.side == "buy":
                            tp1_pnl = (float(trade.tp1_price) - entry_price) * close_qty
                        else:
                            tp1_pnl = (entry_price - float(trade.tp1_price)) * close_qty
                        
                        close_pct = int(close_ratio * 100)
                        logging.info(
                            f"🎯 [{user}] TP1触发！平仓{close_pct}% @ {float(trade.tp1_price):.2f}, "
                            f"盈利={tp1_pnl:.4f}, 保本止损={breakeven_stop:.2f}"
                            + (f" [Climax信号棒，加大平仓比例]" if trade.is_climax_bar else "")
                        )
                        
                        # 标记需要通知实盘平仓（如果存在 TP2）
                        if trade.tp2_price:
                            self._tp2_order_placed[user] = False
                        
                        # 返回 TP1 操作信息，让 user_worker 立即执行
                        return {
                            "action": "tp1",
                            "trade": trade,
                            "close_quantity": close_qty,
                            "close_price": float(trade.tp1_price),
                            "new_stop_loss": breakeven_stop,
                            "tp2_price": float(trade.tp2_price) if trade.tp2_price else None,
                        }
                
                # ========== TP2 触发（阶段1 → 2）==========
                if trade.exit_stage == 1 and trade.tp2_price:
                    tp2_hit = (trade.side == "buy" and current_price >= float(trade.tp2_price)) or \
                              (trade.side == "sell" and current_price <= float(trade.tp2_price))
                    
                    if tp2_hit:
                        # 清理追踪止损状态
                        if user in self._trailing_stop:
                            del self._trailing_stop[user]
                        return self._close_position_unlocked(user, float(trade.tp2_price), "tp2")

                # ========== Breakeven 逻辑（无 TP1 时）==========
                if not trade.tp1_price and not trade.breakeven_moved:
                    breakeven_hit = (trade.side == "buy" and current_price >= float(trade.entry_price) + initial_risk) or \
                                    (trade.side == "sell" and current_price <= float(trade.entry_price) - initial_risk)
                    
                    if breakeven_hit:
                        trade.stop_loss = float(trade.entry_price)
                        trade.breakeven_moved = True
                        if session and SAVE_TRADES_TO_DB and not is_memory_trade:
                            session.merge(trade)
                        
                        # 更新追踪止损状态
                        ts_state["trailing_stop"] = float(trade.entry_price)
                        
                        logging.info(f"💡 [{user}] Breakeven触发！止损移至入场价: {float(trade.entry_price):.2f}")

                # ========== 止损检查（包含追踪止损）==========
                effective_stop = ts_state["trailing_stop"] if ts_state["activated"] else float(trade.stop_loss)
                
                stop_hit = (trade.side == "buy" and current_price <= effective_stop) or \
                           (trade.side == "sell" and current_price >= effective_stop)
                
                if stop_hit:
                    if ts_state["activated"] and ts_state["max_profit"] > 0:
                        reason = "trailing_stop"
                    elif trade.breakeven_moved and float(trade.stop_loss) == float(trade.entry_price):
                        reason = "breakeven_stop"
                    else:
                        reason = "stop_loss"
                    
                    # 清理追踪止损状态
                    if user in self._trailing_stop:
                        del self._trailing_stop[user]
                    
                    return self._close_position_unlocked(user, effective_stop, reason)
                
                # ========== 传统止盈（无 TP1 时）==========
                if not trade.tp1_price:
                    tp_hit = (trade.side == "buy" and current_price >= float(trade.take_profit)) or \
                             (trade.side == "sell" and current_price <= float(trade.take_profit))
                    
                    if tp_hit:
                        # 清理追踪止损状态
                        if user in self._trailing_stop:
                            del self._trailing_stop[user]
                        return self._close_position_unlocked(user, float(trade.take_profit), "take_profit")

                return None

    def needs_tp2_order(self, user: str) -> bool:
        """
        检查是否需要挂 TP2 订单
        
        Returns:
            bool: True 表示需要挂 TP2 订单
        """
        with self._lock:
            trade = self.positions.get(user)
            if not trade:
                return False
            
            # 需要挂 TP2 的条件：
            # 1. TP1 已触发（exit_stage == 1）
            # 2. 存在 TP2 价格
            # 3. 尚未挂 TP2 订单
            if (trade.exit_stage == 1 and trade.tp2_price and 
                not self._tp2_order_placed.get(user, False)):
                return True
            
            return False
    
    def mark_tp2_order_placed(self, user: str):
        """标记 TP2 订单已挂"""
        with self._lock:
            self._tp2_order_placed[user] = True

    def mark_tp1_order_placed(self, user: str):
        """标记 TP1 止盈单已挂（开仓后挂 TP1，TP1 触发后由程序决定移动止盈止损）"""
        with self._lock:
            self._tp1_order_placed[user] = True

    def tp1_order_placed(self, user: str) -> bool:
        """是否已挂 TP1 止盈单"""
        with self._lock:
            return bool(self._tp1_order_placed.get(user, False))

    def update_position_from_binance(
        self, user: str, quantity: float, entry_price: float
    ) -> bool:
        """
        使用币安真实持仓更新内部持仓状态与数量（开仓后调用）
        
        Args:
            user: 用户名
            quantity: 持仓数量（正数）
            entry_price: 入场均价
        
        Returns:
            是否更新成功
        """
        quantity = float(quantity)
        entry_price = float(entry_price)
        with self._lock:
            trade = self.positions.get(user)
            if not trade:
                return False
            trade.quantity = quantity
            trade.entry_price = entry_price
            trade.remaining_quantity = quantity
            self.mark_dirty(user)
            logging.info(
                f"[{user}] 已用币安真实持仓更新: 数量={quantity:.4f}, 入场价={entry_price:.2f}"
            )
            return True

    def needs_tp1_fill_sync(self, user: str) -> bool:
        """是否需要检测 TP1 成交并同步（已挂 TP1 单且尚未同步过）"""
        with self._lock:
            trade = self.positions.get(user)
            if not trade:
                return False
            return bool(self._tp1_order_placed.get(user, False))

    def sync_after_tp1_filled(
        self, user: str, remaining_quantity: float, entry_price: float
    ) -> bool:
        """
        TP1 由交易所触发后同步状态：剩余仓位、保本止损，之后由程序决定止盈止损。
        
        Args:
            user: 用户名
            remaining_quantity: 剩余持仓数量（币安当前持仓）
            entry_price: 当前持仓入场均价（可选，用于保本计算）
        
        Returns:
            是否同步成功
        """
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
            # 保本止损
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
            self.mark_dirty(user)
            logging.info(
                f"[{user}] TP1 已由交易所触发，已同步: 剩余={remaining_quantity:.4f}, "
                f"保本止损={breakeven_stop:.2f}，后续由程序决定止盈止损"
            )
            return True

    def increment_kline(self):
        """递增 K 线计数器（保留用于兼容）"""
        self.kline_count += 1

    def is_in_cooldown(self, user: str) -> bool:
        """
        检查用户是否在冷却期（问题4修复：使用时间戳）
        """
        import time
        cooldown_end = self.cooldown_until.get(user)
        if not cooldown_end:
            return False
        
        current_time = time.time()
        if current_time < cooldown_end:
            remaining = int(cooldown_end - current_time)
            # 每60秒打印一次（避免日志过多）
            if remaining % 60 == 0:
                logging.debug(f"[{user}] 冷却期剩余: {remaining}秒")
            return True
        
        self.cooldown_until[user] = None
        return False
    
    def set_cooldown(self, user: str, cooldown_bars: int = 3, kline_interval_seconds: int = 300):
        """
        设置冷却期（问题4修复：使用时间戳）
        
        Args:
            user: 用户名
            cooldown_bars: 冷却K线数（默认3根）
            kline_interval_seconds: K线周期秒数（默认5分钟=300秒）
        """
        import time
        cooldown_seconds = cooldown_bars * kline_interval_seconds
        self.cooldown_until[user] = time.time() + cooldown_seconds
        logging.info(f"⏳ [{user}] 启动冷却期: {cooldown_bars} 根K线 ({cooldown_seconds}秒)")
    
    def should_allow_reversal(
        self, user: str, new_signal_strength: float, reversal_threshold: float = 1.2
    ) -> bool:
        """检查是否允许反手"""
        trade = self.positions.get(user)
        if not trade:
            return True
        
        current_strength = trade.signal_strength or 0.0
        if current_strength == 0.0:
            return True
        
        return new_signal_strength >= current_strength * reversal_threshold

    def get_statistics(self, user: Optional[str] = None, is_observe: Optional[bool] = None) -> Dict:
        """
        获取交易统计（使用 SQL 聚合函数优化）
        
        直接在数据库层计算统计数据，避免将大量记录加载到内存
        
        问题7修复：包含 partial 状态的交易（TP1 已触发但 TP2 尚未触发）
        
        Args:
            user: 用户名（可选）
            is_observe: 过滤模式（True=观察模式，False=实盘模式，None=全部）
        """
        with self.session_scope() as session:
            try:
                # 构建基础查询（包含 closed 和 partial 状态）
                base_filter = Trade.status.in_(['closed', 'partial'])
                if user:
                    base_filter = base_filter & (Trade.user == user)
                if is_observe is not None:
                    base_filter = base_filter & (Trade.is_observe == is_observe)
                
                # 使用 SQL 聚合函数一次性获取所有统计
                stats = session.query(
                    func.count(Trade.id).label('total_trades'),
                    func.sum(case((Trade.pnl > 0, 1), else_=0)).label('winning_trades'),
                    func.sum(case((Trade.pnl <= 0, 1), else_=0)).label('losing_trades'),
                    func.coalesce(func.sum(Trade.pnl), 0).label('total_pnl'),
                    func.coalesce(func.avg(Trade.pnl), 0).label('avg_pnl'),
                    func.coalesce(func.max(Trade.pnl), 0).label('max_profit'),
                    func.coalesce(func.min(Trade.pnl), 0).label('max_loss'),
                ).filter(base_filter).first()
                
                total = stats.total_trades or 0
                winning = stats.winning_trades or 0
                
                return {
                    'total_trades': total,
                    'winning_trades': winning,
                    'losing_trades': stats.losing_trades or 0,
                    'win_rate': (winning / total * 100) if total > 0 else 0.0,
                    'total_pnl': float(stats.total_pnl or 0),
                    'avg_pnl': float(stats.avg_pnl or 0),
                    'max_profit': float(stats.max_profit or 0),
                    'max_loss': float(stats.max_loss or 0),
                }
                
            except Exception as e:
                logging.error(f"❌ 获取统计信息失败: {e}", exc_info=True)
                return {
                    'total_trades': 0,
                    'winning_trades': 0,
                    'losing_trades': 0,
                    'win_rate': 0.0,
                    'total_pnl': 0.0,
                    'avg_pnl': 0.0,
                    'max_profit': 0.0,
                    'max_loss': 0.0,
                }

    def get_user_stats(self, user: str, is_observe: Optional[bool] = None) -> Dict:
        """
        获取用户统计（兼容旧接口）
        
        Args:
            user: 用户名
            is_observe: 过滤模式（True=观察模式，False=实盘模式，None=全部）
        """
        return self.get_statistics(user, is_observe)
    
    def get_mode_stats(self, user: str) -> Dict:
        """
        获取用户在不同模式下的统计
        
        Returns:
            {
                'observe': {...},  # 观察模式统计
                'live': {...},     # 实盘模式统计
                'total': {...}     # 总计
            }
        """
        return {
            'observe': self.get_statistics(user, is_observe=True),
            'live': self.get_statistics(user, is_observe=False),
            'total': self.get_statistics(user, is_observe=None),
        }

    def print_statistics(self):
        """打印统计信息"""
        users = set(self.positions.keys())
        
        for user in users:
            stats = self.get_statistics(user)
            if stats['total_trades'] > 0:
                logging.info(
                    f"📊 [{user}] 统计: 总交易={stats['total_trades']}, "
                    f"胜率={stats['win_rate']:.2f}%, 总盈亏={stats['total_pnl']:.4f} USDT"
                )
