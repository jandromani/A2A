import json

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.config import Settings
from app.connectors import _mcp_exchange, _rpc_response, validate_outbound_endpoint
from app.mcp_transport import CURRENT_PROTOCOL_VERSION, decode_header_value, encode_header_value
from app.models import ConnectorRecord
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
        json={"scenario": "incident", "message": "Prepara una revisión del pedido."},
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


def test_a2a_agent_card_and_message_send(client):
    card = client.get("/.well-known/agent-card.json")
    assert card.status_code == 200
    assert card.json()["supportedInterfaces"][0]["url"].endswith("/a2a")

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

    def fake_post(url, headers, json, timeout):
        observed.update({"url": url, "headers": headers, "body": json})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})

    monkeypatch.setattr("app.connectors.httpx.post", fake_post)
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
    assert observed["headers"]["Mcp-Method"] == "tools/call"
    assert decode_header_value(observed["headers"]["Mcp-Name"]) == "buscar_área"
    assert (
        observed["body"]["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"]
        == CURRENT_PROTOCOL_VERSION
    )


def test_mcp_header_encoding_round_trip():
    value = "Mcp Área"
    assert decode_header_value(encode_header_value(value)) == value


def test_mcp_client_downgrades_to_legacy_handshake_and_reads_sse(monkeypatch):
    calls = []

    def fake_post(url, headers, json, timeout):
        method = json["method"]
        calls.append((method, headers.copy(), json))
        if method == "tools/call" and "MCP-Protocol-Version" in headers and len(calls) == 1:
            return httpx.Response(
                404,
                json={
                    "jsonrpc": "2.0",
                    "id": "modern",
                    "error": {"code": -32601, "message": "Method not found"},
                },
            )
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
        protocol_version=CURRENT_PROTOCOL_VERSION,
    )
    config = Settings(_env_file=None, environment="test", auth_mode="dev")

    result = _mcp_exchange(connector, config, "tools/call", {"name": "lookup", "arguments": {}})

    assert result == {"ok": True}
    assert [call[0] for call in calls] == [
        "tools/call",
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
