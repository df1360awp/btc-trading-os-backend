"""Home-screen presentation helpers built from existing market modules."""


def order_flow_state(obi, cvd_30m):
    """Describe OBI/CVD confluence without turning it into a trade command."""
    if obi is None or cvd_30m is None:
        return "DATA_PENDING"
    if obi <= -0.08 and cvd_30m < 0:
        return "SELLERS_DOMINATING"
    if obi >= 0.08 and cvd_30m > 0:
        return "BUYERS_DOMINATING"
    if obi >= 0.08 and cvd_30m < 0:
        return "BOOK_SUPPORTS"
    if obi <= -0.08 and cvd_30m > 0:
        return "DEMAND_ABSORBING"
    return "BALANCED"


def build_home_payload(state, signal, support_levels, liquidation, macro, risks):
    """Make a small mobile payload; values remain attributable to source modules."""
    order_flow = []
    sources = state.get("obi", {}).get("sources", {})
    cvd_windows = state.get("cvd", {})
    for exchange in ("binance", "bybit", "okx", "hyperliquid"):
        obi = (sources.get(exchange) or {}).get("obi")
        cvd_30m = (cvd_windows.get(exchange) or {}).get("30m", {}).get("cvd_btc")
        order_flow.append({
            "exchange": exchange,
            "obi": obi,
            "cvd_30m_btc": cvd_30m,
            "state": order_flow_state(obi, cvd_30m),
        })
    return {
        "timestamp": state.get("timestamp"),
        "btc": {
            "price": state.get("price", {}).get("average"),
            "funding": state.get("funding", {}).get("average"),
            "open_interest": state.get("open_interest"),
            "signal": signal.get("signal"),
            "support_resistance": support_levels,
        },
        "order_flow": {"exchanges": order_flow, "composite_obi": state.get("obi", {}).get("composite_obi")},
        "liquidation_map": liquidation,
        "macro_markets": macro.get("markets", []),
        "macro_today": {"events": macro.get("events_nearby", []), "recent_ratings": macro.get("recent_ratings", [])},
        "sudden_risks": risks,
        "sources": {
            "order_flow": "existing OBI and CVD windows",
            "liquidation_map": liquidation.get("source"),
            "macro_markets": "existing macro market snapshots",
        },
    }
