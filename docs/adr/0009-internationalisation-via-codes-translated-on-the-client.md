# ADR-0009: Internationalisation via codes, translated on the client

- **Status:** Accepted
- **Date:** 2026-10-08
- **Deciders:** project owner (plan approved 2026-10-08)

## Context

The UI must support English, Tamil and Hindi, and users may switch language at any time.

## Decision

The server returns stable error codes and notification types with parameters, never pre-translated text. The client translates with i18next (namespaces: `common`, `errors`, one per module), and formats numbers, money and dates with `Intl` (`en-IN` grouping for INR). LLM-generated text (insights) is produced in the user's locale and cached per locale.

## Consequences

Switching language needs no refetch. Messages stay consistent. Translators work only on frontend JSON files.

## Alternatives considered

Server-side translation: re-fetch on every switch and duplicated catalogs.
