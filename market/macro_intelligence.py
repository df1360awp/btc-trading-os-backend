"""AI explanation and rating of released macro events; never an execution source."""
import json
from market.ai_review import ReviewService

class MacroIntelligenceService:
    def __init__(self, requester=None): self.requester=requester or ReviewService()._request
    def evaluate(self, context):
        prompt=("You are BTC Trading OS macro intelligence. Return JSON only with rating (integer 1..5), bias (BULLISH/BEARISH/MIXED), and analysis (Chinese). Compare Actual/Forecast/Previous, DXY, US Treasury yield, gold, oil and supplied BTC market context. Explain evidence and uncertainty. This is retrospective research, never a trading instruction or prediction.\n\n"+json.dumps(context,ensure_ascii=False,default=str))
        text,model=self.requester(prompt,None)
        try: value=json.loads(text.strip().removeprefix("```json").removesuffix("```").strip())
        except json.JSONDecodeError: value={"rating":3,"bias":"MIXED","analysis":text}
        rating=max(1,min(5,int(value.get("rating",3)))); bias=value.get("bias","MIXED")
        if bias not in {"BULLISH","BEARISH","MIXED"}: bias="MIXED"
        return {"rating":rating,"bias":bias,"analysis":str(value.get("analysis",text)),"model":model,"context":context}
