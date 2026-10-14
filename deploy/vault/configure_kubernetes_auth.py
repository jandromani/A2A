"""Configure Vault KV v2 policy and Kubernetes auth for A2A workloads."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class VaultAdmin:
    def __init__(self) -> None:
        self.address = os.environ["VAULT_ADDR"].rstrip("/")
        self.token = os.environ["VAULT_PROVISIONER_TOKEN"]
        hostname = urllib.parse.urlsplit(self.address).hostname
        if not self.address.startswith("https://") and hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError("VAULT_ADDR must use HTTPS outside localhost.")
        self.headers = {"X-Vault-Token": self.token, "Content-Type": "application/json"}
        namespace = os.getenv("VAULT_NAMESPACE")
        if namespace:
            self.headers["X-Vault-Namespace"] = namespace

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"{self.address}/v1/{path.lstrip('/')}",
            data=json.dumps(body).encode() if body is not None else None,
            headers=self.headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = response.read()
                decoded = json.loads(payload) if payload else {}
                return response.status, decoded.get("data", decoded)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            decoded = json.loads(payload) if payload else {}
            return exc.code, decoded.get("data", decoded)


def configure() -> None:
    api = VaultAdmin()
    mount = os.getenv("VAULT_KV_MOUNT", "secret").strip("/")
    kubernetes_mount = os.getenv("VAULT_KUBERNETES_MOUNT", "kubernetes").strip("/")
    cluster_url = os.environ["KUBERNETES_API_URL"]
    reviewer_jwt = os.environ["KUBERNETES_TOKEN_REVIEWER_JWT"]
    ca_file = Path(os.environ["KUBERNETES_CA_FILE"])
    policy_path = Path(__file__).with_name("a2a-runtime-policy.hcl")
    runtime_policy = policy_path.read_text(encoding="utf-8").replace('"secret/', f'"{mount}/')
    database_policy = (
        f'path "{mount}/data/a2a/production/database" {{\n  capabilities = ["read"]\n}}\n'
    )
    migration_policy = (
        f'path "{mount}/data/a2a/production/database-admin" {{\n  capabilities = ["read"]\n}}\n'
        f'path "{mount}/data/a2a/production/database" {{\n'
        '  capabilities = ["create", "read", "update"]\n}\n'
    )
    if not mount or not kubernetes_mount or not cluster_url.startswith("https://"):
        raise ValueError("Vault mount names and the Kubernetes API HTTPS URL are required.")

    _, auth_mounts = api.request("GET", "sys/auth")
    if f"{kubernetes_mount}/" not in auth_mounts:
        status, _ = api.request(
            "POST",
            f"sys/auth/{urllib.parse.quote(kubernetes_mount, safe='')}",
            {"type": "kubernetes", "description": "A2A workload identity"},
        )
        if status not in {200, 204}:
            raise RuntimeError(f"Could not enable Vault Kubernetes auth (HTTP {status}).")

    _, secret_mounts = api.request("GET", "sys/mounts")
    mount_data = secret_mounts.get(f"{mount}/")
    if not mount_data:
        status, _ = api.request(
            "POST",
            f"sys/mounts/{urllib.parse.quote(mount, safe='')}",
            {"type": "kv", "options": {"version": "2"}},
        )
        if status not in {200, 204}:
            raise RuntimeError(f"Could not enable Vault KV v2 (HTTP {status}).")
    elif mount_data.get("type") != "kv" or mount_data.get("options", {}).get("version") != "2":
        raise RuntimeError("The configured Vault secret mount exists but is not KV v2.")

    ca_certificate = ca_file.read_text(encoding="utf-8")
    status, _ = api.request(
        "POST",
        f"auth/{urllib.parse.quote(kubernetes_mount, safe='')}/config",
        {
            "kubernetes_host": cluster_url,
            "kubernetes_ca_cert": ca_certificate,
            "token_reviewer_jwt": reviewer_jwt,
        },
    )
    if status not in {200, 204}:
        raise RuntimeError(f"Could not configure Vault Kubernetes auth (HTTP {status}).")

    role_specs = {
        "a2a-runtime": ("a2a-runtime,a2a-production-preflight", runtime_policy),
        "a2a-db-migrator": ("a2a-db-migrator", migration_policy),
        "a2a-postgres-backup": ("a2a-postgres-backup", database_policy),
        "a2a-audit-archiver": ("a2a-audit-archiver", database_policy),
    }
    auth_path = f"auth/{urllib.parse.quote(kubernetes_mount, safe='')}"
    for role_name, (service_accounts, policy) in role_specs.items():
        status, _ = api.request(
            "PUT",
            f"sys/policies/acl/{urllib.parse.quote(role_name, safe='')}",
            {"policy": policy},
        )
        if status not in {200, 204}:
            raise RuntimeError(f"Could not write Vault policy {role_name} (HTTP {status}).")
        status, _ = api.request(
            "POST",
            f"{auth_path}/role/{urllib.parse.quote(role_name, safe='')}",
            {
                "bound_service_account_names": [
                    item for item in service_accounts.split(",") if item
                ],
                "bound_service_account_namespaces": ["a2a"],
                "token_policies": [role_name],
                "token_ttl": "15m",
                "token_max_ttl": "1h",
            },
        )
        if status not in {200, 204}:
            raise RuntimeError(f"Could not bind Vault role {role_name} (HTTP {status}).")
    print(f"Configured Vault KV v2 and four least-privilege roles on {kubernetes_mount} auth.")


if __name__ == "__main__":
    configure()
