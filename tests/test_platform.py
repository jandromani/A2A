import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.config import Settings
from app.connectors import _mcp_exchange, _rpc_response, validate_outbound_endpoint
from app.mcp_transport import CURRENT_PROTOCOL_VERSION, decode_header_value, encode_header_value
from app.models import (
    ApprovalRecord,
    ConnectorRecord,
    ConversationMemory,
    KnowledgeDocument,
    OutboxEvent,
    TaskRecord,
)
from app.schemas import ArtifactInput, Principal
from app.workflows.engine import inspect_repository_files


def test_knowledge_workflow_persists_citations_and_evaluation(client):
    response = client.post(
        "/api/v1/tasks",
        json={"scenario": "knowledge", "message": "¿Cómo justifico un gasto de viaje?"},
    )

    assert response.status_code == 201
    task = response.json()
    assert task["status"] == "completed"
    assert task["result"]["citations"]
    assert task["result"]["evaluation"]["passed"] is True
    assert client.get(f"/api/v1/tasks/{task['id']}/events").json()["items"]


def test_console_exposes_data_and_first_party_integration_controls(client):
    response = client.get("/")
    assert response.status_code == 200
    for element_id in (
        "data-source-form",
        "data-sources-list",
        "github-search-form",
        "github-comment-form",
        "gmail-search-form",
        "gmail-send-form",
    ):
        assert f'id="{element_id}"' in response.text
    stylesheet = client.get("/static/integrations.css")
    assert stylesheet.status_code == 200


def test_report_extracts_only_supported_configuration(client):
    response = client.post(
        "/api/v1/tasks",
        json={
            "scenario": "report",
            "message": "Inspecciona las dependencias del proyecto.",
            "artifacts": [
                {
                    "path": "package.json",
                    "content": json.dumps(
                        {"name": "demo", "version": "2.1.0", "dependencies": {"fastapi": "^0.115"}}
                    ),
                },
                {"path": "deploy.sh", "content": "rm -rf /"},
            ],
        },
    )

    assert response.status_code == 201
    result = response.json()["result"]
    assert result["inspected_files"] == ["package.json"]
    assert result["skipped_files"][0]["path"] == "deploy.sh"
    assert any(item["value"] == "fastapi@^0.115" for item in result["findings"])
    assert all(item["path"] and item["evidence"] for item in result["findings"])


def test_incident_requires_a_human_decision(client):
    task_response = client.post(
        "/api/v1/tasks",
        json={
            "scenario": "incident",
            "message": "Prepara una revisión del pedido.",
            "context_id": "incident-approval-memory",
        },
    )
    task = task_response.json()
    assert task_response.status_code == 201
    assert task["status"] == "awaiting_approval"
    assert task["result"]["external_write"] == "pending human approval"

    approval_id = task["result"]["approval_id"]
    decision = client.post(
        f"/api/v1/approvals/{approval_id}/decision",
        json={"decision": "approve", "comment": "Revisado."},
    )
    assert decision.status_code == 200
    assert decision.json()["task"]["status"] == "completed"
    assert decision.json()["external_effect"] == "local approval record only"
    memory = client.get("/api/v1/conversations/incident-approval-memory").json()
    assert memory["checkpoint"]["status"] == "completed"
    assert len(memory["messages"]) == 2


def test_custom_workflow_resumes_from_checkpoint_after_approval(client):
    definition = {
        "id": "approval-resume-demo",
        "name": "Flujo con revisión",
        "description": "Se pausa, obtiene aprobación y continúa desde el checkpoint.",
        "steps": [
            {"id": "review", "type": "approval", "config": {"draft": "Revisar el cambio."}},
            {
                "id": "finish",
                "type": "result",
                "config": {"summary": "Continuó: {{steps.review.status}}"},
            },
        ],
    }
    created = client.post("/api/v1/workflows", json=definition)
    assert created.status_code == 201
    run = client.post(
        "/api/v1/workflows/approval-resume-demo/runs",
        json={"input": {"message": "Revisar"}, "context_id": "resume-test"},
    )
    assert run.status_code == 201
    assert run.json()["status"] == "awaiting_approval"
    approval_id = run.json()["result"]["steps"]["review"]["approval_id"]

    decision = client.post(
        f"/api/v1/approvals/{approval_id}/decision",
        json={"decision": "approve", "comment": "Revisado."},
    )
    assert decision.status_code == 200
    task = decision.json()["task"]
    assert task["status"] == "completed"
    assert task["result"]["summary"] == "Continuó: approved"
    assert (
        client.get("/api/v1/conversations/resume-test").json()["checkpoint"]["status"]
        == "completed"
    )


