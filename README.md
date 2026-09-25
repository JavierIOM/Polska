# Polska

An autonomous company operator. An orchestrator decides what a business needs next,
then dispatches agents to do it on a schedule, without anyone driving each step.

Phases 1 through 3 of 5 are built: schema, models, migrations, the agent runner,
cost accounting, and the orchestrator loop and scheduler. It has now run for real
against a live key, against the CarScratch profile: see the dry run notes under
Tests, and the two real bugs that run found and this fixed. Every automated test
still runs against a fake SDK stream, by design.

## Build status

| Phase | Scope | State |
|-------|-------|-------|
| 1 | Schema, models, migrations | Done |
| 2 | Agent runner, dry-run adapter, cost accounting | Done |
| 3 | Orchestrator loop and scheduler | Done |
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
`sync_company` does the loading, at the start of every tick: descriptive fields
refresh from the YAML every time, but once a goal exists, its `current_value` and
`status` are database-owned, since they evolve from real tracked progress, not
from re-reading a static file.

**Goal** is a measurable target with a status, and a stable `key` matching the
profile's `GoalSpec.key`: that key is what a planner proposal's `goal_key` resolves
against, since a goal's title is free text the profile can edit at any time. The
target is a number and a unit, not an aspiration, because a goal the planner
cannot measure is one it will argue with itself about forever.

**Task** has a type (engineering, marketing, support, research), a goal it serves,
a state machine and a result payload.

```
queued ──► running ──► done
   │          │
   │          ├──► awaiting_approval ──► running ──► done
   │          │            └──► abandoned  (rejected)
   │          │
   │          └──► failed ──► queued  (bounded retry, on a wall-clock backoff)
   │                  └──► abandoned  (attempts or per-task budget exhausted)
   └──► abandoned
```

`done` and `abandoned` are terminal. Self-transitions are illegal on purpose: a
task moving from `running` to `running` is a double-dispatch bug, and silence
would hide it. Nothing assigns to `Task.state` directly; everything goes through
`transition_to()`, which raises `IllegalTransition` on an illegal move.

A `failed` task is only requeued once its backoff has elapsed
(`retry_delay_seconds`, doubling from `limits.retry_base_delay_seconds`, capped at
`retry_max_delay_seconds`, measured from `Task.updated_at`), and only if it has
exhausted neither `limits.max_attempts` nor `budget.max_usd_per_task` — the two are
checked independently, because on an expensive model a broken task can exhaust its
budget with attempts still nominally available.

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

**A tick** (`run_company_tick`) is one company's cycle: sync, plan, dedup, enqueue,
requeue, dispatch. Two things worth knowing before touching it:

- **Enqueue caps apply in a fixed order.** The planner's proposals are sorted by
  priority and capped at `limits.max_tasks_per_tick` *before* dedup runs (dedup
  scores what survives the cap, not the raw planner output), and only after that
  does `limits.max_tasks_per_day` gate actual creation, since several ticks can fall
  in one day.
- **Dispatch uses a different session per task, deliberately.** Planning is
  sequential and stays on one session for the whole phase; dispatch runs up to
  `limits.max_concurrent_tasks` tasks *concurrently*, and a synchronous SQLAlchemy
  `Session` is not safe to share across concurrent callers. Each dispatched task
  gets its own session, opened fresh and closed with that task's run. The shared
  `AgentRunner` (and the `BudgetGuard` inside it) is safe to reuse across those
  calls, since its own state is guarded by an `asyncio.Lock`.

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
- **A ceiling is a stop, denominated in dollars.** No soft mode, no degradation, no
  retry past a limit. Tokens are not fungible across models, so the ledger and every
  day/company ceiling are dollars, computed from the same pricing table that prices
  every run; `max_tokens_per_run` is the one token figure left, a same-model,
  single-run output-size safety net that is never used to price anything. Budget is
  reserved (`max_usd_per_run` in full) before a run starts and released when it ends,
  not just summed from `runs` after the fact: a reservation is what stops two
  concurrent dispatches from both reading the same stale total and together crossing
  a ceiling neither would have crossed alone.
