from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.main import create_app
from app.models import AgentIdentity, AuditRecord, ModelUsageLedger, TaskRecord


def test_api_key_uses_configured_least_privilege_identity(tmp_path):
    config = Settings(
        _env_file=None,
        environment="test",
        auth_mode="api_key",
        api_bearer_token="test-api-key",
        api_key_subject="batch-reader",
        api_key_roles="employee",
        api_key_scopes="knowledge:read tasks:read tasks:write",
        database_url=f"sqlite:///{tmp_path / 'api-key.db'}",
    )
    with TestClient(create_app(config)) as client:
        response = client.get("/api/v1/system", headers={"Authorization": "Bearer test-api-key"})
        assert response.status_code == 200
        assert response.json()["actor"] == "batch-reader"
        assert response.json()["roles"] == ["employee"]
        assert (
            client.get(
                "/api/v1/agent-identities", headers={"Authorization": "Bearer test-api-key"}
            ).status_code
            == 403
        )
        assert (
            client.get("/api/v1/system", headers={"Authorization": "Bearer wrong"}).status_code
            == 401
        )


def test_agent_identity_lifecycle_is_audited_and_revocation_is_terminal(client):
    created = client.post(
        "/api/v1/agent-identities",
        json={
            "client_id": "warehouse-agent",
            "display_name": "Warehouse Agent",
            "roles": ["employee"],
            "scopes": ["inventory:read"],
            "expires_at": "2030-01-01T00:00:00Z",
        },
    )
    assert created.status_code == 201
    assert created.json()["status"] == "provisioned"
    assert client.post("/api/v1/agent-identities/warehouse-agent/activate").json()["status"] == (
        "active"
    )
    assert client.post("/api/v1/agent-identities/warehouse-agent/suspend").json()["status"] == (
        "suspended"
    )
    assert client.post("/api/v1/agent-identities/warehouse-agent/activate").json()["status"] == (
        "active"
    )
    revoked = client.post("/api/v1/agent-identities/warehouse-agent/revoke")
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    assert client.post("/api/v1/agent-identities/warehouse-agent/activate").status_code == 409
    assert (
        client.post(
            "/api/v1/agent-identities",
            json={"client_id": "bad-agent", "display_name": "Bad", "roles": ["admin"]},
        ).status_code
        == 422
    )


def test_oidc_agent_tokens_are_checked_against_live_registry(client, monkeypatch):
    client.app.state.settings = client.app.state.settings.model_copy(
        update={
            "auth_mode": "oidc",
            "oidc_issuer_url": "https://identity.example.test/realms/a2a",
            "oidc_audience": "a2a-api",
        }
    )
    claims = {
        "exp": 2_000_000_000,
        "iss": "https://identity.example.test/realms/a2a",
        "aud": "a2a-api",
        "sub": "service-account-warehouse-agent",
        "azp": "warehouse-agent",
        "realm_access": {"roles": ["agent"]},
    }
    monkeypatch.setattr(
        "app.security._jwks_client",
        lambda _issuer: SimpleNamespace(
            get_signing_key_from_jwt=lambda _token: SimpleNamespace(key="verified-key")
        ),
    )
    monkeypatch.setattr("app.security.jwt.decode", lambda *_args, **_kwargs: claims)
    with client.app.state.session_factory() as session:
        session.add(
            AgentIdentity(
                id="warehouse-agent",
                display_name="Warehouse agent",
                status="active",
                roles_json=["employee"],
                scopes_json=["inventory:read"],
                created_by="admin",
            )
        )
        session.commit()

    headers = {"Authorization": "Bearer signed-agent-token"}
    accepted = client.get("/api/v1/system", headers=headers)
    assert accepted.status_code == 200
    assert accepted.json()["actor"] == "agent:warehouse-agent"
    assert accepted.json()["roles"] == ["employee"]

    with client.app.state.session_factory() as session:
        identity = session.get(AgentIdentity, "warehouse-agent")
        identity.status = "suspended"
        session.commit()
    assert client.get("/api/v1/system", headers=headers).status_code == 401

    with client.app.state.session_factory() as session:
        identity = session.get(AgentIdentity, "warehouse-agent")
        identity.status = "active"
        identity.expires_at = datetime.now(UTC)
        session.commit()
    assert client.get("/api/v1/system", headers=headers).status_code == 401


