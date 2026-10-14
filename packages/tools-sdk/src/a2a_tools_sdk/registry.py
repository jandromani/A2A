"""Permission-aware registry and optional MCP server adapter."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

ToolHandler = Callable[[BaseModel, "ToolContext"], Any]
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


@dataclass(frozen=True)
class ToolContext:
    """Authenticated caller facts passed by the host application."""

    subject: str
    scopes: frozenset[str] = frozenset()
    roles: frozenset[str] = frozenset()
    request_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def permits(self, required_scopes: tuple[str, ...]) -> bool:
        return "*" in self.scopes or set(required_scopes).issubset(self.scopes)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    required_scopes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _TOOL_NAME.fullmatch(self.name):
            raise ValueError("Tool names must use 2-64 lowercase letters, digits, or underscores.")
        if not self.description.strip():
            raise ValueError("Tool descriptions must not be empty.")
        if not issubclass(self.input_model, BaseModel):
            raise TypeError("Tool input_model must be a Pydantic BaseModel.")

    def as_mcp_tool(self) -> dict[str, Any]:
        schema = self.input_model.model_json_schema()
        schema.setdefault("type", "object")
        return {"name": self.name, "description": self.description, "inputSchema": schema}


class ToolInvocationError(Exception):
    """Safe-to-return tool error with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ToolRegistry:
    """Register validated handlers and invoke them with a host-authenticated context."""

    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolSpec, ToolHandler]] = {}

    def register(
        self,
        name: str,
        description: str,
        input_model: type[BaseModel],
        handler: ToolHandler,
        *,
        required_scopes: tuple[str, ...] = (),
    ) -> ToolSpec:
        if name in self._tools:
            raise ValueError(f"Tool {name!r} is already registered.")
        if not callable(handler):
            raise TypeError("Tool handler must be callable.")
        spec = ToolSpec(name, description, input_model, tuple(required_scopes))
        self._tools[name] = (spec, handler)
        return spec

    def specs(self) -> list[ToolSpec]:
        return [self._tools[name][0] for name in sorted(self._tools)]

    def list_mcp_tools(self) -> list[dict[str, Any]]:
        return [spec.as_mcp_tool() for spec in self.specs()]

    async def invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> Any:
        registration = self._tools.get(name)
        if registration is None:
            raise ToolInvocationError("tool_not_found", "Tool is not registered.")
        spec, handler = registration
        if not context.permits(spec.required_scopes):
            raise ToolInvocationError("forbidden", "Caller is missing a required tool scope.")
        if not isinstance(arguments, dict):
            raise ToolInvocationError("invalid_arguments", "Tool arguments must be an object.")
        try:
            parsed = spec.input_model.model_validate(arguments)
        except ValidationError as exc:
            fields = sorted({".".join(map(str, item["loc"])) for item in exc.errors()})
            message = "Tool arguments are invalid."
            if fields:
                message += " Check: " + ", ".join(fields[:10]) + "."
            raise ToolInvocationError("invalid_arguments", message) from exc
        try:
            value = handler(parsed, context)
            return await value if inspect.isawaitable(value) else value
        except ToolInvocationError:
            raise
        except Exception as exc:
            raise ToolInvocationError("tool_failed", "Tool execution failed.") from exc

    def register_mcp_tools(
        self,
        server: Any,
        *,
        context_provider: Callable[[Any], ToolContext],
    ) -> None:
        """Register each tool with the official MCP Python server SDK.

        The server SDK derives its input schema from a callable signature. This adapter exposes
        the fields of each Pydantic model as that signature, then delegates all validation and
        authorization back to this registry.
        """
        try:
            from inspect import Parameter, Signature

            from mcp.server.mcpserver import Context
        except ImportError as exc:  # pragma: no cover - optional dependency contract
            raise RuntimeError("Install a2a-tools-sdk[mcp] to register MCP server tools.") from exc

        for spec in self.specs():
            parameters = [
                Parameter("ctx", Parameter.KEYWORD_ONLY, annotation=Context),
            ]
            annotations: dict[str, Any] = {"ctx": Context}
            for field_name, model_field in spec.input_model.model_fields.items():
                if field_name == "ctx":
                    raise ValueError("Tool input models may not use the reserved field name 'ctx'.")
                default = Parameter.empty
                if not model_field.is_required():
                    default = (
                        model_field.default_factory()
                        if model_field.default_factory is not None
                        else model_field.default
                    )
                parameters.append(
                    Parameter(
                        field_name,
                        Parameter.KEYWORD_ONLY,
                        default=default,
                        annotation=model_field.annotation,
                    )
                )
                annotations[field_name] = model_field.annotation
            signature = Signature(parameters)

            async def invoke(
                _tool_name: str = spec.name,
                _registry: ToolRegistry = self,
                **kwargs: Any,
            ) -> Any:
                mcp_context = kwargs.pop("ctx", None)
                if mcp_context is None:
                    raise ToolInvocationError("unauthenticated", "Caller context is unavailable.")
                caller = context_provider(mcp_context)
                return await _registry.invoke(_tool_name, kwargs, caller)

            invoke.__name__ = spec.name
            invoke.__doc__ = spec.description
            invoke.__signature__ = signature  # type: ignore[attr-defined]
            invoke.__annotations__ = {**annotations, "return": Any}
            server.tool(name=spec.name, description=spec.description)(invoke)
