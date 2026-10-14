"""Deterministic chief-officer reviewers; they advise but never authorize actions."""

from typing import Any

from fastapi import HTTPException
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.audit import verify_audit_chain
from app.config import Settings
from app.event_bus import publish_event, write_audit
from app.models import ApprovalRecord, EvaluationRecord, WorkflowCheckpoint
from app.policy import authorize
from app.schemas import Principal
from app.services import get_task

ADVISORS: dict[str, dict[str, str]] = {
    "architecture": {
        "name": "Chief Architect",
        "focus": "evidence, sources, boundaries, and reusable capabilities",
    },
    "engineering": {
        "name": "Chief Engineer",
        "focus": "workflow completion, checkpoints, and operational errors",
    },
    "quality": {
        "name": "Chief Quality Officer",
        "focus": "deterministic evaluations and acceptance checks",
    },
    "governance": {
        "name": "Chief Governance Officer",
        "focus": "audit integrity, access, and pending human decisions",
    },
}


def list_advisors() -> list[dict[str, str]]:
    return [
        {"id": key, **value, "decision_authority": "advisory-only"}
        for key, value in ADVISORS.items()
    ]


def review_task(
    session: Session,
    config: Settings,
    task_id: str,
    advisor_id: str,
    principal: Principal,
) -> dict[str, Any]:
    if advisor_id not in ADVISORS:
        raise HTTPException(status_code=404, detail="Advisor not found.")
    allowed, reason = authorize(principal, "advisor.review", {"task_id": task_id}, config)
    if not allowed:
        raise HTTPException(status_code=403, detail=reason)
    task = get_task(session, task_id, principal)
    result = task.result_json or {}
    checks: list[dict[str, Any]]

    if advisor_id == "architecture":
        evidence = any(
            isinstance(result.get(key), (list, dict)) and bool(result.get(key))
            for key in ("sources", "citations", "findings", "evidence", "facts")
        )
        checks = [
            {"id": "result-recorded", "passed": bool(result), "detail": "Task result exists."},
            {
                "id": "evidence-linked",
                "passed": evidence,
                "detail": "Result includes source or finding references."
                if evidence
                else "No explicit evidence list was found.",
            },
        ]
    elif advisor_id == "engineering":
        checkpoint_count = session.scalar(
            select(WorkflowCheckpoint.id).where(WorkflowCheckpoint.task_id == task.id).limit(1)
        )
        checks = [
            {
                "id": "terminal-state",
                "passed": task.status in {"completed", "needs_review", "awaiting_approval"},
                "detail": f"Task state is {task.status}.",
            },
            {
                "id": "checkpoint-recorded",
                "passed": checkpoint_count is not None,
                "detail": "Workflow checkpoint exists."
                if checkpoint_count
                else "No workflow checkpoint was found.",
            },
        ]
    elif advisor_id == "quality":
        evaluation = session.scalar(
            select(EvaluationRecord)
            .where(EvaluationRecord.task_id == task.id)
            .order_by(desc(EvaluationRecord.created_at))
            .limit(1)
        )
        checks = [
            {
                "id": "evaluation-recorded",
                "passed": evaluation is not None,
                "detail": "Deterministic evaluation exists."
                if evaluation
                else "No evaluation exists yet.",
            },
            {
                "id": "evaluation-passed",
                "passed": bool(evaluation and evaluation.passed and evaluation.score >= 80),
                "detail": f"Latest score: {evaluation.score}/100."
                if evaluation
                else "Evaluation score unavailable.",
            },
        ]
    else:
        audit = verify_audit_chain(session)
        pending = session.scalar(
            select(ApprovalRecord.id)
            .where(
                ApprovalRecord.task_id == task.id,
                ApprovalRecord.status == "pending",
            )
            .limit(1)
        )
        unresolved_write = (
            result.get("external_write") == "pending human approval"
            and task.status == "awaiting_approval"
        )
        checks = [
            {
                "id": "audit-chain-valid",
                "passed": audit["valid"],
                "detail": f"Audit chain contains {audit['entries']} verified entries.",
            },
            {
                "id": "approval-gated",
                "passed": not unresolved_write or pending is not None,
                "detail": "External write is pending human approval."
                if pending
                else "No unapproved external write was detected.",
            },
        ]

    passed = all(item["passed"] for item in checks)
    pending_approval = task.status == "awaiting_approval"
    recommendation = (
        "human_decision_required"
        if pending_approval
        else "recommend_accept"
        if passed
        else "needs_review"
    )
    response = {
        "advisor": {"id": advisor_id, **ADVISORS[advisor_id]},
        "task_id": task.id,
        "recommendation": recommendation,
        "decision_authority": "advisory-only",
        "checks": checks,
    }
    publish_event(
        session,
        config,
        task.id,
        "advisor.reviewed",
        f"{ADVISORS[advisor_id]['name']} completed an advisory review.",
        {"advisor_id": advisor_id, "recommendation": recommendation, "checks": checks},
    )
    write_audit(
        session,
        principal.subject,
        "advisor.review",
        "task",
        task.id,
        details={"advisor_id": advisor_id, "recommendation": recommendation},
    )
    session.commit()
    return response
