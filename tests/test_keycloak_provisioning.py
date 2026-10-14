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


def test_missing_keycloak_realm_is_imported_from_repository_definition():
    state = {"created": None}

    def handler(request):
        if request.method == "GET" and request.url.path.endswith("/admin/realms/a2a"):
            return httpx.Response(404)
        if request.method == "POST" and request.url.path.endswith("/admin/realms"):
            state["created"] = __import__("json").loads(request.content)
            return httpx.Response(201)
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provisioner = KeycloakProvisioner(
            base_url="https://keycloak.example.test", realm="a2a", client=client
        )
        provisioner.token = "admin-token"
        provisioner.ensure_realm()
    assert state["created"]["realm"] == "a2a"
    assert {item["clientId"] for item in state["created"]["clients"]} >= {
        "a2a-api",
        "a2a-console",
    }


def test_keycloak_identity_provisioner_uses_limited_role_and_api_audience():
    state = {"role": None, "client": None, "mappers": [], "mappings": []}

    def handler(request):
        path = request.url.path
        if path.endswith("/protocol/openid-connect/token"):
            assert request.url.path.endswith("/realms/a2a/protocol/openid-connect/token")
            return httpx.Response(200, json={"access_token": "scoped-platform-token"})
        if path.endswith("/roles/identity-manager"):
            return httpx.Response(200, json=state["role"]) if state["role"] else httpx.Response(404)
        if path.endswith("/roles") and request.method == "POST":
            state["role"] = {"id": "identity-role", "name": "identity-manager"}
            return httpx.Response(201)
        if path.endswith("/clients") and request.method == "GET":
            return httpx.Response(200, json=[state["client"]] if state["client"] else [])
        if path.endswith("/clients") and request.method == "POST":
            import json

            payload = json.loads(request.content)
            state["client"] = {
                "id": "provisioner-uuid",
                "clientId": payload["clientId"],
                "publicClient": False,
                "serviceAccountsEnabled": True,
                "enabled": True,
            }
            state["mappers"] = payload["protocolMappers"]
            return httpx.Response(201)
        if path.endswith("/protocol-mappers/models") and request.method == "GET":
            return httpx.Response(200, json=state["mappers"])
        if path.endswith("/protocol-mappers/models") and request.method == "POST":
            import json

            state["mappers"].append(json.loads(request.content))
            return httpx.Response(201)
        if path.endswith("/service-account-user"):
            return httpx.Response(200, json={"id": "provisioner-service-account"})
        if path.endswith("/role-mappings/realm") and request.method == "GET":
            return httpx.Response(200, json=state["mappings"])
        if path.endswith("/role-mappings/realm") and request.method == "POST":
            import json

            state["mappings"] = json.loads(request.content)
            return httpx.Response(204)
        if path.endswith("/client-secret"):
            return httpx.Response(200, json={"value": "temporary-test-secret-value"})
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provisioner = KeycloakProvisioner(
            base_url="https://keycloak.example.test", realm="a2a", client=client
        )
        secret = provisioner.ensure_provisioner()
        token = provisioner.issue_realm_token("a2a-provisioner", secret)

    assert token == "scoped-platform-token"
    assert state["mappings"] == [{"id": "identity-role", "name": "identity-manager"}]
    assert state["mappers"][0]["config"]["included.client.audience"] == "a2a-api"


def test_platform_provisioning_uses_scoped_client_token_without_static_admin_bearer(
    monkeypatch,
):
    from app import keycloak_provisioning as module

    monkeypatch.delenv("A2A_ADMIN_BEARER_TOKEN", raising=False)
    for key, value in {
        "KEYCLOAK_URL": "https://keycloak.example.test",
        "KEYCLOAK_REALM": "a2a",
        "A2A_API_URL": "https://agents.example.test",
        "A2A_PUBLIC_URL": "https://agents.example.test",
        "KEYCLOAK_ADMIN_USERNAMES": "platform-admin",
        "VAULT_ADDR": "https://vault.example.test",
        "VAULT_PROVISIONER_TOKEN": "temporary-test-token",
    }.items():
        monkeypatch.setenv(key, value)

    class DummyClient:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    class DummyProvisioner:
        def __init__(self, **_):
            self.issued_with = None

        def authenticate(self):
            return None

        def ensure_realm(self):
            return None

        def ensure_role(self, _):
            return None

        def configure_console(self, _):
            return None

        def grant_admin_role(self, _):
            return None

        def ensure_provisioner(self):
            return "provisioner-client-secret"

        def issue_realm_token(self, client_id, secret):
            self.issued_with = (client_id, secret)
            return "scoped-identity-manager-token"

        def ensure_agent(self, _):
            return "agent-uuid", "agent-client-secret"

    monkeypatch.setattr(module.httpx, "Client", lambda **_: DummyClient())
    monkeypatch.setattr(module, "KeycloakProvisioner", DummyProvisioner)
    monkeypatch.setattr(
        module, "store_secret_in_vault", lambda client_id, _: f"vault://{client_id}"
    )
    monkeypatch.setattr(module, "register_in_platform", lambda *_: "active")
    monkeypatch.setattr(module, "smoke_knowledge_task", lambda *_: "smoke-task")

    result = module.provision(["a2a-agent-knowledge"])
    assert result[0]["registry_status"] == "active"
    assert result[1] == {"smoke_test": "knowledge-task", "task_id": "smoke-task"}
