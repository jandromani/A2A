"""Append-only S3 Object Lock archive for the tamper-evident audit chain."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.audit import audit_hash, iso_utc
from app.config import Settings
from app.models import AuditChainState, AuditRecord


def _entry(row: AuditRecord) -> dict[str, Any]:
    return {
        "sequence": row.sequence,
        "id": row.id,
        "actor": row.actor,
        "action": row.action,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "decision": row.decision,
        "details_json": row.details_json or {},
        "created_at": iso_utc(row.created_at),
        "previous_hash": row.previous_hash,
        "entry_hash": row.entry_hash,
    }


def _canonical(entry: dict[str, Any]) -> bytes:
    return json.dumps(entry, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def _key(prefix: str, sequence: int) -> str:
    return f"{prefix.strip('/')}/events/{sequence:020d}.json"


def _object_lock_retention(s3: Any, bucket: str, days: int) -> None:
    configuration = s3.get_object_lock_configuration(Bucket=bucket).get(
        "ObjectLockConfiguration", {}
    )
    rule = configuration.get("Rule", {}).get("DefaultRetention", {})
    configured_days = int(rule.get("Days") or 0) + int(rule.get("Years") or 0) * 365
    if (
        configuration.get("ObjectLockEnabled") != "Enabled"
        or rule.get("Mode") != "COMPLIANCE"
        or configured_days < days
    ):
        raise RuntimeError(
            "Audit bucket must enforce COMPLIANCE Object Lock for at least "
            "the configured retention."
        )


def _versions(s3: Any, bucket: str, prefix: str):
    paginator = s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/events/"):
        entries = [
            (item["Key"], item.get("VersionId"), True) for item in page.get("DeleteMarkers", [])
        ] + [(item["Key"], item.get("VersionId"), False) for item in page.get("Versions", [])]
        yield from sorted(entries, key=lambda item: item[0])


def archive_audit_chain(
    factory: sessionmaker,
    s3: Any,
    config: Settings,
    *,
    verify_only: bool = False,
) -> dict[str, Any]:
    """Verify the SQL chain and mirror every row to immutable S3 objects.

    Keys are sequence-based, so an archived sequence cannot be silently replaced
    by a different hash-chain entry. Listing the remote prefix also detects rows
    removed from SQL after they were archived.
    """
    bucket = config.audit_archive_bucket
    if not bucket:
        raise RuntimeError("AUDIT_ARCHIVE_BUCKET is required.")
    prefix = config.audit_archive_prefix.strip("/")
    if not prefix:
        raise RuntimeError("AUDIT_ARCHIVE_PREFIX must not be empty.")
    retention_days = config.audit_archive_retention_days
    _object_lock_retention(s3, bucket, retention_days)

    archived_versions = iter(_versions(s3, bucket, prefix))
    remote_version = next(archived_versions, None)
    previous_hash = ""
    entries = 0
    archived = 0
    now = datetime.now(UTC)

    with factory() as session:
        rows = session.scalars(
            select(AuditRecord)
            .order_by(AuditRecord.sequence.asc())
            .execution_options(yield_per=500)
        )
        for expected_sequence, row in enumerate(rows, start=1):
            if row.sequence != expected_sequence:
                raise RuntimeError(f"Audit chain sequence gap at {expected_sequence}.")
            if row.previous_hash != previous_hash:
                raise RuntimeError(f"Audit chain previous hash mismatch at {expected_sequence}.")
            body = {
                "sequence": row.sequence,
                "id": row.id,
                "actor": row.actor,
                "action": row.action,
                "resource_type": row.resource_type,
                "resource_id": row.resource_id,
                "decision": row.decision,
                "details_json": row.details_json or {},
                "created_at": row.created_at,
            }
            expected_hash = audit_hash(body, previous_hash)
            if row.entry_hash != expected_hash:
                raise RuntimeError(f"Audit entry hash mismatch at {expected_sequence}.")

            object_key = _key(prefix, row.sequence)
            while remote_version and remote_version[0] < object_key:
                raise RuntimeError(
                    "Immutable audit archive contains an entry absent from SQL: "
                    f"{remote_version[0]}."
                )
            payload = _canonical(_entry(row))
            digest = hashlib.sha256(payload).hexdigest()
            if remote_version and remote_version[0] == object_key:
                object_versions = []
                while remote_version and remote_version[0] == object_key:
                    object_versions.append(remote_version)
                    remote_version = next(archived_versions, None)
                if len(object_versions) != 1 or object_versions[0][2]:
                    raise RuntimeError(
                        f"Immutable audit archive has unexpected versions at {expected_sequence}."
                    )
                _, version_id, _ = object_versions[0]
                version_kwargs = {"Bucket": bucket, "Key": object_key}
                if version_id is not None:
                    version_kwargs["VersionId"] = version_id
                existing = s3.get_object(**version_kwargs)["Body"].read()
                head = s3.head_object(**version_kwargs)
                retention_until = head.get("ObjectLockRetainUntilDate")
                if (
                    existing != payload
                    or head.get("Metadata", {}).get("sha256") != digest
                    or head.get("ObjectLockMode") != "COMPLIANCE"
                    or not retention_until
                    or retention_until <= now
                ):
                    raise RuntimeError(f"Immutable audit archive mismatch at {expected_sequence}.")
            else:
                if verify_only:
                    raise RuntimeError(f"Audit entry {expected_sequence} is not archived.")
                kwargs: dict[str, Any] = {
                    "Bucket": bucket,
                    "Key": object_key,
                    "Body": payload,
                    "ContentType": "application/json",
                    "Metadata": {"sha256": digest, "entry-hash": row.entry_hash},
                    "ObjectLockMode": "COMPLIANCE",
                    "ObjectLockRetainUntilDate": now + timedelta(days=retention_days),
                }
                if config.audit_archive_kms_key_id:
                    kwargs["ServerSideEncryption"] = "aws:kms"
                    kwargs["SSEKMSKeyId"] = config.audit_archive_kms_key_id
                else:
                    kwargs["ServerSideEncryption"] = "AES256"
                s3.put_object(**kwargs)
                archived += 1
            previous_hash = expected_hash
            entries += 1

        head = session.get(AuditChainState, "global")
        if head and (head.last_sequence != entries or head.head_hash != previous_hash):
            raise RuntimeError("Audit chain head does not match its entries.")
        if entries and not head:
            raise RuntimeError("Audit chain head is missing.")

    if remote_version:
        raise RuntimeError(
            f"Immutable audit archive contains an entry absent from SQL: {remote_version[0]}."
        )
    return {"valid": True, "entries": entries, "archived": archived, "verified": entries - archived}


def create_s3_client(config: Settings) -> Any:
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - exercised in the backup image
        raise RuntimeError("The audit archive worker image must include boto3.") from exc
    return boto3.client("s3", region_name=config.audit_archive_region)
