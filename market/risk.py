"""Deduplicated sudden-risk intake; it never creates a trading order."""
import hashlib
import json
import time
import uuid

from pydantic import BaseModel, ConfigDict, Field

from market.paper_trading import PaperError, connect


class RiskEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=2, max_length=80)
    headline: str = Field(min_length=4, max_length=500)
    url: str = Field(min_length=8, max_length=2000)
    published_ms: int = Field(gt=0)
    category: str | None = Field(default=None, max_length=80)
    severity: str | None = Field(default=None, pattern=r"^(MEDIUM|HIGH|CRITICAL)$")
    summary: str = Field(default="", max_length=2000)


def init_risk_tables(db_path):
    with connect(db_path) as db: db.executescript("""
      CREATE TABLE IF NOT EXISTS risk_events(
        id TEXT PRIMARY KEY,source TEXT NOT NULL,headline TEXT NOT NULL,url TEXT NOT NULL,
        published_ms INTEGER NOT NULL,category TEXT NOT NULL,severity TEXT NOT NULL,
        summary TEXT NOT NULL,fingerprint TEXT NOT NULL UNIQUE,raw_json TEXT,created_ms INTEGER NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_risk_events_published ON risk_events(published_ms DESC);
      CREATE TABLE IF NOT EXISTS risk_deliveries(risk_event_id TEXT PRIMARY KEY,sent_ms INTEGER NOT NULL,
        FOREIGN KEY(risk_event_id) REFERENCES risk_events(id));
    """)


class RiskStore:
    def __init__(self, db_path, clock=None): self.db_path, self.clock = db_path, clock or (lambda: time.time_ns() // 1_000_000)

    @staticmethod
    def classify(headline, summary=""):
        value=(headline+" "+summary).lower()
        if any(token in value for token in ("hack", "exploit", "attack", "breach", "stolen")): return "EXCHANGE_OR_SECURITY", "CRITICAL"
        if any(token in value for token in ("outage", "halt", "maintenance", "down", "failure")): return "EXCHANGE_OUTAGE", "HIGH"
        if any(token in value for token in ("sanction", "ban", "sec ", "regulator", "lawsuit")): return "REGULATORY", "HIGH"
        if any(token in value for token in ("war", "missile", "invasion", "conflict", "emergency")): return "GEOPOLITICAL", "HIGH"
        if any(token in value for token in ("treasury yield", "bank failure", "liquidity crisis", "default")): return "MACRO_FINANCIAL", "HIGH"
        return "MARKET_NEWS", "MEDIUM"

    def ingest(self, request, raw=None):
        data=request.model_dump() if isinstance(request, RiskEventRequest) else dict(request)
        category,severity=self.classify(data["headline"],data.get("summary", ""))
        data["category"] = data.get("category") or category; data["severity"] = data.get("severity") or severity
        fingerprint=hashlib.sha256((data["url"].strip().lower()+"|"+data["headline"].strip().lower()).encode()).hexdigest()
        with connect(self.db_path) as db:
            row=db.execute("SELECT * FROM risk_events WHERE fingerprint=?",(fingerprint,)).fetchone()
            if row: return dict(row), False
            item={**data,"id":str(uuid.uuid4()),"fingerprint":fingerprint,"raw_json":json.dumps(raw,ensure_ascii=False) if raw else None,"created_ms":self.clock()}
            db.execute("""INSERT INTO risk_events(id,source,headline,url,published_ms,category,severity,summary,fingerprint,raw_json,created_ms)
              VALUES(:id,:source,:headline,:url,:published_ms,:category,:severity,:summary,:fingerprint,:raw_json,:created_ms)""",item)
        return self.get(item["id"]), True

    def get(self,event_id):
        with connect(self.db_path) as db: row=db.execute("SELECT * FROM risk_events WHERE id=?",(event_id,)).fetchone()
        if not row: raise PaperError("NOT_FOUND","Risk event not found",404)
        return dict(row)

    def list(self,limit=100,severity=None):
        query,params="SELECT * FROM risk_events",[]
        if severity: query+=" WHERE severity=?"; params.append(severity)
        query+=" ORDER BY published_ms DESC LIMIT ?";params.append(limit)
        with connect(self.db_path) as db: rows=db.execute(query,params).fetchall()
        return [dict(row) for row in rows]

    def mark_delivered(self,event_id):
        with connect(self.db_path) as db: db.execute("INSERT OR IGNORE INTO risk_deliveries(risk_event_id,sent_ms) VALUES(?,?)",(event_id,self.clock()))

    def needs_delivery(self,event_id):
        with connect(self.db_path) as db: return not bool(db.execute("SELECT 1 FROM risk_deliveries WHERE risk_event_id=?",(event_id,)).fetchone())
