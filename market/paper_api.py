"""Paper routes mounted into the existing FastAPI application."""
import sqlite3

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from market.paper_trading import (AccountRequest, EntryRequest, PaperEngine, PaperError,
                                  ProtectionRequest, require_paper_key)
from market.paper_strategies import StrategyEnabled, StrategyRunner, SystemStrategy, UserStrategy

DB_PATH = "/opt/btc-trading-os/market.db"
engine = PaperEngine(DB_PATH)
strategies = StrategyRunner(engine)
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


@router.get("/accounts")
def accounts(): return engine.accounts(mark_price())


@router.get("/dashboard")
def dashboard(): return engine.dashboard(mark_price())


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


@router.post("/strategies/user", status_code=201)
def create_user_strategy(request: UserStrategy): return strategies.create("USER", request)


@router.post("/strategies/system", status_code=201)
def create_system_strategy(request: SystemStrategy): return strategies.create("SYSTEM", request)


@router.get("/strategies/{route}")
def list_strategies(route: str):
    if route not in ("USER", "SYSTEM"): raise HTTPException(status_code=404, detail="Strategy route not found")
    return strategies.list(route)


@router.get("/strategy/{strategy_id}")
def get_strategy(strategy_id: str): return strategies.get(strategy_id)


@router.put("/strategy/{strategy_id}/enabled")
def set_strategy_enabled(strategy_id: str, request: StrategyEnabled): return strategies.set_enabled(strategy_id, request.enabled)


@router.put("/strategy/{strategy_id}")
def update_strategy(strategy_id: str, request: dict): return strategies.update(strategy_id, request)


@router.delete("/strategy/{strategy_id}")
def delete_strategy(strategy_id: str): return strategies.delete(strategy_id)


@router.get("/strategy/{strategy_id}/events")
def strategy_events(strategy_id: str, limit: int = Query(default=100, ge=1, le=500)): return strategies.events(strategy_id, limit)


@router.get("/accounts/{account_id}/{kind}")
def records(account_id: str,kind: str,limit: int = Query(default=100,ge=1,le=500)):
    return engine.records(account_id,kind,limit)
