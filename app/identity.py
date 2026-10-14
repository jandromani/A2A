"""Governed registry for OIDC service-account agents."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.event_bus import write_audit
from app.models import AgentIdentity
from app.policy import authorize
from app.schemas import AgentIdentityCreate, Principal


def serialize_identity(item: AgentIdentity) -> dict:
    return {
        "client_id": item.id,
        "display_name": item.display_name,
        "status": item.status,
        "roles": item.roles_json,
        "scopes": item.scopes_json,
        "created_by": item.created_by,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "expires_at": item.expires_at.isoformat() if item.expires_at else None,
        "revoked_at": item.revoked_at.isoformat() if item.revoked_at else None,
    }


def _require_admin(config: Settings, principal: Principal) -> None:
    allowed, reason = authorize(principal, "identity.manage", {}, config)
    if not allowed:
        raise HTTPException(status_code=403, detail=reason)


def list_agent_identities(session: Session, config: Settings, principal: Principal) -> list[dict]:
    _require_admin(config, principal)
    records = session.scalars(select(AgentIdentity).order_by(AgentIdentity.created_at.desc())).all()
    return [serialize_identity(item) for item in records]


def register_agent_identity(
    session: Session,
    config: Settings,
    body: AgentIdentityCreate,
    principal: Principal,
) -> dict:
    _require_admin(config, principal)
    if session.get(AgentIdentity, body.client_id):
        raise HTTPException(status_code=409, detail="Agent client_id is already registered.")
    if body.expires_at and body.expires_at.tzinfo is None:
        raise HTTPException(status_code=422, detail="expires_at must include a timezone.")
    item = AgentIdentity(
        id=body.client_id,
        display_name=body.display_name,
        status="provisioned",
        roles_json=body.roles,
        scopes_json=body.scopes,
        created_by=principal.subject,
        expires_at=body.expires_at,
    )
    session.add(item)
    session.flush()
    write_audit(
        session,
        principal.subject,
        "agent_identity.register",
        "agent_identity",
        item.id,
        details={"roles": item.roles_json, "scopes": item.scopes_json},
    )
    session.commit()
    return serialize_identity(item)


def transition_agent_identity(
    session: Session,
    config: Settings,
    client_id: str,
    status: str,
    principal: Principal,
) -> dict:
    _require_admin(config, principal)
    item = session.get(AgentIdentity, client_id)
    if not item:
        raise HTTPException(status_code=404, detail="Agent identity not found.")
    if item.status == "revoked":
        raise HTTPException(status_code=409, detail="A revoked identity is terminal.")
    if status == "active" and item.status not in {"provisioned", "suspended"}:
        raise HTTPException(
            status_code=409, detail="Only provisioned or suspended agents activate."
        )
    if status == "suspended" and item.status != "active":
        raise HTTPException(status_code=409, detail="Only active agents can be suspended.")
    item.status = status
    item.updated_at = datetime.now(UTC)
    if status == "revoked":
        item.revoked_at = item.updated_at
    write_audit(
        session,
        principal.subject,
        f"agent_identity.{status}",
        "agent_identity",
        item.id,
        decision=status,
    )
    session.commit()
    return serialize_identity(item)
