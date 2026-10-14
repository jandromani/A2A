from urllib.parse import unquote, urlsplit

import httpx
from psycopg import sql

from app import database_bootstrap
from app.config import Settings
from app.database_bootstrap import _runtime_url
from app.secrets import write_vault_secret


def test_runtime_database_url_preserves_tls_and_escapes_generated_password():
    admin = (
        "postgresql://a2a_db_admin:admin%40password@db.internal:5432/agentic"
        "?sslmode=verify-full&sslrootcert=%2Fetc%2Fssl%2Frds.pem"
    )
    runtime = urlsplit(_runtime_url(admin, "new:@/#-password"))
    assert runtime.scheme == "postgresql+psycopg"
    assert runtime.hostname == "db.internal"
    assert runtime.username == "a2a_runtime"
    assert unquote(runtime.password or "") == "new:@/#-password"
    assert runtime.path == "/agentic"
    assert "sslmode=verify-full" in runtime.query
    assert "sslrootcert=%2Fetc%2Fssl%2Frds.pem" in runtime.query


def test_runtime_secret_writer_uses_short_lived_vault_token_and_kv_v2(monkeypatch):
    config = Settings(
        _env_file=None,
        environment="test",
        secret_provider="vault",
        vault_addr="https://vault.example.test",
        vault_token="runtime-workload-token",
    )
    seen = {}

    def fake_post(url, **kwargs):
        seen.update({"url": url, **kwargs})
        return httpx.Response(200)

    monkeypatch.setattr("app.secrets.httpx.post", fake_post)
    write_vault_secret(config, "a2a/production/database", {"url": "db://private"})
    assert seen["url"] == "https://vault.example.test/v1/secret/data/a2a/production/database"
    assert seen["headers"]["X-Vault-Token"] == "runtime-workload-token"
    assert seen["json"] == {"data": {"url": "db://private"}}


def test_runtime_secret_writer_rejects_vault_path_traversal():
    config = Settings(
        _env_file=None,
        environment="test",
        secret_provider="vault",
        vault_addr="https://vault.example.test",
        vault_token="runtime-workload-token",
    )
    try:
        write_vault_secret(config, "a2a/../root", {"token": "secret"})
    except ValueError as exc:
        assert "relative path" in str(exc)
    else:
        raise AssertionError("path traversal was accepted")


def test_bootstrap_reenables_an_existing_runtime_role(monkeypatch):
    queries = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, query, params=None):
            queries.append(query)

        def fetchone(self):
            return ("a2a_runtime",)

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def cursor(self):
            return FakeCursor()

    admin_url = "postgresql://admin:password@db.internal:5432/agentic?sslmode=verify-full"
    config = Settings(_env_file=None, environment="test")
    monkeypatch.setattr(database_bootstrap, "resolve_secret", lambda *_: admin_url)
    monkeypatch.setattr(database_bootstrap, "_existing_password", lambda _: "existing-password")
    monkeypatch.setattr(database_bootstrap.psycopg, "connect", lambda *_, **__: FakeConnection())
    monkeypatch.setattr(database_bootstrap, "write_vault_secret", lambda *_: None)

    database_bootstrap.bootstrap_database(config)
    role_statement = next(
        item for item in queries if isinstance(item, sql.Composed) and "ALTER ROLE" in repr(item)
    )
    sql_parts = [part._obj for part in role_statement._obj if isinstance(part, sql.SQL)]
    assert sql_parts[0] == "ALTER ROLE "
    assert "WITH LOGIN" in sql_parts[1]
    assert "CREATE ROLE" not in repr(role_statement)
