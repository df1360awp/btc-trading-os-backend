from decimal import Decimal

import pytest

from market.paper_trading import AccountRequest, EntryRequest, PaperEngine, PaperError, ProtectionRequest, connect
from market.paper_strategies import StrategyRunner
from market.liquidation import liquidation_map, liquidation_pressure
from market.journal import ImageRequest, JournalEntryRequest, JournalStore, init_journal_tables
from market.ai_review import ReviewService
from market.macro import MacroEvent, MacroRelease, MacroStore, MacroUpdate, init_macro_tables
from market.market_analysis import MarketAnalysisService
from market.macro_analysis import MacroAnalysisService
from market.research_context import compose_research_context
from market.data_health import market_data_health
from market.app_auth import init_app_sessions, issue_session


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


def test_dashboard_aggregates_virtual_accounts(engine):
    engine.create_account(AccountRequest(account_id="second",strategy_type="SYSTEM",strategy_id="b",initial_balance="50"),"second",Decimal("100"))
    result=engine.dashboard(Decimal("100"))
    assert len(result["accounts"]) == 2
    assert Decimal(result["totals"]["equity"]) == Decimal("10050")


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


def test_system_strategy_can_gate_existing_signal_with_market_confluence(engine):
    engine.create_account(AccountRequest(account_id="system-filtered",strategy_type="SYSTEM",strategy_id="b-filtered"),"b-filtered",Decimal("100"))
    runner = StrategyRunner(engine)
    runner.create("SYSTEM", {
        "id": "b-filtered", "account_id": "system-filtered", "quantity": "1",
        "stop_distance": "10", "take_distance": "20",
        "conditions": [
            {"field": "obi.composite_obi", "op": "GTE", "value": "0.05"},
            {"field": "liquidation.state", "op": "EQ", "value": "SHORT_SQUEEZE"},
        ],
    })
    signal = {"score": 3, "bias": "BULLISH", "structure": "TREND"}
    assert runner.on_signal(100, signal, {"obi": {"composite_obi": 0.04}, "liquidation": {"state": "SHORT_SQUEEZE"}})["opened"] == 0
    context = {"obi": {"composite_obi": 0.06}, "liquidation": {"state": "SHORT_SQUEEZE"}}
    assert runner.on_signal(100, signal, context)["opened"] == 1


def test_strategy_conditions_use_actual_support_resistance_context_fields(engine):
    engine.create_account(AccountRequest(account_id="levels",strategy_type="USER",strategy_id="levels"),"levels",Decimal("100"))
    runner=StrategyRunner(engine)
    runner.create("USER", {"id":"levels","account_id":"levels","direction":"LONG","entry_price":"100","entry_when":"AT_OR_ABOVE","quantity":"1","conditions":[{"field":"support_resistance.support_distance_pct","op":"LTE","value":"1"}]})
    assert runner.on_market(100,{"price":100,"support_resistance":{"support_distance_pct":0.5}})["opened"] == 1


def test_system_strategy_autonomously_exits_on_reversal_or_market_invalidation(engine):
    engine.create_account(AccountRequest(account_id="system-exit",strategy_type="SYSTEM",strategy_id="b-exit"),"b-exit",Decimal("100"))
    runner = StrategyRunner(engine)
    runner.create("SYSTEM", {
        "id":"b-exit", "account_id":"system-exit", "quantity":"1", "stop_distance":"10", "take_distance":"20",
        "exit_conditions":[{"field":"cvd.composite_5m_btc","op":"LTE","value":"-10"}],
    })
    assert runner.on_signal(100,{"score":3,"bias":"BULLISH","structure":"UP"},{"cvd":{"composite_5m_btc":5}})["opened"] == 1
    result = runner.on_signal(99,{"score":1,"bias":"BULLISH","structure":"WEAK"},{"cvd":{"composite_5m_btc":-11}})
    assert result == {"opened":0,"closed":1}
    assert engine.records("system-exit","trades")[0]["reason"] == "SYSTEM_EXIT_CONDITION"


