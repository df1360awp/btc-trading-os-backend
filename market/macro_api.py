from fastapi import APIRouter, Depends, Query
from market.macro import MacroEvent, MacroRelease, MacroStore, MacroUpdate
from market.fcm_sender import send_to_active_devices
from market.paper_trading import require_paper_key
from market.macro_analysis import MacroAnalysisService
from market.macro_intelligence import MacroIntelligenceService
store=MacroStore("/opt/btc-trading-os/market.db")
router=APIRouter(prefix="/macro",tags=["Macro Intelligence"])

# The event calendar is market research data and is safe to render before a
# device session exists.  Mutating events and requesting AI analysis remain
# protected by the existing device/Paper API authentication.
@router.get("/events")
def list_events(limit:int=Query(default=100,ge=1,le=500)): return store.list(limit)

@router.post("/events",status_code=201,dependencies=[Depends(require_paper_key)])
def create(request:MacroEvent): return store.create(request)
@router.get("/events/{event_id}",dependencies=[Depends(require_paper_key)])
def get_event(event_id:str): return store.get(event_id)
@router.put("/events/{event_id}",dependencies=[Depends(require_paper_key)])
def update(event_id:str, request:MacroUpdate): return store.update(event_id,request)
@router.delete("/events/{event_id}",dependencies=[Depends(require_paper_key)])
def delete(event_id:str): return store.delete(event_id)
@router.put("/events/{event_id}/release",dependencies=[Depends(require_paper_key)])
def release(event_id:str, request:MacroRelease):
    event=store.release(event_id,request)
    try: send_to_active_devices({"alert_type":"MACRO_RELEASE","event_id":event_id,"macro_type":event["event_type"],"actual":event["actual"],"forecast":event["forecast"],"previous":event["previous"],"message":f"{event['title']} 已公布：{event['actual']}"})
    except Exception: pass
    return {"event":event,"impact_context":store.impact_context(event_id)}
@router.post("/events/{event_id}/analysis",dependencies=[Depends(require_paper_key)])
def analyze(event_id:str):
    impact_id, context = store.begin_impact_analysis(event_id)
    try:
        result=MacroAnalysisService().explain(context)
    except Exception as error:
        store.fail_impact_analysis(impact_id,error)
        raise
    return store.complete_impact_analysis(impact_id,result["analysis"],result["model"])

@router.get("/impacts")
def list_impacts(event_id:str|None=None,limit:int=Query(default=100,ge=1,le=500)): return store.list_impacts(event_id,limit)

@router.get("/source-releases")
def list_source_releases(limit:int=Query(default=100,ge=1,le=500)): return store.source_releases(limit)

@router.post("/events/{event_id}/impact-rating",dependencies=[Depends(require_paper_key)])
def impact_rating(event_id:str):
    context=store.impact_context(event_id)
    result=MacroIntelligenceService().evaluate(context)
    return store.save_impact_rating(event_id,**result)
