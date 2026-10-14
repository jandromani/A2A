"""Agora Protocol 1.0 single and multi-round adapter over governed platform tasks."""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import Settings
from app.memory import conversation_for_actor
from app.schemas import Principal, TaskCreate
from app.services import create_task


def _failure(message: str) -> dict[str, Any]:
    return {"status": "failure", "error": message}


def _task_input(body: str | dict[str, Any], context_id: str | None) -> TaskCreate:
    values = body if isinstance(body, dict) else {"message": body}
    message = values.get("message")
    if not isinstance(message, str):
        message = json.dumps(values, ensure_ascii=False, default=str)
    return TaskCreate(
        scenario=values.get("scenario", "knowledge"),
        message=message,
        artifacts=values.get("artifacts", []),
        issue_key=values.get("issue_key"),
        context_id=context_id,
    )


def handle_agora_request(
    payload: dict[str, Any],
    session: Session,
    config: Settings,
    principal: Principal,
    *,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Handle an Agora JSON exchange; unsupported protocol hashes fail at protocol level."""
    if not isinstance(payload, dict) or "body" not in payload:
        raise HTTPException(status_code=400, detail="Agora request must include a body field.")
    body = payload["body"]
    if not isinstance(body, (str, dict)):
        raise HTTPException(status_code=400, detail="Agora body must be a string or JSON object.")
    if (
        len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
        > config.max_artifact_bytes
    ):
        raise HTTPException(
            status_code=413, detail="Agora request exceeds the configured size limit."
        )
    protocol_hash = payload.get("protocolHash")
    if protocol_hash is not None:
        return _failure("Unsupported protocol")

    multiround = payload.get("multiround", False)
    if not isinstance(multiround, bool):
        return _failure("Invalid multiround value")
    if conversation_id is not None:
        if len(conversation_id) != 32 or any(c not in "0123456789abcdef" for c in conversation_id):
            return _failure("Conversation unavailable")
        context_id = f"agora-{conversation_id}"
        context = conversation_for_actor(session, principal, context_id)
        if context is None:
            return _failure("Conversation expired or unavailable")
    elif multiround:
        conversation_id = uuid4().hex
        context_id = f"agora-{conversation_id}"
    else:
        context_id = None

    try:
        task_request = _task_input(body, context_id)
    except (ValidationError, TypeError, ValueError):
        return _failure("Invalid task request")
    try:
        task = create_task(session, config, task_request, principal)
    except HTTPException as exc:
        return _failure(str(exc.detail)[:300])

    response: dict[str, Any] = {
        "status": "success",
        "body": {
            "task_id": task.id,
            "task_status": task.status,
            "result": task.result_json or {},
        },
    }
    if conversation_id is not None:
        context = conversation_for_actor(session, principal, context_id)
        expiry = (
            datetime.fromisoformat(context["expires_at"].replace("Z", "+00:00"))
            if context
            else datetime.now(UTC)
        )
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        response["conversationId"] = conversation_id
        response["conversationExpires"] = int(expiry.timestamp())
    return response
