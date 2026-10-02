"""The task workspace: a read-only clone, credential kept out of the agent's reach.

Runs against a real local git repository rather than GitHub, so these tests need
no network and no token: the clone mechanics, the branch selection, the read-only
enforcement and the credential-never-touches-disk claim are all provable locally.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from polska.config.company import CompanyProfile
from polska.db.enums import TaskState, TaskType
from polska.db.models import Company, Task
from polska.db.types import utcnow
from polska.workspace import (
    REPO_DIRNAME,
    WorkspaceError,
    prepare_task_workspace,
    reclaim_node_modules_for_terminal_tasks,
)


def _run(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def local_repo(tmp_path: Path) -> Path:
    """A real git repo with a `dev` branch, so cloning it is a real clone, not a
    fake. `main`/`master` deliberately holds different content than `dev`, so a
    test asserting the wrong branch checked out would actually fail."""
    repo = tmp_path / "source-repo"
    repo.mkdir()
    _run(["git", "init", "-q"], cwd=repo)
    _run(["git", "checkout", "-q", "-b", "main"], cwd=repo)
    (repo / "README.md").write_text("main branch content\n", encoding="utf-8")
    _run(["git", "add", "."], cwd=repo)
    _run(
        ["git", "-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-q", "-m", "init"],
        cwd=repo,
    )
    _run(["git", "checkout", "-q", "-b", "dev"], cwd=repo)
    (repo / "README.md").write_text("dev branch content\n", encoding="utf-8")
    (repo / "src.py").write_text("print('real code')\n", encoding="utf-8")
    (repo / "package-lock.json").write_text('{"fake": "lockfile"}\n' * 1000, encoding="utf-8")
    (repo / "big-data.json").write_text('{"lots":[]}\n' * 10_000, encoding="utf-8")
    _run(["git", "add", "."], cwd=repo)
    _run(
        ["git", "-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-q", "-m", "dev work"],
        cwd=repo,
    )
    return repo


def _profile_with_repo(repo_path: Path, *, working_branch: str = "dev") -> CompanyProfile:
    return CompanyProfile.model_validate(
        {
            "slug": "test-co",
            "name": "Test Co",
            "idea": "x",
            "goals": [],
            "integrations": {
                "github": {
                    "adapter": "dry_run",
                    "options": {
                        "repo": "local/unused",  # overridden per test
                        "working_branch": working_branch,
                    },
                    "secret_env": ["GITHUB_TOKEN"],
                }
            },
        }
    )


def _make_local_clone(repo_path: Path):
    """A stand-in for ``_clone_read_only`` that clones a local path instead of a
    github.com URL, but otherwise mirrors its contract exactly, including wrapping
    a failed clone as ``WorkspaceError`` rather than letting the raw subprocess
    exception through: a test using this must see the same failure shape
    production code does."""
    import polska.workspace as workspace_module

    def local_clone(repo_slug, branch, repo_dir, secret_env_names, task_id):
        token = workspace_module._read_token(secret_env_names)
        # Prove the token really was read (it flows to here in the real function
        # too), then clone from the local path instead of github.com.
        assert token is None or isinstance(token, str)
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
                    str(repo_path),
                    str(repo_dir),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode(errors="replace") if exc.stderr else ""
            raise WorkspaceError(f"Could not clone {repo_slug}@{branch}: {stderr[:500]}") from exc

        workspace_module._force_rmtree(repo_dir / ".git")
        workspace_module._strip_low_value_files(repo_dir)
        workspace_module._make_read_only(repo_dir)

    return local_clone


def test_a_profile_with_no_github_integration_gets_a_plain_workspace(tmp_path: Path) -> None:
    profile = CompanyProfile.model_validate(
        {"slug": "no-repo-co", "name": "x", "idea": "x", "goals": []}
    )
    workspace = prepare_task_workspace(profile, tmp_path, task_id=1)
    assert workspace == tmp_path / "1"
    assert workspace.exists()
    assert not (workspace / "repo").exists()


def test_the_task_workspace_is_writable_by_a_different_uid(tmp_path: Path) -> None:
    """The orchestrator (one user) creates this directory; the agent subprocess
    that actually writes into it runs as a different, unprivileged user with
    no network access (see runner.py). A plain mkdir's default mode leaves
    "other" without a write bit, which would make this the one directory the
    agent is meant to write to and structurally cannot."""
    profile = CompanyProfile.model_validate(
        {"slug": "no-repo-co", "name": "x", "idea": "x", "goals": []}
    )
    workspace = prepare_task_workspace(profile, tmp_path, task_id=1)
    assert workspace.stat().st_mode & 0o777 == 0o777


def test_the_correct_branch_is_checked_out(tmp_path, local_repo, monkeypatch) -> None:
    profile = _profile_with_repo(local_repo, working_branch="dev")
    _make_workspace_from_local(tmp_path, profile, local_repo, monkeypatch, task_id=1)

    repo_dir = tmp_path / "1" / "repo"
    assert (repo_dir / "README.md").read_text(encoding="utf-8") == "dev branch content\n"
    assert (repo_dir / "src.py").exists()


def test_falls_back_to_default_branch_when_no_working_branch_is_set(
    tmp_path, local_repo, monkeypatch
) -> None:
    profile = CompanyProfile.model_validate(
        {
            "slug": "test-co",
            "name": "x",
            "idea": "x",
            "goals": [],
            "integrations": {
                "github": {
                    "adapter": "dry_run",
                    "options": {"repo": "local/unused", "default_branch": "main"},
                    "secret_env": ["GITHUB_TOKEN"],
                }
            },
        }
    )
    _make_workspace_from_local(tmp_path, profile, local_repo, monkeypatch, task_id=2)

    repo_dir = tmp_path / "2" / "repo"
    assert (repo_dir / "README.md").read_text(encoding="utf-8") == "main branch content\n"


def test_the_clone_has_no_git_directory(tmp_path, local_repo, monkeypatch) -> None:
    profile = _profile_with_repo(local_repo)
    _make_workspace_from_local(tmp_path, profile, local_repo, monkeypatch, task_id=3)

    assert not (tmp_path / "3" / "repo" / ".git").exists()


def test_the_clone_is_read_only(tmp_path, local_repo, monkeypatch) -> None:
    profile = _profile_with_repo(local_repo)
    _make_workspace_from_local(tmp_path, profile, local_repo, monkeypatch, task_id=4)

    target = tmp_path / "4" / "repo" / "README.md"
    with pytest.raises(PermissionError):
        target.write_text("an agent should not be able to do this", encoding="utf-8")


def test_lockfiles_are_stripped_from_the_clone(tmp_path, local_repo, monkeypatch) -> None:
    """This is the fix for a real overshoot: a 478KB package-lock.json in a real
    clone contributed to a run spending 900k+ tokens against a 200k reservation."""
    profile = _profile_with_repo(local_repo)
    workspace = _make_workspace_from_local(tmp_path, profile, local_repo, monkeypatch, task_id=9)

    assert not (workspace / "repo" / "package-lock.json").exists()
    assert (workspace / "repo" / "src.py").exists()  # real source survives


def test_a_large_non_lockfile_is_left_in_place_but_warned_about(
    tmp_path, local_repo, monkeypatch, caplog
) -> None:
    """A large real data file might still be genuinely relevant, unlike a
    lockfile, so it is not deleted -- but its size is worth a warning, since it
    is exactly the shape of thing that caused the real overshoot."""
    profile = _profile_with_repo(local_repo)
    with caplog.at_level("WARNING", logger="polska.workspace"):
        workspace = _make_workspace_from_local(
            tmp_path, profile, local_repo, monkeypatch, task_id=10
        )

    assert (workspace / "repo" / "big-data.json").exists()
    assert any("big-data.json" in r.getMessage() for r in caplog.records)


def test_the_task_workspace_root_itself_stays_writable(tmp_path, local_repo, monkeypatch) -> None:
    """The clone is read-only; the workspace it sits inside is not, so the agent
    still has somewhere to write new files."""
    profile = _profile_with_repo(local_repo)
    workspace = _make_workspace_from_local(tmp_path, profile, local_repo, monkeypatch, task_id=5)

    scratch = workspace / "notes.txt"
    scratch.write_text("proposed change goes here", encoding="utf-8")
    assert scratch.read_text(encoding="utf-8") == "proposed change goes here"


def test_a_retry_does_not_reclone(tmp_path, local_repo, monkeypatch) -> None:
    profile = _profile_with_repo(local_repo)
    calls = {"n": 0}
    real_local_clone = _make_local_clone(local_repo)

    def counting_clone(*args, **kwargs):
        calls["n"] += 1
        return real_local_clone(*args, **kwargs)

    import polska.workspace as workspace_module

    monkeypatch.setattr(workspace_module, "_clone_read_only", counting_clone)

    prepare_task_workspace(profile, tmp_path, task_id=6)
    prepare_task_workspace(profile, tmp_path, task_id=6)

    assert calls["n"] == 1


def test_a_repo_that_cannot_be_cloned_raises_workspace_error(tmp_path: Path, monkeypatch) -> None:
    """No network involved: points the clone at a path that is not a git
    repository at all, which fails exactly the way a bad repo slug or a dead
    token would, just without needing a real network call to prove it."""
    nonexistent = tmp_path / "not-a-git-repo"
    nonexistent.mkdir()

    monkeypatch.setattr("polska.workspace._clone_read_only", _make_local_clone(nonexistent))

    profile = _profile_with_repo(nonexistent)
    with pytest.raises(WorkspaceError):
        prepare_task_workspace(profile, tmp_path, task_id=7)


def test_the_credential_never_touches_a_file(tmp_path, local_repo, monkeypatch) -> None:
    """The real, non-monkeypatched clone path, proven against a local repo: the
    token is read from the environment, handed to one subprocess call, and never
    appears in the resulting .git/config -- except there is no .git left at all,
    which this also confirms."""
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token-should-never-persist-anywhere")
    import polska.workspace as workspace_module

    # Exercise the real _clone_read_only, pointed at the local path by patching
    # only the URL template it builds, not the function itself.
    original_run = subprocess.run

    def redirecting_run(args, **kwargs):
        if args and args[0] == "npm":
            # The fixture's package-lock.json is fake, written to exercise
            # lockfile *stripping* elsewhere in this file, not a real npm
            # project. This test is about credential handling, not vendoring,
            # so a real npm ci against it (which would fail, npm may not even
            # be installed here) is beside the point: pretend it succeeded.
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        patched_args = [
            str(local_repo) if isinstance(a, str) and a.startswith("https://github.com/") else a
            for a in args
        ]
        return original_run(patched_args, **kwargs)

    monkeypatch.setattr(workspace_module.subprocess, "run", redirecting_run)

    profile = _profile_with_repo(local_repo)
    workspace = prepare_task_workspace(profile, tmp_path, task_id=8)

    assert not (workspace / "repo" / ".git").exists()
    # And the environment variable set above is the only place the token ever
    # lived in this process; nothing under the workspace can contain it since
    # there is no file left that git ever wrote credentials into.
    for path in (workspace / "repo").rglob("*"):
        if path.is_file():
            assert "fake-token-should-never-persist-anywhere" not in path.read_text(
                encoding="utf-8", errors="ignore"
            )


# --------------------------------------------------------------- node_modules skip


def test_stripping_never_descends_into_node_modules(tmp_path) -> None:
    from polska.workspace import _strip_low_value_files

    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    node_modules = tmp_path / "node_modules" / "some-package"
    node_modules.mkdir(parents=True)
    # A binary asset a package might genuinely need at runtime: _strip_low_value_files
    # would delete this on sight anywhere else in the tree.
    icon = node_modules / "icon.png"
    icon.write_bytes(b"\x89PNG\r\n")
    nested_lockfile = node_modules / "package-lock.json"
    nested_lockfile.write_text("{}", encoding="utf-8")

    _strip_low_value_files(tmp_path)

    assert not (tmp_path / "package-lock.json").exists()  # source-level lockfile: gone
    assert icon.exists()  # nothing under node_modules touched
    assert nested_lockfile.exists()


def test_read_only_lockdown_never_descends_into_node_modules(tmp_path) -> None:
    from polska.workspace import _make_read_only

    node_modules = tmp_path / "node_modules" / "some-package"
    node_modules.mkdir(parents=True)
    installed_file = node_modules / "index.js"
    installed_file.write_text("module.exports = {};\n", encoding="utf-8")

    _make_read_only(tmp_path)

    # Still writable: node_modules's own permissions were never touched here.
    installed_file.write_text("still writable\n", encoding="utf-8")


# ------------------------------------------------------------------------ vendoring


def test_vendoring_is_skipped_for_a_non_node_repo(tmp_path, monkeypatch) -> None:
    from polska.workspace import _ensure_node_dependencies_vendored

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("npm must never run against a non-Node repo")

    monkeypatch.setattr(subprocess, "run", _fail_if_called)
    _ensure_node_dependencies_vendored(tmp_path, task_id=1)  # no package.json here


def test_vendoring_is_skipped_once_the_completion_marker_exists(tmp_path, monkeypatch) -> None:
    """The real gate: the completion marker, not whether node_modules exists.
    Already-vendored means nothing left to do, on a fresh clone or a
    retroactive one."""
    from polska.workspace import _VENDORED_MARKER_NAME, _ensure_node_dependencies_vendored

    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    node_modules = tmp_path / "node_modules"
    node_modules.mkdir()
    (node_modules / _VENDORED_MARKER_NAME).write_text("ci\n", encoding="utf-8")

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("npm must never run once the completion marker exists")

    monkeypatch.setattr(subprocess, "run", _fail_if_called)
    _ensure_node_dependencies_vendored(tmp_path, task_id=1)


def test_node_modules_without_the_marker_is_treated_as_partial_and_redone(
    tmp_path, monkeypatch, caplog
) -> None:
    """The exact gap this closes: a container killed mid-install leaves
    node_modules existing but incomplete, permanently indistinguishable from
    a real one without something that only gets written on success. This
    must be removed and reinstalled, not trusted."""
    from polska.workspace import _VENDORED_MARKER_NAME, _ensure_node_dependencies_vendored

    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    node_modules = tmp_path / "node_modules"
    stale_file = node_modules / "half-installed-package" / "index.js"
    stale_file.parent.mkdir(parents=True)
    stale_file.write_text("truncated", encoding="utf-8")

    calls: list[list[str]] = []

    def _fake_run(args, **kwargs):
        calls.append(args)
        # Simulate a real npm ci: the stale partial content is gone (a real
        # npm run would not magically un-truncate it), a fresh tree exists.
        assert not stale_file.exists()
        node_modules.mkdir(exist_ok=True)
        (node_modules / "real-package").mkdir()
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    with caplog.at_level("WARNING", logger="polska.workspace"):
        _ensure_node_dependencies_vendored(tmp_path, task_id=1)

    assert calls == [["npm", "ci", "--no-audit", "--no-fund"]]
    assert any("without a completion marker" in r.getMessage() for r in caplog.records)
    assert (node_modules / _VENDORED_MARKER_NAME).exists()
    assert not (node_modules / "half-installed-package").exists()


def test_vendoring_runs_npm_ci_against_the_lockfile(tmp_path, monkeypatch) -> None:
    from polska.workspace import _ensure_node_dependencies_vendored

    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    calls: list[tuple[list[str], Path]] = []

    def _fake_run(args, *, cwd, **kwargs):
        calls.append((args, cwd))
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    _ensure_node_dependencies_vendored(tmp_path, task_id=1)

    assert len(calls) == 1
    args, cwd = calls[0]
    assert args == ["npm", "ci", "--no-audit", "--no-fund"]
    assert cwd == tmp_path


def test_a_failed_npm_ci_raises_workspace_error(tmp_path, monkeypatch) -> None:
    from polska.workspace import _ensure_node_dependencies_vendored

    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")

    def _fake_run(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, output=b"", stderr=b"lockfile drifted")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    with pytest.raises(WorkspaceError, match="ci failed"):
        _ensure_node_dependencies_vendored(tmp_path, task_id=1)


def test_vendored_node_modules_is_left_writable(tmp_path, monkeypatch) -> None:
    """node_modules must survive vendoring writable: a test runner may need to
    put its own cache or temp output somewhere inside it, and the read-only
    lockdown that protects the company's own source must never apply here."""
    from polska.workspace import _ensure_node_dependencies_vendored

    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    node_modules = tmp_path / "node_modules"

    def _fake_run(args, **kwargs):
        # Simulates what a real npm ci would have created.
        (node_modules / "some-package").mkdir(parents=True)
        installed_file = node_modules / "some-package" / "index.js"
        installed_file.write_text("module.exports = {};\n", encoding="utf-8")
        installed_file.chmod(0o444)  # as if npm had installed it read-only
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    _ensure_node_dependencies_vendored(tmp_path, task_id=1)

    installed_file = node_modules / "some-package" / "index.js"
    assert installed_file.stat().st_mode & 0o777 == 0o666
    assert node_modules.stat().st_mode & 0o777 == 0o777


