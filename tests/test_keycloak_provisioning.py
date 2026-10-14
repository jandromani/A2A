import httpx
import pytest

from app.keycloak_provisioning import KeycloakProvisioner


def test_keycloak_agent_client_provisioning_is_idempotent(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_ADMIN_USERNAME", "bootstrap-admin")
    monkeypatch.setenv("KEYCLOAK_ADMIN_PASSWORD", "short-lived-test-password")
    state = {"role": None, "client": None, "mappers": [], "mappings": []}

    def handler(request):
        if request.url.path.endswith("/protocol/openid-connect/token"):
            return httpx.Response(200, json={"access_token": "test-token"})
        if request.url.path.endswith("/roles/agent"):
            if state["role"]:
                return httpx.Response(200, json=state["role"])
            return httpx.Response(404)
        if request.url.path.endswith("/roles") and request.method == "POST":
            state["role"] = {"id": "role-1", "name": "agent"}
            return httpx.Response(201)
        if request.url.path.endswith("/clients") and request.method == "GET":
            return httpx.Response(200, json=[state["client"]] if state["client"] else [])
        if request.url.path.endswith("/clients") and request.method == "POST":
            payload = __import__("json").loads(request.content)
            state["client"] = {
                "id": "client-uuid",
                "clientId": payload["clientId"],
                "publicClient": False,
                "serviceAccountsEnabled": True,
                "enabled": True,
            }
            state["mappers"] = payload["protocolMappers"]
            return httpx.Response(201)
        if request.url.path.endswith("/protocol-mappers/models") and request.method == "GET":
            return httpx.Response(200, json=state["mappers"])
        if request.url.path.endswith("/protocol-mappers/models") and request.method == "POST":
            state["mappers"].append(__import__("json").loads(request.content))
            return httpx.Response(201)
        if request.url.path.endswith("/service-account-user"):
            return httpx.Response(200, json={"id": "service-account-user"})
        if request.url.path.endswith("/role-mappings/realm") and request.method == "GET":
            return httpx.Response(200, json=state["mappings"])
        if request.url.path.endswith("/role-mappings/realm") and request.method == "POST":
            state["mappings"] = [{"name": "agent"}]
            return httpx.Response(204)
        if request.url.path.endswith("/client-secret"):
            return httpx.Response(200, json={"value": "secret-that-never-gets-printed"})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provisioner = KeycloakProvisioner(
        base_url="https://keycloak.example.test", realm="a2a", client=client
    )
    provisioner.authenticate()

    first_id, first_secret = provisioner.ensure_agent("a2a-agent-reports")
    second_id, second_secret = provisioner.ensure_agent("a2a-agent-reports")

    assert first_id == second_id == "client-uuid"
    assert first_secret == second_secret == "secret-that-never-gets-printed"
    assert len(state["mappers"]) == 1
    assert state["mappings"] == [{"name": "agent"}]
    client.close()


def test_keycloak_refuses_http_for_remote_provisioning():
    with httpx.Client() as client, pytest.raises(ValueError, match="HTTPS"):
        KeycloakProvisioner(base_url="http://identity.example.com", realm="a2a", client=client)