def test_conversation_memory_is_durable_and_actor_scoped(client):
    task = client.post(
        "/api/v1/tasks",
        json={
            "scenario": "knowledge",
            "message": "¿Cómo justifico un gasto de viaje?",
            "context_id": "browser-thread-123",
        },
    ).json()
    conversation = client.get("/api/v1/conversations/browser-thread-123")
    assert conversation.status_code == 200
    assert conversation.json()["checkpoint"]["task_id"] == task["id"]
    assert conversation.json()["messages"][-2]["role"] == "user"
    assert conversation.json()["messages"][-1]["role"] == "assistant"
    with client.app.state.session_factory() as session:
        memory = session.scalar(
            select(ConversationMemory).where(ConversationMemory.context_id == "browser-thread-123")
        )
        memory.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    expired = client.get("/api/v1/conversations/browser-thread-123")
    assert expired.status_code == 404


def test_a2a_agent_card_and_message_send(client):
    card = client.get("/.well-known/agent-card.json")
    assert card.status_code == 200
    assert card.json()["supportedInterfaces"][0]["url"].endswith("/a2a-sdk/")
    sdk_card = client.get("/a2a-sdk/.well-known/agent-card.json")
    assert sdk_card.status_code == 200
    assert sdk_card.json()["supportedInterfaces"][0]["protocolVersion"] == "1.0"

    response = client.post(
        "/a2a",
        headers={"A2A-Version": "1.0"},
        json={
            "jsonrpc": "2.0",
            "id": "case-1",
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": "message-1",
                    "role": "ROLE_USER",
                    "parts": [{"text": "¿Qué justificantes se necesitan para viajar?"}],
                }
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"

    legacy_response = client.post(
        "/a2a",
        headers={"A2A-Version": "0.3"},
        json={
            "jsonrpc": "2.0",
            "id": "legacy-1",
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"text": "Consulta otra vez la política de viajes."}],
                }
            },
        },
    )
    assert legacy_response.status_code == 200
    assert legacy_response.json()["result"]["status"]["state"] == "TASK_STATE_COMPLETED"


def test_mcp_initialize_and_tools_list(client):
    headers = {"Accept": "application/json, text/event-stream"}
    initialized = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "tests", "version": "1"},
            },
        },
    )
    assert initialized.status_code == 200
    assert initialized.json()["result"]["protocolVersion"] == "2025-11-25"

    listed = client.post(
        "/mcp",
        headers={**headers, "MCP-Protocol-Version": "2025-11-25"},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    assert listed.status_code == 200
    assert {tool["name"] for tool in listed.json()["result"]["tools"]} >= {
        "run_agent_workflow",
        "search_knowledge",
    }


def test_official_mcp_sdk_server_mount(client):
    initialized = client.post(
        "/mcp-sdk/",
        headers={"Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "tests", "version": "1"},
            },
        },
    )
    assert initialized.status_code == 200
    assert initialized.json()["result"]["serverInfo"]["name"] == "Agentic Atlas Platform"


def test_mcp_current_streamable_http_metadata_is_required(client):
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": CURRENT_PROTOCOL_VERSION,
        "Mcp-Method": "tools/list",
    }
    request = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/list",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": CURRENT_PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientInfo": {"name": "tests", "version": "1"},
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    }
    response = client.post("/mcp", headers=headers, json=request)
    assert response.status_code == 200
    assert response.json()["result"]["tools"]

    mismatched = client.post(
        "/mcp", headers={**headers, "Mcp-Method": "resources/list"}, json=request
    )
    assert mismatched.status_code == 400
    assert mismatched.json()["error"]["code"] == -32020


def test_mcp_remote_client_sends_latest_headers_and_encodes_names(monkeypatch):
    observed = {}

    def fake_call(endpoint, headers, method, params):
        observed.update(
            {"endpoint": endpoint, "headers": headers, "method": method, "params": params}
        )
        return {"ok": True}

    monkeypatch.setattr("app.connectors.call_mcp_sdk", fake_call)
    connector = ConnectorRecord(
        kind="mcp",
        name="Remote",
        description="",
        endpoint="https://mcp.example.com/mcp",
        protocol_version=CURRENT_PROTOCOL_VERSION,
    )
    config = Settings(_env_file=None, environment="test", auth_mode="dev")
    result = _mcp_exchange(
        connector, config, "tools/call", {"name": "buscar_área", "arguments": {}}
    )

    assert result == {"ok": True}
    assert observed["endpoint"] == connector.endpoint
    assert observed["method"] == "tools/call"
    assert observed["params"]["name"] == "buscar_área"
    assert observed["headers"]["Accept"] == "application/json, text/event-stream"


