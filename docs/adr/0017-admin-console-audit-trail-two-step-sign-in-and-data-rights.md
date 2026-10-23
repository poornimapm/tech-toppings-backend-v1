# ADR-0017: Admin console, audit trail, two-step sign-in and data rights

- **Status:** Accepted
- **Date:** 2026-10-17
- **Deciders:** project owner (Phase 10)

## Context

Phase 10 gives the operator an admin console and gives every user stronger sign-in and control
over their data. The plan's acceptance criteria shape the design:

- Admin-only enforcement is tested on every admin route.
- An audit entry exists for every sensitive change.
- The 2FA login flow is tested, including recovery codes.

The constraints still hold:

- Free tier only, and no new dependencies.
- Everything is configurable.
- Enforcement is on the backend.
- No secrets in logs.

## Decision

### Audit trail

**One audit trail, written by the platform** (`app/platform/audit`). `AuditLog.record` writes
one entry per change. Each entry holds:

- who acted, whose account it was, and what was changed;
- facts (`details`);
- the IP address and the request id.

Any detail whose key looks secret (password, token, code, key, OTP) is stored as
`[redacted]`, whoever passes it, at any depth (inside lists too). Entries expire after
`AUDIT_RETENTION_DAYS`, through a TTL index. Entries hold ids and facts, never a record's
content.

These actions are recorded:

- `user.role_changed`, `user.disabled`, `user.enabled`, `user.sessions_revoked` and
  `user.password_reset`;
- `allowlist.added` and `allowlist.removed`;
- `module.changed` (from/to);
- `entry.admin_corrected` (which fields, the revision and the reason, never the values);
- `two_factor.enabled`, `two_factor.disabled`, `two_factor.recovery_codes_renewed` and
  `two_factor.recovery_code_used`;
- `account.exported` and `account.deleted`.

The operator scripts write to the same trail with no actor and `"via": "operator"`:
`poe reset-password` (`user.password_reset`) and the seed's promotion of
`BOOTSTRAP_ADMIN_EMAIL` (`user.role_changed`).

### Admin routes

**Every admin route sits under `/v1/admin` and depends on `AdminDep`**, which requires the
admin role. A test goes through the OpenAPI inventory and asks every one of these routes as a
user (expecting 403) and anonymously (expecting 401).

- **Self-protection.** An admin never changes their own account there (`ADMIN_SELF_CHANGE`),
  so the one acting always stays an admin.
- **The last admin.** Two admins demoting or disabling each other at the same moment could
  leave none. A change to an admin is applied, then the active admins are counted again; if
  none is left, the change is undone and the answer is `ADMIN_LAST_ADMIN`.
- **Password resets.** A reset returns a one-time password once. It sets
  `must_change_password`, signs the account out everywhere, and turns two-step sign-in off,
  because a reset is how someone who lost their phone gets back in. `poe reset-password` does
  the same, for the operator.
- **Allowlist.** The allowlist is the env list (`AUTH_ALLOWED_EMAILS`, read-only in the
  console) plus a collection the console edits.
- **Modules.** Module changes are overrides of the manifest. Setting a field to `null` puts
  the manifest's value back. A change is audited whenever the stored override changes, even
  when what users see doesn't (pinning a field to the manifest's current value).
- **Corrections to someone's records.** A module may add admin routes under
  `/v1/admin/<module>`. Expenses has `/v1/admin/expenses/entries`: everyone's entries with the
  list's own filters, and a correction (`PATCH`, with the revision corrected and a reason). The
  correction runs the entry's own rules in its owner's data. Its revision names the admin as
  `changed_by`, with the source `admin`, and the audit entry keeps the fields and the reason.

### Usage analytics

Usage analytics are counts only:

- users, and sign-ups per day;
- active users: accounts with a session that started before the range ended and was used after
  it began, so someone who comes back today still counts for last week. It reaches back only as
  far as sessions are kept (`AUTH_REFRESH_ABSOLUTE_TTL_SECONDS` plus
  `AUTH_SESSION_PURGE_GRACE_DAYS`);
- assistant runs, by mode and provider;
- error codes per day;
- database size per collection, against `ADMIN_DB_QUOTA_MB`.

A range defaults to the last `ADMIN_USAGE_DEFAULT_DAYS` days and may span at most
`ADMIN_USAGE_MAX_DAYS`. A reversed range, a longer one, or one past the calendar's ends is
refused with `RANGE_INVALID`, as on the dashboards. The audit viewer's day filter refuses a
reversed range too.

The error handlers count codes through `app.state.tally`. It counts in memory and writes all the
counts in one batch at most every `ADMIN_ERROR_TALLY_FLUSH_SECONDS`, and once more at shutdown.
A flood of failing requests costs one write per interval, and counting never fails or slows the
response it counts.

### Two-step sign-in (TOTP)

