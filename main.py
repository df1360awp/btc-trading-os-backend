import asyncio
import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from market.cvd import fetch_all_cvd
from market.cvd_windows import get_all_cvd_windows
from market.obi import fetch_all_obi
from market.signal_engine import build_signal
from market.alert_api import router as alert_router
from market.fcm_api import router as fcm_router
from market.alert_monitor import run_alert_monitor
from market.liquidation import liquidation_map, liquidation_pressure
from market.paper_api import router as paper_router
from market.paper_trading import AccountRequest, PaperError, init_paper_tables
from market.journal import init_journal_tables
from market.journal_api import router as journal_router
from market.macro import MacroEvent, MacroStore, init_macro_tables
from market.macro_api import router as macro_router
from market.risk import RiskEventRequest, RiskStore, init_risk_tables
from market.risk_api import router as risk_router
from market.fcm_sender import send_to_active_devices
from market.market_analysis import MarketAnalysisService
from market.research_context import compose_research_context
from market.journal import JournalStore
from market.data_health import market_data_health
from market.app_auth import init_app_sessions
from market.app_api import router as app_router
from market.paper_trading import require_paper_key

app = FastAPI(title="BTC Trading OS API")

# The Android client serves its bundled WebView from this fixed, local asset
# origin.  It is the only browser origin permitted to call the existing API;
# native clients continue to authenticate with their device session.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://appassets.androidplatform.net"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
)

app.include_router(alert_router)
app.include_router(fcm_router)
app.include_router(paper_router)
app.include_router(journal_router)
app.include_router(macro_router)
app.include_router(risk_router)
app.include_router(app_router)


@app.exception_handler(PaperError)
async def paper_error_handler(request: Request, exc: PaperError):
    return JSONResponse(status_code=exc.status, content={"error": exc.code, "detail": exc.detail})

DB_PATH = "/opt/btc-trading-os/market.db"
macro_store = MacroStore(DB_PATH)
journal_store = JournalStore(DB_PATH)
risk_store = RiskStore(DB_PATH)


async def check_macro_reminders():
    try:
        await asyncio.to_thread(macro_store.send_due, send_to_active_devices)
    except Exception as error:
        print("Macro reminder error:", repr(error))


async def check_sudden_risks():
    """Poll a public news index, then alert only new high-severity items."""
    if os.getenv("RISK_NEWS_ENABLED", "true").lower() not in {"1", "true", "yes"}:
        return
    query = '(bitcoin OR cryptocurrency) AND (hack OR exploit OR outage OR sanctions OR war OR "bank failure")'
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get("https://api.gdeltproject.org/api/v2/doc/doc", params={"query": query, "mode": "artlist", "format": "json", "maxrecords": 25, "format": "json"})
            response.raise_for_status(); articles=response.json().get("articles",[])
        now_ms=int(datetime.now(timezone.utc).timestamp()*1000)
        for article in articles:
            title=str(article.get("title","")).strip(); url=str(article.get("url","")).strip()
            if not title or not url: continue
            event, created=risk_store.ingest(RiskEventRequest(source="GDELT",headline=title,url=url,published_ms=now_ms,summary=str(article.get("domain", ""))),raw=article)
            if created and event["severity"] in {"HIGH","CRITICAL"} and risk_store.needs_delivery(event["id"]):
                result=await asyncio.to_thread(send_to_active_devices,{"alert_type":"SUDDEN_RISK","event_id":event["id"],"severity":event["severity"],"category":event["category"],"message":event["headline"][:300]})
                if result.get("failed",0)==0: risk_store.mark_delivered(event["id"])
    except Exception as error:
        print("Sudden risk monitor error:", repr(error))


async def run_scheduled_journal_review(period):
    """Generate daily/weekly/monthly retrospective reviews, never trade signals."""
    try:
        from market.ai_review import ReviewService
        review, created = await asyncio.to_thread(ReviewService(journal_store, DB_PATH).create_scheduled_period_review, period)
        context = json.loads(review["market_context"])
        if created and context.get("entry_count", 0):
            await asyncio.to_thread(send_to_active_devices, {"alert_type": "JOURNAL_REVIEW", "period": period,
                "review_id": review["id"], "message": f"{period} 交易复盘已生成，包含 {context['entry_count']} 条记录"})
    except Exception as error:
        print("Scheduled journal review error:", repr(error))


def ensure_default_system_paper_strategy(price):
    """Start B as a system-owned virtual strategy when none exists.

    It is intentionally conservative and only touches the shared PaperEngine;
    no exchange client or real order path is involved.
    """
    from market.paper_api import engine, strategies
    if strategies.list("SYSTEM"):
        return
    account_id = "paper-system-btc-v1"
    strategy_id = "system-btc-confluence-v1"
    engine.create_account(AccountRequest(
        account_id=account_id, strategy_type="SYSTEM", strategy_id=strategy_id,
        initial_balance=10_000,
    ), "bootstrap:" + account_id, price)
    strategies.create("SYSTEM", {
        "id": strategy_id, "account_id": account_id,
        "quantity": "0.001", "stop_distance": "600", "take_distance": "1200",
        "cooldown_seconds": 900, "exit_on_opposite_signal": True,
        "min_abs_exit_score": 3, "max_hold_seconds": 86400,
    })


