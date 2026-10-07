# Changelog

All notable changes to this project are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.9.0] - 2026-10-07

A full read of the codebase after a run of reactive fixes. Each finding was checked
against the code, and where possible against the live droplet's database or processes,
before it was fixed.

### Correction to 0.8.8

- 0.8.8 said ticking from cron changed no security boundary. That was wrong. The cron
  tick runs as uid 1000 without `--no-new-privs`, and from there `setpriv --reuid=0`
  gives root in the container (checked: `uid=0(root)`). Root there holds NET_ADMIN and
  could remove the agent's network jail. Manual ticks always had this; 0.8.8 made it the
  daily path. This release closes the main untrusted route into that process (npm
  install scripts, below). The rest is the open privilege-model decision.

### Fixed

- **A budget halt now stops the tick before planning.** An open halt already refused
  every reservation, but the tick planned and dispatched through it anyway: a blocked run
  per agent, and every queued task charged an attempt for a run that never happened, so
  three ticks abandoned a task that never ran. The tick now records why it stopped, and
  `polska-cli tick` prints it and exits 1.
- **Recovering a cut-off run's answer could never work.** It looked for
  `input["input"]` as a JSON string; the accepted `StructuredOutput` call carries the
  fields directly (checked across ten real runs). It now reads every shape seen live.
- **Success with no output and no actions is no longer counted as done**, as
  `AgentResult` always said it would be.
- **`observations` and `iterations_attempted` are kept** in the task result instead of
  being validated and discarded, and observations now reach the next planner prompt,
  as the schema documented.
- **A worker whose launch raises is moved out of `running`**, with an attempt counted.
  Nothing else ever moves a task out of `running`, so it stayed there for good,
  holding a concurrency slot.
- **Startup recovery also moves tasks left in `running`** by a process that died (a
  deploy recreating the container mid-tick is enough), not just their runs.
- **Ticks can no longer overlap.** Each `polska-cli tick` starts by treating every
  `running` run as orphaned, so one started during another mis-priced the first's live
  runs. A lock file under `data/` now covers the scheduler job and every CLI tick.
- **Vendoring runs npm with `--ignore-scripts`.** Package install scripts are
  third-party code running as uid 1000, which on the cron path can reach root (above).
  CarScratch's full test suite passes on a tree installed this way (10/10, as the agent
  user).
- `write_off_orphan` refuses a negative cost.

### Removed

- Config settings no code ever read: `limits.task_timeout_seconds`, `dedup.judge_model`,
  `integrations.default_adapter`, and each agent's `enabled`. Each read like a control and
  did nothing; setting an agent's `enabled: false` did not stop it being dispatched. A
  config that still sets one now fails validation, as any unknown key does.

### Still open, decisions for Javier

- carscratch is halted now (halts 27 and 28, written when run 80 crossed the engineer's
  token ceiling). Nothing runs until it is cleared on the dashboard's budget page.
- The engineer's token ceiling sits below what `max_turns` allows (see 0.8.9).
- The privilege model (narrow `sudo` design in wiki `polska.md`).
- The planner once read its own `$0.25` per-run cap as the day's budget.

## [0.8.9] - 2026-10-07

Found by the first full tick through the new cron line (19:13 UTC, 4m12s, exit 0, $0.92):
planner, dedup judge and engineer all launched with the flags in `config/default.yaml`,
and the engineer did real work for the first time since the v0.6 to v0.8 fixes. It
found the plan, took a baseline (10/10 tests), wrote the check and its tests, and passed
cycle 1 before being cut off at 767,892 tokens against the 750,000 ceiling (22 turns,
$0.86 of its $2.00; 89% of the tokens were cache reads). Task 24 was abandoned for it.

### Fixed

- `node_modules` had no execute bits at all (0 of 27,824 files): the vendoring step ran
  `chmod(0o666)` on every file to make the tree writable, which also stripped `+x`. No
  native binary or script in it could run, so the engineer lost seven turns (about a third
  of the run) to `npx vitest` failing and then copying esbuild to `/tmp` to chmod it
  itself. The permissions are now added, not replaced (`_open_up_vendored_tree`), and
  symlinks are no longer chmod'd through. Only workspaces vendored from now on benefit;
  existing ones keep the marker and are not redone.

### Not changed (a decision, and a standing instruction not to raise ceilings)

- The engineer's `max_tokens_per_run` (750,000) counts cache reads at face value, so it
  caps turns times context size rather than spend: at about 31k tokens a turn it is
  reached at turn 22, below the `max_turns` of 40, while the dollar ceiling ($2.00) is
  less than half used. Every engineer run is likely to hit it however well it behaves.

## [0.8.8] - 2026-10-07

Scheduled ticks have not worked since the entrypoint gained `--no-new-privs` (0.5.0): the
in-container job cannot launch an agent, and 3 to 6 Oct every daily tick died at launch.
This release makes ticks happen through the path that does work, without changing any
security boundary.

### Changed

- `scheduler.enabled` is now `false` in `config/default.yaml`, with the reason and the way
  back written beside it. The in-container job could only fail at launch.
- Ticks run from the droplet's cron (`30 7 * * *`, as `javier`, under `flock`) through
  `docker compose exec --user polska scheduler python -m polska.cli tick`, output to
  `~/polska-tick.log`. README "Scheduled ticks" documents it, including that the cron
  entry must be removed when `scheduler.enabled` goes back to `true`.

### Not fixed

- The privilege model itself: the in-container scheduler still cannot launch agents. A
  narrow design (a `sudo` rule letting uid 1000 become only uid 1001, with `setpriv`'s file
  capability removed so uid 1000 cannot reach root) is proposed in wiki `polska.md`, not
  built, because it changes the agent sandbox and needs a decision.

## [0.8.7] - 2026-10-07

Found by running 0.8.6 under the real failure (a tick as uid 1000 with `--no-new-privs`)
after deploying it: the run row now closed correctly, but the `error` activity event it
was meant to write never reached the feed.

### Fixed

- The `error` event was only flushed, never committed (`activity.log` flushes by design).
  The exception that follows rolls the session back as it leaves the tick, so the event
  was lost; the run row survived only because `_finalize_run` commits. Now committed. The
  test reads back after a rollback, so it can tell the difference (the first version read
  through the same session and passed regardless).

## [0.8.6] - 2026-10-07

Four planner rows sat in `running` on the dashboard from 3 to 6 Oct, with no tokens and
no cost. They were never processes. Each was a scheduled tick where the agent wrapper
exited 127 (`setpriv: setresuid failed`, the scheduler's `--no-new-privs` blocking its own
uid switch, see 0.8.3 "Known issues"), the SDK raised `ProcessError`, and the runner only
closed a run row for a few specific exceptions, not that one.

