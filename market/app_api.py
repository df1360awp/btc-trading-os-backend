from fastapi import APIRouter, Header
from market.app_auth import issue_installation_session, issue_session
from market.fcm_api import _verify_registration_key
router=APIRouter(prefix="/app",tags=["Android App"])
@router.post("/session")
def session(x_fcm_token: str | None = Header(default=None,alias="X-FCM-Token")):
    if not x_fcm_token: from fastapi import HTTPException; raise HTTPException(status_code=400,detail="X-FCM-Token is required")
    return issue_session(x_fcm_token)


@router.post("/session/bootstrap")
def bootstrap_session(
    x_registration_key: str | None = Header(default=None, alias="X-Registration-Key"),
    x_installation_id: str | None = Header(default=None, alias="X-Installation-Id"),
):
    """Fallback for the existing one-user Android registration-key setup."""
    _verify_registration_key(x_registration_key)
    return issue_installation_session(x_installation_id)
