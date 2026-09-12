import os

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from market.fcm_device_store import (
    init_fcm_device_table,
    list_devices,
    register_device,
)

router = APIRouter(
    prefix="/fcm",
    tags=["FCM"],
)

REGISTRATION_KEY_ENV = "FCM_REGISTRATION_KEY"


class FcmDeviceRegisterRequest(BaseModel):
    installation_id: str = Field(
        min_length=4,
        max_length=200,
    )
    token: str = Field(
        min_length=20,
        max_length=4096,
    )
    platform: str = Field(
        default="android",
        max_length=32,
    )


def _get_registration_key():
    key = os.getenv(REGISTRATION_KEY_ENV)

    if not key:
        raise RuntimeError(
            f"{REGISTRATION_KEY_ENV} is not configured"
        )

    return key


def _verify_registration_key(
    x_registration_key: str | None,
):
    expected_key = _get_registration_key()

    if not x_registration_key:
        raise HTTPException(
            status_code=401,
            detail="Missing registration key",
        )

    if x_registration_key != expected_key:
        raise HTTPException(
            status_code=401,
            detail="Invalid registration key",
        )


@router.post("/devices/register")
def register_fcm_device(
    request: FcmDeviceRegisterRequest,
    x_registration_key: str | None = Header(
        default=None,
        alias="X-Registration-Key",
    ),
):
    _verify_registration_key(x_registration_key)

    try:
        device = register_device(
            installation_id=request.installation_id,
            token=request.token,
            platform=request.platform,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    return {
        "status": "ok",
        "device": {
            "id": device["id"],
            "installation_id": device["installation_id"],
            "platform": device["platform"],
            "active": bool(device["active"]),
            "created_at": device["created_at"],
            "updated_at": device["updated_at"],
        },
    }


@router.get("/devices")
def get_fcm_devices():
    devices = list_devices()

    return {
        "count": len(devices),
        "devices": [
            {
                "id": row["id"],
                "installation_id": row["installation_id"],
                "platform": row["platform"],
                "active": bool(row["active"]),
                "last_error": row["last_error"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in devices
        ],
    }


@router.get("/health")
def fcm_health():
    init_fcm_device_table()

    try:
        _get_registration_key()
        registration_key_status = "configured"
    except RuntimeError:
        registration_key_status = "missing"

    return {
        "status": "ok",
        "firebase": "configured",
        "registration_key": registration_key_status,
    }
