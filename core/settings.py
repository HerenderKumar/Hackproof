"""Configuration, read from environment variables only (section 9)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class MissingSettingsError(RuntimeError):
    def __init__(self, service: str, names: list[str]) -> None:
        super().__init__(
            f"{service} cannot start: missing environment variables {', '.join(names)}"
        )
        self.names = names


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    # Deployment
    app_env: str = "production"
    public_base_url: str = "http://localhost:3000"

    # Database and sessions
    database_url: str = "postgresql+asyncpg://hp:hp@postgres:5432/hp"
    organiser_password: SecretStr | None = None
    session_secret: SecretStr | None = None
    session_ttl_hours: int = 12
    link_ttl_days: int = 7

    # Models (worker only). Calls go through the AI Credits gateway, an OpenAI-compatible
    # endpoint, with the gateway's own key and model IDs; never straight to Anthropic (D-037).
    llm_base_url: str = "https://api.aicredits.in/v1"
    llm_api_key: SecretStr | None = None
    draft_model: str = "anthropic/claude-sonnet-5.5"
    audit_model: str = "anthropic/claude-haiku-5.5"  # the human's choice, for cost (D-053)
    brief_model: str = "anthropic/claude-sonnet-5.5"
    fast_model: str = "anthropic/claude-haiku-5.5"
    # live calls the gateway; record calls it and saves each reply under llm_replay_dir; replay
    # answers from those recordings and never touches the network (section 20).
    llm_mode: Literal["live", "record", "replay"] = "live"
    llm_timeout_s: int = 60
    llm_replay_dir: Path = Path("fixtures/llm")
    draft_max_tokens: int = 16000
    # Browser tests for UI claims (M11): a second drafting call. Haiku 5.5 drafted F05's test
    # first time for ₹0.10 (D-059); empty DRAFT_UI_MODEL means DRAFT_MODEL. Off, a UI claim is
    # typed and left without a test.
    draft_ui_tests: bool = True
    draft_ui_model: str | None = "anthropic/claude-haiku-5.5"
    # What one team may ask for (M7): each claims version is a model call, each submission a
    # final run.
    max_claims_versions: int = 20
    max_redrafts: int = 3
    max_submissions: int = 10
    audit_max_steps: int = 12
    audit_max_tokens: int = 60000
    audit_timeout_s: int = 120

    # Repositories
    github_token: SecretStr | None = None
    repo_provider: Literal["github", "local"] = "github"
    local_remotes_dir: Path = Path("fixtures/remotes")

    # Builds and runs
    host_artifact_dir: Path | None = None
    buildkit_host: str = "docker-container://buildkit"
    railpack_version: str = "0.43.0"
    runner_image: str = "hackproof-runner:1"
    runner_version: str = "1"
    build_timeout_s: int = 480
    run_timeout_s: int = 600
    startup_timeout_s: int = 60
    app_memory: str = "1g"
    app_cpus: float = 1.0
    app_pids_limit: int = 256
    runner_memory: str = "2g"
    # A run with UI tests (M11): the runner's browser needs more processes and its own CPUs.
    runner_pids_limit: int = 512
    runner_cpus: float = 2.0
    # Chromium's own sandbox (D-058). Off only on a host whose kernel refuses it.
    runner_browser_sandbox: bool = True

    # Kickoff baselines (M9, D-052). BASELINE_KEY encrypts uploaded device code at rest:
    # 64 hex characters, `openssl rand -hex 32`. A part is at most BASELINE_PART_BYTES as sent.
    baseline_key: SecretStr | None = None
    baseline_part_bytes: int = 4194304
    baseline_max_file_bytes: int = 1048576
    baseline_max_bytes: int = 2147483648
    max_devices: int = 12
    match_context_lines: int = 5
    # Stage 5's "possibly related" list, off unless asked for.
    embeddings: Literal["off", "on"] = "off"
    # Measured with the worker's model on the fixtures (D-062): B6's rewritten functions score
    # 0.78 and 0.81, every unrelated pair of B1 to B6 0.575 or less.
    related_min_score: float = 0.70

    # The model door (Prune design): a team's app may call a model during a run, through a door
    # of ours that forwards to Prune under a per-run budget. Off: a run is what M4 built.
    model_door: Literal["off", "prune"] = "off"
    prune_api_key: SecretStr | None = None
    prune_base_url: str = "https://api.withprune.com"
    door_image: str = "hackproof-door:1"
    door_models: str = "gpt-4.1-nano,gpt-4o-mini,gpt-5-nano,claude-haiku-4-5"
    door_run_budget_usd: float = 0.05
    door_max_calls: int = 20
    door_timeout_s: float = 60.0

    # Worker
    watch_concurrency: int = 4
    heavy_concurrency: int = 1
    index_concurrency: int = 1

    log_level: str = "INFO"

    @property
    def ui_draft_model(self) -> str | None:
        """The model that drafts browser tests, or None when UI drafting is off."""
        if not self.draft_ui_tests:
            return None
        return self.draft_ui_model or self.draft_model

    @property
    def door_model_list(self) -> tuple[str, ...]:
        return tuple(m.strip() for m in self.door_models.split(",") if m.strip())

    @property
    def prune_key(self) -> str:
        if not _has_value(self.prune_api_key):
            raise MissingSettingsError("the model door", ["PRUNE_API_KEY"])
        assert self.prune_api_key is not None
        return self.prune_api_key.get_secret_value()

    @field_validator("prune_base_url")
    @classmethod
    def _prune_base_url_is_safe(cls, value: str) -> str:
        url = value.rstrip("/")
        local = url.startswith(("http://localhost", "http://127.0.0.1", "http://prune-stub"))
        if not url.startswith("https://") and not local:
            raise ValueError(
                "PRUNE_BASE_URL must be https, so the Prune key is never sent in clear"
            )
        return url

    @field_validator("llm_base_url")
    @classmethod
    def _llm_base_url_is_safe(cls, value: str) -> str:
        url = value.rstrip("/")
        local = url.startswith(("http://localhost", "http://127.0.0.1"))
        if not url.startswith("https://") and not local:
            raise ValueError("LLM_BASE_URL must be https, so the API key is never sent in clear")
        return url

    @field_validator("host_artifact_dir")
    @classmethod
    def _artifact_dir_is_absolute(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("HOST_ARTIFACT_DIR must be an absolute path")
        return value

    @field_validator("baseline_key")
    @classmethod
    def _baseline_key_is_256_bits(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value():
            return None
        key = value.get_secret_value()
        if len(key) != 64 or any(c not in "0123456789abcdefABCDEF" for c in key):
            raise ValueError("BASELINE_KEY must be 64 hex characters: openssl rand -hex 32")
        return value

    @property
    def baseline_cipher_key(self) -> bytes:
        if self.baseline_key is None:
            raise MissingSettingsError("kickoff baselines", ["BASELINE_KEY"])
        return bytes.fromhex(self.baseline_key.get_secret_value())

    @property
    def is_local(self) -> bool:
        return self.app_env == "local"

    @property
    def artifact_dir(self) -> Path:
        if self.host_artifact_dir is None:
            raise MissingSettingsError("artifact storage", ["HOST_ARTIFACT_DIR"])
        return self.host_artifact_dir

    def require_for_api(self) -> None:
        missing = []
        if self.organiser_password is None or not self.organiser_password.get_secret_value():
            missing.append("ORGANISER_PASSWORD")
        if self.session_secret is None or not self.session_secret.get_secret_value():
            missing.append("SESSION_SECRET")
        # The API checks a team's repository with the provider when it is registered.
        if self.repo_provider == "github" and not _has_value(self.github_token):
            missing.append("GITHUB_TOKEN")
        if self.baseline_key is None:
            missing.append("BASELINE_KEY")  # the scan routes encrypt what devices upload
        if missing:
            raise MissingSettingsError("api", missing)

    def require_for_worker(self) -> None:
        missing = []
        if self.host_artifact_dir is None:
            missing.append("HOST_ARTIFACT_DIR")
        if self.llm_mode in ("live", "record") and not _has_value(self.llm_api_key):
            missing.append("LLM_API_KEY")
        if self.repo_provider == "github" and not _has_value(self.github_token):
            missing.append("GITHUB_TOKEN")
        if self.baseline_key is None:
            missing.append("BASELINE_KEY")  # indexing and comparing read the encrypted parts
        if self.model_door == "prune" and not _has_value(self.prune_api_key):
            missing.append("PRUNE_API_KEY")  # MODEL_DOOR=prune: each run's door holds it
        if missing:
            raise MissingSettingsError("worker", missing)


def _has_value(secret: SecretStr | None) -> bool:
    return secret is not None and bool(secret.get_secret_value())


@lru_cache
def get_settings() -> Settings:
    return Settings()
