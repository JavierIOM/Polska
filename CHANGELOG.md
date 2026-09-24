# Changelog

All notable changes to this project are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-09-24

Phase 3 of 5: orchestrator loop and scheduler.

### Added

- `Goal.key`: the stable identifier a company profile's `GoalSpec.key` needs to
  resolve against, unique per company. Missing from the phase 1 schema; a planner
  proposal's `goal_key` had nothing to match against without it. Migration
  `3ef3f8649043`.
- `polska/sync.py`: `sync_company` creates or updates a `Company` and its `Goal`
  rows from a loaded profile. Descriptive fields (title, description, metric,
  target, unit, priority) refresh from the YAML on every sync; `current_value` and
  `status` are database-owned once a goal exists, so a static file re-read can
  never silently undo tracked progress or a deliberate pause. Called at the start
  of every tick, which is cheap and means an edited profile takes effect without a
  restart.
- `polska/dedup.py`: the deterministic-fuzzy-match-plus-judge design from review,
  wired up. A normalised composite key scored with `rapidfuzz`'s token-set ratio
  against every comparison candidate; at or above `dedup.high_threshold` a proposal
  is dropped as a duplicate, at or below `dedup.low_threshold` it is kept as novel,
  and only the band between costs one batched call to the dedup judge agent for
  the whole tick. The comparison set is state-based per the 0.1.1 fix:
  `FAILED`/`QUEUED`/`RUNNING`/`AWAITING_APPROVAL` suppress unconditionally, `DONE`
  only inside the lookback window, `ABANDONED` never. A judge call that fails or
  returns no usable verdict defaults to kept, never dropped: silently losing a
  genuine need is worse than an occasional repeat.
- `polska/orchestrator.py`: `run_company_tick` runs one company's full cycle:
  sync, plan (via the planner agent), dedup, enqueue (capped by
  `limits.max_tasks_per_tick` before dedup and `limits.max_tasks_per_day` after),
  requeue any `FAILED` task whose backoff has elapsed, then dispatch as many
  `QUEUED` tasks as `limits.max_concurrent_tasks` allows. Dispatch runs
  concurrently: each dispatched task gets its own freshly opened `Session` rather
  than sharing the planning phase's session, since a synchronous `Session` is not
  safe to use from multiple concurrent callers, while the shared `AgentRunner` (and
  the `BudgetGuard` inside it) is, guarded by its own `asyncio.Lock`. A fatal,
  non-per-task exception from one dispatch (e.g. `CLIConnectionError`) is not lost
  among the others: every dispatch this tick is accounted for before it propagates.
- `retry_delay_seconds` / `is_retry_eligible`: the wall-clock backoff policy stated
  before this was built (`limits.retry_base_delay_seconds`, doubling by
  `retry_backoff_multiplier`, capped at `retry_max_delay_seconds`), measured from
  `Task.updated_at`, not a tick count.
- `run_startup_recovery`: calls `reconcile_orphaned_runs` once. Must run before the
  scheduler's first tick; `main.py` calls it first thing.
- `polska/main.py`: the process entrypoint. Loads config and settings once, builds
  the shared `AgentRunner`/`BudgetGuard`/`AdapterRegistry`, recovers orphans, then
  runs one `AsyncIOScheduler` job on `scheduler.interval_hours` that reloads every
  company profile from disk on each firing (so an edited or newly added profile is
  picked up without a restart) and ticks each active one in turn. One company's
  profile failing to load, or its tick raising, does not stop the others. Does not
  run migrations itself; `alembic upgrade head` stays a separate deployment step.
- 33 new tests across `test_sync.py`, `test_dedup.py`, `test_orchestrator.py` and
  `test_main.py`. 226 tests total.

### Fixed

- `migrations/env.py`'s `fileConfig()` call defaulted `disable_existing_loggers` to
  its own default of `True`, which silently disabled every logger not named in
  `alembic.ini` — including every `polska.*` logger — for the rest of the process.
  Harmless when Alembic runs as its own CLI invocation, but `test_migrations.py`
  imports this same module, so running the full suite silently killed logging for
  every test after it, caught by a `caplog`-based test that passed in isolation
  and failed in the full run.
- The inactive-company branch in `run_company_tick` logged an activity event but
  never committed before returning, so the log line evaporated when the session
  closed. Caught by the same kind of full-suite-vs-isolation discrepancy check.
- `LimitsConfig.max_concurrent_tasks` now allows `0` (plan and enqueue every tick
  without ever dispatching), distinct from `active=false` on the profile, which
  skips the tick entirely.

### Notes

