"""Resolve runtime secrets from environment or Vault KV v2 references.

References use ``vault://path/to/secret#field``. Vault credentials used to
bootstrap AppRole authentication stay outside the secret store they unlock.
Resolved values are cached only in process memory for a short configured TTL.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import SecretStr

from app.config import Settings

_cache: dict[tuple[str, str], tuple[float, str]] = {}
_token_cache: dict[str, tuple[float, str]] = {}
_lock = threading.RLock()


def _secret_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    text = str(value).strip()
    return text or None


def resolve_secret(config: Settings, value: Any) -> str | None:
    """Resolve a raw value or a ``vault://`` / ``env://`` reference."""
    text = _secret_text(value)
    if not text:
        return None
    if text.startswith("env://"):
        name = text.removeprefix("env://")
        if not name or not name.replace("_", "").isalnum():
            raise ValueError("Invalid environment secret reference.")
        result = os.getenv(name)
        if not result:
            raise RuntimeError(f"Environment secret {name} is not configured.")
        return result
    if not text.startswith("vault://"):
        return text

    if not config.vault_addr:
        raise RuntimeError("A vault:// secret reference requires VAULT_ADDR.")
    path_and_key = text.removeprefix("vault://")
    path, separator, key = path_and_key.partition("#")
    field = key if separator else "value"
    if not path or not field or path.startswith("/") or ".." in path.split("/"):
        raise ValueError("Invalid Vault secret reference.")
    cache_key = (config.vault_addr.rstrip("/"), text)
    now = time.monotonic()
    with _lock:
        cached = _cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]

    token = _vault_token(config)
    mount = quote(config.vault_kv_mount.strip("/"), safe="")
    encoded_path = "/".join(quote(part, safe="") for part in path.split("/"))
    headers = {"X-Vault-Token": token}
    if config.vault_namespace:
        headers["X-Vault-Namespace"] = config.vault_namespace
    response = httpx.get(
        f"{config.vault_addr.rstrip('/')}/v1/{mount}/data/{encoded_path}",
        headers=headers,
        timeout=5.0,
    )
    response.raise_for_status()
    payload = response.json()
    secret = payload.get("data", {}).get("data", {}).get(field)
    if not isinstance(secret, str) or not secret:
        raise RuntimeError("Vault secret is missing or is not a non-empty string.")
    if config.vault_cache_ttl_seconds:
        with _lock:
            _cache[cache_key] = (now + config.vault_cache_ttl_seconds, secret)
    return secret


def resolve_config_secret(config: Settings, field: str) -> str | None:
    return resolve_secret(config, getattr(config, field))


def resolve_named_secret(config: Settings, name: str | None) -> str | None:
    if not name:
        return None
    return resolve_secret(config, os.getenv(name))


def _vault_token(config: Settings) -> str:
    if config.vault_token:
        return config.vault_token.get_secret_value()
    assert config.vault_addr
    cache_key = config.vault_addr.rstrip("/")
    now = time.monotonic()
    with _lock:
        cached = _token_cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]
    headers = {"X-Vault-Namespace": config.vault_namespace} if config.vault_namespace else {}
    if config.vault_role_id and config.vault_secret_id:
        auth_path = "approle"
        body = {
            "role_id": config.vault_role_id.get_secret_value(),
            "secret_id": config.vault_secret_id.get_secret_value(),
        }
    elif config.vault_kubernetes_role:
        auth_path = config.vault_kubernetes_mount.strip("/")
        try:
            jwt_token = (
                Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text().strip()
            )
        except OSError as exc:
            raise RuntimeError("Kubernetes service-account token is unavailable.") from exc
        body = {"role": config.vault_kubernetes_role, "jwt": jwt_token}
    else:
        raise RuntimeError("Vault authentication is not configured.")
    response = httpx.post(
        f"{cache_key}/v1/auth/{quote(auth_path, safe='')}/login",
        headers=headers,
        json=body,
        timeout=5.0,
    )
    response.raise_for_status()
    auth = response.json().get("auth") or {}
    token = auth.get("client_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("Vault AppRole login did not return a client token.")
    lease_seconds = max(30, int(auth.get("lease_duration") or 300))
    with _lock:
        _token_cache[cache_key] = (now + max(5, lease_seconds - 30), token)
    return token


def clear_secret_cache() -> None:
    """Clear caches for tests and explicit secret rotation handling."""
    with _lock:
        _cache.clear()
        _token_cache.clear()
