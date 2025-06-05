from sqlalchemy import Column, Integer, String, Float, DateTime, Enum
from database import Base
from datetime import datetime
import enum

class OrderStatus(enum.Enum):
    OPEN = "open"
    PARTIALLY_DEALT = "partially_dealt"  # 新增部分成交狀態
    DEALT = "dealt"
    CANCELLED = "cancelled"

class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    order_serial = Column(String, unique=True, index=True)  # 新增訂單流水號
    user_address = Column(String, index=True)
    market_address = Column(String, index=True)
    outcome = Column(String, index=True)  # "yes" 或 "no"
    price = Column(Float)  # 下單價格
    amount = Column(Integer)  # 總數量
    side = Column(String)  # "buy" 或 "sell"
    market_state = Column(String)  # "Preorder", "Active", "Resolved", "Cancelled"
    created_at = Column(DateTime, default=datetime.utcnow)

class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    order_serial = Column(String, index=True)  # 改為參考訂單流水號
    user_address = Column(String, index=True)
    market_address = Column(String, index=True)
    outcome = Column(String, index=True)  # "yes" 或 "no"
    side = Column(String)  # "buy" 或 "sell"
    deal_amount = Column(Integer)  # 成交數量
    remaining_amount = Column(Integer)  # 新增剩餘未成交數量
    price = Column(Float)  # 成交價（成交時）或下單價格（未成交時）
    status = Column(Enum(OrderStatus), default=OrderStatus.OPEN)
    created_at = Column(DateTime, default=datetime.utcnow)
    dealt_at = Column(DateTime, nullable=True)