import asyncio
import time

from market.alert_api import price_engine, store
from market.fcm_sender import (
    build_price_alert_payload,
    send_to_active_devices,
)


CHECK_INTERVAL_SECONDS = 5


async def get_average_price(fetch_market):
    market_sources, errors = await fetch_market()

    prices = [
        item["price"]
        for item in market_sources
        if item.get("price") is not None
    ]

    if not prices:
        return None, errors

    average_price = sum(prices) / len(prices)

    return average_price, errors


def build_price_alert_message(
    direction,
    current_price,
    upper_trigger_price,
    lower_trigger_price,
    reason,
):
    if direction == "UP":
        if reason == "REPEAT":
            return (
                f"BTC仍高于上方报警价 "
                f"{upper_trigger_price:.2f}，"
                f"当前价格 {current_price:.2f}，"
                f"你尚未确认本轮报警"
            )

        return (
            f"BTC突破上方报警价 "
            f"{upper_trigger_price:.2f}，"
            f"当前价格 {current_price:.2f}"
        )

    if direction == "DOWN":
        if reason == "REPEAT":
            return (
                f"BTC仍低于下方报警价 "
                f"{lower_trigger_price:.2f}，"
                f"当前价格 {current_price:.2f}，"
                f"你尚未确认本轮报警"
            )

        return (
            f"BTC跌破下方报警价 "
            f"{lower_trigger_price:.2f}，"
            f"当前价格 {current_price:.2f}"
        )

    return "BTC价格报警"


async def check_price_once(fetch_market):
    config = store.get_price_config()

    if config is None:
        return {
            "status": "NO_CONFIG"
        }

    if not config["enabled"]:
        return {
            "status": "DISABLED"
        }

    if config["reference_price"] is None:
        return {
            "status": "REFERENCE_PRICE_NOT_SET"
        }

    current_price, market_errors = await get_average_price(
        fetch_market
    )

    if current_price is None:
        return {
            "status": "NO_MARKET_PRICE",
            "errors": market_errors,
        }

    result = price_engine.check_price(
        current_price=current_price,
        now_ts=int(time.time()),
    )

    event_id = None

    if result["should_alert"]:
        message = build_price_alert_message(
            direction=result["direction"],
            current_price=current_price,
            upper_trigger_price=result[
                "upper_trigger_price"
            ],
            lower_trigger_price=result[
                "lower_trigger_price"
            ],
            reason=result["reason"],
        )

        event_id = store.save_alert_event(
            alert_type="PRICE",
            direction=result["direction"],
            reason=result["reason"],
            current_price=current_price,
            message=message,
            created_at=int(time.time()),
        )

        print(
            f"[PRICE ALERT] "
            f"id={event_id} "
            f"direction={result['direction']} "
            f"reason={result['reason']} "
            f"price={current_price:.2f}"
        )

        payload = build_price_alert_payload(
            event_id=event_id,
            direction=result["direction"],
            reason=result["reason"],
            message=message,
            created_at=int(time.time()),
        )

        try:
            fcm_result = await asyncio.to_thread(
                send_to_active_devices,
                payload,
            )

            print(
                f"[FCM ALERT] "
                f"event_id={event_id} "
                f"devices={fcm_result['device_count']} "
                f"sent={fcm_result['sent']} "
                f"failed={fcm_result['failed']} "
                f"deactivated={fcm_result['deactivated']}"
            )

        except Exception as e:
            # FCM delivery must never break the price monitor loop.
            print(
                f"[FCM ALERT ERROR] "
                f"event_id={event_id} "
                f"error={repr(e)}"
            )

    return {
        "status": "OK",
        "current_price": current_price,
        "event_id": event_id,
        **result,
    }


async def run_alert_monitor(fetch_market):
    print(
        f"BTC Price Alert Monitor started, "
        f"interval={CHECK_INTERVAL_SECONDS}s"
    )

    while True:
        try:
            await check_price_once(fetch_market)

        except asyncio.CancelledError:
            print("BTC Price Alert Monitor stopped")
            raise

        except Exception as e:
            print(
                "BTC Price Alert Monitor error:",
                repr(e),
            )

        await asyncio.sleep(
            CHECK_INTERVAL_SECONDS
        )

