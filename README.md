# Polska

An autonomous company operator. An orchestrator decides what a business needs next,
then dispatches agents to do it on a schedule, without anyone driving each step.

Phases 1 through 4 of 4 are built: schema, models, migrations, the agent runner,
cost accounting, the orchestrator loop and scheduler, the approval gate, and the
dashboard. It has now run for real against a live key, against the CarScratch
profile: see the dry run notes under Tests, and the real bugs that run found and
this fixed. Every automated test still runs against a fake SDK stream, by design.
Deployment (Docker Compose, on a real droplet) is reviewed but not yet run for real;
see Deployment below.

## Build status

| Phase | Scope | State |
|-------|-------|-------|
| 1 | Schema, models, migrations | Done |
| 2 | Agent runner, dry-run adapter, cost accounting | Done |
| 3 | Orchestrator loop and scheduler | Done |
| 4 | Approval gate and dashboard (login, approvals, budget halts, orphan write-off, goals/activity/runs) | Done |

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
Agent SDK. Phase 3 adds APScheduler; phase 4 adds FastAPI, Jinja2 and Argon2 for the
dashboard, all plain server-rendered HTML, no frontend build step.

Note on the Agent SDK: it shells out to the Claude Code CLI, which the package
bundles as a platform-specific binary rather than requiring a separate Node install —
confirmed on PyPI: `claude-agent-sdk` publishes distinct wheels per platform
(`manylinux_2_17_x86_64`/`_aarch64`, `win_amd64`, `macosx_11_0_arm64`), each with its
own bundled CLI binary, so a plain `pip install` inside a glibc-based Linux image
(not Alpine/musl) resolves the right one on its own; no Node/npm install step needed
in the Dockerfile. The runner reads its exact API from the installed package rather
than from training-time recall: several fields on `ClaudeAgentOptions`
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
Dockerfile                One image for both containers (see docker-compose.yml).
docker-compose.yml        scheduler + dashboard, sharing data/ and companies/.
src/polska/
  config/                Env settings, config schema, company profile loader.
  db/                    Engine, models, enums, the task state machine.
  schemas/               Pydantic contracts for every agent output.
  adapters/              The adapter interface, the dry-run implementation, registry.
  dashboard/             The FastAPI dashboard: app, routes, auth, templates.
  activity.py            The one place that writes to the activity feed.
  budget.py              The budget guard: reserve before a run, release after.
  cli.py                 Operator commands: init-auth, tick.
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

## Deployment

Two containers from one image (`Dockerfile`, `docker-compose.yml`): `scheduler` runs
`polska.main` (the orchestrator loop), `dashboard` runs `polska.dashboard.server`
(the approval/budget UI). They share the SQLite database and company profiles
through bind mounts and nothing else.

**Current security posture, temporary and deliberate:** the dashboard binds to
`0.0.0.0` and is reachable from the open internet on `POLSKA_DASHBOARD_PORT`
(default 8000), with only a rate-limited, Argon2-hashed single admin password in
front of it. This is acceptable *only* while `integrations.force_dry_run` stays
`true`, i.e. before any adapter can perform a real external effect. **Before that
changes**, either put this behind
[Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/policies/access/)
(or an equivalent identity-aware proxy) or set `POLSKA_DASHBOARD_HOST=127.0.0.1` and
reach it over an SSH tunnel instead. Both are one env var and a restart; neither is
done automatically, because that decision has consequences (losing remote access
entirely, in the tunnel case) that should never happen without someone meaning it.

I have not run this Dockerfile/compose stack myself: there is no Docker in the
environment I built it in. What follows is built and reviewed carefully against the
Claude Agent SDK's own published wheels (confirmed on PyPI: a `manylinux_2_17_x86_64`
wheel exists, with a bundled Linux `claude` binary resolved by a fresh `pip install`
inside the image), not run end to end on real Ubuntu hardware. Treat the first real
`docker compose up` as the actual test, and tell me what breaks.

### Fresh Ubuntu 24.04 box, assuming nothing installed but the OS

