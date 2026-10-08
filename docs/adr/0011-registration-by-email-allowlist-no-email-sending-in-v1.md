# ADR-0011: Registration by email allowlist; no email sending in v1

- **Status:** Accepted
- **Date:** 2026-10-08
- **Deciders:** project owner (plan approved 2026-10-08)

## Context

A public URL on free quotas must not be open to everyone. The owner decided against an email provider in v1.

## Decision

`AUTH_REGISTRATION_MODE` is `allowlist` by default (also `open` or `closed`). Register and Google sign-in require an allowed email: the `AUTH_ALLOWED_EMAILS` env list now, plus an admin-managed collection from Phase 10. There are no forgot-password, reset or verification emails. Recovery is an admin-issued one-time password with a forced change at next login, or Google sign-in.

## Consequences

Zero email cost and setup. The residual risk is that someone who knows an allowlisted email could claim it first. Mitigations: keep the list private, the admin can review, disable or reset accounts, and Google sign-in proves ownership.

## Alternatives considered

Invite codes: more UI. Open registration: quota drain. An email provider: an extra account, secrets and deliverability work.
