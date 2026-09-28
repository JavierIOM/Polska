"""Deduplicates a planner's proposals against work the company already has.

Repeated near-identical work is the main failure mode of systems like this one, so
getting this wrong in either direction matters: too loose and the planner's proposals
get silently swallowed, including genuine new needs; too strict and the same task
gets queued three times a day forever.

The method is deterministic fuzzy matching with an LLM tiebreak, decided in review:
a normalised composite key, scored against every comparison candidate with
``rapidfuzz``'s token-set ratio. At or above ``dedup.high_threshold`` the proposal is
a duplicate and is dropped. At or below ``dedup.low_threshold`` it is novel and is
kept. Only the band between costs a model call, batched through the dedup judge
agent, one call for the whole tick rather than one per ambiguous proposal.

The comparison set itself is state-based, not purely a time window (fixed in review,
see ``polska.db.state``): ``QUEUED``/``RUNNING``/``AWAITING_APPROVAL``/``FAILED``
suppress unconditionally, ``DONE`` suppresses only inside the lookback window, and
``ABANDONED`` never suppresses at any age, because that is the one state that means
the system tried and gave up, and the underlying need is still open.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from polska.config.appconfig import DedupConfig
from polska.db.models import Task
from polska.db.state import DEDUP_LOOKBACK_STATES, DEDUP_SUPPRESSING_STATES
from polska.db.types import utcnow
from polska.runner import AgentRunner
from polska.schemas.planner import ProposedTask


@dataclass(frozen=True, slots=True)
class DedupDecision:
    """What became of one proposed task."""

    keep: bool
    dedup_key: str
    dedup_note: str
    duplicate_of_task_id: int | None = None


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _proposal_key(proposal: ProposedTask) -> str:
    """The string ``_best_match`` fuzzy-compares two tasks by.

    Includes the description, not just the title, because titles for the same
    kind of work are naturally templated ("Document X's silent-failure mode and
    propose a detection signal") and that shared boilerplate can swamp the one
    or two words that actually distinguish the subject. Found live: a real "MOT"
    proposal scored 96 against a completed "DVLA" task on title alone (94 on
    title in isolation), well past ``high_threshold``, purely because eight of
    nine title tokens were the template; their descriptions, which actually name
    the different files and APIs involved, scored 53 against each other.
    Folding the description in moves a case like that from "confidently and
    silently dropped" into the ambiguous band, where the judge actually looks at
    it, rather than trying to force it all the way down to "confidently kept"
    by reweighting further and risking real duplicates slipping through instead.
    """
    return (
        f"{proposal.type.value}:{_normalize(proposal.goal_key)}:"
        f"{_normalize(proposal.title)} {_normalize(proposal.description)}"
    )


def _candidate_key(task: Task) -> str:
    goal_key = task.goal.key if task.goal is not None else ""
    return (
        f"{task.type.value}:{_normalize(goal_key)}:"
        f"{_normalize(task.title)} {_normalize(task.description)}"
    )


def _candidates(session: Session, company_id: int, config: DedupConfig) -> list[Task]:
    """Every task that must be considered for suppression, per the state rules."""
    cutoff = utcnow() - dt.timedelta(days=config.lookback_days)
    stmt = (
        select(Task)
        .options(selectinload(Task.goal))
        .where(Task.company_id == company_id)
        .where(
            Task.state.in_(DEDUP_SUPPRESSING_STATES)
            | (Task.state.in_(DEDUP_LOOKBACK_STATES) & (Task.created_at >= cutoff))
        )
    )
    return list(session.execute(stmt).scalars())


def _best_match(key: str, candidates: list[tuple[Task, str]]) -> tuple[Task, float] | None:
    if not candidates:
        return None
    scored = [
        (task, fuzz.token_set_ratio(key, candidate_key)) for task, candidate_key in candidates
    ]
    return max(scored, key=lambda pair: pair[1])


async def deduplicate(
    session: Session,
    runner: AgentRunner,
    *,
    company_id: int,
    config: DedupConfig,
    proposals: list[ProposedTask],
) -> list[DedupDecision]:
    """One decision per proposal, in the same order they were given.

    Never raises on a judge failure: an ambiguous proposal the judge could not
    reach a verdict on defaults to kept, on the view that silently dropping a
    genuine need is worse than occasionally repeating a small piece of work, which
    is exactly what this deterministic-plus-judge design already tolerates for
    every score sitting in the ambiguous band.
    """
    candidates = _candidates(session, company_id, config)
    candidate_keys = [(task, _candidate_key(task)) for task in candidates]

    decisions: list[DedupDecision | None] = [None] * len(proposals)
    ambiguous: list[tuple[int, ProposedTask, Task, float]] = []

    for index, proposal in enumerate(proposals):
        key = _proposal_key(proposal)
        match = _best_match(key, candidate_keys)

        if match is None:
            decisions[index] = DedupDecision(
                keep=True,
                dedup_key=key,
                dedup_note="No comparable existing task; nothing to match.",
            )
            continue

        candidate, score = match
        if score >= config.high_threshold:
            decisions[index] = DedupDecision(
                keep=False,
                dedup_key=key,
                dedup_note=(
                    f"Duplicate of task {candidate.id} ({candidate.state.value}), "
                    f"score {score:.0f} >= high_threshold {config.high_threshold}."
                ),
                duplicate_of_task_id=candidate.id,
            )
        elif score <= config.low_threshold:
            decisions[index] = DedupDecision(
                keep=True,
                dedup_key=key,
                dedup_note=(
                    f"Novel: best match was task {candidate.id}, score {score:.0f} "
                    f"<= low_threshold {config.low_threshold}."
                ),
            )
        else:
            ambiguous.append((index, proposal, candidate, score))

    if ambiguous and config.judge_enabled:
        await _resolve_ambiguous(session, runner, company_id, config, ambiguous, decisions)
    else:
        for index, proposal, candidate, score in ambiguous:
            key = _proposal_key(proposal)
            reason = (
                "Ambiguous score band, judge disabled by config"
                if not config.judge_enabled
                else "Ambiguous score band, judge produced no usable verdict"
            )
            decisions[index] = DedupDecision(
                keep=True,
                dedup_key=key,
                dedup_note=(
                    f"{reason}: defaulted to kept rather than risk silently dropping "
                    f"a genuine need. Closest match was task {candidate.id}, score "
                    f"{score:.0f}."
                ),
            )

    assert all(d is not None for d in decisions)  # every index was assigned exactly once
    return decisions  # type: ignore[return-value]


async def _resolve_ambiguous(
    session: Session,
    runner: AgentRunner,
    company_id: int,
    config: DedupConfig,
    ambiguous: list[tuple[int, ProposedTask, Task, float]],
    decisions: list[DedupDecision | None],
) -> None:
    from polska.schemas.planner import DedupJudgeOutput

    prompt_lines = [
        "For each proposal below, decide whether it duplicates the existing task "
        "shown beside it, or is genuinely novel work. Return one verdict per index.",
        "",
    ]
    for local_index, (_, proposal, candidate, score) in enumerate(ambiguous):
        prompt_lines.append(
            f"[{local_index}] Proposed: {proposal.title} (type={proposal.type.value}, "
            f"goal={proposal.goal_key})\n"
            f"    {proposal.description}\n"
            f"    Closest existing task #{candidate.id} ({candidate.state.value}, "
            f"fuzzy score {score:.0f}): {candidate.title}\n"
            f"    {candidate.description}"
        )
    user_prompt = "\n\n".join(prompt_lines)

    outcome = await runner.run_dedup_judge(session, company_id=company_id, user_prompt=user_prompt)
    output = outcome.output

    verdict_by_local_index = {}
    if isinstance(output, DedupJudgeOutput):
        verdict_by_local_index = {v.index: v for v in output.verdicts}

    for local_index, (global_index, proposal, candidate, score) in enumerate(ambiguous):
        key = _proposal_key(proposal)
        verdict = verdict_by_local_index.get(local_index)

        if verdict is None:
            decisions[global_index] = DedupDecision(
                keep=True,
                dedup_key=key,
                dedup_note=(
                    "Ambiguous score band, judge produced no usable verdict for this "
                    f"proposal: defaulted to kept. Closest match was task "
                    f"{candidate.id}, score {score:.0f}."
                ),
            )
        elif verdict.is_duplicate:
            decisions[global_index] = DedupDecision(
                keep=False,
                dedup_key=key,
                dedup_note=(
                    f"Judge: duplicate of task {verdict.duplicate_of_task_id}. {verdict.reason}"
                ),
                duplicate_of_task_id=verdict.duplicate_of_task_id,
            )
        else:
            decisions[global_index] = DedupDecision(
                keep=True,
                dedup_key=key,
                dedup_note=f"Judge: novel. {verdict.reason}",
            )
