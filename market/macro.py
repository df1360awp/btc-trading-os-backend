"""Scheduled macro-event calendar and idempotent FCM reminder delivery."""
import json
import time
import uuid
from pydantic import BaseModel, ConfigDict, Field
from market.paper_trading import PaperError, connect


class Model(BaseModel): model_config = ConfigDict(extra="forbid")
class MacroEvent(Model):
    id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    title: str = Field(min_length=2, max_length=200)
    event_type: str = Field(pattern=r"^(CPI|CORE_CPI|PPI|PCE|CORE_PCE|NFP|ADP|JOBLESS_CLAIMS|GDP|ISM|FOMC|FED_SPEECH|OTHER)$")
    scheduled_ms: int = Field(gt=0)
    forecast: str | None = Field(default=None, max_length=100)
    previous: str | None = Field(default=None, max_length=100)


class MacroRelease(Model):
    actual: str = Field(min_length=1, max_length=100)


class MacroUpdate(Model):
    title: str = Field(min_length=2, max_length=200)
    event_type: str = Field(pattern=r"^(CPI|CORE_CPI|PPI|PCE|CORE_PCE|NFP|ADP|JOBLESS_CLAIMS|GDP|ISM|FOMC|FED_SPEECH|OTHER)$")
    scheduled_ms: int = Field(gt=0)
    forecast: str | None = Field(default=None, max_length=100)
    previous: str | None = Field(default=None, max_length=100)


def init_macro_tables(db_path):
    with connect(db_path) as db: db.executescript("""
      CREATE TABLE IF NOT EXISTS macro_events(id TEXT PRIMARY KEY,title TEXT NOT NULL,event_type TEXT NOT NULL,scheduled_ms INTEGER NOT NULL,forecast TEXT,previous TEXT,actual TEXT,created_ms INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS macro_reminders(event_id TEXT NOT NULL,kind TEXT NOT NULL,sent_ms INTEGER NOT NULL,PRIMARY KEY(event_id,kind));
      CREATE TABLE IF NOT EXISTS macro_impacts(id TEXT PRIMARY KEY,event_id TEXT NOT NULL,status TEXT NOT NULL,context_json TEXT NOT NULL,analysis TEXT,model TEXT,created_ms INTEGER NOT NULL,completed_ms INTEGER,FOREIGN KEY(event_id) REFERENCES macro_events(id));
      CREATE INDEX IF NOT EXISTS idx_macro_impacts_event ON macro_impacts(event_id,created_ms);
    """)


