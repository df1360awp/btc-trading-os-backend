"""Protected journal endpoints; review execution is added separately from storage."""
from fastapi import APIRouter, Depends, Query

from market.journal import ImageRequest, JournalEntryRequest, JournalStore
from market.paper_trading import require_paper_key

store = JournalStore()
router = APIRouter(prefix="/journal", tags=["Trading Journal"], dependencies=[Depends(require_paper_key)])


@router.post("/entries", status_code=201)
def create_entry(request: JournalEntryRequest): return store.create(request)


@router.get("/entries")
def list_entries(limit: int = Query(default=100, ge=1, le=500)): return store.list(limit)


@router.get("/entries/{entry_id}")
def get_entry(entry_id: str): return store.get(entry_id)


@router.put("/entries/{entry_id}/image")
def attach_image(entry_id: str, request: ImageRequest): return store.attach_image(entry_id, request)
