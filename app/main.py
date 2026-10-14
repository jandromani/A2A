import asyncio
import json
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.audit import replay_task_timeline, verify_audit_chain
from app.config import Settings, settings
from app.connectors import (
    invoke_connector,
    list_connectors,
    register_connector,
    set_connector_enabled,
)
from app.connectors import mcp_tools as remote_mcp_tools
from app.custom_workflows import (
    create_workflow_definition,
    list_workflow_definitions,
    run_workflow_definition,
)
from app.data_sources import (
    disable_data_source,
    ingest_event_batch,
    list_data_sources,
    queue_data_source_sync,
    register_data_source,
    sync_data_source,
)
from app.database import get_db, initialize_database, make_engine, make_session_factory
from app.event_worker import run_event_worker
from app.identity import (
    list_agent_identities,
    register_agent_identity,
    transition_agent_identity,
)
from app.integrations import (
    github_issues,
    gmail_message,
    gmail_search,
    request_github_comment,
    request_gmail_send,
)
from app.jira import JiraConnector
from app.mcp_transport import CURRENT_PROTOCOL_VERSION, decode_header_value
from app.memory import conversation_for_actor, delete_conversation
from app.model_gateway import gateway_status
from app.models import (
    ApprovalRecord,
    Capability,
    EvaluationRecord,
    TaskRecord,
    WorkflowCheckpoint,
)
from app.mqtt_ingress import MQTTIngress
from app.observability import RequestContextMiddleware, configure_tracing
from app.policy import authorize
from app.protocols import (
    MCP_PROTOCOL_VERSION,
    SUPPORTED_MCP_VERSIONS,
    a2a_agent_card,
    check_mcp_origin,
    handle_a2a,
    handle_mcp,
)
from app.schemas import (
    AgentIdentityCreate,
    ApprovalDecision,
    CapabilityCreate,
    ConnectorCreate,
    ConnectorInvoke,
    DataSourceCreate,
    DataSourceEventBatch,
    GitHubCommentCreate,
    GmailMessageCreate,
    KnowledgeCreate,
    Principal,
    TaskCreate,
    WorkflowDefinitionCreate,
    WorkflowRunRequest,
)
from app.sdk_protocols import ProtocolAuthMiddleware, build_a2a_sdk_app, build_mcp_sdk_app
from app.secrets import resolve_secret
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
from app.usage import aggregate_usage_ledger, list_usage_calls

ROOT = Path(__file__).resolve().parent
EXPECTED_SCHEMA_VERSION = "0003_governance_ledger"


def _require_authorized(config: Settings, principal: Principal, action: str) -> None:
    allowed, reason = authorize(principal, action, {}, config)
    if not allowed:
        raise HTTPException(status_code=403, detail=reason)


def _database_session(request: Request) -> Session:
    return request.app.state.session_factory()