def test_github_write_occurs_only_after_approval(client, monkeypatch):
    client.app.state.settings = Settings(
        _env_file=None,
        environment="test",
        auth_mode="dev",
        database_url=client.app.state.settings.database_url,
        github_token="github-test-token",
    )
    calls = []

    def fake_get(url, **kwargs):
        calls.append(("GET", url))
        return httpx.Response(200, json=[], request=httpx.Request("GET", url))

    def fake_post(url, **kwargs):
        calls.append(("POST", url, kwargs.get("json")))
        return httpx.Response(201, json={"id": 42}, request=httpx.Request("POST", url))

    monkeypatch.setattr("app.integrations.httpx.get", fake_get)
    monkeypatch.setattr("app.integrations.httpx.post", fake_post)
    requested = client.post(
        "/api/v1/integrations/github/acme/platform/issues/7/comments",
        json={"body": "Preparar una actualización."},
    )
    assert requested.status_code == 202
    assert not calls

    decision = client.post(
        f"/api/v1/approvals/{requested.json()['approval_id']}/decision",
        json={"decision": "approve", "comment": "Aprobado."},
    )
    assert decision.status_code == 200
    assert decision.json()["task"]["status"] == "completed"
    assert [call[0] for call in calls] == ["GET", "POST"]
    assert "agentic-approval:" in calls[-1][2]["body"]


def test_gmail_send_waits_for_human_approval(client, monkeypatch):
    config = Settings(
        _env_file=None,
        environment="test",
        auth_mode="dev",
        database_url=client.app.state.settings.database_url,
        google_client_id="client-id",
        google_client_secret="client-secret",
        google_refresh_token="refresh-token",
    )
    client.app.state.settings = config
    calls = []

    def fake_post(url, **kwargs):
        calls.append(("POST", url))
        return httpx.Response(
            200,
            json={"access_token": "access-token", "expires_in": 3600},
            request=httpx.Request("POST", url),
        )

    def fake_request(method, url, **kwargs):
        calls.append((method, url))
        if method == "GET":
            return httpx.Response(200, json={"messages": []}, request=httpx.Request(method, url))
        return httpx.Response(200, json={"id": "sent-message"}, request=httpx.Request(method, url))

    monkeypatch.setattr("app.integrations.httpx.post", fake_post)
    monkeypatch.setattr("app.integrations.httpx.request", fake_request)
    requested = client.post(
        "/api/v1/integrations/gmail/messages",
        json={"to": ["reviewer@example.com"], "subject": "Estado", "body": "Actualización"},
    )
    assert requested.status_code == 202
    assert not calls

    decision = client.post(
        f"/api/v1/approvals/{requested.json()['approval_id']}/decision",
        json={"decision": "approve", "comment": "Aprobado."},
    )
    assert decision.status_code == 200
    assert decision.json()["task"]["status"] == "completed"
    assert [method for method, _url in calls] == ["POST", "GET", "POST"]


def test_queued_github_approval_is_consumed_from_outbox(client, monkeypatch):
    config = Settings(
        _env_file=None,
        environment="test",
        auth_mode="dev",
        database_url=client.app.state.settings.database_url,
        github_token="github-test-token",
        event_worker_enabled=True,
    )
    client.app.state.settings = config
    calls = []

    def fake_get(url, **kwargs):
        calls.append(("GET", url))
        return httpx.Response(200, json=[], request=httpx.Request("GET", url))

    def fake_post(url, **kwargs):
        calls.append(("POST", url, kwargs.get("json")))
        return httpx.Response(201, json={"id": 42}, request=httpx.Request("POST", url))

    monkeypatch.setattr("app.integrations.httpx.get", fake_get)
    monkeypatch.setattr("app.integrations.httpx.post", fake_post)
    requested = client.post(
        "/api/v1/integrations/github/acme/platform/issues/7/comments",
        json={"body": "Actualizar después de revisar."},
    )
    approval_id = requested.json()["approval_id"]
    assert requested.status_code == 202

    decision = client.post(
        f"/api/v1/approvals/{approval_id}/decision",
        json={"decision": "approve", "comment": "Aprobado."},
    )
    assert decision.status_code == 200
    assert decision.json()["approval"]["status"] == "approved_queued"
    assert not calls

    with client.app.state.session_factory() as session:
        event_id = session.scalar(
            select(OutboxEvent.id).where(OutboxEvent.topic == "integration.action.approved")
        )
    assert event_id

    from app.event_worker import _complete_event

    assert _complete_event(client.app.state.session_factory, config, event_id)["status"] == (
        "completed"
    )
    assert [call[0] for call in calls] == ["GET", "POST"]
    with client.app.state.session_factory() as session:
        approval = session.get(ApprovalRecord, approval_id)
        task = session.get(TaskRecord, requested.json()["task_id"])
        event = session.get(OutboxEvent, event_id)
        assert approval.status == "approved"
        assert task.status == "completed"
        assert event.status == "processed"


