"""Scheduled macro-event calendar and idempotent FCM reminder delivery."""
import json
import re
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
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
      CREATE TABLE IF NOT EXISTS macro_source_releases(id TEXT PRIMARY KEY,source TEXT NOT NULL,title TEXT NOT NULL,url TEXT NOT NULL UNIQUE,published_ms INTEGER NOT NULL,event_id TEXT,extracted_actual TEXT,confidence TEXT NOT NULL,raw_text TEXT,created_ms INTEGER NOT NULL,FOREIGN KEY(event_id) REFERENCES macro_events(id));
      CREATE INDEX IF NOT EXISTS idx_macro_source_event ON macro_source_releases(event_id,published_ms DESC);
      CREATE TABLE IF NOT EXISTS macro_market_snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT,timestamp_ms INTEGER NOT NULL,symbol TEXT NOT NULL,value REAL NOT NULL,source TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS idx_macro_market_time ON macro_market_snapshots(symbol,timestamp_ms DESC);
      CREATE TABLE IF NOT EXISTS macro_impact_ratings(id TEXT PRIMARY KEY,event_id TEXT NOT NULL,rating INTEGER NOT NULL,bias TEXT NOT NULL,analysis TEXT NOT NULL,model TEXT NOT NULL,context_json TEXT NOT NULL,created_ms INTEGER NOT NULL,FOREIGN KEY(event_id) REFERENCES macro_events(id));
      CREATE INDEX IF NOT EXISTS idx_macro_ratings_event ON macro_impact_ratings(event_id,created_ms DESC);
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

    def ingest_official_release(self, source, title, url, published_ms, raw_text=""):
        event_type, actual = parse_official_release(title, raw_text)
        confidence = "HIGH" if event_type and actual else "UNPARSED"
        with connect(self.db_path) as db:
            old=db.execute("SELECT * FROM macro_source_releases WHERE url=?",(url,)).fetchone()
            if old: return dict(old), False
            event=None
            if event_type:
                event=db.execute("""SELECT * FROM macro_events WHERE event_type=? AND scheduled_ms BETWEEN ? AND ?
                    ORDER BY ABS(scheduled_ms-?) LIMIT 1""",(event_type,published_ms-3*86400000,published_ms+86400000,published_ms)).fetchone()
            item={"id":str(uuid.uuid4()),"source":source,"title":title[:500],"url":url,"published_ms":published_ms,"event_id":event["id"] if event else None,"extracted_actual":actual,"confidence":confidence,"raw_text":raw_text[:8000],"created_ms":self.clock()}
            db.execute("""INSERT INTO macro_source_releases(id,source,title,url,published_ms,event_id,extracted_actual,confidence,raw_text,created_ms)
                VALUES(:id,:source,:title,:url,:published_ms,:event_id,:extracted_actual,:confidence,:raw_text,:created_ms)""",item)
            if event and actual and event["actual"] is None:
                db.execute("UPDATE macro_events SET actual=? WHERE id=?",(actual,event["id"]))
        return self.source_release(item["id"]), True

    def source_release(self, release_id):
        with connect(self.db_path) as db: row=db.execute("SELECT * FROM macro_source_releases WHERE id=?",(release_id,)).fetchone()
        return dict(row)

    def source_releases(self, limit=100):
        with connect(self.db_path) as db: rows=db.execute("SELECT * FROM macro_source_releases ORDER BY published_ms DESC LIMIT ?",(limit,)).fetchall()
        return [dict(row) for row in rows]
    def impact_context(self, event_id):
        event=self.get(event_id)
        with connect(self.db_path) as db:
            rows=db.execute("SELECT exchange,price,open_interest,oi_usd,funding_rate,timestamp FROM market_snapshots ORDER BY ABS(strftime('%s',timestamp)-?) LIMIT 3", (event["scheduled_ms"] // 1000,)).fetchall()
            structured_row = None
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_context_snapshots'").fetchone():
                structured_row = db.execute("SELECT timestamp_ms,context_json FROM market_context_snapshots ORDER BY ABS(timestamp_ms-?) LIMIT 1", (event["scheduled_ms"],)).fetchone()
            macro_rows=db.execute("""SELECT m.symbol,m.value,m.source,m.timestamp_ms FROM macro_market_snapshots m
                WHERE m.timestamp_ms=(SELECT MAX(x.timestamp_ms) FROM macro_market_snapshots x WHERE x.symbol=m.symbol AND x.timestamp_ms<=?)""",(event["scheduled_ms"],)).fetchall()
        structured = None
        if structured_row:
            try: structured=json.loads(structured_row["context_json"])
            except (TypeError, json.JSONDecodeError): pass
        return {"event":event,"nearby_market_snapshots":[dict(row) for row in rows],"structured_market_context":structured,"structured_market_context_at_ms":structured_row["timestamp_ms"] if structured_row else None,"macro_markets":[dict(row) for row in macro_rows]}

    def save_macro_markets(self, values, timestamp_ms=None, source="YAHOO_FINANCE"):
        now=timestamp_ms or self.clock()
        with connect(self.db_path) as db:
            for symbol,value in values.items():
                if value is not None: db.execute("INSERT INTO macro_market_snapshots(timestamp_ms,symbol,value,source) VALUES(?,?,?,?)",(now,symbol,float(value),source))
        return values

    def save_impact_rating(self,event_id,rating,bias,analysis,model,context):
        item={"id":str(uuid.uuid4()),"event_id":event_id,"rating":int(rating),"bias":bias,"analysis":analysis,"model":model,"context_json":json.dumps(context,ensure_ascii=False,default=str),"created_ms":self.clock()}
        with connect(self.db_path) as db: db.execute("INSERT INTO macro_impact_ratings(id,event_id,rating,bias,analysis,model,context_json,created_ms) VALUES(:id,:event_id,:rating,:bias,:analysis,:model,:context_json,:created_ms)",item)
        return item

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


def parse_official_release(title, text=""):
    """Conservative BLS headline parser; unknown formats remain unparsed."""
    value=(title+" "+text).lower()
    if "consumer price index" in value:
        match=re.search(r"(?:rose|increased|up)\s+([0-9]+(?:\.[0-9]+)?)\s+percent",value)
        return "CPI", f"{match.group(1)}%" if match else None
    if "producer price index" in value or "ppi for final demand" in value:
        match=re.search(r"(?:rose|increased|up)\s+([0-9]+(?:\.[0-9]+)?)\s+percent",value)
        return "PPI", f"{match.group(1)}%" if match else None
    if "payroll employment" in value or "employment situation" in value:
        match=re.search(r"(?:increased|rose)\s+by\s+([0-9,]+)|(?:decreased|fell)\s+by\s+([0-9,]+)",value)
        if match: return "NFP", ("-" if match.group(2) else "") + (match.group(1) or match.group(2))
        return "NFP", None
    if "fomc statement" in value or "federal reserve issues fomc" in value:
        match=re.search(r"target range.*?(\d+(?:\.\d+)?)\s*(?:to|-)\s*(\d+(?:\.\d+)?)\s+percent",value)
        return "FOMC", f"{match.group(1)}%-{match.group(2)}%" if match else None
    if "gross domestic product" in value or "gdp (" in value:
        match=re.search(r"(?:increased|grew).*?([0-9]+(?:\.[0-9]+)?)\s+percent",value)
        return "GDP", f"{match.group(1)}%" if match else None
    if "personal income and outlays" in value or "personal consumption expenditures" in value:
        match=re.search(r"(?:rose|increased).*?([0-9]+(?:\.[0-9]+)?)\s+percent",value)
        return "PCE", f"{match.group(1)}%" if match else None
    return None, None


def parse_bls_rss(payload):
    root=ET.fromstring(payload); items=[]
    for item in root.findall(".//item"):
        title=(item.findtext("title") or "").strip(); url=(item.findtext("link") or "").strip(); description=(item.findtext("description") or "").strip(); published=item.findtext("pubDate")
        if not title or not url or not published: continue
        try: published_ms=int(parsedate_to_datetime(published).astimezone(timezone.utc).timestamp()*1000)
        except (TypeError,ValueError): continue
        items.append({"title":title,"url":url,"description":description,"published_ms":published_ms})
    return items