- The `$defs`/`$ref` live check remains unresolved: attempted twice more this
  session, still "Credit balance is too low" both times. The planner still uses
  the nested schema as designed; flattening it is deferred until there is actual
  evidence it fails, per instruction not to work around an unconfirmed problem.

## [0.2.2] - 2026-09-24

Two follow-ups from 0.2.1's review, still before phase 3 starts.

### Added

- `RunStatus.RECONCILED` and `polska.budget.write_off_orphan`: the manual correction
  path for an `ORPHANED` run once its real cost becomes known some other way (a
  check against the console, or a confirmation it never actually billed). Requires
  a stated `actual_cost_usd`, never defaults to one, and never auto-clears any
  `BudgetHalt` the original worst-case pricing tripped, that stays a separate
  deliberate act. Writes an `ORPHAN_WRITTEN_OFF` activity event recording the old
  and new figures, who decided it and why: that event is the audit trail. No
  dashboard control for this exists yet; until phase 5 it is a direct call.
- `BudgetConfig.max_usd_per_task` and `polska.budget.actual_usd_for_task`:
  `limits.max_attempts` bounds retries, not spend, and on an expensive model one
  broken task retrying to exhaustion could cost most of a day's budget.
  `AgentRunner._fail_or_abandon` now abandons a task on either exhausted attempts
  or exhausted per-task spend, whichever comes first, checked independently.
- `LimitsConfig.retry_base_delay_seconds` / `retry_backoff_multiplier` /
  `retry_max_delay_seconds`: the stated policy for phase 3's requeue logic, wall
  clock against `Task.updated_at` rather than a tick count, so it doesn't need the
  orchestrator to track its own tick number and degrades sensibly if
  `scheduler.interval_hours` changes. Defaults: 15 minutes, doubling, capped at 4h.
- 7 new tests: write-off correctness, its audit trail, refusing a non-orphan, a
  halt surviving a write-off, `actual_usd_for_task` summing across a task's runs,
  and a task abandoned on cost with attempts still nominally available. 192 tests
  total.

### Notes

- The `$defs`/`$ref` live check remains unresolved: retried twice against a topped-up
  account, still "Credit balance is too low" both times. Not a code problem; retry
  again before the planner leans on structured output.

## [0.2.1] - 2026-09-24

Three correctness fixes to phase 2 raised in review, before phase 3 starts.

### Changed

- **The budget ledger is now denominated in dollars, not tokens.** Tokens are not
  fungible across models: summing raw counts across an Opus run and a Haiku run made
  a day-or-company ceiling meaningless. `BudgetConfig.max_tokens_per_day` and
  `max_tokens_per_company` are removed; `max_usd_per_run` is added (was missing
  entirely — only the day and company dollar ceilings existed before). The
  reservation size is now always `max_usd_per_run` in full, also passed to the SDK
  as `max_budget_usd` so both enforcement mechanisms agree on the same figure.
  `max_tokens_per_run` survives as a same-model, single-run output-size safety net,
  never used to derive a dollar estimate (the old worst-case estimate priced
  `max_tokens_per_run` entirely at the output rate, which undercounts badly for a
  tool-heavy agent whose input tokens can dwarf its output).
- `BudgetGuard.check_run_did_not_overshoot` now returns `list[BudgetHalt]` (was
  `BudgetHalt | None`): a run can overshoot its dollar reservation and its token
  safety net independently, and both are worth a halt, not just the first one found.

### Added

- `RunStatus.ORPHANED` and `polska.budget.reconcile_orphaned_runs`: every `Run` is
  now written to the database in `running` state *before* the SDK is ever called
  (`AgentRunner._create_running_run` / `_finalize_run`, replacing the old
  `_build_run_row` which only wrote a row after the call returned). A crash between
  those two points previously left real, possibly-billed spend with no row at all —
  invisible to a ledger defined as the sum of `runs`. `reconcile_orphaned_runs` finds
  any row still `running` at process start (which can only mean a previous process
  died mid-run), prices it at its reservation's worst case rather than assuming zero,
  and re-checks the day/company ceilings against that worse number immediately,
  writing a halt if the worst case alone crosses one. **Must be called once at
  process start, before phase 3's scheduler takes its first tick** — this is not
  wired into anything yet, since there is no process entrypoint until phase 3.
- `AgentRunner._fail_or_abandon`: a task that exhausts `limits.max_attempts` now
  becomes `abandoned` instead of being left in `failed`. Without this, the dedup fix
  from 0.1.1 was incomplete: `failed` suppresses a fresh planner proposal
  unconditionally (by design, since it's the orchestrator's own retry queue), and
  with nothing ever moving a task out of `failed` once retries were exhausted, it
  would suppress the same genuinely unmet need forever — exactly the failure mode
  `abandoned`-never-suppresses was added to prevent. Retrying a `failed` task that
  still has attempts left is left to phase 3's orchestrator (a scheduling decision:
  capacity, budget, backoff), not done by the runner itself.