- **A crash mid-run is not zero spend.** Every run is written to the database in
  `running` state *before* the SDK is ever called, and updated in place once it
  finishes. If the process dies in between, that row is left behind rather than
  silently missing, and `reconcile_orphaned_runs` — which **`main.py` runs once at
  process start, before the scheduler's first tick** — prices it at its
  reservation's worst case, not zero, and re-checks the ceilings against that worse
  number immediately. Nothing corrects that figure automatically, because nothing in
  this system can learn a crashed run's real usage on its own; `write_off_orphan` is
  the deliberate human override once the real figure is known some other way, always
  with a stated cost and an audit trail, and it never auto-clears a halt it caused.
- **Dedup is state-based, not a time window alone.** `FAILED`/`QUEUED`/`RUNNING`/
  `AWAITING_APPROVAL` suppress a fresh planner proposal unconditionally,
  `DONE` suppresses only inside `dedup.lookback_days`, and `ABANDONED` never
  suppresses at any age: it is the one state that means the system tried and gave
  up, and the underlying need is still open. The scoring itself is deterministic
  (`rapidfuzz` token-set ratio) with an LLM tiebreak batched through the dedup
  judge for whatever falls in the ambiguous band; a judge call that fails or gives
  no usable verdict defaults to kept, never dropped.
- **A worker never holds a repository credential.** `prepare_task_workspace` reads
  the token, hands it to exactly one `git clone` subprocess call as a transient
  environment variable, and it is gone: never written to a file, never in the
  clone's `.git/config` (verified directly), and `.git` is deleted from the
  checkout regardless as a second layer. The clone is then made read-only and
  stripped of dependency lockfiles and binary assets, the two things that measured
  contribution to a real run blowing well past its token reservation on its first
  live outing.
- **Every SDK call is isolated from whoever's host it runs on.**
  `setting_sources=[]` is set on every `ClaudeAgentOptions`. Left at its default,
  every call loads `~/.claude/settings.json`, any project settings found from
  `cwd`, and CLAUDE.md: someone's personal Claude Code configuration, entirely
  unrelated to running a company, and measured to cost real tokens doing it
  (28k+ cached tokens on one otherwise-trivial call before this was set).
- **Every agent names its model and its tool allowlist explicitly.** Nothing is
  inherited. An empty list means the agent reasons but touches nothing.
- **Reversible actions run immediately; irreversible ones never do.** They write a
  full preview into `approvals` and stop, unless config explicitly auto-approves that
  action type, in which case the row still exists and says so.
- **`failed` is not a place work goes to die.** A task is abandoned, never left
  sitting in `failed` forever, on either of two independent exhaustions:
  `limits.max_attempts` (retries) or `budget.max_usd_per_task` (spend, since an
  expensive model could burn most of a day's budget on one broken task before
  attempts alone would stop it). `failed` suppresses a fresh planner proposal for
  the same work unconditionally and `abandoned` is the one state dedup never
  suppresses at any age. Retrying a `failed` task that still has room on both is a
  scheduling decision for phase 3's orchestrator, on a stated wall-clock backoff
  (`limits.retry_base_delay_seconds`, doubling, capped), not something the runner
  does on its own.

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
  sync.py                Loads a company profile's goals into the database.
  dedup.py               Deterministic fuzzy match plus the judge tiebreak.
  orchestrator.py        One company's tick: plan, dedup, enqueue, requeue, dispatch.
  workspace.py            Clones a company's repo into a task's workspace, read-only.
  main.py                The process entrypoint: startup recovery, then the scheduler.
scripts/
  observe_ticks.py        Run N real ticks against a company and print what happened.
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

To actually run the orchestrator against a live key: put `ANTHROPIC_API_KEY` in
`.env`, add a company profile under `companies/`, then

```
.venv\Scripts\python.exe -m polska.main
```

This does not run migrations itself; run `alembic upgrade head` first, same as any
deployment. It recovers any orphaned run from a previous crash, then starts one
`AsyncIOScheduler` job on `scheduler.interval_hours` that reloads every company
profile from disk on each firing and ticks each active one in turn.

## Tests

238 tests, no network, about twelve seconds.

### Live dry run

