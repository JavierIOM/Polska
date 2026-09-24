# Polska

An autonomous company operator. An orchestrator decides what a business needs next,
then dispatches agents to do it on a schedule, without anyone driving each step.

Phases 1 and 2 of 5 are built: schema, models, migrations, the agent runner, the
dry-run adapter and cost accounting. Nothing has been run against a live Anthropic
API key yet: every test runs against a fake SDK stream, by design (see Tests below).

## Build status

| Phase | Scope | State |
|-------|-------|-------|
| 1 | Schema, models, migrations | Done |
| 2 | Agent runner, dry-run adapter, cost accounting | Done |
| 3 | Orchestrator loop and scheduler | Not started |
| 4 | Approval gate | Partial: classification, dispatch and execution are done; the dashboard's approve/reject endpoints are not |
| 5 | Dashboard | Not started |

## The idea

Every four hours the orchestrator loads the company's state, asks a planning agent
what should happen next, drops near-duplicate proposals, and queues what survives.
Worker agents pick tasks up. Anything that would touch the outside world stops and
waits for a human.

## Core concepts

**Company** is loaded from a YAML profile in [companies/](companies/): the idea,
the brand voice, the constraints and the connected integrations. The YAML is the
source of truth; the database row is a snapshot with a hash so an edit on disk is
noticed. The schema is multi-company from the start even though one profile ships.

**Goal** is a measurable target with a status. The target is a number and a unit,
not an aspiration, because a goal the planner cannot measure is one it will argue
with itself about forever.

**Task** has a type (engineering, marketing, support, research), a goal it serves,
a state machine and a result payload.

```
queued ──► running ──► done
   │          │
   │          ├──► awaiting_approval ──► running ──► done
   │          │            └──► abandoned  (rejected)
   │          │
   │          └──► failed ──► queued  (bounded retry)
   │                  └──► abandoned
   └──► abandoned
```

`done` and `abandoned` are terminal. Self-transitions are illegal on purpose: a
task moving from `running` to `running` is a double-dispatch bug, and silence
would hide it. Nothing assigns to `Task.state` directly; everything goes through
`transition_to()`, which raises `IllegalTransition` on an illegal move.

**Run** is one agent invocation against one task: the prompt, the model, the tokens,
the cost, the duration, the tools called and the raw output. Cost is stored in USD
and GBP at the moment of the run, with the FX rate used, so a later rate change
cannot rewrite history. `task_id` is nullable because the planner and the dedup
judge run against a company rather than a task, and their tokens count the same.

**Approval** is a proposed external side effect that has stopped. `payload` is the
complete replayable adapter call; approving hands that stored payload to the adapter
exactly as it is, and the agent is never consulted again.

**BudgetHalt** records a ceiling that was crossed. Running totals are not cached:
they are summed from `runs`, which is the only place tokens are recorded, so the two
cannot disagree. An uncleared halt blocks the scheduler, and clearing one is a
deliberate human act.

**ActivityEvent** is the chronological feed, denormalised so the dashboard reads it
in one indexed query. It is also where "log before you act" lands: an external effect
writes `action_proposed` before the adapter is called and `action_executed` after, so
a crash mid-call still leaves evidence.

## Design rules

These are not negotiable and the tests enforce several of them.

- **Agent output is validated, never parsed.** Everything that drives control flow
  goes through a Pydantic model in [src/polska/schemas/](src/polska/schemas/). An
  output that does not validate makes the run `invalid_output`.
- **The gate fails closed.** An action type nobody classified is treated as
  irreversible. `auto_approve` ships empty, and an entry in it that is not also in
  `irreversible_actions` is rejected at config load as a mistake.
- **No secrets in tracked files.** Company profiles name the environment variable an
  adapter should read; they cannot contain the value. Approval payloads are stored in
  SQLite and rendered on the dashboard, so a credential-looking key in one is refused
  by the schema.
- **A ceiling is a stop.** No soft mode, no degradation, no retry past a limit. Budget
  is reserved before a run starts and released when it ends, not just summed from
  `runs` after the fact: a reservation is what stops two concurrent dispatches from
  both reading the same stale total and together crossing a ceiling neither would have
  crossed alone.
- **Every agent names its model and its tool allowlist explicitly.** Nothing is
  inherited. An empty list means the agent reasons but touches nothing.
- **Reversible actions run immediately; irreversible ones never do.** They write a
  full preview into `approvals` and stop, unless config explicitly auto-approves that
  action type, in which case the row still exists and says so.

## Stack

Python 3.12, SQLite in WAL mode, SQLAlchemy 2.0 with Alembic, Pydantic v2, the Claude
Agent SDK. Phase 3 onwards adds APScheduler and FastAPI with plain HTML.

