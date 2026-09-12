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


def init_macro_tables(db_path):
    with connect(db_path) as db: db.executescript("""
      CREATE TABLE IF NOT EXISTS macro_events(id TEXT PRIMARY KEY,title TEXT NOT NULL,event_type TEXT NOT NULL,scheduled_ms INTEGER NOT NULL,forecast TEXT,previous TEXT,actual TEXT,created_ms INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS macro_reminders(event_id TEXT NOT NULL,kind TEXT NOT NULL,sent_ms INTEGER NOT NULL,PRIMARY KEY(event_id,kind));
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
        return {"event":event,"nearby_market_snapshots":[dict(row) for row in rows]}
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
