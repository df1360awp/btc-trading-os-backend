from decimal import Decimal

import pytest

from market.paper_trading import AccountRequest, EntryRequest, PaperEngine, PaperError, ProtectionRequest, connect
from market.paper_strategies import StrategyRunner


@pytest.fixture
def engine(tmp_path):
    now = [1800000000000]
    service = PaperEngine(str(tmp_path / "market.db"), clock=lambda: now[0])
    service.now = now
    service.create_account(AccountRequest(account_id="user", strategy_type="USER", strategy_id="test", initial_balance="10000", fee_bps="0", slippage_bps="0"), "account", Decimal("100"))
    return service


def tick(engine, price):
    engine.now[0] += 1000
    return engine.mark(Decimal(str(price)), engine.now[0])


@pytest.mark.parametrize("direction,exit,pnl", [("LONG",110,10),("SHORT",90,10),("LONG",90,-10),("SHORT",110,-10)])
def test_long_short_lifecycle(engine,direction,exit,pnl):
    order=engine.enter("user",EntryRequest(direction=direction,quantity="1"),"entry",Decimal("100"))
    assert order["status"] == "FILLED"
    tick(engine,exit)
    assert Decimal(engine.account("user",Decimal(str(exit)))["unrealized_pnl"]) == Decimal(str(pnl))
    trade=engine.close("user",order["position_id"],"close",Decimal(str(exit)))
    assert Decimal(trade["profit_loss"]) == Decimal(str(pnl))
    assert Decimal(engine.account("user",Decimal(str(exit)))["balance"]) == 10000 + Decimal(str(pnl))


def test_fees_slippage_stop_tp_and_gap(engine):
    engine.create_account(AccountRequest(account_id="system",strategy_type="SYSTEM",strategy_id="b",fee_bps="10",slippage_bps="10"),"system",Decimal("100"))
    order=engine.enter("system",EntryRequest(direction="LONG",quantity="2",stop_loss="90",take_profit="120"),"entry",Decimal("100"))
    assert Decimal(order["fill_price"]) == Decimal("100.1")
    tick(engine,80)
    trade=engine.records("system","trades")[0]
    assert trade["reason"] == "STOP_LOSS"
    assert Decimal(trade["exit_price"]) == Decimal("79.92")
    assert Decimal(trade["profit_loss"]) < Decimal("-40")


def test_sizing_and_protection(engine):
    assert Decimal(engine.enter("user",EntryRequest(direction="LONG",equity_fraction=".1"),"equity",Decimal("100"))["quantity"]) == 10
    risk=engine.enter("user",EntryRequest(direction="LONG",risk_fraction=".01",stop_loss="90"),"risk",Decimal("100"))
    assert Decimal(risk["quantity"]) == 10
    engine.protect("user",risk["position_id"],ProtectionRequest(stop_loss="95",take_profit="120"),"protect",Decimal("100"))
    tick(engine,94)
    assert engine.records("user","trades")[0]["reason"] == "STOP_LOSS"


def test_statistics_drawdown_and_no_infinity(engine):
    order=engine.enter("user",EntryRequest(direction="LONG",quantity="1"),"one",Decimal("100"))
    tick(engine,200); tick(engine,50)
    metrics=engine.metrics("user")
    assert Decimal(metrics["max_drawdown"]) == 150
    assert metrics["profit_factor"] is None
    engine.close("user",order["position_id"],"close",Decimal("50"))
    assert engine.metrics("user")["profit_factor_state"] == "FINITE"


def test_idempotency_and_isolation(engine):
    first=engine.enter("user",EntryRequest(direction="LONG",quantity="1"),"same",Decimal("100"))
    assert engine.enter("user",EntryRequest(direction="LONG",quantity="1"),"same",Decimal("100")) == first
    with pytest.raises(PaperError,match="another request"):
        engine.enter("user",EntryRequest(direction="SHORT",quantity="1"),"same",Decimal("100"))
    engine.create_account(AccountRequest(account_id="other",strategy_type="SYSTEM",strategy_id="b"),"other",Decimal("100"))
    with pytest.raises(PaperError) as error: engine.close("other",first["position_id"],"close",Decimal("100"))
    assert error.value.status == 404


def test_rejects_overcommit_and_bad_protection_without_position(engine):
    rejected=engine.enter("user",EntryRequest(direction="LONG",quantity="200"),"big",Decimal("100"))
    assert rejected["status"] == "REJECTED"
    with pytest.raises(PaperError,match="wrong side"):
        engine.enter("user",EntryRequest(direction="LONG",quantity="1",stop_loss="110"),"bad",Decimal("100"))
    assert engine.records("user","positions") == []


def test_user_strategy_and_system_signal_adapter_share_engine(engine):
    engine.create_account(AccountRequest(account_id="a-route",strategy_type="USER",strategy_id="a"),"a",Decimal("100"))
    engine.create_account(AccountRequest(account_id="b-route",strategy_type="SYSTEM",strategy_id="b"),"b",Decimal("100"))
    runner=StrategyRunner(engine)
    runner.create("USER",{"id":"a","account_id":"a-route","direction":"LONG","entry_price":"105","entry_when":"AT_OR_ABOVE","quantity":"1","stop_loss":"95","take_profit":"120"})
    assert runner.on_market(104,{"price":104}) == {"opened":0,"closed":0}
    assert runner.on_market(105,{"price":105})["opened"] == 1
    assert Decimal(engine.records("a-route","positions")[0]["entry_price"]) > Decimal("105")
    runner.create("SYSTEM",{"id":"b","account_id":"b-route","quantity":"1","stop_distance":"10","take_distance":"20"})
    assert runner.on_signal(105,{"score":3,"bias":"BULLISH"},{"price":105})["opened"] == 1
    assert engine.records("b-route","positions")[0]["direction"] == "LONG"


def test_user_strategy_conditions_require_current_market_context(engine):
    engine.create_account(AccountRequest(account_id="conditional",strategy_type="USER",strategy_id="c"),"conditional",Decimal("100"))
    runner=StrategyRunner(engine)
    runner.create("USER",{"id":"conditional","account_id":"conditional","direction":"LONG","entry_price":"100","entry_when":"AT_OR_ABOVE","quantity":"1","conditions":[{"field":"signal.score","op":"GTE","value":3},{"field":"signal.bias","op":"EQ","value":"BULLISH"}]})
    assert runner.on_market(100,{"price":100,"signal":{"score":2,"bias":"BULLISH"}})["opened"] == 0
    assert runner.on_market(100,{"price":100,"signal":{"score":3,"bias":"BULLISH"}})["opened"] == 1
    with connect(engine.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM paper_strategy_events").fetchone()[0] == 1
