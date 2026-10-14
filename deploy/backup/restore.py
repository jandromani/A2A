"""Restore the latest PostgreSQL archive into an isolated, temporary PostgreSQL pod."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path

from app.schema_version import EXPECTED_SCHEMA_VERSION


def latest_backup_key(s3, bucket: str, prefix: str) -> str:
    normalized = prefix.strip("/")
    paginator = s3.get_paginator("list_objects_v2")
    objects = [
        item
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{normalized}/")
        for item in page.get("Contents", [])
        if item.get("Key", "").endswith(".dump")
    ]
    if not objects:
        raise RuntimeError("No PostgreSQL backup archive exists in the configured S3 prefix.")
    return str(max(objects, key=lambda item: item["LastModified"])["Key"])


def verify_checksum(path: Path, checksum_text: str, object_key: str) -> None:
    match = re.fullmatch(r"([a-f0-9]{64})\s+([^\s]+)\s*", checksum_text)
    if not match or match.group(2) != object_key.rsplit("/", 1)[-1]:
        raise RuntimeError("The PostgreSQL backup checksum sidecar is malformed.")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != match.group(1):
        raise RuntimeError("The PostgreSQL backup checksum does not match the archive.")


def main() -> None:
    import boto3

    bucket = os.environ["BACKUP_BUCKET"]
    prefix = os.getenv("BACKUP_PREFIX", "a2a/production")
    database = os.getenv("RESTORE_DATABASE", "a2a_restore")
    host = os.getenv("RESTORE_DATABASE_HOST", "127.0.0.1")
    port = os.getenv("RESTORE_DATABASE_PORT", "5432")
    username = os.getenv("RESTORE_DATABASE_USER", "restore")
    s3 = boto3.client("s3", region_name=os.getenv("AWS_REGION", "us-east-1"))
    key = latest_backup_key(s3, bucket, prefix)
    with tempfile.TemporaryDirectory(prefix="a2a-restore-check-") as directory:
        archive = Path(directory) / "database.dump"
        s3.download_file(bucket, key, str(archive))
        checksum_object = s3.get_object(Bucket=bucket, Key=f"{key}.sha256")
        verify_checksum(archive, checksum_object["Body"].read().decode("ascii"), key)
        restored = subprocess.run(
            [
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--no-acl",
                "--host",
                host,
                "--port",
                port,
                "--username",
                username,
                "--dbname",
                database,
                str(archive),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=int(os.getenv("RESTORE_TIMEOUT_SECONDS", "7200")),
        )
        if restored.returncode:
            raise RuntimeError("pg_restore failed in the isolated recovery database.")
        result = subprocess.run(
            [
                "psql",
                "--no-psqlrc",
                "--tuples-only",
                "--no-align",
                "--host",
                host,
                "--port",
                port,
                "--username",
                username,
                "--dbname",
                database,
                "--command",
                "SELECT version_num FROM alembic_version",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode or result.stdout.strip() != EXPECTED_SCHEMA_VERSION:
            raise RuntimeError("The restored database schema is incomplete or out of date.")
    print(f"PostgreSQL restore verification passed for s3://{bucket}/{key}")


if __name__ == "__main__":
    main()
