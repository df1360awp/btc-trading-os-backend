from fastapi import APIRouter, Depends, Query
from market.macro import MacroEvent, MacroStore
from market.paper_trading import require_paper_key
store=MacroStore("/opt/btc-trading-os/market.db")
router=APIRouter(prefix="/macro",tags=["Macro Intelligence"],dependencies=[Depends(require_paper_key)])
@router.post("/events",status_code=201)
def create(request:MacroEvent): return store.create(request)
@router.get("/events")
def list_events(limit:int=Query(default=100,ge=1,le=500)): return store.list(limit)
