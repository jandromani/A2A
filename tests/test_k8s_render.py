import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deploy.k8s.render import render_templates  # noqa: E402

TEMPLATE_DIR = ROOT / "deploy" / "k8s"
REGISTRY = "123456789012.dkr.ecr.us-east-1.amazonaws.com/a2a-prod"
APP_REPOSITORY = f"{REGISTRY}/application"
OPS_REPOSITORY = f"{REGISTRY}/operations"


@pytest.fixture
def production_config():
    return {
        "A2A_IMAGE": f"{APP_REPOSITORY}:release-abc123",
        "BACKUP_IMAGE": f"{OPS_REPOSITORY}:release-abc123",
        "PUBLIC_URL": "https://agents.acme-corp.com",
        "OIDC_ISSUER_URL": "https://identity.acme-corp.com/realms/a2a",
        "VAULT_ADDR": "https://vault.acme-corp.com",
        "CONNECTOR_ALLOWED_HOSTS": "api.atlassian.com,api.github.com,oauth2.googleapis.com",
        "MODEL_BASE_URL": "https://api.openai.com/v1",
        "MODEL_NAME": "gpt-4.1-mini",
        "LANGFUSE_HOST": "https://cloud.langfuse.com",
        "EMBEDDING_BASE_URL": "https://api.openai.com/v1",
        "EMBEDDING_MODEL": "text-embedding-3-small",
        "JIRA_BASE_URL": "",
        "JIRA_EMAIL": "",
    }


@pytest.fixture
def terraform_output():
    return {
        "region": "us-east-1",
        "application_image_repository": APP_REPOSITORY,
        "operations_image_repository": OPS_REPOSITORY,
        "backup_bucket_name": "a2a-prod-backups",
        "audit_bucket_name": "a2a-prod-audit",
        "backup_kms_key_arn": "arn:aws:kms:us-east-1:123456789012:key/backup",
        "audit_kms_key_arn": "arn:aws:kms:us-east-1:123456789012:key/audit",
        "backup_writer_role_arn": "arn:aws:iam::123456789012:role/a2a-backup",
        "audit_archiver_role_arn": "arn:aws:iam::123456789012:role/a2a-audit",
        "audit_retention_days": 2557,
        "efs_file_system_id": "fs-0123456789abcdef0",
        "public_domain": "agents.acme-corp.com",
        "public_certificate_arn": "arn:aws:acm:us-east-1:123456789012:certificate/test-cert",
    }


def test_renderer_resolves_and_validates_all_base_manifests(
    tmp_path, production_config, terraform_output
):
    written = render_templates(
        env_values=production_config,
        outputs=terraform_output,
        output_dir=tmp_path,
        template_dir=TEMPLATE_DIR,
    )

    assert {path.name for path in written} == {
        "namespace.yaml",
        "storage.yaml",
        "opa.yaml",
        "config.yaml",
        "platform.yaml",
        "ops.yaml",
    }
    for path in written:
        contents = path.read_text(encoding="utf-8")
        assert "@@" not in contents
        assert all(isinstance(document, dict) for document in yaml.safe_load_all(contents))
    platform = (tmp_path / "platform.yaml").read_text(encoding="utf-8")
    assert "agents.acme-corp.com" in platform
    assert "alb.ingress.kubernetes.io/certificate-arn" in platform
    assert "a2a-prod/application:release-abc123" in platform
    rendered_opa = list(yaml.safe_load_all((tmp_path / "opa.yaml").read_text()))[0]
    source_policy = (ROOT / "deploy" / "policy" / "agentic.rego").read_text().strip()
    assert rendered_opa["data"]["agentic.rego"] == source_policy


def test_renderer_requires_https_and_real_external_values(
    tmp_path, production_config, terraform_output
):
    production_config["VAULT_ADDR"] = "http://vault.acme-corp.com"
    with pytest.raises(ValueError, match="HTTPS"):
        render_templates(
            env_values=production_config,
            outputs=terraform_output,
            output_dir=tmp_path,
            template_dir=TEMPLATE_DIR,
        )


def test_renderer_requires_domain_to_match_dns_validated_certificate(
    tmp_path, production_config, terraform_output
):
    terraform_output["public_domain"] = "other.acme-corp.com"
    with pytest.raises(ValueError, match="must match Terraform public_domain"):
        render_templates(
            env_values=production_config,
            outputs=terraform_output,
            output_dir=tmp_path,
            template_dir=TEMPLATE_DIR,
        )


def test_renderer_can_enable_iot_without_including_it_by_default(
    tmp_path, production_config, terraform_output
):
    written = render_templates(
        env_values=production_config,
        outputs=terraform_output,
        output_dir=tmp_path,
        template_dir=TEMPLATE_DIR,
        with_iot=True,
    )
    assert "iot.yaml" in {path.name for path in written}