async def notify_paper_activity(price, marked, user_result, system_result):
    opened = user_result["opened"] + system_result["opened"]
    closed = marked["closed_positions"] + user_result["closed"]
    if not opened and not closed:
        return
    payload = {
        "alert_type": "PAPER_TRADE", "price": round(price, 2),
        "opened": opened, "closed": closed,
        "message": f"Paper Trading: 开仓 {opened}，平仓 {closed}，BTC {price:.2f}",
    }
    try:
        await asyncio.to_thread(send_to_active_devices, payload)
    except Exception as error:
        print("Paper FCM notification error:", repr(error))


def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
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

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS market_context_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp_ms INTEGER NOT NULL,
            context_json TEXT NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_context_snapshot_time ON market_context_snapshots(timestamp_ms)")

    conn.commit()
    conn.close()
    init_paper_tables(DB_PATH)
    init_journal_tables(DB_PATH)
    init_macro_tables(DB_PATH)
    init_risk_tables(DB_PATH)
    init_app_sessions(DB_PATH)


def seed_macro_calendar():
    """Idempotently seed the remaining 2026 high-impact US release calendar.

    Dates/times are published US Eastern release times converted to UTC.
    Forecast and previous values are deliberately left empty until sourced.
    """
    events = (
        ("fomc-2026-09", "FOMC 利率决议与新闻发布会", "FOMC", "2026-09-16T18:00:00+00:00"),
        ("nfp-2026-10", "美国非农就业报告（9月）", "NFP", "2026-10-02T12:30:00+00:00"),
        ("fomc-minutes-2026-10", "FOMC 会议纪要", "FOMC", "2026-10-07T18:00:00+00:00"),
        ("cpi-2026-10", "美国 CPI（9月）", "CPI", "2026-10-14T12:30:00+00:00"),
        ("ppi-2026-10", "美国 PPI（9月）", "PPI", "2026-10-15T12:30:00+00:00"),
        ("fomc-2026-10", "FOMC 利率决议与新闻发布会", "FOMC", "2026-10-28T18:00:00+00:00"),
        ("nfp-2026-11", "美国非农就业报告（10月）", "NFP", "2026-11-06T13:30:00+00:00"),
        ("cpi-2026-11", "美国 CPI（10月）", "CPI", "2026-11-10T13:30:00+00:00"),
        ("ppi-2026-11", "美国 PPI（10月）", "PPI", "2026-11-13T13:30:00+00:00"),
        ("nfp-2026-12", "美国非农就业报告（11月）", "NFP", "2026-12-04T13:30:00+00:00"),
        ("fomc-2026-12", "FOMC 利率决议与新闻发布会", "FOMC", "2026-12-09T19:00:00+00:00"),
        ("cpi-2026-12", "美国 CPI（11月）", "CPI", "2026-12-10T13:30:00+00:00"),
    )
    for event_id, title, event_type, scheduled in events:
        try:
            macro_store.create(MacroEvent(
                id=event_id, title=title, event_type=event_type,
                scheduled_ms=int(datetime.fromisoformat(scheduled).timestamp() * 1000),
            ))
        except PaperError as error:
            if error.code != "MACRO_EXISTS":
                raise


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
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
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


def save_market_context_snapshot(context, timestamp_ms=None):
    """Persist the complete server-observed context for retrospective review."""
    timestamp_ms = timestamp_ms or int(datetime.now(timezone.utc).timestamp() * 1000)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute(
        "INSERT INTO market_context_snapshots(timestamp_ms,context_json) VALUES(?,?)",
        (timestamp_ms, json.dumps(context, ensure_ascii=False, default=str)),
    )
    conn.commit()
    conn.close()


