"""Durable SQL-outbox publisher and idempotent event consumer."""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import (
    ApprovalRecord,
    DataSourceRecord,
    EventReceipt,
    OutboxEvent,
    TaskRecord,
)
from app.schemas import Principal
from app.secrets import resolve_secret

logger = logging.getLogger(__name__)
STREAM_NAME = "agentic:events"


def _principal(payload: dict[str, Any]) -> Principal:
    return Principal(
        subject=str(payload.get("actor") or "system:event-worker"),
        roles=frozenset(str(role) for role in payload.get("roles", [])),
        scopes=frozenset(str(scope) for scope in payload.get("scopes", [])),
    )


def _read_pending_ids(factory: sessionmaker[Session], *, published: bool = False) -> list[str]:
    with factory() as session:
        statuses = ["pending", "published"] if published else ["pending"]
        rows = session.scalars(
            select(OutboxEvent.id)
            .where(
                OutboxEvent.status.in_(statuses),
                OutboxEvent.next_attempt_at <= datetime.now(UTC),
            )
            .order_by(OutboxEvent.created_at)
            .limit(100)
        ).all()
        return list(rows)


async def _publish_pending(factory: sessionmaker[Session], redis: Redis) -> None:
    for event_id in await asyncio.to_thread(_read_pending_ids, factory):
        with factory() as session:
            event = session.get(OutboxEvent, event_id)
            if not event or event.status != "pending":
                continue
            value = {
                "outbox_id": event.id,
                "topic": event.topic,
                "payload": json.dumps(event.payload_json or {}, ensure_ascii=False, default=str),
            }
        await redis.xadd(STREAM_NAME, value, maxlen=100_000, approximate=True)
        await asyncio.to_thread(_mark_published, factory, event_id)


def _mark_published(factory: sessionmaker[Session], event_id: str) -> None:
    with factory() as session:
        event = session.get(OutboxEvent, event_id)
        if event and event.status == "pending":
            event.status = "published"
            event.published_at = datetime.now(UTC)
            event.attempts += 1
            session.commit()


def _event_is_due(factory: sessionmaker[Session], event_id: str) -> bool:
    with factory() as session:
        event = session.get(OutboxEvent, event_id)
        if not event or event.status in {"processed", "failed"}:
            return True
        due_at = event.next_attempt_at
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=UTC)
        return due_at <= datetime.now(UTC)


def _set_retry(
    factory: sessionmaker[Session], config: Settings, event_id: str, exc: Exception
) -> None:
    from app.event_bus import publish_event, write_audit

    with factory() as session:
        event = session.get(OutboxEvent, event_id)
        if event:
            event.attempts += 1
            event.last_error = str(exc)[:500]
            if event.attempts >= 10:
                event.status = "failed"
                if event.topic in {"integration.action.approved", "workflow.resume.approved"}:
                    approval_id = str((event.payload_json or {}).get("approval_id") or "")
                    approval = session.get(ApprovalRecord, approval_id)
                    if approval and approval.status == "approved_queued":
                        approval.status = "failed"
                        task = session.get(TaskRecord, approval.task_id)
                        if task:
                            task.status = "failed"
                            task.result_json = {
                                **(task.result_json or {}),
                                "external_effect": "failed after retries",
                                "error": str(exc)[:300],
                            }
                            actor = approval.reviewed_by or "system:event-worker"
                            write_audit(
                                session,
                                actor,
                                "approved_action.failed",
                                "approval",
                                approval.id,
                                decision="failed",
                                details={"task_id": task.id, "error": str(exc)[:300]},
                            )
                            failed_topic = (
                                "integration.action.failed"
                                if event.topic == "integration.action.approved"
                                else "workflow.resume.failed"
                            )
                            publish_event(
                                session,
                                config,
                                task.id,
                                failed_topic,
                                "Approved action could not be completed after retries.",
                                {"approval_id": approval.id},
                            )
            else:
                delay = min(300, 2 ** min(event.attempts, 8))
                event.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
            session.commit()


