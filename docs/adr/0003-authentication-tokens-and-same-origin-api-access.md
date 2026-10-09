# ADR-0003: Authentication tokens and same-origin API access

- **Status:** Accepted
- **Date:** 2026-10-08
- **Deciders:** project owner (plan approved 2026-10-08)

## Context

We need short-lived credentials, revocation and protection against token theft via XSS. On free hosting the frontend (`*.pages.dev`) and API (`*.onrender.com`) live on different sites, so browsers increasingly block cookies set across sites.

## Decision

The access token is a short-lived JWT (15 min, configurable) kept in memory only. The refresh token is opaque and random, stored SHA-256-hashed. It rotates on every use with reuse detection (reusing an old token revokes the whole family) and travels in an `HttpOnly; Secure; SameSite=Strict` cookie scoped to the auth path. The frontend always calls `/api/*` on its own origin: the Vite proxy in development, a Cloudflare Pages Function in production. A token-version claim invalidates access tokens on password change, disable or admin reset.

## Consequences

No tokens in `localStorage` (a weakness seen in the reference). The cookie stays first-party. CORS is mostly unnecessary. The proxy adds a small hop, counted against the Workers free quota (100k requests/day).

## Alternatives considered

Tokens in localStorage: exposed to XSS. Cross-site cookies with `SameSite=None`: blocked by modern browsers. A custom domain for both apps: costs money.

## Implementation notes (Phase 2, 2026-10-08)

- **Sessions.** A *session* is one signed-in device: a single `auth_sessions` document holds
  the digest of the current refresh token plus the last 50 replaced digests.
  - Presenting a replaced token after the 10 s grace window revokes the session
    (`AUTH_REFRESH_REUSED`).
  - Within the grace window it is treated as two tabs racing (`409 AUTH_REFRESH_RACE`), and
    the client retries with the cookie the browser now holds.
  - The frontend also serialises refreshes across tabs with the Web Locks API.
- **`sid` claim.** Access tokens carry a `sid` (session id) claim, so `GET /v1/me/sessions`
  can mark the current device without the refresh cookie. The cookie is scoped to the auth
  path and never reaches `/v1/me`.
- **Revocation timing.**
  - Signing out one *other* device ends its refresh session at once. Its access token stays
    valid until it expires (at most `AUTH_ACCESS_TOKEN_TTL_SECONDS`, 15 min by default), so a
    request never needs an extra session lookup.
  - Password change, logout-all, admin reset, disable and role change take effect
    immediately: the user is reloaded on every request and the `tv` claim must match.
- **CSRF.** `/refresh` and `/logout` (cookie-authenticated) require the `X-TT-CSRF` header
  (`AUTH_CSRF_HEADER`). The SPA sends it on every request.