def create_app(config: Settings | None = None, database_url: str | None = None) -> FastAPI:
    config = config or settings
    if database_url:
        config = config.model_copy(update={"database_url": database_url})
    resolved_database_url = resolve_secret(config, config.database_url)
    if not resolved_database_url:
        raise RuntimeError("DATABASE_URL is not configured.")
    if config.environment == "production" and not resolved_database_url.startswith(
        ("postgresql://", "postgresql+psycopg://")
    ):
        raise RuntimeError("Production DATABASE_URL must resolve to PostgreSQL.")
    engine = make_engine(resolved_database_url)
    factory = make_session_factory(engine)
    mcp_sdk_server, mcp_sdk_app = build_mcp_sdk_app(config, factory)
    a2a_sdk_app = build_a2a_sdk_app(config, factory, config.public_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        initialize_database(engine, factory, config)
        worker_task = None
        mqtt_ingress = None
        app.state.event_worker_running = False
        app.state.mqtt_ingress_running = False
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(mcp_sdk_server.session_manager.run())
            try:
                if config.event_worker_enabled:
                    worker_task = asyncio.create_task(run_event_worker(factory, config))
                    app.state.event_worker_running = True
                if config.mqtt_broker_url:
                    mqtt_ingress = MQTTIngress(factory, config)
                    mqtt_ingress.start()
                    app.state.mqtt_ingress_running = True
                yield
            finally:
                if worker_task:
                    worker_task.cancel()
                    try:
                        await worker_task
                    except asyncio.CancelledError:
                        pass
                app.state.event_worker_running = False
                if mqtt_ingress:
                    mqtt_ingress.stop()
                app.state.mqtt_ingress_running = False
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
    app.add_middleware(ProtocolAuthMiddleware, config=config)
    app.mount("/mcp-sdk", mcp_sdk_app, name="mcp-sdk")
    app.mount("/a2a-sdk", a2a_sdk_app, name="a2a-sdk")
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    configure_tracing(app, config)

    @app.get("/", include_in_schema=False)
    def console():
        return FileResponse(ROOT / "templates" / "index.html")

    @app.get("/service-worker.js", include_in_schema=False)
    def service_worker():
        return FileResponse(
            ROOT / "static" / "service-worker.js",
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
        )

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "agentic-atlas-platform"}

    @app.get("/ready")
    def ready(request: Request):
        try:
            with request.app.state.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                schema_version = None
                if request.app.state.settings.environment == "production":
                    schema_version = connection.scalar(
                        text("SELECT version_num FROM alembic_version")
                    )
                    if schema_version != EXPECTED_SCHEMA_VERSION:
                        raise RuntimeError(
                            "Database migrations have not reached the required version."
                        )
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Database is not ready.") from exc
        response = {"status": "ready", "database": "connected"}
        if request.app.state.settings.environment == "production":
            response["schema_version"] = schema_version
        return response

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
            "event_worker": "running"
            if getattr(request.app.state, "event_worker_running", False)
            else "external worker required"
            if request.app.state.settings.task_execution_mode == "queue"
            else "disabled",
            "mqtt_ingress": "enabled"
            if getattr(request.app.state, "mqtt_ingress_running", False)
            else "disabled",
            "memory": "durable SQL checkpointed conversations",
            "opa": "configured" if request.app.state.settings.opa_url else "local policy engine",
            "secret_manager": request.app.state.settings.secret_provider,
            "langfuse": "configured"
            if request.app.state.settings.langfuse_host
            and request.app.state.settings.langfuse_public_key
            and request.app.state.settings.langfuse_secret_key
            else "not configured",
            "jira": "configured"
            if JiraConnector(request.app.state.settings).configured
            else "not configured",
            "capability_count": len(capabilities),
            "task_counts": {status: count for status, count in counts},
            "pending_approvals": pending_approvals,
            "integrations": {
                "github": bool(request.app.state.settings.github_token),
                "gmail": bool(
                    request.app.state.settings.google_client_id
                    and request.app.state.settings.google_client_secret
                    and request.app.state.settings.google_refresh_token
                ),
            },
            "protocols": {
                "a2a": "A2A 1.0 official SDK · /a2a-sdk/",
                "mcp": f"MCP official SDK · {MCP_PROTOCOL_VERSION} · /mcp-sdk/",
            },
            "secrets_in_database": False,
        }

    @app.get("/api/v1/agent-identities")
    def agent_identities(
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        return {"items": list_agent_identities(db, request.app.state.settings, principal)}

    @app.post("/api/v1/agent-identities", status_code=201)
    def create_agent_identity(
        body: AgentIdentityCreate,
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        return register_agent_identity(db, request.app.state.settings, body, principal)

    @app.post("/api/v1/agent-identities/{client_id}/activate")
    def activate_agent_identity(
        client_id: str,
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        return transition_agent_identity(
            db, request.app.state.settings, client_id, "active", principal
        )

    @app.post("/api/v1/agent-identities/{client_id}/suspend")
    def suspend_agent_identity(
        client_id: str,
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        return transition_agent_identity(
            db, request.app.state.settings, client_id, "suspended", principal
        )

    @app.post("/api/v1/agent-identities/{client_id}/revoke")
    def revoke_agent_identity(
        client_id: str,
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        return transition_agent_identity(
            db, request.app.state.settings, client_id, "revoked", principal
        )

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
        _require_authorized(request.app.state.settings, principal, "connector.read")
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

    @app.get("/api/v1/data-sources")
    def data_sources(
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        _require_authorized(request.app.state.settings, principal, "data.source.read")
        return {"items": list_data_sources(db, request.app.state.settings)}

    @app.post("/api/v1/data-sources", status_code=201)
    def create_data_source(
        body: DataSourceCreate,
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        return register_data_source(db, request.app.state.settings, body, principal)

    @app.post("/api/v1/data-sources/{source_id}/sync")
    def run_data_source_sync(
        source_id: str,
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        config = request.app.state.settings
        if config.event_worker_enabled or config.task_execution_mode == "queue":
            return queue_data_source_sync(db, config, source_id, principal)
        return sync_data_source(db, config, source_id, principal)

    @app.post("/api/v1/data-sources/{source_id}/events", status_code=202)
    def receive_data_source_events(
        source_id: str,
        body: DataSourceEventBatch,
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        body_size = sum(
            len(json.dumps(item, ensure_ascii=False).encode("utf-8")) for item in body.records
        )
        if body_size > request.app.state.settings.max_artifact_bytes:
            raise HTTPException(
                status_code=413, detail="Pipeline event batch exceeds the size limit."
            )
        return ingest_event_batch(db, request.app.state.settings, source_id, body, principal)

    @app.delete("/api/v1/data-sources/{source_id}", status_code=204)
    def remove_data_source(
        source_id: str,
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        disable_data_source(db, request.app.state.settings, source_id, principal)
        return Response(status_code=204)

    @app.get("/api/v1/workflows")
    def workflow_definitions(
        db: Session = Depends(get_db), principal: Principal = Depends(get_principal)
    ):
        return {"items": list_workflow_definitions(db)}

    @app.post("/api/v1/workflows", status_code=201)
    def create_workflow(
        body: WorkflowDefinitionCreate,
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        return create_workflow_definition(db, request.app.state.settings, body, principal)

    @app.post("/api/v1/workflows/{workflow_id}/runs", status_code=201)
    def run_defined_workflow(
        workflow_id: str,
        body: WorkflowRunRequest,
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        return run_workflow_definition(db, request.app.state.settings, workflow_id, body, principal)

    @app.get("/api/v1/integrations/github/{owner}/{repository}/issues")
    def github_issue_list(
        owner: str,
        repository: str,
        request: Request,
        state: str = "open",
        limit: int = 30,
        principal: Principal = Depends(get_principal),
    ):
        return github_issues(
            request.app.state.settings, principal, owner, repository, state=state, limit=limit
        )

    @app.post(
        "/api/v1/integrations/github/{owner}/{repository}/issues/{issue_number}/comments",
        status_code=202,
    )
    def github_comment_request(
        owner: str,
        repository: str,
        issue_number: str,
        body: GitHubCommentCreate,
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        return request_github_comment(
            db, request.app.state.settings, principal, owner, repository, issue_number, body
        )

    @app.get("/api/v1/integrations/gmail/search")
    def gmail_message_search(
        request: Request,
        q: str = "",
        limit: int = 20,
        principal: Principal = Depends(get_principal),
    ):
        return gmail_search(request.app.state.settings, principal, q, limit)

    @app.get("/api/v1/integrations/gmail/messages/{message_id}")
    def gmail_message_detail(
        message_id: str,
        request: Request,
        principal: Principal = Depends(get_principal),
    ):
        return gmail_message(request.app.state.settings, principal, message_id)

    @app.post("/api/v1/integrations/gmail/messages", status_code=202)
    def gmail_send_request(
        body: GmailMessageCreate,
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        return request_gmail_send(db, request.app.state.settings, principal, body)

    @app.get("/api/v1/conversations/{context_id}")
    def get_conversation_memory(
        context_id: str,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        memory = conversation_for_actor(db, principal, context_id)
        if not memory:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        return memory

    @app.delete("/api/v1/conversations/{context_id}", status_code=204)
    def remove_conversation_memory(
        context_id: str,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        if not delete_conversation(db, principal, context_id):
            raise HTTPException(status_code=404, detail="Conversation not found.")
        db.commit()
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

    @app.get("/api/v1/tasks/{task_id}/checkpoints")
    def task_checkpoints(
        task_id: str,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        task = get_task(db, task_id, principal)
        checkpoints = db.scalars(
            select(WorkflowCheckpoint)
            .where(WorkflowCheckpoint.task_id == task.id)
            .order_by(WorkflowCheckpoint.created_at.asc())
        ).all()
        return {
            "items": [
                {
                    "id": item.id,
                    "step": item.step,
                    "state": item.state_json,
                    "created_at": item.created_at.isoformat(),
                }
                for item in checkpoints
            ]
        }

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
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        _require_authorized(request.app.state.settings, principal, "approval.read")
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
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        _require_authorized(request.app.state.settings, principal, "evaluation.read")
        return {"items": list_evaluations(db)}

    @app.get("/api/v1/usage")
    def model_usage(
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        _require_authorized(request.app.state.settings, principal, "usage.read")
        return aggregate_usage_ledger(db)

    @app.get("/api/v1/usage/calls")
    def model_usage_calls(
        request: Request,
        limit: int = 100,
        cursor: str | None = None,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        _require_authorized(request.app.state.settings, principal, "usage.read")
        if not 1 <= limit <= 500:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 500.")
        try:
            return list_usage_calls(db, limit=limit, cursor=cursor)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/activity")
    def activity(
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        _require_authorized(request.app.state.settings, principal, "audit.read")
        return {"items": list_activities(db)}

    @app.get("/api/v1/audit/verify")
    def audit_verification(
        request: Request,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        _require_authorized(request.app.state.settings, principal, "audit.read")
        return verify_audit_chain(db)

    @app.get("/api/v1/tasks/{task_id}/replay")
    def replay_task(
        task_id: str,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_principal),
    ):
        get_task(db, task_id, principal)
        return replay_task_timeline(db, task_id)

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
