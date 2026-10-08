# ADR-0006: Hosting: Cloudflare Pages + Render + Atlas M0 (free subdomains)

- **Status:** Accepted
- **Date:** 2026-10-08
- **Deciders:** project owner (plan approved 2026-10-08)

## Context

Everything must be free with no card. Vercel Hobby is non-commercial only. Render free sleeps after 15 minutes and has an ephemeral disk. Atlas M0 has 512 MB.

## Decision

Frontend on Cloudflare Pages, with a Pages Function proxying `/api/*` to the API. API on Render's free web service, built from the repo Dockerfile. Database on Atlas M0. Free subdomains only. Limits are recorded in `docs/free-tier-limits.md` and re-verified before deployment.

## Consequences

No custom domain is needed. The frontend shows a waking-up state during cold starts. Nothing is written to local disk. Storage is managed with TTL indexes and export scripts.

## Alternatives considered

Vercel Hobby: non-commercial clause. Fly.io/Railway: card required or no free tier.
