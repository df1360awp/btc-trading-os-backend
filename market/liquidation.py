"""Explainable aggregates over publicly observed liquidation events."""
import sqlite3
import time

DB_PATH = "/opt/btc-trading-os/market.db"


def liquidation_map(window_seconds=86400, price_bin_usd=250, db_path=DB_PATH, now_ms=None):
    """Aggregate observed public force-order liquidations into price levels.

    This is intentionally an observed-event map, not an estimate of hidden
    liquidation clusters or a copied third-party heatmap.
    """
    if window_seconds < 60 or window_seconds > 604800:
        raise ValueError("window_seconds must be between 60 and 604800")
    if price_bin_usd <= 0 or price_bin_usd > 10000:
        raise ValueError("price_bin_usd must be between 1 and 10000")
    now_ms = now_ms or time.time_ns() // 1_000_000
    with sqlite3.connect(db_path, timeout=10) as db:
        db.execute("PRAGMA busy_timeout = 10000")
        rows = db.execute("""SELECT side, price, notional_usd, timestamp_ms
            FROM liquidation_events WHERE timestamp_ms>=? ORDER BY timestamp_ms DESC""", (now_ms-window_seconds*1000,)).fetchall()
    levels = {}
    for side, price, notional, timestamp_ms in rows:
        bucket = int(float(price) / price_bin_usd) * price_bin_usd
        item = levels.setdefault(bucket, {"price_from": bucket, "price_to": bucket + price_bin_usd,
                                          "long_liquidation_usd": 0.0, "short_liquidation_usd": 0.0,
                                          "event_count": 0, "last_timestamp_ms": timestamp_ms})
        item[side + "_usd"] += float(notional)
        item["event_count"] += 1
        item["last_timestamp_ms"] = max(item["last_timestamp_ms"], timestamp_ms)
    ordered = []
    for item in levels.values():
        item["total_liquidation_usd"] = item["long_liquidation_usd"] + item["short_liquidation_usd"]
        ordered.append(item)
    ordered.sort(key=lambda item: (-item["total_liquidation_usd"], item["price_from"]))
    return {"source": "BINANCE_OBSERVED_FORCE_ORDERS", "window_seconds": window_seconds,
            "price_bin_usd": price_bin_usd, "event_count": len(rows), "levels": ordered}


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
