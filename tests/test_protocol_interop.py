import asyncio
import json

import pytest
from fastapi import HTTPException

from app.config import Settings
from app.connectors import _agora_exchange
from app.models import ConnectorRecord
from app.schemas import ConnectorCreate, ConnectorInvoke


def test_anp_discovery_advertises_only_live_interfaces(client):
    index = client.get("/.well-known/agent-descriptions")
    assert index.status_code == 200
    assert index.json()["@type"] == "CollectionPage"
    description_url = index.json()["items"][0]["@id"]
    assert description_url.endswith("/agents/a2a/ad.json")

    description = client.get("/agents/a2a/ad.json")
    assert description.status_code == 200
    assert description.json()["protocolType"] == "ANP"
    assert {item["protocol"] for item in description.json()["interfaces"]} == {
        "A2A",
        "MCP",
        "Agora",
    }
    assert "DID-WBA messaging identity is claimed" in description.json()["x-anp-coverage"]


def test_tools_sdk_registers_typed_tools_with_official_mcp_server(client):
    from app.sdk_protocols import build_mcp_sdk_app

    server, _ = build_mcp_sdk_app(
        client.app.state.settings,
        client.app.state.session_factory,
    )

    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert {
        "run_agent_workflow",
        "search_knowledge",
        "list_capability_catalog",
        "get_task_status",
        "list_workflows",
        "run_custom_workflow",
    }.issubset(tools)
    assert tools["search_knowledge"].input_schema["required"] == ["query"]
    assert tools["run_agent_workflow"].input_schema["properties"]["scenario"]["enum"] == [
        "knowledge",
        "report",
        "incident",
    ]


def test_agora_single_round_and_multiround_use_governed_tasks(client):
    assert client.get("/agora/wellknown").json() == {}

    single = client.post("/agora", json={"body": "¿Cómo justifico un viaje?"})
    assert single.status_code == 200
    assert single.json()["status"] == "success"
    assert single.json()["body"]["task_status"] == "completed"

    started = client.post(
        "/agora",
        json={"body": "Inicia una consulta con contexto", "multiround": True},
    )
    assert started.status_code == 200
    conversation_id = started.json()["conversationId"]
    assert len(conversation_id) == 32
    assert started.json()["conversationExpires"] > 0

    continued = client.post(
        f"/agora/conversations/{conversation_id}",
        json={"body": "Continúa usando el contexto anterior"},
    )
    assert continued.status_code == 200
    assert continued.json()["status"] == "success"
    assert continued.json()["conversationId"] == conversation_id


def test_agora_rejects_unsupported_protocol_and_scopes_conversation_to_actor(client):
    unsupported = client.post("/agora", json={"body": "Hello", "protocolHash": "deadbeef"})
    assert unsupported.status_code == 200
    assert unsupported.json() == {"status": "failure", "error": "Unsupported protocol"}

    started = client.post(
        "/agora",
        headers={"x-actor": "alice"},
        json={"body": "Alice conversation", "multiround": True},
    )
    conversation_id = started.json()["conversationId"]
    stolen = client.post(
        f"/agora/conversations/{conversation_id}",
        headers={"x-actor": "bob"},
        json={"body": "Attempt to read another actor's thread"},
    )
    assert stolen.status_code == 200
    assert stolen.json() == {
        "status": "failure",
        "error": "Conversation expired or unavailable",
    }


def test_agora_connector_defaults_to_protocol_10():
    connector = ConnectorCreate(
        kind="agora",
        name="Remote Agora agent",
        endpoint="https://agent.example.test/agora",
    )
    assert connector.protocol_version == "1.0"

    mcp_connector = ConnectorCreate(
        kind="mcp",
        name="Remote MCP server",
        endpoint="https://agent.example.test/mcp",
    )
    assert mcp_connector.protocol_version == "2026-07-28"


def test_agora_connector_sends_single_and_multiround_messages(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield json.dumps({"status": "success", "body": {"answer": "ok"}}).encode()

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            return FakeResponse()

    monkeypatch.setattr("app.connectors.httpx.Client", lambda **_kwargs: FakeClient())
    connector = ConnectorRecord(
        kind="agora",
        name="Remote Agora agent",
        description="",
        endpoint="https://agent.example.test/agora",
    )
    config = Settings(_env_file=None, environment="test", auth_mode="dev")

    single = _agora_exchange(
        connector,
        config,
        ConnectorInvoke(message="Check stock"),
    )
    started = _agora_exchange(
        connector,
        config,
        ConnectorInvoke(message="Start context", multiround=True),
    )
    continued = _agora_exchange(
        connector,
        config,
        ConnectorInvoke(message="Continue", conversation_id="a" * 32),
    )

    assert single["status"] == started["status"] == continued["status"] == "success"
    assert [call[0:2] for call in calls] == [
        ("POST", "https://agent.example.test/agora"),
        ("POST", "https://agent.example.test/agora"),
        ("POST", "https://agent.example.test/agora/conversations/" + "a" * 32),
    ]
    assert calls[0][2]["json"]["body"]["message"] == "Check stock"
    assert "multiround" not in calls[0][2]["json"]
    assert calls[1][2]["json"]["multiround"] is True
    assert "multiround" not in calls[2][2]["json"]


def test_agora_connector_rejects_unsafe_conversation_id_before_request(monkeypatch):
    def fail_if_called(**_kwargs):
        pytest.fail("unsafe conversation id must be rejected before network access")

    monkeypatch.setattr("app.connectors.httpx.Client", fail_if_called)
    connector = ConnectorRecord(
        kind="agora",
        name="Remote Agora agent",
        description="",
        endpoint="https://agent.example.test/agora",
    )
    config = Settings(_env_file=None, environment="test", auth_mode="dev")

    with pytest.raises(HTTPException) as exc_info:
        _agora_exchange(
            connector,
            config,
            ConnectorInvoke(message="Continue", conversation_id="../admin"),
        )
    assert exc_info.value.status_code == 422
