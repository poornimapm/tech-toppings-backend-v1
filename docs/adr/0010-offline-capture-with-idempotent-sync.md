# ADR-0010: Offline capture with idempotent sync

- **Status:** Accepted
- **Date:** 2026-10-08
- **Deciders:** project owner (plan approved 2026-10-08)

## Context

Voice capture needs a network (browser speech services), but users still want to log entries offline.

## Decision

Typed entries are queued in IndexedDB, each with an `Idempotency-Key`, and replayed on reconnect (Background Sync where supported, otherwise the `online` event or app start). Offline free text is parsed server-side when it syncs and lands in a To-confirm inbox.

## Consequences

There is one parser implementation (Python). Retries never create duplicates. Offline voice is not supported.

## Alternatives considered

A duplicate TypeScript parser: two implementations to keep in sync.
