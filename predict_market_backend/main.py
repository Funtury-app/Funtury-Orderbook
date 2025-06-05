from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from web3 import Web3
from web3.exceptions import ContractLogicError
from dotenv import load_dotenv
import os
import json
import logging
import time
import uuid
from database import get_db, Base, engine
from models import Order, Transaction, OrderStatus
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
import urllib3
urllib3.disable_warnings()

app = FastAPI()
load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logger.info("Loaded GANACHE_URL: %s", os.getenv("GANACHE_URL"))

# Web3 配置
w3 = Web3(Web3.HTTPProvider(os.getenv("GANACHE_URL"), request_kwargs={'verify': False}))
funtury_address = Web3.to_checksum_address(os.getenv("FUNTURY_CONTRACT_ADDRESS"))
private_key = os.getenv("PRIVATE_KEY")
owner_address=os.getenv("OWNER_ADDRESS")

# 驗證 Web3 連線
if not w3.is_connected():
    logger.error("Failed to connect to Ganache")
    raise Exception("Web3 provider connection failed")

# 載入合約 ABI
with open("abi/Funtury.json") as f:
    funtury_abi = json.load(f)
with open("abi/PredictionMarket.json") as f:
    market_abi = json.load(f)

funtury_contract = w3.eth.contract(address=funtury_address, abi=funtury_abi)

# Pydantic 模型
class OrderCreate(BaseModel):
    user_address: str
    market_address: str
    outcome: str  # "yes" 或 "no"
    price: float
    amount: int
    side: str  # "buy" 或 "sell"

class OrderResponse(BaseModel):
    id: int
    order_serial: str
    user_address: str
    market_address: str
    outcome: str
    price: float
    amount: int
    side: str
    market_state: str
    created_at: str

class TransactionResponse(BaseModel):
    id: int
    order_serial: str
    user_address: str
    market_address: str
    outcome: str
    side: str
    deal_amount: int
    remaining_amount: int
    price: float
    status: str
    created_at: str
    dealt_at: Optional[str]

# 創建數據庫表
Base.metadata.create_all(bind=engine)

# 根路徑端點
@app.get("/")
async def root():
    return {"message": "Welcome to Predict Market API. Visit /docs for API documentation."}