### Fixed

- Any exception escaping the SDK call now closes the run row as `failed` (keeping the
  usage streamed before it, and the error text) and writes an `error` activity event with
  the run id, then re-raises as before. Before, the row stayed `running` until the next
  process start, where the startup reconciler prices it at its worst case, and nothing in
  the activity feed said anything had gone wrong.

### Not fixed (still the cause of the failing ticks)

- The scheduled job still cannot launch an agent. That needs the privilege-model decision
  recorded under 0.8.3 "Known issues". This release only stops it leaving stuck rows
  behind and makes it visible.
- A failure that raises out of the planner still stops that company's queued tasks being
  dispatched in the same tick (task 24 has been `queued` since 2 Oct for this reason).
- A worker task whose launch raises would be left in `running` (it counts against
  `max_concurrent_tasks`) because `run_worker` does not catch it. Latent: the planner
  fails first today, so no worker has been affected.

## [0.8.5] - 2026-10-02

Found by running 0.8.4's fix against the real stuck task-24 workspace as the scheduler
user, before deploying it: a second bug in the same v0.7.0 `work/` code.

### Fixed

- `work/node_modules` was a dangling symlink in production. A symlink target is
  resolved against the link's own directory, and production's workspace root is the
  relative `data/workspaces`, so the relative target pointed at a path that does not
  exist and the engineer's writable copy had no `node_modules` at all. The target is
  now absolute. pytest's `tmp_path` is absolute, so no test could have seen it; the
  new one runs from a relative root.
- The mode-fixing walk over `work/` no longer chmods symlinks (it raised on the
  dangling one).

### Verified live

- `_ensure_writable_copy` against task 24's real workspace as uid 1000: the broken
  partial `work/` was removed, rebuilt and marked ready, `node_modules` resolves
  (625 packages) and `repo/` is still read-only. Run from a copy of the patched file
  before the image was rebuilt.

## [0.8.4] - 2026-10-02

Found by the first live tick run as the scheduler user (`exec --user polska`), after
0.8.3 deployed: the planner succeeded and enqueued two tasks, the analyst task ran to
completion, and the engineering task died preparing its workspace.

### Fixed

- The engineer's `work/` copy could never be created by a non-root user. `copytree`
  copies the locked read-only repo's mode onto `work/` (0555), and the `node_modules`
  symlink was created inside it before the chmod that makes it writable. Root ignores a
  read-only parent, so every earlier manual tick passed; this was the first time the
  v0.7.0 code ran as anyone else. The chmod now comes first.
- A filesystem error preparing one task's workspace (`OSError`, not just
  `WorkspaceError`) now fails that task and counts an attempt. It used to escape,
  leave the task `queued` with no attempt counted, fail identically on every tick and
  take the whole company's tick down with it.

### Verified live

- Analyst process launched with `--tools Read,Glob,Grep,WebSearch,WebFetch
  --allowedTools Read,Glob,Grep,WebSearch,WebFetch --max-turns 25 --max-budget-usd 1.0
  --model claude-sonnet-5` (read from `/proc` by `scripts/verify_configured_tools.sh`),
  matching `config/default.yaml`. Analyst task 25 completed: $0.24, 23 tool calls.
  The engineer's flags are not yet observed.

## [0.8.3] - 2026-10-02

Follow-up to a full review of the repo after the scheduled ticks on 1 and 2 Oct
produced nothing. 0.8.2 stopped one crash but was not checked as the real scheduler
user (it was ticked by hand through `docker compose exec`, which runs as root and
through a separate copy of the tick code), and when checked properly it deleted
nothing, left `node_modules` at mode `0200` and still reported it as reclaimed.

### Added

- `run_tick_cycle` in `orchestrator.py`: the one function both the scheduler job and
  `polska-cli tick` now run, returning a `CycleResult` (per-company summaries, which
  companies failed).
- `TickSummary.planner_status` / `planner_error`, logged and printed
  (`planner=invalid_output ...`), so `proposed=0` can no longer mean either "chose to
  do nothing" or "its output was rejected". The CLI exits 1 on a failed company or a
  planner run that did not succeed.
- One retry for a planner reply that fails validation, with the validation error in
  the prompt. A malformed structured-output reply on 2 Oct 2026 cost the whole daily
  cycle; the retry costs a few cents.
- `scripts/` is now copied into the image. Every documented
  `docker compose exec ... sh scripts/...` (README, wiki, the three verify scripts)
  failed with "No such file" because it never was.
- Tests: housekeeping isolation and ordering, planner status, planner retry,
  reclaim accounting, `_force_rmtree` behaviour. The existing `test_main.py` fake
  runner had no `budget_guard`, so its "healthy" ticks were crashing and being
  swallowed; it now completes a real tick and the tests assert on the result.

### Changed

- Housekeeping (reconcile removed companies, reclaim `node_modules`) now brackets
  the company ticks and each half is isolated: a failure is logged and skipped
  instead of cancelling every company's planning for the day (the 1 and 2 Oct
  incident).
- `polska-cli tick` calls `run_tick_cycle` instead of its own copy of the loop.
- `_force_rmtree` returns whether the tree is really gone. A partial `node_modules`
  or `work/` that cannot be removed now raises `WorkspaceError` (the task fails
  properly) instead of being built on top of.

### Fixed

- Reclaim counts a workspace only if its `node_modules` is actually gone, warns about
  the ones it could not clear, and carries on past one it cannot touch.
- The permission handler added bits instead of replacing the mode: the old
  `chmod(path, S_IWRITE)` is exactly `0o200`, which strips read and execute from a
  directory and left `node_modules` untraversable. It also now fixes the parent
  directory's write bit, which is what actually blocks an unlink on Linux.
- `docker-compose.yml`, README and `verify_configured_tools.sh` now say to tick with
  `--user polska`, as the compose file already did.

### Known issues (found, not fixed here)

- **The scheduler process cannot launch agents.** The entrypoint starts it with
  `--no-new-privs` (added in 0.5.0), which blocks `polska-agent-cli`'s
  `setpriv --reuid=1001` (`setresuid failed: Operation not permitted`; checked on the
  droplet against PID 1: uid 1000, `NoNewPrivs: 1`). The agent runs recorded since 29 Sep
  sit in tight clusters that look like manual `exec` ticks, which skip the entrypoint;
  I have not proved that none ever came from the schedule. Needs a decision on the
  privilege model, not a quiet edit.
