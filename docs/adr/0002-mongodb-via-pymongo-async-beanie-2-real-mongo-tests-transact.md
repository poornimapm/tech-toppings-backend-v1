# ADR-0002: MongoDB via PyMongo Async + Beanie 2; real-Mongo tests; transaction-free revisions

- **Status:** Accepted
- **Date:** 2026-10-08
- **Deciders:** project owner (plan approved 2026-10-08)

## Context

The brief asks for MongoDB with Motor and Beanie. Motor has been deprecated since 2026-05-14 (critical fixes only until 2027-05-14), and Beanie 2.x is built on PyMongo's native async driver. The local mongod (8.0.26) is a standalone server, so it has no multi-document transactions; Atlas M0 does have them. Analytics need `$dateTrunc` and `$setWindowFields`, which mongomock does not implement.

## Decision

Use `pymongo.AsyncMongoClient` with Beanie 2 (approved deviation D11). Tests run against a real MongoDB: the local server, or a `mongo:8.0` service container in CI, using a throwaway database per run. Expense revisions are written without transactions using an outbox step inside the document: a conditional update sets `pending_revision`, an idempotent insert writes the revision, and a final update clears the field. Reads and a repair job complete any pending revision.

## Consequences

The driver is officially supported. Tests exercise real query semantics. Writes behave the same on standalone and replica-set servers. The test suite requires a reachable MongoDB.

## Alternatives considered

Motor + Beanie 1.x: deprecated line. mongomock: missing aggregation operators. testcontainers: needs Docker running for every test run.
