"""Syncing a company profile into the database."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from polska.config.company import CompanyProfile, LoadedProfile, profile_hash
from polska.db.enums import GoalStatus
from polska.db.models import Company, Goal
from polska.sync import sync_company


def _loaded(**overrides: object) -> LoadedProfile:
    base: dict[str, object] = {
        "slug": "sync-co",
        "name": "Sync Co",
        "idea": "Testing sync.",
        "goals": [
            {
                "key": "growth",
                "title": "Grow the list",
                "metric": "subs",
                "target": 500,
                "current": 10,
            }
        ],
    }
    base.update(overrides)
    profile = CompanyProfile.model_validate(base)
    raw = f"slug: {profile.slug}"  # not real YAML, just needs to hash consistently
    return LoadedProfile(
        profile=profile, path="companies/sync-co.yaml", raw=raw, sha256=profile_hash(raw)
    )


def test_first_sync_creates_the_company_and_its_goals(session: Session) -> None:
    company = sync_company(session, _loaded())

    assert company.slug == "sync-co"
    goal = session.execute(select(Goal).where(Goal.company_id == company.id)).scalar_one()
    assert goal.key == "growth"
    assert goal.current_value == 10
    assert goal.target_value == 500


def test_a_second_sync_with_the_same_hash_does_not_touch_current_value(session: Session) -> None:
    loaded = _loaded()
    sync_company(session, loaded)

    goal = session.execute(select(Goal)).scalar_one()
    goal.current_value = 250  # simulates real-world progress tracking
    session.commit()

    sync_company(session, loaded)

    session.refresh(goal)
    assert goal.current_value == 250


def test_an_edited_profile_refreshes_descriptive_fields_but_not_current_value_or_status(
    session: Session,
) -> None:
    original = _loaded()
    sync_company(session, original)

    goal = session.execute(select(Goal)).scalar_one()
    goal.current_value = 250
    goal.status = GoalStatus.PAUSED  # a human paused it in the database
    session.commit()

    edited_raw = "slug: sync-co  # edited"
    edited = LoadedProfile(
        profile=CompanyProfile.model_validate(
            {
                "slug": "sync-co",
                "name": "Sync Co",
                "idea": "Testing sync.",
                "goals": [
                    {
                        "key": "growth",
                        "title": "Grow the list to 1000",  # title edited in the YAML
                        "metric": "subs",
                        "target": 1000,  # target edited too
                        "current": 10,  # current in the YAML is stale/irrelevant
                    }
                ],
            }
        ),
        path="companies/sync-co.yaml",
        raw=edited_raw,
        sha256=profile_hash(edited_raw),
    )
    sync_company(session, edited)

    session.refresh(goal)
    assert goal.title == "Grow the list to 1000"
    assert goal.target_value == 1000
    # Database-owned once the goal exists: a static file edit must not silently
    # undo tracked progress or a deliberate pause.
    assert goal.current_value == 250
    assert goal.status == GoalStatus.PAUSED


def test_a_new_goal_added_to_an_existing_profile_is_created_alongside_the_old_one(
    session: Session,
) -> None:
    sync_company(session, _loaded())

    raw = "slug: sync-co  # two goals"
    two_goals = LoadedProfile(
        profile=CompanyProfile.model_validate(
            {
                "slug": "sync-co",
                "name": "Sync Co",
                "idea": "Testing sync.",
                "goals": [
                    {"key": "growth", "title": "Grow the list", "metric": "subs", "target": 500},
                    {
                        "key": "repeat",
                        "title": "Repeat purchases",
                        "metric": "rate",
                        "target": 0.25,
                    },
                ],
            }
        ),
        path="companies/sync-co.yaml",
        raw=raw,
        sha256=profile_hash(raw),
    )
    sync_company(session, two_goals)

    company = session.execute(select(Company).where(Company.slug == "sync-co")).scalar_one()
    keys = {
        g.key for g in session.execute(select(Goal).where(Goal.company_id == company.id)).scalars()
    }
    assert keys == {"growth", "repeat"}


def test_sync_is_idempotent_within_the_same_hash(session: Session) -> None:
    loaded = _loaded()
    sync_company(session, loaded)
    sync_company(session, loaded)
    sync_company(session, loaded)

    assert session.execute(select(Company)).scalars().all().__len__() == 1
    assert session.execute(select(Goal)).scalars().all().__len__() == 1
