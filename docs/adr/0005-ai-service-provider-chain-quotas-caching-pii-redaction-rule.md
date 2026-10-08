# ADR-0005: AI service: provider chain, quotas, caching, PII redaction, rule fallback

- **Status:** Accepted
- **Date:** 2026-10-08
- **Deciders:** project owner (plan approved 2026-10-08)

## Context

Free LLM tiers churn (Groq removed Llama from its free tier on 2026-08-16) and have tight daily caps. Gemini's free tier uses prompts to improve Google products. Capture must keep working with no AI at all.

## Decision

All AI calls go through `platform.ai.AIService`, which does the following. It runs an ordered provider chain from config (`gemini,groq`) with cooldown on 429. It validates outputs against JSON schemas. It redacts PII before calls and caches responses in MongoDB (TTL). It enforces a per-user daily quota plus a global daily cap, and logs usage. AI runs only after explicit user consent. Each module supplies prompts, schemas and a deterministic rule-based fallback.

## Consequences

Swapping providers or models is configuration only. Quota exhaustion degrades to rules rather than errors. Consent and redaction support the Play Store Data Safety form.

## Alternatives considered

A single provider: fragile. Calling providers directly from modules: duplicated safety and quota logic.
