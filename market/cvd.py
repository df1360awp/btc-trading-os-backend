from datetime import datetime, timezone

import httpx


async def fetch_binance_cvd(client: httpx.AsyncClient, limit=1000):
    r = await client.get(
        "https://fapi.binance.com/fapi/v1/aggTrades",
        params={
            "symbol": "BTCUSDT",
            "limit": limit
        },
        timeout=10
    )

    r.raise_for_status()
    trades = r.json()

    buy_volume = 0.0
    sell_volume = 0.0

    for trade in trades:
        qty = float(trade["q"])

        # m=True：买方是maker，所以主动方是卖方
        if trade["m"]:
            sell_volume += qty
        else:
            buy_volume += qty

    return {
        "exchange": "binance",
        "buy_volume_btc": buy_volume,
        "sell_volume_btc": sell_volume,
        "cvd_btc": buy_volume - sell_volume,
        "trade_count": len(trades)
    }


async def fetch_bybit_cvd(client: httpx.AsyncClient, limit=1000):
    r = await client.get(
        "https://api.bybit.com/v5/market/recent-trade",
        params={
            "category": "linear",
            "symbol": "BTCUSDT",
            "limit": limit
        },
        timeout=10
    )

    r.raise_for_status()
    data = r.json()

    if data.get("retCode") != 0:
        raise RuntimeError(data.get("retMsg", "Bybit API error"))

    trades = data["result"]["list"]

    buy_volume = 0.0
    sell_volume = 0.0

    for trade in trades:
        qty = float(trade["size"])
        side = trade["side"]

        if side == "Buy":
            buy_volume += qty
        elif side == "Sell":
            sell_volume += qty

    return {
        "exchange": "bybit",
        "buy_volume_btc": buy_volume,
        "sell_volume_btc": sell_volume,
        "cvd_btc": buy_volume - sell_volume,
        "trade_count": len(trades)
    }


async def fetch_okx_cvd(client: httpx.AsyncClient, limit=500):
    r = await client.get(
        "https://www.okx.com/api/v5/market/trades",
        params={
            "instId": "BTC-USDT-SWAP",
            "limit": limit
        },
        timeout=10
    )

    r.raise_for_status()
    data = r.json()

    if data.get("code") != "0":
        raise RuntimeError(data.get("msg", "OKX API error"))

    trades = data["data"]

    buy_contracts = 0.0
    sell_contracts = 0.0

    for trade in trades:
        qty = float(trade["sz"])
        side = trade["side"]

        if side == "buy":
            buy_contracts += qty
        elif side == "sell":
            sell_contracts += qty

    # BTC-USDT-SWAP暂按0.01 BTC/张
    contract_size_btc = 0.01

    buy_volume_btc = buy_contracts * contract_size_btc
    sell_volume_btc = sell_contracts * contract_size_btc

    return {
        "exchange": "okx",
        "buy_volume_btc": buy_volume_btc,
        "sell_volume_btc": sell_volume_btc,
        "cvd_btc": buy_volume_btc - sell_volume_btc,
        "trade_count": len(trades)
    }


async def fetch_all_cvd():
    results = {}
    errors = []

    async with httpx.AsyncClient() as client:
        sources = [
            ("binance", fetch_binance_cvd),
            ("bybit", fetch_bybit_cvd),
            ("okx", fetch_okx_cvd)
        ]

        for name, func in sources:
            try:
                results[name] = await func(client)
            except Exception as e:
                results[name] = None
                errors.append({
                    "exchange": name,
                    "error": str(e)
                })

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sources": results,
        "errors": errors
    }
