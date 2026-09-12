"""Trading journal persistence shared by manual, A-route, and B-route trades."""
import base64
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from market.paper_trading import PaperError, connect

DB_PATH = "/opt/btc-trading-os/market.db"
UPLOAD_DIR = Path("/opt/btc-trading-os/journal_uploads")
MAX_IMAGE_BYTES = 10 * 1024 * 1024


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JournalEntryRequest(Model):
    id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    source: str = Field(pattern=r"^(MANUAL|PAPER_USER|PAPER_SYSTEM)$")
    symbol: str = Field(default="BTCUSDC", min_length=3, max_length=32)
    side: str | None = Field(default=None, pattern=r"^(LONG|SHORT)$")
    occurred_ms: int = Field(gt=0)
    user_reason: str = Field(default="", max_length=8000)
    psychology: str = Field(default="", max_length=8000)
    paper_account_id: str | None = Field(default=None, max_length=128)
    paper_position_id: str | None = Field(default=None, max_length=128)


class ImageRequest(Model):
    mime_type: str = Field(pattern=r"^image/(jpeg|png|webp)$")
    data_base64: str = Field(min_length=1, max_length=14_000_000)


def init_journal_tables(db_path=DB_PATH):
    with connect(db_path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS journal_entries (
          id TEXT PRIMARY KEY, source TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT,
          occurred_ms INTEGER NOT NULL, user_reason TEXT NOT NULL, psychology TEXT NOT NULL,
          paper_account_id TEXT, paper_position_id TEXT, image_path TEXT,
          created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_journal_occurred ON journal_entries(occurred_ms);
        CREATE TABLE IF NOT EXISTS journal_reviews (
          id TEXT PRIMARY KEY, entry_id TEXT, period TEXT NOT NULL,
          period_start_ms INTEGER, period_end_ms INTEGER, status TEXT NOT NULL,
          market_context TEXT NOT NULL, analysis TEXT, model TEXT,
          created_ms INTEGER NOT NULL, completed_ms INTEGER,
          FOREIGN KEY(entry_id) REFERENCES journal_entries(id)
        );
        CREATE INDEX IF NOT EXISTS idx_journal_reviews_entry ON journal_reviews(entry_id, created_ms);
        """)


class JournalStore:
    def __init__(self, db_path=DB_PATH, upload_dir=UPLOAD_DIR, clock=None):
        self.db_path, self.upload_dir = db_path, Path(upload_dir)
        self.clock = clock or (lambda: time.time_ns() // 1_000_000)

    def create(self, request: JournalEntryRequest):
        item, now = request.model_dump(), self.clock()
        item["id"] = item["id"] or str(uuid.uuid4())
        with connect(self.db_path) as db:
            if db.execute("SELECT 1 FROM journal_entries WHERE id=?", (item["id"],)).fetchone():
                raise PaperError("JOURNAL_EXISTS", "Journal entry already exists", 409)
            db.execute("""INSERT INTO journal_entries(id,source,symbol,side,occurred_ms,user_reason,psychology,paper_account_id,paper_position_id,created_ms,updated_ms)
                VALUES(:id,:source,:symbol,:side,:occurred_ms,:user_reason,:psychology,:paper_account_id,:paper_position_id,:created_ms,:updated_ms)""", {**item, "created_ms": now, "updated_ms": now})
        return self.get(item["id"])

    def get(self, entry_id):
        with connect(self.db_path) as db:
            row = db.execute("SELECT * FROM journal_entries WHERE id=?", (entry_id,)).fetchone()
        if not row: raise PaperError("NOT_FOUND", "Journal entry not found", 404)
        return dict(row)

    def list(self, limit=100):
        with connect(self.db_path) as db:
            rows = db.execute("SELECT * FROM journal_entries ORDER BY occurred_ms DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def attach_image(self, entry_id, request: ImageRequest):
        previous = self.get(entry_id).get("image_path")
        try: raw = base64.b64decode(request.data_base64, validate=True)
        except ValueError as error: raise PaperError("INVALID_IMAGE", "Image must be base64 encoded", 422) from error
        if not raw or len(raw) > MAX_IMAGE_BYTES: raise PaperError("INVALID_IMAGE", "Image exceeds 10 MiB limit", 422)
        suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[request.mime_type]
        self.upload_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.upload_dir / f"{entry_id}-{uuid.uuid4().hex}{suffix}"
        path.write_bytes(raw)
        os.chmod(path, 0o600)
        with connect(self.db_path) as db:
            db.execute("UPDATE journal_entries SET image_path=?,updated_ms=? WHERE id=?", (str(path), self.clock(), entry_id))
        if previous: self._remove_private_file(previous)
        return self.get(entry_id)

    def remove_image(self, entry_id):
        previous = self.get(entry_id).get("image_path")
        with connect(self.db_path) as db:
            db.execute("UPDATE journal_entries SET image_path=NULL,updated_ms=? WHERE id=?", (self.clock(), entry_id))
        if previous: self._remove_private_file(previous)
        return self.get(entry_id)

    def import_paper_trades(self, engine, account_id, limit=100):
        with connect(self.db_path) as db:
            account=db.execute("SELECT strategy_type FROM paper_accounts WHERE id=?", (account_id,)).fetchone()
        if not account: raise PaperError("NOT_FOUND", "Paper account not found", 404)
        source = "PAPER_USER" if account["strategy_type"] == "USER" else "PAPER_SYSTEM"
        imported=[]
        for trade in engine.records(account_id, "trades", limit):
            entry_id=f"paper:{trade['id']}"
            try:
                entry=self.create(JournalEntryRequest(id=entry_id,source=source,side=trade["direction"],occurred_ms=trade["closed_ms"],paper_account_id=account_id,paper_position_id=trade["id"],user_reason=f"Paper trade: {trade['reason']}; entry {trade['entry_price']}; exit {trade['exit_price']}; PnL {trade['profit_loss']}"))
                imported.append(entry)
            except PaperError as error:
                if error.code != "JOURNAL_EXISTS": raise
        return imported

    def import_all_paper_trades(self, engine, mark_price):
        imported=[]
        for account in engine.accounts(mark_price):
            imported.extend(self.import_paper_trades(engine, account["account_id"]))
        return imported

    def _remove_private_file(self, value):
        path = Path(value)
        if path.parent == self.upload_dir and path.is_file(): path.unlink()
