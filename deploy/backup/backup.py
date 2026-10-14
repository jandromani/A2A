from __future__ import annotations

import hashlib
import os
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import boto3

from app.config import settings
from app.secrets import resolve_secret


def main() -> None:
    database_url = resolve_secret(settings, os.environ.get("DATABASE_URL"))
    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured.")
    if database_url.startswith("postgresql+psycopg://"):
        database_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgresql", "postgres"} or not parsed.hostname:
        raise RuntimeError("DATABASE_URL must identify PostgreSQL for this backup job.")
    bucket = os.environ["BACKUP_BUCKET"]
    prefix = os.getenv("BACKUP_PREFIX", "a2a/postgres").strip("/")
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    key = f"{prefix}/{timestamp}.dump"
    with tempfile.TemporaryDirectory(prefix="a2a-pg-backup-") as directory:
        dump_path = Path(directory) / "database.dump"
        password_file = Path(directory) / ".pgpass"
        port = parsed.port or 5432
        db_name = unquote(parsed.path.lstrip("/"))
        username = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        if not db_name or not username:
            raise RuntimeError("DATABASE_URL must contain a database name and user.")

        def escape_pgpass(value: str) -> str:
            return value.replace("\\", "\\\\").replace(":", "\\:")

        password_file.write_text(
            ":".join(
                escape_pgpass(value)
                for value in (parsed.hostname, str(port), db_name, username, password)
            )
            + "\n"
        )
        password_file.chmod(0o600)
        pg_env = {**os.environ, "PGPASSFILE": str(password_file)}
        query = parse_qs(parsed.query)
        for parameter, variable in (
            ("sslmode", "PGSSLMODE"),
            ("sslrootcert", "PGSSLROOTCERT"),
            ("sslcert", "PGSSLCERT"),
            ("sslkey", "PGSSLKEY"),
        ):
            if query.get(parameter):
                pg_env[variable] = query[parameter][0]
        result = subprocess.run(
            [
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-acl",
                "--no-password",
                "--host",
                parsed.hostname,
                "--port",
                str(port),
                "--username",
                username,
                "--dbname",
                db_name,
                "--file",
                str(dump_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=int(os.getenv("BACKUP_TIMEOUT_SECONDS", "3600")),
            env=pg_env,
        )
        if result.returncode:
            # Never echo the connection URL; driver diagnostics can contain host metadata.
            raise RuntimeError(f"pg_dump failed with exit code {result.returncode}.")
        listing = subprocess.run(
            ["pg_restore", "--list", str(dump_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if listing.returncode or not listing.stdout.strip():
            raise RuntimeError("pg_dump produced an invalid or empty custom-format archive.")
        digest = hashlib.sha256(dump_path.read_bytes()).hexdigest()
        checksum_path = Path(directory) / "database.dump.sha256"
        checksum_path.write_text(f"{digest}  {key.rsplit('/', 1)[-1]}\n")
        kwargs = {}
        kms_key_id = os.getenv("S3_KMS_KEY_ID")
        if kms_key_id:
            kwargs.update(ServerSideEncryption="aws:kms", SSEKMSKeyId=kms_key_id)
        else:
            kwargs.update(ServerSideEncryption="AES256")
        s3 = boto3.client(
            "s3",
            region_name=os.getenv("AWS_REGION", "us-east-1"),
            endpoint_url=os.getenv("S3_ENDPOINT_URL") or None,
        )
        s3.upload_file(str(dump_path), bucket, key, ExtraArgs=kwargs)
        s3.upload_file(
            str(checksum_path),
            bucket,
            f"{key}.sha256",
            ExtraArgs={
                "ServerSideEncryption": kwargs["ServerSideEncryption"],
                **({"SSEKMSKeyId": kwargs["SSEKMSKeyId"]} if "SSEKMSKeyId" in kwargs else {}),
            },
        )
        data_lake_path = os.getenv("DATA_LAKE_PATH")
        if data_lake_path and Path(data_lake_path).is_dir():
            lake_archive = Path(directory) / "data-lake.tar.gz"
            with tarfile.open(lake_archive, "w:gz") as archive:
                archive.add(data_lake_path, arcname="lake", recursive=True)
            s3.upload_file(
                str(lake_archive),
                bucket,
                f"{prefix}/data-lake/{timestamp}.tar.gz",
                ExtraArgs=kwargs,
            )
    print(f"PostgreSQL backup uploaded to s3://{bucket}/{key}")


if __name__ == "__main__":
    main()
