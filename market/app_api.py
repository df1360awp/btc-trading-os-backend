from fastapi import APIRouter, Header
from market.app_auth import issue_session
router=APIRouter(prefix="/app",tags=["Android App"])
@router.post("/session")
def session(x_fcm_token: str | None = Header(default=None,alias="X-FCM-Token")):
    if not x_fcm_token: from fastapi import HTTPException; raise HTTPException(status_code=400,detail="X-FCM-Token is required")
    return issue_session(x_fcm_token)