def test_configured_opa_is_authoritative_for_read_routes(client, monkeypatch):
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"opa_url": "http://opa.test"}
    )
    requests = []

    def denied(url, **kwargs):
        requests.append(kwargs["json"]["input"]["action"])
        import httpx

        return httpx.Response(200, json={"result": False}, request=httpx.Request("POST", url))

    monkeypatch.setattr("app.policy.httpx.post", denied)
    response = client.get("/api/v1/usage")
    assert response.status_code == 403
    assert requests == ["usage.read"]

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("OPA is offline")

    monkeypatch.setattr("app.policy.httpx.post", unavailable)
    assert client.get("/api/v1/usage").status_code == 403


def test_audit_hash_chain_is_append_only_verifiable_and_replay_is_read_only(client):
    task = client.post(
        "/api/v1/tasks",
        json={"scenario": "knowledge", "message": "¿Qué documentación necesito para viajar?"},
    ).json()
    verified = client.get("/api/v1/audit/verify")
    assert verified.status_code == 200
    assert verified.json()["valid"] is True
    replay = client.get(f"/api/v1/tasks/{task['id']}/replay")
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["side_effects_reissued"] is False
    assert any(item["kind"] == "event" for item in replay.json()["timeline"])
    assert any(item["kind"] == "audit" for item in replay.json()["timeline"])

    with client.app.state.session_factory() as session:
        with pytest.raises(IntegrityError):
            session.execute(update(AuditRecord).values(decision="tampered"))
        session.rollback()

    with client.app.state.engine.begin() as connection:
        connection.execute(text("DROP TRIGGER audit_events_no_update"))
        connection.execute(text("UPDATE audit_events SET details_json='{}' WHERE sequence=1"))
    assert client.get("/api/v1/audit/verify").json()["valid"] is False


def test_usage_ledger_is_lifetime_complete_paginated_and_append_only(client):
    with client.app.state.session_factory() as session:
        task = TaskRecord(
            scenario="knowledge",
            title="Ledger test",
            request_text="Ledger test",
            status="completed",
            actor="local-admin",
        )
        session.add(task)
        session.flush()
        session.add_all(
            [
                ModelUsageLedger(
                    call_key=f"{task.id}:{index}",
                    task_id=task.id,
                    actor=task.actor,
                    provider="test-provider",
                    model="test-model",
                    attempted=True,
                    succeeded=True,
                    input_tokens=10,
                    output_tokens=5,
                    input_rate_per_million=1.0,
                    output_rate_per_million=2.0,
                    estimated_cost_usd=0.00002,
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
                for index in range(1001)
            ]
        )
        session.commit()

    summary = client.get("/api/v1/usage").json()
    assert summary["calls"] == 1001
    assert summary["ledger_rows"] == 1001
    assert summary["period"] == "lifetime"
    assert summary["estimated_cost_usd"] == pytest.approx(0.02002)

    seen = 0
    cursor = None
    while True:
        suffix = f"?limit=500&cursor={cursor}" if cursor else "?limit=500"
        page = client.get(f"/api/v1/usage/calls{suffix}").json()
        seen += len(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == 1001

    with client.app.state.session_factory() as session:
        row = session.scalar(select(ModelUsageLedger))
        with pytest.raises(IntegrityError):
            session.execute(
                update(ModelUsageLedger)
                .where(ModelUsageLedger.id == row.id)
                .values(model="rewritten")
            )
        session.rollback()
