from market.home import build_home_payload, order_flow_state


def test_order_flow_states_describe_obi_and_cvd_without_signal():
    assert order_flow_state(-0.2, -4) == "SELLERS_DOMINATING"
    assert order_flow_state(0.2, -4) == "BOOK_SUPPORTS"
    assert order_flow_state(None, 2) == "DATA_PENDING"


def test_home_payload_uses_existing_module_payloads():
    state = {"timestamp": "now", "price": {"average": 70000}, "funding": {"average": 0.01},
             "open_interest": {"binance": {"oi_usd": 1}}, "obi": {"composite_obi": -0.1, "sources": {"binance": {"obi": -0.2}}},
             "cvd": {"binance": {"30m": {"cvd_btc": -5}}}}
    result = build_home_payload(state, {"signal": {"bias": "BEARISH"}}, {"support": 69000},
                                {"source": "BINANCE_OBSERVED_FORCE_ORDERS", "levels": []}, {"markets": []}, [])
    assert result["btc"]["price"] == 70000
    assert result["order_flow"]["exchanges"][0]["state"] == "SELLERS_DOMINATING"
    assert result["liquidation_map"]["source"] == "BINANCE_OBSERVED_FORCE_ORDERS"
