import re
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ArtifactInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=240)
    content: str = Field(max_length=300_000)

    @field_validator("path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        value = value.replace("\\", "/").strip()
        if value.startswith("/") or ".." in value.split("/") or "\x00" in value:
            raise ValueError("Artifact paths must be relative and may not traverse directories.")
        return value


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: Literal["knowledge", "report", "incident"]
    message: str = Field(min_length=3, max_length=12_000)
    artifacts: list[ArtifactInput] = Field(default_factory=list, max_length=10)
    issue_key: str | None = Field(default=None, max_length=32)
    context_id: str | None = Field(default=None, max_length=120)


class KnowledgeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=2, max_length=240)
    content: str = Field(min_length=20, max_length=100_000)
    scope: str = Field(min_length=2, max_length=80)
    source_uri: str = Field(default="manual", max_length=500)
    tags: list[str] = Field(default_factory=list, max_length=30)


class CapabilityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=3, max_length=120, pattern=r"^[a-zA-Z0-9._:-]+$")
    kind: Literal["agent", "workflow", "tool", "skill", "mcp", "api"]
    name: str = Field(min_length=2, max_length=180)
    description: str = Field(min_length=5, max_length=4_000)
    version: str = Field(default="1.0.0", max_length=40)
    tags: list[str] = Field(default_factory=list, max_length=30)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    endpoint: str | None = Field(default=None, max_length=500)

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Endpoint must be an absolute HTTP or HTTPS URL.")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Endpoint URLs may not contain credentials or fragments.")
        return value.rstrip("/")


class ConnectorCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["a2a", "mcp", "agora", "rest"]
    name: str = Field(min_length=2, max_length=180)
    description: str = Field(default="", max_length=4_000)
    endpoint: str = Field(min_length=8, max_length=1_000)
    auth_env: str | None = Field(default=None, max_length=120, pattern=r"^[A-Z][A-Z0-9_]*$")
    auth_header_name: str = Field(default="Authorization", max_length=80)
    auth_prefix: str = Field(default="Bearer ", max_length=80)
    protocol_version: Literal[
        "2026-07-28",
        "2025-11-25",
        "2025-06-18",
        "2025-03-26",
        "2024-11-05",
        "1.0",
    ] = "2026-07-28"

    @model_validator(mode="before")
    @classmethod
    def default_agora_protocol_version(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("kind") == "agora":
            value = dict(value)
            value.setdefault("protocol_version", "1.0")
        return value

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Endpoint must be an absolute HTTP or HTTPS URL.")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Endpoint URLs may not contain credentials or fragments.")
        return value.rstrip("/")

    @field_validator("auth_header_name")
    @classmethod
    def validate_auth_header_name(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", value) or value.lower() in {
            "host",
            "content-length",
            "transfer-encoding",
            "connection",
        }:
            raise ValueError("auth_header_name must be a safe HTTP header name.")
        return value

    @field_validator("auth_prefix")
    @classmethod
    def validate_auth_prefix(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("auth_prefix may not contain line breaks.")
        return value


class ConnectorInvoke(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str | None = Field(default=None, max_length=12_000)
    scenario: Literal["knowledge", "report", "incident"] = "knowledge"
    tool_name: str | None = Field(default=None, max_length=180)
    arguments: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    multiround: bool = False
    conversation_id: str | None = Field(default=None, max_length=120)
    path: str = Field(default="", max_length=500)
    query: dict[str, str | int | float | bool] = Field(default_factory=dict)


class RestActionCreate(BaseModel):
    """A write request to a registered REST connector; execution always needs approval."""

    model_config = ConfigDict(extra="forbid")

    method: Literal["POST", "PUT", "PATCH", "DELETE"]
    path: str = Field(min_length=1, max_length=500)
    query: dict[str, str | int | float | bool] = Field(default_factory=dict, max_length=30)
    body: dict[str, Any] = Field(default_factory=dict)

    @field_validator("path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        value = value.strip()
        if (
            value.startswith("//")
            or "\\" in value
            or "\x00" in value
            or "?" in value
            or "#" in value
            or "://" in value
            or any(part == ".." for part in value.split("/"))
        ):
            raise ValueError("REST action path must stay under the registered connector origin.")
        return value.lstrip("/")


class DataSourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["rest", "crm", "erp", "data_lake"]
    name: str = Field(min_length=2, max_length=180)
    endpoint: str | None = Field(default=None, max_length=1_000)
    auth_env: str | None = Field(default=None, max_length=120, pattern=r"^[A-Z][A-Z0-9_]*$")
    scope: str = Field(min_length=2, max_length=80)
    format: Literal["json", "odata", "csv", "parquet"] = "json"
    source_options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("endpoint")
    @classmethod
    def validate_data_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value.startswith("./") or value.startswith("data/"):
            return value
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Endpoint must be an absolute HTTP or HTTPS URL.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "Endpoint may not contain credentials, query parameters, or fragments."
            )
        return value.rstrip("/")

    @field_validator("source_options")
    @classmethod
    def validate_source_options(cls, value: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "provider",
            "records_path",
            "title_field",
            "id_field",
            "content_fields",
            "next_field",
            "max_pages",
            "max_records",
            "exclude_fields",
            "query_params",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"Unsupported source options: {', '.join(sorted(unknown))}.")
        if "provider" in value and value["provider"] not in {
            "generic",
            "hubspot",
            "salesforce",
            "sap_odata",
        }:
            raise ValueError("provider must be generic, hubspot, salesforce or sap_odata.")
        for key in ("records_path", "title_field", "id_field", "next_field"):
            if key in value and not isinstance(value[key], str):
                raise ValueError(f"{key} must be a string.")
        for key in ("content_fields", "exclude_fields"):
            if key in value and (
                not isinstance(value[key], list)
                or any(not isinstance(item, str) for item in value[key])
            ):
                raise ValueError(f"{key} must be a list of field names.")
        if "query_params" in value:
            params = value["query_params"]
            if not isinstance(params, dict) or len(params) > 30:
                raise ValueError("query_params must be an object with at most 30 entries.")
            if any(
                not isinstance(key, str)
                or not key
                or len(key) > 100
                or isinstance(item, (dict, list))
                or not isinstance(item, (str, int, float, bool))
                for key, item in params.items()
            ):
                raise ValueError("query_params values must be primitive values.")
        for key, maximum in (("max_pages", 100), ("max_records", 20_000)):
            if key in value and (
                isinstance(value[key], bool)
                or not isinstance(value[key], int)
                or not 1 <= value[key] <= maximum
            ):
                raise ValueError(f"{key} must be between 1 and {maximum}.")
        return value


class DataSourceEventBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str | None = Field(default=None, max_length=160)
    records: list[dict[str, Any]] = Field(min_length=1, max_length=500)


class WorkflowStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=2, max_length=80, pattern=r"^[a-zA-Z0-9._-]+$")
    type: Literal["knowledge.search", "model.generate", "connector.invoke", "approval", "result"]
    config: dict[str, Any] = Field(default_factory=dict)


class WorkflowDefinitionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=3, max_length=120, pattern=r"^[a-zA-Z0-9._:-]+$")
    name: str = Field(min_length=2, max_length=180)
    description: str = Field(min_length=5, max_length=4_000)
    version: str = Field(default="1.0.0", max_length=40)
    steps: list[WorkflowStep] = Field(min_length=1, max_length=20)
    enabled: bool = True

    @field_validator("steps")
    @classmethod
    def validate_unique_steps(cls, value: list[WorkflowStep]) -> list[WorkflowStep]:
        identifiers = [step.id for step in value]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Workflow step IDs must be unique.")
        return value


class WorkflowRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: dict[str, Any] = Field(default_factory=dict)
    context_id: str | None = Field(default=None, max_length=120)


class GitHubCommentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=12_000)


class GmailMessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: list[str] = Field(min_length=1, max_length=20)
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=100_000)

    @field_validator("subject")
    @classmethod
    def validate_subject(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("Subject must be a single line.")
        return value.strip()

    @field_validator("to")
    @classmethod
    def validate_recipients(cls, value: list[str]) -> list[str]:
        import re

        if any(
            len(item) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", item.strip())
            for item in value
        ):
            raise ValueError("Every recipient must be a valid email address.")
        return [item.strip() for item in value]


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=2_000)


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True)

    subject: str
    roles: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()
    email: str | None = None


class AgentIdentityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=3, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")
    display_name: str = Field(min_length=2, max_length=180)
    roles: list[Literal["employee", "operator"]] = Field(default_factory=lambda: ["employee"])
    scopes: list[str] = Field(default_factory=list, max_length=40)
    expires_at: datetime | None = None

    @field_validator("roles")
    @classmethod
    def unique_agent_roles(cls, value: list[str]) -> list[str]:
        if not value or len(value) != len(set(value)):
            raise ValueError("Agent identities need one or more distinct least-privilege roles.")
        return value

    @field_validator("scopes")
    @classmethod
    def validate_agent_scopes(cls, value: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[a-zA-Z0-9._:-]{1,120}", item) for item in value):
            raise ValueError(
                "Agent scopes may contain letters, digits, dot, underscore, colon and hyphen."
            )
        if len(value) != len(set(value)):
            raise ValueError("Agent scopes must be distinct.")
        return value

    @field_validator("expires_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("expires_at must include a timezone.")
        return value


class RpcRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    jsonrpc: str = "2.0"
    id: str | int | None = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)