def support_resistance(price, lookback_minutes=240):
    """Derive conservative key levels from existing multi-exchange snapshots.

    This is deliberately not presented as a liquidation heatmap: it is an
    explainable price-structure input until a permitted liquidation feed exists.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)).isoformat()
    rows = conn.execute("SELECT price FROM market_snapshots WHERE timestamp>=?", (cutoff,)).fetchall()
    conn.close()
    values = sorted({float(row[0]) for row in rows if row[0] > 0})
    below = [value for value in values if value < price]
    above = [value for value in values if value > price]
    support = below[-1] if below else min(values, default=None)
    resistance = above[0] if above else max(values, default=None)
    return {
        "lookback_minutes": lookback_minutes,
        "support": support,
        "resistance": resistance,
        "support_distance_pct": ((price - support) / price * 100) if support else None,
        "resistance_distance_pct": ((resistance - price) / price * 100) if resistance else None,
        "state": "BREAKOUT" if resistance is None and values else "BREAKDOWN" if support is None and values else "IN_RANGE",
    }


async def collect_market_snapshot():
    results, errors = await fetch_market()

    if results:
        save_snapshot(results)
        # The paper engine receives only the existing server-side aggregate quote.
        # It cannot submit a real-exchange order.
        from market.paper_api import engine, strategies
        price = sum(item["price"] for item in results) / len(results)
        marked = engine.mark(price)
        context = {"price": price, "sources": results}
        # B consumes the existing Signal Engine's output; it never changes its rules.
        changes = {exchange: {label: get_change(exchange, minutes) for label, minutes in {"5m":5,"30m":30,"1h":60,"4h":240}.items()} for exchange in ["binance","bybit","okx"]}
        cvd_windows = get_all_cvd_windows()
        obi = await fetch_all_obi()
        funding_average = sum(x["funding_rate"] for x in results) / len(results)
        signal = build_signal(changes, cvd_windows, obi, {"average": funding_average})
        def average_change(label):
            values=[item.get(label,{}).get("oi_change_pct") for item in changes.values()]
            values=[value for value in values if value is not None]
            return sum(values)/len(values) if values else None
        def composite_cvd(label):
            values=[]
            for exchange in cvd_windows.values():
                point=exchange.get(label,{}) if isinstance(exchange,dict) else {}
                value=point.get("cvd_btc") if isinstance(point,dict) else None
                if value is not None: values.append(value)
            return sum(values) if values else None
        enriched_context = {**context, "signal": signal, "funding": {"average": funding_average}, "obi": obi,
            "oi": {"average_change_5m_pct": average_change("5m"), "average_change_30m_pct": average_change("30m"), "average_change_1h_pct": average_change("1h")},
            "cvd": {"composite_5m_btc": composite_cvd("5m"), "composite_30m_btc": composite_cvd("30m"), "composite_1h_btc": composite_cvd("1h")},
            "support_resistance": support_resistance(price), "liquidation": liquidation_pressure()}
        save_market_context_snapshot(enriched_context)
        strategies.protective_exits(marked["closed_position_ids"], enriched_context)
        user_result = strategies.on_market(price, enriched_context)
        system_result = strategies.on_signal(price, signal, enriched_context)
        try:
            journal_store.import_all_paper_trades(engine, price)
        except Exception as error:
            print("Paper journal synchronization error:", repr(error))
        await notify_paper_activity(price, marked, user_result, system_result)

    if errors:
        print("snapshot errors:", errors)


def get_change(exchange, minutes):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
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
    seed_macro_calendar()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        collect_market_snapshot,
        "interval",
        minutes=1,
        max_instances=1
    )
    scheduler.add_job(check_macro_reminders, "interval", minutes=5, max_instances=1)
    scheduler.add_job(check_sudden_risks, "interval", minutes=10, max_instances=1)
    scheduler.add_job(run_scheduled_journal_review, CronTrigger(hour=0, minute=15, timezone="Asia/Shanghai"), args=["DAILY"], max_instances=1)
    scheduler.add_job(run_scheduled_journal_review, CronTrigger(day_of_week="mon", hour=0, minute=25, timezone="Asia/Shanghai"), args=["WEEKLY"], max_instances=1)
    scheduler.add_job(run_scheduled_journal_review, CronTrigger(day=1, hour=0, minute=35, timezone="Asia/Shanghai"), args=["MONTHLY"], max_instances=1)
    scheduler.start()

    await collect_market_snapshot()
    # The first trusted quote is now available for virtual-account valuation.
    from market.paper_api import mark_price
    ensure_default_system_paper_strategy(mark_price())


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


@app.get("/market/btc/research-context")
async def btc_research_context():
    state, signal = await asyncio.gather(btc_state(), btc_signal())
    price = state["price"]["average"]
    return compose_research_context(state, signal, support_resistance(price) if price else {}, liquidation_pressure())


@app.get("/market/btc/data-health")
async def btc_data_health(): return market_data_health(DB_PATH)


@app.get("/market/btc/liquidation-map")
async def btc_liquidation_map(window_seconds: int = 86400, price_bin_usd: int = 250):
    try:
        return liquidation_map(window_seconds, price_bin_usd)
    except ValueError as error:
        return JSONResponse(status_code=422, content={"error": "INVALID_LIQUIDATION_MAP", "detail": str(error)})


@app.post("/ai/market-analysis", dependencies=[Depends(require_paper_key)])
async def ai_market_analysis():
    state, signal = await asyncio.gather(btc_state(), btc_signal())
    context = {"market_state": state, "signal_engine": signal, "liquidation": liquidation_pressure()}
    return await asyncio.to_thread(MarketAnalysisService().explain, context)

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
