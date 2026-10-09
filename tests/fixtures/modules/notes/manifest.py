from pydantic import BaseModel, ConfigDict, Field

from app.platform import ModuleManifest, ModuleStatus


class NotesSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_sort: str = Field(default="newest", pattern="^(newest|oldest)$")
    preview_length: int = Field(default=80, ge=10, le=500)


MANIFEST = ModuleManifest(
    key="notes",
    version="1.0.0",
    status=ModuleStatus.LIVE,
    order=5,
    settings_model=NotesSettings,
    router="tests.fixtures.modules.notes.router:router",
    admin_router="tests.fixtures.modules.notes.router:admin_router",
    documents=("tests.fixtures.modules.notes.models:Note",),
    tile_stat="tests.fixtures.modules.notes.stats:note_count",
)