**TOTP is implemented with the standard library (RFC 6238, SHA-1, 6 digits, 30 seconds)**, so
there's no `pyotp`. The RFC test vectors are tests.

- **Secrets.** The shared secret is encrypted at rest with Fernet from `cryptography`, which is
  already installed through `pyjwt[crypto]`. The key is `AUTH_FIELD_ENCRYPTION_KEY`. Without
  that key, 2FA can't be turned on, and the API answers `AUTH_TWO_FACTOR_UNAVAILABLE`.
  Recovery codes still work without the key, because they are kept only as SHA-256 hashes.
- **Setup.** Setup is two steps: `setup` keeps a pending secret, then `enable` checks a code
  from it before 2FA turns on. Enabling returns `AUTH_RECOVERY_CODES` codes, once. Both writes
  are conditional (only while 2FA is off, and only for the secret the code was checked
  against), so racing requests can't enable an unchecked secret or hand out codes that don't
  work.
- **Codes as typed.** Spaces are ignored (apps show `123 456`). Only ASCII digits make a TOTP
  code: a code typed in Devanagari or Tamil digits is a wrong code, not a server error.
- **Single use.** A code is accepted once only. The newest accepted step is stored
  (`totp_last_step`), and recovery codes are pulled from the list as they're used. Both
  updates are atomic, so two requests can't spend the same code.
- **Login flow.** With 2FA on, a correct password or Google sign-in returns a short-lived
  challenge instead of a session. The challenge is a JWT with `typ=two_factor`,
  `AUTH_TOTP_CHALLENGE_SECONDS`, and the account's token version. `/v1/auth/login/totp`
  exchanges the challenge plus a code for the session. A password change or reset in between
  ends the challenge.
- **Lockout.** Wrong codes count toward the same lockout as wrong passwords. The lock is part
  of the same conditional write that spends a code, so guesses sent all at once can't get
  past it.
- **Turning it on or off.** This needs the first factor again (see below) and a code.

### Proof it's really them, now

**Changes a stolen session must not make ask for the first factor again** (`Reauth`): turning
2FA on or off, deleting the account, and giving an account that has no password (Google only)
its first password. The proof is the password when the account has one. Otherwise it's a Google
sign-in for this account whose ID token was issued at most `AUTH_GOOGLE_REAUTH_MAX_AGE_SECONDS`
ago; anything else is `AUTH_GOOGLE_REAUTH_REQUIRED`. The first-password case matters: without
it, a stolen session could set a password and then use it to delete the account.

### Export and deletion

**A person's data is found generically: a document is someone's when it has a `user_id`.**
That covers every `OwnedDocument` and every revision. No module needs export or erase hooks,
and a new module is included automatically. Documents name the fields that are never handed
out in `PRIVATE_FIELDS`: password hash, token digests, the TOTP secret, recovery codes.

- **Export.** `GET /v1/me/export` streams one JSON file collection by collection, never all
  in memory. The file holds the account, every owned collection, and the person's audit
  entries. An entry about something someone else did (an admin) says what was done, without
  who did it or from where (`actor_id`, `ip` and `request_id` are left out).
- **Deletion.** `DELETE /v1/me` asks for proof it is really the person, right now:
  - the email typed out;
  - the password, or a fresh Google sign-in for an account without one;
  - a code if 2FA is on.

  The last active admin can't delete their own account. Deletion then runs in this order:
  1. every owned record is deleted, so a failure here leaves a working account to delete again;
  2. the account is marked deleted and signed out everywhere, so nothing new can be written;
  3. if it was an admin and no active admin is left (two admins leaving at once), the mark is
     undone and the answer is `ACCOUNT_LAST_ADMIN`;
  4. owned records are swept again, for what requests already in flight wrote;
  5. an invitation for its email on the allowlist is removed, and then the user.

  Their audit entries are kept until retention ends.

`docs/privacy-policy-template.md` describes these facts and maps them to the Play Store Data
Safety form.

## Consequences

- No new dependencies. TOTP uses `hmac`, `struct` and `base64`. Fernet comes from
  `cryptography`, which was already installed.
- Losing `AUTH_FIELD_ENCRYPTION_KEY` makes stored TOTP secrets unreadable. Users with 2FA on
  can still sign in with recovery codes, or an admin reset turns 2FA off.
- The audit trail outlives a deleted account for up to `AUDIT_RETENTION_DAYS`. It holds ids
  and IP addresses, not content. Allowlist entries are the exception: their audit records name
  the invited email address. The privacy policy template says both.
- A deletion that fails in step 1 leaves a working account to delete again. One that fails
  after step 2 leaves the user marked deleted and signed out, with most records already gone.
  That user can't sign in. There is no operator script for this yet: an operator finishes it
  by deleting the records that still have the user's `user_id`, then the user.
- Tests that move the clock forward must start it in the past. PyJWT checks `iat` against the
  real time, so a token dated in the future would be rejected.
