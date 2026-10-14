from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        secrets_dir="/run/secrets" if Path("/run/secrets").is_dir() else None,
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Agentic Platform"
    environment: Literal["development", "production", "test"] = "development"
    public_url: str = "http://localhost:8000"
    database_url: str = "sqlite:///./data/agentic.db"
    redis_url: str | None = None
    event_worker_enabled: bool = False
    event_consumer_group: str = "agentic-workers"
    event_consumer_name: str = "agentic-worker-1"
    task_execution_mode: Literal["sync", "queue"] = "sync"
    event_poll_interval_seconds: int = Field(default=1, ge=1, le=60)
    opa_url: str | None = None
    connector_allowed_hosts: str = ""
    auth_mode: Literal["dev", "api_key", "oidc"] = "dev"
    api_bearer_token: SecretStr | None = None
    api_key_subject: str = "configured-api-key"
    api_key_roles: str = "employee"
    api_key_scopes: str = "knowledge:read tasks:read tasks:write"
    oidc_issuer_url: str | None = None
    oidc_audience: str | None = None
    agent_identity_claim: str = "agentic_agent_id"
    secret_provider: Literal["env", "vault"] = "env"
    vault_addr: str | None = None
    vault_token: SecretStr | None = None
    vault_role_id: SecretStr | None = None
    vault_secret_id: SecretStr | None = None
    vault_kubernetes_role: str | None = None
    vault_kubernetes_mount: str = "kubernetes"
    vault_namespace: str | None = None
    vault_kv_mount: str = "secret"
    vault_cache_ttl_seconds: int = Field(default=60, ge=0, le=3600)
    model_base_url: str | None = None
    model_provider: Literal["openai-compatible", "openai", "anthropic", "groq", "ollama"] = (
        "openai-compatible"
    )
    model_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    groq_api_key: SecretStr | None = None
    model_name: str | None = None
    model_max_tokens: int = Field(default=1200, ge=1, le=32_000)
    model_input_cost_per_million: float | None = Field(default=None, ge=0)
    model_output_cost_per_million: float | None = Field(default=None, ge=0)
    langfuse_host: str | None = None
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    audit_archive_bucket: str | None = None
    audit_archive_prefix: str = "a2a/production/audit"
    audit_archive_region: str = "us-east-1"
    audit_archive_retention_days: int = Field(default=2557, ge=1, le=36500)
    audit_archive_kms_key_id: str | None = None
    ollama_base_url: str = "http://localhost:11434/v1"
    embedding_provider: Literal["openai-compatible", "openai", "ollama"] = "openai-compatible"
    embedding_base_url: str | None = None
    embedding_api_key: SecretStr | None = None
    embedding_model: str | None = None
    jira_base_url: str | None = None
    jira_email: str | None = None
    jira_api_token: str | None = None
    github_token: SecretStr | None = None
    google_client_id: SecretStr | None = None
    google_client_secret: SecretStr | None = None
    google_refresh_token: SecretStr | None = None
    data_lake_root: Path = Path("./data/lake")
    data_source_sync_interval_seconds: int | None = Field(default=None, ge=30, le=86_400)
    conversation_memory_turns: int = Field(default=20, ge=1, le=200)
    conversation_memory_ttl_days: int = Field(default=90, ge=1, le=3650)
    max_artifact_bytes: int = 300_000
    max_task_artifacts: int = 10
    mqtt_broker_url: str | None = None
    mqtt_topic_filter: str = "a2a/telemetry/+/events"
    mqtt_client_id: str = "a2a-platform-ingress"
    mqtt_username: SecretStr | None = None
    mqtt_password: SecretStr | None = None
    mqtt_ca_file: Path | None = None
    mqtt_max_message_bytes: int = Field(default=256_000, ge=1_024, le=2_000_000)

    @model_validator(mode="after")
    def validate_auth_configuration(self) -> "Settings":
        if self.environment == "production" and self.auth_mode == "dev":
            raise ValueError("AUTH_MODE=dev is not allowed in production.")
        if self.environment == "production" and not self.public_url.startswith("https://"):
            raise ValueError("Production PUBLIC_URL must use HTTPS.")
        if self.environment == "production" and self.database_url.startswith("sqlite"):
            raise ValueError("Production DATABASE_URL must use PostgreSQL, not SQLite.")
        if (
            self.environment == "production"
            and self.redis_url
            and not self.redis_url.startswith(("rediss://", "vault://", "env://"))
        ):
            raise ValueError("Production REDIS_URL must use TLS (rediss://).")
        if self.auth_mode == "api_key" and not self.api_bearer_token:
            raise ValueError("API_BEARER_TOKEN is required when AUTH_MODE=api_key.")
        if self.auth_mode == "oidc" and not (self.oidc_issuer_url and self.oidc_audience):
            raise ValueError("OIDC_ISSUER_URL and OIDC_AUDIENCE are required when AUTH_MODE=oidc.")
        if (
            self.environment == "production"
            and self.auth_mode == "oidc"
            and not (self.oidc_issuer_url or "").startswith("https://")
        ):
            raise ValueError("Production OIDC_ISSUER_URL must use HTTPS.")
        if self.secret_provider == "vault" and not self.vault_addr:
            raise ValueError("VAULT_ADDR is required when SECRET_PROVIDER=vault.")
        if self.secret_provider == "vault" and not (
            self.vault_token
            or (self.vault_role_id and self.vault_secret_id)
            or self.vault_kubernetes_role
        ):
            raise ValueError(
                "Vault requires VAULT_TOKEN, AppRole credentials, or VAULT_KUBERNETES_ROLE."
            )
        if self.environment == "production" and self.secret_provider == "vault":
            if not (self.vault_addr or "").startswith("https://"):
                raise ValueError("Production VAULT_ADDR must use HTTPS.")
        if self.environment == "production" and self.langfuse_host:
            if not self.langfuse_host.startswith("https://"):
                raise ValueError("Production LANGFUSE_HOST must use HTTPS.")
        langfuse_values = (
            bool(self.langfuse_host),
            bool(self.langfuse_public_key),
            bool(self.langfuse_secret_key),
        )
        if any(langfuse_values) and not all(langfuse_values):
            raise ValueError(
                "LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY must be set together."
            )
        if self.langfuse_host and not self.langfuse_host.startswith(("https://", "http://")):
            raise ValueError("LANGFUSE_HOST must be an http:// or https:// URL.")
        if self.mqtt_broker_url and self.environment == "production":
            if not self.mqtt_broker_url.startswith("mqtts://"):
                raise ValueError("Production MQTT_BROKER_URL must use mqtts:// TLS.")
            if not (self.mqtt_username and self.mqtt_password):
                raise ValueError("Production MQTT requires broker authentication.")
        if self.mqtt_broker_url:
            if self.mqtt_topic_filter.count("+") != 1 or "#" in self.mqtt_topic_filter:
                raise ValueError("MQTT_TOPIC_FILTER must contain exactly one device wildcard (+).")
            if bool(self.mqtt_username) != bool(self.mqtt_password):
                raise ValueError("MQTT_USERNAME and MQTT_PASSWORD must be configured together.")
        return self


settings = Settings()
