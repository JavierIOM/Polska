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

from sqlalchemy import select
from sqlalchemy.orm import Session

from polska.config.company import LoadedProfile
from polska.db.models import Company, Goal


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
        company.profile_path = loaded.path
        company.profile_hash = loaded.sha256
        company.profile = profile.model_dump(mode="json")
        company.name = profile.name
        company.idea = profile.idea
        company.brand_voice = profile.brand_voice
        company.active = profile.active

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
