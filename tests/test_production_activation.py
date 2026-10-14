import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy" / "aws" / "production"))
from activate import _bucket_names, _redact, read_config, validate_config  # noqa: E402


def valid_config():
    return {
        "AWS_PROFILE": "",
        "AWS_REGION": "us-east-1",
        "PUBLIC_DOMAIN": "agents.acme-corp.net",
        "ROUTE53_ZONE_ID": "Z123456789012345678",
        "CLUSTER_ADMIN_CIDRS": "198.51.100.22/32,198.51.100.23/32",
        "AUDIT_BUCKET_NAME": "",
        "BACKUP_BUCKET_NAME": "",
        "DATABASE_ADMIN_PASSWORD": "d" * 32,
        "VALKEY_AUTH_TOKEN": "v" * 40,
        "KEYCLOAK_URL": "https://identity.acme-corp.net",
        "KEYCLOAK_REALM": "a2a",
        "KEYCLOAK_ADMIN_REALM": "master",
        "KEYCLOAK_ADMIN_USERNAME": "bootstrap-admin",
        "KEYCLOAK_ADMIN_PASSWORD": "keycloak-password-not-real",
        "KEYCLOAK_ADMIN_USERNAMES": "platform-admin",
        "AGENT_CLIENT_IDS": "a2a-agent-knowledge,a2a-agent-operations",
        "VAULT_ADDR": "https://vault.acme-corp.net",
        "VAULT_NAMESPACE": "",
        "VAULT_KV_MOUNT": "secret",
        "VAULT_PROVISIONER_TOKEN": "vault-test-token-not-real",
        "MODEL_BASE_URL": "https://api.openai.com/v1",
        "MODEL_NAME": "gpt-4.1-mini",
        "MODEL_API_KEY": "model-key-not-real",
        "LANGFUSE_HOST": "https://cloud.langfuse.com",
        "LANGFUSE_PUBLIC_KEY": "pk-lf-not-real",
        "LANGFUSE_SECRET_KEY": "sk-lf-not-real",
        "EMBEDDING_BASE_URL": "https://api.openai.com/v1",
        "EMBEDDING_MODEL": "text-embedding-3-small",
        "CONNECTOR_ALLOWED_HOSTS": "api.github.com,api.atlassian.com",
        "JIRA_BASE_URL": "",
        "JIRA_EMAIL": "",
        "JIRA_API_TOKEN": "",
        "GITHUB_TOKEN": "",
        "GOOGLE_CLIENT_ID": "",
        "GOOGLE_CLIENT_SECRET": "",
        "GOOGLE_REFRESH_TOKEN": "",
        "MQTT_BROKER_URL": "",
        "MQTT_USERNAME": "",
        "MQTT_PASSWORD": "",
        "MQTT_CA_FILE": "",
    }


def test_production_input_validates_and_generates_bucket_names():
    values = valid_config()
    validate_config(values)
    assert _bucket_names(values, "123456789012") == (
        "a2a-prod-audit-123456789012-useast1",
        "a2a-prod-backup-123456789012-useast1",
    )


def test_production_input_rejects_template_and_partial_integrations(tmp_path):
    values = valid_config()
    values["PUBLIC_DOMAIN"] = "agents.your-company.com"
    with pytest.raises(ValueError, match="template"):
        validate_config(values)

    values = valid_config()
    values["JIRA_BASE_URL"] = "https://acme.atlassian.net"
    with pytest.raises(ValueError, match="JIRA_EMAIL"):
        validate_config(values)


def test_production_input_file_parser_keeps_equals_and_restricts_permissions(tmp_path):
    values = valid_config()
    config_file = tmp_path / "production.env"
    values["LANGFUSE_SECRET_KEY"] = "secret=a=b=="
    config_file.write_text(
        "# protected input\n" + "\n".join(f"{key}={value}" for key, value in values.items()),
        encoding="utf-8",
    )
    parsed = read_config(config_file)
    assert parsed["LANGFUSE_SECRET_KEY"] == "secret=a=b=="
    assert config_file.stat().st_mode & 0o077 == 0


def test_diagnostics_redact_exact_secret_values():
    assert _redact("failed with secret-value", ["secret-value"]) == "failed with [REDACTED]"
