"""AI explanation for released macro events; it has no execution capability."""
import json
from market.ai_review import ReviewService


class MacroAnalysisService:
    def __init__(self, requester=None): self.requester = requester or ReviewService()._request
    def explain(self, context):
        prompt = ("You are the macro explanation layer of BTC Trading OS. In Chinese compare actual, forecast, and previous values, then explain the supplied BTC price, OI, and funding snapshots. "
                  "Separate observed facts from uncertainty and list risk factors. Do not predict price, issue buy/sell instructions, or execute trades.\n\n" + json.dumps(context, ensure_ascii=False, default=str))
        analysis, model = self.requester(prompt, None)
        return {"analysis":analysis,"model":model,"context":context}
