"""Everything under ``/v1/admin/expenses``: every route depends on the admin role."""

from fastapi import APIRouter

from app.modules.expenses.features.entries.admin import router as entries

router = APIRouter()
router.include_router(entries)