- Files a test run creates as the agent user inside `node_modules` (vitest's
  `.vite/` cache) cannot be deleted by the scheduler user, so reclaim now reports them
  instead of hiding them but still does not free the space.
- The planner reads its own `max_budget_usd` ($0.25) as the day's remaining budget and
  declines to plan, even though its prompt says $10.00. The retry does not fix this.

## [0.8.2] - 2026-10-02

The `reclaim_node_modules_for_terminal_tasks` cleanup crashed on every tick
when trying to delete node_modules owned by a different UID (agent uid 1001,
scheduler uid 1000). Files created by the agent during a run are owned by
1001; the scheduler running as 1000 cannot chmod them, so `_force_rmtree`'s
error handler would crash trying. Silenced on permission errors: cleanup is
best-effort for old artifacts and a stray file from a past run does not block
future work.

### Fixed

- `_force_rmtree` in `workspace.py` wraps the chmod in try/except and skips
  files it cannot delete rather than crashing. Scheduled cleanup that fails
  for one file now continues with the rest.

## [0.8.1] - 2026-09-30

`runner.py` had, since phase 2 (24 Sep), only ever set `allowed_tools`
(auto-approval only, moot under `bypassPermissions`) and never `tools`
(the field that actually restricts which tools exist). Every agent has
had the CLI's full default toolset regardless of its configured
allowlist, for six days, across every real dispatched run. Reported last
session; fixed now on explicit instruction rather than left open while a
follow-on question was chased.

### Fixed

- `tools=list(agent_config.tools)` added alongside the existing
  `allowed_tools=` in `ClaudeAgentOptions`. Two stale comments in the same
  file corrected: both previously asserted `allowed_tools` was the actual
  restriction, which is exactly the belief this closes.

### Added

- `test_the_configured_tool_list_actually_restricts_availability`
  (`test_runner.py`): captures the real `ClaudeAgentOptions` passed to a
  fake `query_fn` and asserts `options.tools` matches the agent's
  configured list. Nothing previously asserted on either field.
- `scripts/verify_webfetch_channel.sh`: checks by observation, not
  documentation, whether `WebFetch` makes its own local network call
  (which the uid-1001 iptables jail would catch) or is served through
  Anthropic's hosted infrastructure (which the jail cannot see). Reads
  the REJECT rule's own packet counter before and after a real WebFetch
  call. Not yet run against the real droplet.
- `scripts/verify_configured_tools.sh`: watches `/proc` for the real
  `claude` CLI process during a live tick and prints its actual
  `--tools`/`--allowedTools` flags, so the fix can be confirmed from
  outside the Python source on a real dispatch, not just from a passing
  test suite.

## [0.8.0] - 2026-09-30

Two confirmed instances of the same shape (task 13, task 22) plus a third
overnight (the six-source analyst task failing again at 905k tokens) made
this the next real fix: a single planner proposal bundling several
independent, unrelated units of work into one task, which can only ever
finish some of them and has no way to report on the rest.

### Added

- `ProposedTask.sub_units` (`schemas/planner.py`): a list of `SubUnit`
  (title + description), written by the planner itself rather than
  templated mechanically, since it already has the context to scope each
  unit correctly. Exactly one entry is rejected by the schema (not a real
  split); zero is the normal case and stays untouched.
- `orchestrator._flatten_sub_units`: runs before the per-tick cap and
  dedup, not after — expands a proposal with `sub_units` into one
  synthetic `ProposedTask` per unit, each inheriting the parent's
  `type`/`goal_key`/`rationale`/`priority` but with its own title and
  description, so each unit gets its own dedup match, its own budget
  ceiling, and its own verify-loop attempt instead of sharing one. The
  parent proposal itself is never enqueued once split. A new
  `ActivityKind.TASK_SPLIT` event records which units a proposal became.
- The planner's system prompt now explicitly names this pattern and when
  to use `sub_units` instead of describing bundled work as one task.

### Changed

- `PlannerOutput`'s JSON schema grew (the nested `SubUnit` definition adds
  roughly 800 estimated tokens to every planner call's pre-dispatch size
  check). Four `test_runner.py` watchdog tests had their token ceilings and
  fake usage values scaled up proportionally (×3) so they still exercise
  the mid-stream watchdog rather than being refused pre-dispatch by their
  own deliberately tight ceilings; the boundary and crossing behaviour
  they test is unchanged, just at a larger absolute scale.

## [0.7.0] - 2026-09-29

Node/npm fixed run 40. Vendoring fixed tasks 9 and 10. The loop bound never
got a chance to run: the engineer was inventing a writable working area by
hand, mid-run, at real token cost, before it could ever attempt cycle 1.

### Added

