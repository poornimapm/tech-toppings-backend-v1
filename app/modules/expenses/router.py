"""Everything under ``/v1/expenses``: each feature brings its own router."""

from fastapi import APIRouter

from app.modules.expenses.features.categories.router import router as categories
from app.modules.expenses.features.entries.router import router as entries
from app.modules.expenses.features.products.router import router as products
from app.modules.expenses.units import UnitOut, units
from app.platform import error_responses

router = APIRouter()
router.include_router(entries)
router.include_router(categories)
router.include_router(products)


@router.get("/units", summary="Units a quantity can be in", responses=error_responses(401, 403))
async def list_units() -> list[UnitOut]:
    return [
        UnitOut(code=unit.code, dimension=unit.dimension, factor=float(unit.factor))
        for unit in units().values()
    ]
