"""OpenAI-backed explanation layer for journal reviews; it never executes trades."""
import base64
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from market.journal import JournalStore
from market.paper_trading import PaperError, connect


class ReviewService:
    def __init__(self, store=None, db_path="/opt/btc-trading-os/market.db", clock=None, requester=None):
        self.store, self.db_path = store or JournalStore(db_path), db_path
        self.clock = clock or (lambda: time.time_ns() // 1_000_000)
        self.requester = requester or self._request

    def market_context(self, occurred_ms):
        timestamp = datetime.fromtimestamp(occurred_ms / 1000, timezone.utc).isoformat()
        with connect(self.db_path) as db:
            rows = db.execute("""SELECT exchange,price,open_interest,oi_usd,funding_rate,timestamp
                FROM market_snapshots ORDER BY ABS(strftime('%s', timestamp) - strftime('%s', ?)) LIMIT 3""", (timestamp,)).fetchall()
        return {"requested_at": timestamp, "nearby_snapshots": [dict(row) for row in rows]}

    def create_entry_review(self, entry_id):
        entry = self.store.get(entry_id)
        context = self.market_context(entry["occurred_ms"])
        image = self._image_part(entry.get("image_path"))
        prompt = self._entry_prompt(entry, context)
        return self._run(entry_id, "ENTRY", None, None, context, prompt, image)

    def create_period_review(self, period, end_ms=None):
        if period not in {"DAILY", "WEEKLY", "MONTHLY"}: raise PaperError("INVALID_PERIOD", "Use DAILY, WEEKLY, or MONTHLY", 422)
        end_ms = end_ms or self.clock()
        spans = {"DAILY": 86400, "WEEKLY": 7 * 86400, "MONTHLY": 30 * 86400}
        start_ms = end_ms - spans[period] * 1000
        with connect(self.db_path) as db:
            rows = db.execute("SELECT * FROM journal_entries WHERE occurred_ms BETWEEN ? AND ? ORDER BY occurred_ms", (start_ms, end_ms)).fetchall()
        entries = [dict(row) for row in rows]
        context = {"period": period, "entries": entries, "entry_count": len(entries)}
        prompt = ("You are a BTC trading journal reviewer. Produce a concise Chinese review of the user's "
                  f"{period.lower()} period. Identify repeatable strengths, mistakes, psychological patterns, risk control lessons, and 3 practical improvements. "
                  "Use only supplied records. This is education and retrospective analysis, never a price prediction or trading instruction.\n\n" + json.dumps(context, ensure_ascii=False, default=str))
        return self._run(None, period, start_ms, end_ms, context, prompt, None)

    def _run(self, entry_id, period, start_ms, end_ms, context, prompt, image):
        review_id, now = str(uuid.uuid4()), self.clock()
        with connect(self.db_path) as db:
            db.execute("INSERT INTO journal_reviews(id,entry_id,period,period_start_ms,period_end_ms,status,market_context,created_ms) VALUES(?,?,?,?,?,?,?,?)", (review_id, entry_id, period, start_ms, end_ms, "RUNNING", json.dumps(context, default=str), now))
        try:
            analysis, model = self.requester(prompt, image)
        except Exception as error:
            with connect(self.db_path) as db: db.execute("UPDATE journal_reviews SET status=?,analysis=?,completed_ms=? WHERE id=?", ("FAILED", str(error)[:1000], self.clock(), review_id))
            raise
        with connect(self.db_path) as db:
            db.execute("UPDATE journal_reviews SET status=?,analysis=?,model=?,completed_ms=? WHERE id=?", ("COMPLETED", analysis, model, self.clock(), review_id))
        return self.get(review_id)

    def get(self, review_id):
        with connect(self.db_path) as db: row = db.execute("SELECT * FROM journal_reviews WHERE id=?", (review_id,)).fetchone()
        if not row: raise PaperError("NOT_FOUND", "Journal review not found", 404)
        return dict(row)

    def list(self, entry_id=None, limit=100):
        query, params = "SELECT * FROM journal_reviews", []
        if entry_id:
            query += " WHERE entry_id=?"; params.append(entry_id)
        query += " ORDER BY created_ms DESC LIMIT ?"; params.append(limit)
        with connect(self.db_path) as db: rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def _request(self, prompt, image):
        key, model = os.getenv("OPENAI_API_KEY"), os.getenv("OPENAI_REVIEW_MODEL")
        if not key or not model: raise PaperError("AI_NOT_CONFIGURED", "Set OPENAI_API_KEY and OPENAI_REVIEW_MODEL in server secrets", 503)
        content = [{"type": "input_text", "text": prompt}]
        if image: content.append(image)
        payload = json.dumps({"model": model, "store": False, "input": [{"role": "user", "content": content}]}).encode()
        request = urllib.request.Request("https://api.openai.com/v1/responses", payload, {"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=90) as response: data = json.load(response)
        except urllib.error.HTTPError as error: raise PaperError("AI_REQUEST_FAILED", f"OpenAI request failed ({error.code})", 502) from error
        text = data.get("output_text") or "".join(part.get("text", "") for item in data.get("output", []) for part in item.get("content", []) if part.get("type") == "output_text")
        if not text: raise PaperError("AI_REQUEST_FAILED", "OpenAI returned no review text", 502)
        return text, model

    @staticmethod
    def _entry_prompt(entry, context):
        return ("You are a BTC trading journal reviewer. Review the user's trade rationale and psychology against the supplied market snapshot. "
                "State supporting evidence, conflicting evidence, ignored risks, timing/risk-control lessons, and specific process improvements. "
                "The image is contextual only. Do not forecast price, issue a buy/sell signal, or execute any action. Answer in Chinese.\n\n" + json.dumps({"entry": {k: v for k, v in entry.items() if k != "image_path"}, "market_context": context}, ensure_ascii=False, default=str))

    @staticmethod
    def _image_part(path):
        if not path or not Path(path).is_file(): return None
        suffix = Path(path).suffix.lower(); mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(suffix)
        if not mime: return None
        data = base64.b64encode(Path(path).read_bytes()).decode()
        return {"type": "input_image", "image_url": f"data:{mime};base64,{data}", "detail": "high"}
