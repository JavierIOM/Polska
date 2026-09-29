"""Syncs a company's profile YAML into the database.

The YAML is the source of truth for the idea, voice, constraints and a goal's
descriptive fields (title, description, metric, target, unit, priority): these
refresh from the profile on every sync. Once a goal exists, ``current_value`` and
``status`` are database-owned: they evolve from real tracked progress, not from
re-reading a static file, so a re-sync never resets a goal a real update has moved
on from. Called at the start of every orchestrator tick, which is cheap (a handful
of rows) and means an edited profile takes effect without a restart.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from polska.activity import log
from polska.config.company import LoadedProfile, discover_profiles, load_company_profile
from polska.db.enums import ActivityKind, TaskState
from polska.db.models import Company, Goal, Task
from polska.db.state import can_transition

logger = logging.getLogger("polska.sync")


def sync_company(session: Session, loaded: LoadedProfile) -> Company:
    """Create or update the ``Company`` and its ``Goal`` rows from ``loaded``."""
    profile = loaded.profile
    company = session.execute(
        select(Company).where(Company.slug == profile.slug)
    ).scalar_one_or_none()

    if company is None:
        company = Company(
            slug=profile.slug,
            name=profile.name,
            profile_path=loaded.path,
            profile_hash=loaded.sha256,
            profile=profile.model_dump(mode="json"),
            idea=profile.idea,
            brand_voice=profile.brand_voice,
            active=profile.active,
        )
        session.add(company)
        session.flush()
    elif company.profile_hash != loaded.sha256:
        was_active = company.active
        company.profile_path = loaded.path
        company.profile_hash = loaded.sha256
        company.profile = profile.model_dump(mode="json")
        company.name = profile.name
        company.idea = profile.idea
        company.brand_voice = profile.brand_voice
        company.active = profile.active
        if was_active and not company.active:
            # An explicit active: false edit, not a removed file (see
            # reconcile_removed_companies for that). Same consequence either
            # way: a company that stops being active must stop having open
            # tasks that look dispatchable, not just stop being ticked.
            _abandon_open_tasks_for_inactive_company(
                session,
                company,
                f"Company '{company.slug}' was deactivated (active: false in its profile).",
            )

    existing_by_key = {
        goal.key: goal
        for goal in session.execute(select(Goal).where(Goal.company_id == company.id)).scalars()
    }

    for spec in profile.goals:
        goal = existing_by_key.get(spec.key)
        if goal is None:
            session.add(
                Goal(
                    company_id=company.id,
                    key=spec.key,
                    title=spec.title,
                    description=spec.description,
                    metric=spec.metric,
                    target_value=spec.target,
                    current_value=spec.current,
                    unit=spec.unit,
                    priority=spec.priority,
                    status=spec.status,
                )
            )
        else:
            goal.title = spec.title
            goal.description = spec.description
            goal.metric = spec.metric
            goal.target_value = spec.target
            goal.unit = spec.unit
            goal.priority = spec.priority

    session.commit()
    return company


def _abandon_open_tasks_for_inactive_company(
    session: Session, company: Company, reason: str
) -> int:
    """Abandon every task under ``company`` that could still be dispatched or
    resumed, now that it is inactive. Returns how many were touched.

    Skips anything already ``done``/``abandoned``/``blocked``: those are
    already concluded, and this is never what decides that. Does not commit;
    callers own the transaction, since both of this function's callers do
    other work in the same one.
    """
    touched = 0
    tasks = session.execute(select(Task).where(Task.company_id == company.id)).scalars().all()
    for task in tasks:
        if not can_transition(task.state, TaskState.ABANDONED):
            continue
        task.transition_to(TaskState.ABANDONED, result={"abandoned_reason": reason})
        touched += 1
    if touched:
        log(
            session,
            company_id=company.id,
            kind=ActivityKind.TASK_STATE_CHANGED,
            summary=f"{company.slug} deactivated: {touched} open task(s) abandoned. {reason}",
        )
    return touched


def reconcile_removed_companies(session: Session, companies_dir: str | Path) -> int:
    """Deactivate, and abandon the open tasks of, any company whose profile
    file no longer exists in ``companies_dir``. Returns how many tasks were
    abandoned across every such company.

    Call once per tick cycle, before ticking any discovered profile, not
    once per company: this is a diff against every active company in the
    database, unrelated to any single tick.

    The gap this closes: dispatch only ever happens inside a per-company
    tick, itself only ever entered for a company whose profile is currently
    discovered (see main.py / cli.py), so a queued task under a removed
    company cannot currently be dispatched through the normal path. Found
    live: that protection was never a decision anyone made, it was an
    accident of where dispatch happens to live, and it stops holding the
    moment that code is refactored without anyone knowing this was relied
    on. This makes "gone" an explicit, durable database fact -- ``active``
    turned off, open tasks abandoned with a stated reason -- rather than an
    implicit consequence of where a loop happens to iterate.
    """
    loaded_slugs: set[str] = set()
    for path in discover_profiles(companies_dir):
        try:
            loaded_slugs.add(load_company_profile(path).profile.slug)
        except Exception:
            # A profile that fails to parse right now might just have a
            # typo, not have actually been removed -- and this function's
            # whole point is to tell those two apart correctly. Abstaining
            # entirely this cycle (rather than treating the broken file as
            # simply "not present", which _tick_all_companies's own
            # per-profile handling would do safely for a normal tick) is the
            # conservative choice: it costs one cycle's delay, on the same
            # profile _tick_all_companies would also skip and retry, rather
            # than risk abandoning a company whose file is only briefly
            # broken.
            logger.warning(
                "Could not parse %s while checking for removed companies; "
                "skipping this reconciliation pass entirely rather than risk "
                "treating a temporarily broken profile as a removed one.",
                path,
            )
            return 0

    abandoned = 0
    touched_any_company = False
    companies = session.execute(select(Company).where(Company.active.is_(True))).scalars().all()
    for company in companies:
        if company.slug in loaded_slugs:
            continue
        company.active = False
        touched_any_company = True
        abandoned += _abandon_open_tasks_for_inactive_company(
            session,
            company,
            f"Company '{company.slug}' profile file no longer found in {companies_dir}.",
        )
    if touched_any_company:
        session.commit()
    return abandoned