# ------------------------------------------------------------- retroactive vendoring


def test_a_clone_missing_only_the_lockfile_falls_back_to_npm_install(
    tmp_path, monkeypatch, caplog
) -> None:
    """The exact incident this closes: an old clone whose lockfile
    _strip_low_value_files already removed, from before vendoring existed to
    need it kept. No lockfile means no npm ci, and re-fetching just that one
    file would mean a second use of the clone credential -- so this resolves
    fresh from package.json instead."""
    from polska.workspace import _ensure_node_dependencies_vendored

    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    # Deliberately no package-lock.json: already stripped, same as a real
    # pre-vendoring clone.
    calls: list[list[str]] = []

    def _fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    with caplog.at_level("WARNING", logger="polska.workspace"):
        _ensure_node_dependencies_vendored(tmp_path, task_id=9)

    assert calls == [["npm", "install", "--no-audit", "--no-fund"]]
    assert any("predates dependency vendoring" in r.getMessage() for r in caplog.records)


def test_retroactive_vendoring_temporarily_unlocks_and_relocks_a_read_only_clone(
    tmp_path, monkeypatch
) -> None:
    """An old clone this runs against retroactively may already have been
    locked read-only by a previous _make_read_only pass, long before this
    existed to check for it. npm needs to create node_modules inside it
    regardless, and the directory must end up exactly as locked as it
    started once done.

    Drives this through the module's own os.access/Path.chmod calls rather
    than real filesystem permissions: chmod's actual enforcement is, per
    this project's own established caveat (see _make_read_only's docstring),
    complete on Linux and only partial on Windows, so relying on the real OS
    to hold a directory read-only would make this test meaningless on the
    machine most likely to run it during development.
    """
    import polska.workspace as workspace_module
    from polska.workspace import _ensure_node_dependencies_vendored

    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(workspace_module.os, "access", lambda *a, **kw: False)  # "locked"
    chmod_calls: list[int] = []
    monkeypatch.setattr(Path, "chmod", lambda self, mode: chmod_calls.append(mode))
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0))

    _ensure_node_dependencies_vendored(tmp_path, task_id=9)

    # Unlocked (0o755) before the install, relocked (0o555) after -- in that
    # order, and nothing left mid-way if the install itself had failed
    # (see the next test for that half).
    assert chmod_calls == [0o755, 0o555]


