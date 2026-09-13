import httpx


def calculate_obi(bids, asks):
    bid_volume = sum(float(level[1]) for level in bids)
    ask_volume = sum(float(level[1]) for level in asks)

    total = bid_volume + ask_volume

    if total == 0:
        obi = 0.0
    else:
        obi = (bid_volume - ask_volume) / total

    return {
        "bid_volume": bid_volume,
        "ask_volume": ask_volume,
        "obi": obi,
    }


async def fetch_binance_obi(client: httpx.AsyncClient):
    r = await client.get(
        "https://fapi.binance.com/fapi/v1/depth",
        params={
            "symbol": "BTCUSDT",
            "limit": 20,
        },
        timeout=10,
    )

    r.raise_for_status()
    data = r.json()

    result = calculate_obi(
        data["bids"],
        data["asks"],
    )

    return {
        "exchange": "binance",
        **result,
    }


async def fetch_bybit_obi(client: httpx.AsyncClient):
    r = await client.get(
        "https://api.bybit.com/v5/market/orderbook",
        params={
            "category": "linear",
            "symbol": "BTCUSDT",
            "limit": 25,
        },
        timeout=10,
    )

    r.raise_for_status()
    data = r.json()

    if data.get("retCode") != 0:
        raise RuntimeError(
            data.get("retMsg", "Bybit API error")
        )

    book = data["result"]

    result = calculate_obi(
        book["b"][:20],
        book["a"][:20],
    )

    return {
        "exchange": "bybit",
        **result,
    }


async def fetch_okx_obi(client: httpx.AsyncClient):
    r = await client.get(
        "https://www.okx.com/api/v5/market/books",
        params={
            "instId": "BTC-USDT-SWAP",
            "sz": 20,
        },
        timeout=10,
    )

    r.raise_for_status()
    data = r.json()

    if data.get("code") != "0":
        raise RuntimeError(
            data.get("msg", "OKX API error")
        )

    book = data["data"][0]

    result = calculate_obi(
        book["bids"],
        book["asks"],
    )

    return {
        "exchange": "okx",
        **result,
    }


async def fetch_hyperliquid_obi(client: httpx.AsyncClient):
    """Read the public Hyperliquid BTC perpetual order book.

    Hyperliquid's information API is public.  Only the visible L2 book is
    used here, exactly as for the other OBI sources; no account or order API
    is involved.
    """
    response = await client.post(
        "https://api.hyperliquid.xyz/info",
        json={"type": "l2Book", "coin": "BTC"},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    levels = payload.get("levels") or []
    if len(levels) < 2:
        raise RuntimeError("Hyperliquid returned no BTC L2 levels")

    def normalize(book):
        return [[item["px"], item["sz"]] for item in book[:20]]

    result = calculate_obi(normalize(levels[0]), normalize(levels[1]))
    return {"exchange": "hyperliquid", **result}


async def fetch_all_obi():
    results = {}
    errors = []

    async with httpx.AsyncClient() as client:
        sources = [
            ("binance", fetch_binance_obi),
            ("bybit", fetch_bybit_obi),
            ("okx", fetch_okx_obi),
            ("hyperliquid", fetch_hyperliquid_obi),
        ]

        for name, func in sources:
            try:
                results[name] = await func(client)
            except Exception as e:
                results[name] = None
                errors.append({
                    "exchange": name,
                    "error": str(e),
                })

    valid_obi = [
        item["obi"]
        for item in results.values()
        if item is not None
    ]

    composite_obi = (
        sum(valid_obi) / len(valid_obi)
        if valid_obi
        else None
    )

    return {
        "sources": results,
        "composite_obi": composite_obi,
        "errors": errors,
    }
