"""Official SDK server adapters for MCP and A2A with shared API authorization."""

import asyncio
import json
from contextvars import ContextVar
from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import Settings
from app.protocols import check_mcp_origin
from app.schemas import Principal, TaskCreate, WorkflowRunRequest
from app.security import get_principal

_current_principal: ContextVar[Principal | None] = ContextVar("a2a_sdk_principal", default=None)


class ProtocolAuthMiddleware:
    """Authenticate only the SDK protocol mounts and pass identity to each tool call."""

    def __init__(self, app: ASGIApp, config: Settings):
        self.app = app
        self.config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        is_mcp = path == "/mcp-sdk" or path.startswith("/mcp-sdk/")
        is_a2a = path == "/a2a-sdk" or path.startswith("/a2a-sdk/")
        if not is_mcp and not is_a2a:
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        if is_mcp and not check_mcp_origin(request, self.config):
            response = JSONResponse({"detail": "Origin is not allowed."}, status_code=403)
            await response(scope, receive, send)
            return
        try:
            authorization = request.headers.get("authorization", "")
            scheme, separator, token = authorization.partition(" ")
            credentials = None
            if separator and scheme.lower() == "bearer" and token:
                from fastapi.security import HTTPAuthorizationCredentials

                credentials = HTTPAuthorizationCredentials(scheme=scheme, credentials=token)
            principal = get_principal(request, credentials)
        except HTTPException as exc:
            response = JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers=exc.headers,
            )
            await response(scope, receive, send)
            return

        scope.setdefault("state", {})["agentic_principal"] = principal
        marker = _current_principal.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_principal.reset(marker)


def _mcp_principal(context: Any) -> Principal:
    request = getattr(getattr(context, "request_context", None), "request", None)
    if request is not None:
        principal = request.scope.get("state", {}).get("agentic_principal")
        if principal:
            return principal
    principal = _current_principal.get()
    if principal:
        return principal
    raise PermissionError("Authenticated caller context is unavailable.")


def build_mcp_sdk_app(config: Settings, factory):
    from mcp.server import MCPServer
    from mcp.server.mcpserver import Context
    from mcp.server.transport_security import TransportSecuritySettings

    from app.custom_workflows import list_workflow_definitions, run_workflow_definition
    from app.policy import authorize
    from app.services import get_task, list_capabilities, serialize_task
    from app.workflows.engine import _search_documents

    server = MCPServer(
        "Agentic Atlas Platform",
        instructions=(
            "Run governed workflows, search authorized knowledge, inspect task status, "
            "and use custom workflows. External writes require human approval."
        ),
    )

    async def run_sync(func, *args):
        return await asyncio.to_thread(func, *args)

    def run_task_sync(scenario, message, artifacts, issue_key, context_id, principal):
        from app.services import create_task

        with factory() as session:
            request = TaskCreate(
                scenario=scenario,
                message=message,
                artifacts=artifacts,
                issue_key=issue_key,
                context_id=context_id,
            )
            return serialize_task(create_task(session, config, request, principal))

    @server.tool()
    async def run_agent_workflow(
        scenario: str,
        message: str,
        ctx: Context,
        artifacts: list[dict[str, str]] | None = None,
        issue_key: str | None = None,
        context_id: str | None = None,
    ) -> dict[str, Any]:
        """Run a governed knowledge, technical inventory, or incident workflow."""
        principal = _mcp_principal(ctx)
        return await run_sync(
            run_task_sync,
            scenario,
            message,
            artifacts or [],
            issue_key,
            context_id,
            principal,
        )

    def search_sync(query: str, principal: Principal):
        with factory() as session:
            allowed, reason = authorize(principal, "knowledge.read", {}, config)
            if not allowed:
                raise HTTPException(status_code=403, detail=reason)
            documents = _search_documents(session, query, principal.scopes, config, limit=5)
            return {
                "results": [
                    {
                        "title": doc.title,
                        "scope": doc.scope,
                        "source_uri": doc.source_uri,
                        "excerpt": doc.content[:420],
                        "score": round(score, 4),
                    }
                    for doc, score in documents
                ]
            }

    @server.tool()
    async def search_knowledge(query: str, ctx: Context) -> dict[str, Any]:
        """Search knowledge documents inside the authenticated caller's scopes."""
        return await run_sync(search_sync, query, _mcp_principal(ctx))

    def capabilities_sync(kind: str | None):
        with factory() as session:
            return {"capabilities": list_capabilities(session, kind)}

    @server.tool()
    async def list_capability_catalog(ctx: Context, kind: str | None = None) -> dict[str, Any]:
        """List registered agents, workflows, tools, and APIs."""
        _mcp_principal(ctx)
        return await run_sync(capabilities_sync, kind)

    def task_status_sync(task_id: str, principal: Principal):
        with factory() as session:
            return serialize_task(get_task(session, task_id, principal))

    @server.tool()
    async def get_task_status(task_id: str, ctx: Context) -> dict[str, Any]:
        """Read a task only when it belongs to the caller or their role can inspect it."""
        return await run_sync(task_status_sync, task_id, _mcp_principal(ctx))

    def workflows_sync():
        with factory() as session:
            return {"items": list_workflow_definitions(session)}

    @server.tool()
    async def list_workflows(ctx: Context) -> dict[str, Any]:
        """List enabled declarative workflows that can be started by this caller."""
        principal = _mcp_principal(ctx)
        allowed, reason = authorize(principal, "workflow.run", {}, config)
        if not allowed:
            raise HTTPException(status_code=403, detail=reason)
        return await run_sync(workflows_sync)

    def run_defined_sync(workflow_id, input_data, context_id, principal):
        with factory() as session:
            result = run_workflow_definition(
                session,
                config,
                workflow_id,
                WorkflowRunRequest(input=input_data, context_id=context_id),
                principal,
            )
            return result

    @server.tool()
    async def run_custom_workflow(
        workflow_id: str,
        input: dict[str, Any],
        ctx: Context,
        context_id: str | None = None,
    ) -> dict[str, Any]:
        """Start an administrator-defined workflow with a JSON input object."""
        return await run_sync(
            run_defined_sync,
            workflow_id,
            input,
            context_id,
            _mcp_principal(ctx),
        )

    allowed_hosts = [
        "localhost:*",
        "127.0.0.1:*",
        "[::1]:*",
        "a2a-api.a2a.svc.cluster.local:*",
    ]
    allowed_origins = ["http://localhost:*", "http://127.0.0.1:*", "http://[::1]:*"]
    public_host = (
        __import__("urllib.parse", fromlist=["urlparse"]).urlparse(config.public_url).hostname
    )
    if public_host:
        allowed_hosts.append(f"{public_host}:*")
        allowed_origins.extend([f"http://{public_host}:*", f"https://{public_host}:*"])
    if config.environment == "test":
        allowed_hosts.append("testserver")
        allowed_origins.append("http://testserver")
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )
    asgi_app = server.streamable_http_app(
        streamable_http_path="/",
        json_response=True,
        stateless_http=True,
        host="0.0.0.0",
        transport_security=security,
    )
    asgi_app.state.settings = config
    return server, asgi_app


