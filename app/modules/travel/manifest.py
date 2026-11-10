from app.platform import ModuleManifest, ModuleStatus

MANIFEST = ModuleManifest(
    key="travel",
    version="0.1.0",
    status=ModuleStatus.BETA,
    order=35,
    router="app.modules.travel.router:router",
    documents=(
        "app.modules.travel.models:Trip",
        "app.modules.travel.models:TripRevision",
        "app.modules.travel.models:TripActivity",
        "app.modules.travel.models:ActivityRevision",
        "app.modules.travel.models:ChecklistItem",
        "app.modules.travel.models:ChecklistRevision",
        "app.modules.travel.models:Booking",
        "app.modules.travel.models:BookingRevision",
        "app.modules.travel.models:TripExpense",
        "app.modules.travel.models:ExpenseRevision",
        "app.modules.travel.models:TripEvent",
    ),
    tile_stat="app.modules.travel.stats:trips_ahead",
)
