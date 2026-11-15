from pathlib import Path

import pytest

from core.settings import MissingSettingsError, Settings

KEY = "ab" * 32  # a BASELINE_KEY: 64 hex characters


def make(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_defaults_match_the_brief(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "DATABASE_URL", "LLM_MODE", "REPO_PROVIDER", "BUILD_TIMEOUT_S",
        "LLM_BASE_URL", "DRAFT_MODEL", "AUDIT_MODEL", "BRIEF_MODEL", "FAST_MODEL",
        "LLM_REPLAY_DIR", "DRAFT_MAX_TOKENS",
        "MAX_CLAIMS_VERSIONS", "MAX_REDRAFTS", "MAX_SUBMISSIONS",
    ):  # fmt: skip
        monkeypatch.delenv(name, raising=False)
    s = make()
    assert s.database_url == "postgresql+asyncpg://hp:hp@postgres:5432/hp"
    assert s.session_ttl_hours == 12
    assert s.link_ttl_days == 7
    # Models are reached through the AI Credits gateway, under its model IDs (D-037).
    assert s.llm_base_url == "https://api.aicredits.in/v1"
    assert (s.draft_model, s.brief_model) == ("anthropic/claude-sonnet-5.5",) * 2
    assert (s.audit_model, s.fast_model) == ("anthropic/claude-haiku-5.5",) * 2  # D-053
    assert s.buildkit_host == "docker-container://buildkit"
    assert s.runner_image == "hackproof-runner:1"
    assert (s.build_timeout_s, s.run_timeout_s, s.startup_timeout_s) == (480, 600, 60)
    assert (s.app_memory, s.app_cpus, s.app_pids_limit, s.runner_memory) == ("1g", 1.0, 256, "2g")
    assert (s.watch_concurrency, s.heavy_concurrency) == (4, 1)
    assert (s.audit_max_steps, s.audit_max_tokens, s.audit_timeout_s) == (12, 60000, 120)
    assert s.llm_mode == "live"
    assert (s.llm_replay_dir, s.draft_max_tokens) == (Path("fixtures/llm"), 16000)
    # What one team may ask of the model and the worker (M7).
    assert (s.max_claims_versions, s.max_redrafts, s.max_submissions) == (20, 3, 10)
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
    make(
        organiser_password="pw", session_secret="s" * 32, repo_provider="local", baseline_key=KEY
    ).require_for_api()


def test_worker_requires_artifact_dir_and_live_llm_key() -> None:
    with pytest.raises(MissingSettingsError) as err:
        make(host_artifact_dir=None, llm_api_key=None, llm_mode="live").require_for_worker()
    assert "HOST_ARTIFACT_DIR" in str(err.value)
    assert "LLM_API_KEY" in str(err.value)


def test_recording_calls_the_model_so_it_needs_the_key(tmp_path: Path) -> None:
    with pytest.raises(MissingSettingsError, match="LLM_API_KEY"):
        make(
            host_artifact_dir=tmp_path, llm_api_key=None, llm_mode="record", repo_provider="local"
        ).require_for_worker()


def test_worker_does_not_need_an_llm_key_in_replay_mode(tmp_path: Path) -> None:
    make(
        host_artifact_dir=tmp_path,
        llm_api_key=None,
        llm_mode="replay",
        repo_provider="local",
        baseline_key=KEY,
    ).require_for_worker()


def test_an_anthropic_key_is_never_used_as_the_llm_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Model calls go through the gateway with its own key, not straight to Anthropic (D-037).
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-used")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    settings = make(
        host_artifact_dir=tmp_path, llm_mode="live", repo_provider="local", baseline_key=KEY
    )

    assert not hasattr(settings, "anthropic_api_key")
    with pytest.raises(MissingSettingsError) as missing:
        settings.require_for_worker()
    assert missing.value.names == ["LLM_API_KEY"]


def test_the_llm_base_url_is_normalised_and_must_not_send_the_key_in_clear_text() -> None:
    assert make(llm_base_url="https://api.aicredits.in/v1/").llm_base_url == (
        "https://api.aicredits.in/v1"
    )
    assert make(llm_base_url="http://localhost:9999/v1").llm_base_url == "http://localhost:9999/v1"
    for url in ("http://api.aicredits.in/v1", "api.aicredits.in/v1", "ftp://example.com"):
        with pytest.raises(ValueError):
            make(llm_base_url=url)


def test_artifact_dir_must_be_absolute() -> None:
    with pytest.raises(ValueError):
        make(host_artifact_dir="relative/dir")


def test_secrets_are_not_shown_in_repr() -> None:
    s = make(organiser_password="hunter2", session_secret="topsecret", llm_api_key="sk-gateway")
    assert "hunter2" not in repr(s)
    assert "topsecret" not in repr(s)
    assert "sk-gateway" not in repr(s)


def test_the_api_needs_a_github_token_to_validate_github_repositories() -> None:
    base = {"organiser_password": "pw", "session_secret": "s" * 32, "baseline_key": KEY}

    make(**base, repo_provider="local").require_for_api()
    make(**base, repo_provider="github", github_token="t").require_for_api()
    with pytest.raises(MissingSettingsError) as missing:
        make(**base, repo_provider="github", github_token=None).require_for_api()

    assert missing.value.names == ["GITHUB_TOKEN"]


def test_the_baseline_key_must_be_64_hex_characters() -> None:
    assert make(baseline_key=KEY).baseline_cipher_key == bytes.fromhex(KEY)
    for bad in ("ab" * 31, "zz" * 32, KEY + "00"):
        with pytest.raises(ValueError, match="BASELINE_KEY"):
            make(baseline_key=bad)
    with pytest.raises(MissingSettingsError, match="BASELINE_KEY"):
        _ = make(baseline_key=None).baseline_cipher_key


def test_the_api_and_the_worker_need_the_baseline_key(tmp_path: Path) -> None:
    api = make(organiser_password="pw", session_secret="s" * 32, repo_provider="local")
    with pytest.raises(MissingSettingsError, match="BASELINE_KEY"):
        api.require_for_api()
    worker = make(host_artifact_dir=tmp_path, llm_mode="replay", repo_provider="local")
    with pytest.raises(MissingSettingsError, match="BASELINE_KEY"):
        worker.require_for_worker()


def test_the_baseline_limits_default_to_the_plan() -> None:
    s = make()
    assert (s.baseline_part_bytes, s.baseline_max_file_bytes, s.baseline_max_bytes) == (
        4194304, 1048576, 2147483648,
    )  # fmt: skip
    assert (s.max_devices, s.match_context_lines, s.index_concurrency) == (12, 5, 1)
    assert (s.embeddings, s.related_min_score) == ("off", 0.80)