# 提交自由市場訂單
@app.post("/orders/", response_model=OrderResponse)
async def create_order(order: OrderCreate, db: Session = Depends(get_db)):
    market_contract = w3.eth.contract(address=Web3.to_checksum_address(order.market_address), abi=market_abi)
    market_state = market_contract.functions.getMarketState().call()

    if market_state != "Active":
        raise HTTPException(status_code=400, detail="Market not in active phase")

    # 生成唯一訂單流水號
    order_serial = str(uuid.uuid4())

    # 創建訂單和交易物件，但不立即提交
    user_address = Web3.to_checksum_address(order.user_address)
    db_order = Order(
        order_serial=order_serial,
        user_address=user_address,
        market_address=order.market_address,
        outcome=order.outcome,
        price=order.price,
        amount=order.amount,
        side=order.side,
        market_state=market_state,
        created_at=datetime.utcnow()
    )
    db.add(db_order)

    db_transaction = Transaction(
        order_serial=order_serial,
        user_address=user_address,
        market_address=order.market_address,
        outcome=order.outcome,
        side=order.side,
        deal_amount=0,  # 初始成交數量為0
        remaining_amount=order.amount,  # 初始剩餘數量等於訂單總數量
        price=order.price,
        created_at=datetime.utcnow()
    )
    db.add(db_transaction)

    # 使用事務管理，確保資料庫操作原子性
    with db.begin_nested():
        # 撮合訂單
        matched_orders = match_order(db, db_order)
        remaining_amount = db_order.amount
        for matched_order in matched_orders:
            if remaining_amount == 0:
                break

            matched_transaction = db.query(Transaction).filter(Transaction.order_serial == matched_order.order_serial).first()
            if not matched_transaction:
                logger.error(f"No transaction found for matched order {matched_order.id} (serial: {matched_order.order_serial})")
                raise HTTPException(status_code=400, detail="Matched transaction not found")

            is_yes = order.outcome.lower() == "yes"
            matched_amount = min(remaining_amount, matched_order.amount)
            transaction_price = matched_order.price  # 使用匹配訂單的價格以最大化利益

            logger.info(f"Matching order {db_order.id} (serial: {order_serial}) with {matched_order.id} (serial: {matched_order.order_serial}): amount={matched_amount}, price={transaction_price}")

            # TODO: 應由前端提供簽署交易，後端僅中繼
            logger.warning("Using backend private key to sign transferShares. Consider frontend signing for decentralization.")
            try:
                tx = market_contract.functions.transferShares(
                    Web3.to_checksum_address(db_order.user_address if db_order.side == "sell" else matched_order.user_address),
                    Web3.to_checksum_address(db_order.user_address if db_order.side == "buy" else matched_order.user_address),
                    is_yes,
                    int(transaction_price * 10**18),
                    matched_amount
                ).build_transaction({
                    "from": w3.eth.accounts[0],
                    "nonce": w3.eth.get_transaction_count(w3.eth.accounts[0]),
                    "gas": 300000,
                    "gasPrice": w3.to_wei("20", "gwei")
                })
                signed_tx = w3.eth.account.sign_transaction(tx, private_key)
                tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
                logger.info(f"Transfer shares tx: {tx_hash.hex()}, Status: {receipt['status']}")
                if receipt["status"] == 0:
                    logger.error(f"Transfer shares failed for order {db_order.id}.")
                    raise HTTPException(status_code=400, detail="Transfer shares failed: Transaction reverted")

                # 更新交易紀錄（當前訂單）
                db_transaction.deal_amount += matched_amount
                db_transaction.remaining_amount -= matched_amount
                db_transaction.price = transaction_price
                db_transaction.status = OrderStatus.PARTIALLY_DEALT if db_transaction.remaining_amount > 0 else OrderStatus.DEALT
                db_transaction.dealt_at = datetime.utcnow() if db_transaction.status == OrderStatus.DEALT else db_transaction.dealt_at
                logger.info(f"Updated transaction {db_transaction.id} (serial: {order_serial}): amount={db_transaction.deal_amount}, remaining_amount={db_transaction.remaining_amount}, status={db_transaction.status}")

                # 更新匹配訂單的交易紀錄
                matched_transaction.deal_amount += matched_amount
                matched_transaction.remaining_amount -= matched_amount
                matched_transaction.price = transaction_price
                matched_transaction.status = OrderStatus.PARTIALLY_DEALT if matched_transaction.remaining_amount > 0 else OrderStatus.DEALT
                matched_transaction.dealt_at = datetime.utcnow() if matched_transaction.status == OrderStatus.DEALT else matched_transaction.dealt_at
                logger.info(f"Updated matched transaction {matched_transaction.id} (serial: {matched_order.order_serial}): amount={matched_transaction.deal_amount}, remaining_amount={matched_transaction.remaining_amount}, status={matched_transaction.status}")

                # 更新訂單數量
                matched_order.amount -= matched_amount
                remaining_amount -= matched_amount
                db_order.amount = remaining_amount
                logger.info(f"Updated order {db_order.id} (serial: {order_serial}): remaining_amount={remaining_amount}")
                logger.info(f"Updated matched order {matched_order.id} (serial: {matched_order.order_serial}): remaining_amount={matched_order.amount}")

                # 刪除已完全成交的訂單
                if matched_order.amount == 0:
                    db.delete(matched_order)
                    logger.info(f"Deleted matched order {matched_order.id} (serial: {matched_order.order_serial}) from order book")
                if remaining_amount == 0:
                    db.delete(db_order)
                    logger.info(f"Deleted order {db_order.id} (serial: {order_serial}) from order book")

            except ContractLogicError as e:
                logger.error(f"ContractLogicError_Transfer shares failed for order {db_order.id}: {str(e)}")
                raise HTTPException(status_code=400, detail=f"Transfer shares failed: {str(e)}")
            except Exception as e:
                logger.error(f"Exception_Transfer shares failed for order {db_order.id}: {str(e)}")
                raise HTTPException(status_code=400, detail=f"Transfer shares failed: {str(e)}")

    # 如果還有剩餘數量，保留訂單
    if remaining_amount > 0:
        logger.info(f"Order {db_order.id} (serial: {order_serial}) remains in order book: amount={remaining_amount}")
        db_order.amount = remaining_amount
        db.commit()
        db.refresh(db_order)  # 只在訂單未被刪除時刷新
    else:
        db.delete(db_order)
        logger.info(f"Deleted order {db_order.id} (serial: {order_serial}) from order book")
        db.commit()
        # 返回已完全成交的訂單資訊（使用最後已知的 db_order 狀態）
        return OrderResponse(
            id=db_order.id,
            order_serial=db_order.order_serial,
            user_address=db_order.user_address,
            market_address=db_order.market_address,
            outcome=db_order.outcome,
            price=db_order.price,
            amount=0,  # 訂單已完全成交，數量為 0
            side=db_order.side,
            market_state=db_order.market_state,
            created_at=db_order.created_at.isoformat()
        )

    # 如果訂單未被刪除，返回刷新後的狀態
    return OrderResponse(
        id=db_order.id,
        order_serial=db_order.order_serial,
        user_address=db_order.user_address,
        market_address=db_order.market_address,
        outcome=db_order.outcome,
        price=db_order.price,
        amount=db_order.amount,
        side=db_order.side,
        market_state=db_order.market_state,
        created_at=db_order.created_at.isoformat()
    )

