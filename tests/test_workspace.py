"""The task workspace: a read-only clone, credential kept out of the agent's reach.

Runs against a real local git repository rather than GitHub, so these tests need
no network and no token: the clone mechanics, the branch selection, the read-only
enforcement and the credential-never-touches-disk claim are all provable locally.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from polska.config.company import CompanyProfile
from polska.workspace import WorkspaceError, prepare_task_workspace


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


def test_vendoring_is_skipped_without_a_lockfile(tmp_path, monkeypatch) -> None:
    from polska.workspace import _vendor_node_dependencies

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("npm ci must never run against a non-Node repo")

    monkeypatch.setattr(subprocess, "run", _fail_if_called)
    _vendor_node_dependencies(tmp_path, task_id=1)  # no package-lock.json here


def test_vendoring_runs_npm_ci_against_the_lockfile(tmp_path, monkeypatch) -> None:
    from polska.workspace import _vendor_node_dependencies

    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    calls: list[tuple[list[str], Path]] = []

    def _fake_run(args, *, cwd, **kwargs):
        calls.append((args, cwd))
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    _vendor_node_dependencies(tmp_path, task_id=1)

    assert len(calls) == 1
    args, cwd = calls[0]
    assert args == ["npm", "ci", "--no-audit", "--no-fund"]
    assert cwd == tmp_path


def test_a_failed_npm_ci_raises_workspace_error(tmp_path, monkeypatch) -> None:
    from polska.workspace import _vendor_node_dependencies

    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")

    def _fake_run(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, output=b"", stderr=b"lockfile drifted")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    with pytest.raises(WorkspaceError, match="npm ci failed"):
        _vendor_node_dependencies(tmp_path, task_id=1)


def test_vendored_node_modules_is_left_writable(tmp_path, monkeypatch) -> None:
    """node_modules must survive vendoring writable: a test runner may need to
    put its own cache or temp output somewhere inside it, and the read-only
    lockdown that protects the company's own source must never apply here."""
    from polska.workspace import _vendor_node_dependencies

    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    node_modules = tmp_path / "node_modules"
    (node_modules / "some-package").mkdir(parents=True)
    installed_file = node_modules / "some-package" / "index.js"
    installed_file.write_text("module.exports = {};\n", encoding="utf-8")
    installed_file.chmod(0o444)  # as if npm had installed it read-only

    monkeypatch.setattr(
        subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0)
    )
    _vendor_node_dependencies(tmp_path, task_id=1)

    assert installed_file.stat().st_mode & 0o777 == 0o666
    assert node_modules.stat().st_mode & 0o777 == 0o777


def _make_workspace_from_local(tmp_path, profile, local_repo, monkeypatch, *, task_id: int) -> Path:
    monkeypatch.setattr("polska.workspace._clone_read_only", _make_local_clone(local_repo))
    return prepare_task_workspace(profile, tmp_path, task_id)
