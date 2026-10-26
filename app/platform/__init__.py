"""Platform layer: capabilities shared by all modules (auth, users, module registry, ...).

Feature modules import from this package only (``from app.platform import CurrentUser``), never
from ``app.core`` or a platform sub-package: this is the API modules are written against, so
the platform's internals can change without touching every module. Platform code may import
``app.core`` but never a module. Both rules are enforced (import-linter + an architecture test).
"""

from app.core.clock import Clock
from app.core.config import AgentMode
from app.core.daterange import DateRange
from app.core.deps import ClockDep, SettingsDep
from app.core.documents import OwnedDocument, TimestampedDocument
from app.core.errors import (
    AppError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    error_responses,
    patched,
)
from app.core.idempotency import REPLAYED_HEADER, IdempotencyKey, answer_once
from app.core.logging import get_logger
from app.core.money import convert, major_text, to_major, to_minor
from app.core.pagination import Page, PageParams, PageParamsDep, page_params, parse_sort
from app.core.rate_limit import rate_limit
from app.core.repository import NEWEST_FIRST, Scope, ScopedRepository
from app.core.revisions import (
    CLIENT_KEY_INDEX,
    REVISION_INDEXES,
    FieldChange,
    Revision,
    RevisionAction,
    RevisionedDocument,
    RevisionedRepository,
    VersionConflictError,
)
from app.platform.activity.log import Activity, ActivityLog
from app.platform.admin.deps import AdminAction, AdminActionDep, AdminDep
from app.platform.admin.tally import ErrorTally
from app.platform.agents.catalog import StaticOutcome, StaticRequest
from app.platform.agents.lexicon import load_lexicon, match_action, normalise
from app.platform.agents.models import AgentRun, AgentUsage, RunStatus
from app.platform.agents.runtime import run_skill
from app.platform.agents.tools import ToolBox, tool
from app.platform.audit.models import AuditEntry
from app.platform.audit.service import AuditLog
from app.platform.auth.allowlist import AllowedEmail
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
from app.platform.fx.models import FxRate
from app.platform.fx.service import Fx, FxUnavailableError, FxUnsupportedError
from app.platform.jobs.contract import job
from app.platform.jobs.models import JobRun
from app.platform.modules.manifest import ModuleManifest, ModuleStatus, TileStat
from app.platform.modules.models import ModuleOverride, UserModule
from app.platform.notifications.inbox import Inbox, Notification
from app.platform.push.models import PushSubscription
from app.platform.reminders.models import Reminder
from app.platform.reminders.service import Reminders
from app.platform.search.contract import SearchHit, search
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
    JobRun,
    Reminder,
    FxRate,
    AuditEntry,
    AllowedEmail,
    ErrorTally,
    PushSubscription,
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
    "AdminAction",
    "AdminActionDep",
    "AdminDep",
    "AdminUser",
    "AgentMode",
    "AppError",
    "AuditLog",
    "Clock",
    "ClockDep",
    "ConflictError",
    "CurrentUser",
    "DateRange",
    "FieldChange",
    "ForbiddenError",
    "Fx",
    "FxUnavailableError",
    "FxUnsupportedError",
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
    "Reminder",
    "Reminders",
    "Revision",
    "RevisionAction",
    "RevisionedDocument",
    "RevisionedRepository",
    "RunStatus",
    "Scope",
    "ScopedRepository",
    "SearchHit",
    "Series",
    "SettingsDep",
    "StaticOutcome",
    "StaticRequest",
    "TileStat",
    "TimestampedDocument",
    "ToolBox",
    "User",
    "UserScope",
    "VersionConflictError",
    "answer_once",
    "convert",
    "count_in",
    "daily_counts",
    "error_responses",
    "get_logger",
    "job",
    "load_lexicon",
    "major_text",
    "match_action",
    "normalise",
    "page_params",
    "parse_sort",
    "patched",
    "rate_limit",
    "run_skill",
    "search",
    "to_major",
    "to_minor",
    "tool",
    "widget",
]
