"""Everything under ``/v1/expenses``: each feature brings its own router."""

from fastapi import APIRouter

from app.modules.expenses.features.categories.router import router as categories
from app.modules.expenses.features.entries.router import router as entries

router = APIRouter()
router.include_router(entries)
router.include_router(categories)
