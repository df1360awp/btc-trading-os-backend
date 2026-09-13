"""Per-device API sessions derived from already registered FCM devices."""
import hashlib
import secrets
import sqlite3
import time
from fastapi import Header, HTTPException

DB_PATH="/opt/btc-trading-os/market.db"

def init_app_sessions(db_path=DB_PATH):
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS app_sessions(token_hash TEXT PRIMARY KEY,installation_id TEXT NOT NULL,expires_ms INTEGER NOT NULL,created_ms INTEGER NOT NULL)")

def issue_session(fcm_token, db_path=DB_PATH, now_ms=None):
    now_ms=now_ms or time.time_ns()//1_000_000
    with sqlite3.connect(db_path) as db:
        row=db.execute("SELECT installation_id FROM fcm_devices WHERE token=? AND active=1",(fcm_token,)).fetchone()
        if not row: raise HTTPException(status_code=401,detail="Active FCM device is required")
        token=secrets.token_urlsafe(32); digest=hashlib.sha256(token.encode()).hexdigest(); expires=now_ms+30*86400000
        db.execute("DELETE FROM app_sessions WHERE installation_id=? OR expires_ms<?",(row[0],now_ms))
        db.execute("INSERT INTO app_sessions VALUES(?,?,?,?)",(digest,row[0],expires,now_ms))
    return {"access_token":token,"token_type":"Bearer","expires_ms":expires}


def issue_installation_session(installation_id, db_path=DB_PATH, now_ms=None):
    """Issue the same limited app session for a registered app installation.

    This is used only by the existing single-user registration-key flow when
    Firebase cannot initialize on a locally signed development APK.  It does
    not grant exchange access and still stores only a hashed session token.
    """
    if not installation_id or len(installation_id) < 4:
        raise HTTPException(status_code=400, detail="Valid installation ID is required")
    now_ms = now_ms or time.time_ns() // 1_000_000
    with sqlite3.connect(db_path) as db:
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        expires = now_ms + 30 * 86400000
        db.execute("DELETE FROM app_sessions WHERE installation_id=? OR expires_ms<?", (installation_id, now_ms))
        db.execute("INSERT INTO app_sessions VALUES(?,?,?,?)", (digest, installation_id, expires, now_ms))
    return {"access_token": token, "token_type": "Bearer", "expires_ms": expires}

def require_device_or_paper(authorization: str | None = Header(default=None)):
    import os
    expected=os.getenv("PAPER_API_KEY")
    if authorization == f"Bearer {expected}" and expected: return "paper"
    if not authorization or not authorization.startswith("Bearer "): raise HTTPException(status_code=401,detail="Invalid API credential",headers={"WWW-Authenticate":"Bearer"})
    digest=hashlib.sha256(authorization[7:].encode()).hexdigest(); now=time.time_ns()//1_000_000
    with sqlite3.connect(DB_PATH) as db: row=db.execute("SELECT installation_id FROM app_sessions WHERE token_hash=? AND expires_ms>?",(digest,now)).fetchone()
    if not row: raise HTTPException(status_code=401,detail="Invalid API credential",headers={"WWW-Authenticate":"Bearer"})
    return row[0]
