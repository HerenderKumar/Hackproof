from pathlib import Path

import pytest

from core.settings import MissingSettingsError, Settings


def make(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_defaults_match_the_brief(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DATABASE_URL", "LLM_MODE", "REPO_PROVIDER", "BUILD_TIMEOUT_S"):
        monkeypatch.delenv(name, raising=False)
    s = make()
    assert s.database_url == "postgresql+asyncpg://hp:hp@postgres:5432/hp"
    assert s.session_ttl_hours == 12
    assert s.link_ttl_days == 7
    assert s.draft_model == "claude-sonnet-5-5"
    assert s.fast_model == "claude-haiku-5-5"
    assert s.buildkit_host == "docker-container://buildkit"
    assert s.runner_image == "hackproof-runner:1"
    assert (s.build_timeout_s, s.run_timeout_s, s.startup_timeout_s) == (480, 600, 60)
    assert (s.app_memory, s.app_cpus, s.app_pids_limit, s.runner_memory) == ("1g", 1.0, 256, "2g")
    assert (s.watch_concurrency, s.heavy_concurrency) == (4, 1)
    assert (s.audit_max_steps, s.audit_max_tokens, s.audit_timeout_s) == (12, 60000, 120)
    assert s.llm_mode == "live"
    assert s.repo_provider == "github"


def test_values_come_from_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEAVY_CONCURRENCY", "3")
    monkeypatch.setenv("LLM_MODE", "replay")
    s = make()
    assert s.heavy_concurrency == 3
    assert s.llm_mode == "replay"


def test_unknown_llm_mode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MODE", "improvise")
    with pytest.raises(ValueError):
        make()


def test_api_requires_organiser_password_and_session_secret() -> None:
    with pytest.raises(MissingSettingsError) as err:
        make(organiser_password=None, session_secret=None).require_for_api()
    assert "ORGANISER_PASSWORD" in str(err.value)
    assert "SESSION_SECRET" in str(err.value)


def test_api_requirements_pass_when_secrets_are_set() -> None:
    make(organiser_password="pw", session_secret="s" * 32).require_for_api()


def test_worker_requires_artifact_dir_and_live_llm_key() -> None:
    with pytest.raises(MissingSettingsError) as err:
        make(host_artifact_dir=None, anthropic_api_key=None, llm_mode="live").require_for_worker()
    assert "HOST_ARTIFACT_DIR" in str(err.value)
    assert "ANTHROPIC_API_KEY" in str(err.value)


def test_worker_does_not_need_a_claude_key_in_replay_mode(tmp_path: Path) -> None:
    make(
        host_artifact_dir=tmp_path, anthropic_api_key=None, llm_mode="replay", repo_provider="local"
    ).require_for_worker()


def test_artifact_dir_must_be_absolute() -> None:
    with pytest.raises(ValueError):
        make(host_artifact_dir="relative/dir")


def test_secrets_are_not_shown_in_repr() -> None:
    s = make(organiser_password="hunter2", session_secret="topsecret")
    assert "hunter2" not in repr(s)
    assert "topsecret" not in repr(s)
