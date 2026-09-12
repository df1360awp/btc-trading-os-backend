"""Freshness and availability checks for the existing market data stores."""
import sqlite3
import time
from datetime import datetime, timezone


def market_data_health(db_path, now_ms=None):
    now_ms = now_ms or time.time_ns() // 1_000_000
    with sqlite3.connect(db_path, timeout=10) as db:
        db.execute("PRAGMA busy_timeout=10000")
        snapshots=db.execute("SELECT exchange,MAX(timestamp) timestamp,COUNT(*) samples FROM market_snapshots GROUP BY exchange").fetchall()
        liquidations=db.execute("SELECT MAX(timestamp_ms),COUNT(*) FROM liquidation_events").fetchone()
    sources=[]
    for exchange,timestamp,samples in snapshots:
        age_ms=now_ms-int(datetime.fromisoformat(timestamp.replace("Z","+00:00")).timestamp()*1000) if timestamp else None
        sources.append({"exchange":exchange,"last_timestamp":timestamp,"age_ms":age_ms,"samples":samples,"status":"FRESH" if age_ms is not None and age_ms<=180000 else "STALE"})
    last_liquidation,count=liquidations
    return {"market_sources":sources,"market_status":"FRESH" if sources and all(row["status"]=="FRESH" for row in sources) else "STALE","liquidation_events":{"count":count,"last_timestamp_ms":last_liquidation,"age_ms":now_ms-last_liquidation if last_liquidation else None}}
