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
    from a2a_tools_sdk import ToolContext, ToolRegistry
    from mcp.server import MCPServer
    from mcp.server.transport_security import TransportSecuritySettings

    from app.custom_workflows import list_workflow_definitions, run_workflow_definition
    from app.policy import authorize
    from app.services import create_task, get_task, list_capabilities, serialize_task
    from app.tool_contracts import (
        EmptyToolInput,
        GetTaskStatusInput,
        ListCapabilitiesInput,
        RunAgentWorkflowInput,
        RunCustomWorkflowInput,
        SearchKnowledgeInput,
    )
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

    def principal_from_context(context: ToolContext) -> Principal:
        return Principal(
            subject=context.subject,
            roles=context.roles,
            scopes=context.scopes,
        )

    def sdk_context(mcp_context: Any) -> ToolContext:
        principal = _mcp_principal(mcp_context)
        return ToolContext(
            subject=principal.subject,
            scopes=principal.scopes,
            roles=principal.roles,
        )

    registry = ToolRegistry()

    def run_task_sync(request: RunAgentWorkflowInput, principal: Principal):
        with factory() as session:
            task_request = TaskCreate(
                scenario=request.scenario,
                message=request.message,
                artifacts=request.artifacts,
                issue_key=request.issue_key,
                context_id=request.context_id,
            )
            return serialize_task(create_task(session, config, task_request, principal))

    async def run_workflow_tool(arguments: RunAgentWorkflowInput, context: ToolContext):
        return await run_sync(run_task_sync, arguments, principal_from_context(context))

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

    async def search_knowledge_tool(arguments: SearchKnowledgeInput, context: ToolContext):
        return await run_sync(search_sync, arguments.query, principal_from_context(context))

    def capabilities_sync(kind: str | None):
        with factory() as session:
            return {"capabilities": list_capabilities(session, kind)}

    async def list_capabilities_tool(arguments: ListCapabilitiesInput, _context: ToolContext):
        return await run_sync(capabilities_sync, arguments.kind)

    def task_status_sync(task_id: str, principal: Principal):
        with factory() as session:
            return serialize_task(get_task(session, task_id, principal))

    async def get_task_status_tool(arguments: GetTaskStatusInput, context: ToolContext):
        return await run_sync(
            task_status_sync,
            arguments.task_id,
            principal_from_context(context),
        )

    def workflows_sync():
        with factory() as session:
            return {"items": list_workflow_definitions(session)}

    async def list_workflows_tool(_arguments: EmptyToolInput, context: ToolContext):
        principal = principal_from_context(context)
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

    async def run_custom_workflow_tool(
        arguments: RunCustomWorkflowInput,
        context: ToolContext,
    ) -> dict[str, Any]:
        return await run_sync(
            run_defined_sync,
            arguments.workflow_id,
            arguments.input,
            arguments.context_id,
            principal_from_context(context),
        )

    registry.register(
        "run_agent_workflow",
        "Run a governed knowledge, technical inventory, or incident workflow.",
        RunAgentWorkflowInput,
        run_workflow_tool,
    )
    registry.register(
        "search_knowledge",
        "Search knowledge documents inside the authenticated caller's scopes.",
        SearchKnowledgeInput,
        search_knowledge_tool,
    )
    registry.register(
        "list_capability_catalog",
        "List registered agents, workflows, tools, and APIs.",
        ListCapabilitiesInput,
        list_capabilities_tool,
    )
    registry.register(
        "get_task_status",
        "Read a task only when it belongs to the caller or their role can inspect it.",
        GetTaskStatusInput,
        get_task_status_tool,
    )
    registry.register(
        "list_workflows",
        "List enabled declarative workflows that can be started by this caller.",
        EmptyToolInput,
        list_workflows_tool,
    )
    registry.register(
        "run_custom_workflow",
        "Start an administrator-defined workflow with a JSON input object.",
        RunCustomWorkflowInput,
        run_custom_workflow_tool,
    )
    registry.register_mcp_tools(server, context_provider=sdk_context)

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
