"""Store enabled provider credentials from the protected production input file in Vault."""

from __future__ import annotations

import os

from store_runtime_secrets import vault_write


def store() -> list[str]:
    groups: dict[str, dict[str, str]] = {
        "a2a/production/providers/model": {"api_key": os.getenv("MODEL_API_KEY", "")},
        "a2a/production/observability/langfuse": {
            "public_key": os.getenv("LANGFUSE_PUBLIC_KEY", ""),
            "secret_key": os.getenv("LANGFUSE_SECRET_KEY", ""),
        },
        "a2a/production/integrations/jira": {"api_token": os.getenv("JIRA_API_TOKEN", "")},
        "a2a/production/integrations/github": {"token": os.getenv("GITHUB_TOKEN", "")},
        "a2a/production/integrations/gmail": {
            "client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
            "refresh_token": os.getenv("GOOGLE_REFRESH_TOKEN", ""),
        },
        "a2a/production/integrations/mqtt": {
            "broker_url": os.getenv("MQTT_BROKER_URL", ""),
            "username": os.getenv("MQTT_USERNAME", ""),
            "password": os.getenv("MQTT_PASSWORD", ""),
        },
    }
    written = []
    for path, values in groups.items():
        configured = {key: value for key, value in values.items() if value}
        if not configured:
            continue
        if len(configured) != len(values):
            raise ValueError(f"Vault secret group {path} is only partially configured.")
        vault_write(path, configured)
        written.append(path)
    return written


def main() -> None:
    paths = store()
    for path in paths:
        print(f"Stored configured credentials at vault://{path}")


if __name__ == "__main__":
    main()
