# ADR-0013: Skill-driven agents with mode and fallback switches

- **Status:** Accepted
- **Date:** 2026-10-09
- **Deciders:** project owner (decisions given at the start of Phase 4)
- **Supersedes in part:** ADR-0005 (the per-module rule fallback and hand-written prompts)

## Context

The owner asked for an agentic architecture built to current standards. Agents must be
driven entirely by skills, with nothing hardcoded and no static fallback data unless they
choose it. Free AI quotas run out, so the app must keep working when that happens. Which
behaviour applies (AI, deterministic, or an honest "unavailable") has to be a switch, not a
code change.

## Decision

**Definitions are content, not code.** Any platform sub-package or module may contain:

- `agents/<name>.md`: an agent. Frontmatter holds `name`, `description` and an optional
  `skills` list. The body holds the instructions, with runtime placeholders `{{name}}`,
  `{{today}}`, `{{locale}}`, `{{skills}}` and `{{context}}`.
- `skills/<name>/SKILL.md`: a skill in the open Agent Skills format. Frontmatter holds
  `name`, `description` and `allowed-tools`; the body holds instructions. Two optional parts:
  - `scripts/static.py` is the skill's deterministic implementation ("static mode");
  - `references/` holds its data files (for example a multilingual lexicon).
- `tools.py`: typed async functions decorated with `@tool("read" | "write" | "client")`.
  The JSON Schema comes from type hints and the description from the docstring. The
  definitions are OpenAI-compatible and also valid MCP input schemas.

Discovery uses the same folder scan as modules (`platform/plugins.py`). A malformed agent,
skill or tool stops startup with its path.

**Running.** `platform/agents/runtime.py` contains no prompt text. In agent mode the model
sees:
- the agent's instructions;
- each skill's one-line description, then the full instructions and tools once it calls
  `load_skill` (progressive disclosure, as Agent Skills intends).

In static mode, each skill's `static.py` gets a turn and uses the same tools. Static replies
are message codes that the client translates.

**Switches** (`AGENT_` settings):

| Setting | Effect |
|---|---|
| `AGENT_MODE=agent\|static` | Global mode. `static` never calls a provider. |
| `AGENT_SKILL_MODES=skill=static,...` | Per-skill override. |
| `AGENT_FALLBACK=true\|false` | When AI can't run (no consent, quota used up, no provider, all providers failing), `true` runs the static script and `false` answers honestly that AI is unavailable, with the reason. |

`policy.plan()` is the single pure function that applies these switches.

**Safety.**
- Tools run with the caller's `Scope`, so every rule of the API applies.
- `write` tools never run during a run. They become proposals the user confirms, and
  confirmation is a single atomic claim, so a double click can't run one twice. Proposals
  expire after `AGENT_PROPOSAL_TTL_MINUTES`.
- User text is redacted (`AGENT_REDACT_PATTERNS`) before it reaches a provider.
- AI runs only after the user's consent (ADR-0005), within per-user and global daily call
  budgets.
- Runs are kept for `AGENT_RUN_RETENTION_DAYS` (TTL), and logs carry only ids, tool names
  and counts, never the request text.

**Providers.** One adapter speaks OpenAI-compatible chat completions with tool calling. It
covers Gemini's OpenAI endpoint, Groq, OpenRouter free models and a local Ollama, and they
are tried in configured order. A 429 rests a provider for `AGENT_PROVIDER_COOLDOWN_SECONDS`.
Free tier only.

## Consequences

- Adding an AI capability means writing a skill folder and, if it needs data access, a few
  small tools. Changing behaviour means editing markdown or a lexicon file.
- The same tools serve the agent, the static script and (later) an MCP endpoint.
- Small free models occasionally misuse tools. Errors go back to the model as data, a step
  limit (`AGENT_MAX_STEPS`) bounds every run, and nothing changes without the user's
  confirmation.
- Message codes from module skills need translations; module frontend manifests will carry
  them when the first module skill ships.

## Alternatives considered

- An agent framework (LangChain, LlamaIndex, CrewAI): heavy dependencies and hidden prompts,
  for a loop of about 60 lines.
- Prompts in Python: hard to review and against the owner's "no hardcode" rule.
- Skills stored in the database: editable without a deploy, but unreviewed and they need an
  editor UI. Folders were chosen (decision 2026-10-09).

## Implementation notes (Phase 5, 2026-10-13)

- A module's skills and tools serve a user only while the module is available to them: its
  skills are left out of the run, and confirming an earlier proposal fails with
  `MODULE_DISABLED`.
- In static mode a module's skills get their turn before the platform's, so "show my
  expenses" opens Expenses rather than being read as "show the module".

## Implementation notes (Phase 6, 2026-10-14)

- Capture is a skill like any other. Its client tool `expenses_draft` hands the page draft
  entries; the user saves them, so the assistant needs no write tool for capture.
- A page that asks on purpose says how the input was given (`context.input_type`). The
  expenses static script then reads anything as an entry; from the general assistant it
  takes only text that speaks of money and has an amount.
- The rule parser is the skill's static mode, so the mode and fallback switches decide
  between AI and rules for capture exactly as for every other skill.