- `<task_workspace>/work/`: a genuinely writable copy of the read-only
  `repo/` clone, created once at workspace prep, with `node_modules`
  symlinked rather than duplicated (it is already vendored once, at real
  disk cost; copying several hundred MB a second time for a directory that
  never needs editing would undo the reclaim policy's own point).
  `repo/` stays exactly as it was, untouched, what a proposed change is
  diffed against. Engineering tasks only; research never writes anything
  and would just pay for a copy it cannot use. Self-heals via a completion
  marker the same way `node_modules` vendoring does, and applies
  retroactively to existing workspaces the same way too.
- `polska.trace_analysis.summarize_verification_attempts`: derives how many
  distinct verification attempts a run actually made from its own
  `tools_called`, not from the model's self-reported
  `iterations_attempted`. Built because that field is only ever set on a
  run that finishes cleanly, since it is part of the model's own final
  structured answer, and a run cut off by the token watchdog is exactly
  the case with no such answer. Deduplicates two different invocation
  methods for the same underlying attempt (found live: `npx vitest run`
  then, after a permissions check that changed nothing, `node
  node_modules/vitest/vitest.mjs run`) into one, using the same
  edit-then-verify cycle boundary the loop bound itself describes.
  Distinguishes "attempted" from "confirmed to have run" where the trace
  allows it (an unpiped command's own exit status), and says plainly when
  it cannot (a piped command's exit status reflects the pipe's last stage,
  not necessarily the verification command).

## [0.6.0] - 2026-09-29

Every engineer run traced this session was cut off by the token ceiling
mid-attempt, never by finishing and being wrong, never by reporting a
bounded failure, because nothing had ever told it there was a bound to
reach. This is the fix for the actual finding of the investigation, plus
two operational gaps found along the way.

### Added

- The engineer's edit-and-verify loop is now bounded at 3 cycles instead of
  open-ended. `AgentResult.iterations_attempted` records how many it
  actually used. Exhausting the bound without a passing result is an
  ordinary `failed` outcome with a cycle-by-cycle `failure_reason`, ready
  for the normal retry/backoff path, not a new state: retrying may
  genuinely help here, unlike a ceiling-driven `interrupted`. Does not
  weaken the verifiable-artefact requirement; gives the agent an honest way
  to stop short of meeting it instead of the only alternatives being "keep
  going until something external cuts the connection" or "claim success
  anyway".
- `reconcile_removed_companies`, run once per tick cycle: deactivates, and
  abandons the open tasks of, any company whose profile file no longer
  exists. `sync_company` now does the same the moment `active` flips
  `true -> false` on an edit. Both close the same gap: dispatch only ever
  happens inside a per-company tick, itself only ever entered for a company
  whose profile is currently discovered, so a queued task under a removed
  company was never actually reachable through the normal path, but that
  protection was an accident of where dispatch happens to live, not a
  decision anyone made, and would have stopped holding the moment that code
  changed without anyone knowing this was relied on.
- `node_modules` is now reclaimed automatically once a task has sat in a
  terminal state for `limits.workspace_node_modules_grace_hours` (default
  24). It is ~95%+ of a vendored workspace's size (measured: 393-428MB per
  task) and has zero diagnostic value once a task is done with it; the rest
  of the workspace is left alone indefinitely. At the daily task cap,
  unreclaimed installs would otherwise accumulate at several GB/day.
- A completion marker (`node_modules/.polska-vendored`) written only once
  an install genuinely succeeds. `node_modules` existing was already
  corrected once this session, from standing in for "a workspace was
  prepared before"; this is the same correction one level deeper, to "the
  install that made it finished" rather than "something under this name
  exists" -- a container killed mid-install used to leave a partial tree
  permanently indistinguishable from a real one. Found while answering
  whether a short install is detectable, not hypothetically: the fix self-
  heals by deleting and reinstalling rather than trusting a marker-less
  `node_modules`.

## [0.5.4] - 2026-09-29

### Added

- The database moved onto its own bind mount, `db/`, `chmod 700`, separate
  from `data/`. It used to share `data/` with task workspaces, which the
  agent subprocess needs to traverse into for its own task — and, found
  live, used to read the database directly through that same mount. Cheaper
  than a filesystem overhaul: an unprivileged UID has no path at all into a
  mount it's simply not on, no `CAP_SYS_ADMIN` needed. `Settings.database_url`
  now defaults to `db/polska.db`; see README's "Moving the database onto its
  own mount" for migrating an existing deployment across without losing
  the WAL sidecar files.
- README's "The agent's filesystem access" section: what's confirmed closed
  (the database), what's deliberately still open (cross-task workspace
  access, since the real fix needs `CAP_SYS_ADMIN` and the actual exposure
  is bounded while `force_dry_run` holds), and the trigger to revisit that
  decision — the first real adapter going live, not a fixed date.

## [0.5.3] - 2026-09-29

### Fixed

