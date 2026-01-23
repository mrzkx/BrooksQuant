"""
交易日志记录器 - PostgreSQL 持久化版本（优化版 v3）

优化内容：
- 清理 Trade 模型中的冗余字段
- 统一使用 SQLAlchemy 处理数据
- 使用 SQL 聚合函数优化统计查询
- 移除废弃的 dataclass 转换逻辑
"""

import logging
from datetime import datetime
from typing import Dict, Optional
from contextlib import contextmanager

from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean, DateTime,
    func, case
)
from sqlalchemy.orm import sessionmaker, declarative_base
from config import DATABASE_URL


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
    
    # 市场上下文
    market_state = Column(String(50), nullable=True)
    tight_channel_score = Column(Float, nullable=True)
    signal_strength = Column(Float, nullable=True)
    
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
        
        # 内存缓存
        self.positions: Dict[str, Optional[Trade]] = {}
        
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
    ) -> Trade:
        """开仓并持久化"""
        # 已有持仓则先平仓
        if self.positions.get(user):
            self.close_position(user, float(entry_price), "manual", "新信号开仓")

        # 将 numpy 类型转换为 Python 原生类型（PostgreSQL 不支持 np.float64）
        entry_price = float(entry_price)
        quantity = float(quantity)
        stop_loss = float(stop_loss)
        take_profit = float(take_profit)
        signal_strength = float(signal_strength) if signal_strength is not None else None
        tp1_price = float(tp1_price) if tp1_price is not None else None
        tp2_price = float(tp2_price) if tp2_price is not None else None
        tight_channel_score = float(tight_channel_score) if tight_channel_score is not None else None

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
                market_state=market_state,
                tight_channel_score=tight_channel_score,
                signal_strength=signal_strength,
            )

            session.add(trade)
            session.flush()
            session.expunge(trade)

            self.positions[user] = trade

            logging.info(
                f"用户 {user} 开仓 [ID={trade.id}]: {signal} {side} @ {entry_price:.2f}, "
                f"止损={stop_loss:.2f}, TP1={tp1_price or take_profit:.2f}, TP2={tp2_price or take_profit:.2f}"
            )
            
            return trade

    def close_position(
        self, user: str, exit_price: float, exit_reason: str, note: str = ""
    ) -> Optional[Trade]:
        """平仓并持久化"""
        trade = self.positions.get(user)
        if not trade:
            return None

        # 将 numpy 类型转换为 Python 原生类型
        exit_price = float(exit_price)

        with self.session_scope() as session:
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
            
            trade.pnl_percent = (trade.pnl / (trade.entry_price * trade.quantity)) * 100

            session.merge(trade)

            logging.info(
                f"用户 {user} 平仓 [ID={trade.id}]: {exit_reason} @ {exit_price:.2f}, "
                f"盈亏={trade.pnl:.4f} USDT ({trade.pnl_percent:.2f}%) {note}"
            )

            # 止损亏损启动冷却期
            if exit_reason == "stop_loss" and trade.pnl and trade.pnl < 0:
                self.set_cooldown(user, cooldown_bars=3)

            self.positions[user] = None
            return trade

    def check_stop_loss_take_profit(self, user: str, current_price: float) -> Optional[Trade]:
        """检查止损止盈（支持分批止盈）"""
        trade = self.positions.get(user)
        if not trade:
            return None

        # 将 numpy 类型转换为 Python 原生类型
        current_price = float(current_price)

        with self.session_scope() as session:
            # TP1 触发（阶段0 → 1）
            if trade.exit_stage == 0 and trade.tp1_price:
                tp1_hit = (trade.side == "buy" and current_price >= trade.tp1_price) or \
                          (trade.side == "sell" and current_price <= trade.tp1_price)
                
                if tp1_hit:
                    half_qty = trade.quantity * 0.5
                    trade.remaining_quantity = trade.quantity - half_qty
                    trade.exit_stage = 1
                    trade.status = "partial"
                    trade.stop_loss = trade.entry_price
                    trade.breakeven_moved = True
                    
                    session.merge(trade)
                    
                    if trade.side == "buy":
                        tp1_pnl = (trade.tp1_price - trade.entry_price) * half_qty
                    else:
                        tp1_pnl = (trade.entry_price - trade.tp1_price) * half_qty
                    
                    logging.info(
                        f"🎯 [{user}] TP1触发！平仓50% @ {trade.tp1_price:.2f}, "
                        f"盈利={tp1_pnl:.4f}, 止损移至入场价"
                    )
                    return None
            
            # TP2 触发（阶段1 → 2）
            if trade.exit_stage == 1 and trade.tp2_price:
                tp2_hit = (trade.side == "buy" and current_price >= trade.tp2_price) or \
                          (trade.side == "sell" and current_price <= trade.tp2_price)
                
                if tp2_hit:
                    return self.close_position(user, trade.tp2_price, "tp2")

            # Breakeven 逻辑（无 TP1 时）
            if not trade.tp1_price and not trade.breakeven_moved:
                risk = abs(trade.entry_price - trade.stop_loss)
                
                breakeven_hit = (trade.side == "buy" and current_price >= trade.entry_price + risk) or \
                                (trade.side == "sell" and current_price <= trade.entry_price - risk)
                
                if breakeven_hit:
                    trade.stop_loss = trade.entry_price
                    trade.breakeven_moved = True
                    session.merge(trade)
                    
                    logging.info(f"💡 [{user}] Breakeven触发！止损移至入场价: {trade.entry_price:.2f}")

            # 止损检查
            stop_hit = (trade.side == "buy" and current_price <= trade.stop_loss) or \
                       (trade.side == "sell" and current_price >= trade.stop_loss)
            
            if stop_hit:
                reason = "breakeven_stop" if trade.breakeven_moved and trade.stop_loss == trade.entry_price else "stop_loss"
                return self.close_position(user, trade.stop_loss, reason)
            
            # 传统止盈（无 TP1 时）
            if not trade.tp1_price:
                tp_hit = (trade.side == "buy" and current_price >= trade.take_profit) or \
                         (trade.side == "sell" and current_price <= trade.take_profit)
                
                if tp_hit:
                    return self.close_position(user, trade.take_profit, "take_profit")

            return None

    def increment_kline(self):
        """递增 K 线计数器"""
        self.kline_count += 1

    def is_in_cooldown(self, user: str) -> bool:
        """检查用户是否在冷却期"""
        cooldown_end = self.cooldown_until.get(user)
        if not cooldown_end:
            return False
        
        if self.kline_count < cooldown_end:
            return True
        
        self.cooldown_until[user] = None
        return False
    
    def set_cooldown(self, user: str, cooldown_bars: int = 3):
        """设置冷却期"""
        self.cooldown_until[user] = self.kline_count + cooldown_bars
        logging.info(f"⏳ [{user}] 启动冷却期: {cooldown_bars} 根K线")
    
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

    def get_statistics(self, user: Optional[str] = None) -> Dict:
        """
        获取交易统计（使用 SQL 聚合函数优化）
        
        直接在数据库层计算统计数据，避免将大量记录加载到内存
        """
        with self.session_scope() as session:
            try:
                # 构建基础查询
                base_filter = Trade.status == 'closed'
                if user:
                    base_filter = base_filter & (Trade.user == user)
                
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

    def get_user_stats(self, user: str) -> Dict:
        """获取用户统计（兼容旧接口）"""
        return self.get_statistics(user)

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
