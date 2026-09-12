import asyncio
import json
import sqlite3

import httpx
import websockets

DB_PATH = "/opt/btc-trading-os/market.db"


def init_trade_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cvd_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exchange TEXT NOT NULL,
            trade_id TEXT NOT NULL,
            timestamp_ms INTEGER NOT NULL,
            side TEXT NOT NULL,
            qty_btc REAL NOT NULL,
            price REAL,
            UNIQUE(exchange, trade_id)
        )
    """)

    cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_cvd_time
        ON cvd_trades(exchange, timestamp_ms)
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS liquidation_events (
            exchange TEXT NOT NULL, event_id TEXT NOT NULL,
            timestamp_ms INTEGER NOT NULL, side TEXT NOT NULL,
            price REAL NOT NULL, qty_btc REAL NOT NULL, notional_usd REAL NOT NULL,
            PRIMARY KEY(exchange, event_id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_liquidation_time ON liquidation_events(timestamp_ms)")

    conn.commit()
    conn.close()


def save_liquidation(event_id, timestamp_ms, side, price, qty_btc):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("""INSERT OR IGNORE INTO liquidation_events
        VALUES ('binance',?,?,?,?,?,?)""", (str(event_id), int(timestamp_ms), side, float(price), float(qty_btc), float(price) * float(qty_btc)))
    conn.commit(); conn.close()


async def binance_liquidation_stream():
    """Public force-order feed; records observed liquidations only."""
    url = "wss://fstream.binance.com/ws/btcusdt@forceOrder"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                async for message in ws:
                    payload = json.loads(message); order = payload.get("o", {})
                    if order.get("s") != "BTCUSDT": continue
                    # SELL liquidation closes a long; BUY liquidation closes a short.
                    side = "long_liquidation" if order.get("S") == "SELL" else "short_liquidation"
                    price = order.get("ap") or order.get("p")
                    if price and float(price) > 0:
                        save_liquidation(order.get("i"), payload.get("E"), side, price, order.get("q"))
        except Exception as e:
            print("Binance liquidation WS error:", e); await asyncio.sleep(5)


def save_trade(
    exchange,
    trade_id,
    timestamp_ms,
    side,
    qty_btc,
    price
):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    cursor = conn.cursor()

    cursor.execute("""
        INSERT OR IGNORE INTO cvd_trades (
            exchange,
            trade_id,
            timestamp_ms,
            side,
            qty_btc,
            price
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        exchange,
        trade_id,
        timestamp_ms,
        side,
        qty_btc,
        price
    ))

    conn.commit()
    conn.close()


def get_last_binance_trade_id():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    cursor = conn.cursor()

    cursor.execute("""
        SELECT trade_id
        FROM cvd_trades
        WHERE exchange = 'binance'
        ORDER BY CAST(trade_id AS INTEGER) DESC
        LIMIT 1
    """)

    row = cursor.fetchone()
    conn.close()

    if row:
        return int(row[0])

    return None


async def binance_rest_stream():
    url = "https://fapi.binance.com/fapi/v1/aggTrades"

    async with httpx.AsyncClient() as client:
        while True:
            try:
                last_id = get_last_binance_trade_id()

                params = {
                    "symbol": "BTCUSDT",
                    "limit": 1000
                }

                if last_id is not None:
                    params["fromId"] = last_id + 1

                r = await client.get(
                    url,
                    params=params,
                    timeout=10
                )

                r.raise_for_status()
                trades = r.json()

                for trade in trades:
                    side = (
                        "sell"
                        if trade["m"]
                        else "buy"
                    )

                    save_trade(
                        exchange="binance",
                        trade_id=str(trade["a"]),
                        timestamp_ms=int(trade["T"]),
                        side=side,
                        qty_btc=float(trade["q"]),
                        price=float(trade["p"])
                    )

                if trades:
                    print(
                        "Binance REST:",
                        len(trades),
                        "last_id=",
                        trades[-1]["a"]
                    )

                await asyncio.sleep(1)

            except Exception as e:
                print("Binance REST error:", e)
                await asyncio.sleep(5)


async def bybit_stream():
    url = "wss://stream.bybit.com/v5/public/linear"

    while True:
        try:
            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=20
            ) as ws:

                await ws.send(json.dumps({
                    "op": "subscribe",
                    "args": [
                        "publicTrade.BTCUSDT"
                    ]
                }))

                print("Bybit WebSocket connected")

                async for message in ws:
                    data = json.loads(message)

                    if "data" not in data:
                        continue

                    for trade in data["data"]:
                        side = (
                            "buy"
                            if trade["S"] == "Buy"
                            else "sell"
                        )

                        save_trade(
                            exchange="bybit",
                            trade_id=str(trade["i"]),
                            timestamp_ms=int(trade["T"]),
                            side=side,
                            qty_btc=float(trade["v"]),
                            price=float(trade["p"])
                        )

        except Exception as e:
            print("Bybit WS error:", e)
            await asyncio.sleep(5)


async def okx_stream():
    url = "wss://ws.okx.com:8443/ws/v5/public"

    while True:
        try:
            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=20
            ) as ws:

                await ws.send(json.dumps({
                    "op": "subscribe",
                    "args": [
                        {
                            "channel": "trades",
                            "instId": "BTC-USDT-SWAP"
                        }
                    ]
                }))

                print("OKX WebSocket connected")

                async for message in ws:
                    data = json.loads(message)

                    if "data" not in data:
                        continue

                    for trade in data["data"]:

                        side = trade["side"]

                        # BTC-USDT-SWAP:
                        # 当前按 0.01 BTC / contract
                        contract_size_btc = 0.01

                        qty_btc = (
                            float(trade["sz"])
                            * contract_size_btc
                        )

                        save_trade(
                            exchange="okx",
                            trade_id=str(trade["tradeId"]),
                            timestamp_ms=int(trade["ts"]),
                            side=side,
                            qty_btc=qty_btc,
                            price=float(trade["px"])
                        )

        except Exception as e:
            print("OKX WS error:", e)
            await asyncio.sleep(5)


async def run_cvd_streams():
    init_trade_db()

    await asyncio.gather(
        binance_rest_stream(),
        binance_liquidation_stream(),
        bybit_stream(),
        okx_stream()
    )


if __name__ == "__main__":
    asyncio.run(run_cvd_streams())
