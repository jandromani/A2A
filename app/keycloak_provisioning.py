"""Idempotently provision Keycloak service-account agents and put secrets in Vault."""

from __future__ import annotations

import argparse
import json
import os
import re
from urllib.parse import quote, urlsplit

import httpx

AUDIENCE_MAPPER = {
    "name": "a2a-api-audience",
    "protocol": "openid-connect",
    "protocolMapper": "oidc-audience-mapper",
    "consentRequired": False,
    "config": {
        "included.client.audience": "a2a-api",
        "id.token.claim": "false",
        "access.token.claim": "true",
    },
}
REALM_ROLES = {
    "employee": "Read approved knowledge and create tasks",
    "operator": "Operate workflows, sources and audit",
    "approver": "Review gated external actions",
    "admin": "Manage the platform and agent identities",
    "agent": "Marks a service-account token for registry validation",
}


class KeycloakProvisioner:
    def __init__(self, *, base_url: str, realm: str, client: httpx.Client):
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("KEYCLOAK_URL must use HTTPS outside localhost.")
        if not parsed.netloc or parsed.path not in {"", "/"}:
            raise ValueError("KEYCLOAK_URL must be the Keycloak origin without a path.")
        self.base_url = base_url.rstrip("/")
        self.realm = realm
        self.client = client
        self.token = ""

    def authenticate(self) -> None:
        admin_realm = os.getenv("KEYCLOAK_ADMIN_REALM", "master")
        endpoint = f"{self.base_url}/realms/{quote(admin_realm)}/protocol/openid-connect/token"
        client_id = os.getenv("KEYCLOAK_PROVISIONER_CLIENT_ID", "admin-cli")
        client_secret = os.getenv("KEYCLOAK_PROVISIONER_CLIENT_SECRET")
        if client_secret and not os.getenv("KEYCLOAK_PROVISIONER_CLIENT_ID"):
            raise ValueError(
                "KEYCLOAK_PROVISIONER_CLIENT_ID is required for client-credentials authentication."
            )
        data = (
            {"grant_type": "client_credentials"}
            if client_secret
            else {
                "grant_type": "password",
                "username": os.environ["KEYCLOAK_ADMIN_USERNAME"],
                "password": os.environ["KEYCLOAK_ADMIN_PASSWORD"],
            }
        )
        data["client_id"] = client_id
        if client_secret:
            data["client_secret"] = client_secret
        response = self.client.post(endpoint, data=data)
        response.raise_for_status()
        self.token = response.json()["access_token"]

    def request(
        self, method: str, path: str, *, allow_status: set[int] | None = None, **kwargs
    ) -> httpx.Response:
        response = self.client.request(
            method,
            f"{self.base_url}/admin/realms/{quote(self.realm, safe='')}/{path.lstrip('/')}",
            headers={"Authorization": f"Bearer {self.token}"},
            **kwargs,
        )
        if response.status_code >= 400 and response.status_code not in (allow_status or set()):
            response.raise_for_status()
        return response

    def ensure_agent(self, client_id: str) -> tuple[str, str]:
        self.ensure_role("agent")
        clients = self.request("GET", "clients", params={"clientId": client_id}).json()
        if clients:
            client_uuid = clients[0]["id"]
            current = clients[0]
            if current.get("publicClient") or not current.get("serviceAccountsEnabled"):
                raise RuntimeError(
                    f"Existing Keycloak client {client_id} is not a confidential service account."
                )
            if not current.get("enabled"):
                raise RuntimeError(
                    f"Existing Keycloak client {client_id} is disabled; refusing to reactivate it."
                )
        else:
            response = self.request(
                "POST",
                "clients",
                json={
                    "clientId": client_id,
                    "name": client_id,
                    "enabled": True,
                    "protocol": "openid-connect",
                    "publicClient": False,
                    "standardFlowEnabled": False,
                    "implicitFlowEnabled": False,
                    "directAccessGrantsEnabled": False,
                    "serviceAccountsEnabled": True,
                    "clientAuthenticatorType": "client-secret",
                    "protocolMappers": [AUDIENCE_MAPPER],
                },
            )
            if response.status_code not in {201, 204}:
                raise RuntimeError(f"Keycloak did not create client {client_id}.")
            clients = self.request("GET", "clients", params={"clientId": client_id}).json()
            if not clients:
                raise RuntimeError(f"Keycloak created but could not read client {client_id}.")
            client_uuid = clients[0]["id"]

        self.ensure_audience_mapper(client_uuid)
        self.ensure_service_role(client_uuid)
        secret = self.request("GET", f"clients/{client_uuid}/client-secret").json().get("value")
        if not isinstance(secret, str) or len(secret) < 16:
            raise RuntimeError(f"Keycloak returned no client secret for {client_id}.")
        return client_uuid, secret

    def ensure_role(self, role_name: str) -> dict:
        response = self.request("GET", f"roles/{quote(role_name, safe='')}", allow_status={404})
        if response.status_code == 404:
            created = self.request(
                "POST",
                "roles",
                json={"name": role_name, "description": REALM_ROLES.get(role_name, role_name)},
            )
            if created.status_code not in {201, 204}:
                raise RuntimeError(f"Keycloak did not create realm role {role_name}.")
            response = self.request("GET", f"roles/{quote(role_name, safe='')}")
        return response.json()

    def configure_console(self, public_url: str) -> None:
        parsed = urlsplit(public_url)
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("A2A_PUBLIC_URL must use HTTPS outside localhost.")
        if not parsed.netloc or parsed.path not in {"", "/"}:
            raise ValueError("A2A_PUBLIC_URL must be an origin without a path.")
        clients = self.request("GET", "clients", params={"clientId": "a2a-console"}).json()
        if not clients:
            raise RuntimeError("The imported realm must contain the a2a-console client.")
        client_uuid = clients[0]["id"]
        client = self.request("GET", f"clients/{client_uuid}").json()
        origin = public_url.rstrip("/")
        client["redirectUris"] = [f"{origin}/*"]
        client["webOrigins"] = [origin]
        response = self.request("PUT", f"clients/{client_uuid}", json=client)
        if response.status_code not in {200, 204}:
            raise RuntimeError("Keycloak did not update the console redirect URIs.")

    def grant_admin_role(self, username: str) -> None:
        users = self.request("GET", "users", params={"username": username, "exact": "true"}).json()
        user = next((item for item in users if item.get("username") == username), None)
        if not user:
            raise RuntimeError(f"Configured Keycloak administrator {username!r} was not found.")
        role = self.ensure_role("admin")
        mappings = self.request(
            "GET", f"users/{quote(user['id'], safe='')}/role-mappings/realm"
        ).json()
        if not any(item.get("name") == "admin" for item in mappings):
            response = self.request(
                "POST",
                f"users/{quote(user['id'], safe='')}/role-mappings/realm",
                json=[role],
            )
            if response.status_code not in {201, 204}:
                raise RuntimeError(f"Keycloak did not grant the admin role to {username!r}.")

    def ensure_audience_mapper(self, client_uuid: str) -> None:
        mappers = self.request("GET", f"clients/{client_uuid}/protocol-mappers/models").json()
        if not any(mapper.get("name") == AUDIENCE_MAPPER["name"] for mapper in mappers):
            response = self.request(
                "POST", f"clients/{client_uuid}/protocol-mappers/models", json=AUDIENCE_MAPPER
            )
            if response.status_code not in {201, 204}:
                raise RuntimeError(
                    f"Keycloak did not add the API audience mapper to {client_uuid}."
                )

    def ensure_service_role(self, client_uuid: str) -> None:
        user = self.request("GET", f"clients/{client_uuid}/service-account-user").json()
        role = self.ensure_role("agent")
        mappings = self.request(
            "GET", f"users/{quote(user['id'], safe='')}/role-mappings/realm"
        ).json()
        if not any(item.get("name") == "agent" for item in mappings):
            response = self.request(
                "POST",
                f"users/{quote(user['id'], safe='')}/role-mappings/realm",
                json=[role],
            )
            if response.status_code not in {201, 204}:
                raise RuntimeError(
                    "Keycloak did not grant the agent realm role to the service account."
                )


