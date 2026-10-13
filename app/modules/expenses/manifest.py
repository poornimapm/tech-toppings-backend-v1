from app.platform import ModuleManifest, ModuleStatus

MANIFEST = ModuleManifest(
    key="expenses",
    version="0.2.0",
    status=ModuleStatus.BETA,  # live once voice capture is in (Phase 6)
    order=10,
    router="app.modules.expenses.router:router",
    documents=(
        "app.modules.expenses.features.entries.models:Entry",
        "app.modules.expenses.features.entries.models:EntryRevision",
        "app.modules.expenses.features.categories.models:Category",
    ),
    tile_stat="app.modules.expenses.stats:month_total",
)
