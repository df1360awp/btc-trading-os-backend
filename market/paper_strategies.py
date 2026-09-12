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
    # Optional confluence gates let B consume the existing market context without
    # changing the Signal Engine that provides its directional signal.
    conditions: list[dict] = Field(default_factory=list, max_length=12)


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
            "funding.average", "obi.composite_obi",
            "support_resistance.state", "support_resistance.distance_to_support_pct",
            "support_resistance.distance_to_resistance_pct",
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

    def event(self, strategy_id, position_id, event, context):
        with connect(self.engine.db_path) as db:
            db.execute("INSERT INTO paper_strategy_events(strategy_id,position_id,event,timestamp_ms,market_context) VALUES(?,?,?,?,?)", (strategy_id,position_id,event,self.engine.clock(),json.dumps(context,sort_keys=True,default=str)))

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
        price = Decimal(str(price)); opened = 0
        for row in self.list("SYSTEM"):
            d = SystemStrategy.model_validate(row["definition"]); score = signal.get("score",0); bias = signal.get("bias")
            direction = "LONG" if bias == "BULLISH" else "SHORT" if bias == "BEARISH" else None
            fingerprint = f"{direction}:{score}:{signal.get('structure')}"; state = row["state"]
            cooling = self.engine.clock() - state.get("last_entry_ms", 0) < d.cooldown_seconds * 1000
            if (not direction or abs(score) < d.min_abs_score or cooling
                    or self.engine.records(d.account_id,"positions")
                    or not self.conditions_match(d.conditions, context)): continue
            stop = Decimal(amount(price-d.stop_distance if direction == "LONG" else price+d.stop_distance))
            take = Decimal(amount(price+d.take_distance if direction == "LONG" else price-d.take_distance))
            order = self.engine.enter(d.account_id,EntryRequest(direction=direction,quantity=d.quantity,stop_loss=stop,take_profit=take),"system:"+d.id+":"+str(self.engine.clock()),price)
            if order["status"] == "FILLED":
                self.event(d.id,order["position_id"],"SIGNAL_ENTRY",{**context,"signal":signal})
                self.save_state(d.id,{"last_entry_ms":self.engine.clock(),"last_signal":fingerprint,"last_direction":direction})
                opened += 1
        return {"opened":opened}
