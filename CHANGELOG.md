# Changelog

All notable changes to this project are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
