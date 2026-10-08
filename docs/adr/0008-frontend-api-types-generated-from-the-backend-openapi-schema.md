# ADR-0008: Frontend API types generated from the backend OpenAPI schema

- **Status:** Accepted
- **Date:** 2026-10-08
- **Deciders:** project owner (plan approved 2026-10-08)

## Context

Two repositories share request and response shapes. Hand-written frontend types drift.

## Decision

The backend exports its schema (`poe openapi`). The frontend generates `src/core/api/schema.d.ts` with `openapi-typescript` and calls the API through `openapi-fetch`. The generated file is committed and regenerated when the API changes.

## Consequences

Type errors surface at compile time when the API changes. The error envelope is part of the schema (FastAPI's default validation schema is replaced), so error handling is typed too.

## Alternatives considered

Hand-written types: drift. A shared package: publishing overhead across two repos.
