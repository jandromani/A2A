from datetime import UTC
from typing import Any

from fastapi import HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.embeddings import lexical_score
from app.jira import JiraConnector
from app.mcp_transport import CURRENT_PROTOCOL_VERSION
from app.model_gateway import gateway_status
from app.models import KnowledgeDocument, TaskRecord
from app.policy import authorize
from app.schemas import Principal, TaskCreate
from app.services import (
    cancel_task,
    create_task,
    get_task,
    list_capabilities,
    serialize_task,
)

A2A_PROTOCOL_VERSION = "1.0"
MCP_PROTOCOL_VERSION = CURRENT_PROTOCOL_VERSION
SUPPORTED_MCP_VERSIONS = {
    MCP_PROTOCOL_VERSION,
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
}


def a2a_agent_card(public_url: str, config: Settings | None = None) -> dict[str, Any]:
    card = {
        "name": "Agentic Atlas Platform",
        "description": (
            "Governed agent capabilities for corporate knowledge, technical inventory, "
            "and operational incident workflows."
        ),
        "supportedInterfaces": [
            {
                "url": f"{public_url.rstrip('/')}/a2a-sdk/",
                "protocolBinding": "JSONRPC",
                "protocolVersion": A2A_PROTOCOL_VERSION,
            },
            {
                "url": f"{public_url.rstrip('/')}/a2a",
                "protocolBinding": "JSONRPC",
                "protocolVersion": A2A_PROTOCOL_VERSION,
            },
        ],
        "version": "0.1.0",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": True,
        },
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": [
            {
                "id": "corporate-knowledge",
                "name": "Corporate knowledge",
                "description": "Answer with authorized source references.",
                "tags": ["knowledge", "retrieval", "evidence"],
                "examples": ["How do I justify a work trip expense?"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain", "application/json"],
            },
            {
                "id": "technical-inventory",
                "name": "Technical inventory",
                "description": "Inspect supplied build and deployment configuration files.",
                "tags": ["repository", "inventory"],
                "examples": ["Inspect these pom.xml and Dockerfile contents."],
                "inputModes": ["text/plain", "application/json"],
                "outputModes": ["application/json"],
            },
            {
                "id": "operational-incident",
                "name": "Operational incident",
                "description": "Prepare an incident draft and pause for human approval.",
                "tags": ["incident", "approval"],
                "examples": ["Prepare a response for incident INC-42."],
                "inputModes": ["text/plain", "application/json"],
                "outputModes": ["application/json"],
            },
        ],
    }
    if config and config.auth_mode != "dev":
        card["securitySchemes"] = {
            "BearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT" if config.auth_mode == "oidc" else "opaque",
            }
        }
        card["securityRequirements"] = [{"BearerAuth": []}]
    return card


def _a2a_task(task: TaskRecord) -> dict[str, Any]:
    states = {
        "running": "TASK_STATE_WORKING",
        "awaiting_approval": "TASK_STATE_INPUT_REQUIRED",
        "completed": "TASK_STATE_COMPLETED",
        "needs_review": "TASK_STATE_COMPLETED",
        "rejected": "TASK_STATE_REJECTED",
        "failed": "TASK_STATE_FAILED",
        "cancelled": "TASK_STATE_CANCELED",
    }
    result = task.result_json or {}
    parts = [{"data": result}]
    updated_at = task.updated_at
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    return {
        "id": task.id,
        "contextId": task.context_id or task.id,
        "status": {
            "state": states.get(task.status, "TASK_STATE_UNSPECIFIED"),
            "timestamp": updated_at.isoformat(),
        },
        "artifacts": [{"artifactId": f"{task.id}-result", "name": task.title, "parts": parts}],
        "metadata": {"scenario": task.scenario, "platformStatus": task.status},
    }


