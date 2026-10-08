# ADR-0001: Modular monolith with manifest auto-discovery

- **Status:** Accepted
- **Date:** 2026-10-08
- **Deciders:** project owner (plan approved 2026-10-08)

## Context

The platform will host many independent modules: expenses first, and later medicine, receipts, study, gov-docs, meals, habits and journal. It runs on a single free Render instance and must stay cheap to change. Each module should be addable without touching core code.

## Decision

One FastAPI service and one React SPA, both organised as layers: `core` -> `platform` -> `modules/<key>`. Each module declares a manifest (key, status, routes, documents, widgets, jobs, settings schema, privacy hooks). The app discovers manifests from configured packages at startup (backend) and at build time via `import.meta.glob` (frontend). Modules never import each other. `import-linter` (backend) and ESLint restricted imports (frontend) enforce the boundaries in CI.

## Consequences

Adding a module means adding a folder. The boundaries are machine-checked, not left to review. A single deployable fits the free tier. If modules ever need independent scaling, they can be extracted into services later.

## Alternatives considered

Microservices: too costly on free tiers and operationally heavy. Module federation on the frontend: build complexity and version skew (the reference project showed its pitfalls).
