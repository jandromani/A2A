import asyncio

import pytest
from a2a_tools_sdk import ToolContext, ToolInvocationError, ToolRegistry
from pydantic import BaseModel, Field


class LookupInput(BaseModel):
    key: str = Field(min_length=2)


def test_registry_validates_scopes_and_invokes_async_handler():
    registry = ToolRegistry()
    registry.register(
        "lookup_record",
        "Look up one record.",
        LookupInput,
        lambda args, context: {"key": args.key, "actor": context.subject},
        required_scopes=("records:read",),
    )

    result = asyncio.run(
        registry.invoke(
            "lookup_record",
            {"key": "r7"},
            ToolContext("agent:search", scopes=frozenset({"records:read"})),
        )
    )
    assert result == {"key": "r7", "actor": "agent:search"}

    with pytest.raises(ToolInvocationError) as denied:
        asyncio.run(registry.invoke("lookup_record", {"key": "r7"}, ToolContext("anonymous")))
    assert denied.value.code == "forbidden"


def test_validation_error_does_not_echo_untrusted_values():
    registry = ToolRegistry()
    registry.register("lookup_record", "Look up one record.", LookupInput, lambda args, ctx: {})

    with pytest.raises(ToolInvocationError) as invalid:
        asyncio.run(
            registry.invoke(
                "lookup_record",
                {"key": "x", "password": "do-not-echo"},
                ToolContext("a"),
            )
        )

    assert invalid.value.code == "invalid_arguments"
    assert "do-not-echo" not in str(invalid.value)


def test_mcp_tool_schema_is_derived_from_input_model():
    registry = ToolRegistry()
    registry.register("lookup_record", "Look up one record.", LookupInput, lambda args, ctx: {})

    tool = registry.list_mcp_tools()[0]
    assert tool["name"] == "lookup_record"
    assert tool["inputSchema"]["properties"]["key"]["minLength"] == 2
