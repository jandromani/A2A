from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Agentic Platform"
    environment: Literal["development", "production", "test"] = "development"
    public_url: str = "http://localhost:8000"
    database_url: str = "sqlite:///./data/agentic.db"
    redis_url: str | None = None
    opa_url: str | None = None
    connector_allowed_hosts: str = ""
    auth_mode: Literal["dev", "api_key", "oidc"] = "dev"
    api_bearer_token: str | None = None
    oidc_issuer_url: str | None = None
    oidc_audience: str | None = None
    model_base_url: str | None = None
    model_api_key: str | None = None
    model_name: str | None = None
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None
    embedding_model: str | None = None
    jira_base_url: str | None = None
    jira_email: str | None = None
    jira_api_token: str | None = None
    max_artifact_bytes: int = 300_000
    max_task_artifacts: int = 10

    @model_validator(mode="after")
    def validate_auth_configuration(self) -> "Settings":
        if self.environment == "production" and self.auth_mode == "dev":
            raise ValueError("AUTH_MODE=dev is not allowed in production.")
        if self.auth_mode == "api_key" and not self.api_bearer_token:
            raise ValueError("API_BEARER_TOKEN is required when AUTH_MODE=api_key.")
        if self.auth_mode == "oidc" and not (self.oidc_issuer_url and self.oidc_audience):
            raise ValueError("OIDC_ISSUER_URL and OIDC_AUDIENCE are required when AUTH_MODE=oidc.")
        return self


settings = Settings()
