from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit_hash
from app.config import Settings
from app.models import AuditChainState, AuditRecord, OutboxEvent, TaskEvent, new_id, utcnow


def publish_event(
    session: Session,
    config: Settings,
    task_id: str | None = None,
    event_type: str = "platform.event",
    message: str = "",
    payload: dict[str, Any] | None = None,
    *,
    aggregate_id: str | None = None,
    external_id: str | None = None,
) -> TaskEvent:
    body = payload or {}
    event = TaskEvent(
        task_id=task_id,
        event_type=event_type,
        message=message,
        payload_json=body,
    )
    session.add(event)
    session.flush()
    session.add(
        OutboxEvent(
            task_event_id=event.id,
            external_id=external_id,
            topic=event_type,
            aggregate_id=aggregate_id or task_id,
            payload_json={
                **body,
                "task_id": task_id,
                "aggregate_id": aggregate_id or task_id,
                "event_type": event_type,
                "message": message,
            },
        )
    )
    session.flush()
    # The SQL outbox is committed with the business event. A worker publishes
    # and consumes it after commit, so a broker outage cannot lose the event.
    del config
    return event


def write_audit(
    session: Session,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: str | None,
    decision: str = "allowed",
    details: dict[str, Any] | None = None,
) -> AuditRecord:
    state = session.scalar(
        select(AuditChainState).where(AuditChainState.id == "global").with_for_update()
    )
    if state is None:
        state = AuditChainState(id="global", last_sequence=0, head_hash="")
        session.add(state)
        session.flush()
    sequence = state.last_sequence + 1
    previous_hash = state.head_hash
    record = AuditRecord(
        id=new_id(),
        sequence=sequence,
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        decision=decision,
        details_json=details or {},
        previous_hash=previous_hash,
        created_at=utcnow(),
    )
    record.entry_hash = audit_hash(
        {
            "sequence": sequence,
            "id": record.id,
            "actor": record.actor,
            "action": record.action,
            "resource_type": record.resource_type,
            "resource_id": record.resource_id,
            "decision": record.decision,
            "details_json": record.details_json,
            "created_at": record.created_at,
        },
        previous_hash,
    )
    state.last_sequence = sequence
    state.head_hash = record.entry_hash
    state.updated_at = record.created_at
    session.add(record)
    session.flush()
    return record
