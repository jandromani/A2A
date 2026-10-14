from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc, func, select, text
from sqlalchemy.orm import Session

from app.config import Settings, settings
from app.connectors import (
    invoke_connector,
    list_connectors,
    register_connector,
    set_connector_enabled,
)
from app.connectors import mcp_tools as remote_mcp_tools
from app.database import get_db, initialize_database, make_engine, make_session_factory
from app.jira import JiraConnector
from app.mcp_transport import CURRENT_PROTOCOL_VERSION, decode_header_value
from app.model_gateway import gateway_status
from app.models import (
    ApprovalRecord,
    Capability,
    EvaluationRecord,
    TaskRecord,
)
from app.observability import RequestContextMiddleware, configure_tracing
from app.protocols import (
    MCP_PROTOCOL_VERSION,
    SUPPORTED_MCP_VERSIONS,
    a2a_agent_card,
    check_mcp_origin,
    handle_a2a,
    handle_mcp,
)
from app.schemas import (
    ApprovalDecision,
    CapabilityCreate,
    ConnectorCreate,
    ConnectorInvoke,
    KnowledgeCreate,
    Principal,
    TaskCreate,
)
from app.security import get_principal
from app.services import (
    add_capability,
    add_knowledge,
    cancel_task,
    create_task,
    decide_approval,
    delete_knowledge,
    disable_capability,
    get_task,
    list_activities,
    list_approvals,
    list_capabilities,
    list_evaluations,
    list_knowledge,
    list_tasks,
    serialize_task,
    task_events,
)
from app.usage import aggregate_model_usage

ROOT = Path(__file__).resolve().parent


def _database_session(request: Request) -> Session:
    return request.app.state.session_factory()


