"""Platform layer: capabilities shared by all modules (auth, users, module registry, ...).

Feature modules import from this package only (``from app.platform import CurrentUser``), never
from ``app.core`` or a platform sub-package: this is the API modules are written against, so
the platform's internals can change without touching every module. Platform code may import
``app.core`` but never a module. Both rules are enforced (import-linter + an architecture test).
"""

from app.core.clock import Clock
from app.core.daterange import DateRange
from app.core.deps import ClockDep, SettingsDep
from app.core.documents import OwnedDocument, TimestampedDocument
from app.core.errors import (
    AppError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    error_responses,
)
from app.core.idempotency import REPLAYED_HEADER, IdempotencyKey
from app.core.logging import get_logger
from app.core.money import to_major, to_minor
from app.core.pagination import Page, PageParams, PageParamsDep, page_params, parse_sort
from app.core.repository import NEWEST_FIRST, Scope, ScopedRepository
from app.core.revisions import (
    CLIENT_KEY_INDEX,
    REVISION_INDEXES,
    FieldChange,
    Revision,
    RevisionAction,
    RevisionedDocument,
    RevisionedRepository,
)
from app.platform.activity.log import Activity, ActivityLog
from app.platform.agents.catalog import StaticOutcome, StaticRequest
from app.platform.agents.lexicon import load_lexicon, match_action, normalise
from app.platform.agents.models import AgentRun, AgentUsage
from app.platform.agents.tools import ToolBox, tool
from app.platform.auth.deps import AdminUser, CurrentUser, UserScope
from app.platform.auth.models import AuthSession
from app.platform.context import ActorContext, ActorDep
from app.platform.dashboard.contract import (
    Item,
    Items,
    Kpi,
    Point,
    Series,
    count_in,
    daily_counts,
    widget,
)
from app.platform.modules.manifest import ModuleManifest, ModuleStatus, TileStat
from app.platform.modules.models import ModuleOverride, UserModule
from app.platform.notifications.inbox import Inbox, Notification
from app.platform.users.models import User

# Beanie document models owned by the platform; registered with the database at startup.
PLATFORM_DOCUMENTS = (
    User,
    AuthSession,
    ModuleOverride,
    UserModule,
    AgentRun,
    AgentUsage,
    Activity,
    Notification,
)

__all__ = [
    "CLIENT_KEY_INDEX",
    "NEWEST_FIRST",
    "PLATFORM_DOCUMENTS",
    "REPLAYED_HEADER",
    "REVISION_INDEXES",
    "ActivityLog",
    "ActorContext",
    "ActorDep",
    "AdminUser",
    "AppError",
    "Clock",
    "ClockDep",
    "ConflictError",
    "CurrentUser",
    "DateRange",
    "FieldChange",
    "ForbiddenError",
    "IdempotencyKey",
    "Inbox",
    "Item",
    "Items",
    "Kpi",
    "ModuleManifest",
    "ModuleStatus",
    "NotFoundError",
    "OwnedDocument",
    "Page",
    "PageParams",
    "PageParamsDep",
    "Point",
    "Revision",
    "RevisionAction",
    "RevisionedDocument",
    "RevisionedRepository",
    "Scope",
    "ScopedRepository",
    "Series",
    "SettingsDep",
    "StaticOutcome",
    "StaticRequest",
    "TileStat",
    "TimestampedDocument",
    "ToolBox",
    "User",
    "UserScope",
    "count_in",
    "daily_counts",
    "error_responses",
    "get_logger",
    "load_lexicon",
    "match_action",
    "normalise",
    "page_params",
    "parse_sort",
    "to_major",
    "to_minor",
    "tool",
    "widget",
]
