from app.platform import ModuleManifest, ModuleStatus

MANIFEST = ModuleManifest(
    key="labor",
    version="0.1.0",
    status=ModuleStatus.BETA,
    order=20,
    # Only the owners an admin creates (or gives it to) see it; admins see every owner's.
    audience="granted",
    router="app.modules.labor.router:router",
    admin_router="app.modules.labor.admin:router",
    documents=(
        "app.modules.labor.models:LaborProject",
        "app.modules.labor.models:LaborWorker",
        "app.modules.labor.models:LaborWageChange",
        "app.modules.labor.models:LaborAttendance",
        "app.modules.labor.models:LaborPayment",
        "app.modules.labor.models:LaborAuditEntry",
    ),
    tile_stat="app.modules.labor.stats:open_projects",
)