def test_queued_workflow_resumes_after_approval_event(client):
    config = Settings(
        _env_file=None,
        environment="test",
        auth_mode="dev",
        database_url=client.app.state.settings.database_url,
        event_worker_enabled=True,
    )
    client.app.state.settings = config
    definition = {
        "id": "queued-approval-resume",
        "name": "Flujo reanudable en cola",
        "description": "Continúa tras consumir la aprobación.",
        "steps": [
            {"id": "review", "type": "approval", "config": {"draft": "Revisar."}},
            {
                "id": "finish",
                "type": "result",
                "config": {"summary": "Estado: {{steps.review.status}}"},
            },
        ],
    }
    assert client.post("/api/v1/workflows", json=definition).status_code == 201
    run = client.post(
        "/api/v1/workflows/queued-approval-resume/runs",
        json={"input": {"message": "Validar"}},
    )
    assert run.status_code == 201
    approval_id = run.json()["result"]["steps"]["review"]["approval_id"]
    decision = client.post(
        f"/api/v1/approvals/{approval_id}/decision",
        json={"decision": "approve", "comment": "Aprobado."},
    )
    assert decision.status_code == 200
    assert decision.json()["approval"]["status"] == "approved_queued"

    with client.app.state.session_factory() as session:
        event_id = session.scalar(
            select(OutboxEvent.id).where(OutboxEvent.topic == "workflow.resume.approved")
        )
    assert event_id

    from app.event_worker import _complete_event

    result = _complete_event(client.app.state.session_factory, config, event_id)
    assert result["task"]["status"] == "completed"
    assert result["task"]["result"]["summary"] == "Estado: approved"


def test_streaming_pipeline_records_are_consumed_and_indexed(client):
    config = Settings(
        _env_file=None,
        environment="test",
        auth_mode="dev",
        database_url=client.app.state.settings.database_url,
        event_worker_enabled=True,
    )
    client.app.state.settings = config
    registered = client.post(
        "/api/v1/data-sources",
        json={
            "kind": "crm",
            "name": "CRM de prueba",
            "endpoint": "https://crm.example.test/api/records",
            "scope": "commercial",
            "format": "json",
            "source_options": {
                "title_field": "name",
                "id_field": "id",
                "content_fields": ["description"],
            },
        },
    )
    assert registered.status_code == 201
    source_id = registered.json()["id"]
    accepted = client.post(
        f"/api/v1/data-sources/{source_id}/events",
        json={
            "event_id": "crm-change-123",
            "records": [
                {"id": "account-7", "name": "Cuenta Norte", "description": "Cliente activo"}
            ],
        },
    )
    assert accepted.status_code == 202
    assert accepted.json()["status"] == "queued"

    with client.app.state.session_factory() as session:
        event_id = session.scalar(
            select(OutboxEvent.id).where(OutboxEvent.topic == "data.source.records_received")
        )
    assert event_id

    from app.event_worker import _complete_event

    result = _complete_event(client.app.state.session_factory, config, event_id)
    assert result["records_received"] == 1
    with client.app.state.session_factory() as session:
        document = session.scalar(
            select(KnowledgeDocument).where(
                KnowledgeDocument.source_uri == f"source://{source_id}/account-7"
            )
        )
        assert document.title == "Cuenta Norte"
        assert "Cliente activo" in document.content


def test_mcp_header_encoding_round_trip():
    value = "Mcp Área"
    assert decode_header_value(encode_header_value(value)) == value