def build_a2a_sdk_app(config: Settings, factory, public_url: str):
    from a2a.helpers import get_message_text, new_text_message
    from a2a.server.agent_execution import AgentExecutor
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.routes import (
        add_a2a_routes_to_fastapi,
        create_agent_card_routes,
        create_jsonrpc_routes,
    )
    from a2a.server.tasks import InMemoryTaskStore
    from a2a.types import (
        AgentCapabilities,
        AgentCard,
        AgentInterface,
        AgentSkill,
        Role,
    )
    from fastapi import FastAPI
    from google.protobuf.json_format import MessageToDict

    from app.services import create_task, serialize_task

    class GovernedAgentExecutor(AgentExecutor):
        async def execute(self, context, event_queue) -> None:
            principal = _current_principal.get()
            if principal is None:
                raise PermissionError("Authenticated caller context is unavailable.")
            message = context.message
            content = get_message_text(message) if message else ""
            message_dict = (
                MessageToDict(message, preserving_proto_field_name=True) if message else {}
            )
            metadata = message_dict.get("metadata") or {}
            request = TaskCreate(
                scenario=metadata.get("scenario", "knowledge"),
                message=content,
                artifacts=metadata.get("artifacts", []),
                issue_key=metadata.get("issue_key"),
                context_id=context.context_id,
            )

            def run_task():
                with factory() as session:
                    return serialize_task(create_task(session, config, request, principal))

            result = await asyncio.to_thread(run_task)
            await event_queue.enqueue_event(
                new_text_message(json.dumps(result, ensure_ascii=False), role=Role.ROLE_AGENT)
            )

        async def cancel(self, context, event_queue) -> None:
            return None

    card = AgentCard(
        name="Agentic Atlas Platform",
        description="Governed agent workflows with authorization, evidence, and approvals.",
        version="0.1.0",
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC",
                protocol_version="1.0",
                url=f"{public_url.rstrip('/')}/a2a-sdk/",
            ),
            AgentInterface(
                protocol_binding="JSONRPC",
                protocol_version="0.3",
                url=f"{public_url.rstrip('/')}/a2a-sdk/",
            ),
        ],
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain", "application/json"],
        capabilities=AgentCapabilities(streaming=False),
        skills=[
            AgentSkill(
                id="governed-workflows",
                name="Governed workflows",
                description="Run knowledge, technical inventory, and incident tasks.",
                tags=["knowledge", "inventory", "incidents", "approval"],
                examples=["Search approved policy documents."],
                input_modes=["text/plain", "application/json"],
                output_modes=["text/plain", "application/json"],
            )
        ],
    )
    handler = DefaultRequestHandler(
        agent_executor=GovernedAgentExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    child = FastAPI()
    add_a2a_routes_to_fastapi(
        child,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(
            handler,
            rpc_url="/",
            enable_v0_3_compat=True,
        ),
    )
    return child