def test_system_strategy_autonomously_exits_on_opposite_signal(engine):
    engine.create_account(AccountRequest(account_id="system-reversal",strategy_type="SYSTEM",strategy_id="b-reversal"),"b-reversal",Decimal("100"))
    runner = StrategyRunner(engine)
    runner.create("SYSTEM", {"id":"b-reversal","account_id":"system-reversal","quantity":"1","stop_distance":"10","take_distance":"20"})
    assert runner.on_signal(100,{"score":3,"bias":"BULLISH","structure":"UP"},{})["opened"] == 1
    assert runner.on_signal(99,{"score":-3,"bias":"BEARISH","structure":"DOWN"},{}) == {"opened":0,"closed":1}
    assert engine.records("system-reversal","trades")[0]["reason"] == "OPPOSITE_SIGNAL"


def test_strategy_can_pause_and_delete_without_open_position(engine):
    engine.create_account(AccountRequest(account_id="managed",strategy_type="USER",strategy_id="m"),"managed",Decimal("100"))
    runner=StrategyRunner(engine)
    runner.create("USER", {"id":"managed","account_id":"managed","direction":"LONG","entry_price":"100","entry_when":"AT_OR_ABOVE","quantity":"1"})
    assert runner.set_enabled("managed", False)["enabled"] is False
    assert runner.list("USER") == []
    assert runner.delete("managed") == {"id":"managed","deleted":True}


def test_strategy_events_return_saved_market_context(engine):
    engine.create_account(AccountRequest(account_id="events",strategy_type="USER",strategy_id="events"),"events",Decimal("100"))
    runner=StrategyRunner(engine)
    runner.create("USER", {"id":"events","account_id":"events","direction":"LONG","entry_price":"100","entry_when":"AT_OR_ABOVE","quantity":"1"})
    runner.on_market(100,{"price":100,"signal":{"score":3}})
    assert runner.events("events")[0]["market_context"]["signal"]["score"] == 3


def test_strategy_update_and_protective_exit_event(engine):
    engine.create_account(AccountRequest(account_id="protected",strategy_type="USER",strategy_id="protected"),"protected",Decimal("100"))
    runner=StrategyRunner(engine)
    runner.create("USER", {"id":"protected","account_id":"protected","direction":"LONG","entry_price":"100","entry_when":"AT_OR_ABOVE","quantity":"1","stop_loss":"90"})
    assert runner.update("protected", {"direction":"LONG","entry_price":"101","entry_when":"AT_OR_ABOVE","quantity":"1","stop_loss":"90"})["definition"]["entry_price"] == "101"
    order=engine.enter("protected",EntryRequest(direction="LONG",quantity="1",stop_loss="90"),"protective",Decimal("100"))
    marked=engine.mark(Decimal("89"))
    assert runner.protective_exits(marked["closed_position_ids"], {"price":89}) == 1
    assert runner.events("protected")[0]["event"] == "STOP_LOSS"


def test_liquidation_pressure_uses_public_event_history(tmp_path):
    db_path = tmp_path / "market.db"
    now = 1_800_000_000_000
    with connect(str(db_path)) as db:
        db.execute("""CREATE TABLE liquidation_events (
            exchange TEXT, event_id TEXT, timestamp_ms INTEGER, side TEXT,
            price REAL, qty_btc REAL, notional_usd REAL,
            PRIMARY KEY(exchange, event_id))""")
        db.executemany(
            "INSERT INTO liquidation_events VALUES(?,?,?,?,?,?,?)",
            [
                ("binance", "one", now - 60_000, "long_liquidation", 100, 1, 100),
                ("binance", "two", now - 120_000, "short_liquidation", 100, 3, 300),
                ("binance", "old", now - 1_900_000, "short_liquidation", 100, 9, 900),
            ],
        )
    summary = liquidation_pressure(db_path=str(db_path), now_ms=now)
    assert summary["long_liquidation_usd"] == 100
    assert summary["short_liquidation_usd"] == 300
    assert summary["total_liquidation_usd"] == 400
    assert summary["recent_5m_liquidation_usd"] == 400
    assert summary["imbalance"] == pytest.approx(0.5)
    assert summary["state"] == "SHORT_SQUEEZE"


