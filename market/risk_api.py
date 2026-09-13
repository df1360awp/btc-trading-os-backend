from fastapi import APIRouter, Depends, Query
from market.paper_trading import require_paper_key
from market.risk import RiskEventRequest, RiskStore

store=RiskStore("/opt/btc-trading-os/market.db")
router=APIRouter(prefix="/risk",tags=["Sudden Risk Intelligence"])

@router.get("/events")
def list_events(limit:int=Query(default=100,ge=1,le=500),severity:str|None=None): return store.list(limit,severity)

@router.post("/events",status_code=201,dependencies=[Depends(require_paper_key)])
def create_event(request:RiskEventRequest): return store.ingest(request)[0]