class MacroStore:
    def __init__(self, db_path, clock=None): self.db_path, self.clock = db_path, clock or (lambda: time.time_ns() // 1_000_000)
    def create(self, request):
        data = request.model_dump(); data["id"] = data["id"] or str(uuid.uuid4())
        with connect(self.db_path) as db:
            if db.execute("SELECT 1 FROM macro_events WHERE id=?", (data["id"],)).fetchone(): raise PaperError("MACRO_EXISTS", "Macro event already exists", 409)
            db.execute("INSERT INTO macro_events(id,title,event_type,scheduled_ms,forecast,previous,created_ms) VALUES(:id,:title,:event_type,:scheduled_ms,:forecast,:previous,:created_ms)", {**data,"created_ms":self.clock()})
        return self.get(data["id"])
    def get(self, event_id):
        with connect(self.db_path) as db: row=db.execute("SELECT * FROM macro_events WHERE id=?",(event_id,)).fetchone()
        if not row: raise PaperError("NOT_FOUND","Macro event not found",404)
        return dict(row)
    def list(self, limit=100):
        with connect(self.db_path) as db: rows=db.execute("SELECT * FROM macro_events ORDER BY scheduled_ms LIMIT ?",(limit,)).fetchall()
        return [dict(x) for x in rows]
    def update(self, event_id, request):
        event=self.get(event_id)
        if event["actual"] is not None: raise PaperError("MACRO_RELEASED", "Released macro events cannot be edited", 422)
        with connect(self.db_path) as db:
            db.execute("UPDATE macro_events SET title=?,event_type=?,scheduled_ms=?,forecast=?,previous=? WHERE id=?", (*request.model_dump().values(),event_id))
        return self.get(event_id)
    def delete(self, event_id):
        event=self.get(event_id)
        if event["actual"] is not None: raise PaperError("MACRO_RELEASED", "Released macro events cannot be deleted", 422)
        with connect(self.db_path) as db:
            db.execute("DELETE FROM macro_reminders WHERE event_id=?",(event_id,)); db.execute("DELETE FROM macro_events WHERE id=?",(event_id,))
        return {"id":event_id,"deleted":True}
    def release(self, event_id, request):
        self.get(event_id)
        with connect(self.db_path) as db:
            db.execute("UPDATE macro_events SET actual=? WHERE id=?", (request.actual, event_id))
            row=db.execute("SELECT * FROM macro_events WHERE id=?", (event_id,)).fetchone()
        return dict(row)
    def impact_context(self, event_id):
        event=self.get(event_id)
        with connect(self.db_path) as db:
            rows=db.execute("SELECT exchange,price,open_interest,oi_usd,funding_rate,timestamp FROM market_snapshots ORDER BY ABS(strftime('%s',timestamp)-?) LIMIT 3", (event["scheduled_ms"] // 1000,)).fetchall()
            structured_row = None
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_context_snapshots'").fetchone():
                structured_row = db.execute("SELECT timestamp_ms,context_json FROM market_context_snapshots ORDER BY ABS(timestamp_ms-?) LIMIT 1", (event["scheduled_ms"],)).fetchone()
        structured = None
        if structured_row:
            try: structured=json.loads(structured_row["context_json"])
            except (TypeError, json.JSONDecodeError): pass
        return {"event":event,"nearby_market_snapshots":[dict(row) for row in rows],"structured_market_context":structured,"structured_market_context_at_ms":structured_row["timestamp_ms"] if structured_row else None}

    def begin_impact_analysis(self, event_id):
        event=self.get(event_id)
        if event["actual"] is None: raise PaperError("MACRO_NOT_RELEASED", "Record actual data before impact analysis", 422)
        impact_id=str(uuid.uuid4()); now=self.clock(); context=self.impact_context(event_id)
        with connect(self.db_path) as db:
            db.execute("INSERT INTO macro_impacts(id,event_id,status,context_json,created_ms) VALUES(?,?,?,?,?)", (impact_id,event_id,"RUNNING",json.dumps(context,ensure_ascii=False,default=str),now))
        return impact_id, context

    def complete_impact_analysis(self, impact_id, analysis, model):
        with connect(self.db_path) as db:
            db.execute("UPDATE macro_impacts SET status='COMPLETED',analysis=?,model=?,completed_ms=? WHERE id=?", (analysis,model,self.clock(),impact_id))
            row=db.execute("SELECT * FROM macro_impacts WHERE id=?",(impact_id,)).fetchone()
        return dict(row)

    def fail_impact_analysis(self, impact_id, error):
        with connect(self.db_path) as db:
            db.execute("UPDATE macro_impacts SET status='FAILED',analysis=?,completed_ms=? WHERE id=?", (str(error)[:1000],self.clock(),impact_id))

    def list_impacts(self, event_id=None, limit=100):
        query, params="SELECT * FROM macro_impacts", []
        if event_id: query += " WHERE event_id=?"; params.append(event_id)
        query += " ORDER BY created_ms DESC LIMIT ?"; params.append(limit)
        with connect(self.db_path) as db: rows=db.execute(query,params).fetchall()
        return [dict(row) for row in rows]
    def due(self, now_ms):
        windows={"T24H":86400000,"T1H":3600000}; result=[]
        with connect(self.db_path) as db:
            for kind, window in windows.items():
                rows=db.execute("SELECT * FROM macro_events e WHERE e.scheduled_ms BETWEEN ? AND ? AND NOT EXISTS(SELECT 1 FROM macro_reminders r WHERE r.event_id=e.id AND r.kind=?)",(now_ms,now_ms+window,kind)).fetchall()
                result.extend((kind,dict(row)) for row in rows)
        return result
    def mark_sent(self,event_id,kind):
        with connect(self.db_path) as db: db.execute("INSERT OR IGNORE INTO macro_reminders(event_id,kind,sent_ms) VALUES(?,?,?)",(event_id,kind,self.clock()))

    def send_due(self, sender, now_ms=None):
        sent=[]
        for kind,event in self.due(now_ms or self.clock()):
            sender({"alert_type":"MACRO","event_id":event["id"],"macro_type":event["event_type"],"reminder":kind,"message":f"{event['title']} 将在 {kind[1:]} 后公布"})
            self.mark_sent(event["id"],kind); sent.append({"event_id":event["id"],"kind":kind})
        return sent