def _complete_event(
    factory: sessionmaker[Session],
    config: Settings,
    event_id: str,
) -> dict[str, Any]:
    """Apply one event. A receipt and the resulting changes commit together."""
    with factory() as session:
        event = session.get(OutboxEvent, event_id)
        if not event:
            return {"missing": True}
        if event.status == "failed":
            return {"failed": True}
        receipt = session.get(EventReceipt, event_id)
        if receipt:
            if event.status != "processed":
                event.status = "processed"
                event.processed_at = receipt.processed_at
                session.commit()
            return receipt.result_json

        payload = dict(event.payload_json or {})
        result: dict[str, Any] = {"handled": True, "topic": event.topic}
        try:
            if event.topic == "task.queued":
                task_id = str(payload.get("task_id") or event.aggregate_id or "")
                task = session.get(TaskRecord, task_id)
                principal = _principal(payload)
                if task and task.status == "queued":
                    if task.actor != principal.subject:
                        raise ValueError("Queued task actor does not match its event.")
                    task.status = "running"
                    session.commit()
                    task = session.get(TaskRecord, task_id)
                    if task.scenario == "workflow":
                        from app.custom_workflows import execute_workflow_task

                        execute_workflow_task(session, config, task, principal)
                    else:
                        from app.memory import save_conversation_exchange
                        from app.workflows.engine import run_workflow

                        task = run_workflow(session, config, task, principal)
                        save_conversation_exchange(
                            session, config, principal, task.context_id, task
                        )
                        session.commit()
                    result.update({"task_id": task_id, "status": task.status})
                else:
                    result.update({"task_id": task_id, "skipped": True})

            elif event.topic == "data.source.sync.requested":
                from app.data_sources import sync_data_source

                source_id = str(payload.get("source_id") or event.aggregate_id or "")
                result.update(sync_data_source(session, config, source_id, _principal(payload)))

            elif event.topic == "data.source.records_received":
                from app.data_sources import _upsert_records
                from app.event_bus import write_audit
                from app.policy import authorize

                source_id = str(payload.get("source_id") or event.aggregate_id or "")
                source = session.get(DataSourceRecord, source_id)
                principal = _principal(payload)
                if not source or not source.enabled:
                    raise ValueError("Data source was removed or disabled before ingestion.")
                allowed, reason = authorize(
                    principal,
                    "data.source.write",
                    {"scope": source.scope, "kind": source.kind},
                    config,
                )
                if not allowed:
                    raise PermissionError(reason)
                records = payload.get("records")
                if not isinstance(records, list) or len(records) > 500:
                    raise ValueError("Pipeline event contains an invalid record batch.")
                count = _upsert_records(session, config, source, records)
                source.last_sync_at = datetime.now(UTC)
                source.last_sync_status = "streaming"
                source.last_sync_error = None
                write_audit(
                    session,
                    principal.subject,
                    "data_source.event_batch.processed",
                    "data_source",
                    source_id,
                    details={"records": count, "event_id": payload.get("event_id")},
                )
                result.update({"source_id": source_id, "records_received": count})

            elif event.topic == "integration.action.approved":
                from app.integrations import execute_approved_integration

                approval_id = str(payload.get("approval_id") or "")
                approval = session.get(ApprovalRecord, approval_id)
                if not approval:
                    raise ValueError("Approved integration action no longer exists.")
                if approval.status == "approved_queued":
                    result.update(execute_approved_integration(session, config, approval))
                else:
                    result.update({"approval_id": approval_id, "skipped": True})

            elif event.topic == "workflow.resume.approved":
                from app.custom_workflows import resume_approved_workflow

                approval_id = str(payload.get("approval_id") or "")
                approval = session.get(ApprovalRecord, approval_id)
                if not approval:
                    raise ValueError("Workflow approval no longer exists.")
                if approval.status == "approved_queued":
                    task = session.get(TaskRecord, approval.task_id)
                    if not task:
                        raise ValueError("Workflow task no longer exists.")
                    approval.status = "approved"
                    actor = Principal(
                        subject=task.actor,
                        roles=frozenset(task.payload_json.get("actor_roles", [])),
                        scopes=frozenset(task.payload_json.get("actor_scopes", [])),
                    )
                    result["task"] = resume_approved_workflow(
                        session, config, task, approval, actor
                    )
                else:
                    result.update({"approval_id": approval_id, "skipped": True})

            # Other event types are durable and observable in the stream; they
            # require no background side effect in this platform version.
            event.status = "processed"
            event.processed_at = datetime.now(UTC)
            event.last_error = None
            session.add(
                EventReceipt(
                    event_id=event.id,
                    consumer=config.event_consumer_group,
                    result_json=result,
                )
            )
            session.commit()
            return result
        except Exception as exc:
            session.rollback()
            _set_retry(factory, config, event_id, exc)
            raise


