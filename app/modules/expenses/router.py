"""Everything under ``/v1/expenses``: each feature brings its own router."""

from fastapi import APIRouter

from app.modules.expenses.features.analytics.router import router as analytics
from app.modules.expenses.features.budgets.router import router as budgets
from app.modules.expenses.features.categories.router import router as categories
from app.modules.expenses.features.entries.router import router as entries
from app.modules.expenses.features.exports.router import router as exports
from app.modules.expenses.features.goals.router import router as goals
from app.modules.expenses.features.imports.router import router as imports
from app.modules.expenses.features.insights.router import router as insights
from app.modules.expenses.features.ious.router import router as ious
from app.modules.expenses.features.products.router import router as products
from app.modules.expenses.features.queries.router import router as queries
from app.modules.expenses.features.recurring.router import router as recurring
from app.modules.expenses.features.reports.router import router as reports
from app.modules.expenses.features.saved_filters.router import router as saved_filters
from app.modules.expenses.units import UnitOut, units
from app.platform import error_responses

router = APIRouter()
router.include_router(entries)
router.include_router(categories)
router.include_router(products)
router.include_router(analytics)
router.include_router(insights)
router.include_router(budgets)
router.include_router(goals)
router.include_router(ious)
router.include_router(recurring)
router.include_router(saved_filters)
router.include_router(exports)
router.include_router(imports)
router.include_router(reports)
router.include_router(queries)


@router.get("/units", summary="Units a quantity can be in", responses=error_responses(401, 403))
async def list_units() -> list[UnitOut]:
    return [
        UnitOut(code=unit.code, dimension=unit.dimension, factor=float(unit.factor))
        for unit in units().values()
    ]
