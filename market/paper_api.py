"""Paper routes mounted into the existing FastAPI application."""
import sqlite3

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from market.paper_trading import (AccountRequest, EntryRequest, PaperEngine, PaperError,
                                  ProtectionRequest, require_paper_key)

DB_PATH = "/opt/btc-trading-os/market.db"
engine = PaperEngine(DB_PATH)
router = APIRouter(prefix="/paper", tags=["Paper Trading"], dependencies=[Depends(require_paper_key)])


def mark_price():
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute("SELECT AVG(price) FROM (SELECT price FROM market_snapshots WHERE timestamp IN (SELECT MAX(timestamp) FROM market_snapshots))").fetchone()
    if row is None or row[0] is None:
        raise HTTPException(status_code=503, detail="No trusted market price is available")
    return row[0]


def key(value: str | None = Header(default=None, alias="Idempotency-Key")):
    if not value or len(value) > 128: raise HTTPException(status_code=400, detail="A valid Idempotency-Key is required")
    return value


@router.post("/accounts",status_code=201)
def create_account(request: AccountRequest, idempotency_key: str = Depends(key)):
    return engine.create_account(request,idempotency_key,mark_price())


@router.get("/accounts/{account_id}")
def account(account_id: str): return engine.account(account_id,mark_price())


@router.post("/accounts/{account_id}/orders",status_code=201)
def enter(account_id: str,request: EntryRequest,idempotency_key: str = Depends(key)):
    return engine.enter(account_id,request,idempotency_key,mark_price())


@router.post("/accounts/{account_id}/positions/{position_id}/close")
def close(account_id: str,position_id: str,idempotency_key: str = Depends(key)):
    return engine.close(account_id,position_id,idempotency_key,mark_price())


@router.put("/accounts/{account_id}/positions/{position_id}/protection")
def protect(account_id: str,position_id: str,request: ProtectionRequest,idempotency_key: str = Depends(key)):
    return engine.protect(account_id,position_id,request,idempotency_key,mark_price())


@router.get("/accounts/{account_id}/metrics")
def metrics(account_id: str): return engine.metrics(account_id)


@router.get("/accounts/{account_id}/{kind}")
def records(account_id: str,kind: str,limit: int = Query(default=100,ge=1,le=500)):
    return engine.records(account_id,kind,limit)
