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

import logging
import os
import shutil
import stat
import subprocess
from base64 import b64encode
from pathlib import Path

from polska.config.company import CompanyProfile

logger = logging.getLogger("polska.workspace")

#: Name of the subdirectory the clone lands in, inside the task's own workspace.
#: Kept separate from the workspace root so the engineer still has somewhere
#: writable (the root) even though the clone itself is read-only.
REPO_DIRNAME = "repo"

_CLONE_TIMEOUT_SECONDS = 120
_NPM_CI_TIMEOUT_SECONDS = 300

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
    company_profile: CompanyProfile, workspace_root: Path, task_id: int
) -> Path:
    """Ensure ``workspace_root/<task_id>/`` exists, with a read-only clone of the
    company's repository at ``<task_id>/repo/`` if one is configured.

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
        logger.info("Workspace for task %d already has a clone; leaving it as is.", task_id)
        return task_workspace

    branch = github.options.get("working_branch") or github.options.get("default_branch") or "main"
    _clone_read_only(repo_slug, branch, repo_dir, github.secret_env, task_id)
    return task_workspace


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

    _vendor_node_dependencies(repo_dir, task_id)
    _strip_low_value_files(repo_dir)
    _make_read_only(repo_dir)


def _vendor_node_dependencies(repo_dir: Path, task_id: int) -> None:
    """Install exactly what ``package-lock.json`` specifies, once, before the
    workspace is ever handed to an agent.

    Runs here, in Polska's own process, before the clone is stripped and
    locked read-only -- the same "controlled step outside the agent's own
    execution" the credential injection above already relies on. This is the
    one legitimate place a real network call to fetch a dependency happens;
    the agent's own subprocess, run under a separate, network-restricted
    user, never reaches the registry itself.

    ``npm ci`` (not ``npm install``): deterministic against the committed
    lockfile, and it refuses outright if the lockfile and package.json have
    drifted, rather than silently resolving something slightly different from
    what the repository's own CI would install.

    Skipped entirely when there is no ``package-lock.json``: not every
    company's repository is a Node project, and running ``npm ci`` against
    one that isn't is an error, not a no-op.
    """
    lockfile = repo_dir / "package-lock.json"
    if not lockfile.exists():
        return

    try:
        subprocess.run(
            ["npm", "ci", "--no-audit", "--no-fund"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            timeout=_NPM_CI_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace") if exc.stderr else ""
        raise WorkspaceError(
            f"npm ci failed for task {task_id}'s workspace: {stderr[:500]}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError(
            f"npm ci for task {task_id}'s workspace did not finish within "
            f"{_NPM_CI_TIMEOUT_SECONDS}s."
        ) from exc

    # node_modules is vendored, not source: nothing under it needs the same
    # write protection as the repository the engineer must not edit, and a
    # test runner may need to write its own cache or temp output somewhere
    # inside it. World-writable is a deliberate simplification, not an
    # oversight: this directory holds public npm packages, never a secret or
    # a line of the company's own code, and the agent subprocess runs as a
    # different UID (see runner.py) with no group relationship to this one
    # worth setting up just to avoid it.
    node_modules = repo_dir / "node_modules"
    if node_modules.exists():
        node_modules.chmod(0o777)
        for root, dirs, files in os.walk(node_modules):
            for name in dirs:
                (Path(root) / name).chmod(0o777)
            for name in files:
                (Path(root) / name).chmod(0o666)


def _strip_low_value_files(repo_dir: Path) -> None:
    """Remove lockfiles and binary assets, and warn about anything else large.

    Called before ``_make_read_only``, since these are real deletions and need
    write access to do. Skips ``node_modules`` entirely: vendored dependencies
    are not the low-value noise this exists to trim (see
    ``_vendor_node_dependencies``), and a binary asset a package genuinely
    needs at runtime, an icon, a compiled native addon, must survive here.
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


def _force_rmtree(path: Path) -> None:
    """Delete a tree even when some files in it are read-only.

    git marks some of its own files (pack files in particular) read-only on
    Windows, and a plain ``shutil.rmtree`` fails to delete them; passing
    ``ignore_errors=True`` "fixes" that by silently leaving them behind, which is
    exactly wrong for a directory whose entire purpose is to not exist afterwards.
    The standard fix: on a permission error, clear the read-only bit and retry.
    """

    def _on_error(func, target_path, exc_info):  # noqa: ANN001 - shutil's onexc signature
        os.chmod(target_path, stat.S_IWRITE)
        func(target_path)

    shutil.rmtree(path, onexc=_on_error)


def _basic_auth(token: str) -> str:
    """git's http.extraHeader wants a ready-made Authorization value. A GitHub
    PAT as the basic-auth password with any non-empty username is what GitHub's
    own HTTPS-clone-with-a-token documentation uses."""
    return b64encode(f"x-access-token:{token}".encode()).decode()


def _make_read_only(path: Path) -> None:
    """Best-effort write-protection for the whole tree. See the module docstring:
    this is complete on the Linux deployment target and partial on Windows.

    Never descends into ``node_modules``: see ``_vendor_node_dependencies``,
    which deliberately leaves it writable so a test runner has somewhere to
    put its own cache or temp output, and skips its chmod here entirely
    rather than lock it down and immediately contradict that.
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