def store_secret_in_vault(client_id: str, secret: str) -> str:
    address = os.environ["VAULT_ADDR"].rstrip("/")
    token = os.environ["VAULT_PROVISIONER_TOKEN"]
    mount = os.getenv("VAULT_KV_MOUNT", "secret").strip("/")
    prefix = os.getenv("VAULT_AGENT_SECRET_PREFIX", "a2a/production/agents").strip("/")
    if not mount or any(part in {"", ".", ".."} for part in prefix.split("/")):
        raise ValueError("Vault mount and agent secret prefix must be valid relative paths.")
    if not address.startswith("https://") and urlsplit(address).hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError("VAULT_ADDR must use HTTPS outside localhost.")
    path = f"{prefix}/{client_id}"
    headers = {"X-Vault-Token": token}
    namespace = os.getenv("VAULT_NAMESPACE")
    if namespace:
        headers["X-Vault-Namespace"] = namespace
    response = httpx.post(
        f"{address}/v1/{quote(mount, safe='')}/data/{quote(path, safe='/')}",
        headers=headers,
        json={"data": {"client_secret": secret}},
        timeout=10,
    )
    response.raise_for_status()
    return f"vault://{path}#client_secret"


def register_in_platform(api_url: str, token: str, client_id: str, display_name: str) -> str:
    headers = {"Authorization": f"Bearer {token}"}
    base = f"{api_url.rstrip('/')}/api/v1/agent-identities"
    response = httpx.post(
        base,
        headers=headers,
        json={"client_id": client_id, "display_name": display_name, "roles": ["employee"]},
        timeout=15,
    )
    if response.status_code == 409:
        existing = httpx.get(base, headers=headers, timeout=15)
        existing.raise_for_status()
        item = next(
            (
                record
                for record in existing.json().get("items", [])
                if record["client_id"] == client_id
            ),
            None,
        )
        if not item:
            raise RuntimeError(f"Agent {client_id} conflicts with an unreadable registry entry.")
        status = item["status"]
    else:
        response.raise_for_status()
        status = response.json()["status"]
    if status == "provisioned":
        activated = httpx.post(
            f"{base}/{quote(client_id, safe='')}/activate", headers=headers, timeout=15
        )
        activated.raise_for_status()
        status = activated.json()["status"]
    if status != "active":
        raise RuntimeError(f"Agent {client_id} is {status}; provisioning will not reactivate it.")
    return status