def _rpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def handle_a2a(
    rpc: dict[str, Any],
    session: Session,
    config: Settings,
    principal: Principal,
) -> dict[str, Any]:
    request_id = rpc.get("id")
    if rpc.get("jsonrpc") != "2.0" or not isinstance(rpc.get("method"), str):
        return _rpc_error(request_id, -32600, "Invalid JSON-RPC request.")
    params = rpc.get("params") or {}
    if not isinstance(params, dict):
        return _rpc_error(request_id, -32602, "Request params must be an object.")
    method = rpc["method"]
    if method in {"SendMessage", "message/send"}:
        message = params.get("message") or {}
        if not isinstance(message, dict):
            return _rpc_error(request_id, -32602, "Message must be an object.")
        if method == "SendMessage":
            if not message.get("messageId"):
                return _rpc_error(request_id, -32602, "Message.messageId is required.")
            if message.get("role") != "ROLE_USER":
                return _rpc_error(request_id, -32602, "SendMessage requires role ROLE_USER.")
        message_parts = message.get("parts") or []
        if not isinstance(message_parts, list):
            return _rpc_error(request_id, -32602, "Message.parts must be an array.")
        text = "\n".join(
            str(part.get("text", ""))
            for part in message_parts
            if isinstance(part, dict) and part.get("text") is not None
        ).strip()
        metadata = {}
        message_metadata = message.get("metadata") or {}
        request_metadata = params.get("metadata") or {}
        if not isinstance(message_metadata, dict) or not isinstance(request_metadata, dict):
            return _rpc_error(request_id, -32602, "Message metadata must be an object.")
        metadata.update(message_metadata)
        metadata.update(request_metadata)
        scenario = metadata.get("scenario", "knowledge")
        if not text:
            return _rpc_error(request_id, -32602, "A text message part is required.")
        if scenario not in {"knowledge", "report", "incident"}:
            return _rpc_error(request_id, -32602, "Unsupported workflow scenario.")
        try:
            task_input = TaskCreate(
                scenario=scenario,
                message=text,
                artifacts=metadata.get("artifacts", []),
                issue_key=metadata.get("issue_key"),
                context_id=params.get("contextId") or message.get("contextId"),
            )
        except ValidationError:
            return _rpc_error(request_id, -32602, "Task request parameters are invalid.")
        try:
            task = create_task(session, config, task_input, principal)
        except HTTPException as exc:
            return _rpc_error(request_id, -32003, str(exc.detail), {"http_status": exc.status_code})
        result = _a2a_task(task)
        if method == "SendMessage":
            result = {"task": result}
        return _rpc_result(request_id, result)

    if method in {"GetTask", "CancelTask", "tasks/get", "tasks/cancel"}:
        task_id = params.get("id") or params.get("taskId")
        if not task_id:
            return _rpc_error(request_id, -32602, "A task id is required.")
        try:
            task = get_task(session, str(task_id), principal)
            if method in {"CancelTask", "tasks/cancel"}:
                cancel_task(session, config, task.id, principal)
                task = get_task(session, task.id, principal)
        except HTTPException as exc:
            return _rpc_error(request_id, -32003, str(exc.detail), {"http_status": exc.status_code})
        return _rpc_result(request_id, _a2a_task(task))

    return _rpc_error(request_id, -32601, f"Method not found: {method}")


def mcp_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "run_agent_workflow",
            "description": (
                "Run a governed knowledge, technical inventory, or incident workflow. "
                "Incident writes always wait for human approval."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "scenario": {"type": "string", "enum": ["knowledge", "report", "incident"]},
                    "message": {"type": "string", "minLength": 3},
                    "artifacts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "content": {"type": "string"},
                            },
                            "required": ["path", "content"],
                        },
                    },
                    "issue_key": {"type": "string"},
                },
                "required": ["scenario", "message"],
            },
        },
        {
            "name": "search_knowledge",
            "description": "Search source documents within the caller's granted scopes.",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string", "minLength": 2}},
                "required": ["query"],
            },
        },
        {
            "name": "list_capabilities",
            "description": "List registered agents, workflows, and tools.",
            "inputSchema": {
                "type": "object",
                "properties": {"kind": {"type": "string"}},
            },
        },
        {
            "name": "get_task_status",
            "description": "Read a task and its current governed status.",
            "inputSchema": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        },
    ]


def mcp_resources() -> list[dict[str, Any]]:
    return [
        {
            "uri": "agentic://catalog",
            "name": "Agentic capability catalog",
            "description": "Registered local agents, workflows, and tools.",
            "mimeType": "application/json",
        },
        {
            "uri": "agentic://system",
            "name": "Platform system status",
            "description": "Configured provider and integration status without secret values.",
            "mimeType": "application/json",
        },
    ]


def _json_content(value: Any) -> dict[str, Any]:
    import json

    text = json.dumps(value, ensure_ascii=False, default=str)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": value,
        "isError": False,
    }


def _mcp_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return _rpc_error(request_id, code, message)


