"""Resolve and validate production Kubernetes YAML from local config and Terraform outputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

ENV_KEYS = (
    "A2A_IMAGE",
    "BACKUP_IMAGE",
    "PUBLIC_URL",
    "OIDC_ISSUER_URL",
    "VAULT_ADDR",
    "CONNECTOR_ALLOWED_HOSTS",
    "MODEL_BASE_URL",
    "MODEL_UPSTREAM_BASE_URL",
    "MODEL_NAME",
    "LANGFUSE_HOST",
    "EMBEDDING_BASE_URL",
    "EMBEDDING_MODEL",
    "JIRA_BASE_URL",
    "JIRA_EMAIL",
    "VAULT_KV_MOUNT",
)
TOKEN = re.compile(r"@@([A-Z0-9_]+)@@")
OPTIONAL_ENV_KEYS = {"EMBEDDING_BASE_URL", "EMBEDDING_MODEL", "JIRA_BASE_URL", "JIRA_EMAIL"}
MODEL_GATEWAY_URL = "http://agentgateway.a2a.svc.cluster.local:4000/v1"
TEMPLATE_VALUE = re.compile(
    r"(?i)(example\.(org|com|net)|change[-_ ]?me|replace[-_ ]?me|placeholder|todo)"
)
TEMPLATE_FILES = (
    "namespace.yaml",
    "storage.yaml",
    "opa.yaml",
    "config.yaml",
    "bootstrap.yaml",
    "platform.yaml",
    "ops.yaml",
    "agentgateway.yaml",
    "agentgateway-edge.yaml",
)


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"{path}:{line_number}: expected KEY=VALUE.")
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"{path}:{line_number}: invalid key {key!r}.")
        if key not in ENV_KEYS:
            raise ValueError(f"{path}:{line_number}: unsupported key {key!r}.")
        if key in values:
            raise ValueError(f"{path}:{line_number}: duplicate key {key!r}.")
        values[key] = value.strip().strip("\"'")
    missing = [key for key in ENV_KEYS if key not in values]
    if missing:
        raise ValueError(f"Missing production values: {', '.join(missing)}.")
    return values


def terraform_outputs(directory: Path) -> dict[str, object]:
    result = subprocess.run(
        ["terraform", "output", "-json"],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("terraform output failed; initialize and apply infrastructure first.")
    decoded = json.loads(result.stdout)
    return {name: item["value"] for name, item in decoded.items()}


def _validate_config(values: dict[str, str]) -> tuple[str, str]:
    for key in ENV_KEYS:
        value = values.get(key, "").strip()
        if key not in OPTIONAL_ENV_KEYS and not value:
            raise ValueError(f"{key} must be configured for production.")
        if TEMPLATE_VALUE.search(value):
            raise ValueError(f"{key} still contains a template value.")

    def https_origin(key: str, *, allow_path: bool = False) -> str:
        value = values[key].strip().rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError(f"{key} must be an HTTPS URL without embedded credentials.")
        if not allow_path and parsed.path not in {"", "/"}:
            raise ValueError(f"{key} must be an origin without a path.")
        if parsed.query or parsed.fragment:
            raise ValueError(f"{key} cannot include a query or fragment.")
        return value

    public_url = https_origin("PUBLIC_URL")
    public_host = urlsplit(public_url).hostname
    https_origin("OIDC_ISSUER_URL", allow_path=True)
    https_origin("VAULT_ADDR")
    https_origin("LANGFUSE_HOST", allow_path=True)
    if values["MODEL_BASE_URL"].rstrip("/") != MODEL_GATEWAY_URL:
        raise ValueError("MODEL_BASE_URL must point to the in-cluster agentgateway service.")
    https_origin("MODEL_UPSTREAM_BASE_URL", allow_path=True)
    if values["EMBEDDING_BASE_URL"]:
        https_origin("EMBEDDING_BASE_URL", allow_path=True)
    if bool(values["JIRA_BASE_URL"]) != bool(values["JIRA_EMAIL"]):
        raise ValueError("JIRA_BASE_URL and JIRA_EMAIL must be set together.")
    if values["JIRA_BASE_URL"]:
        https_origin("JIRA_BASE_URL", allow_path=True)
    return public_url, str(public_host)


def render_templates(
    *,
    env_values: dict[str, str],
    outputs: dict[str, object],
    output_dir: Path,
    template_dir: Path,
    with_iot: bool = False,
) -> list[Path]:
    public_url, public_host = _validate_config(env_values)
    if public_host.casefold() != str(outputs.get("public_domain", "")).casefold():
        raise ValueError(
            "PUBLIC_URL host must match Terraform public_domain for the ACM certificate."
        )
    output_fields = (
        "region",
        "application_image_repository",
        "operations_image_repository",
        "backup_bucket_name",
        "audit_bucket_name",
        "backup_kms_key_arn",
        "audit_kms_key_arn",
        "backup_writer_role_arn",
        "audit_archiver_role_arn",
        "audit_retention_days",
        "efs_file_system_id",
        "public_domain",
        "public_certificate_arn",
        "postgres_max_allocated_storage_gb",
    )
    missing = [name for name in output_fields if not outputs.get(name)]
    if missing:
        raise ValueError(f"Missing Terraform outputs: {', '.join(missing)}.")

    for key, output_name in (
        ("A2A_IMAGE", "application_image_repository"),
        ("BACKUP_IMAGE", "operations_image_repository"),
    ):
        image = env_values[key].strip()
        repository = str(outputs[output_name])
        digest_image = re.fullmatch(re.escape(repository) + r"@sha256:[a-f0-9]{64}", image)
        if digest_image:
            continue
        if not image.startswith(f"{repository}:"):
            raise ValueError(f"{key} must use its private Terraform-provisioned ECR repository.")
        tag = image.removeprefix(f"{repository}:")
        if tag and (tag == "latest" or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag)):
            raise ValueError(f"{key} must use an immutable non-latest image tag or SHA-256 digest.")

    replacements = {key: value for key, value in env_values.items()}
    replacements["RELEASE_SUFFIX"] = hashlib.sha256(
        env_values["A2A_IMAGE"].encode("utf-8")
    ).hexdigest()[:12]
    replacements.update(
        {
            "PUBLIC_URL": public_url,
            "PUBLIC_HOST": public_host,
            "PUBLIC_CERTIFICATE_ARN": str(outputs["public_certificate_arn"]),
            "STORAGE_CLASS": "a2a-efs-rwx",
            "AWS_REGION": str(outputs["region"]),
            "BACKUP_BUCKET": str(outputs["backup_bucket_name"]),
            "AUDIT_BUCKET": str(outputs["audit_bucket_name"]),
            "BACKUP_KMS_KEY_ARN": str(outputs["backup_kms_key_arn"]),
            "AUDIT_KMS_KEY_ARN": str(outputs["audit_kms_key_arn"]),
            "BACKUP_WRITER_ROLE_ARN": str(outputs["backup_writer_role_arn"]),
            "AUDIT_ARCHIVER_ROLE_ARN": str(outputs["audit_archiver_role_arn"]),
            "AUDIT_RETENTION_DAYS": str(outputs["audit_retention_days"]),
            "EFS_FILE_SYSTEM_ID": str(outputs["efs_file_system_id"]),
            "RESTORE_STORAGE_GIB": (
                f"{max(1, int(outputs['postgres_max_allocated_storage_gb']) * 5 // 4)}Gi"
            ),
        }
    )
    replacements["OPA_POLICY"] = (
        (template_dir.parent / "policy" / "agentic.rego").read_text(encoding="utf-8").strip()
    )
    filenames = list(TEMPLATE_FILES)
    if with_iot:
        filenames.append("iot.yaml")
    output_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for filename in filenames:
        source = (template_dir / filename).read_text(encoding="utf-8")
        found = set(TOKEN.findall(source))
        unknown = sorted(found - replacements.keys())
        if unknown:
            raise ValueError(f"{filename} contains unknown template values: {', '.join(unknown)}.")
        resolved = TOKEN.sub(
            lambda match: json.dumps(str(replacements[match.group(1)]), ensure_ascii=True)[1:-1],
            source,
        )
        if TOKEN.search(resolved):
            raise ValueError(f"{filename} still contains unresolved template values.")
        try:
            documents = list(yaml.safe_load_all(resolved))
        except yaml.YAMLError as exc:
            raise ValueError(f"{filename} is invalid YAML: {exc}") from exc
        if not documents or any(not isinstance(item, dict) for item in documents):
            raise ValueError(f"{filename} must contain Kubernetes object documents.")
        target = output_dir / filename
        target.write_text(resolved, encoding="utf-8")
        written.append(target)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--terraform-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--with-iot", action="store_true")
    args = parser.parse_args()
    try:
        values = load_env_file(args.env_file)
        outputs = terraform_outputs(args.terraform_dir.resolve())
        written = render_templates(
            env_values=values,
            outputs=outputs,
            output_dir=args.output_dir,
            template_dir=Path(__file__).resolve().parent,
            with_iot=args.with_iot,
        )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"render failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    for path in written:
        print(path)


if __name__ == "__main__":
    main()
