"""Gives the engineer agent something real to read, without ever handing it a
credential.

Before phase 4, an engineering task's workspace was an empty directory: nothing to
read, nothing to ground a proposed change in. This is the fix: if the company
profile has a ``github`` integration configured, clone that repo's working branch
into the task's workspace before the agent ever starts, then strip everything that
would let the agent reach the credential or write back to the real code.

The credential never touches disk and never reaches the agent's own process:

- It is read once, here, from the environment variable the profile's
  ``secret_env`` names (the same convention every adapter follows).
- It is passed to exactly one ``git clone`` subprocess call as a transient
  environment variable (``GIT_CONFIG_KEY_0`` / ``GIT_CONFIG_VALUE_0``), which git
  treats as a command-line-equivalent override. Verified directly: a clone done
  this way leaves no trace of the header in the resulting ``.git/config``.
- The clone's ``.git`` directory is deleted immediately afterwards regardless, so
  even a mechanism this project did not anticipate (a future git version, a
  different credential helper) has nothing left to find.
- All of this happens in Polska's own process, before ``AgentRunner.run_worker``
  is ever called. The agent's subprocess is started with its own environment,
  built fresh by the SDK; the dict holding the header here is never passed to it
  and goes out of scope the moment the clone finishes.

The checked-out tree is then made read-only, "where possible" per the instruction
that asked for this: it is enforced properly on the Linux deployment target, but
Windows' read-only attribute does not fully replicate POSIX write-protection on
directories, so treat the filesystem permission as defence in depth alongside the
engineer's own system prompt, not as the only thing standing between it and the
protected scraper paths named in a company's constraints.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import shutil
import stat
import subprocess
from base64 import b64encode
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from polska.config.company import CompanyProfile
from polska.db.enums import TaskState
from polska.db.models import Task
from polska.db.types import utcnow

logger = logging.getLogger("polska.workspace")

#: Name of the subdirectory the clone lands in, inside the task's own workspace.
#: Kept separate from the workspace root so the engineer still has somewhere
#: writable (the root) even though the clone itself is read-only.
REPO_DIRNAME = "repo"
#: The writable copy, alongside REPO_DIRNAME, for a task type that actually
#: edits and verifies something. See _ensure_writable_copy.
WORK_DIRNAME = "work"

_CLONE_TIMEOUT_SECONDS = 120
#: Covers both npm ci and its npm install fallback; installing a project's
#: full dependency tree is the slow part either way.
_NPM_INSTALL_TIMEOUT_SECONDS = 300

#: Dependency lockfiles: never useful to an agent doing product work, often huge
#: (CarScratch's own package-lock.json is 478KB, ~120k tokens on its own), and
#: exactly the kind of file a broad Read or Glob sweep finds and reads in full
#: with no signal that it was a waste. Removed unconditionally, regardless of size.
_LOCKFILE_NAMES = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Cargo.lock",
        "poetry.lock",
        "Gemfile.lock",
        "composer.lock",
    }
)

#: Binary/generated asset extensions: never text an agent needs to reason about
#: product logic, and some are large (images, fonts).
_BINARY_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".eot", ".pdf", ".zip"}
)

#: Above this, a text file is left in place but is exactly the shape of file that
#: caused the real overshoot this constant exists to prevent a repeat of: measured
#: directly, a single worker task's clone-and-read of two files under this size
#: (a 478KB lockfile plus a 124KB data file) contributed to runs of 900k-2M
#: tokens against a 200k reservation. This does not delete such files (a large
#: real data file may still be genuinely relevant); it exists so
#: ``prepare_task_workspace`` can log a warning naming them, so a cost anomaly
#: has an obvious first place to look rather than needing to be re-discovered.
_LARGE_FILE_WARNING_BYTES = 50_000


class WorkspaceError(Exception):
    """Raised when a repo clone was expected to succeed and did not."""


def prepare_task_workspace(
    company_profile: CompanyProfile,
    workspace_root: Path,
    task_id: int,
    *,
    needs_writable_copy: bool = False,
) -> Path:
    """Ensure ``workspace_root/<task_id>/`` exists, with a read-only clone of the
    company's repository at ``<task_id>/repo/`` if one is configured.

    ``needs_writable_copy`` additionally ensures a genuinely writable copy at
    ``<task_id>/work/`` (see ``_ensure_writable_copy``), for a task type that
    will actually edit and verify something -- pass ``True`` only for that
    case (engineering), never for one that only ever reads (research), which
    would just pay the copy's cost for a directory it structurally cannot use.

    Idempotent: called again for a retried task, an existing clone is left alone
    rather than re-fetched (a retry should see exactly what the first attempt saw
    to reason from, and re-cloning would be a second, unnecessary use of a
    credential this function is built to minimise the use of).

    Returns the task's own workspace directory (not the ``repo`` subdirectory):
    this is what becomes the agent's ``cwd``.
    """
    task_workspace = workspace_root / str(task_id)
    task_workspace.mkdir(parents=True, exist_ok=True)
    # Created by this process (the orchestrator, running as one user), but the
    # actual agent subprocess runs as a different, unprivileged user with no
    # network access (see runner.py's cli_path); a plain mkdir's default mode
    # leaves "other" with no write bit, which would make this directory --
    # the one place the agent is actually meant to write scratch files, a
    # proposed/ copy of a change, a diff -- unwritable to it. World-writable
    # is a deliberate simplification, not an oversight: this is ephemeral,
    # per-task scratch space, never a secret or the company's own source.
    task_workspace.chmod(0o777)

    github = company_profile.integrations.get("github")
    if github is None or not github.enabled:
        return task_workspace

    repo_slug = github.options.get("repo")
    if not repo_slug:
        return task_workspace

    repo_dir = task_workspace / REPO_DIRNAME
    if repo_dir.exists():
        # Correct not to re-clone (a retry should see exactly what the first
        # attempt saw, and re-cloning would be a second, unnecessary use of
        # the credential) -- but that used to be the *only* thing checked
        # here, which silently meant "and therefore skip everything else
        # workspace prep does too", including dependency vendoring added
        # long after some of these clones were made. A task whose workspace
        # already existed before that feature shipped got its lockfile
        # stripped, same as always, and then got the same missing-toolchain
        # experience forever, on every retry, indistinguishable from Node
        # never having been installed at all. Checking "does this clone
        # exist" was answering "did we do this before"; what actually needs
        # answering is "is what this task needs present now", which is what
        # _ensure_node_dependencies_vendored checks directly instead of
        # inferring.
        logger.info("Workspace for task %d already has a clone; leaving it as is.", task_id)
        _ensure_node_dependencies_vendored(repo_dir, task_id)
        if needs_writable_copy:
            _ensure_writable_copy(task_workspace, repo_dir, task_id)
        return task_workspace

    branch = github.options.get("working_branch") or github.options.get("default_branch") or "main"
    _clone_read_only(repo_slug, branch, repo_dir, github.secret_env, task_id)
    if needs_writable_copy:
        _ensure_writable_copy(task_workspace, repo_dir, task_id)
    return task_workspace


def reclaim_node_modules_for_terminal_tasks(
    session: Session, workspace_root: Path, *, grace_period: dt.timedelta
) -> int:
    """Delete ``node_modules`` for any task that has sat in a terminal state
    for at least ``grace_period``, keeping the rest of its workspace (the
    source clone, any ``proposed/`` diff an engineer wrote) intact for
    exactly as long as before. Returns how many workspaces were reclaimed.

    ``node_modules`` is ~95%+ of a vendored workspace's size (measured:
    393-428MB per task against a source clone of a few MB) and has zero
    diagnostic value once a task is done with it: it is vendored public
    packages, identical to what npm would fetch again, never anything
    task-specific. The rest of the workspace is the one part worth being
    able to look at after the fact, and this never touches it.

    Never touches ``failed``: that state is still retry-eligible, and a
    retry reusing the same clone (see ``prepare_task_workspace``'s own
    idempotency check) would need its dependencies back, paying for an
    install it did not need to pay for again if this had left it alone.

    Call once per tick cycle, not once per company: this is a query across
    every company's tasks, unrelated to any single one's tick.
    """
    cutoff = utcnow() - grace_period
    terminal_tasks = session.execute(
        select(Task).where(
            Task.state.in_((TaskState.DONE, TaskState.ABANDONED, TaskState.BLOCKED)),
            Task.finished_at < cutoff,
        )
    ).scalars()

    reclaimed = 0
    for task in terminal_tasks:
        node_modules = workspace_root / str(task.id) / REPO_DIRNAME / "node_modules"
        if not node_modules.exists():
            continue
        repo_dir = node_modules.parent
        try:
            was_locked = not os.access(repo_dir, os.W_OK)
            if was_locked:
                repo_dir.chmod(0o755)
            try:
                removed = _force_rmtree(node_modules)
            finally:
                if was_locked:
                    repo_dir.chmod(0o555)
        except OSError as exc:
            # One workspace this process cannot touch must not stop the rest.
            logger.warning("Could not reclaim node_modules for task %d: %s", task.id, exc)
            continue
        if not removed:
            logger.warning(
                "node_modules for terminal task %d is only partly removed: what is left is "
                "not deletable by this process (typically files a test run created as the "
                "agent user). Not counted as reclaimed; retried next cycle.",
                task.id,
            )
            continue
        reclaimed += 1
        logger.info("Reclaimed node_modules for terminal task %d.", task.id)
    return reclaimed


def _clone_read_only(
    repo_slug: str, branch: str, repo_dir: Path, secret_env_names: list[str], task_id: int
) -> None:
    token = _read_token(secret_env_names)

    clone_env = os.environ.copy()
    if token:
        # A one-shot config override for this single subprocess call only. Never
        # written to any file; see the module docstring for what was verified.
        clone_env["GIT_CONFIG_COUNT"] = "1"
        clone_env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
        clone_env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {_basic_auth(token)}"
    else:
        logger.warning(
            "No credential found for %s (looked for env var(s) %s); attempting an "
            "unauthenticated clone, which will fail for a private repository.",
            repo_slug,
            secret_env_names,
        )

    try:
        subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                branch,
                "--single-branch",
                f"https://github.com/{repo_slug}.git",
                str(repo_dir),
            ],
            check=True,
            capture_output=True,
            env=clone_env,
            timeout=_CLONE_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace") if exc.stderr else ""
        raise WorkspaceError(
            f"Could not clone {repo_slug}@{branch} for task {task_id}: {stderr[:500]}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError(
            f"Cloning {repo_slug}@{branch} for task {task_id} did not finish within "
            f"{_CLONE_TIMEOUT_SECONDS}s."
        ) from exc
    finally:
        # The only reference to the token anywhere in this process. Dropping the
        # dict that held it is not what makes this safe (the subprocess call
        # already completed by the time we get here) -- it just avoids the
        # temptation to reuse `clone_env` for anything else further down.
        del clone_env

    git_dir = repo_dir / ".git"
    if git_dir.exists():
        _force_rmtree(git_dir)
        if git_dir.exists():
            # This is the one thing that must never silently half-happen: the
            # whole point of deleting .git is that nothing is left holding the
            # remote URL. Fail loudly rather than proceed with a repo that still
            # has it.
            raise WorkspaceError(
                f"Could not remove {git_dir} after cloning {repo_slug}@{branch} for "
                f"task {task_id}; refusing to hand this workspace to an agent while "
                "its .git directory still exists."
            )

    _ensure_node_dependencies_vendored(repo_dir, task_id)
    _strip_low_value_files(repo_dir)
    _make_read_only(repo_dir)


#: Written into node_modules only once the install subprocess actually
#: returns success. Checking for this, not for node_modules itself,
#: distinguishes a finished install from a partial one left behind by a
#: process that died mid-install (killed, OOM'd, restarted): node_modules
#: existing was already corrected once, from standing in for "a workspace
#: was prepared before" (see this function's own docstring); this is the
#: same correction applied one level deeper, to "the install that made it
#: finished" rather than "something under this name exists".
_VENDORED_MARKER_NAME = ".polska-vendored"


def _ensure_node_dependencies_vendored(repo_dir: Path, task_id: int) -> None:
    """Install what a Node project's dependencies actually require to be
    present right now, checked directly rather than inferred from whether
    this looks like the first time a workspace has been prepared.

    Keyed on whether ``node_modules/.polska-vendored`` exists, nothing else:
    a clone made before this feature existed used to never get this call at
    all (see ``prepare_task_workspace``'s own early return for an existing
    clone), which meant "was a workspace already prepared" was silently
    standing in for "does this workspace have what it needs", and a
    workspace could satisfy the first without ever satisfying the second.
    Checking the real condition means this runs, and self-heals, for an old
    clone exactly the same way it does for a brand new one -- and the same
    correction applies to ``node_modules`` existing at all: that alone means
    "something was written here", not "the install that wrote it finished",
    which a container killed mid-install would leave permanently
    indistinguishable from a real one without the marker.

    Runs here, in Polska's own process, before the agent ever starts, on
    either a brand new clone or a years-old one -- the same "controlled step
    outside the agent's own execution" the credential injection above already
    relies on. This is the one legitimate place a real network call to fetch
    a dependency happens; the agent's own subprocess, run under a separate,
    network-restricted user, never reaches the registry itself.

    Skipped entirely when there is no ``package.json``: not every company's
    repository is a Node project, and running an npm command against one that
    isn't is an error, not a no-op.
    """
    node_modules = repo_dir / "node_modules"
    marker = node_modules / _VENDORED_MARKER_NAME
    if marker.exists():
        return

    if not (repo_dir / "package.json").exists():
        return

    # repo_dir may already be locked read-only: an old clone _make_read_only
    # already ran against, in a process that predated this function knowing
    # to look for it. Both removing a partial node_modules and creating a
    # fresh one need write access to repo_dir itself, so this is computed
    # and acted on once, up front, covering either path below.
    was_locked = not os.access(repo_dir, os.W_OK)
    if was_locked:
        repo_dir.chmod(0o755)

    if node_modules.exists():
        # No marker: either an install from before the marker existed, or
        # one a process died in the middle of. Either way, node_modules
        # existing is not the same claim as the marker existing, so this is
        # not known-complete -- remove it and reinstall from scratch rather
        # than layer a fresh install on top of an unknown partial one, which
        # is exactly the "fails in a confusing way" a task working against
        # missing files would produce.
        logger.warning(
            "Task %d's node_modules exists without a completion marker (a "
            "partial install, or one that predates the marker); removing it "
            "and reinstalling from scratch rather than trust it.",
            task_id,
        )
        if not _force_rmtree(node_modules):
            if was_locked:
                repo_dir.chmod(0o555)
            raise WorkspaceError(
                f"Could not remove task {task_id}'s partial node_modules to reinstall it; "
                "refusing to layer a fresh install on top of an unknown partial one."
            )

    lockfile = repo_dir / "package-lock.json"
    if lockfile.exists():
        # The common case, a clone made after this existed: deterministic
        # against exactly what's committed, and refuses outright if the
        # lockfile and package.json have drifted, rather than silently
        # resolving something slightly different from what the repository's
        # own CI would install.
        command = ["npm", "ci", "--no-audit", "--no-fund"]
    else:
        # An old clone: _strip_low_value_files already removed its lockfile,
        # from long before this function existed to need it kept. Re-fetching
        # just that one file would mean using the git credential a second
        # time for a workspace already paid for once, which is exactly the
        # cost prepare_task_workspace's own idempotency check exists to
        # avoid. npm install resolves fresh from package.json instead, no
        # lockfile and no credential needed, at the honest cost of not being
        # pinned to the exact tree the original commit would have installed.
        logger.warning(
            "Task %d's clone predates dependency vendoring and its lockfile "
            "is already gone; resolving fresh with npm install instead of "
            "npm ci, since re-fetching the lockfile would mean a second use "
            "of the clone credential.",
            task_id,
        )
        command = ["npm", "install", "--no-audit", "--no-fund"]

    try:
        subprocess.run(
            command,
            cwd=repo_dir,
            check=True,
            capture_output=True,
            timeout=_NPM_INSTALL_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace") if exc.stderr else ""
        raise WorkspaceError(
            f"{command[1]} failed for task {task_id}'s workspace: {stderr[:500]}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError(
            f"{command[1]} for task {task_id}'s workspace did not finish within "
            f"{_NPM_INSTALL_TIMEOUT_SECONDS}s."
        ) from exc
    finally:
        if was_locked:
            repo_dir.chmod(0o555)

    if not node_modules.exists():
        return

    # Written only once subprocess.run above has actually returned success:
    # an exception from either except branch above returns out of this
    # function entirely, so the marker's presence is a real claim, "an
    # install finished here", not "an install was attempted here".
    marker.write_text(f"{command[1]}\n", encoding="utf-8")

    # node_modules is vendored, not source: nothing under it needs the same
    # write protection as the repository the engineer must not edit, and a
    # test runner may need to write its own cache or temp output somewhere
    # inside it. World-writable is a deliberate simplification, not an
    # oversight: this directory holds public npm packages, never a secret or
    # a line of the company's own code, and the agent subprocess runs as a
    # different UID (see runner.py) with no group relationship to this one
    # worth setting up just to avoid it. The marker file just written above
    # is caught by this same loop, since it lives inside node_modules too.
    node_modules.chmod(0o777)
    for root, dirs, files in os.walk(node_modules):
        for name in dirs:
            (Path(root) / name).chmod(0o777)
        for name in files:
            (Path(root) / name).chmod(0o666)


#: Written into work/ only once the copy has genuinely finished. Same
#: reasoning as _VENDORED_MARKER_NAME: "work/ exists" is not the same claim
#: as "the copy that made it finished", and a container dying mid-copy would
#: otherwise leave a half-written tree permanently indistinguishable from a
#: real one.
_WORK_READY_MARKER_NAME = ".polska-work-ready"


def _ensure_writable_copy(task_workspace: Path, repo_dir: Path, task_id: int) -> None:
    """Ensure ``<task_workspace>/work/`` exists: a genuinely writable copy of
    the read-only ``repo/`` clone, source only, with ``node_modules``
    symlinked in rather than duplicated.

    Found live: without this, the engineer invents the same arrangement
    itself, mid-run, at real token cost, the moment it discovers ``repo/``
    cannot be written to and a test runner needs somewhere it can write (a
    cache, a snapshot). This does it once, in Polska's own process, before
    the agent ever starts, and the agent is told where to work in its own
    system prompt rather than left to work it out.

    Checked against a completion marker inside ``work/``, not against
    whether the directory exists, for the identical reason
    ``_ensure_node_dependencies_vendored`` is: existence alone is "something
    was written here", not "the thing that wrote it finished".

    ``node_modules`` is symlinked, never copied: it is already vendored once
    at real disk cost (see that same function), and duplicating several
    hundred MB a second time for a directory that never needs editing would
    undo exactly the space discipline the reclaim policy exists for.

    A no-op if ``repo_dir`` itself does not exist: no repository was
    configured for this company at all, so there is nothing to copy.
    """
    if not repo_dir.exists():
        return

    work_dir = task_workspace / WORK_DIRNAME
    marker = work_dir / _WORK_READY_MARKER_NAME
    if marker.exists():
        return

    if work_dir.exists():
        logger.warning(
            "Task %d's work/ exists without a completion marker (a partial "
            "copy, or one that predates the marker); removing it and "
            "recreating it from scratch rather than trust it.",
            task_id,
        )
        if not _force_rmtree(work_dir):
            raise WorkspaceError(
                f"Could not remove task {task_id}'s partial work/ copy to recreate it; "
                "refusing to copy on top of an unknown partial one."
            )

    shutil.copytree(repo_dir, work_dir, ignore=shutil.ignore_patterns("node_modules"))

    node_modules_source = repo_dir / "node_modules"
    if node_modules_source.exists():
        (work_dir / "node_modules").symlink_to(node_modules_source, target_is_directory=True)

    # copytree preserves the source's own mode bits, which for a repo_dir
    # already locked read-only (see _make_read_only) means no write bit for
    # "other" at all -- exactly the thing this directory exists not to be.
    # Same world-writable simplification as node_modules gets, for the same
    # reason: this is the agent's own scratch copy, never a secret.
    work_dir.chmod(0o777)
    for root, dirs, files in os.walk(work_dir):
        if "node_modules" in dirs:
            dirs.remove("node_modules")  # a symlink, not a real tree to chmod
        for name in dirs:
            (Path(root) / name).chmod(0o777)
        for name in files:
            (Path(root) / name).chmod(0o666)

    marker.write_text("ready\n", encoding="utf-8")


def _strip_low_value_files(repo_dir: Path) -> None:
    """Remove lockfiles and binary assets, and warn about anything else large.

    Called before ``_make_read_only``, since these are real deletions and need
    write access to do. Skips ``node_modules`` entirely: vendored dependencies
    are not the low-value noise this exists to trim (see
    ``_ensure_node_dependencies_vendored``), and a binary asset a package
    genuinely needs at runtime, an icon, a compiled native addon, must
    survive here.
    """
    removed_bytes = 0
    for path in list(repo_dir.rglob("*")):
        if "node_modules" in path.parts:
            continue
        if not path.is_file():
            continue
        if path.name in _LOCKFILE_NAMES or path.suffix.lower() in _BINARY_EXTENSIONS:
            removed_bytes += path.stat().st_size
            path.unlink()
            continue
        size = path.stat().st_size
        if size > _LARGE_FILE_WARNING_BYTES:
            logger.warning(
                "%s is %d bytes (~%d tokens): left in place, but this is the exact "
                "shape of file that caused a real run to blow well past its token "
                "reservation. Worth checking an agent's own instructions if cost "
                "looks wrong on tasks touching it.",
                path.relative_to(repo_dir),
                size,
                size // 4,
            )
    if removed_bytes:
        logger.info("Stripped %d bytes of lockfiles/binaries from the clone.", removed_bytes)


def _read_token(secret_env_names: list[str]) -> str | None:
    for name in secret_env_names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _force_rmtree(path: Path) -> bool:
    """Delete a tree even when some of it is read-only. Returns True only if
    ``path`` is actually gone afterwards; never raises for a permission problem.

    git marks some of its own files (pack files in particular) read-only on
    Windows, and a plain ``shutil.rmtree`` fails to delete them; passing
    ``ignore_errors=True`` "fixes" that by silently leaving them behind, which is
    exactly wrong for a directory whose entire purpose is to not exist afterwards.
    So: on an error, add the owner write bit and retry, then report whether the
    tree really went. Callers decide what a tree that would not go means.

    A file owned by another user (a test runner's cache, written as the agent
    UID into a tree this process owns) cannot be fixed from here: it stays, and
    the False return says so, rather than the old behaviour of either raising
    and killing the whole tick or pretending it worked.
    """
    root = Path(path)

    def _add_owner_bits(target: Path, bits: int) -> None:
        # Add, never replace: chmod(dir, S_IWRITE) sets the mode to exactly 0o200,
        # which strips read and execute and leaves a directory nothing can enter.
        os.chmod(target, stat.S_IMODE(target.lstat().st_mode) | bits)

    def _on_error(func, target_path, exc_info):  # noqa: ANN001 - shutil's onexc signature
        target = Path(target_path)
        parent = target.parent
        try:
            # On POSIX it is the parent directory's write bit that blocks unlink and
            # rmdir; on Windows it is the file's own read-only attribute. Only ever
            # touch directories inside the tree being deleted.
            if parent == root or root in parent.parents:
                _add_owner_bits(parent, stat.S_IWUSR | stat.S_IXUSR)
            if not target.is_symlink():
                _add_owner_bits(target, stat.S_IWUSR)
            func(target_path)
        except OSError:
            pass

    shutil.rmtree(root, onexc=_on_error)
    return not root.exists()


def _basic_auth(token: str) -> str:
    """git's http.extraHeader wants a ready-made Authorization value. A GitHub
    PAT as the basic-auth password with any non-empty username is what GitHub's
    own HTTPS-clone-with-a-token documentation uses."""
    return b64encode(f"x-access-token:{token}".encode()).decode()


def _make_read_only(path: Path) -> None:
    """Best-effort write-protection for the whole tree. See the module docstring:
    this is complete on the Linux deployment target and partial on Windows.

    Never descends into ``node_modules``: see
    ``_ensure_node_dependencies_vendored``, which deliberately leaves it
    writable so a test runner has somewhere to put its own cache or temp
    output, and skips its chmod here entirely rather than lock it down and
    immediately contradict that.
    """
    for root, dirs, files in os.walk(path):
        if "node_modules" in dirs:
            dirs.remove("node_modules")
        for name in files:
            (Path(root) / name).chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        for name in dirs:
            # Directories need the execute bit to be traversable/listable at all.
            (Path(root) / name).chmod(
                stat.S_IRUSR
                | stat.S_IXUSR
                | stat.S_IRGRP
                | stat.S_IXGRP
                | stat.S_IROTH
                | stat.S_IXOTH
            )
    path.chmod(
        stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
    )