def handle_mcp(
    rpc: dict[str, Any],
    session: Session,
    config: Settings,
    principal: Principal,
) -> tuple[int, dict[str, Any] | None]:
    request_id = rpc.get("id")
    is_notification = request_id is None
    if rpc.get("jsonrpc") != "2.0" or not isinstance(rpc.get("method"), str):
        return 400, _mcp_error(request_id, -32600, "Invalid JSON-RPC request.")
    method = rpc["method"]
    params = rpc.get("params") or {}
    if not isinstance(params, dict):
        return 400, _mcp_error(request_id, -32602, "Request params must be an object.")
    if method == "notifications/initialized":
        return 202, None
    if method == "initialize":
        requested = params.get("protocolVersion", MCP_PROTOCOL_VERSION)
        if requested not in SUPPORTED_MCP_VERSIONS:
            return 200, _mcp_error(request_id, -32602, "Unsupported MCP protocol version.")
        return 200, _rpc_result(
            request_id,
            {
                "protocolVersion": requested,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                },
                "serverInfo": {"name": "agentic-atlas-platform", "version": "0.1.0"},
                "instructions": (
                    "Use run_agent_workflow for an end-to-end task. Check task status and "
                    "approval state before describing any external side effect."
                ),
            },
        )
    if method == "ping":
        return 200, _rpc_result(request_id, {})
    if method == "server/discover":
        return 200, _rpc_result(
            request_id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"listChanged": False, "subscribe": False},
                },
                "instructions": (
                    "Use run_agent_workflow for an end-to-end task. Check task status and "
                    "approval state before describing any external side effect."
                ),
            },
        )
    if method == "tools/list":
        return 200, _rpc_result(request_id, {"tools": mcp_tools()})
    if method == "resources/list":
        return 200, _rpc_result(request_id, {"resources": mcp_resources()})

    if method == "resources/read":
        uri = params.get("uri")
        if uri == "agentic://catalog":
            return 200, _rpc_result(
                request_id,
                {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "application/json",
                            "text": _json_content(list_capabilities(session))["content"][0]["text"],
                        }
                    ]
                },
            )
        if uri == "agentic://system":
            value = {
                "model": gateway_status(config),
                "database": "connected",
                "event_bus": "valkey configured" if config.redis_url else "SQL event log",
                "jira": "configured" if JiraConnector(config).configured else "not configured",
            }
            return 200, _rpc_result(
                request_id,
                {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "application/json",
                            "text": _json_content(value)["content"][0]["text"],
                        }
                    ]
                },
            )
        return 200, _mcp_error(request_id, -32602, "Unknown resource URI.")

    if method != "tools/call":
        if is_notification:
            return 202, None
        return 200, _mcp_error(request_id, -32601, f"Method not found: {method}")

    tool_name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(tool_name, str) or not isinstance(arguments, dict):
        return 200, _mcp_error(request_id, -32602, "Tool name and arguments are invalid.")
    try:
        if tool_name == "run_agent_workflow":
            task_input = TaskCreate(**arguments)
            task = create_task(session, config, task_input, principal)
            result = serialize_task(task)
        elif tool_name == "search_knowledge":
            allowed, reason = authorize(principal, "knowledge.read", {}, config)
            if not allowed:
                raise HTTPException(status_code=403, detail=reason)
            query = str(arguments.get("query", ""))
            docs = session.scalars(
                select(KnowledgeDocument).where(KnowledgeDocument.enabled.is_(True))
            ).all()
            if "*" not in principal.scopes:
                docs = [doc for doc in docs if doc.scope in principal.scopes]
            results = sorted(
                [
                    {
                        "id": doc.id,
                        "title": doc.title,
                        "scope": doc.scope,
                        "source_uri": doc.source_uri,
                        "excerpt": doc.content[:420],
                        "score": lexical_score(query, f"{doc.title} {doc.content}"),
                    }
                    for doc in docs
                ],
                key=lambda item: item["score"],
                reverse=True,
            )
            result = {"results": [item for item in results if item["score"] > 0][:5]}
        elif tool_name == "list_capabilities":
            result = {"capabilities": list_capabilities(session, arguments.get("kind"))}
        elif tool_name == "get_task_status":
            task = get_task(session, str(arguments.get("task_id", "")), principal)
            result = serialize_task(task)
        else:
            return 200, _mcp_error(request_id, -32602, "Unknown tool.")
    except HTTPException as exc:
        return 200, {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": str(exc.detail)}],
                "isError": True,
            },
        }
    except Exception as exc:
        return 200, {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": str(exc)[:400]}],
                "isError": True,
            },
        }
    return 200, _rpc_result(request_id, _json_content(result))


def check_mcp_origin(request: Request, config: Settings) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    try:
        from urllib.parse import urlparse

        origin_host = urlparse(origin).netloc.lower()
        request_host = request.url.netloc.lower()
        public_host = urlparse(config.public_url).netloc.lower()
        return origin_host in {request_host, public_host}
    except ValueError:
        return False
