# ADR-0026: Strands runs the agent loop; background runs with stages

- **Status:** Accepted
- **Date:** 2026-11-10
- **Deciders:** project owner (Phase 19, Basic AI: "Add Strands" for Basic and Premium AI, after
  the memory gate)
- **Supersedes in part:** ADR-0013 (the runtime's own model loop). Its contracts still apply:
  skills, tools and agents as content, the mode and fallback switches, proposals, consent,
  quotas, redaction and the provider chain.

## Context

ADR-0013 built its own loop of about 60 lines and rejected agent frameworks for their weight and
hidden prompts. For Travel's Basic and Premium AI the owner chose Strands Agents, on one
condition: it must fit the 512 MB server. The plan (`modules-usecases/Travel_Companion_Plan.md`,
§8) also asks for one AI system, not a second one next to the first, so Strands becomes the
engine for every skill. Travel's AI pages need real progress while a run works, and the app has
no streaming.

## Decision

### The gate: measured before anything was built

The real API (`app.main:create_app` under uvicorn, Windows 11, local MongoDB, no AI provider)
ran with Strands 1.64 against a local fake OpenAI-compatible server (`.probe-strands/RESULTS.md`):

| API process | RSS | USS |
|---|---|---|
| Today's app, started | 115 MB | 99 MB |
| With Strands imported | 152 MB | 136 MB |
| After 1 agent run | 167 MB | 150 MB |
| After 30 runs, one at a time | 169 MB | 152 MB |
| Peak during 20 runs at once | 172 MB | 155 MB |

Strands costs about 55 MB, and the peak is a third of Render's 512 MB; memory did not grow over
30 runs. Importing it adds about 2.4 s. Not measured on Linux (Render's metrics can confirm).

### One engine for every skill

`AgentRuntime._agent` builds a Strands `Agent` for each run (`platform/agents/engine.py`), with:

- `callback_handler=None`: nothing printed;
- `retry_strategy=None`: the provider chain already fails over, and Strands' default retries
  wait up to 4 minutes;
- `SequentialToolExecutor`: one tool at a time, in the model's order, as before;
- `NullConversationManager`: a run is short, and nothing may be dropped or summarised;
- `limits={"turns": AGENT_MAX_STEPS}`: Strands' `limit_turns` is the run's `incomplete`.

Everything around the loop is as ADR-0013 built it: `policy.plan`, static mode, the run record,
the routes, the module API (`run_skill`), and the tests' fake provider. Every existing agent test
passes without weakening. Two that took the only agent from `GET /v1/agents`
(`tests/modules/labor/test_marks.py`, `tests/platform/agents/test_photos.py`) now pick it by name,
as that list now holds Travel's agent too, and the pinned tool and skill sets grew by Travel's.

### The model: an adapter over the provider chain

`ChainModel` is a Strands `Model`. For each model call it takes one call from the quota, then
asks `ProviderChain.complete`, and hands the whole answer to Strands as its stream events. It
converts both ways:

- system blocks become one system message each (the agent's instructions, then a preloaded
  skill's);
- the user's turn is the masked words, or with photos the words (if any) and then one
  `image_url` part per photo, the same data URLs as before;
- the model's turns carry their text and `tool_calls`; each tool result is a `tool` message with
  the same JSON text as before.

The tools sent to the provider are the runtime's own list (the loaded skills' tools, then
`load_skill`), not Strands' list of every registered tool. When the quota or the providers stop
a call, the adapter ends the turn with an empty answer and the runtime stops the run as it did
before: re-planned when nothing was produced yet (`_Blocked`), else `incomplete` with the
reason. Raising through Strands instead would be logged as a failure and wrapped.

**Why this adapter and not Strands' `OpenAIModel` or `ModelRouter`.** Those need the `openai`
extra (the `openai` SDK on `httpx2`) and would replace the provider chain: the configured order,
the rest after a 429, photos only to providers that read images, the room a skill asks for
(`max-output-tokens`), and the shared HTTP client with the office's certificates. The tests' fake
provider patches `httpx`, which `httpx2` is not. Strands has no Groq provider, and the use case
allows "a clean adapter" where Strands has no direct support.

### Tools, skills and the gate

- Each `ToolSpec` of the agent's skills is a Strands tool calling `ToolBox.call`, so a read
  answers at once, a client tool becomes a client action and a write becomes a proposal.
- `load_skill` stays the runtime's own tool (progressive disclosure, as before). Strands'
  `AgentSkills` plugin is not used: it neither enforces `allowed-tools` nor runs static scripts.
- A `BeforeToolCallEvent` hook refuses a call whose arguments weren't JSON
  (`arguments_not_json`) and a tool of a skill not loaded, unknown tools included
  (`tool_not_loaded`). The refusal is the call's result, and every call is a step, as before.
- Known difference: a tool name Strands deems invalid (a character outside `A-Z a-z 0-9 _ -`,
  or longer than 64) is answered by Strands itself and not recorded as a step.
- **A tool that fails** for an unexpected reason (anything but the mistakes `ToolBox.call` answers
  as data: invalid arguments, an `AppError`) is caught in `engine.py`, never by Strands, which
  would log it, send its words to the provider and carry on. The model is answered only
  `{"error": "tool_failed"}` (a step), the next model call ends the loop at once (no quota, no
  provider), and the fault is raised once Strands returns: a run that answers when done fails
  (500), and a background run ends as `incomplete`, as before Strands. Only `Exception` is caught,
  so a cancel or the run's timeout still goes through.
- **Money** goes to and from the model in the currency's main unit, as people say it: read tools
  convert every `_minor` figure (`in_major`, the platform's), and a draft's amounts are converted
  (`to_minor`) before the same checks as the person's own input. The model never has to know a
  currency's decimals (none for JPY, three for KWD).

### Imported with the first AI run

Only `engine.py` imports Strands, and the runtime imports `engine.py` inside `_agent`, in a
worker thread (`asyncio.to_thread(importlib.import_module, ...)`): on the event loop, the 2.4 s
would stall every request in flight, other people's too. Startup stays as it was. A test checks
that creating the app imports neither Strands nor boto3.

### OpenTelemetry stays unconfigured

The app sets no tracer or meter provider, so Strands' spans and metrics go to the API's no-op
proxies: nothing is recorded or exported. Strands' tracer does instrument `threading` on first
use, which only carries context between threads. A test checks that no SDK provider is set after
an AI run.

**Rule:** Strands puts prompts, tool arguments and tool results on its spans, unredacted, by
default. Before anyone configures OpenTelemetry (an exporter, `OTEL_*` settings, or Strands'
own `StrandsTelemetry`), that content must be redacted, in a superseding ADR. Strands' logger is
kept at WARNING for the same reason: its debug lines carry tool arguments, and agent runs log
ids, tool names and counts only (ADR-0013).

### Background runs and their stages

- `start_skill(ctx, skill, text, *, locale, context)` (from `app.platform`) saves the run as
  `running` and answers at once; a module route answers 202 with its id. The run goes on in a
  task the runtime holds until it ends. `GET /v1/agents/runs/{id}` gives its status and `stages`.
- Stages are codes the runtime sets, never the model's words: `understanding` (the run started),
  `reading` (a read tool), `drafting` (a client tool), `proposing` (a write tool), `checking`
  (the model again, with what the tools answered) and `done`. A background run's record is
  updated at each new stage; a run that answers when done (`run`, `run_skill`) stores them with
  its outcome. Static mode answers at once: `understanding`, then `done`.
- `AGENT_RUN_TIMEOUT_SECONDS` (default 90, at most 120, as pages stop following a run at 150 s):
  a background run still working then ends as `incomplete` with reason `timeout`, keeping what
  it made. At shutdown the runtime stops its runs; a record still `running` past the timeout (a
  restart cut it short) reads as `incomplete` with reason `timeout`. An unexpected fault ends
  the run as `incomplete` (`agent_run_failed` in the log, with the fault's type only) rather than
  leaving it running.
- **Cancel**: `POST /v1/agents/runs/{id}/cancel` stops the caller's own running run. The runtime
  keeps each background run's task by its id and cancels it: no further model call or quota, and
  the run is saved as `incomplete` with what it made so far (`agent_run_cancelled` in the log;
  its reason stays null, so the pages need no new word). A run a restart left `running` is ended
  the same way. Once a run has ended, nothing more is written over its record. Another person's
  run answers 404; one finished otherwise 409 `AGENT_RUN_FINISHED`; one cancelled already answers
  as it is.
- New values in the API: run status `running`, reason `timeout`; `GET /v1/agents` also lists the
  `travel` agent, so a client chooses an agent by name; the cancel route.

### Skills kept for one agent

A skill's `SKILL.md` may name one agent (`metadata: agent: <name>`). Only that agent runs it,
and it must list it; agents with no skills list leave it out. So the general assistant (the
palette) never runs a page's own skill, such as a travel skill without its trip. Startup fails
when another agent lists such a skill, or its own agent doesn't.

## Consequences

- One AI system: expenses, labor, receipts, notifications, modules and Travel's Basic AI run on
  Strands.
- The runtime lock grows by 27 packages (Strands with boto3 and botocore, the OpenTelemetry SDK,
  `mcp` with its server packages, `jsonschema`, `watchdog` ...); no existing pin moved and
  `pip-audit` is clean. `pywin32`, which `mcp` needs on Windows only, is named in `pyproject.toml`
  with its marker, so the lock compiled on Windows still installs on Linux (CI, the image).
- Strands releases about weekly and its loop changes between them: it is pinned exactly, and an
  upgrade is its own change, checked against every agent test.
- Each model call, Strands serialises the conversation for its spans even though they record
  nothing; with a photo that is some work, never storage.

## Alternatives considered

- Strands' `OpenAIModel` or `ModelRouter`: see above.
- Strands for Travel only: two loops with their own quotas, consent and switches. The plan asks
  for one system.
- Streaming progress (SSE): the app has no streaming, and polling the run fits the server and
  the frontend's first-load budget.
- Strands' structured output: skills answer through tools (drafts are client tools), which the
  gate and the proposal rules already cover.