def _due_sources(factory: sessionmaker[Session], interval: int) -> list[str]:
    cutoff = datetime.now(UTC) - timedelta(seconds=interval)
    with factory() as session:
        sources = session.scalars(
            select(DataSourceRecord).where(DataSourceRecord.enabled.is_(True))
        ).all()
        return [
            source.id
            for source in sources
            if source.last_sync_at is None
            or (
                source.last_sync_at.replace(tzinfo=UTC)
                if source.last_sync_at.tzinfo is None
                else source.last_sync_at
            )
            <= cutoff
        ]


def _enqueue_scheduled_syncs(
    factory: sessionmaker[Session], config: Settings, source_ids: list[str]
) -> None:
    from app.event_bus import publish_event

    with factory() as session:
        existing = set(
            session.scalars(
                select(OutboxEvent.aggregate_id).where(
                    OutboxEvent.topic == "data.source.sync.requested",
                    OutboxEvent.status.in_(["pending", "published"]),
                )
            ).all()
        )
        for source_id in source_ids:
            if source_id in existing:
                continue
            publish_event(
                session,
                config,
                aggregate_id=source_id,
                event_type="data.source.sync.requested",
                message="Scheduled data-source sync.",
                payload={
                    "source_id": source_id,
                    "actor": "system:data-source-scheduler",
                    "roles": ["admin", "operator"],
                    "scopes": ["*"],
                },
            )
        session.commit()


async def run_event_worker(factory: sessionmaker[Session], config: Settings) -> None:
    """Consume committed outbox rows through Valkey Streams or SQL polling."""
    redis: Redis | None = None
    redis_url = resolve_secret(config, config.redis_url)
    if config.environment == "production" and redis_url and not redis_url.startswith("rediss://"):
        raise RuntimeError("Production REDIS_URL must resolve to a TLS Valkey/Redis URL.")
    if redis_url:
        try:
            redis = Redis.from_url(redis_url, decode_responses=True)
            await redis.ping()
            try:
                await redis.xgroup_create(
                    STREAM_NAME, config.event_consumer_group, id="0", mkstream=True
                )
            except ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
        except Exception:
            logger.exception("Valkey Streams unavailable; event worker is using SQL polling.")
            if redis:
                await redis.aclose()
            redis = None

    next_source_scan = datetime.now(UTC)
    try:
        while True:
            if config.data_source_sync_interval_seconds and datetime.now(UTC) >= next_source_scan:
                await asyncio.to_thread(
                    _enqueue_scheduled_syncs,
                    factory,
                    config,
                    await asyncio.to_thread(
                        _due_sources, factory, config.data_source_sync_interval_seconds
                    ),
                )
                next_source_scan = datetime.now(UTC) + timedelta(
                    seconds=min(config.data_source_sync_interval_seconds, 60)
                )

            if redis:
                try:
                    await _publish_pending(factory, redis)
                    claimed = await redis.xautoclaim(
                        STREAM_NAME,
                        config.event_consumer_group,
                        config.event_consumer_name,
                        min_idle_time=30_000,
                        start_id="0-0",
                        count=20,
                    )
                    pending_messages = claimed[1] if len(claimed) > 1 else []
                    streams = await redis.xreadgroup(
                        config.event_consumer_group,
                        config.event_consumer_name,
                        {STREAM_NAME: ">"},
                        count=20,
                        block=100,
                    )
                    messages_to_process = list(pending_messages)
                    messages_to_process.extend(
                        message
                        for _stream, stream_messages in streams
                        for message in stream_messages
                    )
                    for message_id, fields in messages_to_process:
                        event_id = fields.get("outbox_id")
                        if not event_id:
                            await redis.xack(STREAM_NAME, config.event_consumer_group, message_id)
                            continue
                        if not await asyncio.to_thread(_event_is_due, factory, event_id):
                            continue
                        try:
                            await asyncio.to_thread(_complete_event, factory, config, event_id)
                        except Exception:
                            logger.exception("Event %s failed and will be retried.", event_id)
                            continue
                        await redis.xack(STREAM_NAME, config.event_consumer_group, message_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Valkey consumer failed; processing due events from SQL.")
                    await redis.aclose()
                    redis = None

            if not redis:
                event_ids = await asyncio.to_thread(_read_pending_ids, factory, published=True)
                for event_id in event_ids:
                    try:
                        await asyncio.to_thread(_complete_event, factory, config, event_id)
                    except Exception:
                        logger.exception(
                            "SQL outbox event %s failed and will be retried.", event_id
                        )

            await asyncio.sleep(config.event_poll_interval_seconds)
    finally:
        if redis:
            await redis.aclose()
