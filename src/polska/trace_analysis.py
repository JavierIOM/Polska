"""Derives what actually happened in a run from its own tool-call trace,
rather than trusting what the agent says happened, or waiting for it to say
anything at all.

Built for one specific question the loop bound (see prompts.py's
_VERIFY_LOOP_BOUND) cannot answer on its own: ``AgentResult.iterations_attempted``
is only ever set on a run that finished cleanly, because it is part of the
model's own final structured answer. A run cut off by the mid-stream token
watchdog never produces one, which is exactly the case this project most
wants visibility into. ``Run.tools_called`` is written incrementally as
messages stream in and survives an interruption completely intact (see
``AgentRunner._collect_message``), so it is the one source that is never
missing, whether the run finished, failed, or was cut off mid-thought.

This does not replace ``iterations_attempted``. Where both exist, one is a
self-report and the other is derived from the same trace a human would
read by hand; they should usually agree, and it is worth knowing when they
do not.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from polska.db.models import Run

#: A command is treated as a verification attempt only if it contains one
#: of these *anchored* substrings, an actual invocation shape, not just the
#: word "vitest" and the word "test" appearing anywhere in the command.
#: The first version of this checked for the two words independently, which
#: matched `grep -n "vitest" package.json` next to a *separate* command
#: that happened to grep for `"test"` too -- neither line runs anything, one
#: just confirms a script is wired up and the other confirms the word
#: "vitest" appears in package.json at all, and both are exactly the kind
#: of inspection a research task does while never trying to execute
#: anything. Deliberately a short, explicit list of what has actually been
#: observed, not every test runner that could exist; extend it if a
#: company's own scripts differ.
_VERIFY_PATTERNS = (
    "npx vitest",
    "vitest run",
    "vitest.mjs",
    "npm test",
    "npm run test",
    "yarn test",
    "pnpm test",
)

#: What counts as the agent changing a file, for the purpose of deciding
#: whether two verification attempts are the same attempt (retried a
#: different way) or two genuinely separate cycles. A real Edit/Write tool
#: call is unambiguous; the Bash-only patterns are a heuristic over shell
#: text, not a parser, and are deliberately narrow rather than trying to
#: catch every way a command could write a file.
_EDIT_TOOL_NAMES = frozenset({"Edit", "Write"})
_EDIT_BASH_MARKERS = ("cp ", "mv ", "sed -i", "<<'EOF'", "<<EOF", "<< 'EOF'", "<< EOF")


def _looks_like_a_verification_attempt(command: str) -> bool:
    lowered = command.lower()
    return any(pattern in lowered for pattern in _VERIFY_PATTERNS)


def _looks_like_an_edit(call: dict) -> bool:
    if call["name"] in _EDIT_TOOL_NAMES:
        return True
    if call["name"] != "Bash":
        return False
    command = str(call.get("input", {}).get("command", "")).lower()
    return any(marker in command for marker in _EDIT_BASH_MARKERS)


@dataclass
class VerificationAttempt:
    """One group of tool calls that represent a single logical attempt to
    verify something, however many different ways it was invoked."""

    call_indices: list[int] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    #: True only if at least one call in this group was not piped through
    #: another command (no ``|``) and reported no error -- a piped command's
    #: own exit status reflects the last stage of the pipe, not necessarily
    #: the verification command itself, so a piped call is never treated as
    #: confirming completion either way, only an unpiped one can.
    clean_success_observed: bool = False
    #: True if every call in this group was piped, meaning the trace has no
    #: way to confirm completion versus a failure to start for this attempt
    #: specifically -- distinct from clean_success_observed being False for
    #: an unpiped call that did report an error.
    completion_unobservable: bool = True


@dataclass
class TraceVerificationSummary:
    """What a run's own tool-call trace shows about verification attempts,
    independent of anything the agent's final result claims."""

    run_id: int
    total_tool_calls: int
    attempts: list[VerificationAttempt]
    reached_verification: bool

    @property
    def attempt_count(self) -> int:
        """The number to compare against AgentResult.iterations_attempted,
        when that field exists. Deduplicated: retrying the same verification
        a different way, with no edit in between, counts once."""
        return len(self.attempts)


def summarize_verification_attempts(run: Run) -> TraceVerificationSummary:
    """Derive verification-attempt evidence from ``run.tools_called`` alone.

    Groups consecutive verification-looking calls into one attempt as long
    as no edit-looking call sits between them -- the same "cycle" boundary
    ``_VERIFY_LOOP_BOUND`` describes to the engineer (change, then verify),
    just detected from the outside rather than taken on the model's word.
    Two different ways of invoking the same test runner, tried back to back
    with nothing changed in between, are the same attempt at the same thing,
    not two.

    Works on any run's trace, complete or interrupted: this is the whole
    point of building it against ``tools_called`` rather than the model's
    own final answer, which an interrupted run never produces.
    """
    calls = run.tools_called
    attempts: list[VerificationAttempt] = []
    current: VerificationAttempt | None = None

    for index, call in enumerate(calls):
        if call["name"] != "Bash":
            if _looks_like_an_edit(call):
                current = None  # a real Edit/Write tool call ends the group
            continue

        command = str(call.get("input", {}).get("command", ""))

        if _looks_like_an_edit(call):
            current = None
            continue

        if not _looks_like_a_verification_attempt(command):
            continue

        if current is None:
            current = VerificationAttempt()
            attempts.append(current)

        current.call_indices.append(index)
        current.commands.append(command)
        if "|" in command:
            continue
        current.completion_unobservable = False
        if not call["is_error"]:
            current.clean_success_observed = True

    return TraceVerificationSummary(
        run_id=run.id,
        total_tool_calls=len(calls),
        attempts=attempts,
        reached_verification=bool(attempts),
    )
