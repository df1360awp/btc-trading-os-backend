"""Protected journal endpoints; review execution is added separately from storage."""
from fastapi import APIRouter, Depends, Query

from market.journal import ImageRequest, JournalEntryRequest, JournalStore
from market.ai_review import ReviewService
from market.paper_trading import require_paper_key
from market.paper_api import engine

store = JournalStore()
reviews = ReviewService(store)
router = APIRouter(prefix="/journal", tags=["Trading Journal"], dependencies=[Depends(require_paper_key)])


@router.post("/entries", status_code=201)
def create_entry(request: JournalEntryRequest): return store.create(request)


@router.get("/entries")
def list_entries(limit: int = Query(default=100, ge=1, le=500)): return store.list(limit)


@router.get("/entries/{entry_id}")
def get_entry(entry_id: str): return store.get(entry_id)


@router.put("/entries/{entry_id}")
def update_entry(entry_id: str, request: JournalEntryRequest): return store.update(entry_id, request)


@router.delete("/entries/{entry_id}")
def delete_entry(entry_id: str): return store.delete(entry_id)


@router.put("/entries/{entry_id}/image")
def attach_image(entry_id: str, request: ImageRequest): return store.attach_image(entry_id, request)


@router.delete("/entries/{entry_id}/image")
def remove_image(entry_id: str): return store.remove_image(entry_id)


@router.post("/import-paper/{account_id}", status_code=201)
def import_paper(account_id: str, limit: int = Query(default=100, ge=1, le=500)): return store.import_paper_trades(engine, account_id, limit)


@router.get("/summary")
def journal_summary(start_ms: int | None = None, end_ms: int | None = None): return store.summary(start_ms, end_ms)


@router.post("/entries/{entry_id}/reviews", status_code=201)
def review_entry(entry_id: str): return reviews.create_entry_review(entry_id)


@router.post("/reviews/{period}", status_code=201)
def review_period(period: str, end_ms: int | None = None): return reviews.create_period_review(period.upper(), end_ms)


@router.get("/reviews/{review_id}")
def get_review(review_id: str): return reviews.get(review_id)


@router.get("/reviews")
def list_reviews(entry_id: str | None = None, limit: int = Query(default=100, ge=1, le=500)): return reviews.list(entry_id, limit)
