"""Tamper-evident audit-chain helpers and read-only replay projections."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditChainState, AuditRecord, TaskEvent


def iso_utc(value: datetime | str) -> str:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def audit_hash(record: dict[str, Any], previous_hash: str) -> str:
    canonical = {
        "sequence": int(record["sequence"]),
        "id": str(record["id"]),
        "actor": str(record["actor"]),
        "action": str(record["action"]),
        "resource_type": str(record["resource_type"]),
        "resource_id": record.get("resource_id"),
        "decision": str(record["decision"]),
        "details_json": record.get("details_json") or {},
        "created_at": iso_utc(record["created_at"]),
        "previous_hash": previous_hash,
    }
    encoded = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def verify_audit_chain(session: Session) -> dict[str, Any]:
    rows = session.scalars(select(AuditRecord).order_by(AuditRecord.sequence.asc())).all()
    head = session.get(AuditChainState, "global")
    previous_hash = ""
    failure: dict[str, Any] | None = None
    for expected, row in enumerate(rows, start=1):
        if row.sequence != expected:
            failure = {"sequence": row.sequence, "reason": "sequence_gap"}
            break
        if row.previous_hash != previous_hash:
            failure = {"sequence": row.sequence, "reason": "previous_hash_mismatch"}
            break
        body = {
            "sequence": row.sequence,
            "id": row.id,
            "actor": row.actor,
            "action": row.action,
            "resource_type": row.resource_type,
            "resource_id": row.resource_id,
            "decision": row.decision,
            "details_json": row.details_json,
            "created_at": row.created_at,
        }
        expected_hash = audit_hash(body, previous_hash)
        if row.entry_hash != expected_hash:
            failure = {"sequence": row.sequence, "reason": "entry_hash_mismatch"}
            break
        previous_hash = expected_hash
    if not failure and rows and not head:
        failure = {"sequence": len(rows), "reason": "chain_head_missing"}
    if (
        not failure
        and head
        and (head.last_sequence != len(rows) or head.head_hash != previous_hash)
    ):
        failure = {"sequence": len(rows), "reason": "chain_head_mismatch"}
    return {
        "valid": failure is None,
        "entries": len(rows),
        "head_sequence": head.last_sequence if head else 0,
        "head_hash": head.head_hash if head else "",
        "failure": failure,
    }


def replay_task_timeline(session: Session, task_id: str) -> dict[str, Any]:
    events = session.scalars(
        select(TaskEvent)
        .where(TaskEvent.task_id == task_id)
        .order_by(TaskEvent.created_at.asc(), TaskEvent.id.asc())
    ).all()
    audit = session.scalars(
        select(AuditRecord)
        .where(AuditRecord.resource_type == "task", AuditRecord.resource_id == task_id)
        .order_by(AuditRecord.sequence.asc())
    ).all()
    timeline = [
        {
            "kind": "event",
            "id": event.id,
            "type": event.event_type,
            "message": event.message,
            "payload": event.payload_json,
            "created_at": iso_utc(event.created_at),
        }
        for event in events
    ] + [
        {
            "kind": "audit",
            "id": record.id,
            "sequence": record.sequence,
            "action": record.action,
            "actor": record.actor,
            "decision": record.decision,
            "details": record.details_json,
            "entry_hash": record.entry_hash,
            "created_at": iso_utc(record.created_at),
        }
        for record in audit
    ]
    timeline.sort(key=lambda item: (item["created_at"], item["kind"], item["id"]))
    return {
        "task_id": task_id,
        "replayed": True,
        "side_effects_reissued": False,
        "timeline": timeline,
    }
