"""Auth error codes. Clients translate them (ADR-0009); messages are English fallbacks."""

from __future__ import annotations

from fastapi import status

from app.core.errors import (
    AppError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    RateLimitedError,
    UnauthenticatedError,
)


class InvalidCredentialsError(UnauthenticatedError):
    code = "AUTH_INVALID_CREDENTIALS"
    default_message = "Email or password is incorrect."


class TokenInvalidError(UnauthenticatedError):
    code = "AUTH_TOKEN_INVALID"
    default_message = "The access token is invalid."


class TokenExpiredError(UnauthenticatedError):
    code = "AUTH_TOKEN_EXPIRED"
    default_message = "The access token has expired."


class SessionRevokedError(UnauthenticatedError):
    code = "AUTH_SESSION_REVOKED"
    default_message = "This session is no longer valid. Please sign in again."


class RefreshInvalidError(UnauthenticatedError):
    code = "AUTH_REFRESH_INVALID"
    default_message = "Your session has ended. Please sign in again."


class RefreshReusedError(UnauthenticatedError):
    code = "AUTH_REFRESH_REUSED"
    default_message = "This session was signed out for your security. Please sign in again."


class RefreshRaceError(ConflictError):
    code = "AUTH_REFRESH_RACE"
    default_message = "The session was just refreshed elsewhere. Retry the request."


class CsrfError(ForbiddenError):
    code = "AUTH_CSRF_FAILED"
    default_message = "Missing anti-forgery header."


class AccountDisabledError(ForbiddenError):
    code = "AUTH_ACCOUNT_DISABLED"
    default_message = "This account is disabled."


class AccountLockedError(RateLimitedError):
    code = "AUTH_ACCOUNT_LOCKED"
    default_message = "Too many failed sign-in attempts. Try again later."


class PasswordChangeRequiredError(ForbiddenError):
    code = "AUTH_PASSWORD_CHANGE_REQUIRED"
    default_message = "Please set a new password before continuing."


class PasswordPolicyError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "AUTH_PASSWORD_POLICY"
    default_message = "The password does not meet the requirements."


class RegistrationClosedError(ForbiddenError):
    code = "AUTH_REGISTRATION_CLOSED"
    default_message = "Registration is closed."


class RegistrationNotAllowedError(ForbiddenError):
    code = "AUTH_REGISTRATION_NOT_ALLOWED"
    default_message = "This email is not invited to register."


class GoogleSignInDisabledError(NotFoundError):
    code = "AUTH_GOOGLE_DISABLED"
    default_message = "Google sign-in is not enabled."


class GoogleTokenInvalidError(UnauthenticatedError):
    code = "AUTH_GOOGLE_TOKEN_INVALID"
    default_message = "The Google sign-in could not be verified."


class CurrentPasswordInvalidError(AppError):
    # 422, not 401: a wrong *current* password must not look like an expired session to clients.
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "AUTH_CURRENT_PASSWORD_INVALID"
    default_message = "The current password is incorrect."
