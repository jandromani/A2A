"""Official A2A and MCP SDK clients used for remote protocol connectors."""

import asyncio
from typing import Any

from fastapi import HTTPException


def _as_json(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    try:
        from google.protobuf.json_format import MessageToDict

        return MessageToDict(value, preserving_proto_field_name=True)
    except (ImportError, TypeError, ValueError):
        return value


def _run(awaitable):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    raise RuntimeError("Protocol SDK connector calls must run outside the active event loop.")


def call_mcp_sdk(
    endpoint: str,
    headers: dict[str, str],
    method: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    async def call() -> dict[str, Any]:
        try:
            import httpx2
            from mcp import Client
            from mcp.client.streamable_http import streamable_http_client
        except ImportError as exc:  # pragma: no cover - installation contract
            raise HTTPException(status_code=503, detail="MCP Python SDK is not installed.") from exc

        http_client = httpx2.AsyncClient(headers=headers, timeout=15.0)
        try:
            transport = streamable_http_client(endpoint, http_client=http_client)
            async with Client(transport) as client:
                if method == "tools/list":
                    result = await client.list_tools()
                elif method == "tools/call":
                    arguments = params or {}
                    result = await client.call_tool(
                        str(arguments.get("name", "")),
                        arguments.get("arguments") or {},
                    )
                else:
                    raise HTTPException(status_code=422, detail="Unsupported MCP SDK operation.")
        finally:
            await http_client.aclose()
        value = _as_json(result)
        return value if isinstance(value, dict) else {"result": value}

    try:
        return _run(call())
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Remote MCP SDK request failed.") from exc


def call_a2a_sdk(
    endpoint: str,
    headers: dict[str, str],
    message: str,
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    async def call() -> list[dict[str, Any]]:
        try:
            import httpx
            from a2a.client import ClientConfig, create_client
            from a2a.helpers import new_text_message
            from a2a.types import Role, SendMessageRequest
            from google.protobuf.json_format import ParseDict
            from google.protobuf.struct_pb2 import Struct
        except ImportError as exc:  # pragma: no cover - installation contract
            raise HTTPException(status_code=503, detail="A2A Python SDK is not installed.") from exc

        client_http = httpx.AsyncClient(
            headers=headers,
            timeout=15.0,
            follow_redirects=False,
        )
        client = None
        try:
            client = await create_client(
                endpoint,
                client_config=ClientConfig(httpx_client=client_http, streaming=False),
            )
            message_object = new_text_message(message, role=Role.ROLE_USER)
            if metadata:
                message_object.metadata.CopyFrom(ParseDict(metadata, Struct()))
            request = SendMessageRequest(message=message_object)
            responses = []
            async for response in client.send_message(request):
                responses.append(_as_json(response))
                if len(responses) >= 100:
                    break
            return responses
        finally:
            if client is not None:
                await client.close()
            else:
                await client_http.aclose()

    try:
        return _run(call())
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Remote A2A SDK request failed.") from exc
