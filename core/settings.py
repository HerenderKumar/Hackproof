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

    # Models (worker only)
    anthropic_api_key: SecretStr | None = None
    draft_model: str = "claude-sonnet-5-5"
    audit_model: str = "claude-sonnet-5-5"
    brief_model: str = "claude-sonnet-5-5"
    fast_model: str = "claude-haiku-5-5"
    llm_mode: Literal["live", "replay"] = "live"
    llm_timeout_s: int = 60
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

    # Worker
    watch_concurrency: int = 4
    heavy_concurrency: int = 1

    log_level: str = "INFO"

    @field_validator("host_artifact_dir")
    @classmethod
    def _artifact_dir_is_absolute(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("HOST_ARTIFACT_DIR must be an absolute path")
        return value

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
        if missing:
            raise MissingSettingsError("api", missing)

    def require_for_worker(self) -> None:
        missing = []
        if self.host_artifact_dir is None:
            missing.append("HOST_ARTIFACT_DIR")
        if self.llm_mode == "live" and not _has_value(self.anthropic_api_key):
            missing.append("ANTHROPIC_API_KEY")
        if self.repo_provider == "github" and not _has_value(self.github_token):
            missing.append("GITHUB_TOKEN")
        if missing:
            raise MissingSettingsError("worker", missing)


def _has_value(secret: SecretStr | None) -> bool:
    return secret is not None and bool(secret.get_secret_value())


@lru_cache
def get_settings() -> Settings:
    return Settings()
