"""A/B strategy producers. All fills remain inside PaperEngine."""
import json
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from market.paper_trading import EntryRequest, PaperEngine, PaperError, amount, connect


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserStrategy(Model):
    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    account_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    direction: str = Field(pattern=r"^(LONG|SHORT)$")
    entry_price: Decimal = Field(gt=0)
    entry_when: str = Field(pattern=r"^(AT_OR_ABOVE|AT_OR_BELOW)$")
    quantity: Decimal = Field(gt=0)
    stop_loss: Decimal | None = Field(default=None, gt=0)
    take_profit: Decimal | None = Field(default=None, gt=0)
    exit_price: Decimal | None = Field(default=None, gt=0)
    exit_when: str | None = Field(default=None, pattern=r"^(AT_OR_ABOVE|AT_OR_BELOW)$")
    conditions: list[dict] = Field(default_factory=list, max_length=12)


class SystemStrategy(Model):
    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    account_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    min_abs_score: int = Field(default=3, ge=3, le=10)
    quantity: Decimal = Field(gt=0)
    stop_distance: Decimal = Field(gt=0)
    take_distance: Decimal = Field(gt=0)
    cooldown_seconds: int = Field(default=300, ge=0, le=86400)
    # Entry and exit gates consume the existing market context without changing
    # the Signal Engine that supplies the directional signal.
    conditions: list[dict] = Field(default_factory=list, max_length=12)
    exit_conditions: list[dict] = Field(default_factory=list, max_length=12)
    exit_on_opposite_signal: bool = True
    min_abs_exit_score: int | None = Field(default=None, ge=1, le=10)
    max_hold_seconds: int | None = Field(default=None, ge=60, le=2_592_000)
    min_context_confirmations: int = Field(default=0, ge=0, le=5)
    max_level_distance_pct: Decimal | None = Field(default=None, gt=0, le=20)


class StrategyEnabled(Model):
    enabled: bool