Note on the Agent SDK: it shells out to the Claude Code CLI, so the Docker image will
need Node 20+ alongside Python. That is a fatter image than a plain Messages API loop
would need, and it buys the tool harness, the permission modes and per-run usage
reporting without writing them. The runner reads its exact API from the installed
package rather than from training-time recall: several fields on `ClaudeAgentOptions`
(`max_budget_usd`, `output_format`, `sandbox`, the `permission_mode` literals) did not
exist as documented here, and the runner's cost accounting depends on getting the
`ResultMessage.model_usage` key casing right, which was confirmed by reading the SDK's
own source rather than guessed.

Every worker agent runs with `permission_mode="bypassPermissions"`: this is an
unattended scheduler with nobody present to answer an interactive tool-use prompt, so
the per-agent tool allowlist is the security boundary, not a runtime confirmation.

Concurrency is a single process: `AsyncIOScheduler` with an `asyncio.Semaphore` for
the max-concurrent ceiling. Separate worker processes are the wrong call at this size.

## Layout

```
config/default.yaml      Tunables: intervals, ceilings, agent models, tool allowlists,
                         the irreversible-action list, model pricing. No secrets.
companies/               One YAML profile per company.
migrations/              Alembic. The DSN comes from settings, not alembic.ini.
src/polska/
  config/                Env settings, config schema, company profile loader.
  db/                    Engine, models, enums, the task state machine.
  schemas/               Pydantic contracts for every agent output.
  adapters/              The adapter interface, the dry-run implementation, registry.
  activity.py            The one place that writes to the activity feed.
  budget.py              The budget guard: reserve before a run, release after.
  gate.py                Classifies and dispatches proposed actions.
  prompts.py             Per-agent system prompt templates.
  runner.py              One agent invocation against the SDK, fully accounted for.
tests/
```

## Running it

Windows, from the repo root:

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m alembic upgrade head
.venv\Scripts\python.exe -m pytest
```

Ubuntu:

```
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/alembic upgrade head
.venv/bin/pytest
```

Copy `.env.example` to `.env` before running against a live key. Nothing in the test
suite needs one: every SDK call in it goes through a fake `query_fn`, never the network.

## Tests

174 tests, no network, about six seconds.

The state machine is tested exhaustively rather than by example: all 36 ordered pairs
of states are asserted legal or illegal against a table written independently of the
implementation, and a graph walk proves no state can trap a task forever. The dedup
state sets (`DEDUP_SUPPRESSING_STATES`, `DEDUP_LOOKBACK_STATES`,
`DEDUP_NEVER_SUPPRESSES`) have a test proving they partition every `TaskState` with no
gaps and no overlap.

The approval gate is split across two files: `test_approval_gate.py` covers
classification, fail-closed behaviour, the credential-in-payload refusal and the
record's own lifecycle; `test_gate_dispatch.py` covers the phase 2 half, actually
calling an adapter, the `force_dry_run` interception, and auto-approve.
`test_budget_guard.py` includes a real concurrency test: several reservations fired
at once with `asyncio.gather`, asserting exactly as many are granted as fit under the
ceiling and the rest are refused, not just that the arithmetic is right in isolation.
`test_runner.py` builds fake SDK message streams from the real `claude_agent_sdk`
dataclasses and checks the runner's handling of success, a schema-invalid result, the
CLI's own error result, a `ResultError` exception, a wall-clock timeout, a
budget-blocked run that never calls the SDK at all, and a `CLIConnectionError`
propagating past a Run row rather than being swallowed as an ordinary task failure.

`test_migrations.py` builds a database by running every migration and compares tables,
columns, indexes and foreign keys against the models. The rest of the suite uses
`create_all` for speed, which is only safe while that test passes.

None of this has been run against a live Anthropic key. The fake `query_fn` is built
from the SDK's real dataclasses and matches its documented behaviour as read from the
installed package, but a schema built with nested Pydantic models (`$defs`/`$ref`) has
not been confirmed to round-trip through the CLI's `--json-schema` flag against a real
model. That is the first thing to check with a key in hand, before trusting this in
production.

## Conventions

- Timestamps are timezone-aware UTC everywhere. A naive datetime reaching the database
  raises rather than being guessed at.
- SQLite runs with `foreign_keys=ON` and WAL. Neither is a default and both are needed.
- Enums are stored as VARCHAR with a CHECK constraint, so they stay readable in a
  `sqlite3` shell.
- Files are read with an explicit UTF-8 encoding: Python 3.12 on Windows defaults to
  cp1252 and would mangle a pound sign in a company brief.

## Licence

Private project. All rights reserved.
