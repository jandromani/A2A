from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

try:
    from pgvector.sqlalchemy import Vector

    EMBEDDING_TYPE = JSON().with_variant(Vector(384), "postgresql")
except ImportError:  # pragma: no cover - pgvector is an application dependency
    EMBEDDING_TYPE = JSON()


def new_id() -> str:
    return uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


class Capability(Base):
    __tablename__ = "capabilities"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(180))
    description: Mapped[str] = mapped_column(Text)
    version: Mapped[str] = mapped_column(String(40), default="1.0.0")
    status: Mapped[str] = mapped_column(String(32), default="active")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    endpoint: Mapped[str | None] = mapped_column(String(500), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(240))
    content: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String(80), index=True)
    source_uri: Mapped[str] = mapped_column(String(500), default="manual")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    embedding: Mapped[list[float] | None] = mapped_column(EMBEDDING_TYPE, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TaskRecord(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    scenario: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(240))
    request_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default="running", index=True)
    actor: Mapped[str] = mapped_column(String(200), index=True)
    context_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TaskEvent(Base):
    __tablename__ = "task_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    message: Mapped[str] = mapped_column(String(500))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class ApprovalRecord(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    requested_by: Mapped[str] = mapped_column(String(200))
    reviewed_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    decision_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EvaluationRecord(Base):
    __tablename__ = "evaluations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    suite: Mapped[str] = mapped_column(String(100))
    passed: Mapped[bool] = mapped_column(Boolean, index=True)
    score: Mapped[int] = mapped_column(Integer)
    checks_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditRecord(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    actor: Mapped[str] = mapped_column(String(200), index=True)
    action: Mapped[str] = mapped_column(String(120), index=True)
    resource_type: Mapped[str] = mapped_column(String(80), index=True)
    resource_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    decision: Mapped[str] = mapped_column(String(40), default="allowed")
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class ConnectorRecord(Base):
    __tablename__ = "connectors"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    name: Mapped[str] = mapped_column(String(180))
    description: Mapped[str] = mapped_column(Text, default="")
    endpoint: Mapped[str] = mapped_column(String(1000))
    auth_env: Mapped[str | None] = mapped_column(String(120), nullable=True)
    protocol_version: Mapped[str] = mapped_column(String(40), default="2026-07-28")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_invoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
