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