def test_a_locked_clone_stays_locked_if_the_install_itself_fails(tmp_path, monkeypatch) -> None:
    """The relock has to happen even when npm fails, or a workspace that was
    read-only before this ran ends up writable after a failed attempt --
    exactly the kind of half-finished state this project refuses to leave
    behind elsewhere (see WorkspaceError's own callers)."""
    import polska.workspace as workspace_module
    from polska.workspace import WorkspaceError, _ensure_node_dependencies_vendored

    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(workspace_module.os, "access", lambda *a, **kw: False)
    chmod_calls: list[int] = []
    monkeypatch.setattr(Path, "chmod", lambda self, mode: chmod_calls.append(mode))

    def _fake_run(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, output=b"", stderr=b"network unreachable")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    with pytest.raises(WorkspaceError):
        _ensure_node_dependencies_vendored(tmp_path, task_id=9)

    assert chmod_calls == [0o755, 0o555]  # relocked even though the install raised


def test_a_freshly_writable_clone_is_never_chmodded_by_vendoring(tmp_path, monkeypatch) -> None:
    """The non-retroactive case (a brand new clone, already writable) must
    take no lock/unlock action at all: _make_read_only runs right after this
    and is what applies the real lockdown, exactly once, the same as always."""
    import polska.workspace as workspace_module
    from polska.workspace import _ensure_node_dependencies_vendored

    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(workspace_module.os, "access", lambda *a, **kw: True)  # "writable"
    chmod_calls: list[int] = []
    monkeypatch.setattr(Path, "chmod", lambda self, mode: chmod_calls.append(mode))
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0))

    _ensure_node_dependencies_vendored(tmp_path, task_id=1)

    assert chmod_calls == []