class StrategyRunner:
    def __init__(self, engine: PaperEngine): self.engine = engine
    @staticmethod
    def hit(price, target, rule): return price >= target if rule == "AT_OR_ABOVE" else price <= target
    @staticmethod
    def conditions_match(conditions, context):
        """Small allowlisted condition DSL; missing/stale fields never trigger a trade."""
        allowed = {
            "price", "signal.score", "signal.bias", "signal.structure",
            "oi.average_change_5m_pct", "oi.average_change_30m_pct",
            "oi.average_change_1h_pct", "cvd.composite_5m_btc",
            "cvd.composite_30m_btc", "cvd.composite_1h_btc",
            "funding.average", "obi.composite_obi",
            "support_resistance.state", "support_resistance.support_distance_pct",
            "support_resistance.resistance_distance_pct",
            "liquidation.state", "liquidation.imbalance",
            "liquidation.total_liquidation_usd", "liquidation.recent_5m_liquidation_usd",
        }
        for item in conditions:
            field, op, expected = item.get("field"), item.get("op"), item.get("value")
            if field not in allowed or op not in {"EQ","GTE","LTE"}: raise PaperError("INVALID_CONDITION","Unsupported strategy condition",422)
            actual = context
            for part in field.split("."):
                actual = actual.get(part) if isinstance(actual,dict) else None
            if actual is None: return False
            if op == "EQ" and actual != expected: return False
            if op == "GTE" and Decimal(str(actual)) < Decimal(str(expected)): return False
            if op == "LTE" and Decimal(str(actual)) > Decimal(str(expected)): return False
        return True

    @staticmethod
    def system_confluence(direction, context, definition):
        """Directional confirmation from existing OI/CVD/OBI/levels/liquidations."""
        confirmations, reasons = 0, []
        positive = direction == "LONG"
        oi = context.get("oi", {}).get("average_change_5m_pct")
        if oi is not None and (float(oi) > 0 if positive else float(oi) < 0):
            confirmations += 1; reasons.append("OI方向一致")
        cvd = context.get("cvd", {}).get("composite_5m_btc")
        if cvd is not None and (float(cvd) > 0 if positive else float(cvd) < 0):
            confirmations += 1; reasons.append("CVD方向一致")
        obi = context.get("obi", {}).get("composite_obi")
        if obi is not None and (float(obi) > 0 if positive else float(obi) < 0):
            confirmations += 1; reasons.append("OBI方向一致")
        levels = context.get("support_resistance", {})
        distance = levels.get("support_distance_pct" if positive else "resistance_distance_pct")
        if distance is not None and definition.max_level_distance_pct is not None and float(distance) <= float(definition.max_level_distance_pct):
            confirmations += 1; reasons.append("接近关键支撑阻力")
        liquidation_state = context.get("liquidation", {}).get("state")
        adverse = "LONG_LIQUIDATION" if positive else "SHORT_SQUEEZE"
        if liquidation_state and liquidation_state != adverse:
            confirmations += 1; reasons.append("清算压力未逆向")
        return confirmations >= definition.min_context_confirmations, {"confirmations": confirmations, "required": definition.min_context_confirmations, "reasons": reasons}

    def create(self, route, payload):
        cls = UserStrategy if route == "USER" else SystemStrategy
        item = cls.model_validate(payload); now = self.engine.clock()
        with connect(self.engine.db_path) as db:
            self.engine._account(db, item.account_id)
            if db.execute("SELECT 1 FROM paper_strategies WHERE id=?", (item.id,)).fetchone(): raise PaperError("STRATEGY_EXISTS", "Strategy already exists")
            # One strategy per account preserves deterministic A/B ownership.
            if db.execute("SELECT 1 FROM paper_strategies WHERE account_id=?", (item.account_id,)).fetchone(): raise PaperError("ACCOUNT_IN_USE", "Use one paper account per strategy", 422)
            db.execute("INSERT INTO paper_strategies(id,account_id,route,definition,created_ms,updated_ms) VALUES(?,?,?,?,?,?)", (item.id,item.account_id,route,item.model_dump_json(),now,now))
        return self.get(item.id)

    def get(self, strategy_id):
        with connect(self.engine.db_path) as db:
            row = db.execute("SELECT * FROM paper_strategies WHERE id=?", (strategy_id,)).fetchone()
            if not row: raise PaperError("NOT_FOUND", "Strategy not found", 404)
            result = dict(row); result["enabled"] = bool(result["enabled"]); result["definition"] = json.loads(result["definition"]); result["state"] = json.loads(result["state"]); return result

    def save_state(self, strategy_id, state):
        with connect(self.engine.db_path) as db:
            db.execute("UPDATE paper_strategies SET state=?,updated_ms=? WHERE id=?", (json.dumps(state,sort_keys=True),self.engine.clock(),strategy_id))

    def list(self, route):
        with connect(self.engine.db_path) as db: rows = db.execute("SELECT * FROM paper_strategies WHERE route=? AND enabled=1", (route,)).fetchall()
        return [self.get(row["id"]) for row in rows]

    def set_enabled(self, strategy_id, enabled):
        row = self.get(strategy_id)
        if not enabled and self.engine.records(row["account_id"], "positions"):
            raise PaperError("OPEN_POSITION", "Close the paper position before pausing this strategy", 422)
        with connect(self.engine.db_path) as db:
            db.execute("UPDATE paper_strategies SET enabled=?,updated_ms=? WHERE id=?", (int(enabled), self.engine.clock(), strategy_id))
        return self.get(strategy_id)

    def delete(self, strategy_id):
        row = self.get(strategy_id)
        if self.engine.records(row["account_id"], "positions"):
            raise PaperError("OPEN_POSITION", "Close the paper position before deleting this strategy", 422)
        with connect(self.engine.db_path) as db:
            db.execute("DELETE FROM paper_strategies WHERE id=?", (strategy_id,))
        return {"id": strategy_id, "deleted": True}

    def update(self, strategy_id, payload):
        row = self.get(strategy_id)
        if self.engine.records(row["account_id"], "positions"):
            raise PaperError("OPEN_POSITION", "Close the paper position before editing this strategy", 422)
        cls = UserStrategy if row["route"] == "USER" else SystemStrategy
        item = cls.model_validate({**payload, "id":strategy_id, "account_id":row["account_id"]})
        with connect(self.engine.db_path) as db:
            db.execute("UPDATE paper_strategies SET definition=?,updated_ms=? WHERE id=?", (item.model_dump_json(),self.engine.clock(),strategy_id))
        return self.get(strategy_id)

    def event(self, strategy_id, position_id, event, context):
        with connect(self.engine.db_path) as db:
            db.execute("INSERT INTO paper_strategy_events(strategy_id,position_id,event,timestamp_ms,market_context) VALUES(?,?,?,?,?)", (strategy_id,position_id,event,self.engine.clock(),json.dumps(context,sort_keys=True,default=str)))

    def events(self, strategy_id, limit=100):
        self.get(strategy_id)
        with connect(self.engine.db_path) as db:
            rows=db.execute("SELECT * FROM paper_strategy_events WHERE strategy_id=? ORDER BY id DESC LIMIT ?", (strategy_id,limit)).fetchall()
        return [{**dict(row), "market_context":json.loads(row["market_context"])} for row in rows]

    def protective_exits(self, position_ids, context):
        if not position_ids: return 0
        with connect(self.engine.db_path) as db:
            rows=db.execute("SELECT id,account_id,reason FROM paper_positions WHERE id IN (%s)" % ",".join("?" * len(position_ids)), position_ids).fetchall()
            strategies={row["account_id"]:row["id"] for row in db.execute("SELECT id,account_id FROM paper_strategies").fetchall()}
        for row in rows:
            strategy_id=strategies.get(row["account_id"])
            if strategy_id: self.event(strategy_id,row["id"],row["reason"],context)
        return len(rows)

    def on_market(self, price, context):
        price = Decimal(str(price)); opened = closed = 0
        for row in self.list("USER"):
            d = UserStrategy.model_validate(row["definition"]); positions = self.engine.records(d.account_id,"positions")
            if not positions and self.hit(price,d.entry_price,d.entry_when) and self.conditions_match(d.conditions,context):
                order = self.engine.enter(d.account_id,EntryRequest(direction=d.direction,quantity=d.quantity,stop_loss=d.stop_loss,take_profit=d.take_profit),"user:"+d.id+":"+str(self.engine.clock()),price)
                if order["status"] == "FILLED": self.event(d.id,order["position_id"],"ENTRY",context); opened += 1
            elif positions and d.exit_price is not None and self.hit(price,d.exit_price,d.exit_when):
                self.engine.close(d.account_id,positions[0]["id"],"user-exit:"+d.id+":"+str(self.engine.clock()),price); self.event(d.id,positions[0]["id"],"EXIT_CONDITION",context); closed += 1
        return {"opened":opened,"closed":closed}

    def on_signal(self, price, signal, context):
        price = Decimal(str(price)); opened = closed = 0
        for row in self.list("SYSTEM"):
            d = SystemStrategy.model_validate(row["definition"]); score = signal.get("score",0); bias = signal.get("bias")
            direction = "LONG" if bias == "BULLISH" else "SHORT" if bias == "BEARISH" else None
            fingerprint = f"{direction}:{score}:{signal.get('structure')}"; state = row["state"]
            cooling = self.engine.clock() - state.get("last_entry_ms", 0) < d.cooldown_seconds * 1000
            positions = self.engine.records(d.account_id,"positions")
            if positions:
                position = positions[0]
                opposite = ((position["direction"] == "LONG" and direction == "SHORT")
                            or (position["direction"] == "SHORT" and direction == "LONG"))
                exit_score = d.min_abs_exit_score or d.min_abs_score
                held_ms = self.engine.clock() - int(position["opened_ms"])
                reason = None
                if d.exit_conditions and self.conditions_match(d.exit_conditions, context):
                    reason = "SYSTEM_EXIT_CONDITION"
                elif d.exit_on_opposite_signal and opposite and abs(score) >= exit_score:
                    reason = "OPPOSITE_SIGNAL"
                elif d.max_hold_seconds is not None and held_ms >= d.max_hold_seconds * 1000:
                    reason = "MAX_HOLD_TIME"
                if reason:
                    self.engine.close(d.account_id, position["id"], "system-exit:"+d.id+":"+str(self.engine.clock()), price, reason)
                    self.event(d.id, position["id"], reason, {**context, "signal": signal})
                    closed += 1
                continue
            if (not direction or abs(score) < d.min_abs_score or cooling
                    or not self.conditions_match(d.conditions, context)): continue
            confluence_ok, confluence = self.system_confluence(direction, context, d)
            if not confluence_ok:
                continue
            stop = Decimal(amount(price-d.stop_distance if direction == "LONG" else price+d.stop_distance))
            take = Decimal(amount(price+d.take_distance if direction == "LONG" else price-d.take_distance))
            order = self.engine.enter(d.account_id,EntryRequest(direction=direction,quantity=d.quantity,stop_loss=stop,take_profit=take),"system:"+d.id+":"+str(self.engine.clock()),price)
            if order["status"] == "FILLED":
                self.event(d.id,order["position_id"],"SIGNAL_ENTRY",{**context,"signal":signal,"system_confluence":confluence})
                self.save_state(d.id,{"last_entry_ms":self.engine.clock(),"last_signal":fingerprint,"last_direction":direction})
                opened += 1
        return {"opened":opened, "closed":closed}
