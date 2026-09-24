"""Config and company profile validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from polska.config.appconfig import AppConfig, BudgetConfig, DedupConfig, load_app_config
from polska.config.company import CompanyProfile, load_company_profile, profile_hash
from polska.db.enums import AgentName

REPO_ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------- shipped files


def test_the_shipped_config_is_valid(app_config: AppConfig) -> None:
    assert app_config.scheduler.interval_hours == 4
    assert set(app_config.agents) == set(AgentName)


def test_the_example_company_profile_is_valid() -> None:
    loaded = load_company_profile(REPO_ROOT / "companies" / "example.yaml")
    assert loaded.profile.slug == "example-co"
    assert len(loaded.profile.open_goals) == 3
    assert loaded.sha256 == profile_hash(loaded.raw)


def test_every_agent_has_an_explicit_tool_allowlist(app_config: AppConfig) -> None:
    """No agent may inherit tools. An empty list is fine, an absent one is not."""
    for name, agent in app_config.agents.items():
        assert isinstance(agent.tools, list), name


def test_the_planner_and_judge_hold_no_tools(app_config: AppConfig) -> None:
    """They decide, they do not act. A tool on either is a design error."""
    assert app_config.agents[AgentName.PLANNER].tools == []
    assert app_config.agents[AgentName.DEDUP_JUDGE].tools == []


def test_no_worker_agent_can_push_or_send_directly(app_config: AppConfig) -> None:
    """External effects go through the gate, never through a tool the agent holds."""
    forbidden = {"WebFetchPost", "SendEmail", "GitPush", "Publish"}
    for name, agent in app_config.agents.items():
        assert not forbidden & set(agent.tools), name


def test_the_support_agent_cannot_write_anywhere(app_config: AppConfig) -> None:
    """Support drafts replies into its result payload. It has no business on disk."""
    assert set(app_config.agents[AgentName.SUPPORT].tools) <= {"Read", "Grep", "Glob"}


def test_integrations_ship_in_dry_run(app_config: AppConfig) -> None:
    """Phase 1 has no real adapters, so the global switch must be on."""
    assert app_config.integrations.force_dry_run
    assert app_config.integrations.default_adapter == "dry_run"


# ------------------------------------------------------------------------- budgets


def test_a_daily_ceiling_above_the_lifetime_one_is_refused() -> None:
    with pytest.raises(ValidationError, match="inside a single day"):
        BudgetConfig(max_tokens_per_day=100, max_tokens_per_company=50)


def test_a_run_ceiling_above_the_daily_one_is_refused() -> None:
    with pytest.raises(ValidationError, match="exhaust the day"):
        BudgetConfig(max_tokens_per_run=2_000_000, max_tokens_per_day=1_000_000)


def test_a_zero_fx_rate_is_refused() -> None:
    with pytest.raises(ValidationError):
        BudgetConfig(usd_to_gbp=0)


def test_pricing_maths(app_config: AppConfig) -> None:
    """One million input tokens on Opus 5 is five dollars, by the shipped rates."""
    price = app_config.price_for("claude-opus-5")
    assert price.cost_usd(input_tokens=1_000_000, output_tokens=0) == pytest.approx(5.0)
    assert price.cost_usd(input_tokens=0, output_tokens=1_000_000) == pytest.approx(25.0)
    assert price.cost_usd(
        input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000
    ) == pytest.approx(0.5)
    assert price.cost_usd(
        input_tokens=0, output_tokens=0, cache_creation_tokens=1_000_000
    ) == pytest.approx(6.25)


def test_an_unpriced_model_is_refused(app_config: AppConfig) -> None:
    with pytest.raises(KeyError, match="No pricing configured"):
        app_config.price_for("claude-imaginary-9")


def test_an_agent_on_an_unpriced_model_fails_to_load(tmp_path: Path) -> None:
    """The budget guard cannot cost what it has no rate for, so it refuses to start."""
    raw = yaml.safe_load((REPO_ROOT / "config" / "default.yaml").read_text(encoding="utf-8"))
    raw["agents"]["analyst"]["model"] = "claude-nonexistent-1"
    path = tmp_path / "broken.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ValidationError, match="no entry under 'pricing'"):
        load_app_config(path)


def test_a_missing_agent_fails_to_load(tmp_path: Path) -> None:
    raw = yaml.safe_load((REPO_ROOT / "config" / "default.yaml").read_text(encoding="utf-8"))
    del raw["agents"]["marketer"]
    path = tmp_path / "missing-agent.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ValidationError, match="marketer"):
        load_app_config(path)


def test_an_unknown_config_key_fails_to_load(tmp_path: Path) -> None:
    """A typo must not be silently ignored into a default."""
    raw = yaml.safe_load((REPO_ROOT / "config" / "default.yaml").read_text(encoding="utf-8"))
    raw["limits"]["max_concurent_tasks"] = 9  # deliberate typo
    path = tmp_path / "typo.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ValidationError):
        load_app_config(path)


# --------------------------------------------------------------------------- dedup


def test_dedup_thresholds_must_leave_a_band() -> None:
    with pytest.raises(ValidationError, match="no band left"):
        DedupConfig(low_threshold=90, high_threshold=90)


def test_dedup_defaults_match_the_agreed_bands(app_config: AppConfig) -> None:
    assert app_config.dedup.high_threshold == 90
    assert app_config.dedup.low_threshold == 60
    assert app_config.dedup.lookback_days == 14


# ------------------------------------------------------------------ company profile


def _profile(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "slug": "test-co",
        "name": "Test Co",
        "idea": "Selling things.",
        "goals": [{"key": "list", "title": "Grow the list", "metric": "subs", "target": 500}],
    }
    base.update(overrides)
    return base


def test_a_minimal_profile_validates() -> None:
    profile = CompanyProfile.model_validate(_profile())
    assert profile.active
    assert profile.constraints == []


def test_an_uppercase_slug_is_refused() -> None:
    with pytest.raises(ValidationError, match="lowercase"):
        CompanyProfile.model_validate(_profile(slug="Test_Co"))


def test_duplicate_goal_keys_are_refused() -> None:
    goals = [
        {"key": "list", "title": "Grow the list", "metric": "subs", "target": 500},
        {"key": "list", "title": "Grow it more", "metric": "subs", "target": 900},
    ]
    with pytest.raises(ValidationError, match="Duplicate goal key"):
        CompanyProfile.model_validate(_profile(goals=goals))


def test_a_zero_target_goal_is_refused() -> None:
    goals = [{"key": "x", "title": "Do nothing", "metric": "nothing", "target": 0}]
    with pytest.raises(ValidationError, match="greater than zero"):
        CompanyProfile.model_validate(_profile(goals=goals))


def test_a_credential_in_an_integration_option_is_refused() -> None:
    """Profiles are tracked in git. A pasted token must not survive validation."""
    integrations = {
        "github": {"adapter": "dry_run", "options": {"repo": "x/y", "token": "ghp_xxx"}}
    }
    with pytest.raises(ValidationError, match="looks like a credential"):
        CompanyProfile.model_validate(_profile(integrations=integrations))


def test_naming_the_env_var_is_the_supported_way() -> None:
    integrations = {
        "github": {
            "adapter": "dry_run",
            "options": {"repo": "x/y"},
            "secret_env": ["GITHUB_TOKEN"],
        }
    }
    profile = CompanyProfile.model_validate(_profile(integrations=integrations))
    assert profile.integrations["github"].secret_env == ["GITHUB_TOKEN"]


def test_an_empty_profile_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "empty.yaml"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="is empty"):
        load_company_profile(path)


def test_a_profile_is_read_as_utf8(tmp_path: Path) -> None:
    """Python 3.12 on Windows defaults to cp1252, which would mangle the brief."""
    path = tmp_path / "accents.yaml"
    path.write_text(
        yaml.safe_dump(_profile(name="Café £49 Ltd"), allow_unicode=True),
        encoding="utf-8",
    )
    loaded = load_company_profile(path)
    assert loaded.profile.name == "Café £49 Ltd"
