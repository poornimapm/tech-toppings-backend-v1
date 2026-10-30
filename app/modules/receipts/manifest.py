from app.platform import ModuleManifest, ModuleStatus

MANIFEST = ModuleManifest(
    key="receipts",
    version="0.2.0",
    status=ModuleStatus.BETA,
    order=30,
    router="app.modules.receipts.router:router",
    documents=("app.modules.receipts.models:ReceiptsItem",),
    tile_stat="app.modules.receipts.stats:receipt_count",
)