# -------------------------------------------------------------- writable copy


def _repo_with_node_modules(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "1" / REPO_DIRNAME
    (repo_dir / "src").mkdir(parents=True)
    (repo_dir / "src" / "index.ts").write_text("export const x = 1;\n", encoding="utf-8")
    (repo_dir / "package.json").write_text("{}", encoding="utf-8")
    (repo_dir / "node_modules" / "some-package").mkdir(parents=True)
    (repo_dir / "node_modules" / "some-package" / "index.js").write_text(
        "module.exports = {};\n", encoding="utf-8"
    )
    return repo_dir


def test_writable_copy_is_created_alongside_the_read_only_clone(tmp_path: Path) -> None:
    from polska.workspace import WORK_DIRNAME, _ensure_writable_copy

    repo_dir = _repo_with_node_modules(tmp_path)
    task_workspace = tmp_path / "1"

    _ensure_writable_copy(task_workspace, repo_dir, task_id=1)

    work_dir = task_workspace / WORK_DIRNAME
    assert (work_dir / "src" / "index.ts").read_text(encoding="utf-8") == "export const x = 1;\n"
    # Writable, unlike the source it was copied from would be once locked.
    (work_dir / "src" / "index.ts").write_text("export const x = 2;\n", encoding="utf-8")


def test_node_modules_is_symlinked_not_duplicated(tmp_path: Path) -> None:
    from polska.workspace import _ensure_writable_copy

    repo_dir = _repo_with_node_modules(tmp_path)
    task_workspace = tmp_path / "1"

    _ensure_writable_copy(task_workspace, repo_dir, task_id=1)

    work_node_modules = task_workspace / "work" / "node_modules"
    assert work_node_modules.is_symlink()
    assert work_node_modules.resolve() == (repo_dir / "node_modules").resolve()


def test_a_repo_with_no_node_modules_gets_a_copy_with_no_symlink(tmp_path: Path) -> None:
    """Not every company's repository is a Node project; the symlink step
    must not assume node_modules exists at all."""
    from polska.workspace import _ensure_writable_copy

    repo_dir = tmp_path / "1" / REPO_DIRNAME
    (repo_dir / "notes.md").parent.mkdir(parents=True)
    (repo_dir / "notes.md").write_text("plain repo, no node project", encoding="utf-8")
    task_workspace = tmp_path / "1"

    _ensure_writable_copy(task_workspace, repo_dir, task_id=1)

    work_dir = task_workspace / "work"
    assert (work_dir / "notes.md").exists()
    assert not (work_dir / "node_modules").exists()


def test_writable_copy_is_a_no_op_without_a_repo_at_all(tmp_path: Path) -> None:
    from polska.workspace import _ensure_writable_copy

    task_workspace = tmp_path / "1"
    task_workspace.mkdir(parents=True)
    _ensure_writable_copy(task_workspace, task_workspace / REPO_DIRNAME, task_id=1)

    assert not (task_workspace / "work").exists()


def test_writable_copy_is_skipped_once_the_ready_marker_exists(tmp_path: Path, monkeypatch) -> None:
    from polska.workspace import WORK_DIRNAME, _ensure_writable_copy

    repo_dir = _repo_with_node_modules(tmp_path)
    task_workspace = tmp_path / "1"
    work_dir = task_workspace / WORK_DIRNAME
    work_dir.mkdir()
    (work_dir / ".polska-work-ready").write_text("ready\n", encoding="utf-8")

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("must not recopy once the ready marker exists")

    monkeypatch.setattr(shutil, "copytree", _fail_if_called)
    _ensure_writable_copy(task_workspace, repo_dir, task_id=1)  # must not raise


def test_a_partial_copy_without_the_marker_is_redone(tmp_path: Path, caplog) -> None:
    """The exact gap this closes, same shape as node_modules's own: a
    container dying mid-copy leaves work/ existing but incomplete,
    permanently indistinguishable from a real one without a marker."""
    from polska.workspace import WORK_DIRNAME, _ensure_writable_copy

    repo_dir = _repo_with_node_modules(tmp_path)
    task_workspace = tmp_path / "1"
    work_dir = task_workspace / WORK_DIRNAME
    (work_dir / "half-copied").mkdir(parents=True)  # no .polska-work-ready

    with caplog.at_level("WARNING", logger="polska.workspace"):
        _ensure_writable_copy(task_workspace, repo_dir, task_id=1)

    assert any("without a completion marker" in r.getMessage() for r in caplog.records)
    assert not (work_dir / "half-copied").exists()
    assert (work_dir / "src" / "index.ts").exists()
    assert (work_dir / ".polska-work-ready").exists()


def test_engineering_tasks_get_a_writable_copy_research_does_not(
    tmp_path, local_repo, monkeypatch
) -> None:
    """prepare_task_workspace's own gate: only a task type that will actually
    edit something pays for the copy."""
    profile = _profile_with_repo(local_repo)

    monkeypatch.setattr("polska.workspace._clone_read_only", _make_local_clone(local_repo))
    prepare_task_workspace(profile, tmp_path, task_id=1, needs_writable_copy=True)
    prepare_task_workspace(profile, tmp_path, task_id=2, needs_writable_copy=False)

    assert (tmp_path / "1" / "work").exists()
    assert not (tmp_path / "2" / "work").exists()


# ---------------------------------------------------------------- reclaiming


def _terminal_task(session, company: Company, state: TaskState, *, finished_at) -> Task:
    task = Task(
        company_id=company.id,
        type=TaskType.ENGINEERING,
        title="x",
        description="x",
        rationale="x",
    )
    session.add(task)
    session.flush()
    task.transition_to(TaskState.RUNNING)
    task.transition_to(state, now=finished_at)
    session.commit()
    return task


def test_reclaim_deletes_node_modules_past_its_grace_period(session, company, tmp_path) -> None:
    old_enough = utcnow() - dt.timedelta(hours=48)
    task = _terminal_task(session, company, TaskState.DONE, finished_at=old_enough)
    node_modules = tmp_path / str(task.id) / REPO_DIRNAME / "node_modules"
    (node_modules / "some-package").mkdir(parents=True)

    reclaimed = reclaim_node_modules_for_terminal_tasks(
        session, tmp_path, grace_period=dt.timedelta(hours=24)
    )

    assert reclaimed == 1
    assert not node_modules.exists()
    assert (tmp_path / str(task.id) / REPO_DIRNAME).exists()  # the rest of the workspace survives


def test_reclaim_leaves_a_recently_finished_task_alone(session, company, tmp_path) -> None:
    just_finished = utcnow() - dt.timedelta(hours=1)
    task = _terminal_task(session, company, TaskState.DONE, finished_at=just_finished)
    node_modules = tmp_path / str(task.id) / REPO_DIRNAME / "node_modules"
    (node_modules / "some-package").mkdir(parents=True)

    reclaimed = reclaim_node_modules_for_terminal_tasks(
        session, tmp_path, grace_period=dt.timedelta(hours=24)
    )

    assert reclaimed == 0
    assert node_modules.exists()


def test_reclaim_never_touches_a_failed_task(session, company, tmp_path) -> None:
    """failed is still retry-eligible; a retry reusing the clone needs its
    dependencies back, which would cost a fresh install this should not
    force just by having run once."""
    task = Task(
        company_id=company.id,
        type=TaskType.ENGINEERING,
        title="x",
        description="x",
        rationale="x",
    )
    session.add(task)
    session.flush()
    old_enough = utcnow() - dt.timedelta(hours=48)
    task.transition_to(TaskState.RUNNING)
    task.transition_to(TaskState.FAILED, error="x", now=old_enough)
    session.commit()

    node_modules = tmp_path / str(task.id) / REPO_DIRNAME / "node_modules"
    (node_modules / "some-package").mkdir(parents=True)

    reclaimed = reclaim_node_modules_for_terminal_tasks(
        session, tmp_path, grace_period=dt.timedelta(hours=24)
    )

    assert reclaimed == 0
    assert node_modules.exists()


def test_reclaim_is_a_no_op_when_there_is_nothing_to_reclaim(session, company, tmp_path) -> None:
    old_enough = utcnow() - dt.timedelta(hours=48)
    _terminal_task(session, company, TaskState.ABANDONED, finished_at=old_enough)
    # No node_modules ever created for this task, e.g. a non-Node company.

    reclaimed = reclaim_node_modules_for_terminal_tasks(
        session, tmp_path, grace_period=dt.timedelta(hours=24)
    )
    assert reclaimed == 0


def _make_workspace_from_local(tmp_path, profile, local_repo, monkeypatch, *, task_id: int) -> Path:
    monkeypatch.setattr("polska.workspace._clone_read_only", _make_local_clone(local_repo))
    return prepare_task_workspace(profile, tmp_path, task_id)


# ------------------------------------------------- removing trees that will not go


def test_force_rmtree_removes_a_tree_and_reports_it(tmp_path: Path) -> None:
    import polska.workspace as workspace_module

    tree = tmp_path / "tree"
    (tree / "a" / "b").mkdir(parents=True)
    (tree / "a" / "b" / "f.txt").write_text("x", encoding="utf-8")

    assert workspace_module._force_rmtree(tree) is True
    assert not tree.exists()


def test_force_rmtree_on_a_missing_path_is_a_quiet_success(tmp_path: Path) -> None:
    import polska.workspace as workspace_module

    assert workspace_module._force_rmtree(tmp_path / "never-existed") is True


def test_force_rmtree_reports_a_tree_it_could_not_delete_instead_of_raising(
    tmp_path: Path, monkeypatch
) -> None:
    """The failure that stopped every scheduled tick: a file owned by another user
    (the agent UID) that this process can neither chmod nor unlink."""
    import polska.workspace as workspace_module

    tree = tmp_path / "tree"
    tree.mkdir()
    stuck = tree / "results.json"
    stuck.write_text("{}", encoding="utf-8")

    def fake_rmtree(path, onexc):
        onexc(os.unlink, str(stuck), PermissionError(13, "Permission denied"))

    def refuse(*args, **kwargs):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(workspace_module.shutil, "rmtree", fake_rmtree)
    monkeypatch.setattr(workspace_module.os, "chmod", refuse)

    assert workspace_module._force_rmtree(tree) is False
    assert stuck.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX directory modes")
def test_force_rmtree_never_strips_a_directorys_read_and_execute_bits(
    tmp_path: Path, monkeypatch
) -> None:
    """The old handler did chmod(path, S_IWRITE), which is exactly mode 0o200: on a
    directory that removes read and execute and leaves nothing able to enter it."""
    import polska.workspace as workspace_module

    tree = tmp_path / "tree"
    sub = tree / "sub"
    sub.mkdir(parents=True)
    sub.chmod(0o755)
    before = stat.S_IMODE(sub.stat().st_mode)

    def always_denied(target):
        raise PermissionError(13, "Permission denied")

    def fake_rmtree(path, onexc):
        onexc(always_denied, str(sub), PermissionError(13, "Permission denied"))

    monkeypatch.setattr(workspace_module.shutil, "rmtree", fake_rmtree)
    workspace_module._force_rmtree(tree)

    after = stat.S_IMODE(sub.stat().st_mode)
    assert after & before == before
    assert after & stat.S_IRUSR
    assert after & stat.S_IXUSR


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs real POSIX permissions and a non-root user",
)
def test_force_rmtree_removes_files_inside_a_read_only_directory(tmp_path: Path) -> None:
    """On POSIX it is the parent directory's write bit that blocks the unlink, which
    the old handler (it chmod'd the file itself) never fixed."""
    import polska.workspace as workspace_module

    tree = tmp_path / "tree"
    sub = tree / "sub"
    sub.mkdir(parents=True)
    (sub / "f.txt").write_text("x", encoding="utf-8")
    sub.chmod(0o555)

    assert workspace_module._force_rmtree(tree) is True
    assert not tree.exists()