def test_liquidation_map_groups_observed_events_by_price_level(tmp_path):
    db_path=tmp_path / "market.db"; now=1_800_000_000_000
    with connect(str(db_path)) as db:
        db.execute("CREATE TABLE liquidation_events(exchange TEXT,event_id TEXT,timestamp_ms INTEGER,side TEXT,price REAL,qty_btc REAL,notional_usd REAL)")
        db.executemany("INSERT INTO liquidation_events VALUES(?,?,?,?,?,?,?)", [
            ("binance","a",now-1_000,"long_liquidation",70020,1,70020),
            ("binance","b",now-2_000,"short_liquidation",70110,2,140220),
            ("binance","old",now-90_000_000,"short_liquidation",69900,1,69900),
        ])
    result=liquidation_map(3600,250,str(db_path),now)
    assert result["source"] == "BINANCE_OBSERVED_FORCE_ORDERS"
    assert result["event_count"] == 2
    assert result["levels"][0]["price_from"] == 70000
    assert result["levels"][0]["short_liquidation_usd"] == 140220


def test_journal_keeps_reason_psychology_and_private_image(tmp_path):
    db_path = tmp_path / "market.db"
    init_journal_tables(str(db_path))
    journal = JournalStore(str(db_path), tmp_path / "uploads", clock=lambda: 1_800_000_000_000)
    entry = journal.create(JournalEntryRequest(source="MANUAL", occurred_ms=1_799_999_000_000, side="LONG", user_reason="breakout", psychology="FOMO"))
    assert entry["user_reason"] == "breakout"
    result = journal.attach_image(entry["id"], ImageRequest(mime_type="image/png", data_base64="aW1hZ2U="))
    assert result["image_path"]
    assert (tmp_path / "uploads").exists()
    assert journal.remove_image(entry["id"])["image_path"] is None
    updated=journal.update(entry["id"],JournalEntryRequest(source="MANUAL",occurred_ms=1_799_999_000_001,user_reason="revised"))
    assert updated["user_reason"] == "revised"
    assert journal.delete(entry["id"]) == {"id":entry["id"],"deleted":True}


def test_journal_imports_closed_paper_trades(engine, tmp_path):
    order=engine.enter("user",EntryRequest(direction="LONG",quantity="1"),"journal-import",Decimal("100"))
    engine.close("user",order["position_id"],"journal-close",Decimal("110"))
    init_journal_tables(engine.db_path)
    journal=JournalStore(engine.db_path,tmp_path / "uploads",clock=lambda:1_800_000_000_000)
    assert len(journal.import_paper_trades(engine,"user")) == 1
    assert journal.import_paper_trades(engine,"user") == []


def test_journal_syncs_all_paper_accounts(engine, tmp_path):
    order=engine.enter("user",EntryRequest(direction="LONG",quantity="1"),"sync-all",Decimal("100"))
    engine.close("user",order["position_id"],"sync-all-close",Decimal("101"))
    init_journal_tables(engine.db_path)
    journal=JournalStore(engine.db_path,tmp_path / "uploads",clock=lambda:1_800_000_000_000)
    assert len(journal.import_all_paper_trades(engine,Decimal("101"))) == 1
    assert journal.summary()["paper_pnl"] > 0


def test_ai_review_persists_entry_result_without_network(tmp_path):
    db_path = tmp_path / "market.db"; init_journal_tables(str(db_path))
    with connect(str(db_path)) as db:
        db.execute("CREATE TABLE market_snapshots(exchange TEXT, price REAL, open_interest REAL, oi_usd REAL, funding_rate REAL, timestamp TEXT)")
    store = JournalStore(str(db_path), tmp_path / "uploads", clock=lambda: 1_800_000_000_000)
    entry = store.create(JournalEntryRequest(source="MANUAL", occurred_ms=1_799_999_000_000, user_reason="test"))
    service = ReviewService(store, str(db_path), clock=lambda: 1_800_000_001_000, requester=lambda prompt, image: ("复盘结果", "test-model"))
    review = service.create_entry_review(entry["id"])
    assert review["status"] == "COMPLETED"
    assert review["analysis"] == "复盘结果"
    assert service.list(entry["id"])[0]["id"] == review["id"]


