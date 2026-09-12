"""AI explanation of existing market and signal outputs; never an execution source."""
import json
from market.ai_review import ReviewService


class MarketAnalysisService:
    def __init__(self, requester=None):
        self.requester = requester or ReviewService()._request

    def explain(self, context):
        prompt = ("You are the explanation layer of BTC Trading OS. Analyze only the supplied market state and Signal Engine output. "
                  "In Chinese explain market regime, long/short pressure, OI and CVD behavior, funding crowding, OBI, liquidation pressure, supporting/conflicting factors, and key risks. "
                  "Conclude whether evidence is mixed or coherent, but do not predict price, issue a trade instruction, or execute anything.\n\n" + json.dumps(context, ensure_ascii=False, default=str))
        analysis, model = self.requester(prompt, None)
        return {"analysis": analysis, "model": model, "context": context}
