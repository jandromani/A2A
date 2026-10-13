import json
import os
from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from fastapi import HTTPException
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.event_bus import write_audit
from app.mcp_transport import CURRENT_PROTOCOL_VERSION, encode_header_value
from app.models import ConnectorRecord
from app.policy import authorize
from app.schemas import ConnectorCreate, ConnectorInvoke, Principal

MCP_ACCEPT = "application/json, text/event-stream"


def validate_outbound_endpoint(endpoint: str, config: Settings) -> None:
    parsed = urlparse(endpoint)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise HTTPException(status_code=422, detail="Only absolute HTTP(S) endpoints are accepted.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(
            status_code=422,
            detail="Credentials, query parameters, and fragments are not allowed in endpoints.",
        )
    try:
        address = ip_address(hostname)
    except ValueError:
        address = None
    if config.environment == "production":
        allowed_hosts = {
            item.strip().lower()
            for item in config.connector_allowed_hosts.split(",")
            if item.strip()
        }
        if parsed.scheme != "https":
            raise HTTPException(status_code=422, detail="Production connectors must use HTTPS.")
        if hostname not in allowed_hosts:
            raise HTTPException(
                status_code=422,
                detail="Production connector host is not in CONNECTOR_ALLOWED_HOSTS.",
            )
        if address and (address.is_private or address.is_loopback or address.is_link_local):
            raise HTTPException(
                status_code=422,
                detail="Private IP endpoints require a DNS name explicitly approved by policy.",
            )


def serialize_connector(connector: ConnectorRecord, config: Settings) -> dict[str, Any]:
    return {
        "id": connector.id,
        "kind": connector.kind,
        "name": connector.name,
        "description": connector.description,
        "endpoint": connector.endpoint,
        "auth_env": connector.auth_env,
        "auth_configured": bool(connector.auth_env and os.getenv(connector.auth_env)),
        "protocol_version": connector.protocol_version,
        "enabled": connector.enabled,
        "last_invoked_at": connector.last_invoked_at.isoformat()
        if connector.last_invoked_at
        else None,
        "created_at": connector.created_at.isoformat(),
        "registration_state": "registered and configured"
        if not connector.auth_env or os.getenv(connector.auth_env)
        else "secret environment variable is missing",
    }


def list_connectors(session: Session, config: Settings) -> list[dict[str, Any]]:
    records = session.scalars(
        select(ConnectorRecord).order_by(desc(ConnectorRecord.created_at))
    ).all()
    return [serialize_connector(record, config) for record in records]


def register_connector(
    session: Session,
    config: Settings,
    body: ConnectorCreate,
    principal: Principal,
) -> dict[str, Any]:
    allowed, reason = authorize(principal, "catalog.manage", {}, config)
    if not allowed:
        raise HTTPException(status_code=403, detail=reason)
    validate_outbound_endpoint(body.endpoint, config)
    record = ConnectorRecord(
        kind=body.kind,
        name=body.name,
        description=body.description,
        endpoint=body.endpoint,
        auth_env=body.auth_env,
        protocol_version=body.protocol_version,
    )
    session.add(record)
    session.flush()
    write_audit(
        session,
        principal.subject,
        "connector.register",
        "connector",
        record.id,
        details={"kind": record.kind, "endpoint_host": urlparse(record.endpoint).hostname},
    )
    session.commit()
    return serialize_connector(record, config)


def set_connector_enabled(
    session: Session,
    config: Settings,
    connector_id: str,
    enabled: bool,
    principal: Principal,
) -> dict[str, Any]:
    allowed, reason = authorize(principal, "catalog.manage", {}, config)
    if not allowed:
        raise HTTPException(status_code=403, detail=reason)
    record = session.get(ConnectorRecord, connector_id)
    if not record:
        raise HTTPException(status_code=404, detail="Connector not found.")
    if record.enabled == enabled:
        raise HTTPException(
            status_code=409,
            detail="Connector is already enabled." if enabled else "Connector is already disabled.",
        )
    record.enabled = enabled
    write_audit(
        session,
        principal.subject,
        "connector.enable" if enabled else "connector.disable",
        "connector",
        record.id,
        details={"kind": record.kind},
    )
    session.commit()
    return serialize_connector(record, config)