- Every agent subprocess had been inheriting the dashboard's own
  `POLSKA_ADMIN_PASSWORD_HASH` and `POLSKA_SESSION_SECRET` since the
  dashboard shipped. `ClaudeAgentOptions.env` only adds to or overrides
  individual keys in the SDK's own subprocess environment, it never
  replaces it (confirmed against the SDK's own source), so nothing set on
  that field had ever actually stopped the full parent environment
  reaching the agent. The `polska-agent-cli` wrapper script now runs the
  real CLI under `env -i`, passing through only `ANTHROPIC_API_KEY`, `PATH`
  and `HOME` — the only point in the chain that can produce a genuinely
  minimal environment rather than one more key layered onto a full one.
  The build smoke check now also greps the generated wrapper for `env -i`,
  so a future edit that quietly drops this fails the build.

## [0.5.2] - 2026-09-29

The v0.5.0 toolchain fix didn't fix tasks 9 and 10: two more engineer runs,
interrupted at their ceiling, that never got as far as running a test.

### Fixed

- `prepare_task_workspace` only checked whether a clone existed, and skipped
  dependency vendoring entirely if it did. Correct for its original purpose
  (never re-clone on a retry, a second unnecessary use of the credential),
  but it meant any task whose workspace was cloned before vendoring shipped
  never got it, on any future retry, forever, indistinguishable from Node
  never being installed at all. `_ensure_node_dependencies_vendored`
  (renamed from `_vendor_node_dependencies`) now checks the thing that
  actually matters, whether `node_modules` is present, on every workspace
  prep call, fresh or years old, and self-heals the old ones. When the
  lockfile has already been stripped from an old clone, it resolves with
  `npm install` instead of `npm ci` rather than re-fetching just that file,
  which would mean a second use of the clone credential — the exact cost
  the idempotency check was protecting against in the first place.

## [0.5.1] - 2026-09-29

The v0.5.0 build failed on the first real attempt: `setcap` isn't in
`python:3.12-slim` and wasn't pulled in by anything else, so
`docker compose build` exited non-zero at that step — and `docker compose up`
then happily started the *previous*, unenforced image, because it had no
reason to know the new one never finished. A container carrying the network
boundary silently running without it is the same class of problem as
starting against a stale schema.

### Fixed

- `setcap`/`getcap` are provided by `libcap2-bin`, not installed by the
  packages already in the image. Added.

### Added

- A build-time smoke check verifying `setcap`, `getcap`, `iptables`, `node`,
  `npm`, `npx` and `setpriv` are all actually present (and that `setpriv`
  really carries the capability `setcap` was meant to grant it), failing
  `docker compose build` loudly and by name rather than at first use.
- `POLSKA_AGENT_ENFORCEMENT_VERSION`, baked into the image only if every
  step the network restriction depends on succeeded, and checked by
  `docker/entrypoint.sh` before it does anything else. A missing or
  mismatched marker (an image that predates this, or one from an
  incomplete build) refuses to start, the same fail-closed principle
  `schema_check.py` already applies to a stale database, now covering a
  stale image too — this is what actually closes the incident above, the
  smoke check only stops it happening in the first place.

## [0.5.0] - 2026-09-29

Found running the real deployment: an engineer task with a genuinely narrow,
single-file scope still burned most of its run ceiling, because the container
had no way to run the TypeScript test suite it was asked to verify against,
and instead of reporting that, it improvised a workaround (copied the file
out of its read-only checkout, patched it with a hand-written script).

### Added

- Node 22 in the image, and dependencies (`npm ci` against the repo's own
  `package-lock.json`) vendored once per task workspace, before the agent
  ever starts — the same controlled, outside-the-agent's-own-execution step
  that already injects the git credential. `node_modules` is deliberately
  excluded from the read-only lockdown the rest of a cloned repo gets, so an
  existing test command has somewhere to write its own cache or temp output.
- Real network enforcement for every agent subprocess, not the "you have no
  network access" sentence in a system prompt this project shipped with
  until now. Every Claude Agent SDK invocation runs as a second, dedicated
  user (`agent`, uid 1001) with an iptables rule restricting its egress to
  the Anthropic API and nothing else — REJECT, not DROP, so a blocked
  attempt reads as deliberate rather than a transient failure worth
  retrying. `scripts/verify_agent_egress.sh` proves this from the
  restricted UID's own perspective rather than asking anyone to trust that
  the rule is present in the table.
- `TaskState.BLOCKED`, a first-class outcome distinct from `failed` or
  `abandoned`: the agent reporting that this environment cannot execute a
  task at all (missing runtime, no way to verify a change) rather than that
  it tried and failed. Never subject to `limits.max_attempts` or
  `budget.max_usd_per_task` — retrying an environment gap at the same scope
  cannot help, only fixing the gap (or deciding not to) changes the answer —
  and never suppresses a future proposal for the same work, since the gap
  that caused it may since be fixed. Every worker agent's system prompt now
  states this as the expected response to a wall it cannot get past.

## [0.4.4] - 2026-09-29

### Fixed

- A run-scoped `max_tokens_per_run` halt could render with a `$`, e.g.
  `$773927.0000 against a limit of 750000`, a token count formatted as
  dollars with four decimal places. Found on the dashboard's budget page, but
  the same unconditional `$` formatting was also in `BudgetExceeded`'s own
  message, so it was reaching `Run.error` and the activity feed too: every
  dispatch blocked by an already-open token-ceiling halt (via `reserve()`'s
  `active_halt()` check) got the mislabelled figure baked into a stored
  error, not just a display glitch. `BudgetHalt.is_token_denominated` is now
  the one place that decides this, used by both `BudgetExceeded.__init__` and
  the template rather than each re-deriving `limit_name == 'max_tokens_per_run'`
  on its own.

## [0.4.3] - 2026-09-29

Found running the real deployment: every tick was failing, and the planner had
already reasoned itself into an incorrect budget figure before this was caught.

### Fixed

- The scheduler could not run any agent at all. The container ran as root, and
  the Claude Code CLI refuses `--dangerously-skip-permissions` (what
  `permission_mode="bypassPermissions"` becomes at the CLI level, see
  `runner.py`) for root, by the CLI's own design. `Dockerfile` now creates a
  fixed-UID (1000) non-root user and runs as it. `permission_mode` itself is
  unchanged — the tool allowlist stays the security boundary, this only fixes
  who the process is. Also installs `git`, never actually present in the
  image before now: `workspace.py` shells out to it for the engineer/analyst
  task workspace, and nothing had reached that code path yet only because
  every tick was failing earlier, at CLI startup. See README's new
  "Upgrading a deployment that predates the non-root container user" section
  — every file already under `data/` on a running droplet is root-owned on
  the host and needs a one-time `chown` before this rebuild, or the new
  non-root container cannot write to its own database.
- The planner was given no real budget figure and, separately, was shown raw
  exception text as a task's "recent outcome" — text that can (and did)
  describe a completely unrelated run's cost ceiling, which it then mined a
  dollar figure out of and reported as "remaining budget for this cycle,"
  wrongly, while `max_usd_per_day` had a lot of room left. Fixed in two
  parts, deliberately not one: `BudgetGuard.remaining_today_usd`, the exact
  figure `reserve()` itself checks against, is now the one number the
  planner is given for "how much is left today"; and a concluded task's
  outcome is now a structured, number-free reason
  (`orchestrator._task_outcome_reason`, keyed off the task's own last `Run`
  status) rather than its raw `error` text, so a task requeued after being
  blocked by someone else's halt can never again surface that halt's figures
  as if they were its own. The planner's system prompt also now states the
  rule directly: never cite a number not explicitly given in the prompt.

## [0.4.2] - 2026-09-28

Two more found running the real deployment.

### Fixed

- `polska.cli init-auth` printed the Argon2 hash unquoted. Docker Compose's
  `.env` parsing interpolates `$` as a variable reference in an unquoted or
  double-quoted value (confirmed against Compose's own `env_file` docs), and
  an Argon2 hash is full of literal `$` (`$argon2id$v=19$m=...$salt$hash`) —
  silently mangling it into whatever the referenced variable expands to
  (usually nothing), with no error anywhere to say why the login stopped
  working. Now prints both the hash and the session secret single-quoted,
  which Compose's docs confirm is taken literally, verified against both
  Compose's `env_file:` parsing and python-dotenv (used for a non-Docker
  `.env`) — and prints the `docker compose run --rm scheduler env | grep`
  command to verify it landed correctly rather than assuming it did.
- Nothing checked the database's schema against migrations at startup. A
  database copied forward from a previous version let both containers start
  clean; only the dashboard failed, and only once it happened to query a
  column that did not exist yet — the scheduler would have ticked against the
  same stale schema in 24 hours, whatever that failure looked like.
  `polska.db.schema_check.assert_schema_is_current`, called at the top of
  `main.py`, `dashboard/server.py` and `cli.py`'s `tick` command, now refuses
  to start at all when the database's alembic revision does not match the
  migrations' head, naming both revisions and the exact command to fix it.
  Deliberately does not auto-migrate: this project's own rule is that
  `alembic upgrade head` stays a separate, human-run step; the fix for silent
  staleness is to fail loudly and immediately, not to remove the step.

## [0.4.1] - 2026-09-28

Found on the first real `docker compose build` (both containers crash-looped on
startup): `Settings.config_path`/`companies_dir`/`workspace_root`/`database_url`
defaulted to a path derived from `Path(__file__).resolve().parents[3]`, correct
only for an editable install. The non-editable install the Docker image
actually does copies the package into site-packages, where that computation
lands under `/usr/local/lib/python3.12/` instead of the repo — surfaced as
`load_app_config` raising `FileNotFoundError` for
`/usr/local/lib/python3.12/config/default.yaml`. `migrations/env.py` inherits
the same bug through `database_url`.

### Fixed

- Those four `Settings` fields now default to plain paths relative to the
  process's current working directory, not to this module's own location.
  That is the one resolution strategy already correct in both real contexts
  this project has (repo root in dev, `WORKDIR /app` in the container, where
  `docker-compose.yml` bind-mounts `config/`/`companies/`/`data/` at exactly
  that path) — correct with or without `.env` populated, rather than
  depending on every path env var reaching the container intact. `.env`'s
  explicit values still override these; they are just no longer load-bearing
  for basic correctness.
- Audited every other path resolution for the same class of bug:
  `dashboard/app.py`'s template directory correctly uses a `__file__`-relative
  path (verified by building a real wheel and checking the 5 template files
  are actually packaged); `workspace_root` inside `workspace.py` is a passed
  parameter, not an independent computation; `scripts/observe_ticks.py`'s own
  `REPO_ROOT` is a dev-only tool always run from the repo root, unaffected.
- Verified the fix directly, not just reasoned about it: simulated the exact
  container conditions locally (empty environment, `config`/`companies`/`data`
  laid out under a directory playing `/app`) and confirmed both
  `load_app_config` and `alembic upgrade head` now resolve correctly with zero
  `POLSKA_*` env vars set at all.

## [0.4.0] - 2026-09-28

Phase 4: the dashboard, plus a first pass at real deployment (Docker Compose for
a fresh Ubuntu 24.04 droplet). Function over polish throughout: plain
server-rendered HTML via Jinja2, no frontend build step.

### Added

- `polska/dashboard/`: a FastAPI app with session-cookie login (Argon2 password
  hash, per-IP rate limiting on failed attempts, HttpOnly + SameSite=Lax
  cookies, a CSRF synchronizer token required on every state-changing form),
  and nine routes covering: the goals/activity-feed/runs overview; approving or
  rejecting a pending `Approval` (showing its stored `preview`, executing its
  stored `payload` exactly, never re-planning); clearing a `BudgetHalt`, with
  the run that caused it shown, not just its reason text; writing off an
  `ORPHANED` run.
- `gate.decide_approval`: records a human's approve/reject decision, separately
  from `execute_approval` actually running it — the dashboard's approve route
  calls both, in that order. Refuses a decision on anything but a still-`PENDING`
  approval, and is the first thing that actually moves a `PENDING` approval to
  `EXPIRED` once its window has passed (nothing else in the system swept for
  that before).
- `budget.clear_halt`: the one function that resumes a company after a halt,
  writing a `BUDGET_RESUMED` activity event. Nothing in `BudgetGuard` calls
  this itself, by design.
- `BudgetHalt.run_id` (migration `1dd12b8e308b`): the specific run that tripped
  a RUN-scoped halt, so the dashboard can show it directly instead of parsing
  it out of the reason text.
- `polska/cli.py`: `init-auth` (prompts for a password via `getpass`, never a
  CLI argument or a value anyone else picks; prints the Argon2 hash and a
  fresh session secret to paste into `.env`) and `tick [slug]` (runs one
  planning/dispatch cycle immediately, for one or every company — the manual
  override for the scheduler's own 24-hour interval, which deliberately never
  fires on its own the moment a fresh container boots).
- `Dockerfile` / `docker-compose.yml` / `.dockerignore`: one image, two
  services (`scheduler`, `dashboard`) sharing the SQLite database and company
  profiles through bind mounts. Neither container runs migrations or seeds
  auth on its own — both stay explicit, separate steps, the same rule
  `main.py` already applied to migrations.
- README `## Deployment`: the full fresh-Ubuntu-24.04 walkthrough, and an
  explicit note that the dashboard is temporarily, deliberately public
  (`0.0.0.0`, no proxy) while `force_dry_run` stays true, with the exact two
  ways to close that (Cloudflare Access, or `POLSKA_DASHBOARD_HOST=127.0.0.1`
  behind an SSH tunnel) before any adapter can perform a real effect.

### Fixed

- README claimed the Docker image would need a separate Node.js install
  alongside Python for the Claude Agent SDK's CLI. Checked against PyPI:
  `claude-agent-sdk` publishes a platform-specific wheel per platform
  (including `manylinux_2_17_x86_64`), each bundling its own CLI binary, so a
  plain `pip install` inside a glibc-based Linux image resolves it with no
  Node/npm step at all.

### Note

Not run against a real Docker installation: none exists in the environment
this was built in. Reviewed carefully against the SDK's published wheels
instead of run end to end; the first real `docker compose up` on the actual
droplet is the real test.

Four fixes from reviewing the single-source spread test's results.

### Changed

- Analyst's `max_tokens_per_run` raised from 600,000 to 900,000: two of five
  single-source tasks hit 600k on legitimate work, not runaway exploration.
  Catching 40% of normal tasks meant the ceiling was wrong, not the tasks.
  `max_usd_per_run` stays at $1.00 as the real backstop, since tokens bind
  first for this cache-heavy agent.
- CarScratch's `upstream-monitoring` goal target changed from 6 to 5.
  `chrystals.ts` makes no network call (a pre-generated JSON file, not a live
  source) and its staleness risk is already `auction-data-freshness`'s job;
  the reason is now in the profile's own description so nobody re-adds it.

### Fixed

- Dedup's fuzzy match key now includes each task's description, not just its
  title. Found live: a new "MOT" proposal scored 96 against a completed
  "DVLA" task on title alone (both followed the same "Document X's
  silent-failure mode..." template) and was auto-dropped without the judge
  ever seeing it. Their descriptions, which actually name the different files
  and APIs, score 53 against each other; folding them in moves a case like
  that into the ambiguous band instead of confidently and silently dropping
  it. Chosen over lowering `high_threshold`: a 96 score needs the threshold
  dropped drastically to catch, which would send far more genuinely-duplicate
  proposals to the judge too, not just templated-title collisions.
- `_build_worker_prompt` now includes the goal's own description, not just its
  title and progress numbers. Found live: an analyst task burned real tokens
  hunting a repo-only workspace for another goal's definition, which
  structurally cannot exist there — a goal lives in the database and the
  company profile, neither visible to an agent whose workspace is a read-only
  repo clone. Any agent working toward a goal now gets that goal's text
  handed to it at dispatch, since it has no other way to ever see it.

## [0.3.7] - 2026-09-28

Found running the single-source spread test: the mid-stream watchdog can close
the stream on the exact same message that carried the model's own valid,
complete answer, throwing away real finished work and wrongly abandoning a
task that had, in fact, succeeded. Confirmed on a real run (MOT's silent-failure
audit): the CLI's internal `StructuredOutput` tool call, sitting right there in
`tools_called`, validated cleanly against `AgentResult` after the fact, but
nothing was looking at it.

### Fixed

- `AgentRunner._recover_structured_output`: when a run ends with no
  `ResultMessage` (`INTERRUPTED` or `TIMED_OUT`), scans `tools_called`
  backwards for the CLI's own terminal output tool call and validates it
  against the task's schema. The `Run` row still honestly records
  `INTERRUPTED`/`TIMED_OUT` (the SDK call really was cut off, and any
  `BudgetHalt` it wrote still stands), but `run_worker` decides a task's fate
  from the validated output, not from `run.status`, so a task that actually
  finished is no longer thrown away and abandoned for it.
- Task #5 (CarScratch, MOT audit) manually corrected: its run had already
  produced a valid answer before today's fix existed. Left `abandoned` (a
  terminal state, not reopened) with the recovered answer annotated onto its
  result for anyone reviewing it.

## [0.3.6] - 2026-09-28

### Changed

- Analyst's `max_usd_per_run` reverted from $0.75 back to $1.00 on review: one
  completed run (DVLA, the simplest of the six upstream sources) is a floor, not
  a range. gov.im's scraper alone is ~2.5x DVLA's line count with real regex
  parsing to read, and could plausibly cost double — headroom stays until three
  or four sources have actually completed.

## [0.3.5] - 2026-09-27

First clean, real, end-to-end task completion on CarScratch. A hand-written,
single-source version of the task that twice blew its ceiling at broad scope
("map the 6 upstream data sources") completed on the first real attempt once an
unrelated leftover halt was cleared: 300,033 tokens, $0.22569, a genuine
verifiable proposal (a structured log line for DVLA's silent-failure mode).

### Changed

- Analyst's `max_usd_per_run` tightened from $1.00 to $0.75, using the real rate
  this completion measured ($0.22569 / 300,033 tokens = $0.7522/M) projected out
  to the existing 600,000-token ceiling (~$0.45), rounded up for margin against a
  pricier, less-cached source among the other five. `max_tokens_per_run` is left
  at 600,000: real headroom (~2x this completion) that the other five sources
  have not yet tested.
- `tests/test_budget_guard.py`'s `tight_config` fixture now clears every agent's
  own per-run override, not just the global figures: several of its tests were
  silently relying on no agent having one, which broke the moment analyst got a
  real override earlier today.

## [0.3.4] - 2026-09-27

Found rerunning today's tripled analyst ceiling and still hitting it: a task
that fails because it genuinely crossed its own run ceiling was coming back as
`failed` and getting requeued at identical scope by the ordinary retry path,
burning budget on repeat attempts that were always going to fail the same way,
before `max_attempts` finally caught up with it. The planner itself noticed this
exact problem independently in the same tick and recommended holding for
narrower scoping — this fix makes the mechanical retry path agree with it.

### Fixed

- `AgentRunner.fail_or_abandon` takes an optional `run_status`. A task whose run
  ended `RunStatus.INTERRUPTED` is now abandoned immediately, regardless of
  attempts or cumulative cost remaining, with `needs_rescoping: true` in its
  result — never requeued unchanged. Deliberately does *not* extend this to
  `RunStatus.BUDGET_BLOCKED`: that status also covers a run blocked by an
  unrelated, already-open company halt, which says nothing about whether this
  particular task is too big, and treating the two alike would abandon a
  perfectly reasonable task for someone else's overshoot.

## [0.3.3] - 2026-09-27

Per-agent budget ceilings, sized from real measured runs rather than guessed. The
first live rerun with credit restored also confirmed the mid-run watchdog live: it
correctly cut off a real analyst run at 223,194 tokens, and correctly left a real
planner run alone at 58,543 tokens in the same tick.

### Added

- `AgentConfig.max_usd_per_run` / `max_tokens_per_run`, both `None` by default
  ("no override, use the global figure" — a deliberate choice for an agent nobody
  has real usage data for yet, not a guessed number).
- `AppConfig.max_usd_per_run_for(agent_name)` / `max_tokens_per_run_for(agent_name)`:
  the one place every enforcement path (the reservation, the SDK's own
  `max_budget_usd`, the mid-stream watchdog, the pre-dispatch check, the post-hoc
  overshoot check, and orphan write-off pricing) resolves an agent's effective
  ceiling, so none of them can disagree about which figure applies to a run.
- A validator: no agent's `max_usd_per_run` override may exceed
  `budget.max_usd_per_task`, mirroring the existing check on the global figure.
- Real overrides in `config/default.yaml`: planner tightened to $0.25 / 100,000
  tokens (observed: a real successful run used 58,543 tokens, $0.10826); analyst
  loosened to $1.00 / 600,000 tokens (observed: a real run was still climbing when
  cut off at 223,194 tokens, $0.24909 — the binding constraint is tokens, not
  dollars, because caching makes tokens cheap); engineer loosened to $2.00 /
  750,000 tokens (no engineer run completed today, so sized from the previous dry
  run's real 351k/471k-token, $0.644/$0.552 runs instead). Dedup judge, marketer
  and support get no override: no real usage data for any of them yet.
- `scripts/observe_ticks.py --max-concurrent N` and `--stop-if-over USD`: override
  `limits.max_concurrent_tasks` for one invocation without touching the config
  file, and stop before starting another tick once cumulative spend has been
  exceeded, so a session with a real dollar cap can't blow through it unattended.

### Fixed

- `BudgetGuard.reserve` reserved a flat `max_usd_per_run` regardless of which
  agent was calling; it now takes `agent_name` and resolves that agent's own
  figure. `check_run_did_not_overshoot` and `reconcile_orphaned_runs` priced every
  run against the same flat global figure regardless of agent; both now resolve
  per run.

## [0.3.2] - 2026-09-27

The budget guard was bookkeeping, not control: `max_budget_usd` reached the SDK but
never actually stopped an observed overrun (one was caught by `max_turns`, two by the
Anthropic account running out of credit externally), and there was no mid-run check
on token spend at all, only a post-hoc record after the run had already finished.
Fixed with an active mid-stream watchdog and a pre-dispatch input-size refusal. A
verification rerun against CarScratch is blocked, separately, by the account having
no credit right now.

### Added

- `RunStatus.INTERRUPTED`: Polska cut a run off itself, mid-stream, after it crossed
  its own ceiling. Distinct from `BUDGET_BLOCKED` (never dispatched at all) and from
  `FAILED` (the CLI ended on its own terms) so the record never conflates "we refused
  to start" with "we started and then stopped it ourselves."
- `AgentRunner._RunningUsage`: accumulates real usage from every `AssistantMessage`
  as a run streams in (per-turn, not cumulative), and can price itself against the
  same pricing table used everywhere else.
- `AgentRunner._check_input_size`: estimates the system prompt, task prompt and
  `--json-schema` payload before a call is ever dispatched (a deliberately
  pessimistic 3 chars/token heuristic, no network round-trip), and refuses to call
  the SDK at all as `BUDGET_BLOCKED` if that alone would breach the run's ceiling.
- Two tests against the fake SDK: one proves the mid-stream watchdog closes the
  stream and stops pulling further messages the instant the ceiling is crossed; the
  other proves the fake `query_fn` is never invoked when the pre-dispatch check
  refuses.
- A third test proving the negative case Javier asked for explicitly: a run whose
  accumulated usage lands exactly ON the token ceiling (not over it), then finishes
  normally, must pull every message, succeed, and write no watchdog halt. A guard
  that fires when it shouldn't is as bad as one that doesn't fire.

### Changed

- `_execute_and_record` now holds an explicit stream handle (`stream =
  self._query_fn(...)`) instead of iterating a bare `async for`, so it can call
  `await stream.aclose()` on crossing the ceiling — the SDK's own documented
  mechanism for a caller to stop a stream early, already used internally by
  `client.py`'s own cleanup path.
- A run that ends without ever receiving a `ResultMessage` (interrupted, timed out,
  or errored) now records its real accumulated usage from `_RunningUsage`, instead
  of the zeroed figure `_extract_usage(None, ...)` previously gave it.

## [0.3.1] - 2026-09-25

The first real dry run, against CarScratch: constraints from an actual repo audit,
a per-task read-only repo clone, a settings-isolation fix, and a real overshoot
bug found and fixed by that same dry run.

### Added

- CarScratch profile: 13 hard constraints from a direct audit of the CarScratch
  repo (never touch the gov.im or CheckCarDetails scraping paths, never raise
  scrape rate, never claim data accuracy, no analytics, master is protected,
  no mock-as-real, no inference-as-observation, verifiable artefacts only, no
  re-proposing what a human stopped, Buffer/Instagram limits, no em-dashes).
  `social-cadence` goal dropped per instruction (lowest-value work, not wanted
  yet); replaced with `upstream-monitoring` (detect the site's six silently-
  failing data sources before a user reports it). `known-issues-audit` tightened
  to name its own pass criteria (an explicit checklist against real files, not
  an assertion), since as first written an agent could pass it by doing nothing.
- `polska/workspace.py`: `prepare_task_workspace` clones a company's repo at its
  configured `working_branch` into `<task>/repo/`, for engineering *and* research
  tasks (the analyst reads real code as much as the engineer does). The
  credential is read once from the profile's `secret_env`, passed to exactly one
  `git clone` subprocess call as a transient `GIT_CONFIG_KEY_0` override (verified
  directly: leaves no trace in the resulting `.git/config`), and the clone's
  `.git` directory is deleted regardless as a second layer. The tree is then made
  read-only and stripped of dependency lockfiles and binary assets (see Fixed).
  `AgentRunner.fail_or_abandon` (renamed from a private method so this module can
  call it) applies the same attempts/cost exhaustion logic to a failed clone as
  to a failed agent run, so a persistently broken profile setting still abandons
  rather than looping forever.
- `AgentConfig.effort`, set to `low` for the planner (a classification-shaped
  decision, not open-ended reasoning). Not set for the dedup judge: Haiku 4.5
  does not support the `effort` parameter at all and would 400 on it, unlike
  Sonnet/Opus tier.
- `scripts/observe_ticks.py`, promoted from a scratchpad one-off to a permanent
  dev tool: runs N real ticks against a named company profile and prints the
  activity feed, a per-run cost breakdown by agent, and every task's state and
  dedup note. Respects `integrations.force_dry_run` and warns if it is off.
- `scheduler.interval_hours` set to 24 (daily). Measured cost, not budget, was
  never the constraint on this; how often the business actually changes is.

### Fixed

- **Every agent call was silently loading Javier's personal Claude Code
  configuration.** `ClaudeAgentOptions.setting_sources` was never set, and its
  documented default is "all sources are loaded, matching CLI defaults":
  `~/.claude/settings.json`, any project `.claude/settings.json`, and CLAUDE.md.
  Measured directly on a single dedup judge call: 28k+ tokens of cache creation
  for a request whose actual content was under 1k tokens. Fixed with
  `setting_sources=[]` (the SDK's own name for "load nothing from disk"; unrelated
  to `allowed_tools`, which still governs which tools an agent may call). This was
  inflating every measured cost figure and, worse, meant every agent's behaviour
  could have been influenced by personal rules that have nothing to do with
  running a company.
- **A real run blew 5-10x past its own budget reservation before the guard's
  safety nets caught it**, found by the first CarScratch dry run: an analyst run
  hit its `max_turns` ceiling at 928k tokens (reservation: 200k) and an engineer
  run hit the SDK's own `max_budget_usd` at $3.15 against a $3.00 ceiling, having
  already used 2.03M tokens. Root cause, confirmed against the real repo: a
  478KB `package-lock.json` and a 124KB data file, both tracked in git, land in
  every clone with nothing stopping a broad read from pulling either in whole,
  repeatedly. `prepare_task_workspace` now strips lockfiles and binary assets
  from the clone outright, warns on anything else large enough to matter, and
  both worker prompts now say explicitly to Grep/Glob rather than read a large
  file wholesale. The budget guard's own enforcement worked exactly as designed
  once triggered (a `BudgetHalt` correctly blocked every subsequent reservation
  for the company until cleared); this fixes what caused the trigger, not the
  guard's response to it. **Partial, not complete**: a second measured dry run
  after this fix still saw two Opus-tier engineer runs at 351k and 471k tokens
  against the same 200k reservation, roughly half the first run's overshoot but
  still over. Both also ended on the account running out of credit mid-call
  rather than on the SDK's own ceiling, so how much of that figure is genuine
  task size versus an arbitrary cutoff point is not yet known. See the session
  notes on the wiki for the open per-agent-ceiling question this points at.
- `_force_rmtree`: a plain `shutil.rmtree(path, ignore_errors=True)` silently
  left `.git` behind on Windows, because git marks some of its own files
  (pack files) read-only and `ignore_errors=True` swallows the resulting
  `PermissionError` rather than fixing it. Caught by a test asserting `.git` was
  actually gone, not just attempted-to-be-gone. Fixed with an `onexc` handler
  that clears the read-only bit and retries; a directory that still can't be
  removed after that now raises `WorkspaceError` instead of proceeding with a
  workspace that still holds the remote URL.
- 11 new tests (`test_workspace.py`). 238 tests total.

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