def test_mcp_legacy_handshake_and_sse(monkeypatch):
    calls = []

    def fake_post(url, headers, json, timeout):
        method = json["method"]
        calls.append((method, headers.copy(), json))
        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": json["id"],
                    "result": {"protocolVersion": "2025-11-25"},
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content='data: {"jsonrpc":"2.0","id":"legacy","result":{"ok":true}}\n\n',
        )

    monkeypatch.setattr("app.connectors.httpx.post", fake_post)
    connector = ConnectorRecord(
        kind="mcp",
        name="Legacy remote",
        description="",
        endpoint="https://legacy.example.com/mcp",
        protocol_version="2025-11-25",
    )
    config = Settings(_env_file=None, environment="test", auth_mode="dev")

    result = _mcp_exchange(connector, config, "tools/call", {"name": "lookup", "arguments": {}})

    assert result == {"ok": True}
    assert [call[0] for call in calls] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]
    assert calls[-1][1]["MCP-Protocol-Version"] == "2025-11-25"
    assert (
        _rpc_response(
            httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content='data: {"jsonrpc":"2.0","id":"sse","result":{}}\n\n',
            )
        )["id"]
        == "sse"
    )


def test_connector_registration_never_returns_secret_value(client, monkeypatch):
    monkeypatch.setenv("SUPPORT_AGENT_TOKEN", "do-not-return-this-value")
    created = client.post(
        "/api/v1/connectors",
        json={
            "kind": "a2a",
            "name": "Support agent",
            "description": "Answers support questions.",
            "endpoint": "https://support.example.com/a2a",
            "auth_env": "SUPPORT_AGENT_TOKEN",
        },
    )
    assert created.status_code == 201
    assert created.json()["auth_configured"] is True
    assert "do-not-return-this-value" not in created.text
    listed = client.get("/api/v1/connectors").json()["items"]
    assert listed[0]["id"] == created.json()["id"]


def test_connector_can_be_disabled_and_reenabled_with_audit(client):
    created = client.post(
        "/api/v1/connectors",
        json={
            "kind": "a2a",
            "name": "Support agent",
            "endpoint": "https://support.example.com/a2a",
        },
    )
    connector_id = created.json()["id"]

    disabled = client.delete(f"/api/v1/connectors/{connector_id}")
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert client.get(f"/api/v1/connectors/{connector_id}/tools").status_code == 404
    assert client.delete(f"/api/v1/connectors/{connector_id}").status_code == 409

    enabled = client.post(f"/api/v1/connectors/{connector_id}/enable")
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True
    actions = {item["action"] for item in client.get("/api/v1/activity").json()["items"]}
    assert {"connector.register", "connector.disable", "connector.enable"} <= actions


def test_artifact_paths_cannot_traverse_directories():
    with pytest.raises(ValidationError):
        ArtifactInput(path="../secrets.env", content="x")


def test_production_connector_requires_allowlisted_https_host():
    config = Settings(
        _env_file=None,
        environment="production",
        public_url="https://a2a.example.com",
        database_url="postgresql+psycopg://agentic:test@db.example.com/agentic",
        auth_mode="api_key",
        api_bearer_token="strong-local-test-token",
        connector_allowed_hosts="agents.example.com",
    )
    validate_outbound_endpoint("https://agents.example.com/a2a", config)
    with pytest.raises(HTTPException, match="HTTPS"):
        validate_outbound_endpoint("http://agents.example.com/a2a", config)
    with pytest.raises(HTTPException, match="CONNECTOR_ALLOWED_HOSTS"):
        validate_outbound_endpoint("https://other.example.com/a2a", config)


def test_development_authentication_cannot_be_used_in_production():
    with pytest.raises(ValidationError, match="AUTH_MODE=dev"):
        Settings(_env_file=None, environment="production", auth_mode="dev")


def test_inspector_keeps_provenance_for_every_fact():
    result = inspect_repository_files(
        [
            {
                "path": "Dockerfile",
                "content": "FROM python:3.12-slim\nWORKDIR /srv/app\n",
            }
        ]
    )
    assert len(result["findings"]) == 2
    assert all(item["path"] == "Dockerfile" and item["evidence"] for item in result["findings"])


def test_employee_role_is_not_granted_catalog_write():
    from app.policy import local_decision

    employee = Principal(subject="worker-1", roles=frozenset({"employee"}))
    allowed, _ = local_decision(employee, "catalog.manage")
    assert allowed is False