def _connector(session: Session, connector_id: str) -> ConnectorRecord:
    item = session.get(ConnectorRecord, connector_id)
    if not item or not item.enabled:
        raise HTTPException(status_code=404, detail="Connector not found or disabled.")
    return item


def _headers(connector: ConnectorRecord, accept: str) -> dict[str, str]:
    result = {"Accept": accept, "Content-Type": "application/json"}
    if connector.auth_env:
        token = os.getenv(connector.auth_env)
        if not token:
            raise HTTPException(
                status_code=424,
                detail=f"Connector secret {connector.auth_env} is not present in the environment.",
            )
        result["Authorization"] = f"Bearer {token}"
    return result


def _post(
    connector: ConnectorRecord,
    config: Settings,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    validate_outbound_endpoint(connector.endpoint, config)
    try:
        return httpx.post(
            connector.endpoint,
            headers=headers or _headers(connector, MCP_ACCEPT),
            json=payload,
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Remote connector request failed.") from exc


def _rpc_response(response: httpx.Response) -> dict[str, Any] | None:
    try:
        value = response.json()
        return value if isinstance(value, dict) else None
    except ValueError:
        if "text/event-stream" not in response.headers.get("content-type", ""):
            return None
    for line in response.text.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            value = json.loads(line[5:].strip())
        except ValueError:
            continue
        if isinstance(value, dict) and ("result" in value or "error" in value):
            return value
    return None


def _mcp_exchange(
    connector: ConnectorRecord,
    config: Settings,
    method: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params = params or {}
    if connector.protocol_version == CURRENT_PROTOCOL_VERSION:
        modern_params = {
            **params,
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": CURRENT_PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientInfo": {
                    "name": "agentic-atlas-platform",
                    "version": "0.1.0",
                },
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        }
        headers = _headers(connector, MCP_ACCEPT)
        headers["MCP-Protocol-Version"] = CURRENT_PROTOCOL_VERSION
        headers["Mcp-Method"] = method
        header_name = (
            params.get("name")
            if method in {"tools/call", "prompts/get"}
            else params.get("uri")
            if method == "resources/read"
            else None
        )
        if header_name is not None:
            headers["Mcp-Name"] = encode_header_value(str(header_name))
        response = _post(
            connector,
            config,
            {
                "jsonrpc": "2.0",
                "id": uuid4().hex,
                "method": method,
                "params": modern_params,
            },
            headers,
        )
        modern_body = _rpc_response(response)
        modern_error = (modern_body or {}).get("error") or {}
        if response.is_success and modern_body is not None and not modern_error:
            return modern_body.get("result", {})
        if modern_error.get("code") in {-32020, -32022}:
            raise HTTPException(
                status_code=502,
                detail="Remote MCP rejected protocol metadata; version fallback was blocked.",
            )
        if response.status_code not in {400, 404, 405} and modern_error.get("code") != -32601:
            raise HTTPException(status_code=502, detail="Remote MCP operation failed.")

    return _mcp_exchange_legacy(connector, config, method, params)


def _mcp_exchange_legacy(
    connector: ConnectorRecord,
    config: Settings,
    method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    legacy_versions = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
    if connector.protocol_version in legacy_versions:
        legacy_versions.remove(connector.protocol_version)
        legacy_versions.insert(0, connector.protocol_version)

    negotiated = None
    init = None
    for version in legacy_versions:
        candidate = _post(
            connector,
            config,
            {
                "jsonrpc": "2.0",
                "id": uuid4().hex,
                "method": "initialize",
                "params": {
                    "protocolVersion": version,
                    "capabilities": {},
                    "clientInfo": {"name": "agentic-atlas-platform", "version": "0.1.0"},
                },
            },
        )
        candidate_body = _rpc_response(candidate)
        if candidate_body is None:
            continue
        if candidate.is_success and not candidate_body.get("error"):
            negotiated = candidate_body.get("result", {}).get("protocolVersion")
            if negotiated in legacy_versions:
                init = candidate
                break
    if not init or not negotiated:
        raise HTTPException(status_code=502, detail="Remote MCP initialization failed.")

    headers = _headers(connector, MCP_ACCEPT)
    headers["MCP-Protocol-Version"] = negotiated
    session_id = init.headers.get("Mcp-Session-Id")
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    notification = _post(
        connector,
        config,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers,
    )
    if notification.status_code not in {200, 202, 204}:
        raise HTTPException(
            status_code=502, detail="Remote MCP initialization notification failed."
        )
    response = _post(
        connector,
        config,
        {"jsonrpc": "2.0", "id": uuid4().hex, "method": method, "params": params},
        headers,
    )
    if not response.is_success:
        raise HTTPException(status_code=502, detail="Remote MCP operation failed.")
    result = _rpc_response(response)
    if result is None:
        raise HTTPException(status_code=502, detail="Remote MCP returned invalid JSON.")
    if "error" in result:
        raise HTTPException(status_code=502, detail="Remote MCP rejected the operation.")
    return result.get("result", {})


def mcp_tools(
    session: Session,
    config: Settings,
    connector_id: str,
    principal: Principal,
) -> dict[str, Any]:
    allowed, reason = authorize(principal, "connector.invoke", {}, config)
    if not allowed:
        raise HTTPException(status_code=403, detail=reason)
    record = _connector(session, connector_id)
    if record.kind != "mcp":
        raise HTTPException(status_code=409, detail="This connector does not use MCP.")
    result = _mcp_exchange(record, config, "tools/list")
    record.last_invoked_at = datetime.now(UTC)
    write_audit(
        session,
        principal.subject,
        "connector.mcp.tools_list",
        "connector",
        record.id,
        details={"kind": record.kind, "remote_operation": "tools/list"},
    )
    session.commit()
    return {"connector_id": record.id, "tools": result.get("tools", [])}


def invoke_connector(
    session: Session,
    config: Settings,
    connector_id: str,
    body: ConnectorInvoke,
    principal: Principal,
) -> dict[str, Any]:
    allowed, reason = authorize(principal, "connector.invoke", {}, config)
    if not allowed:
        raise HTTPException(status_code=403, detail=reason)
    record = _connector(session, connector_id)
    if record.kind == "a2a":
        if not body.message:
            raise HTTPException(status_code=422, detail="An A2A message is required.")
        metadata = dict(body.metadata)
        metadata["scenario"] = body.scenario
        headers = _headers(record, "application/json")
        headers["A2A-Version"] = "1.0"
        result = _post(
            record,
            config,
            {
                "jsonrpc": "2.0",
                "id": uuid4().hex,
                "method": "SendMessage",
                "params": {
                    "message": {
                        "role": "ROLE_USER",
                        "parts": [{"text": body.message}],
                        "messageId": uuid4().hex,
                    },
                    "metadata": metadata,
                },
            },
            headers,
        )
        if not result.is_success:
            raise HTTPException(status_code=502, detail="Remote A2A request failed.")
        try:
            response = result.json()
        except ValueError as exc:
            raise HTTPException(
                status_code=502, detail="Remote A2A returned invalid JSON."
            ) from exc
        if "error" in response:
            raise HTTPException(status_code=502, detail="Remote A2A rejected the task.")
        output: dict[str, Any] = {"protocol": "a2a", "response": response.get("result")}
        operation = "connector.a2a.message_send"
    else:
        if not body.tool_name:
            raise HTTPException(status_code=422, detail="An MCP tool name is required.")
        response = _mcp_exchange(
            record,
            config,
            "tools/call",
            {"name": body.tool_name, "arguments": body.arguments},
        )
        output = {"protocol": "mcp", "response": response}
        operation = "connector.mcp.tools_call"
    record.last_invoked_at = datetime.now(UTC)
    write_audit(
        session,
        principal.subject,
        operation,
        "connector",
        record.id,
        details={
            "kind": record.kind,
            "remote_operation": "message/send" if record.kind == "a2a" else body.tool_name,
        },
    )
    session.commit()
    return output