def test_vision_review_uses_verified_image_time_and_full_market_context(tmp_path):
    db_path = tmp_path / "market.db"; init_journal_tables(str(db_path)); init_macro_tables(str(db_path))
    image_time = 1_799_999_500_000
    with connect(str(db_path)) as db:
        db.execute("CREATE TABLE market_snapshots(exchange TEXT, price REAL, open_interest REAL, oi_usd REAL, funding_rate REAL, timestamp TEXT)")
        db.execute("CREATE TABLE market_context_snapshots(timestamp_ms INTEGER, context_json TEXT)")
        db.execute("INSERT INTO market_context_snapshots VALUES(?,?)", (image_time, '{"cvd":{"composite_30m_btc":12},"obi":{"binance":0.1},"signal":{"bias":"BULLISH"},"support_resistance":{"support":70000},"liquidation":{"state":"BALANCED"}}'))
        db.execute("INSERT INTO macro_events VALUES('cpi','CPI','CPI',?, '3%', '3.1%', '2.9%', 1)", (image_time,))
    store = JournalStore(str(db_path), tmp_path / "uploads", clock=lambda: 1_800_000_000_000)
    entry = store.create(JournalEntryRequest(source="MANUAL", occurred_ms=1_799_999_000_000, user_reason="test"))
    store.attach_image(entry["id"], ImageRequest(mime_type="image/png", data_base64="aW1hZ2U="))
    def fake_request(prompt, image):
        if "Read only explicitly visible" in prompt:
            return ('{"occurred_at":"2027-01-15T08:05:00Z","price":90123.4,"confidence":"HIGH","evidence":"截图顶部时间与价格"}', "vision-model")
        return ("你的逻辑\n成立部分\n不足\n当时风险\n更优执行方案", "review-model")
    service = ReviewService(store, str(db_path), requester=fake_request)
    review = service.create_entry_review(entry["id"])
    saved = store.get(entry["id"])
    assert saved["image_occurred_ms"] == 1_800_000_300_000
    assert saved["image_price"] == pytest.approx(90123.4)
    assert '"time_source": "IMAGE"' in review["market_context"]
    assert '"composite_30m_btc": 12' in review["market_context"]
    assert '"event_type": "CPI"' in review["market_context"]


def test_macro_reminders_are_once_only(tmp_path):
    db_path=tmp_path / "market.db"; now=1_800_000_000_000; init_macro_tables(str(db_path))
    store=MacroStore(str(db_path), clock=lambda: now)
    event=store.create(MacroEvent(title="US CPI",event_type="CPI",scheduled_ms=now+30*60*1000))
    delivered=[]
    assert store.send_due(delivered.append, now) == [{"event_id":event["id"],"kind":"T24H"},{"event_id":event["id"],"kind":"T1H"}]
    assert store.send_due(delivered.append, now) == []


def test_macro_release_keeps_actual_and_market_context(tmp_path):
    db_path=tmp_path / "market.db"; init_macro_tables(str(db_path))
    with connect(str(db_path)) as db: db.execute("CREATE TABLE market_snapshots(exchange TEXT,price REAL,open_interest REAL,oi_usd REAL,funding_rate REAL,timestamp TEXT)")
    store=MacroStore(str(db_path),clock=lambda:1_800_000_000_000)
    event=store.create(MacroEvent(title="US CPI",event_type="CPI",scheduled_ms=1_800_000_000_000,forecast="3%",previous="3.1%"))
    assert store.release(event["id"],MacroRelease(actual="2.9%"))["actual"] == "2.9%"
    assert store.impact_context(event["id"])["event"]["forecast"] == "3%"


