"""Runtime configuration from environment variables (prefix ASQL_) or a .env file."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ASQL_", env_file=".env", extra="ignore")

    db_host: str = "localhost"
    db_port: int = 5433
    db_name: str = "portfolio"
    admin_user: str = "portfolio_admin"
    admin_password: SecretStr = SecretStr("admin-local-only")
    reader_user: str = "nl2sql_reader"
    reader_password: SecretStr = SecretStr("reader-local-only")
    app_user: str = "nl2sql_app"
    app_password: SecretStr = SecretStr("app-local-only")

    semantic_layer_path: Path = PROJECT_ROOT / "config" / "semantic_layer.yaml"
    max_rows: int = 500
    statement_timeout_ms: int = 5_000
    max_repair_attempts: int = 1

    # Read from ANTHROPIC_API_KEY (environment or .env). When unset, the SDK falls back to
    # its own credential chain.
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    use_fallbacks: bool = True

    def dsn(self, user: str, password: SecretStr) -> str:
        return (
            f"host={self.db_host} port={self.db_port} dbname={self.db_name} "
            f"user={user} password={password.get_secret_value()}"
        )

    @property
    def admin_dsn(self) -> str:
        return self.dsn(self.admin_user, self.admin_password)

    @property
    def reader_dsn(self) -> str:
        return self.dsn(self.reader_user, self.reader_password)

    @property
    def app_dsn(self) -> str:
        return self.dsn(self.app_user, self.app_password)
