import re
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.embeddings import embedding_for
from app.event_bus import publish_event, write_audit
from app.jira import JiraConnector, JiraConnectorError
from app.models import (
    ApprovalRecord,
    AuditRecord,
    Capability,
    EvaluationRecord,
    KnowledgeDocument,
    TaskEvent,
    TaskRecord,
)
from app.policy import authorize
from app.schemas import (
    ApprovalDecision,
    CapabilityCreate,
    KnowledgeCreate,
    Principal,
    TaskCreate,
)
from app.workflows.engine import SCENARIO_TITLES, run_workflow

ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9]+-\d{1,10}$")


def serialize_task(task: TaskRecord) -> dict[str, Any]:
    return {
        "id": task.id,
        "scenario": task.scenario,
        "title": task.title,
        "request": task.request_text,
        "status": task.status,
        "actor": task.actor,
        "context_id": task.context_id,
        "result": task.result_json,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


def serialize_event(event: TaskEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "type": event.event_type,
        "message": event.message,
        "payload": event.payload_json,
        "created_at": event.created_at.isoformat(),
    }


def serialize_approval(approval: ApprovalRecord, task: TaskRecord | None = None) -> dict[str, Any]:
    return {
        "id": approval.id,
        "task_id": approval.task_id,
        "task_title": task.title if task else None,
        "task_status": task.status if task else None,
        "status": approval.status,
        "requested_by": approval.requested_by,
        "reviewed_by": approval.reviewed_by,
        "comment": approval.decision_comment,
        "draft": approval.decision_json.get("draft"),
        "source": approval.decision_json.get("source"),
        "issue_key": approval.decision_json.get("issue_key"),
        "created_at": approval.created_at.isoformat(),
        "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
    }


def _require_allowed(
    config: Settings,
    principal: Principal,
    action: str,
    resource: dict[str, Any] | None = None,
) -> None:
    allowed, reason = authorize(principal, action, resource or {}, config)
    if not allowed:
        raise HTTPException(status_code=403, detail=reason)


def create_task(
    session: Session,
    config: Settings,
    request: TaskCreate,
    principal: Principal,
) -> TaskRecord:
    _require_allowed(config, principal, "task.create", {"scenario": request.scenario})
    if request.scenario == "incident" and request.issue_key:
        if not ISSUE_KEY.fullmatch(request.issue_key):
            raise HTTPException(status_code=422, detail="Jira issue key is invalid.")
        if not JiraConnector(config).configured:
            raise HTTPException(
                status_code=422,
                detail="An issue key was provided, but Jira is not configured. "
                "Set JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN.",
            )
    task = TaskRecord(
        scenario=request.scenario,
        title=SCENARIO_TITLES[request.scenario],
        request_text=request.message,
        status="running",
        actor=principal.subject,
        context_id=request.context_id,
        payload_json=request.model_dump(),
    )
    session.add(task)
    session.flush()
    return run_workflow(session, config, task, principal)


def list_tasks(
    session: Session,
    principal: Principal,
    limit: int = 30,
) -> list[dict[str, Any]]:
    query = select(TaskRecord).order_by(desc(TaskRecord.created_at)).limit(limit)
    if not principal.roles.intersection({"admin", "operator", "approver"}):
        query = (
            select(TaskRecord)
            .where(TaskRecord.actor == principal.subject)
            .order_by(desc(TaskRecord.created_at))
            .limit(limit)
        )
    return [serialize_task(task) for task in session.scalars(query).all()]


def get_task(session: Session, task_id: str, principal: Principal) -> TaskRecord:
    task = session.get(TaskRecord, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    if task.actor != principal.subject and not principal.roles.intersection(
        {"admin", "operator", "approver"}
    ):
        raise HTTPException(status_code=404, detail="Task not found.")
    return task


def task_events(
    session: Session,
    task_id: str,
    principal: Principal,
) -> list[dict[str, Any]]:
    get_task(session, task_id, principal)
    events = session.scalars(
        select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.created_at.asc())
    ).all()
    return [serialize_event(event) for event in events]


def cancel_task(
    session: Session,
    config: Settings,
    task_id: str,
    principal: Principal,
) -> dict[str, Any]:
    _require_allowed(config, principal, "task.cancel")
    task = session.get(TaskRecord, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    if task.status not in {"running", "awaiting_approval"}:
        raise HTTPException(status_code=409, detail=f"Task is already {task.status}.")
    task.status = "cancelled"
    task.updated_at = datetime.now(UTC)
    for approval in session.scalars(
        select(ApprovalRecord).where(
            ApprovalRecord.task_id == task_id,
            ApprovalRecord.status == "pending",
        )
    ).all():
        approval.status = "cancelled"
        approval.reviewed_by = principal.subject
        approval.decision_comment = "Task cancelled."
        approval.decided_at = datetime.now(UTC)
    publish_event(session, config, task_id, "task.cancelled", "Task cancelled.", {})
    write_audit(session, principal.subject, "task.cancel", "task", task_id)
    session.commit()
    return serialize_task(task)


def list_approvals(session: Session) -> list[dict[str, Any]]:
    approvals = session.scalars(
        select(ApprovalRecord)
        .where(ApprovalRecord.status == "pending")
        .order_by(ApprovalRecord.created_at.asc())
    ).all()
    return [
        serialize_approval(approval, session.get(TaskRecord, approval.task_id))
        for approval in approvals
    ]


def decide_approval(
    session: Session,
    config: Settings,
    approval_id: str,
    decision: ApprovalDecision,
    principal: Principal,
) -> dict[str, Any]:
    _require_allowed(config, principal, "approval.decide")
    approval = session.get(ApprovalRecord, approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found.")
    if approval.status != "pending":
        raise HTTPException(status_code=409, detail="This approval has already been decided.")
    task = session.get(TaskRecord, approval.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")

    side_effect = "local approval record only"
    if decision.decision == "approve":
        issue_key = approval.decision_json.get("issue_key")
        if issue_key:
            try:
                JiraConnector(config).add_approved_comment(
                    issue_key, approval.decision_json.get("draft") or decision.comment
                )
            except JiraConnectorError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            side_effect = "Jira comment written after approval"
        approval.status = "approved"
        task.status = "completed"
        task.result_json = {
            **(task.result_json or {}),
            "approval": "approved",
            "approved_by": principal.subject,
            "external_effect": side_effect,
        }
    else:
        approval.status = "rejected"
        task.status = "rejected"
        task.result_json = {
            **(task.result_json or {}),
            "approval": "rejected",
            "rejected_by": principal.subject,
            "external_effect": "none",
        }
    approval.reviewed_by = principal.subject
    approval.decision_comment = decision.comment
    approval.decided_at = datetime.now(UTC)
    task.updated_at = datetime.now(UTC)
    publish_event(
        session,
        config,
        task.id,
        f"approval.{approval.status}",
        f"Approval {approval.status} by {principal.subject}.",
        {"approval_id": approval.id, "external_effect": side_effect},
    )
    write_audit(
        session,
        principal.subject,
        f"approval.{approval.status}",
        "approval",
        approval.id,
        details={"task_id": task.id, "external_effect": side_effect},
    )
    session.commit()
    return {
        "approval": serialize_approval(approval, task),
        "task": serialize_task(task),
        "external_effect": side_effect,
    }


def list_knowledge(
    session: Session,
    principal: Principal,
    config: Settings,
) -> list[dict[str, Any]]:
    _require_allowed(config, principal, "knowledge.read")
    docs = session.scalars(
        select(KnowledgeDocument).where(KnowledgeDocument.enabled.is_(True))
    ).all()
    if "admin" not in principal.roles and "*" not in principal.scopes:
        docs = [doc for doc in docs if doc.scope in principal.scopes]
    return [
        {
            "id": doc.id,
            "title": doc.title,
            "scope": doc.scope,
            "source_uri": doc.source_uri,
            "tags": doc.tags,
            "content": doc.content,
            "updated_at": doc.updated_at.isoformat(),
        }
        for doc in docs
    ]


def add_knowledge(
    session: Session,
    config: Settings,
    data: KnowledgeCreate,
    principal: Principal,
) -> dict[str, Any]:
    _require_allowed(config, principal, "knowledge.write", {"scope": data.scope})
    doc = KnowledgeDocument(
        title=data.title,
        content=data.content,
        scope=data.scope,
        source_uri=data.source_uri,
        tags=data.tags,
        embedding=embedding_for(f"{data.title} {data.content}", config),
    )
    session.add(doc)
    session.flush()
    write_audit(
        session,
        principal.subject,
        "knowledge.create",
        "knowledge_document",
        doc.id,
        details={"scope": doc.scope, "source_uri": doc.source_uri},
    )
    session.commit()
    return {
        "id": doc.id,
        "title": doc.title,
        "scope": doc.scope,
        "source_uri": doc.source_uri,
        "tags": doc.tags,
    }


def delete_knowledge(
    session: Session,
    config: Settings,
    document_id: str,
    principal: Principal,
) -> None:
    _require_allowed(config, principal, "knowledge.write")
    doc = session.get(KnowledgeDocument, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Knowledge document not found.")
    doc.enabled = False
    write_audit(
        session,
        principal.subject,
        "knowledge.disable",
        "knowledge_document",
        doc.id,
        details={"scope": doc.scope},
    )
    session.commit()


def list_capabilities(session: Session, kind: str | None = None) -> list[dict[str, Any]]:
    query = (
        select(Capability)
        .where(Capability.status == "active")
        .order_by(Capability.kind, Capability.name)
    )
    if kind:
        query = query.where(Capability.kind == kind)
    return [
        {
            "id": item.id,
            "kind": item.kind,
            "name": item.name,
            "description": item.description,
            "version": item.version,
            "status": item.status,
            "tags": item.tags,
            "endpoint": item.endpoint,
            "input_schema": item.input_schema,
            "output_schema": item.output_schema,
        }
        for item in session.scalars(query).all()
    ]


def add_capability(
    session: Session,
    config: Settings,
    body: CapabilityCreate,
    principal: Principal,
) -> dict[str, Any]:
    _require_allowed(config, principal, "catalog.manage")
    if session.get(Capability, body.id):
        raise HTTPException(status_code=409, detail="Capability id already exists.")
    item = Capability(**body.model_dump())
    session.add(item)
    write_audit(
        session,
        principal.subject,
        "catalog.register",
        "capability",
        item.id,
        details={"kind": item.kind, "version": item.version},
    )
    session.commit()
    return {
        "id": item.id,
        "kind": item.kind,
        "name": item.name,
        "description": item.description,
        "version": item.version,
        "status": item.status,
        "tags": item.tags,
    }


def disable_capability(
    session: Session,
    config: Settings,
    capability_id: str,
    principal: Principal,
) -> None:
    _require_allowed(config, principal, "catalog.manage")
    item = session.get(Capability, capability_id)
    if not item:
        raise HTTPException(status_code=404, detail="Capability not found.")
    item.status = "disabled"
    write_audit(
        session,
        principal.subject,
        "catalog.disable",
        "capability",
        item.id,
        details={"kind": item.kind},
    )
    session.commit()


def list_activities(session: Session, limit: int = 100) -> list[dict[str, Any]]:
    return [
        {
            "id": item.id,
            "actor": item.actor,
            "action": item.action,
            "resource_type": item.resource_type,
            "resource_id": item.resource_id,
            "decision": item.decision,
            "details": item.details_json,
            "created_at": item.created_at.isoformat(),
        }
        for item in session.scalars(
            select(AuditRecord).order_by(desc(AuditRecord.created_at)).limit(limit)
        ).all()
    ]


def list_evaluations(session: Session, limit: int = 100) -> list[dict[str, Any]]:
    return [
        {
            "id": item.id,
            "task_id": item.task_id,
            "suite": item.suite,
            "passed": item.passed,
            "score": item.score,
            "checks": item.checks_json,
            "created_at": item.created_at.isoformat(),
        }
        for item in session.scalars(
            select(EvaluationRecord).order_by(desc(EvaluationRecord.created_at)).limit(limit)
        ).all()
    ]
