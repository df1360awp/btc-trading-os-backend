"""AI explanation of existing market and signal outputs; never an execution source."""
import json
import time
import uuid
from market.ai_review import ReviewService
from market.paper_trading import connect


def init_market_analysis_tables(db_path):
    with connect(db_path) as db: db.executescript("""
      CREATE TABLE IF NOT EXISTS market_analysis_reports(
        id TEXT PRIMARY KEY,context_json TEXT NOT NULL,analysis TEXT NOT NULL,
        model TEXT NOT NULL,created_ms INTEGER NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_market_analysis_created ON market_analysis_reports(created_ms DESC);
    """)


class MarketAnalysisStore:
    def __init__(self, db_path, clock=None): self.db_path, self.clock=db_path, clock or (lambda:time.time_ns()//1_000_000)
    def create(self,result):
        item={"id":str(uuid.uuid4()),"context_json":json.dumps(result["context"],ensure_ascii=False,default=str),"analysis":result["analysis"],"model":result["model"],"created_ms":self.clock()}
        with connect(self.db_path) as db: db.execute("INSERT INTO market_analysis_reports(id,context_json,analysis,model,created_ms) VALUES(:id,:context_json,:analysis,:model,:created_ms)",item)
        return self.get(item["id"])
    def get(self,report_id):
        with connect(self.db_path) as db: row=db.execute("SELECT * FROM market_analysis_reports WHERE id=?",(report_id,)).fetchone()
        result=dict(row); result["context"]=json.loads(result.pop("context_json")); return result
    def list(self,limit=100):
        with connect(self.db_path) as db: rows=db.execute("SELECT * FROM market_analysis_reports ORDER BY created_ms DESC LIMIT ?",(limit,)).fetchall()
        return [self.get(row["id"]) for row in rows]


class MarketAnalysisService:
    def __init__(self, requester=None):
        self.requester = requester or ReviewService()._request

    def explain(self, context):
        prompt = ("You are the explanation layer of BTC Trading OS. Analyze only the supplied market state, macro intelligence, risk events and Signal Engine output. "
                  "In Chinese explain market regime, long/short pressure, OI and CVD behavior, funding crowding, OBI, liquidation pressure, supporting/conflicting factors, and key risks. "
                  "Conclude whether evidence is mixed or coherent, but do not predict price, issue a trade instruction, or execute anything.\n\n" + json.dumps(context, ensure_ascii=False, default=str))
        analysis, model = self.requester(prompt, None)
        return {"analysis": analysis, "model": model, "context": context}
