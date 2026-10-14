"""Provision and deploy A2A after one protected production input file is filled in."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from ipaddress import ip_network
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[3]
PRODUCTION = Path(__file__).resolve().parent
STATE_BACKEND = ROOT / "deploy" / "aws" / "state-backend"
K8S = ROOT / "deploy" / "k8s"
PRIVATE = ROOT / ".secrets" / "production"
DEFAULT_CONFIG = PRODUCTION / "production.env"
REQUIRED = {
    "AWS_REGION",
    "PUBLIC_DOMAIN",
    "ROUTE53_ZONE_ID",
    "CLUSTER_ADMIN_CIDRS",
    "DATABASE_ADMIN_PASSWORD",
    "VALKEY_AUTH_TOKEN",
    "KEYCLOAK_URL",
    "KEYCLOAK_REALM",
    "KEYCLOAK_ADMIN_REALM",
    "KEYCLOAK_ADMIN_USERNAME",
    "KEYCLOAK_ADMIN_PASSWORD",
    "KEYCLOAK_ADMIN_USERNAMES",
    "AGENT_CLIENT_IDS",
    "VAULT_ADDR",
    "VAULT_KV_MOUNT",
    "VAULT_PROVISIONER_TOKEN",
    "MODEL_BASE_URL",
    "MODEL_NAME",
    "MODEL_API_KEY",
    "LANGFUSE_HOST",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "EMBEDDING_BASE_URL",
    "EMBEDDING_MODEL",
    "CONNECTOR_ALLOWED_HOSTS",
}
OPTIONAL = {
    "AWS_PROFILE",
    "AUDIT_BUCKET_NAME",
    "BACKUP_BUCKET_NAME",
    "VAULT_NAMESPACE",
    "JIRA_BASE_URL",
    "JIRA_EMAIL",
    "JIRA_API_TOKEN",
    "GITHUB_TOKEN",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_REFRESH_TOKEN",
    "MQTT_BROKER_URL",
    "MQTT_USERNAME",
    "MQTT_PASSWORD",
    "MQTT_CA_FILE",
}
ALLOWED = REQUIRED | OPTIONAL
SECRET_KEYS = {
    "DATABASE_ADMIN_PASSWORD",
    "VALKEY_AUTH_TOKEN",
    "KEYCLOAK_ADMIN_PASSWORD",
    "VAULT_PROVISIONER_TOKEN",
    "MODEL_API_KEY",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "JIRA_API_TOKEN",
    "GITHUB_TOKEN",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_REFRESH_TOKEN",
    "MQTT_PASSWORD",
}
PLACEHOLDER = re.compile(
    r"(?i)(replace|your[-_ ]|example\.(org|com|net)|placeholder|todo|z_replace)"
)
TOOLS = ("aws", "terraform", "docker", "kubectl", "helm", "git")


def read_config(path: Path) -> dict[str, str]:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise ValueError(
            f"Production input file not found: {path}. Copy production.env.example and fill it in."
        )
    try:
        path.chmod(0o600)
    except OSError as exc:
        raise ValueError("Could not restrict production input file permissions to 0600.") from exc
    values: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"{path}:{line_number}: expected KEY=VALUE.")
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"{path}:{line_number}: invalid setting name.")
        if key not in ALLOWED:
            raise ValueError(f"{path}:{line_number}: unsupported setting {key}.")
        if key in values:
            raise ValueError(f"{path}:{line_number}: duplicate setting {key}.")
        values[key] = value.strip().strip("\"'")
    missing = sorted(key for key in REQUIRED if not values.get(key, "").strip())
    if missing:
        raise ValueError(f"Missing production settings: {', '.join(missing)}.")
    return values


def _https_url(key: str, value: str, *, allow_path: bool = False) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{key} must be a valid HTTPS URL without embedded credentials.")
    if not allow_path and parsed.path not in {"", "/"}:
        raise ValueError(f"{key} must be an origin without a path.")
    return value.rstrip("/")


def validate_config(values: dict[str, str]) -> None:
    for key, value in values.items():
        if PLACEHOLDER.search(value):
            raise ValueError(f"{key} still contains a template value.")
    if len(values["DATABASE_ADMIN_PASSWORD"]) < 24:
        raise ValueError("DATABASE_ADMIN_PASSWORD must contain at least 24 characters.")
    if len(values["VALKEY_AUTH_TOKEN"]) < 32:
        raise ValueError("VALKEY_AUTH_TOKEN must contain at least 32 characters.")
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", values["AWS_REGION"]):
        raise ValueError("AWS_REGION must be an AWS region such as us-east-1.")
    if not re.fullmatch(r"Z[A-Z0-9]+", values["ROUTE53_ZONE_ID"]):
        raise ValueError("ROUTE53_ZONE_ID must be an existing public hosted-zone ID.")
    domain = values["PUBLIC_DOMAIN"].rstrip(".")
    if not re.fullmatch(
        r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}",
        domain,
    ):
        raise ValueError("PUBLIC_DOMAIN must be a DNS hostname without scheme or path.")
    for cidr in values["CLUSTER_ADMIN_CIDRS"].split(","):
        try:
            network = ip_network(cidr.strip(), strict=False)
        except ValueError as exc:
            raise ValueError("CLUSTER_ADMIN_CIDRS must be comma-separated IPv4 CIDRs.") from exc
        if network.version != 4:
            raise ValueError("CLUSTER_ADMIN_CIDRS must use IPv4 CIDRs.")
    for key in ("KEYCLOAK_URL", "VAULT_ADDR"):
        _https_url(key, values[key])
    for key in ("MODEL_BASE_URL", "LANGFUSE_HOST", "EMBEDDING_BASE_URL"):
        _https_url(key, values[key], allow_path=True)
    realm = values["KEYCLOAK_REALM"]
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", realm):
        raise ValueError("KEYCLOAK_REALM contains unsupported characters.")
    if not values["KEYCLOAK_ADMIN_USERNAMES"].strip():
        raise ValueError("Set at least one existing realm user in KEYCLOAK_ADMIN_USERNAMES.")
    client_ids = [item.strip() for item in values["AGENT_CLIENT_IDS"].split(",") if item.strip()]
    if not client_ids or len(client_ids) != len(set(client_ids)):
        raise ValueError("AGENT_CLIENT_IDS must contain unique agent client IDs.")
    for client_id in client_ids:
        if not re.fullmatch(r"[A-Za-z0-9._:-]{3,200}", client_id):
            raise ValueError("AGENT_CLIENT_IDS contains an invalid client ID.")
    hosts = [item.strip().lower() for item in values["CONNECTOR_ALLOWED_HOSTS"].split(",")]
    if not hosts or any(not re.fullmatch(r"[A-Za-z0-9.-]+", host) for host in hosts):
        raise ValueError("CONNECTOR_ALLOWED_HOSTS must be comma-separated DNS hostnames.")
    if values.get("AUDIT_BUCKET_NAME") and values.get("BACKUP_BUCKET_NAME"):
        if values["AUDIT_BUCKET_NAME"] == values["BACKUP_BUCKET_NAME"]:
            raise ValueError("Audit and backup buckets must be separate.")
    if bool(values.get("JIRA_BASE_URL")) != bool(values.get("JIRA_EMAIL")):
        raise ValueError("JIRA_BASE_URL and JIRA_EMAIL must be set together.")
    if values.get("JIRA_BASE_URL"):
        _https_url("JIRA_BASE_URL", values["JIRA_BASE_URL"], allow_path=True)
        if not values.get("JIRA_API_TOKEN"):
            raise ValueError("JIRA_API_TOKEN is required when Jira is enabled.")
    for keys, label in (
        (("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"), "Gmail"),
        (("MQTT_BROKER_URL", "MQTT_USERNAME", "MQTT_PASSWORD", "MQTT_CA_FILE"), "MQTT"),
    ):
        enabled = [bool(values.get(key)) for key in keys]
        if any(enabled) and not all(enabled):
            raise ValueError(f"{label} settings must be supplied together.")
    if values.get("MQTT_BROKER_URL"):
        parsed = urlsplit(values["MQTT_BROKER_URL"])
        if parsed.scheme != "mqtts" or not parsed.hostname:
            raise ValueError("MQTT_BROKER_URL must use mqtts:// and include a broker hostname.")
        if not Path(values["MQTT_CA_FILE"]).expanduser().is_file():
            raise ValueError("MQTT_CA_FILE must point to the broker CA certificate.")


def _secret_values(values: dict[str, str]) -> list[str]:
    return [value for key, value in values.items() if key in SECRET_KEYS and value]


def _redact(text: str, secrets_to_hide: list[str]) -> str:
    for value in secrets_to_hide:
        text = text.replace(value, "[REDACTED]")
    return text


def run(
    command: list[str],
    *,
    env: dict[str, str],
    secrets_to_hide: list[str],
    capture: bool = False,
    input_text: str | None = None,
) -> str:
    if capture or input_text is not None:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            message = _redact((result.stderr or result.stdout).strip(), secrets_to_hide)
            raise RuntimeError(message or f"Command failed: {Path(command[0]).name}.")
        if not capture and result.stdout.strip():
            print(_redact(result.stdout.rstrip(), secrets_to_hide))
        return result.stdout
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout
    for line in process.stdout:
        print(_redact(line.rstrip(), secrets_to_hide), flush=True)
    if process.wait():
        raise RuntimeError(f"Command failed: {Path(command[0]).name}.")
    return ""


def _capture_json(command: list[str], env: dict[str, str], hidden: list[str]) -> dict:
    return json.loads(run(command, env=env, secrets_to_hide=hidden, capture=True))


def _write_private(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o600)


def _bucket_names(values: dict[str, str], account: str) -> tuple[str, str]:
    region = values["AWS_REGION"].replace("-", "")
    account_region = f"{account}-{region}"
    audit = values.get("AUDIT_BUCKET_NAME") or f"a2a-prod-audit-{account_region}"
    backup = values.get("BACKUP_BUCKET_NAME") or f"a2a-prod-backup-{account_region}"
    bucket_pattern = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
    for value in (audit, backup):
        if not bucket_pattern.fullmatch(value) or ".." in value:
            raise ValueError("S3 bucket names must be valid globally unique DNS names.")
    if audit == backup:
        raise ValueError("Audit and backup buckets must be separate.")
    return audit, backup


def _terraform_outputs(directory: Path, env: dict[str, str], hidden: list[str]) -> dict:
    data = _capture_json(["terraform", f"-chdir={directory}", "output", "-json"], env, hidden)
    return {name: item["value"] for name, item in data.items()}


def _apply_terraform(values: dict[str, str], env: dict[str, str], hidden: list[str]) -> dict:
    account = run(
        ["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"],
        env=env,
        secrets_to_hide=hidden,
        capture=True,
    ).strip()
    if not re.fullmatch(r"\d{12}", account):
        raise RuntimeError("AWS did not return an account ID; authenticate the configured profile.")
    audit_bucket, backup_bucket = _bucket_names(values, account)
    if not env.get("AWS_PROFILE") and values.get("AWS_PROFILE"):
        env["AWS_PROFILE"] = values["AWS_PROFILE"]

    state_metadata = STATE_BACKEND / ".terraform" / "terraform.tfstate"
    state_backend_type = ""
    if state_metadata.exists():
        try:
            state_backend_type = (
                json.loads(state_metadata.read_text(encoding="utf-8"))
                .get("backend", {})
                .get("type", "")
            )
        except (OSError, json.JSONDecodeError):
            state_backend_type = ""
    state_init = ["terraform", f"-chdir={STATE_BACKEND}", "init", "-input=false"]
    if state_backend_type != "s3":
        state_init.append("-backend=false")
    run(state_init, env=env, secrets_to_hide=hidden)
    run(
        [
            "terraform",
            f"-chdir={STATE_BACKEND}",
            "apply",
            "-input=false",
            "-auto-approve",
            "-var",
            f"region={values['AWS_REGION']}",
        ],
        env=env,
        secrets_to_hide=hidden,
    )
    state_outputs = _terraform_outputs(STATE_BACKEND, env, hidden)
    state_bucket = str(state_outputs["state_bucket"])
    state_key = str(state_outputs["state_kms_key_arn"])
    state_backend = PRIVATE / "state-backend.hcl"
    _write_private(
        state_backend,
        "\n".join(
            (
                f'bucket = "{state_bucket}"',
                'key = "a2a/state-backend.tfstate"',
                f'region = "{values["AWS_REGION"]}"',
                "encrypt = true",
                f'kms_key_id = "{state_key}"',
                "use_lockfile = true",
                "",
            )
        ),
    )
    if state_backend_type != "s3":
        run(
            [
                "terraform",
                f"-chdir={STATE_BACKEND}",
                "init",
                "-input=false",
                "-migrate-state",
                "-backend-config=" + str(state_backend),
            ],
            env=env,
            secrets_to_hide=hidden,
        )

    production_backend = PRIVATE / "production-backend.hcl"
    _write_private(
        production_backend,
        "\n".join(
            (
                f'bucket = "{state_bucket}"',
                'key = "a2a/production/terraform.tfstate"',
                f'region = "{values["AWS_REGION"]}"',
                "encrypt = true",
                f'kms_key_id = "{state_key}"',
                "use_lockfile = true",
                "",
            )
        ),
    )
    production_dir = PRODUCTION
    run(
        [
            "terraform",
            f"-chdir={production_dir}",
            "init",
            "-input=false",
            "-reconfigure",
            "-backend-config=" + str(production_backend),
        ],
        env=env,
        secrets_to_hide=hidden,
    )
    terraform_env = env.copy()
    for key in tuple(terraform_env):
        if key.startswith("TF_VAR_"):
            terraform_env.pop(key)
    terraform_env.update(
        {
            "TF_VAR_region": values["AWS_REGION"],
            "TF_VAR_public_domain": values["PUBLIC_DOMAIN"],
            "TF_VAR_route53_zone_id": values["ROUTE53_ZONE_ID"],
            "TF_VAR_cluster_admin_cidrs": json.dumps(
                [item.strip() for item in values["CLUSTER_ADMIN_CIDRS"].split(",")]
            ),
            "TF_VAR_audit_bucket_name": audit_bucket,
            "TF_VAR_backup_bucket_name": backup_bucket,
            "TF_VAR_database_admin_password": values["DATABASE_ADMIN_PASSWORD"],
            "TF_VAR_valkey_auth_token": values["VALKEY_AUTH_TOKEN"],
        }
    )
    plan_path = PRIVATE / "production.tfplan"
    run(
        [
            "terraform",
            f"-chdir={production_dir}",
            "plan",
            "-input=false",
            "-out=" + str(plan_path),
        ],
        env=terraform_env,
        secrets_to_hide=hidden,
    )
    plan_path.chmod(0o600)
    run(
        ["terraform", f"-chdir={production_dir}", "apply", "-input=false", str(plan_path)],
        env=terraform_env,
        secrets_to_hide=hidden,
    )
    return _terraform_outputs(production_dir, terraform_env, hidden)


def _image_tag() -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--short=12", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("Run activation from a Git checkout with a committed release.")
    dirty = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain"],
        text=True,
        capture_output=True,
        check=False,
    )
    if dirty.returncode or dirty.stdout.strip():
        raise RuntimeError("Commit the release before building immutable production images.")
    return "release-" + result.stdout.strip()


def _terraform_runtime_secrets(env: dict[str, str], hidden: list[str], outputs: dict) -> None:
    region = str(outputs["region"])
    endpoint = str(outputs["cluster_endpoint"])
    ca_data = run(
        [
            "aws",
            "eks",
            "describe-cluster",
            "--region",
            region,
            "--name",
            str(outputs["cluster_name"]),
            "--query",
            "cluster.certificateAuthority.data",
            "--output",
            "text",
        ],
        env=env,
        secrets_to_hide=hidden,
        capture=True,
    ).strip()
    ca_file = PRIVATE / "eks-ca.pem"
    _write_private(ca_file, base64.b64decode(ca_data, validate=True).decode("ascii"))
    reviewer_token = ""
    for _ in range(60):
        reviewer_token = run(
            [
                "kubectl",
                "-n",
                "kube-system",
                "get",
                "secret",
                "a2a-vault-token-reviewer-token",
                "-o",
                "jsonpath={.data.token}",
            ],
            env=env,
            secrets_to_hide=hidden,
            capture=True,
        ).strip()
        if reviewer_token:
            break
        time.sleep(5)
    if not reviewer_token:
        raise RuntimeError("Kubernetes did not issue the Vault TokenReview service token.")
    decoded_token = base64.b64decode(reviewer_token).decode("utf-8")
    vault_env = env.copy()
    vault_env.update(
        {
            "KUBERNETES_API_URL": endpoint,
            "KUBERNETES_TOKEN_REVIEWER_JWT": decoded_token,
            "KUBERNETES_CA_FILE": str(ca_file),
            "VAULT_KUBERNETES_MOUNT": "kubernetes",
        }
    )
    run(
        [sys.executable, str(ROOT / "deploy" / "vault" / "configure_kubernetes_auth.py")],
        env=vault_env,
        secrets_to_hide=hidden + [decoded_token],
    )
    run(
        [
            sys.executable,
            str(PRODUCTION / "store_runtime_secrets.py"),
            "--terraform-dir",
            str(PRODUCTION),
        ],
        env=env,
        secrets_to_hide=hidden,
    )
    run(
        [sys.executable, str(PRODUCTION / "store_external_secrets.py")],
        env=env,
        secrets_to_hide=hidden,
    )


def _render_env(values: dict[str, str], outputs: dict, tag: str) -> Path:
    app_image = f"{outputs['application_image_repository']}:{tag}"
    ops_image = f"{outputs['operations_image_repository']}:{tag}"
    render_values = {
        "A2A_IMAGE": app_image,
        "BACKUP_IMAGE": ops_image,
        "PUBLIC_URL": f"https://{values['PUBLIC_DOMAIN']}",
        "OIDC_ISSUER_URL": (
            f"{values['KEYCLOAK_URL'].rstrip('/')}/realms/{values['KEYCLOAK_REALM']}"
        ),
        "VAULT_ADDR": values["VAULT_ADDR"],
        "CONNECTOR_ALLOWED_HOSTS": values["CONNECTOR_ALLOWED_HOSTS"],
        "MODEL_BASE_URL": "http://agentgateway.a2a.svc.cluster.local:4000/v1",
        "MODEL_UPSTREAM_BASE_URL": values["MODEL_BASE_URL"],
        "MODEL_NAME": values["MODEL_NAME"],
        "VAULT_KV_MOUNT": values["VAULT_KV_MOUNT"],
        "LANGFUSE_HOST": values["LANGFUSE_HOST"],
        "EMBEDDING_BASE_URL": values["EMBEDDING_BASE_URL"],
        "EMBEDDING_MODEL": values["EMBEDDING_MODEL"],
        "JIRA_BASE_URL": values.get("JIRA_BASE_URL", ""),
        "JIRA_EMAIL": values.get("JIRA_EMAIL", ""),
    }
    path = PRIVATE / "render.env"
    contents = "".join(f"{key}={value}\n" for key, value in render_values.items())
    _write_private(path, contents)
    return path


def _kubectl(env: dict[str, str], hidden: list[str], *args: str) -> str:
    return run(["kubectl", *args], env=env, secrets_to_hide=hidden, capture=True)


def _wait_public(url: str, hidden: list[str]) -> None:
    for _ in range(60):
        try:
            response = httpx.get(url.rstrip("/") + "/ready", timeout=10.0, follow_redirects=False)
            if response.is_success:
                card = httpx.get(
                    url.rstrip("/") + "/.well-known/agent-card.json",
                    timeout=10.0,
                    follow_redirects=False,
                )
                if card.is_success:
                    print("Public HTTPS readiness and A2A Agent Card are reachable.")
                    return
        except httpx.HTTPError:
            pass
        time.sleep(10)
    raise RuntimeError("Public HTTPS readiness or the A2A Agent Card did not become healthy.")


def deploy(
    values: dict[str, str],
    outputs: dict,
    env: dict[str, str],
    hidden: list[str],
    tag: str,
) -> None:
    image_env = env.copy()
    image_env["IMAGE_TAG"] = tag
    run([str(PRODUCTION / "publish_images.sh")], env=image_env, secrets_to_hide=hidden)
    run([str(K8S / "cluster-addons.sh")], env=image_env, secrets_to_hide=hidden)
    run(
        ["kubectl", "apply", "-f", str(K8S / "vault-auth.yaml")],
        env=image_env,
        secrets_to_hide=hidden,
    )
    _terraform_runtime_secrets(image_env, hidden, outputs)

    render_env = _render_env(values, outputs, tag)
    output_dir = PRIVATE / "rendered-k8s"
    render_args = [
        sys.executable,
        str(K8S / "render.py"),
        "--env-file",
        str(render_env),
        "--terraform-dir",
        str(PRODUCTION),
        "--output-dir",
        str(output_dir),
    ]
    if values.get("MQTT_BROKER_URL"):
        render_args.append("--with-iot")
    run(render_args, env=image_env, secrets_to_hide=hidden)
    for filename in ("namespace.yaml", "storage.yaml", "config.yaml", "opa.yaml"):
        run(
            ["kubectl", "apply", "-f", str(output_dir / filename)],
            env=image_env,
            secrets_to_hide=hidden,
        )
    if values.get("MQTT_CA_FILE"):
        cert_file = str(Path(values["MQTT_CA_FILE"]).expanduser().resolve())
        manifest = run(
            [
                "kubectl",
                "-n",
                "a2a",
                "create",
                "secret",
                "generic",
                "a2a-mqtt-tls",
                "--from-file=ca.crt=" + cert_file,
                "--dry-run=client",
                "-o",
                "yaml",
            ],
            env=image_env,
            secrets_to_hide=hidden,
            capture=True,
        )
        run(
            ["kubectl", "apply", "-f", "-"],
            env=image_env,
            secrets_to_hide=hidden,
            input_text=manifest,
        )
    gateway_env_file = PRIVATE / "agentgateway.env"
    _write_private(gateway_env_file, f"MODEL_API_KEY={values['MODEL_API_KEY']}\n")
    gateway_secret = run(
        [
            "kubectl",
            "-n",
            "a2a",
            "create",
            "secret",
            "generic",
            "a2a-agentgateway-model",
            "--from-env-file=" + str(gateway_env_file),
            "--dry-run=client",
            "-o",
            "yaml",
        ],
        env=image_env,
        secrets_to_hide=hidden,
        capture=True,
    )
    run(
        ["kubectl", "apply", "-f", "-"],
        env=image_env,
        secrets_to_hide=hidden,
        input_text=gateway_secret,
    )
    gateway_env_file.unlink(missing_ok=True)
    for filename in ("agentgateway.yaml", "agentgateway-edge.yaml"):
        run(
            ["kubectl", "apply", "-f", str(output_dir / filename)],
            env=image_env,
            secrets_to_hide=hidden,
        )
    _kubectl(
        image_env,
        hidden,
        "delete",
        "job",
        "-n",
        "a2a",
        "-l",
        "app=a2a-db-bootstrap",
        "--ignore-not-found=true",
    )
    run(
        ["kubectl", "apply", "-f", str(output_dir / "bootstrap.yaml")],
        env=image_env,
        secrets_to_hide=hidden,
    )
    release_suffix = hashlib.sha256(image_env["A2A_IMAGE"].encode()).hexdigest()[:12]
    bootstrap_name = "a2a-db-bootstrap-" + release_suffix
    run(
        [
            "kubectl",
            "wait",
            "-n",
            "a2a",
            "--for=condition=complete",
            f"job/{bootstrap_name}",
            "--timeout=10m",
        ],
        env=image_env,
        secrets_to_hide=hidden,
    )
    _kubectl(
        image_env,
        hidden,
        "delete",
        "job",
        "-n",
        "a2a",
        "-l",
        "app=a2a-migrate",
        "--ignore-not-found=true",
    )
    run(
        ["kubectl", "apply", "-f", str(output_dir / "ops.yaml")],
        env=image_env,
        secrets_to_hide=hidden,
    )
    migration_name = "a2a-migrate-" + release_suffix
    run(
        [
            "kubectl",
            "wait",
            "-n",
            "a2a",
            "--for=condition=complete",
            f"job/{migration_name}",
            "--timeout=10m",
        ],
        env=image_env,
        secrets_to_hide=hidden,
    )
    run(
        ["kubectl", "apply", "-f", str(output_dir / "platform.yaml")],
        env=image_env,
        secrets_to_hide=hidden,
    )
    for deployment in (
        "agentgateway",
        "agentgateway-edge",
        "a2a-api",
        "a2a-event-worker",
        "opa",
    ):
        run(
            [
                "kubectl",
                "rollout",
                "status",
                "-n",
                "a2a",
                f"deployment/{deployment}",
                "--timeout=10m",
            ],
            env=image_env,
            secrets_to_hide=hidden,
        )
    if values.get("MQTT_BROKER_URL"):
        run(
            ["kubectl", "apply", "-f", str(output_dir / "iot.yaml")],
            env=image_env,
            secrets_to_hide=hidden,
        )
        run(
            [
                "kubectl",
                "rollout",
                "status",
                "-n",
                "a2a",
                "deployment/a2a-mqtt-ingress",
                "--timeout=10m",
            ],
            env=image_env,
            secrets_to_hide=hidden,
        )
    run([str(PRODUCTION / "publish_dns.sh")], env=image_env, secrets_to_hide=hidden)
    public_url = f"https://{values['PUBLIC_DOMAIN']}"
    _wait_public(public_url, hidden)

    keycloak_env = image_env.copy()
    keycloak_env.update(
        {
            "KEYCLOAK_URL": values["KEYCLOAK_URL"],
            "KEYCLOAK_REALM": values["KEYCLOAK_REALM"],
            "KEYCLOAK_ADMIN_REALM": values["KEYCLOAK_ADMIN_REALM"],
            "KEYCLOAK_ADMIN_USERNAME": values["KEYCLOAK_ADMIN_USERNAME"],
            "KEYCLOAK_ADMIN_PASSWORD": values["KEYCLOAK_ADMIN_PASSWORD"],
            "KEYCLOAK_ADMIN_USERNAMES": values["KEYCLOAK_ADMIN_USERNAMES"],
            "A2A_API_URL": public_url,
            "A2A_PUBLIC_URL": public_url,
            "VAULT_ADDR": values["VAULT_ADDR"],
            "VAULT_KV_MOUNT": values["VAULT_KV_MOUNT"],
            "VAULT_PROVISIONER_TOKEN": values["VAULT_PROVISIONER_TOKEN"],
        }
    )
    for key in ("VAULT_NAMESPACE",):
        if values.get(key):
            keycloak_env[key] = values[key]
    client_ids = ",".join(item.strip() for item in values["AGENT_CLIENT_IDS"].split(","))
    run(
        [sys.executable, "-m", "app.keycloak_provisioning", "--agent-client-ids", client_ids],
        env=keycloak_env,
        secrets_to_hide=hidden,
    )

    backup_name = "a2a-postgres-backup-initial-" + tag.removeprefix("release-")
    run(
        [
            "kubectl",
            "create",
            "job",
            "-n",
            "a2a",
            "--from=cronjob/a2a-postgres-backup",
            backup_name,
        ],
        env=image_env,
        secrets_to_hide=hidden,
    )
    run(
        [
            "kubectl",
            "wait",
            "-n",
            "a2a",
            "--for=condition=complete",
            f"job/{backup_name}",
            "--timeout=2h",
        ],
        env=image_env,
        secrets_to_hide=hidden,
    )
    backup_logs = _kubectl(image_env, hidden, "logs", "-n", "a2a", f"job/{backup_name}")
    if "PostgreSQL backup uploaded to s3://" not in backup_logs:
        raise RuntimeError("The initial PostgreSQL backup did not confirm its S3 upload.")

    restore_name = "a2a-postgres-restore-initial-" + tag.removeprefix("release-")
    run(
        [
            "kubectl",
            "create",
            "job",
            "-n",
            "a2a",
            "--from=cronjob/a2a-postgres-restore-verification",
            restore_name,
        ],
        env=image_env,
        secrets_to_hide=hidden,
    )
    run(
        [
            "kubectl",
            "wait",
            "-n",
            "a2a",
            "--for=condition=complete",
            f"job/{restore_name}",
            "--timeout=2h",
        ],
        env=image_env,
        secrets_to_hide=hidden,
    )
    restore_logs = _kubectl(image_env, hidden, "logs", "-n", "a2a", f"job/{restore_name}")
    if "PostgreSQL restore verification passed for s3://" not in restore_logs:
        raise RuntimeError("The initial isolated PostgreSQL restore verification failed.")

    archive_name = "a2a-audit-initial-" + tag.removeprefix("release-")
    run(
        ["kubectl", "create", "job", "-n", "a2a", "--from=cronjob/a2a-audit-archive", archive_name],
        env=image_env,
        secrets_to_hide=hidden,
    )
    run(
        [
            "kubectl",
            "wait",
            "-n",
            "a2a",
            "--for=condition=complete",
            f"job/{archive_name}",
            "--timeout=10m",
        ],
        env=image_env,
        secrets_to_hide=hidden,
    )
    preflight_name = "a2a-preflight-" + tag.removeprefix("release-")
    run(
        [
            "kubectl",
            "create",
            "job",
            "-n",
            "a2a",
            "--from=cronjob/a2a-production-preflight",
            preflight_name,
        ],
        env=image_env,
        secrets_to_hide=hidden,
    )
    run(
        [
            "kubectl",
            "wait",
            "-n",
            "a2a",
            "--for=condition=complete",
            f"job/{preflight_name}",
            "--timeout=10m",
        ],
        env=image_env,
        secrets_to_hide=hidden,
    )
    logs = _kubectl(image_env, hidden, "logs", "-n", "a2a", f"job/{preflight_name}")
    print(_redact(logs.rstrip(), hidden))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create cloud resources, write secrets to Vault and deploy production workloads.",
    )
    args = parser.parse_args()
    try:
        values = read_config(args.config)
        validate_config(values)
        missing_tools = [tool for tool in TOOLS if not shutil.which(tool)]
        if missing_tools:
            raise RuntimeError(
                f"Install the required production tools: {', '.join(missing_tools)}."
            )
        hidden = _secret_values(values)
        env = os.environ.copy()
        env.update(values)
        if not values.get("AWS_PROFILE") and os.environ.get("AWS_PROFILE"):
            env["AWS_PROFILE"] = os.environ["AWS_PROFILE"]
        env["AWS_REGION"] = values["AWS_REGION"]
        if values.get("AWS_PROFILE"):
            env["AWS_PROFILE"] = values["AWS_PROFILE"]
        if not args.apply:
            print("Production input file and required local tools are valid.")
            print(
                "Review the AWS account and run this command with --apply to provision and deploy."
            )
            return
        print("Checking the authenticated AWS account and preparing encrypted Terraform state.")
        tag = _image_tag()
        outputs = _apply_terraform(values, env, hidden)
        deploy(values, outputs, env, hidden, tag)
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
    ) as exc:
        print(
            f"Production activation stopped: {_redact(str(exc), locals().get('hidden', []))}",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    print(f"A2A production activation completed: https://{values['PUBLIC_DOMAIN']}")


if __name__ == "__main__":
    main()
