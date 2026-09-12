import asyncio
import sqlite3
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import FastAPI
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from market.cvd import fetch_all_cvd
from market.cvd_windows import get_all_cvd_windows
from market.obi import fetch_all_obi
from market.signal_engine import build_signal
from market.alert_api import router as alert_router
from market.alert_monitor import run_alert_monitor

app = FastAPI(title="BTC Trading OS API")

app.include_router(alert_router)

DB_PATH = "/opt/btc-trading-os/market.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            exchange TEXT NOT NULL,
            symbol TEXT NOT NULL,
            price REAL NOT NULL,
            open_interest REAL,
            oi_usd REAL,
            funding_rate REAL
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_market_time
        ON market_snapshots(timestamp)
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_market_exchange
        ON market_snapshots(exchange)
    """)

    conn.commit()
    conn.close()


async def fetch_binance(client):
    ticker = await client.get(
        "https://fapi.binance.com/fapi/v1/ticker/price",
        params={"symbol": "BTCUSDT"},
        timeout=10
    )

    oi = await client.get(
        "https://fapi.binance.com/fapi/v1/openInterest",
        params={"symbol": "BTCUSDT"},
        timeout=10
    )

    funding = await client.get(
        "https://fapi.binance.com/fapi/v1/premiumIndex",
        params={"symbol": "BTCUSDT"},
        timeout=10
    )

    ticker.raise_for_status()
    oi.raise_for_status()
    funding.raise_for_status()

    ticker_data = ticker.json()
    oi_data = oi.json()
    funding_data = funding.json()

    price = float(ticker_data["price"])
    open_interest = float(oi_data["openInterest"])

    return {
        "exchange": "binance",
        "symbol": "BTCUSDT",
        "price": price,
        "open_interest": open_interest,
        "oi_usd": open_interest * price,
        "funding_rate": float(funding_data["lastFundingRate"])
    }


async def fetch_bybit(client):
    r = await client.get(
        "https://api.bybit.com/v5/market/tickers",
        params={
            "category": "linear",
            "symbol": "BTCUSDT"
        },
        timeout=10
    )

    r.raise_for_status()

    data = r.json()

    if data.get("retCode") != 0:
        raise RuntimeError(data.get("retMsg"))

    ticker = data["result"]["list"][0]

    price = float(ticker["lastPrice"])
    open_interest = float(ticker["openInterest"])

    return {
        "exchange": "bybit",
        "symbol": "BTCUSDT",
        "price": price,
        "open_interest": open_interest,
        "oi_usd": open_interest * price,
        "funding_rate": float(ticker["fundingRate"])
    }


async def fetch_okx(client):
    ticker_r = await client.get(
        "https://www.okx.com/api/v5/market/ticker",
        params={"instId": "BTC-USDT-SWAP"},
        timeout=10
    )

    oi_r = await client.get(
        "https://www.okx.com/api/v5/public/open-interest",
        params={
            "instType": "SWAP",
            "instId": "BTC-USDT-SWAP"
        },
        timeout=10
    )

    funding_r = await client.get(
        "https://www.okx.com/api/v5/public/funding-rate",
        params={"instId": "BTC-USDT-SWAP"},
        timeout=10
    )

    ticker_r.raise_for_status()
    oi_r.raise_for_status()
    funding_r.raise_for_status()

    ticker_data = ticker_r.json()
    oi_data = oi_r.json()
    funding_data = funding_r.json()

    ticker = ticker_data["data"][0]
    oi = oi_data["data"][0]
    funding = funding_data["data"][0]

    price = float(ticker["last"])
    open_interest_contracts = float(oi["oi"])

    # BTC-USDT-SWAP 每张合约面值通常为 0.01 BTC
    contract_size_btc = 0.01
    oi_btc = open_interest_contracts * contract_size_btc
    oi_usd = oi_btc * price

    return {
        "exchange": "okx",
        "symbol": "BTC-USDT-SWAP",
        "price": price,
        "open_interest": open_interest_contracts,
        "oi_usd": oi_usd,
        "funding_rate": float(funding["fundingRate"])
    }


async def fetch_market():
    results = []
    errors = []

    async with httpx.AsyncClient() as client:
        tasks = [
            ("binance", fetch_binance(client)),
            ("bybit", fetch_bybit(client)),
            ("okx", fetch_okx(client))
        ]

        for name, task in tasks:
            try:
                result = await task
                results.append(result)
            except Exception as e:
                errors.append({
                    "exchange": name,
                    "error": str(e)
                })

    return results, errors


def save_snapshot(results):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    timestamp = datetime.now(timezone.utc).isoformat()

    for item in results:
        cursor.execute("""
            INSERT INTO market_snapshots (
                timestamp,
                exchange,
                symbol,
                price,
                open_interest,
                oi_usd,
                funding_rate
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            timestamp,
            item["exchange"],
            item["symbol"],
            item["price"],
            item["open_interest"],
            item["oi_usd"],
            item["funding_rate"]
        ))

    conn.commit()
    conn.close()


async def collect_market_snapshot():
    results, errors = await fetch_market()

    if results:
        save_snapshot(results)

    if errors:
        print("snapshot errors:", errors)


