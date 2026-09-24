# Changelog

All notable changes to this project are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
