from app.platform import ModuleManifest, ModuleStatus

MANIFEST = ModuleManifest(
    key="expenses",
    version="0.3.0",
    status=ModuleStatus.BETA,  # live once voice capture is in (Phase 6)
    order=10,
    router="app.modules.expenses.router:router",
    documents=(
        "app.modules.expenses.features.entries.models:Entry",
        "app.modules.expenses.features.entries.models:EntryRevision",
        "app.modules.expenses.features.categories.models:Category",
        "app.modules.expenses.features.products.models:Product",
        "app.modules.expenses.features.insights.models:Insight",
        "app.modules.expenses.features.budgets.models:Budget",
        "app.modules.expenses.features.goals.models:Goal",
        "app.modules.expenses.features.goals.models:GoalRevision",
        "app.modules.expenses.features.ious.models:Iou",
        "app.modules.expenses.features.ious.models:IouRevision",
        "app.modules.expenses.features.recurring.models:RecurringRule",
        "app.modules.expenses.features.recurring.models:RecurringSuggestion",
    ),
    tile_stat="app.modules.expenses.stats:month_total",
)