- 5 new tests proving each fix rather than just re-asserting the new numbers: a
  fake `query_fn` that queries the database mid-call to confirm the `Run` row exists
  in `running` state before the SDK is invoked; a simulated crash (a pre-seeded
  `running` row) recovered by `reconcile_orphaned_runs` and shown to trip a halt; a
  task run to exhaustion across repeated `run_worker` calls, ending `abandoned` with
  the reason and attempt count recorded. 184 tests total.

### Notes

- One live verification was attempted and blocked, not completed: a script sending
  a Pydantic model with nested `$defs`/`$ref` as `output_format` to `claude-haiku-4-5`
  got as far as a real `ResultMessage`, confirming the runner's
  `ResultError`-after-a-result handling fires as designed, before failing on
  "Credit balance is too low" — an account billing issue, not a schema bug. Retry
  once the account has credit, before phase 3 relies on structured output.

## [0.2.0] - 2026-09-24

Phase 2 of 5: agent runner, dry-run adapter, cost accounting.

### Added

- `BudgetGuard` (`polska/budget.py`): reserves a run's full `max_tokens_per_run`
  ceiling before it starts and releases it on completion, checked under an
  `asyncio.Lock` against actual spend plus every other outstanding reservation.
  Fixes the concurrency gap raised in review: two dispatches checking the same
  ledger before either had written a Run row could together cross a ceiling
  neither would have crossed alone. Also enforces daily and lifetime dollar
  ceilings, and detects a run whose actual usage exceeded its own reservation.
  An uncleared `BudgetHalt`, global or per-company, blocks every new reservation
  regardless of current sums until a human clears it.
- Adapter interface (`polska/adapters/`): `IntegrationAdapter`, `AdapterResult`, an
  `AdapterRegistry`, and the one implementation phase 1 promised: `DryRunAdapter`,
  which records what it would have done and performs nothing.
- `polska/gate.py`: `dispatch_action` classifies a proposed action and either runs
  it immediately (reversible), auto-approves and runs it (irreversible, listed in
  config), or parks it pending (irreversible, not listed). `execute_approval`
  replays a decided approval's stored payload exactly as written; it is the same
  function an auto-approval calls now and the dashboard's approve button will call
  in phase 4. `force_dry_run` substitutes the adapter actually called without
  rewriting what was proposed, and marks the substitution in the execution record.
- `polska/runner.py`: `AgentRunner` executes one call to the Claude Agent SDK's
  `query()`, builds `ClaudeAgentOptions` from config (model, tool allowlist,
  system prompt, a JSON Schema passed to the CLI's `--json-schema`), and writes a
  `Run` row whatever happened. Cost prefers the SDK's own per-model `costUSD`,
  falling back to this project's pricing table only when the SDK reported none.
  `run_worker` owns a task's transition out of `queued` and into `done`,
  `awaiting_approval` or `failed`, and dispatches any actions the agent proposed
  through the gate. `query_fn` is dependency-injected so every test runs against a
  fake SDK stream, never the network.
- `polska/prompts.py`: one base system prompt per agent, folding in company brand
  voice and constraints for every agent except the dedup judge, plus config's
  `system_prompt_extra`.
- `polska/activity.py`: the one function that writes to the activity feed.
- 47 new tests: `test_budget_guard.py` (including a real `asyncio.gather`
  concurrency test), `test_adapters.py`, `test_gate_dispatch.py`,
  `test_runner.py`.
- `DEDUP_SUPPRESSING_STATES`, `DEDUP_LOOKBACK_STATES`, `DEDUP_NEVER_SUPPRESSES` in
  `polska.db.state`: see 0.1.1 below, folded in here as part of the same review pass.

### Changed

- `claude-agent-sdk` moved from a listed-but-unused `runtime` extra to a core
  dependency: the runner imports it directly.
- Agent model tiers retiered: see 0.1.1 below.

### Notes for whoever picks this up with a live key

- The SDK's exact surface (`ClaudeAgentOptions` fields, `ResultMessage` shape,
  `ModelUsage` key casing) was confirmed by introspecting the installed
  `claude-agent-sdk` 0.2.159 and reading its source, not recalled from training
  data. Several fields (`max_budget_usd`, `output_format`, `sandbox`, the
  `permission_mode` literal set) are newer than what a training-time guess would
  have produced.
- Every worker agent runs with `permission_mode="bypassPermissions"`: there is
  nobody present to answer an interactive tool-use prompt in an unattended
  scheduler, so the per-agent tool allowlist is the security boundary, not a
  runtime confirmation.
