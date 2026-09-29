"""Deriving verification-attempt evidence from a run's own tool-call trace.

Uses a plain stand-in for Run rather than the real ORM model: this module
only ever reads .id and .tools_called, and a fake keeps these tests fast and
independent of the database entirely.
"""

from __future__ import annotations

from polska.trace_analysis import summarize_verification_attempts


def _run(tools_called: list[dict], run_id: int = 1):
    return type("FakeRun", (), {"id": run_id, "tools_called": tools_called})()


def _bash(command: str, *, is_error: bool = False) -> dict:
    return {"id": "x", "name": "Bash", "input": {"command": command}, "is_error": is_error}


def _read(path: str) -> dict:
    return {"id": "x", "name": "Read", "input": {"file_path": path}, "is_error": False}


def _grep(pattern: str, path: str) -> dict:
    return {
        "id": "x",
        "name": "Grep",
        "input": {"pattern": pattern, "path": path},
        "is_error": False,
    }


def test_a_trace_with_no_verification_command_reports_zero_attempts() -> None:
    run = _run([_bash("ls -la"), _read("/repo/src/x.ts"), _grep("foo", "/repo/x.ts")])
    summary = summarize_verification_attempts(run)
    assert summary.attempt_count == 0
    assert not summary.reached_verification


def test_two_invocation_methods_for_the_same_goal_count_as_one_attempt() -> None:
    """The exact run 62 shape: npx vitest run, then (after a permissions
    check that changes nothing) node node_modules/vitest/vitest.mjs run.
    Same attempt at the same thing, not two."""
    run = _run(
        [
            _bash("cat src/lib/mot.ts"),
            _bash("timeout 300 npx vitest run 2>&1 | tail -40"),
            _bash("cat src/lib/aggregator.test.ts"),
            _bash(
                "ls -la node_modules/.bin/ | grep -i vite && "
                "ls -l node_modules/vitest/ | head"
            ),
            _bash(
                "timeout 420 node node_modules/vitest/vitest.mjs run "
                "--reporter=verbose 2>&1 | tail -60"
            ),
            _bash("head -60 CHANGELOG.md"),
        ]
    )
    summary = summarize_verification_attempts(run)
    assert summary.attempt_count == 1
    assert summary.attempts[0].call_indices == [1, 4]


def test_an_edit_between_two_verification_commands_makes_them_separate_attempts() -> None:
    run = _run(
        [
            _bash("npx vitest run 2>&1 | tail -40"),
            _bash("sed -i 's/foo/bar/' src/lib/mot.ts"),
            _bash("npx vitest run 2>&1 | tail -40"),
        ]
    )
    summary = summarize_verification_attempts(run)
    assert summary.attempt_count == 2


def test_a_real_edit_tool_call_also_separates_attempts() -> None:
    run = _run(
        [
            _bash("npx vitest run 2>&1 | tail -40"),
            {"id": "x", "name": "Edit", "input": {}, "is_error": False},
            _bash("npx vitest run 2>&1 | tail -40"),
        ]
    )
    summary = summarize_verification_attempts(run)
    assert summary.attempt_count == 2


def test_grepping_for_the_word_vitest_is_not_an_attempt() -> None:
    """The exact bug this closes: checking whether a test script or the
    word "vitest" exists in package.json is inspection, not an invocation.
    The first version of this heuristic checked for "vitest" and "test"
    appearing anywhere in the command, independently, and miscounted this
    as a real attempt."""
    run = _run(
        [
            _bash('grep -A2 \'"test"\' package.json'),
            _bash('grep -n "vitest" package.json'),
        ]
    )
    summary = summarize_verification_attempts(run)
    assert summary.attempt_count == 0


def test_non_bash_tool_calls_are_never_treated_as_verification() -> None:
    """A Grep call whose pattern happens to be "vitest" is a text search,
    not an execution: only Bash can actually run something."""
    run = _run([_grep("vitest run", "/repo/package.json")])
    summary = summarize_verification_attempts(run)
    assert summary.attempt_count == 0


def test_a_piped_command_never_confirms_completion_either_way() -> None:
    run = _run([_bash("npx vitest run 2>&1 | tail -40", is_error=False)])
    summary = summarize_verification_attempts(run)
    assert summary.attempts[0].completion_unobservable is True
    assert summary.attempts[0].clean_success_observed is False


def test_an_unpiped_successful_command_confirms_completion() -> None:
    run = _run([_bash("npm test", is_error=False)])
    summary = summarize_verification_attempts(run)
    assert summary.attempts[0].completion_unobservable is False
    assert summary.attempts[0].clean_success_observed is True


def test_an_unpiped_errored_command_confirms_it_ran_but_not_that_it_passed() -> None:
    run = _run([_bash("npm test", is_error=True)])
    summary = summarize_verification_attempts(run)
    assert summary.attempts[0].completion_unobservable is False
    assert summary.attempts[0].clean_success_observed is False


def test_one_unpiped_call_in_a_group_is_enough_even_if_others_are_piped() -> None:
    run = _run(
        [
            _bash("npx vitest run 2>&1 | tail -40"),  # piped, unobservable alone
            _bash("npm test", is_error=False),  # same group, unpiped, confirms it ran
        ]
    )
    summary = summarize_verification_attempts(run)
    assert summary.attempt_count == 1
    assert summary.attempts[0].completion_unobservable is False
    assert summary.attempts[0].clean_success_observed is True
