"""Syncing a company profile into the database."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from polska.config.company import CompanyProfile, LoadedProfile, profile_hash
from polska.db.enums import GoalStatus, TaskState, TaskType
from polska.db.models import Company, Goal, Task
from polska.sync import reconcile_removed_companies, sync_company


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


# --------------------------------------------------------------- active flips


def _open_task(session: Session, company: Company, goal_id: int) -> Task:
    task = Task(
        company_id=company.id,
        goal_id=goal_id,
        type=TaskType.MARKETING,
        title="Draft the launch announcement",
        description="x",
        rationale="x",
    )
    session.add(task)
    session.commit()
    return task


def _loaded_with_raw(raw: str, **overrides: object) -> LoadedProfile:
    """Like _loaded, but with an explicit raw string: _loaded's own default
    raw is a function of slug only, so two calls that differ only in some
    other field (active, here) hash identically and sync_company correctly
    treats them as unchanged. A real edited YAML file's content, and
    therefore its hash, always changes when a field does; this is what
    lets a test simulate that instead of an unchanged re-sync."""
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
    return LoadedProfile(
        profile=profile, path="companies/sync-co.yaml", raw=raw, sha256=profile_hash(raw)
    )


def test_deactivating_a_company_abandons_its_open_tasks(session: Session) -> None:
    company = sync_company(session, _loaded())
    goal = session.execute(select(Goal)).scalar_one()
    task = _open_task(session, company, goal.id)

    deactivated = _loaded_with_raw("slug: sync-co  # now inactive", active=False)
    sync_company(session, deactivated)

    session.refresh(task)
    assert task.state == TaskState.ABANDONED
    assert "deactivated" in task.result["abandoned_reason"]


def test_a_company_staying_active_does_not_touch_its_tasks(session: Session) -> None:
    company = sync_company(session, _loaded())
    goal = session.execute(select(Goal)).scalar_one()
    task = _open_task(session, company, goal.id)

    still_active = _loaded_with_raw("slug: sync-co  # a real edit, still active")
    sync_company(session, still_active)

    session.refresh(task)
    assert task.state == TaskState.QUEUED


def test_reactivating_a_company_does_not_re_abandon_anything(session: Session) -> None:
    """Only a true->false flip matters. false->true, or false->false, must
    not run the abandon logic at all."""
    company = sync_company(session, _loaded_with_raw("slug: sync-co  # inactive", active=False))
    goal = session.execute(select(Goal)).scalar_one()
    task = _open_task(session, company, goal.id)

    reactivated = _loaded_with_raw("slug: sync-co  # active again", active=True)
    sync_company(session, reactivated)

    session.refresh(task)
    assert task.state == TaskState.QUEUED  # untouched either way


# ------------------------------------------------------- reconcile_removed_companies


def _write_profile(directory: Path, slug: str, *, malformed: bool = False) -> None:
    if malformed:
        (directory / f"{slug}.yaml").write_text("not: [valid, yaml: at all", encoding="utf-8")
        return
    (directory / f"{slug}.yaml").write_text(
        f"slug: {slug}\nname: {slug.title()}\nidea: Testing reconciliation.\ngoals: []\n",
        encoding="utf-8",
    )


def test_reconcile_abandons_tasks_for_a_company_whose_file_is_gone(
    session: Session, tmp_path: Path
) -> None:
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir()

    loaded = _loaded()
    company = sync_company(session, loaded)
    goal = session.execute(select(Goal)).scalar_one()
    task = _open_task(session, company, goal.id)
    # Deliberately no file written for "sync-co": it's gone.

    abandoned = reconcile_removed_companies(session, companies_dir)

    assert abandoned == 1
    session.refresh(task)
    session.refresh(company)
    assert task.state == TaskState.ABANDONED
    assert company.active is False


def test_reconcile_leaves_a_still_discovered_company_alone(
    session: Session, tmp_path: Path
) -> None:
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir()
    _write_profile(companies_dir, "sync-co")

    loaded = _loaded()
    company = sync_company(session, loaded)
    goal = session.execute(select(Goal)).scalar_one()
    task = _open_task(session, company, goal.id)

    abandoned = reconcile_removed_companies(session, companies_dir)

    assert abandoned == 0
    session.refresh(task)
    assert task.state == TaskState.QUEUED


def test_reconcile_skips_a_company_with_no_abandonable_tasks(
    session: Session, tmp_path: Path
) -> None:
    """No open tasks doesn't mean nothing happens: the company itself still
    needs deactivating, even with an empty queue."""
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir()

    company = sync_company(session, _loaded())

    abandoned = reconcile_removed_companies(session, companies_dir)

    assert abandoned == 0
    session.refresh(company)
    assert company.active is False


def test_reconcile_aborts_the_whole_pass_on_a_malformed_profile(
    session: Session, tmp_path: Path
) -> None:
    """A profile that fails to parse right now might be mid-edit, not
    removed. Abstaining this cycle (and trying again next tick, the same
    retry _tick_all_companies gives a malformed file on its own path) is
    the safe read, not treating the parse failure as absence."""
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir()
    _write_profile(companies_dir, "broken", malformed=True)

    company = sync_company(session, _loaded())  # "sync-co": genuinely gone
    goal = session.execute(select(Goal)).scalar_one()
    task = _open_task(session, company, goal.id)

    abandoned = reconcile_removed_companies(session, companies_dir)

    assert abandoned == 0
    session.refresh(task)
    assert task.state == TaskState.QUEUED  # untouched: the whole pass abstained
