"""Request dependencies: the current user, role checks, the caller's data scope, CSRF.

Use the strictest that fits:

* ``CurrentUser``: signed in and not forced to change password (the default for every route)
* ``CurrentUserAllowingPasswordChange``: also lets a must-change-password user through
  (only ``GET /v1/me``, password change and logout-all)
* ``AdminUser``: role admin
* ``UserScope``: ``Scope.for_user(current user)`` for ``ScopedRepository``
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, cast

import structlog
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.deps import ClockDep, SettingsDep
from app.core.errors import ForbiddenError, UnauthenticatedError
from app.core.rate_limit import client_ip
from app.core.repository import Scope
from app.core.security import PasswordHasher
from app.platform.auth.errors import CsrfError, PasswordChangeRequiredError, SessionRevokedError
from app.platform.auth.google import GoogleIdTokenVerifier
from app.platform.auth.service import AuthService
from app.platform.auth.sessions import ClientInfo, SessionService
from app.platform.auth.tokens import AccessClaims, AccessTokenService
from app.platform.users.models import Role, User
from app.platform.users.repository import UserRepository

_bearer = HTTPBearer(auto_error=False, description="Access token from /v1/auth/login")

USER_AGENT_MAX_LENGTH = 256


def get_auth_service(request: Request, settings: SettingsDep, clock: ClockDep) -> AuthService:
    state = request.app.state
    return AuthService(
        settings=settings,
        clock=clock,
        users=UserRepository(clock),
        sessions=SessionService(settings.auth, clock),
        tokens=AccessTokenService(settings.auth, clock),
        hasher=cast(PasswordHasher, state.password_hasher),
        google=cast(GoogleIdTokenVerifier | None, state.google_verifier),
    )


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


def get_client_info(request: Request) -> ClientInfo:
    user_agent = request.headers.get("user-agent")
    return ClientInfo(
        user_agent=user_agent[:USER_AGENT_MAX_LENGTH] if user_agent else None,
        ip=client_ip(request),
    )


ClientInfoDep = Annotated[ClientInfo, Depends(get_client_info)]


async def access_claims(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: SettingsDep,
    clock: ClockDep,
) -> AccessClaims:
    """Verified claims of the bearer token (FastAPI caches this per request)."""
    if credentials is None:
        raise UnauthenticatedError
    return AccessTokenService(settings.auth, clock).verify(credentials.credentials)


CurrentClaims = Annotated[AccessClaims, Depends(access_claims)]


async def authenticated_user(claims: CurrentClaims, clock: ClockDep) -> User:
    user = await UserRepository(clock).by_id(claims.user_id)
    # token_version mismatch = signed out everywhere / password changed since this token.
    if user is None or not user.is_active or user.token_version != claims.token_version:
        raise SessionRevokedError
    structlog.contextvars.bind_contextvars(user_id=str(user.id))
    return user


async def active_user(user: Annotated[User, Depends(authenticated_user)]) -> User:
    if user.must_change_password:
        raise PasswordChangeRequiredError
    return user


def require_role(role: Role) -> Callable[[User], Awaitable[User]]:
    async def dependency(user: Annotated[User, Depends(active_user)]) -> User:
        if user.role is not role:
            raise ForbiddenError
        return user

    return dependency


CurrentUser = Annotated[User, Depends(active_user)]
CurrentUserAllowingPasswordChange = Annotated[User, Depends(authenticated_user)]
AdminUser = Annotated[User, Depends(require_role(Role.ADMIN))]


def user_scope(user: CurrentUser) -> Scope:
    if user.id is None:  # pragma: no cover - persisted users always have an id
        raise UnauthenticatedError
    return Scope.for_user(user.id)


UserScope = Annotated[Scope, Depends(user_scope)]


def require_csrf_header(request: Request, settings: SettingsDep) -> None:
    """Cookie-authenticated endpoints need a custom header: cross-site forms can't add one
    and cross-origin scripts are stopped by CORS preflight."""
    if not request.headers.get(settings.auth.csrf_header):
        raise CsrfError
