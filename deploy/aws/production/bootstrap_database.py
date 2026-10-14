"""Create a least-privilege PostgreSQL runtime role and put its URL in Vault."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import urllib.parse
from pathlib import Path

import psycopg
from psycopg import sql
from store_runtime_secrets import vault_write

RUNTIME_RDS_CA_BUNDLE = "/etc/ssl/certs/rds-global-bundle.pem"


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
    outputs = terraform_outputs(args.terraform_dir.resolve())
    ca_bundle = (
        Path(os.getenv("LOCAL_RDS_CA_BUNDLE", "/etc/ssl/certs/rds-global-bundle.pem"))
        .expanduser()
        .resolve()
    )
    if not ca_bundle.is_file():
        raise RuntimeError(
            "Download the AWS RDS CA bundle and set LOCAL_RDS_CA_BUNDLE before connecting."
        )
    admin_username = urllib.parse.quote(str(outputs["postgres_admin_username"]), safe="")
    admin_password = urllib.parse.quote(str(outputs["postgres_admin_password"]), safe="")
    admin_query = urllib.parse.urlencode(
        {"sslmode": "verify-full", "sslrootcert": str(ca_bundle)}, safe="/"
    )
    admin_url = (
        f"postgresql://{admin_username}:{admin_password}@{outputs['postgres_address']}:5432/"
        f"{outputs['postgres_database']}?{admin_query}"
    )
    runtime_user = str(outputs["postgres_runtime_username"])
    database = str(outputs["postgres_database"])
    admin_user = str(outputs["postgres_admin_username"])
    runtime_password = secrets.token_urlsafe(48)

    with psycopg.connect(admin_url, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (runtime_user,))
            exists = cursor.fetchone() is not None
            if exists:
                cursor.execute(
                    sql.SQL(
                        "ALTER ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}"
                    ).format(sql.Identifier(runtime_user), sql.Literal(runtime_password))
                )
            else:
                cursor.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}"
                    ).format(sql.Identifier(runtime_user), sql.Literal(runtime_password))
                )
            cursor.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(database), sql.Identifier(runtime_user)
                )
            )
            cursor.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(runtime_user))
            )
            cursor.execute(
                sql.SQL(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}"
                ).format(sql.Identifier(runtime_user))
            )
            cursor.execute(
                sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(
                    sql.Identifier(runtime_user)
                )
            )
            cursor.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
                ).format(sql.Identifier(admin_user), sql.Identifier(runtime_user))
            )
            cursor.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                    "GRANT USAGE, SELECT ON SEQUENCES TO {}"
                ).format(sql.Identifier(admin_user), sql.Identifier(runtime_user))
            )

    username = urllib.parse.quote(runtime_user, safe="")
    password = urllib.parse.quote(runtime_password, safe="")
    address = str(outputs["postgres_address"])
    runtime_query = urllib.parse.urlencode(
        {"sslmode": "verify-full", "sslrootcert": RUNTIME_RDS_CA_BUNDLE}, safe="/"
    )
    runtime_url = (
        f"postgresql+psycopg://{username}:{password}@{address}:5432/{database}?{runtime_query}"
    )
    path = "a2a/production/database"
    vault_write(path, {"url": runtime_url})
    print(f"Created PostgreSQL runtime role {runtime_user} and stored vault://{path}#url")


if __name__ == "__main__":
    main()
