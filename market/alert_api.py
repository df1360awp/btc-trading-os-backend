import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from market.alert_engine import PriceAlertEngine
from market.alert_store import AlertStore


DB_PATH = "/opt/btc-trading-os/market.db"

router = APIRouter(
    prefix="/alerts",
    tags=["alerts"],
)

store = AlertStore(DB_PATH)


class PriceAlertConfigUpdate(BaseModel):
    reference_price: float | None = None
    upper_distance: float | None = None
    lower_distance: float | None = None
    repeat_seconds: int | None = None
    enabled: bool | None = None


class PriceCheckRequest(BaseModel):
    current_price: float


def create_engine_from_store():
    config = store.get_price_config()

    reference_price = (
        config["reference_price"]
        if config["reference_price"] is not None
        else 0
    )

    return PriceAlertEngine(
        reference_price=reference_price,
        upper_distance=config["upper_distance"],
        lower_distance=config["lower_distance"],
        repeat_seconds=config["repeat_seconds"],
        enabled=config["enabled"],
    )


price_engine = create_engine_from_store()


def sync_engine_with_store():
    global price_engine

    config = store.get_price_config()

    reference_price = (
        config["reference_price"]
        if config["reference_price"] is not None
        else 0
    )

    price_engine.update_config(
        reference_price=reference_price,
        upper_distance=config["upper_distance"],
        lower_distance=config["lower_distance"],
        repeat_seconds=config["repeat_seconds"],
        enabled=config["enabled"],
    )

    return config


@router.get("/price/config")
async def get_price_alert_config():
    config = store.get_price_config()

    if config is None:
        raise HTTPException(
            status_code=500,
            detail="Price alert config not found",
        )

    upper_trigger_price = None
    lower_trigger_price = None

    if config["reference_price"] is not None:
        upper_trigger_price = (
            config["reference_price"]
            + config["upper_distance"]
        )

        lower_trigger_price = (
            config["reference_price"]
            - config["lower_distance"]
        )

    return {
        **config,
        "upper_trigger_price": upper_trigger_price,
        "lower_trigger_price": lower_trigger_price,
    }


@router.put("/price/config")
async def update_price_alert_config(
    payload: PriceAlertConfigUpdate,
):
    try:
        config = store.update_price_config(
            reference_price=payload.reference_price,
            upper_distance=payload.upper_distance,
            lower_distance=payload.lower_distance,
            repeat_seconds=payload.repeat_seconds,
            enabled=payload.enabled,
        )

        sync_engine_with_store()

    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=str(e),
        )

    upper_trigger_price = None
    lower_trigger_price = None

    if config["reference_price"] is not None:
        upper_trigger_price = (
            config["reference_price"]
            + config["upper_distance"]
        )

        lower_trigger_price = (
            config["reference_price"]
            - config["lower_distance"]
        )

    return {
        "status": "updated",
        "config": {
            **config,
            "upper_trigger_price": upper_trigger_price,
            "lower_trigger_price": lower_trigger_price,
        },
    }


@router.post("/price/check")
async def check_price_alert(
    payload: PriceCheckRequest,
):
    config = store.get_price_config()

    if config is None:
        raise HTTPException(
            status_code=500,
            detail="Price alert config not found",
        )

    if config["reference_price"] is None:
        return {
            "should_alert": False,
            "reason": "REFERENCE_PRICE_NOT_SET",
        }

    result = price_engine.check_price(
        current_price=payload.current_price,
        now_ts=int(time.time()),
    )

    event_id = None

    if result["should_alert"]:
        direction = result["direction"]

        if direction == "UP":
            message = (
                f"BTC价格已突破上方报警价，"
                f"当前价格 {payload.current_price:.2f}"
            )
        else:
            message = (
                f"BTC价格已跌破下方报警价，"
                f"当前价格 {payload.current_price:.2f}"
            )

        event_id = store.save_alert_event(
            alert_type="PRICE",
            direction=direction,
            reason=result["reason"],
            current_price=payload.current_price,
            message=message,
            created_at=int(time.time()),
        )

    return {
        **result,
        "event_id": event_id,
    }


@router.post("/price/acknowledge")
async def acknowledge_current_price_alert():
    runtime_result = price_engine.acknowledge()

    acknowledged_events = 0

    if (
        runtime_result["acknowledged"]
        and runtime_result["direction"] is not None
    ):
        acknowledged_events = (
            store.acknowledge_current_price_episode(
                acknowledged_at=int(time.time())
            )
        )

    return {
        "status": "acknowledged",
        **runtime_result,
        "acknowledged_events": acknowledged_events,
    }


@router.get("/price/status")
async def get_price_alert_status():
    config = store.get_price_config()

    engine_status = price_engine.get_status()

    return {
        "config": config,
        "runtime": engine_status,
    }


@router.get("/events")
async def get_alert_events(
    limit: int = 50,
):
    if limit < 1:
        limit = 1

    if limit > 200:
        limit = 200

    return {
        "events": store.list_alert_events(
            limit=limit
        )
    }


@router.post("/events/{event_id}/acknowledge")
async def acknowledge_alert_event(
    event_id: int,
):
    now = int(time.time())

    success = store.acknowledge_event(
        event_id=event_id,
        acknowledged_at=now,
    )

    if not success:
        raise HTTPException(
            status_code=404,
            detail="Alert event not found",
        )

    price_engine.acknowledge()

    return {
        "status": "acknowledged",
        "event_id": event_id,
    }