def test_reclaim_does_not_count_a_workspace_it_could_not_clear(
    session, company, tmp_path, monkeypatch
) -> None:
    old_enough = utcnow() - dt.timedelta(hours=48)
    task = _terminal_task(session, company, TaskState.DONE, finished_at=old_enough)
    node_modules = tmp_path / str(task.id) / REPO_DIRNAME / "node_modules"
    (node_modules / "some-package").mkdir(parents=True)
    monkeypatch.setattr("polska.workspace._force_rmtree", lambda path: False)

    reclaimed = reclaim_node_modules_for_terminal_tasks(
        session, tmp_path, grace_period=dt.timedelta(hours=24)
    )

    assert reclaimed == 0
    assert node_modules.exists()


def test_reclaim_carries_on_past_a_workspace_it_cannot_touch(
    session, company, tmp_path, monkeypatch
) -> None:
    import polska.workspace as workspace_module

    old_enough = utcnow() - dt.timedelta(hours=48)
    stuck = _terminal_task(session, company, TaskState.DONE, finished_at=old_enough)
    fine = _terminal_task(session, company, TaskState.DONE, finished_at=old_enough)
    stuck_modules = tmp_path / str(stuck.id) / REPO_DIRNAME / "node_modules"
    fine_modules = tmp_path / str(fine.id) / REPO_DIRNAME / "node_modules"
    (stuck_modules / "pkg").mkdir(parents=True)
    (fine_modules / "pkg").mkdir(parents=True)

    real = workspace_module._force_rmtree

    def selective(path):
        if str(stuck.id) in Path(path).parts:
            raise PermissionError(13, "Permission denied")
        return real(path)

    monkeypatch.setattr("polska.workspace._force_rmtree", selective)

    reclaimed = reclaim_node_modules_for_terminal_tasks(
        session, tmp_path, grace_period=dt.timedelta(hours=24)
    )

    assert reclaimed == 1
    assert stuck_modules.exists()
    assert not fine_modules.exists()


