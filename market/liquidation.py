"""Explainable aggregates over publicly observed liquidation events."""
import sqlite3
import time

DB_PATH = "/opt/btc-trading-os/market.db"


def liquidation_pressure(window_seconds=1800, db_path=DB_PATH, now_ms=None):
    now_ms = now_ms or time.time_ns() // 1_000_000
    cutoff = now_ms - window_seconds * 1000
    with sqlite3.connect(db_path, timeout=10) as db:
        db.execute("PRAGMA busy_timeout = 10000")
        rows = db.execute("""SELECT side, COALESCE(SUM(notional_usd),0), COUNT(*)
            FROM liquidation_events WHERE timestamp_ms>=? GROUP BY side""", (cutoff,)).fetchall()
        recent = db.execute("""SELECT COALESCE(SUM(notional_usd),0) FROM liquidation_events
            WHERE timestamp_ms>=?""", (now_ms - 300000,)).fetchone()[0]
    values = {side: amount for side, amount, count in rows}
    long_value = float(values.get("long_liquidation", 0))
    short_value = float(values.get("short_liquidation", 0))
    total = long_value + short_value
    return {
        "window_seconds": window_seconds,
        "long_liquidation_usd": long_value,
        "short_liquidation_usd": short_value,
        "total_liquidation_usd": total,
        "imbalance": (short_value - long_value) / total if total else 0.0,
        "recent_5m_liquidation_usd": float(recent),
        "state": "SHORT_SQUEEZE" if short_value > long_value * 1.5 else "LONG_LIQUIDATION" if long_value > short_value * 1.5 else "BALANCED",
    }
