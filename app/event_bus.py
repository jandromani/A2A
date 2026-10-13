import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AuditRecord, TaskEvent

logger = logging.getLogger(__name__)


def publish_event(
    session: Session,
    config: Settings,
    task_id: str,
    event_type: str,
    message: str,
    payload: dict[str, Any] | None = None,
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
    if config.redis_url:
        try:
            from redis import Redis

            client = Redis.from_url(config.redis_url, decode_responses=True, socket_timeout=1.5)
            client.xadd(
                "agentic:task-events",
                {
                    "task_id": task_id,
                    "type": event_type,
                    "message": message,
                    "payload": json.dumps(body, ensure_ascii=False),
                },
                maxlen=10_000,
                approximate=True,
            )
            client.close()
        except Exception as exc:  # Database event is already durable.
            logger.warning("Valkey publish failed; event retained in SQL: %s", exc)
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
    record = AuditRecord(
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        decision=decision,
        details_json=details or {},
    )
    session.add(record)
    session.flush()
    return record
