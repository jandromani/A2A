"""Fail-closed readiness checks for the external production services."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from sqlalchemy import text

from app.audit_archive import _object_lock_retention, archive_audit_chain, create_s3_client
from app.config import settings
from app.database import make_engine, make_session_factory
from app.policy import authorize
from app.schemas import Principal
from app.secrets import resolve_config_secret, resolve_secret

logging.basicConfig(level=logging.ERROR)


def _check_services() -> list[tuple[str, bool]]:
    results: list[tuple[str, bool]] = []
    config = settings

    def check(name: str, action) -> None:
        try:
            action()
            results.append((name, True))
        except Exception:
            # Do not print exception text: database drivers and auth libraries can
            # include connection details or tokens in diagnostics.
            results.append((name, False))

    def static_configuration() -> None:
        if config.environment != "production":
            raise ValueError
        if config.auth_mode != "oidc" or not config.oidc_issuer_url or not config.oidc_audience:
            raise ValueError
        if not config.public_url.startswith("https://"):
            raise ValueError
        if config.secret_provider != "vault" or not (config.vault_addr or "").startswith(
            "https://"
        ):
            raise ValueError
        if not config.opa_url:
            raise ValueError
        if (
            not config.langfuse_host
            or not config.langfuse_public_key
            or not config.langfuse_secret_key
        ):
            raise ValueError
        if not config.audit_archive_bucket:
            raise ValueError
        if not config.redis_url:
            raise ValueError
        if not config.database_url.startswith("vault://") or not config.redis_url.startswith(
            "vault://"
        ):
            raise ValueError

    check("production configuration", static_configuration)

    def oidc() -> None:
        issuer = (config.oidc_issuer_url or "").rstrip("/")
        metadata = httpx.get(f"{issuer}/.well-known/openid-configuration", timeout=8).json()
        if metadata.get("issuer", "").rstrip("/") != issuer:
            raise ValueError
        if not metadata.get("jwks_uri") or not metadata.get("token_endpoint"):
            raise ValueError

    check("Keycloak OIDC discovery", oidc)

    def vault_values() -> tuple[str, str]:
        database_url = resolve_secret(config, config.database_url)
        redis_url = resolve_secret(config, config.redis_url)
        langfuse_public = resolve_config_secret(config, "langfuse_public_key")
        langfuse_secret = resolve_config_secret(config, "langfuse_secret_key")
        if not database_url or not redis_url or not langfuse_public or not langfuse_secret:
            raise ValueError
        return database_url, redis_url

    resolved: list[str] = []

    def resolve_runtime_secrets() -> None:
        resolved[:] = vault_values()

    check("Vault authentication and runtime secret reads", resolve_runtime_secrets)

    def database() -> None:
        if len(resolved) < 2:
            raise ValueError
        query = parse_qs(urlsplit(resolved[0]).query)
        if query.get("sslmode", [""])[0] not in {"verify-full", "verify-ca"}:
            raise ValueError
        ca_bundle = query.get("sslrootcert", [""])[0]
        if not ca_bundle or not Path(ca_bundle).is_file():
            raise ValueError
        engine = make_engine(resolved[0])
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                version = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar()
                if version != "0003_governance_ledger":
                    raise ValueError
        finally:
            engine.dispose()

    check("PostgreSQL TLS and migrations", database)

    def valkey() -> None:
        if len(resolved) < 2 or not resolved[1].startswith("rediss://"):
            raise ValueError
        from redis import Redis

        redis = Redis.from_url(resolved[1], decode_responses=True, socket_timeout=5)
        try:
            if redis.ping() is not True:
                raise ValueError
        finally:
            redis.close()

    check("Valkey TLS connectivity", valkey)

    def opa_policy() -> None:
        allowed, _ = authorize(
            Principal(subject="preflight", roles=frozenset({"employee"}), scopes=frozenset()),
            "task.create",
            {},
            config,
        )
        denied, _ = authorize(
            Principal(subject="preflight", roles=frozenset({"employee"}), scopes=frozenset()),
            "identity.manage",
            {},
            config,
        )
        if not allowed or denied:
            raise ValueError

    check("OPA allow and deny policy", opa_policy)

    def langfuse_configuration() -> None:
        if not (config.langfuse_host or "").startswith("https://"):
            raise ValueError
        if not resolve_config_secret(config, "langfuse_public_key"):
            raise ValueError
        if not resolve_config_secret(config, "langfuse_secret_key"):
            raise ValueError
        if not resolve_config_secret(config, "model_api_key"):
            raise ValueError

    check("Langfuse and model provider credentials", langfuse_configuration)

    def immutable_audit() -> None:
        s3 = create_s3_client(config)
        _object_lock_retention(
            s3, config.audit_archive_bucket or "", config.audit_archive_retention_days
        )
        if len(resolved) < 1:
            raise ValueError
        engine = make_engine(resolved[0])
        try:
            result = archive_audit_chain(make_session_factory(engine), s3, config, verify_only=True)
            if not result["valid"]:
                raise ValueError
        finally:
            engine.dispose()

    check("S3 WORM audit archive and SQL parity", immutable_audit)
    return results


def main() -> None:
    results = _check_services()
    for name, passed in results:
        print(f"{'OK' if passed else 'FAIL'}  {name}")
    if any(not passed for _, passed in results):
        print("Production readiness failed. Diagnostics intentionally omit secret-bearing errors.")
        sys.exit(1)
    print("Production dependencies and immutable audit archive are ready.")


if __name__ == "__main__":
    main()
