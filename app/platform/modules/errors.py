"""Module system error codes; clients translate them (ADR-0009)."""

from __future__ import annotations

from app.core.errors import AppError, ConflictError, ForbiddenError, NotFoundError


class UnknownModuleError(NotFoundError):
    code = "MODULE_NOT_FOUND"
    default_message = "There is no such module."


class ModuleDisabledError(ForbiddenError):
    code = "MODULE_DISABLED"
    default_message = "This module is turned off."


class ModuleHasNoSettingsError(NotFoundError):
    code = "MODULE_NO_SETTINGS"
    default_message = "This module has no settings."


class ModuleNotUpcomingError(ConflictError):
    code = "MODULE_NOT_UPCOMING"
    default_message = "This module is already available."


class InvalidModuleSettingsError(AppError):
    status_code = 422
    code = "MODULE_SETTINGS_INVALID"
    default_message = "Some settings are invalid."
