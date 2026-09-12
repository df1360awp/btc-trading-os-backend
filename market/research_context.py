"""Stable market-research context shared by Android, AI, and paper strategies."""
from datetime import datetime, timezone


def compose_research_context(state, signal, support_resistance, liquidation):
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "price": state.get("price"),
        "open_interest": state.get("open_interest"),
        "funding": state.get("funding"),
        "cvd": state.get("cvd"),
        "obi": state.get("obi"),
        "signal_engine": signal.get("signal"),
        "price_changes": signal.get("price_changes"),
        "support_resistance": support_resistance,
        "liquidation": liquidation,
        "errors": {"market": state.get("errors", {}).get("market", []), "obi": state.get("errors", {}).get("obi", [])},
    }