- Not yet confirmed against a live key: whether a Pydantic schema with nested
  models (`$defs`/`$ref`) round-trips cleanly through the CLI's `--json-schema`
  flag. Worth checking before phase 3 relies on it.
- `sandbox`/network isolation for the engineer's workspace (the "no network"
  half of the phase-1 decision) is not wired up. The primary control today is
  that the engineer's tool allowlist grants no `WebSearch`/`WebFetch`; the SDK's
  own `SandboxSettings.network` would add defence in depth but depends on OS
  sandbox support (bubblewrap on Linux) being present in the deployment
  container, which has not been verified against the target droplet.

## [0.1.1] - 2026-09-24

Fixes to the phase 1 design raised in review, ahead of phase 2.

### Changed

- Agent model tiers: `planner`, `marketer` and `analyst` moved from Opus to Sonnet 5;
  `support` moved to Haiku 4.5. `engineer` stays on Opus 5, and `dedup_judge` stays on
  Haiku 4.5. Opus everywhere was the safe first default; it is not sustainable for a
  loop firing every 4 hours, and engineering is the one job worth paying Opus rates for.

### Added

- `DEDUP_SUPPRESSING_STATES`, `DEDUP_LOOKBACK_STATES` and `DEDUP_NEVER_SUPPRESSES` in
  `polska.db.state`, replacing a design that would have used "queued or recently
  completed" and swallowed a real, unmet need for up to 14 days once its task was
  abandoned. `ABANDONED` now never suppresses a fresh proposal at any age. `FAILED`
  suppresses unconditionally (it is the orchestrator's own retry queue, not settled
  work). `DONE` suppresses only inside the lookback window. A test asserts the three
  sets partition every `TaskState` with no gaps and no overlap.

### Planned (recorded here so the design survives to phase 2)

- Budget accounting will reserve a run's full `max_tokens_per_run` ceiling before
  dispatch and release it on completion, rather than only summing actual spend from
  `runs` after the fact. Concurrent dispatch checking a stale sum before either run
  had written its cost was a real gap, caught before the runner existed to hit it.

## [0.1.0] - 2026-09-24

Phase 1 of 5: schema, models and migrations.

### Added

- Seven tables: `companies`, `goals`, `tasks`, `runs`, `approvals`, `budget_halts`
  and `activity_events`, multi-company from the start.
- Task state machine in `polska.db.state` with an explicit transition table.
  `Task.transition_to()` is the only supported way to change state; it raises
  `IllegalTransition` on an illegal move and keeps timestamps and the attempt
  counter in step.
- Alembic migrations wired to read the database URL from settings rather than
  `alembic.ini`, so the app and the migrations cannot point at different databases.
- `UTCDateTime` column type: timezone-aware UTC in and out, rejecting naive
  datetimes rather than guessing what they meant.
- SQLite configured with WAL, `foreign_keys=ON`, `synchronous=NORMAL` and a busy
  timeout, applied on every connection.
- Config layer: `config/default.yaml` validated by `polska.config.appconfig`, with
  scheduler intervals, concurrency and daily limits, budget ceilings, dedup
  thresholds, per-agent model and tool allowlists, the irreversible-action list and
  model pricing. Unknown keys are refused so a typo cannot fall through to a default.
- Company profile schema and loader, plus `companies/example.yaml` as a template.
- Environment settings via pydantic-settings, and `.env.example` documenting every
  variable with no values.
- Pydantic contracts for every agent output: `PlannerOutput`, `ProposedTask`,
  `DedupJudgeOutput`, `DedupVerdict`, `AgentResult` and `ActionRequest`.
- 127 tests. The state machine is covered exhaustively across all 36 state pairs
  plus a graph walk proving no state traps a task. The approval gate has its own
  file. `test_migrations.py` asserts the migration and the models agree.
- Ruff lint and format configuration.

### Notes on decisions taken

- The budget guard refuses to start if any configured agent model has no pricing
  entry, rather than treating an unpriced model as free.
- `auto_approve` ships empty, and listing an action there that is not also in
  `irreversible_actions` fails config validation.
- An action type nobody has classified is treated as irreversible. Fail closed.
- Credential-looking keys are refused in both company profile options and approval
  payloads, since both end up in tracked files or on the dashboard.
- Run cost is frozen in USD and GBP at write time with the FX rate used, so changing
  the rate later cannot rewrite historical figures.

## [0.0.1] - 2026-09-24

### Added

- Repository scaffold: git init on `master`, `dev` working branch.
- `.gitignore` covering `.claude/`, node, Astro, build output and secrets.
- `README.md` and `CHANGELOG.md` placeholders.
