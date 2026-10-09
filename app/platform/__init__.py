"""Platform layer: capabilities shared by all modules (auth, users, module registry, ...).

Feature modules import from this package only (``from app.platform import CurrentUser``), never
from ``app.core`` or a platform sub-package: this is the API modules are written against, so
the platform's internals can change without touching every module. Platform code may import
``app.core`` but never a module. Both rules are enforced (import-linter + an architecture test).
"""

from app.core.clock import Clock
from app.core.deps import ClockDep, SettingsDep
from app.core.documents import OwnedDocument, TimestampedDocument
from app.core.errors import (
    AppError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    error_responses,
)
from app.core.logging import get_logger
from app.core.pagination import Page, PageParams, PageParamsDep, parse_sort
from app.core.repository import Scope, ScopedRepository
from app.platform.auth.deps import AdminUser, CurrentUser, UserScope
from app.platform.auth.models import AuthSession
from app.platform.modules.manifest import ModuleManifest, ModuleStatus, TileStat
from app.platform.modules.models import ModuleOverride, UserModule
from app.platform.users.models import User

# Beanie document models owned by the platform; registered with the database at startup.
PLATFORM_DOCUMENTS = (User, AuthSession, ModuleOverride, UserModule)

__all__ = [
    "PLATFORM_DOCUMENTS",
    "AdminUser",
    "AppError",
    "Clock",
    "ClockDep",
    "ConflictError",
    "CurrentUser",
    "ForbiddenError",
    "ModuleManifest",
    "ModuleStatus",
    "NotFoundError",
    "OwnedDocument",
    "Page",
    "PageParams",
    "PageParamsDep",
    "Scope",
    "ScopedRepository",
    "SettingsDep",
    "TileStat",
    "TimestampedDocument",
    "User",
    "UserScope",
    "error_responses",
    "get_logger",
    "parse_sort",
]