def create_app(config: Settings | None = None, database_url: str | None = None) -> FastAPI:
    config = config or settings
    if database_url:
        config = config.model_copy(update={"database_url": database_url})
    engine = make_engine(config.database_url)
    factory = make_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        initialize_database(engine, factory, config)
        yield
        engine.dispose()

    app = FastAPI(
        title=config.app_name,
        version="0.1.0",
        description="Governed agent workflows, capability catalog, A2A and MCP interfaces.",
        lifespan=lifespan,
    )
    app.state.settings = config
    app.state.engine = engine
    app.state.session_factory = factory
    app.add_middleware(RequestContextMiddleware)
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    configure_tracing(app)

    @app.get("/", include_in_schema=False)
    def console():
        return FileResponse(ROOT / "templates" / "index.html")

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "agentic-atlas-platform"}

    @app.get("/ready")
    def ready(request: Request):
        try:
            with request.app.state.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Database is not ready.") from exc
        return {"status": "ready", "database": "connected"}

    @app.get("/api/v1/system")
    def system_status(
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        capabilities = list_capabilities(db)
        counts = db.execute(
            select(TaskRecord.status, func.count(TaskRecord.id)).group_by(TaskRecord.status)
        ).all()
        pending_approvals = (
            db.scalar(
                select(func.count(ApprovalRecord.id)).where(ApprovalRecord.status == "pending")
            )
            or 0
        )
        return {
            "name": request.app.state.settings.app_name,
            "environment": request.app.state.settings.environment,
            "actor": principal.subject,
            "roles": sorted(principal.roles),
            "model": gateway_status(request.app.state.settings),
            "database": request.app.state.engine.dialect.name,
            "event_bus": "valkey-streams"
            if request.app.state.settings.redis_url
            else "durable SQL event log",
            "opa": "configured" if request.app.state.settings.opa_url else "local policy engine",
            "jira": "configured"
            if JiraConnector(request.app.state.settings).configured
            else "not configured",
            "capability_count": len(capabilities),
            "task_counts": {status: count for status, count in counts},
            "pending_approvals": pending_approvals,
            "protocols": {"a2a": "1.0 JSON-RPC", "mcp": MCP_PROTOCOL_VERSION},
            "secrets_in_database": False,
        }

    @app.get("/api/v1/dashboard")
    def dashboard(
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        tasks = list_tasks(db, principal, limit=8)
        total = db.scalar(select(func.count(TaskRecord.id))) or 0
        pending = (
            db.scalar(
                select(func.count(ApprovalRecord.id)).where(ApprovalRecord.status == "pending")
            )
            or 0
        )
        evaluation_count = db.scalar(select(func.count(EvaluationRecord.id))) or 0
        passed_count = (
            db.scalar(
                select(func.count(EvaluationRecord.id)).where(EvaluationRecord.passed.is_(True))
            )
            or 0
        )
        agents = db.scalar(select(func.count(Capability.id)).where(Capability.kind == "agent")) or 0
        tools = db.scalar(select(func.count(Capability.id)).where(Capability.kind == "tool")) or 0
        return {
            "tasks_total": total,
            "pending_approvals": pending,
            "evaluation_pass_rate": round(100 * passed_count / evaluation_count)
            if evaluation_count
            else None,
            "agents": agents,
            "tools": tools,
            "recent_tasks": tasks,
            "model": gateway_status(request.app.state.settings),
        }

    @app.get("/api/v1/catalog")
    def catalog(db: Session = Depends(get_db), principal: Principal = Depends(get_principal)):
        return {"capabilities": list_capabilities(db)}

    @app.post("/api/v1/catalog", status_code=201)
    def create_capability(
        body: CapabilityCreate,
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        return add_capability(db, request.app.state.settings, body, principal)

    @app.delete("/api/v1/catalog/{capability_id}", status_code=204)
    def remove_capability(
        capability_id: str,
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        disable_capability(db, request.app.state.settings, capability_id, principal)
        return Response(status_code=204)

    @app.get("/api/v1/connectors")
    def connectors(
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        if not principal.roles.intersection({"admin", "operator", "approver"}):
            raise HTTPException(status_code=403, detail="Operator role required.")
        return {"items": list_connectors(db, request.app.state.settings)}

    @app.post("/api/v1/connectors", status_code=201)
    def create_connector(
        body: ConnectorCreate,
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        return register_connector(db, request.app.state.settings, body, principal)

    @app.delete("/api/v1/connectors/{connector_id}")
    def disable_remote_connector(
        connector_id: str,
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        return set_connector_enabled(db, request.app.state.settings, connector_id, False, principal)

    @app.post("/api/v1/connectors/{connector_id}/enable")
    def enable_remote_connector(
        connector_id: str,
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        return set_connector_enabled(db, request.app.state.settings, connector_id, True, principal)

    @app.get("/api/v1/connectors/{connector_id}/tools")
    def connector_tools(
        connector_id: str,
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        return remote_mcp_tools(db, request.app.state.settings, connector_id, principal)

    @app.post("/api/v1/connectors/{connector_id}/invoke")
    def invoke_remote_connector(
        connector_id: str,
        body: ConnectorInvoke,
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        return invoke_connector(db, request.app.state.settings, connector_id, body, principal)

    @app.get("/api/v1/agents")
    def agents(db: Session = Depends(get_db), principal: Principal = Depends(get_principal)):
        return {"items": list_capabilities(db, "agent")}

    @app.get("/api/v1/tools")
    def tools(db: Session = Depends(get_db), principal: Principal = Depends(get_principal)):
        return {"items": list_capabilities(db, "tool")}

    @app.get("/api/v1/knowledge")
    def knowledge(
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        return {"items": list_knowledge(db, principal, request.app.state.settings)}

    @app.post("/api/v1/knowledge", status_code=201)
    def create_knowledge(
        body: KnowledgeCreate,
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        return add_knowledge(db, request.app.state.settings, body, principal)

    @app.delete("/api/v1/knowledge/{document_id}", status_code=204)
    def remove_knowledge(
        document_id: str,
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        delete_knowledge(db, request.app.state.settings, document_id, principal)
        return Response(status_code=204)

    @app.get("/api/v1/tasks")
    def tasks(db: Session = Depends(get_db), principal: Principal = Depends(get_principal)):
        return {"items": list_tasks(db, principal)}

    @app.post("/api/v1/tasks", status_code=201)
    def start_task(
        body: TaskCreate,
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        content_size = sum(len(item.content.encode("utf-8")) for item in body.artifacts)
        if len(body.artifacts) > request.app.state.settings.max_task_artifacts:
            raise HTTPException(status_code=413, detail="Too many files in the request.")
        if content_size > request.app.state.settings.max_artifact_bytes:
            raise HTTPException(
                status_code=413, detail="Attached configuration files exceed the size limit."
            )
        return serialize_task(create_task(db, request.app.state.settings, body, principal))

    @app.get("/api/v1/tasks/{task_id}")
    def task_detail(
        task_id: str,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        return serialize_task(get_task(db, task_id, principal))

    @app.get("/api/v1/tasks/{task_id}/events")
    def task_event_list(
        task_id: str,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        return {"items": task_events(db, task_id, principal)}

    @app.post("/api/v1/tasks/{task_id}/cancel")
    def stop_task(
        task_id: str,
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        return cancel_task(db, request.app.state.settings, task_id, principal)

    @app.get("/api/v1/approvals")
    def approvals(
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        if not principal.roles.intersection({"admin", "approver"}):
            raise HTTPException(status_code=403, detail="Approver role required.")
        return {"items": list_approvals(db)}

    @app.post("/api/v1/approvals/{approval_id}/decision")
    def approval_decision(
        approval_id: str,
        body: ApprovalDecision,
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        return decide_approval(db, request.app.state.settings, approval_id, body, principal)

    @app.get("/api/v1/evaluations")
    def evaluations(
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        if not principal.roles.intersection({"admin", "operator", "approver"}):
            raise HTTPException(status_code=403, detail="Operator role required.")
        return {"items": list_evaluations(db)}

    @app.get("/api/v1/usage")
    def model_usage(
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        if not principal.roles.intersection({"admin", "operator", "approver"}):
            raise HTTPException(status_code=403, detail="Operator role required.")
        config = request.app.state.settings
        results = db.scalars(
            select(TaskRecord.result_json).order_by(desc(TaskRecord.created_at)).limit(1000)
        ).all()
        return aggregate_model_usage(
            results,
            pricing_configured=(
                config.model_input_cost_per_million is not None
                and config.model_output_cost_per_million is not None
            ),
        )

    @app.get("/api/v1/activity")
    def activity(
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        if not principal.roles.intersection({"admin", "operator", "approver"}):
            raise HTTPException(status_code=403, detail="Operator role required.")
        return {"items": list_activities(db)}

    @app.get("/.well-known/agent-card.json", include_in_schema=False)
    def agent_card(request: Request):
        return a2a_agent_card(request.app.state.settings.public_url, request.app.state.settings)

    @app.post("/a2a", include_in_schema=False)
    def a2a(
        rpc: dict[str, Any],
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        version = request.headers.get("a2a-version", "0.3")
        if version not in {"0.3", "1.0"}:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": rpc.get("id"),
                    "error": {
                        "code": -32009,
                        "message": f"A2A protocol version {version} is not supported.",
                        "data": {"supportedVersions": ["1.0"]},
                    },
                },
                status_code=400,
            )
        return handle_a2a(rpc, db, request.app.state.settings, principal)

    @app.get("/mcp", include_in_schema=False)
    def mcp_get():
        return Response(status_code=405, headers={"Allow": "POST"})

    @app.post("/mcp", include_in_schema=False)
    def mcp(
        rpc: dict[str, Any],
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        config = request.app.state.settings
        if not check_mcp_origin(request, config):
            raise HTTPException(status_code=403, detail="Origin is not allowed.")
        accept = request.headers.get("accept", "")
        if "application/json" not in accept or "text/event-stream" not in accept:
            raise HTTPException(
                status_code=406,
                detail="MCP clients must accept application/json and text/event-stream.",
            )
        method = rpc.get("method")
        version_header = request.headers.get("mcp-protocol-version")
        params = rpc.get("params") or {}
        if not isinstance(method, str) or not isinstance(params, dict):
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": rpc.get("id"),
                    "error": {"code": -32600, "message": "Invalid MCP JSON-RPC request."},
                },
                status_code=400,
            )
        if method != "initialize":
            if version_header not in SUPPORTED_MCP_VERSIONS:
                return JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": rpc.get("id"),
                        "error": {
                            "code": -32022,
                            "message": "A supported MCP-Protocol-Version is required.",
                            "data": {"supported": sorted(SUPPORTED_MCP_VERSIONS)},
                        },
                    },
                    status_code=400,
                )
            if version_header == CURRENT_PROTOCOL_VERSION:
                metadata = params.get("_meta") or {}
                if not isinstance(metadata, dict):
                    metadata = {}
                protocol_meta = metadata.get("io.modelcontextprotocol/protocolVersion")
                method_header = request.headers.get("mcp-method")
                client_info = metadata.get("io.modelcontextprotocol/clientInfo")
                client_capabilities = metadata.get("io.modelcontextprotocol/clientCapabilities")
                if (
                    protocol_meta != version_header
                    or method_header != method
                    or not isinstance(client_info, dict)
                    or not isinstance(client_capabilities, dict)
                ):
                    return JSONResponse(
                        {
                            "jsonrpc": "2.0",
                            "id": rpc.get("id"),
                            "error": {
                                "code": -32020,
                                "message": "MCP request headers do not match request metadata.",
                            },
                        },
                        status_code=400,
                    )
                if method in {"tools/call", "resources/read", "prompts/get"}:
                    name = params.get("uri") if method == "resources/read" else params.get("name")
                    try:
                        header_name = decode_header_value(request.headers.get("mcp-name", ""))
                    except ValueError:
                        header_name = None
                    if not name or header_name != name:
                        return JSONResponse(
                            {
                                "jsonrpc": "2.0",
                                "id": rpc.get("id"),
                                "error": {
                                    "code": -32020,
                                    "message": "Mcp-Name does not match the request body.",
                                },
                            },
                            status_code=400,
                        )
        http_status, body = handle_mcp(rpc, db, config, principal)
        if body is None:
            return Response(status_code=http_status)
        return JSONResponse(body, status_code=http_status)

    @app.get("/metrics", include_in_schema=False)
    def metrics(db: Session = Depends(get_db)):
        task_counts = db.execute(
            select(TaskRecord.status, func.count(TaskRecord.id)).group_by(TaskRecord.status)
        ).all()
        lines = [
            "# HELP agentic_tasks_total Number of tasks by state.",
            "# TYPE agentic_tasks_total gauge",
        ]
        lines.extend(
            f'agentic_tasks_total{{status="{status}"}} {count}' for status, count in task_counts
        )
        pending_approvals = (
            db.scalar(
                select(func.count(ApprovalRecord.id)).where(ApprovalRecord.status == "pending")
            )
            or 0
        )
        lines.extend(
            [
                "# HELP agentic_approvals_pending Pending human approvals.",
                "# TYPE agentic_approvals_pending gauge",
                f"agentic_approvals_pending {pending_approvals}",
            ]
        )
        return PlainTextResponse("\n".join(lines) + "\n")

    return app


app = create_app()
