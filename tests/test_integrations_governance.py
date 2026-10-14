import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import AuditRecord, OutboxEvent
from app.mqtt_ingress import persist_mqtt_event, record_mqtt_rejection
from app.secrets import clear_secret_cache, resolve_secret


def test_vault_kv_v2_secret_reference_is_resolved_and_cached(monkeypatch):
    clear_secret_cache()


def test_api_key_authentication_resolves_vault_reference(monkeypatch, tmp_path):
    clear_secret_cache()
    config = Settings(
        _env_file=None,
        environment="test",
        auth_mode="api_key",
        api_bearer_token="vault://a2a/production/auth/api#token",
        secret_provider="vault",
        vault_addr="https://vault.example.test",
        vault_token="test-token",
        database_url=f"sqlite:///{tmp_path / 'vault-api-key.db'}",
    )

    def fake_get(url, **kwargs):
        return httpx.Response(
            200,
            json={"data": {"data": {"token": "resolved-api-key"}}},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("app.secrets.httpx.get", fake_get)
    with TestClient(create_app(config)) as client:
        response = client.get(
            "/api/v1/system", headers={"Authorization": "Bearer resolved-api-key"}
        )
        assert response.status_code == 200
        assert response.json()["secret_manager"] == "vault"
    clear_secret_cache()
    config = Settings(
        _env_file=None,
        environment="test",
        secret_provider="vault",
        vault_addr="https://vault.example.test",
        vault_token="test-token",
    )
    requests = []

    def fake_get(url, **kwargs):
        requests.append((url, kwargs["headers"]))
        return httpx.Response(
            200,
            json={"data": {"data": {"api_key": "vault-secret-value"}}},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("app.secrets.httpx.get", fake_get)
    reference = "vault://a2a/production/providers/model#api_key"
    assert resolve_secret(config, reference) == "vault-secret-value"
    assert resolve_secret(config, reference) == "vault-secret-value"
    assert len(requests) == 1
    assert requests[0][0] == (
        "https://vault.example.test/v1/secret/data/a2a/production/providers/model"
    )
    assert requests[0][1]["X-Vault-Token"] == "test-token"
    clear_secret_cache()


def test_mqtt_ingress_is_idempotent_and_audits_rejections(client):
    config = client.app.state.settings.model_copy(
        update={"mqtt_topic_filter": "a2a/telemetry/+/events"}
    )
    payload = b'{"event_id":"telemetry-7","temperature":21.5}'
    event_id = persist_mqtt_event(
        client.app.state.session_factory, config, "a2a/telemetry/device-7/events", payload
    )
    repeated_id = persist_mqtt_event(
        client.app.state.session_factory, config, "a2a/telemetry/device-7/events", payload
    )
    assert repeated_id == event_id

    record_mqtt_rejection(
        client.app.state.session_factory,
        config,
        "a2a/telemetry/device-7/events",
        "payload_must_be_json_object",
    )
    with client.app.state.session_factory() as session:
        events = session.query(OutboxEvent).filter_by(external_id="mqtt:device-7:telemetry-7").all()
        rejection = session.scalar(
            session.query(AuditRecord).filter_by(action="iot.telemetry.reject").statement
        )
        assert len(events) == 1
        assert rejection is not None
        assert rejection.decision == "rejected"