def test_macro_impact_analysis_is_persisted_with_structured_context(tmp_path):
    db_path=tmp_path / "market.db"; now=1_800_000_000_000; init_macro_tables(str(db_path))
    with connect(str(db_path)) as db:
        db.execute("CREATE TABLE market_snapshots(exchange TEXT,price REAL,open_interest REAL,oi_usd REAL,funding_rate REAL,timestamp TEXT)")
        db.execute("CREATE TABLE market_context_snapshots(timestamp_ms INTEGER,context_json TEXT)")
        db.execute("INSERT INTO market_context_snapshots VALUES(?,?)",(now,'{"cvd":{"composite_30m_btc":5},"obi":{"binance":0.2}}'))
    store=MacroStore(str(db_path),clock=lambda:now)
    event=store.create(MacroEvent(title="US CPI",event_type="CPI",scheduled_ms=now,forecast="3%",previous="3.1%"))
    store.release(event["id"],MacroRelease(actual="2.9%"))
    impact_id,context=store.begin_impact_analysis(event["id"])
    saved=store.complete_impact_analysis(impact_id,"宏观复盘","test-model")
    assert saved["status"] == "COMPLETED"
    assert '"composite_30m_btc": 5' in saved["context_json"]
    assert store.list_impacts(event["id"])[0]["analysis"] == "宏观复盘"


def test_macro_event_can_update_or_delete_before_release(tmp_path):
    db_path=tmp_path / "market.db"; init_macro_tables(str(db_path)); store=MacroStore(str(db_path))
    event=store.create(MacroEvent(title="US CPI",event_type="CPI",scheduled_ms=1_800_000_000_000))
    assert store.update(event["id"],MacroUpdate(title="US Core CPI",event_type="CORE_CPI",scheduled_ms=1_800_000_100_000))["event_type"] == "CORE_CPI"
    assert store.delete(event["id"]) == {"id":event["id"],"deleted":True}


def test_market_ai_explains_context_without_becoming_executor():
    service=MarketAnalysisService(requester=lambda prompt, image: ("市场解释", "test-model"))
    result=service.explain({"signal_engine":{"signal":{"bias":"BULLISH"}},"liquidation":{"state":"BALANCED"}})
    assert result["analysis"] == "市场解释"
    assert result["model"] == "test-model"


def test_macro_ai_explains_release_without_becoming_executor():
    result=MacroAnalysisService(requester=lambda prompt, image:("宏观解释","test-model")).explain({"event":{"actual":"2.9%","forecast":"3%"}})
    assert result["analysis"] == "宏观解释"


def test_research_context_exposes_existing_market_layers():
    result=compose_research_context({"price":{"average":100},"open_interest":{},"funding":{},"cvd":{},"obi":{},"errors":{}},{"signal":{"bias":"BULLISH"},"price_changes":{}},{"state":"IN_RANGE"},{"state":"BALANCED"})
    assert result["signal_engine"]["bias"] == "BULLISH"
    assert result["liquidation"]["state"] == "BALANCED"


def test_market_data_health_marks_fresh_sources(tmp_path):
    db_path=tmp_path / "market.db"; now=1_800_000_000_000
    with connect(str(db_path)) as db:
        db.execute("CREATE TABLE market_snapshots(exchange TEXT,timestamp TEXT)")
        db.execute("CREATE TABLE liquidation_events(timestamp_ms INTEGER)")
        db.execute("INSERT INTO market_snapshots VALUES('binance','2027-01-15T08:00:00+00:00')")
        db.execute("INSERT INTO liquidation_events VALUES(?)",(now-1000,))
    result=market_data_health(str(db_path),now)
    assert result["market_status"] == "FRESH"
    assert result["liquidation_events"]["count"] == 1


def test_registered_fcm_device_can_receive_separate_app_session(tmp_path):
    db_path=tmp_path / "market.db"
    with connect(str(db_path)) as db:
        db.execute("CREATE TABLE fcm_devices(installation_id TEXT,token TEXT,active INTEGER)")
        db.execute("INSERT INTO fcm_devices VALUES('device-1','fcm-token',1)")
    init_app_sessions(str(db_path))
    session=issue_session("fcm-token",str(db_path),now_ms=1_800_000_000_000)
    assert session["access_token"]
    assert session["expires_ms"] > 1_800_000_000_000