# 取消訂單
@app.post("/orders/{order_id}/cancel", response_model=OrderResponse)
async def cancel_order(order_id: int, db: Session = Depends(get_db)):
    db_order = db.query(Order).filter(Order.id == order_id).first()
    db_transaction = db.query(Transaction).filter(Transaction.order_serial == db_order.order_serial).first()
    if not db_order or not db_transaction:
        raise HTTPException(status_code=404, detail="Order not found")
    if db_transaction.status not in [OrderStatus.OPEN, OrderStatus.PARTIALLY_DEALT]:
        raise HTTPException(status_code=400, detail="Order cannot be cancelled")

    market_contract = w3.eth.contract(address=Web3.to_checksum_address(db_order.market_address), abi=market_abi)
    market_state = market_contract.functions.getMarketState().call()
    if market_state != "Active":
        raise HTTPException(status_code=400, detail="Market not in active phase")

    db_transaction.status = OrderStatus.CANCELLED
    db.delete(db_order)
    db.commit()

    return OrderResponse(
        id=db_order.id,
        order_serial=db_order.order_serial,
        user_address=db_order.user_address,
        market_address=db_order.market_address,
        outcome=db_order.outcome,
        price=db_order.price,
        amount=db_order.amount,
        side=db_order.side,
        market_state=db_order.market_state,
        created_at=db_order.created_at.isoformat()
    )

# 查詢訂單簿
@app.get("/orders/{market_address}/{outcome}", response_model=List[OrderResponse])
async def get_orderbook(market_address: str, outcome: str, db: Session = Depends(get_db)):
    orders = db.query(Order).filter(
        Order.market_address == market_address,
        Order.outcome == outcome,
        Order.amount > 0,
        Order.market_state == "Active"
    ).all()
    return [
        OrderResponse(
            id=o.id,
            order_serial=o.order_serial,
            user_address=o.user_address,
            market_address=o.market_address,
            outcome=o.outcome,
            price=o.price,
            amount=o.amount,
            side=o.side,
            market_state=o.market_state,
            created_at=o.created_at.isoformat()
        ) for o in orders
    ]

# 查詢用戶交易明細
@app.get("/user/{user_address}/transactions", response_model=List[TransactionResponse])
async def get_user_transactions(user_address: str, db: Session = Depends(get_db)):
    transactions = db.query(Transaction).filter(Transaction.user_address == user_address).all()
    return [
        TransactionResponse(
            id=t.id,
            order_serial=t.order_serial,
            user_address=t.user_address,
            market_address=t.market_address,
            outcome=t.outcome,
            side=t.side,
            deal_amount=t.deal_amount,
            remaining_amount=t.remaining_amount,
            price=t.price,
            status=t.status.value,
            created_at=t.created_at.isoformat(),
            dealt_at=t.dealt_at.isoformat() if t.dealt_at else None
        ) for t in transactions
    ]

# 撮合邏輯
def match_order(db: Session, new_order: Order):
    if new_order.amount <= 0:
        return []

    opposite_side = "sell" if new_order.side == "buy" else "buy"
    query = db.query(Order).filter(
        Order.market_address == new_order.market_address,
        Order.outcome == new_order.outcome,
        Order.side == opposite_side,
        Order.amount > 0,
        Order.market_state == "Active"
    )

    # 利益最大化排序
    if new_order.side == "buy":
        # 買單：匹配最低賣單價格（price <= new_order.price），按價格升序
        query = query.filter(Order.price <= new_order.price).order_by(Order.price.asc())
    else:
        # 賣單：匹配最高買單價格（price >= new_order.price），按價格降序
        query = query.filter(Order.price >= new_order.price).order_by(Order.price.desc())

    return query.all()