import logging
import os
from datetime import datetime, timezone

import firebase_admin
from firebase_admin import credentials, messaging

from market.fcm_device_store import (
    DEFAULT_DB_PATH,
    deactivate_token,
    list_active_devices,
)

logger = logging.getLogger(__name__)

DEFAULT_CREDENTIAL_PATH = (
    "/opt/btc-trading-os/secrets/firebase-service-account.json"
)


def _get_firebase_app():
    try:
        return firebase_admin.get_app()
    except ValueError:
        credential_path = os.getenv(
            "FIREBASE_CREDENTIALS",
            DEFAULT_CREDENTIAL_PATH,
        )

        if not os.path.isfile(credential_path):
            raise FileNotFoundError(
                f"Firebase credential file not found: {credential_path}"
            )

        cred = credentials.Certificate(credential_path)
        return firebase_admin.initialize_app(cred)


def _stringify_payload(data):
    result = {}

    for key, value in data.items():
        if value is None:
            continue

        if isinstance(value, bool):
            result[str(key)] = "true" if value else "false"
        else:
            result[str(key)] = str(value)

    return result


def send_to_token(token, data):
    app = _get_firebase_app()

    payload = _stringify_payload(data)

    message = messaging.Message(
        token=token,
        data=payload,
        android=messaging.AndroidConfig(
            priority="high",
        ),
    )

    return messaging.send(message, app=app)


def send_to_active_devices(
    data,
    db_path=DEFAULT_DB_PATH,
):
    devices = list_active_devices(db_path)

    result = {
        "device_count": len(devices),
        "sent": 0,
        "failed": 0,
        "deactivated": 0,
        "results": [],
    }

    for device in devices:
        token = device["token"]

        try:
            message_id = send_to_token(token, data)

            result["sent"] += 1
            result["results"].append(
                {
                    "installation_id": device["installation_id"],
                    "status": "sent",
                    "message_id": message_id,
                }
            )

        except Exception as exc:
            result["failed"] += 1

            error_name = exc.__class__.__name__
            error_text = str(exc)

            if error_name in {
                "UnregisteredError",
                "SenderIdMismatchError",
            }:
                deactivate_token(
                    token,
                    error_message=f"{error_name}: {error_text}",
                    db_path=db_path,
                )
                result["deactivated"] += 1

            logger.exception(
                "FCM send failed for installation_id=%s",
                device["installation_id"],
            )

            result["results"].append(
                {
                    "installation_id": device["installation_id"],
                    "status": "failed",
                    "error_type": error_name,
                    "error": error_text,
                }
            )

    return result


def build_price_alert_payload(
    event_id,
    direction,
    reason,
    message,
    created_at=None,
):
    if created_at is None:
        created_at = int(
            datetime.now(timezone.utc).timestamp()
        )

    return {
        "event_id": event_id,
        "alert_type": "PRICE",
        "direction": direction,
        "reason": reason,
        "message": message,
        "created_at": created_at,
    }
