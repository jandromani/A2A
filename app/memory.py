"""Durable, actor-scoped conversation memory and workflow checkpoints."""

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import ConversationMemory, TaskRecord, WorkflowCheckpoint
from app.schemas import Principal


def _thread_key(actor: str, context_id: str) -> str:
    digest = hashlib.sha256(f"{actor}\0{context_id}".encode()).hexdigest()
    return digest


def read_conversation_context(
    session: Session,
    config: Settings,
    principal: Principal,
    context_id: str | None,
) -> list[dict[str, str]]:
    if not context_id:
        return []
    key = _thread_key(principal.subject, context_id)
    memory = session.get(ConversationMemory, key)
    if not memory:
        return []
    now = datetime.now(UTC)
    expiry = memory.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    if expiry <= now:
        session.delete(memory)
        session.flush()
        return []
    granted = set(principal.scopes)
    previous = set(memory.scope_snapshot or [])
    if "*" not in granted and not previous.issubset(granted):
        return []
    turns = max(1, config.conversation_memory_turns) * 2
    return [
        {"role": str(item.get("role", "user")), "content": str(item.get("content", ""))[:4_000]}
        for item in memory.messages_json[-turns:]
        if isinstance(item, dict) and item.get("role") in {"user", "assistant"}
    ]


def save_conversation_exchange(
    session: Session,
    config: Settings,
    principal: Principal,
    context_id: str | None,
    task: TaskRecord,
) -> None:
    if not context_id:
        return
    key = _thread_key(principal.subject, context_id)
    memory = session.get(ConversationMemory, key)
    if not memory:
        memory = ConversationMemory(
            thread_key=key,
            context_id=context_id,
            actor=principal.subject,
            scope_snapshot=sorted(principal.scopes),
            messages_json=[],
            checkpoint_json={},
        )
        session.add(memory)
    result = task.result_json or {}
    assistant = (
        result.get("answer")
        or result.get("summary")
        or result.get("draft")
        or result.get("message")
        or "La tarea terminó sin una respuesta textual."
    )
    if isinstance(assistant, (dict, list)):
        assistant = str(assistant)
    messages = list(memory.messages_json or [])
    user_found = assistant_found = False
    for item in messages:
        if item.get("task_id") != task.id:
            continue
        if item.get("role") == "user":
            item["content"] = task.request_text[:4_000]
            user_found = True
        elif item.get("role") == "assistant":
            item["content"] = str(assistant)[:4_000]
            assistant_found = True
    if not user_found:
        messages.append({"role": "user", "content": task.request_text[:4_000], "task_id": task.id})
    if not assistant_found:
        messages.append(
            {"role": "assistant", "content": str(assistant)[:4_000], "task_id": task.id}
        )
    memory.messages_json = messages[-max(2, config.conversation_memory_turns * 2) :]
    memory.scope_snapshot = sorted(principal.scopes)
    memory.updated_at = datetime.now(UTC)
    memory.expires_at = memory.updated_at + timedelta(days=config.conversation_memory_ttl_days)
    memory.checkpoint_json = {
        "task_id": task.id,
        "status": task.status,
        "scenario": task.scenario,
        "updated_at": memory.updated_at.isoformat(),
    }
    session.flush()


def save_workflow_checkpoint(
    session: Session,
    task_id: str,
    step: str,
    state: dict[str, Any],
) -> WorkflowCheckpoint:
    safe_state = {
        key: value
        for key, value in state.items()
        if key
        in {
            "authorized",
            "denial",
            "final_status",
            "checks",
            "result",
            "approval_id",
            "status",
            "completed_steps",
            "outputs",
            "workflow_id",
        }
    }
    checkpoint = WorkflowCheckpoint(task_id=task_id, step=step, state_json=safe_state)
    session.add(checkpoint)
    session.flush()
    return checkpoint


def conversation_for_actor(
    session: Session,
    principal: Principal,
    context_id: str,
) -> dict[str, Any] | None:
    memory = session.get(ConversationMemory, _thread_key(principal.subject, context_id))
    if not memory:
        return None
    expires_at = memory.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
        session.delete(memory)
        session.flush()
        return None
    if "admin" not in principal.roles and memory.actor != principal.subject:
        return None
    return {
        "context_id": memory.context_id,
        "messages": memory.messages_json,
        "checkpoint": memory.checkpoint_json,
        "updated_at": memory.updated_at.isoformat(),
        "expires_at": memory.expires_at.isoformat(),
    }


def delete_conversation(
    session: Session,
    principal: Principal,
    context_id: str,
) -> bool:
    key = _thread_key(principal.subject, context_id)
    memory = session.get(ConversationMemory, key)
    if not memory:
        return False
    session.delete(memory)
    session.flush()
    return True


def purge_expired_conversations(session: Session) -> int:
    result = session.execute(
        delete(ConversationMemory).where(ConversationMemory.expires_at <= datetime.now(UTC))
    )
    return int(result.rowcount or 0)