def get_change(exchange, minutes):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    now = datetime.now(timezone.utc)
    target = now - timedelta(minutes=minutes)

    cursor.execute("""
        SELECT price, oi_usd, timestamp
        FROM market_snapshots
        WHERE exchange = ?
        AND timestamp <= ?
        ORDER BY timestamp DESC
        LIMIT 1
    """, (
        exchange,
        target.isoformat()
    ))

    old_row = cursor.fetchone()

    cursor.execute("""
        SELECT price, oi_usd, timestamp
        FROM market_snapshots
        WHERE exchange = ?
        ORDER BY timestamp DESC
        LIMIT 1
    """, (exchange,))

    current_row = cursor.fetchone()

    conn.close()

    if not old_row or not current_row:
        return None

    old_price, old_oi, _ = old_row
    current_price, current_oi, _ = current_row

    price_change = (
        ((current_price - old_price) / old_price) * 100
        if old_price
        else None
    )

    oi_change = (
        ((current_oi - old_oi) / old_oi) * 100
        if old_oi
        else None
    )

    return {
        "price_change_pct": price_change,
        "oi_change_pct": oi_change
    }


@app.on_event("startup")
async def startup():
    init_db()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        collect_market_snapshot,
        "interval",
        minutes=1,
        max_instances=1
    )
    scheduler.start()

    await collect_market_snapshot()


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "btc-trading-os",
        "time": datetime.now(timezone.utc).isoformat()
    }


@app.get("/market/btc")
async def market_btc():
    results, errors = await fetch_market()

    prices = [item["price"] for item in results]

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "average_price": (
            sum(prices) / len(prices)
            if prices
            else None
        ),
        "sources": results,
        "errors": errors
    }


@app.get("/market/btc/changes")
async def btc_changes():
    exchanges = [
        "binance",
        "bybit",
        "okx"
    ]

    windows = {
        "5m": 5,
        "30m": 30,
        "1h": 60,
        "4h": 240
    }

    data = {}

    for exchange in exchanges:
        data[exchange] = {}

        for label, minutes in windows.items():
            data[exchange][label] = get_change(
                exchange,
                minutes
            )

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "changes": data
    }

@app.get("/market/btc/cvd")
async def btc_cvd():
    return await fetch_all_cvd()

@app.get("/market/btc/cvd/windows")
async def btc_cvd_windows():
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "windows": get_all_cvd_windows()
    }

@app.get("/market/btc/obi")
async def btc_obi():
    data = await fetch_all_obi()

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **data
    }

@app.get("/market/btc/state")
async def btc_state():

    market_task = fetch_market()
    obi_task = fetch_all_obi()

    market_result, obi_data = await asyncio.gather(
        market_task,
        obi_task
    )

    market_sources, market_errors = market_result

    cvd_data = get_all_cvd_windows()

    prices = [
        item["price"]
        for item in market_sources
        if item.get("price") is not None
    ]

    funding_rates = [
        item["funding_rate"]
        for item in market_sources
        if item.get("funding_rate") is not None
    ]

    average_price = (
        sum(prices) / len(prices)
        if prices
        else None
    )

    average_funding = (
        sum(funding_rates) / len(funding_rates)
        if funding_rates
        else None
    )

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),

        "price": {
            "average": average_price,
            "sources": {
                item["exchange"]: item["price"]
                for item in market_sources
            }
        },

        "open_interest": {
            item["exchange"]: {
                "raw": item["open_interest"],
                "oi_usd": item["oi_usd"]
            }
            for item in market_sources
        },

        "funding": {
            "average": average_funding,
            "sources": {
                item["exchange"]: item["funding_rate"]
                for item in market_sources
            }
        },

        "cvd": cvd_data,

        "obi": obi_data,

        "errors": {
            "market": market_errors,
            "obi": obi_data.get("errors", [])
        }
    }

@app.get("/market/btc/signal")
async def btc_signal():

    market_sources, market_errors = await fetch_market()

    price_changes = {}

    for exchange in ["binance", "bybit", "okx"]:
        price_changes[exchange] = {}

        for label, minutes in {
            "5m": 5,
            "30m": 30,
            "1h": 60,
            "4h": 240
        }.items():
            price_changes[exchange][label] = get_change(
                exchange,
                minutes
            )

    cvd = get_all_cvd_windows()
    obi = await fetch_all_obi()

    funding_rates = [
        item["funding_rate"]
        for item in market_sources
        if item.get("funding_rate") is not None
    ]

    funding = {
        "average": (
            sum(funding_rates) / len(funding_rates)
            if funding_rates
            else None
        )
    }

    signal = build_signal(
        price_changes,
        cvd,
        obi,
        funding
    )

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "signal": signal,
        "price_changes": price_changes,
        "errors": {
            "market": market_errors,
            "obi": obi.get("errors", [])
        }
    }

# =========================
# Automatic Price Alert Monitor
# =========================

alert_monitor_task = None


@app.on_event("startup")
async def start_price_alert_monitor():
    global alert_monitor_task

    if alert_monitor_task is None:
        alert_monitor_task = asyncio.create_task(
            run_alert_monitor(fetch_market)
        )


@app.on_event("shutdown")
async def stop_price_alert_monitor():
    global alert_monitor_task

    if alert_monitor_task is not None:
        alert_monitor_task.cancel()

        try:
            await alert_monitor_task
        except asyncio.CancelledError:
            pass

        alert_monitor_task = None
