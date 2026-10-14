"""Shared Pydantic inputs for platform tools served through legacy and SDK MCP endpoints."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class RunAgentWorkflowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: Literal["knowledge", "report", "incident"]
    message: str = Field(min_length=3, max_length=12_000)
    artifacts: list[dict[str, str]] = Field(default_factory=list, max_length=10)
    issue_key: str | None = Field(default=None, max_length=32)
    context_id: str | None = Field(default=None, max_length=120)


class SearchKnowledgeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=2, max_length=4_000)


class ListCapabilitiesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str | None = Field(default=None, max_length=32)


class GetTaskStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=80)


class EmptyToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunCustomWorkflowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str = Field(min_length=3, max_length=120)
    input: dict[str, Any] = Field(default_factory=dict)
    context_id: str | None = Field(default=None, max_length=120)
