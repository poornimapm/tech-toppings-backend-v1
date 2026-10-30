"""Platform layer: capabilities shared by all modules (auth, users, module registry, ...).

Feature modules import from this package only (``from app.platform import CurrentUser``), never
from ``app.core`` or a platform sub-package: this is the API modules are written against, so
the platform's internals can change without touching every module. Platform code may import
``app.core`` but never a module. Both rules are enforced (import-linter + an architecture test).
"""

from app.core.clock import Clock
from app.core.config import AgentMode
from app.core.context import current_request_id
from app.core.csv_file import (
    CSV_MEDIA_TYPE,
    CSV_RESPONSE,
    attachment,
    csv_text,
    csv_writer,
    safe_text,
)
from app.core.daterange import DateRange, on_or_before
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
from app.core.repository import NEWEST_FIRST, Scope, ScopedRepository, parse_object_id
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
from app.platform.agents.lexicon import (
    load_lexicon,
    match_action,
    normalise,
    western_digits,
)
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
from app.platform.files.models import StoredFile
from app.platform.files.service import (
    Files,
    FileTooLargeError,
    FileTypeError,
    file_response,
)
from app.platform.fx.models import FxRate
from app.platform.fx.service import Fx, FxUnavailableError, FxUnsupportedError
from app.platform.jobs.contract import job
from app.platform.jobs.models import JobRun
from app.platform.modules.grants import granted_users
from app.platform.modules.manifest import ModuleManifest, ModuleStatus, TileStat
from app.platform.modules.models import ModuleOverride, UserModule
from app.platform.notifications.inbox import Inbox, Notification
from app.platform.push.models import PushSubscription
from app.platform.reminders.models import Reminder
from app.platform.reminders.service import Reminders
from app.platform.search.contract import SearchHit, search
from app.platform.slips.reader import (
    Slip,
    SlipFigure,
    SlipLine,
    SlipWords,
    load_slip_words,
    read_slip,
    word_pattern,
)
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
    StoredFile,
)

__all__ = [
    "CLIENT_KEY_INDEX",
    "CSV_MEDIA_TYPE",
    "CSV_RESPONSE",
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
    "AgentRun",
    "AppError",
    "AuditLog",
    "Clock",
    "ClockDep",
    "ConflictError",
    "CurrentUser",
    "DateRange",
    "FieldChange",
    "FileTooLargeError",
    "FileTypeError",
    "Files",
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
    "Slip",
    "SlipFigure",
    "SlipLine",
    "SlipWords",
    "StaticOutcome",
    "StaticRequest",
    "StoredFile",
    "TileStat",
    "TimestampedDocument",
    "ToolBox",
    "User",
    "UserScope",
    "VersionConflictError",
    "answer_once",
    "attachment",
    "convert",
    "count_in",
    "csv_text",
    "csv_writer",
    "current_request_id",
    "daily_counts",
    "error_responses",
    "file_response",
    "get_logger",
    "granted_users",
    "job",
    "load_lexicon",
    "load_slip_words",
    "major_text",
    "match_action",
    "normalise",
    "on_or_before",
    "page_params",
    "parse_object_id",
    "parse_sort",
    "patched",
    "rate_limit",
    "read_slip",
    "run_skill",
    "safe_text",
    "search",
    "to_major",
    "to_minor",
    "tool",
    "western_digits",
    "widget",
    "word_pattern",
]