def test_a_partial_node_modules_that_will_not_delete_fails_loudly(tmp_path, monkeypatch) -> None:
    import polska.workspace as workspace_module

    repo_dir = tmp_path / "repo"
    (repo_dir / "node_modules" / "pkg").mkdir(parents=True)
    (repo_dir / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(workspace_module, "_force_rmtree", lambda path: False)

    with pytest.raises(WorkspaceError, match="partial node_modules"):
        workspace_module._ensure_node_dependencies_vendored(repo_dir, task_id=7)


def test_a_partial_work_copy_that_will_not_delete_fails_loudly(tmp_path, monkeypatch) -> None:
    import polska.workspace as workspace_module

    task_workspace = tmp_path / "7"
    repo_dir = task_workspace / "repo"
    repo_dir.mkdir(parents=True)
    (task_workspace / "work").mkdir()
    monkeypatch.setattr(workspace_module, "_force_rmtree", lambda path: False)

    with pytest.raises(WorkspaceError, match=r"partial work/"):
        workspace_module._ensure_writable_copy(task_workspace, repo_dir, task_id=7)


def test_the_writable_copy_is_writable_before_node_modules_is_linked_into_it(
    tmp_path, monkeypatch
) -> None:
    """2 Oct 2026, first time work/ ever ran as anyone but root: copytree copies the
    locked read-only repo's mode onto work/, and the symlink for node_modules was
    created inside it before the chmod that makes it writable. Root ignores that;
    every other user gets PermissionError."""
    import polska.workspace as workspace_module

    task_workspace = tmp_path / "7"
    repo_dir = task_workspace / "repo"
    (repo_dir / "src").mkdir(parents=True)
    (repo_dir / "src" / "a.ts").write_text("export {}\n", encoding="utf-8")
    (repo_dir / "node_modules").mkdir()
    workspace_module._make_read_only(repo_dir)

    seen: dict[str, int] = {}

    def record_symlink(self, target, target_is_directory=False):
        seen["parent_mode"] = stat.S_IMODE(self.parent.stat().st_mode)

    monkeypatch.setattr(Path, "symlink_to", record_symlink)

    workspace_module._ensure_writable_copy(task_workspace, repo_dir, task_id=7)

    assert seen["parent_mode"] & stat.S_IWUSR, (
        "work/ was still read-only when node_modules was linked"
    )


def test_the_node_modules_link_survives_a_relative_workspace_root(tmp_path, monkeypatch) -> None:
    """Production's workspace root is the relative ``data/workspaces``. A symlink target
    is resolved against the link's own directory, not the cwd, so a relative target
    dangled and work/ had no node_modules at all (found live, 2 Oct 2026). pytest's
    tmp_path is absolute, which is why nothing caught it."""
    import polska.workspace as workspace_module

    probe = tmp_path / "probe"
    try:
        probe.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("this platform or user cannot create symlinks")
    probe.unlink()

    monkeypatch.chdir(tmp_path)
    task_workspace = Path("data/workspaces/7")
    repo_dir = task_workspace / "repo"
    (repo_dir / "src").mkdir(parents=True)
    (repo_dir / "src" / "a.ts").write_text("export {}\n", encoding="utf-8")
    (repo_dir / "node_modules" / "pkg").mkdir(parents=True)

    workspace_module._ensure_writable_copy(task_workspace, repo_dir, task_id=7)

    link = task_workspace / "work" / "node_modules"
    assert link.is_symlink()
    assert link.exists(), "the link dangles: it points at a path relative to the wrong directory"
    assert link.resolve() == (repo_dir / "node_modules").resolve()