def provision(agent_client_ids: list[str]) -> list[dict[str, str]]:
    for client_id in agent_client_ids:
        if not re.fullmatch(r"[A-Za-z0-9._:-]{3,200}", client_id):
            raise ValueError(f"Invalid agent client id: {client_id!r}")
    base_url = os.environ["KEYCLOAK_URL"]
    realm = os.getenv("KEYCLOAK_REALM", "a2a")
    api_url = os.environ["A2A_API_URL"]
    parsed_api_url = urlsplit(api_url)
    if parsed_api_url.scheme != "https" and parsed_api_url.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError("A2A_API_URL must use HTTPS outside localhost.")
    if not parsed_api_url.netloc or parsed_api_url.query or parsed_api_url.fragment:
        raise ValueError("A2A_API_URL must be a valid origin or base URL without a query.")
    api_token = os.environ["A2A_ADMIN_BEARER_TOKEN"]
    results = []
    with httpx.Client(timeout=15) as client:
        keycloak = KeycloakProvisioner(base_url=base_url, realm=realm, client=client)
        keycloak.authenticate()
        for role in REALM_ROLES:
            keycloak.ensure_role(role)
        keycloak.configure_console(os.getenv("A2A_PUBLIC_URL", os.getenv("PUBLIC_URL", "")))
        for username in filter(None, (os.getenv("KEYCLOAK_ADMIN_USERNAMES", "").split(","))):
            keycloak.grant_admin_role(username.strip())
        for client_id in agent_client_ids:
            _, secret = keycloak.ensure_agent(client_id)
            vault_reference = store_secret_in_vault(client_id, secret)
            status = register_in_platform(api_url, api_token, client_id, client_id)
            results.append(
                {
                    "client_id": client_id,
                    "vault_reference": vault_reference,
                    "registry_status": status,
                }
            )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent-client-ids", required=True, help="Comma-separated Keycloak client IDs"
    )
    args = parser.parse_args()
    client_ids = [item.strip() for item in args.agent_client_ids.split(",") if item.strip()]
    if not client_ids or len(client_ids) != len(set(client_ids)):
        parser.error("provide a non-empty list of distinct client IDs")
    print(json.dumps(provision(client_ids), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
