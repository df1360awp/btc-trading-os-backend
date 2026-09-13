import sqlite3
import time

DB_PATH = "/opt/btc-trading-os/market.db"


def calculate_cvd(exchange, minutes):
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - minutes * 60 * 1000

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            COALESCE(SUM(
                CASE
                    WHEN side = 'buy' THEN qty_btc
                    ELSE 0
                END
            ), 0),
            COALESCE(SUM(
                CASE
                    WHEN side = 'sell' THEN qty_btc
                    ELSE 0
                END
            ), 0),
            COUNT(*)
        FROM cvd_trades
        WHERE exchange = ?
        AND timestamp_ms >= ?
    """, (
        exchange,
        start_ms
    ))

    buy_volume, sell_volume, trade_count = cursor.fetchone()

    conn.close()

    return {
        "buy_volume_btc": buy_volume,
        "sell_volume_btc": sell_volume,
        "cvd_btc": buy_volume - sell_volume,
        "trade_count": trade_count
    }


def get_all_cvd_windows():
    exchanges = [
    "binance",
    "bybit",
    "okx",
    "hyperliquid",
]

    windows = {
        "5m": 5,
        "30m": 30,
        "1h": 60,
        "4h": 240,
        "24h": 1440
    }

    result = {}

    for exchange in exchanges:
        result[exchange] = {}

        for label, minutes in windows.items():
            result[exchange][label] = calculate_cvd(
                exchange,
                minutes
            )

    return result