Run for real, against CarScratch, twice: `.venv\Scripts\python.exe scripts\observe_ticks.py carscratch 3`.
The first run found two real bugs (the settings-isolation leak and a run
overshooting its own reservation by 5-10x on a repo clone containing a large
lockfile); both are fixed and covered by tests. A second run with the fixes in
place still overshot, by less (roughly half), and both attempts in it ended on
the account running out of credit rather than on a controlled ceiling, so
whether 200k is simply too tight for an Opus-tier engineering task against a
real repo is still an open question, not yet answered on clean data. Full
account of both runs, real dollar figures and what was proposed and why: the
`polska.md` wiki page.

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

`test_budget_guard.py` covers what review specifically asked to see proven, not just
asserted: a real concurrency test (several reservations fired at once with
`asyncio.gather`, asserting exactly as many are granted as fit under the dollar
ceiling and the rest are refused); orphan recovery (`reconcile_orphaned_runs` finding
a row left in `running` from a simulated crash, pricing it at the reservation's worst
case rather than zero, and writing a halt when that worst case alone crosses a
ceiling); that a run overshooting either its dollar or its token safety net trips its
own, independent halt; and the write-off path (correcting an orphan's cost, refusing
to write off anything that isn't one, and a halt surviving a write-off that clears
the numbers it was raised over).

`test_runner.py` builds fake SDK message streams from the real `claude_agent_sdk`
dataclasses and checks: success; a schema-invalid result; the CLI's own error result;
a `ResultError` exception; a wall-clock timeout; a budget-blocked run that never
calls the SDK at all; a `CLIConnectionError` propagating past a Run row rather than
being swallowed as an ordinary task failure; that the Run row exists in `running`
state *before* the SDK is invoked, confirmed by a fake that queries the database
mid-call; that a `CLIConnectionError` updates that same row rather than leaving a
second one behind; that a task exhausting its retries becomes `abandoned`, not left
in `failed` forever; and that a task can be abandoned on cost alone, with attempts
still nominally available.

`test_sync.py` covers the profile-into-database sync: creation, that descriptive
fields refresh from an edited YAML while `current_value` and a manually-set `status`
survive it, and that a second goal added to an existing profile is created alongside
the first rather than replacing it.

`test_dedup.py` proves the state rules against the *matching* code, not just the
state sets themselves: an identical `QUEUED` task suppresses, a `FAILED` one
suppresses however old, a `DONE` one suppresses only inside the lookback window and
stops once outside it, and an `ABANDONED` one never suppresses however identical
and recent. Separately covers the judge path: marking an ambiguous proposal a
duplicate or novel, and defaulting to kept when the judge is disabled or returns
nothing usable.

`test_orchestrator.py` covers the backoff formula in isolation, then a full tick end
to end against a scripted fake runner: an empty plan enqueues nothing, a proposal
resolves against the right goal, an inactive company is skipped entirely (and that
skip is itself logged, which is what caught the commit bug below), dispatch respects
`max_concurrent_tasks` and leaves the rest `queued`, a `FAILED` task past its backoff
is requeued and redispatched, and a fatal `CLIConnectionError` from one dispatch
propagates only once every other dispatch that tick has been accounted for.
`test_main.py` covers the same error isolation one level up: a malformed profile, or
one company's tick raising, does not stop the others from running.

`test_migrations.py` builds a database by running every migration and compares tables,
columns, indexes and foreign keys against the models. The rest of the suite uses
`create_all` for speed, which is only safe while that test passes.

**None of this has been run against a live Anthropic key**, and five attempts across
three sessions were blocked, not completed: a one-off script mirroring the runner's
real request (a Pydantic schema with nested models, `$defs`/`$ref`, sent as
`output_format` to `claude-haiku-4-5`) got as far as a real `ResultMessage` coming
back — confirming the `ResultError`-after-a-yielded-result code path fires exactly as
`runner.py` expects — before failing on "Credit balance is too low" every time. The
schema mechanics are unverified, not broken; check this again once the account has
credit, before leaning on it further. The script is
`smoke_test_json_schema.py` in the scratchpad, not part of the repo.

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
