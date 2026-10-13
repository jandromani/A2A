from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    kind: Literal["agent", "workflow", "tool", "mcp", "api"]
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

    kind: Literal["a2a", "mcp"]
    name: str = Field(min_length=2, max_length=180)
    description: str = Field(default="", max_length=4_000)
    endpoint: str = Field(min_length=8, max_length=1_000)
    auth_env: str | None = Field(default=None, max_length=120, pattern=r"^[A-Z][A-Z0-9_]*$")
    protocol_version: Literal[
        "2026-07-28",
        "2025-11-25",
        "2025-06-18",
        "2025-03-26",
        "2024-11-05",
        "1.0",
    ] = "2026-07-28"

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Endpoint must be an absolute HTTP or HTTPS URL.")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Endpoint URLs may not contain credentials or fragments.")
        return value.rstrip("/")


class ConnectorInvoke(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str | None = Field(default=None, max_length=12_000)
    scenario: Literal["knowledge", "report", "incident"] = "knowledge"
    tool_name: str | None = Field(default=None, max_length=180)
    arguments: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


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


class RpcRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    jsonrpc: str = "2.0"
    id: str | int | None = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)