1. **Install Docker Engine and the Compose plugin** (not Docker Desktop, which
   Ubuntu server doesn't need):
   ```
   curl -fsSL https://get.docker.com | sudo sh
   sudo usermod -aG docker $USER   # log out and back in after this
   docker compose version          # confirms the plugin is present
   ```

2. **Clone the repo and prepare local state:**
   ```
   git clone <this repo's URL> polska && cd polska
   mkdir -p data db
   sudo chown -R 1000:1000 data db
   sudo chmod 700 db
   cp .env.example .env
   ```
   The container runs as a non-root user, fixed at UID/GID 1000 in the
   `Dockerfile` (the Claude Code CLI refuses `--dangerously-skip-permissions`,
   what `permission_mode="bypassPermissions"` becomes at the CLI level, for
   root — see `runner.py`). `data/` and `db/` are bind-mounted, so *their*
   write access is decided entirely by this host-side ownership, not anything
   set inside the image; skip the `chown` and the scheduler's very first write
   to the database fails with a permission error instead of a missing-file
   one.

   `db/` is deliberately its own mount, separate from `data/`, and `chmod 700`
   on top of the ownership: it holds the budget ledger and the approval queue,
   and the agent subprocess (a second, unprivileged UID, see "The agent's
   filesystem access" below) must have no path into it at all, not merely be
   denied by a permission it could still see. `data/` holds task workspaces,
   which that same UID does need to traverse into for its own task, so it
   stays at the ownership-only, more permissive default.

   `companies/` and `config/` don't need any of this: the app only ever reads
   them, and a plain `git clone` already leaves them world-readable.

3. **Put a real `ANTHROPIC_API_KEY` in `.env`.** Leave `POLSKA_ADMIN_PASSWORD_HASH`
   and `POLSKA_SESSION_SECRET` blank for now; the next steps generate them.

4. **Build the image and run the database migrations** (a deliberate, separate step;
   nothing here or in the app auto-migrates on boot, and both `polska.main` and
   `polska.dashboard.server` refuse to start at all against a database whose schema
   is behind head, rather than run degraded and fail later on whatever code path
   first touches the difference):
   ```
   docker compose build
   docker compose run --rm scheduler alembic upgrade head
   ```
   Copying a database forward from an older deployment? Run this migration step
   before anything else touches it, same as a fresh one.

5. **Generate the dashboard's admin credentials.** This is a command you run, not a
   value anyone hands you: it prompts for a password (never echoed, never taken as a
   command-line argument, never logged) and prints an Argon2 hash plus a fresh random
   session secret, **each already single-quoted**.
   ```
   docker compose run --rm scheduler python -m polska.cli init-auth
   ```
   Paste the two printed lines into `.env` exactly as printed, quotes included: an
   Argon2 hash contains literal `$` characters (`$argon2id$v=19$...`), and Compose's
   `.env` parsing treats an unquoted or double-quoted `$` as a variable reference,
   silently mangling the hash into whatever that (usually nonexistent) variable
   expands to. Single quotes make Compose take the value literally. Verify it landed
   correctly rather than assuming it did:
   ```
   docker compose run --rm scheduler env | grep POLSKA_ADMIN_PASSWORD_HASH
   ```
   and compare it character-for-character against what `init-auth` printed.

6. **Start both containers:**
   ```
   docker compose up -d
   docker compose logs -f
   ```
   The dashboard is now on `http://<droplet-ip>:8000/login`. The scheduler is running
   but, per `scheduler.run_on_start: false` and `IntervalTrigger`'s own default
   (`now + interval_hours`, computed fresh on every process start), it will not tick
   on its own until a full `interval_hours` has passed.

7. **Trigger the first tick yourself, on your own schedule, not the container's:**
   ```
   docker compose exec --user polska scheduler python -m polska.cli tick
   ```
   Omit the company slug to tick every active company, or pass one
   (`python -m polska.cli tick carscratch`) to run just it. This is the exact same
   `run_company_tick` call the scheduler makes on its own interval, run once, now,
   and it prints the same summary line `scripts/observe_ticks.py` does.

   `--user polska` matters here specifically because `exec` (unlike `run`) attaches
   to the already-running container directly rather than going through its
   entrypoint, so it defaults to root. The container's own main process never runs
   as root beyond the one entrypoint step that sets up the agent's network
   restriction (see "Running as non-root" below); this flag is what keeps an
   ad-hoc `exec` command the same way.

### Upgrading a deployment that predates the non-root container user

Every file under `data/` on the host — the database, any workspace clones —
was written by the container running as root, so it is owned by root on the
host. Rebuilding the image without fixing this leaves the new, non-root
container unable to write to any of it. Before pulling the fix:
```
docker compose down
sudo chown -R 1000:1000 data
docker compose build
docker compose up -d
```
Nothing under `companies/` or `config/` needs this: the app only reads them.

### Moving the database onto its own mount

The database used to live under `data/`, alongside task workspaces — which
meant the agent subprocess, needing to traverse `data/` for its own task,
also had a path to the database, and used it (see "The agent's filesystem
access" below). Fixed by giving the database its own bind mount, `db/`, that
the agent UID has no path into at all. Moving an existing database across:

```
docker compose down
git pull
mkdir -p db
sudo mv data/polska.db data/polska.db-wal data/polska.db-shm db/ 2>/dev/null || true
sudo chown -R 1000:1000 db
sudo chmod 700 db
docker compose build
docker compose up -d
```

`docker compose down` first, always: SQLite's WAL mode (which this project
runs in) keeps uncommitted state in `polska.db-wal` alongside the main file,
and moving one without the other, or moving either while something still
has it open, is exactly how you lose the tail of a database rather than all
of it, the kind of failure that looks fine until the next read of a page the
move corrupted. Both containers must be stopped and nothing else touches
`db/`, `-wal`, or `-shm` while they're not.

`|| true` on the move: the `-wal`/`-shm` sidecars only exist while something
was actually connected recently (SQLite removes them on a clean close in
some circumstances), so their absence isn't an error, only `polska.db`
itself missing would be.

Verify it landed rather than assuming it did:
```
ls -la db/
docker compose run --rm scheduler python <<'PYEOF'
from polska.config.settings import load_settings
from polska.db.base import make_engine

settings = load_settings()
print("resolved to:", settings.database_url)
engine = make_engine(settings.database_url)
with engine.connect() as conn:
    print("companies:", conn.exec_driver_sql("SELECT COUNT(*) FROM companies").scalar())
PYEOF
```
A real count, not zero and not an error, confirms the moved file is what
`Settings.database_url`'s new default actually opened. Skip `tick`, `--help`
or otherwise, for this: argparse exits before the command body ever runs, so
it would prove nothing about the database at all, and a real (non-`--help`)
tick would dispatch actual work, not something to do just to check a path.

### The agent's network restriction, and how to verify it before trusting it

"The agent has no network access" is enforced, not just stated in its system
prompt. Every Claude Agent SDK subprocess runs as a second, dedicated user
(`agent`, uid 1001, separate from `polska`, uid 1000, which runs the scheduler
and dashboard themselves) with an iptables rule dropping its egress to
everything except the Anthropic API. This exists so an engineer task reaching
for `npm install` (now that Node is in the image, see below) or anything else
that touches the network fails immediately and by design, not as a route to
the outside world.

Don't take that on trust. Before relying on it for anything real:
```
docker compose exec --user agent scheduler sh scripts/verify_agent_egress.sh
```
This proves, from the restricted UID's own perspective: an arbitrary host is
refused immediately (not timed out), the Anthropic API is still reachable,
and a DNS lookup for anything not already known is refused too. `--user agent`
matters — running this as any other user proves nothing about what the agent
itself can reach.

The container refuses to start, rather than run the agent unrestricted while
believing otherwise, in two distinct cases — check `docker compose logs
scheduler` to tell them apart:

- **The image itself predates this, or its build didn't finish.** Every build
  step this depends on (`setcap`, `iptables`, Node, the CLI wrapper) is
  smoke-checked at build time — a missing tool fails `docker compose build`
  loudly, by name, rather than at first use. A version marker baked into the
  image only if that check passed is what `entrypoint.sh` looks for at
  startup; a stale image from a build that failed partway through (this
  happened once already: `setcap` was missing, the build failed, and
  `docker compose up` happily started the *previous*, unenforced image
  without complaint) fails this check and refuses to start. Always check that
  `docker compose build` actually succeeded before `up`.
- **The scheduler container's own capabilities or kernel are the problem**:
  missing the `NET_ADMIN`/`NET_RAW` capabilities `docker-compose.yml` grants
  it, or a kernel that doesn't support the `iptables` `owner` match.

### The agent's filesystem access

The agent subprocess runs as its own UID (`agent`, 1001, see the network
section above), but a UID is not automatically a filesystem boundary: it
only restricts what the *kernel's* ownership and mode bits actually deny,
never what a prompt says. Checked directly rather than assumed, as of this
project's own investigation:

- **The database is no longer reachable.** It was, through `data/`, the same
  mount the agent needs for its own task workspace — confirmed live, an
  engineer task that had run out of other ideas read it directly. Moved to
  its own `db/` mount, `chmod 700`, that UID has no path into at all (see
  "Moving the database onto its own mount" above). Verified the same way the
  network restriction is: `test -r`/`test -w` run *as* the agent UID, not
  `stat` output reasoned about afterward.
  ```
  docker compose exec --user agent scheduler sh -c 'test -e /app/db && echo "reachable" || echo "no such path"'
  ```
  Expect `no such path`. If it prints `reachable`, the mount or its mode
  didn't land as intended, before anything else, check that.
- **Company profiles and Polska's own source are still world-readable.**
  Deliberately left alone: nothing secret lives in either (a company profile
  names an env var, never its value), and locking them down buys nothing.
- **Cross-task workspace access is still open, on purpose, for now.**
  `data/workspaces/<task id>/` is `chmod 0o777` (`workspace.py`) so the
  agent's own task can write to it, but there is one `agent` UID shared
  across every task, so that grant isn't scoped to "this task", it reaches
  every task's workspace, including another company's. The real fix needs
  either a UID per task or a mount namespace per task
  (`CAP_SYS_ADMIN`, a much larger grant than the network work needed);
  deferred deliberately, not overlooked, because the actual exposure is
  bounded: no secret reaches the agent's environment (fixed separately, see
  below), no network egress beyond the Anthropic API exists to send anything
  found to, and the database move above closes the one path that could have
  mattered most. What's left is one task's scratch work readable, and in
  principle writable, by another task's agent — recoverable, not a secret or
  a forged record.

  **Revisit this the moment `force_dry_run` goes off for any adapter.** The
  containment argument above rests entirely on an agent having nothing to
  *do* with what it finds beyond writing it into its own output. Once a real
  adapter can act on the world, that argument no longer holds, and this gap
  needs a real answer, not a deferred one.
- **The agent subprocess's own environment is minimal, separately from all
  of the above.** It used to inherit the dashboard's login secrets, unrelated
  to any file it could reach at all. Fixed in the `polska-agent-cli` wrapper
  itself (`env -i`, three variables survive: the API key, `PATH`, `HOME`),
  not by anything filesystem-shaped, since the two are genuinely different
  channels and closing one says nothing about the other.

### Node, and what the engineer/analyst agents can actually run

If a task's company repository has a `package-lock.json`, its dependencies
are installed with `npm ci` once, when the workspace is cloned — before the
agent ever starts, in the same controlled step that injects the git
credential. This is deliberate: it's the one legitimate place a real network
call to fetch something happens, and it happens outside the agent's own
execution, under the unrestricted `polska` user, never inside it. The agent
gets a working `node`/`npm`/`npx` and an already-populated `node_modules` to
run an existing test command against; it cannot install anything itself, by
the network restriction above, not by convention.

### What each container actually restarts on

Both services are `restart: unless-stopped`: a crash restarts them, a host reboot
restarts them, an explicit `docker compose stop` does not get silently undone. That
policy has nothing to do with whether a *tick* fires on restart, which is a
completely separate, application-level decision (step 7's `IntervalTrigger` point) —
restarting the scheduler container never fires an immediate tick on its own.

### Editing a company profile or a ceiling after deployment

`companies/` and `config/` are bind-mounted, not baked into the image: edit
`companies/carscratch.yaml` or `config/default.yaml` directly on the droplet and the
*next* tick picks it up (each tick reloads every profile from disk; `config/` is
read once at process start, so a config change needs
`docker compose restart scheduler dashboard` to take effect). No rebuild needed
either way, since neither is `COPY`'d into the image.

### The dashboard's login, specifically

- **Password hashing:** Argon2id, via `argon2-cffi`, generated and verified in
  `polska/dashboard/security.py`. Never a plaintext password stored anywhere.
- **Rate limiting:** 5 failed attempts per IP locks that IP out for 15 minutes,
  in-process memory (see the module docstring in `security.py` for what that
  means if this ever runs with more than one uvicorn worker — it currently
  does not).
- **Session cookie:** Starlette's `SessionMiddleware`, `HttpOnly` and
  `SameSite=Lax` always set; `Secure` is `POLSKA_DASHBOARD_COOKIE_SECURE`
  (default `false`, because this currently serves plain HTTP — flip it to
  `true` the moment a TLS-terminating proxy, Cloudflare or otherwise, sits in
  front of it, or logins will silently fail because the browser refuses to
  send the cookie back).
- **CSRF:** a synchronizer token, one per session, stored server-side and
  required as a hidden field on every state-changing form (login, logout,
  approve, reject, clear a halt, write off an orphan). A request missing it or
  carrying the wrong one gets a 403 before touching anything.

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
