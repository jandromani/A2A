"""Write generated PostgreSQL/Valkey connection URLs to Vault KV v2."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

RUNTIME_RDS_CA_BUNDLE = "/etc/ssl/certs/rds-global-bundle.pem"


def _database_url(username: str, password: str, address: str, database: str, ca_bundle: str) -> str:
    query = urllib.parse.urlencode({"sslmode": "verify-full", "sslrootcert": ca_bundle}, safe="/")
    return f"postgresql+psycopg://{username}:{password}@{address}:5432/{database}?{query}"


def vault_write(path: str, values: dict[str, str]) -> None:
    address = os.environ["VAULT_ADDR"].rstrip("/")
    token = os.environ["VAULT_PROVISIONER_TOKEN"]
    if not address.startswith("https://") and urllib.parse.urlsplit(address).hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError("VAULT_ADDR must use HTTPS outside localhost.")
    mount = os.getenv("VAULT_KV_MOUNT", "secret").strip("/")
    if not mount or any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError("Vault KV mount and secret path must be valid.")
    namespace = os.getenv("VAULT_NAMESPACE")
    headers = {"X-Vault-Token": token, "Content-Type": "application/json"}
    if namespace:
        headers["X-Vault-Namespace"] = namespace
    secret_path = urllib.parse.quote(path, safe="/")
    request = urllib.request.Request(
        f"{address}/v1/{urllib.parse.quote(mount, safe='')}/data/{secret_path}",
        data=json.dumps({"data": values}).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status not in {200, 204}:
                raise RuntimeError("Vault did not accept the runtime secret write.")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Vault rejected a secret write (HTTP {exc.code}).") from None


def terraform_outputs(directory: Path) -> dict[str, object]:
    result = subprocess.run(
        ["terraform", "output", "-json"],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            "terraform output failed; initialize and apply the production stack first."
        )
    decoded = json.loads(result.stdout)
    return {name: item["value"] for name, item in decoded.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--terraform-dir", type=Path, required=True)
    args = parser.parse_args()
    output = terraform_outputs(args.terraform_dir.resolve())
    address = str(output["postgres_address"])
    database = str(output["postgres_database"])
    username = urllib.parse.quote(str(output["postgres_admin_username"]), safe="")
    password = urllib.parse.quote(str(output["postgres_admin_password"]), safe="")
    database_url = _database_url(username, password, address, database, RUNTIME_RDS_CA_BUNDLE)
    valkey_host = str(output["valkey_primary_endpoint"])
    valkey_port = int(output["valkey_port"])
    valkey_password = urllib.parse.quote(str(output["valkey_auth_token"]), safe="")
    redis_url = f"rediss://:{valkey_password}@{valkey_host}:{valkey_port}/0?ssl_cert_reqs=required"

    admin_path = os.getenv("VAULT_DATABASE_ADMIN_SECRET_PATH", "a2a/production/database-admin")
    vault_write(admin_path, {"url": database_url})
    vault_write(os.getenv("VAULT_VALKEY_SECRET_PATH", "a2a/production/valkey"), {"url": redis_url})
    print(f"Stored PostgreSQL admin URL at vault://{admin_path}#url")
    valkey_path = os.getenv("VAULT_VALKEY_SECRET_PATH", "a2a/production/valkey")
    print(f"Stored Valkey URL at vault://{valkey_path}#url")


if __name__ == "__main__":
    main()
