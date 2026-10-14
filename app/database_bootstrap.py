"""Provision the least-privilege PostgreSQL role from inside the private cluster."""

from __future__ import annotations

import secrets
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import httpx
import psycopg
from psycopg import sql

from app.config import Settings, settings
from app.secrets import resolve_secret, write_vault_secret

ADMIN_REFERENCE = "vault://a2a/production/database-admin#url"
RUNTIME_REFERENCE = "vault://a2a/production/database#url"
RUNTIME_SECRET_PATH = "a2a/production/database"
RUNTIME_USERNAME = "a2a_runtime"


def _existing_password(config: Settings) -> str | None:
    try:
        current = resolve_secret(config, RUNTIME_REFERENCE)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise RuntimeError("Vault could not read the current runtime database secret.") from None
    except RuntimeError as exc:
        # A missing Vault field is a first-install condition; auth/network errors
        # have different messages and must not be mistaken for an empty store.
        if "is missing" in str(exc):
            return None
        raise
    if not current:
        return None
    password = urlsplit(current).password
    if not password:
        raise RuntimeError("The current Vault database URL has no runtime password.")
    return unquote(password)


def _runtime_url(admin_url: str, password: str) -> str:
    parsed = urlsplit(admin_url)
    if parsed.scheme not in {"postgresql", "postgresql+psycopg"}:
        raise ValueError("The Vault database administrator URL is not PostgreSQL.")
    if not parsed.hostname or not parsed.path or not parsed.username:
        raise ValueError("The Vault database administrator URL is incomplete.")
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{quote(RUNTIME_USERNAME, safe='')}:{quote(password, safe='')}@{host}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunsplit(("postgresql+psycopg", netloc, parsed.path, parsed.query, ""))


def bootstrap_database(config: Settings | None = None) -> None:
    config = config or settings
    admin_url = resolve_secret(config, ADMIN_REFERENCE)
    if not admin_url:
        raise RuntimeError("Vault database administrator URL is not configured.")
    parsed_admin = urlsplit(admin_url)
    if parsed_admin.scheme not in {"postgresql", "postgresql+psycopg"}:
        raise ValueError("Vault database administrator URL is not PostgreSQL.")

    password = _existing_password(config) or secrets.token_urlsafe(48)
    psycopg_url = admin_url.replace("postgresql+psycopg://", "postgresql://", 1)
    database = unquote(parsed_admin.path.lstrip("/"))
    admin_username = unquote(parsed_admin.username or "")
    if not database or not admin_username:
        raise ValueError("Vault database administrator URL is incomplete.")

    with psycopg.connect(psycopg_url, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (RUNTIME_USERNAME,))
            exists = cursor.fetchone() is not None
            statement = "ALTER ROLE {} WITH LOGIN" if exists else "CREATE ROLE {} LOGIN"
            cursor.execute(
                sql.SQL(statement + " NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(
                    sql.Identifier(RUNTIME_USERNAME), sql.Literal(password)
                )
            )
            cursor.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(database), sql.Identifier(RUNTIME_USERNAME)
                )
            )
            cursor.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                    sql.Identifier(RUNTIME_USERNAME)
                )
            )
            cursor.execute(
                sql.SQL(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}"
                ).format(sql.Identifier(RUNTIME_USERNAME))
            )
            cursor.execute(
                sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(
                    sql.Identifier(RUNTIME_USERNAME)
                )
            )
            cursor.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
                ).format(sql.Identifier(admin_username), sql.Identifier(RUNTIME_USERNAME))
            )
            cursor.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                    "GRANT USAGE, SELECT ON SEQUENCES TO {}"
                ).format(sql.Identifier(admin_username), sql.Identifier(RUNTIME_USERNAME))
            )

    write_vault_secret(config, RUNTIME_SECRET_PATH, {"url": _runtime_url(admin_url, password)})
    print("PostgreSQL runtime role is ready; its connection URL was updated in Vault.")


def main() -> None:
    bootstrap_database()


if __name__ == "__main__":
    main()
