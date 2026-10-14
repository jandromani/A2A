"""Safe, declarative workflow definitions for new use cases."""

import json
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.connectors import invoke_connector
from app.event_bus import publish_event, write_audit
from app.ledger import record_model_usage
from app.memory import save_conversation_exchange, save_workflow_checkpoint
from app.model_gateway import ModelGateway
from app.models import (
    ApprovalRecord,
    EvaluationRecord,
    TaskRecord,
    WorkflowCheckpoint,
    WorkflowDefinition,
)
from app.policy import authorize
from app.schemas import ConnectorInvoke, Principal, WorkflowDefinitionCreate, WorkflowRunRequest
from app.services import serialize_task
from app.workflows.engine import _search_documents

_TEMPLATE = re.compile(r"\{\{\s*(input|steps)\.([a-zA-Z0-9_.-]+)\s*\}\}")
_STEP_KEYS = {
    "knowledge.search": {"query", "limit"},
    "model.generate": {"prompt", "context"},
    "connector.invoke": {"connector_id", "message", "scenario", "tool_name", "arguments"},
    "approval": {"title", "draft"},
    "result": {"summary", "include_steps"},
}


def _validate_workflow(body: WorkflowDefinitionCreate) -> None:
    completed: set[str] = set()
    for index, step in enumerate(body.steps):
        unknown = set(step.config) - _STEP_KEYS[step.type]
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"Unsupported options in step {step.id}: {', '.join(sorted(unknown))}.",
            )
        if step.type == "connector.invoke" and not step.config.get("connector_id"):
            raise HTTPException(status_code=422, detail=f"Step {step.id} requires connector_id.")
        if step.type == "model.generate" and not step.config.get("prompt"):
            raise HTTPException(status_code=422, detail=f"Step {step.id} requires a prompt.")
        if step.type == "knowledge.search" and not step.config.get("query"):
            raise HTTPException(status_code=422, detail=f"Step {step.id} requires a query.")
        if step.type == "approval" and not step.config.get("draft"):
            raise HTTPException(
                status_code=422, detail=f"Approval step {step.id} requires a review draft."
            )
        if step.type == "connector.invoke" and (
            index == 0 or body.steps[index - 1].type != "approval"
        ):
            raise HTTPException(
                status_code=422,
                detail=f"Connector step {step.id} must immediately follow a human approval step.",
            )
        for value in step.config.values():
            for referenced in _step_references(value):
                if referenced not in completed:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Step {step.id} references unavailable step {referenced}.",
                    )
        completed.add(step.id)


def _step_references(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set().union(*(_step_references(item) for item in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_step_references(item) for item in value)) if value else set()
    if not isinstance(value, str):
        return set()
    return {
        match.group(2).split(".", maxsplit=1)[0]
        for match in _TEMPLATE.finditer(value)
        if match.group(1) == "steps"
    }


def _resolve(value: Any, input_data: dict[str, Any], outputs: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: _resolve(item, input_data, outputs) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item, input_data, outputs) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        root, path = match.groups()
        current: Any = input_data if root == "input" else outputs
        for part in path.split("."):
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
                current = current[int(part)]
            else:
                current = ""
                break
        return (
            current
            if isinstance(current, str)
            else json.dumps(current, ensure_ascii=False, default=str)
        )

    return _TEMPLATE.sub(replace, value)


def _checkpoint_state(outputs: dict[str, Any], status: str) -> dict[str, Any]:
    return {"status": status, "completed_steps": sorted(outputs), "outputs": outputs}


def _workflow_summary(steps: list[dict[str, Any]], outputs: dict[str, Any], fallback: str) -> str:
    for step in reversed(steps):
        if step.get("type") == "result":
            result = outputs.get(step.get("id"))
            if isinstance(result, dict) and result.get("summary"):
                return str(result["summary"])
    return fallback


def create_workflow_definition(
    session: Session,
    config: Settings,
    body: WorkflowDefinitionCreate,
    principal: Principal,
) -> dict[str, Any]:
    allowed, reason = authorize(principal, "workflow.manage", {}, config)
    if not allowed:
        raise HTTPException(status_code=403, detail=reason)
    _validate_workflow(body)
    existing = session.get(WorkflowDefinition, body.id)
    now = datetime.now(UTC)
    if existing:
        existing.name = body.name
        existing.description = body.description
        existing.version = body.version
        existing.definition_json = {"steps": [step.model_dump() for step in body.steps]}
        existing.enabled = body.enabled
        existing.updated_at = now
        record = existing
        action = "workflow.update"
    else:
        record = WorkflowDefinition(
            id=body.id,
            name=body.name,
            description=body.description,
            version=body.version,
            definition_json={"steps": [step.model_dump() for step in body.steps]},
            enabled=body.enabled,
            created_by=principal.subject,
        )
        session.add(record)
        action = "workflow.create"
    session.flush()
    write_audit(session, principal.subject, action, "workflow", record.id)
    session.commit()
    return serialize_workflow(record)


def serialize_workflow(record: WorkflowDefinition) -> dict[str, Any]:
    return {
        "id": record.id,
        "name": record.name,
        "description": record.description,
        "version": record.version,
        "steps": record.definition_json.get("steps", []),
        "enabled": record.enabled,
        "created_by": record.created_by,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }


def list_workflow_definitions(session: Session) -> list[dict[str, Any]]:
    records = session.scalars(
        select(WorkflowDefinition)
        .where(WorkflowDefinition.enabled.is_(True))
        .order_by(WorkflowDefinition.name)
    ).all()
    return [serialize_workflow(record) for record in records]


def _execute_step(
    step: dict[str, Any],
    session: Session,
    config: Settings,
    principal: Principal,
    task: TaskRecord,
    input_data: dict[str, Any],
    outputs: dict[str, Any],
) -> dict[str, Any]:
    action = step["type"]
    options = _resolve(step.get("config", {}), input_data, outputs)
    if action == "knowledge.search":
        allowed, reason = authorize(principal, "knowledge.read", {}, config)
        if not allowed:
            raise HTTPException(status_code=403, detail=reason)
        query = str(options["query"])
        items = _search_documents(
            session, query, principal.scopes, config, limit=int(options.get("limit", 5))
        )
        return {
            "items": [
                {
                    "title": document.title,
                    "source_uri": document.source_uri,
                    "scope": document.scope,
                    "quote": document.content[:600],
                    "score": round(score, 4),
                }
                for document, score in items
            ]
        }
    if action == "model.generate":
        result = ModelGateway(config).generate(
            str(options["prompt"]), str(options.get("context", ""))
        )
        usage = result.as_dict()
        record_model_usage(session, config, task, f"{task.id}:{step['id']}", usage)
        return {"text": result.text, "model_usage": usage}
    if action == "connector.invoke":
        request = ConnectorInvoke(
            message=options.get("message"),
            scenario=options.get("scenario", "knowledge"),
            tool_name=options.get("tool_name"),
            arguments=options.get("arguments", {}),
        )
        return invoke_connector(session, config, str(options["connector_id"]), request, principal)
    if action == "approval":
        approval = ApprovalRecord(
            task_id=task.id,
            status="pending",
            requested_by=principal.subject,
            decision_json={
                "title": str(options.get("title", task.title))[:180],
                "draft": str(options.get("draft", ""))[:12_000],
                "source": "workflow",
                "workflow_id": task.payload_json.get("workflow_id"),
            },
        )
        session.add(approval)
        session.flush()
        task.status = "awaiting_approval"
        return {"approval_id": approval.id, "status": "pending"}
    if action == "result":
        return {
            "summary": str(options.get("summary", "Workflow completed."))[:4_000],
            "steps": json.loads(json.dumps(outputs, ensure_ascii=False, default=str))
            if options.get("include_steps", True)
            else {},
        }
    raise HTTPException(status_code=422, detail=f"Unsupported workflow action: {action}.")


def run_workflow_definition(
    session: Session,
    config: Settings,
    workflow_id: str,
    body: WorkflowRunRequest,
    principal: Principal,
) -> dict[str, Any]:
    record = session.get(WorkflowDefinition, workflow_id)
    if not record or not record.enabled:
        raise HTTPException(status_code=404, detail="Workflow not found or disabled.")
    allowed, reason = authorize(principal, "workflow.run", {}, config)
    if not allowed:
        raise HTTPException(status_code=403, detail=reason)
    if len(json.dumps(body.input, default=str).encode("utf-8")) > config.max_artifact_bytes:
        raise HTTPException(
            status_code=413, detail="Workflow input exceeds the configured size limit."
        )
    for step in record.definition_json.get("steps", []):
        if step.get("type") == "connector.invoke" and not principal.roles.intersection(
            {"approver", "admin"}
        ):
            raise HTTPException(
                status_code=403, detail="Connector steps require approver or admin role."
            )
    message = str(
        body.input.get("message") or body.input.get("query") or f"Run workflow {record.name}"
    )[:12_000]
    task = TaskRecord(
        scenario="workflow",
        title=record.name,
        request_text=message,
        status="queued" if config.task_execution_mode == "queue" else "running",
        actor=principal.subject,
        context_id=body.context_id,
        payload_json={
            "workflow_id": workflow_id,
            "input": body.input,
            "workflow_version": record.version,
            "workflow_steps": record.definition_json.get("steps", []),
            "actor_roles": sorted(principal.roles),
            "actor_scopes": sorted(principal.scopes),
        },
    )
    session.add(task)
    session.flush()
    write_audit(
        session,
        principal.subject,
        "workflow.run",
        "workflow",
        workflow_id,
        details={"task_id": task.id, "version": record.version},
    )
    event_type = "task.queued" if config.task_execution_mode == "queue" else "task.started"
    publish_event(
        session,
        config,
        task.id,
        event_type,
        f"{record.name} queued."
        if config.task_execution_mode == "queue"
        else f"{record.name} started.",
        {
            "scenario": "workflow",
            "workflow_id": workflow_id,
            "actor": principal.subject,
            "roles": sorted(principal.roles),
            "scopes": sorted(principal.scopes),
        },
    )
    if config.task_execution_mode == "queue":
        session.commit()
        return serialize_task(task)

    return execute_workflow_task(session, config, task, principal)


def execute_workflow_task(
    session: Session,
    config: Settings,
    task: TaskRecord,
    principal: Principal,
) -> dict[str, Any]:
    workflow_id = str(task.payload_json.get("workflow_id") or "")
    record = session.get(WorkflowDefinition, workflow_id)
    if not record or not record.enabled:
        task.status = "failed"
        task.result_json = {"error": "Workflow definition is missing or disabled."}
        session.commit()
        return serialize_task(task)
    body = WorkflowRunRequest(input=task.payload_json.get("input", {}), context_id=task.context_id)
    allowed, reason = authorize(principal, "workflow.run", {}, config)
    if not allowed:
        task.status = "rejected"
        task.result_json = {"error": reason}
        session.commit()
        return serialize_task(task)

    task.status = "running"
    input_data = dict(body.input)
    outputs: dict[str, Any] = {}
    steps = task.payload_json.get("workflow_steps") or record.definition_json.get("steps", [])
    workflow_version = task.payload_json.get("workflow_version") or record.version
    try:
        for step in steps:
            outputs[step["id"]] = _execute_step(
                step, session, config, principal, task, input_data, outputs
            )
            save_workflow_checkpoint(
                session, task.id, step["id"], _checkpoint_state(outputs, task.status)
            )
            publish_event(
                session,
                config,
                task.id,
                "workflow.step.completed",
                f"Workflow step {step['id']} completed.",
                {"workflow_id": workflow_id, "step": step["id"]},
            )
            if task.status == "awaiting_approval":
                break
        task.result_json = {
            "workflow_id": workflow_id,
            "workflow_version": workflow_version,
            "steps": outputs,
            "summary": _workflow_summary(steps, outputs, f"{record.name} completed."),
            "approval_required": task.status == "awaiting_approval",
        }
        if task.status != "awaiting_approval":
            task.status = "completed"
        completed = len(outputs)
        approval_paused = task.status == "awaiting_approval"
        checks = [
            {
                "id": "step_execution",
                "passed": completed > 0,
                "detail": f"{completed} workflow steps completed.",
            },
            {
                "id": "approval_gate",
                "passed": not approval_paused
                or any(output.get("status") == "pending" for output in outputs.values()),
                "detail": "External action is paused for approval."
                if approval_paused
                else "No pending external write is waiting for approval.",
            },
        ]
        passed = all(check["passed"] for check in checks)
        evaluation = EvaluationRecord(
            task_id=task.id,
            suite=f"workflow:{workflow_id}",
            passed=passed,
            score=round(100 * sum(check["passed"] for check in checks) / len(checks)),
            checks_json=checks,
        )
        session.add(evaluation)
        save_workflow_checkpoint(
            session, task.id, "finished", _checkpoint_state(outputs, task.status)
        )
        terminal_event = "task.awaiting_approval" if approval_paused else "task.completed"
        publish_event(
            session,
            config,
            task.id,
            terminal_event,
            f"{record.name} finished with {task.status}.",
            {"status": task.status},
        )
        save_conversation_exchange(session, config, principal, body.context_id, task)
        session.commit()
    except Exception as exc:
        session.rollback()
        task = session.get(TaskRecord, task.id)
        if task:
            task.status = "failed"
            task.result_json = {"error": "Workflow execution failed.", "detail": str(exc)[:500]}
            write_audit(
                session, principal.subject, "workflow.execute", "task", task.id, decision="failed"
            )
            publish_event(
                session,
                config,
                task.id,
                "task.failed",
                "Workflow execution failed.",
                {"workflow_id": workflow_id},
            )
            session.commit()
    return serialize_task(task)


def resume_approved_workflow(
    session: Session,
    config: Settings,
    task: TaskRecord,
    approval: ApprovalRecord,
    principal: Principal,
) -> dict[str, Any]:
    """Resume a persisted declarative workflow after its human approval gate."""
    # Continue with the original caller's authorization snapshot. The approver
    # releases the pause; their role or scopes do not become the workflow's.
    principal = Principal(
        subject=task.actor,
        roles=frozenset(task.payload_json.get("actor_roles", [])),
        scopes=frozenset(task.payload_json.get("actor_scopes", [])),
    )
    workflow_id = str(task.payload_json.get("workflow_id") or "")
    record = session.get(WorkflowDefinition, workflow_id)
    if not record or not record.enabled:
        raise HTTPException(status_code=409, detail="Workflow definition is missing or disabled.")
    checkpoints = session.scalars(
        select(WorkflowCheckpoint)
        .where(WorkflowCheckpoint.task_id == task.id)
        .order_by(WorkflowCheckpoint.created_at.desc())
    ).all()
    state = next(
        (
            item.state_json
            for item in checkpoints
            if isinstance(item.state_json.get("outputs"), dict)
        ),
        None,
    )
    if state is None:
        raise HTTPException(status_code=409, detail="Workflow checkpoint is missing.")
    outputs = dict(state["outputs"])
    approved_step = next(
        (
            step_id
            for step_id, value in outputs.items()
            if isinstance(value, dict) and value.get("approval_id") == approval.id
        ),
        None,
    )
    if not approved_step:
        raise HTTPException(
            status_code=409, detail="Approval does not belong to this workflow checkpoint."
        )
    outputs[approved_step] = {"approval_id": approval.id, "status": "approved"}
    task.status = "running"
    input_data = dict(task.payload_json.get("input", {}))
    try:
        for step in task.payload_json.get("workflow_steps") or record.definition_json.get(
            "steps", []
        ):
            step_id = step["id"]
            if step_id in outputs:
                continue
            outputs[step_id] = _execute_step(
                step, session, config, principal, task, input_data, outputs
            )
            save_workflow_checkpoint(
                session, task.id, step_id, _checkpoint_state(outputs, task.status)
            )
            publish_event(
                session,
                config,
                task.id,
                "workflow.step.completed",
                f"Workflow step {step_id} completed.",
                {"workflow_id": workflow_id, "step": step_id},
            )
            if task.status == "awaiting_approval":
                break
        paused = task.status == "awaiting_approval"
        task.result_json = {
            "workflow_id": workflow_id,
            "workflow_version": task.payload_json.get("workflow_version") or record.version,
            "steps": outputs,
            "summary": _workflow_summary(
                task.payload_json.get("workflow_steps") or record.definition_json.get("steps", []),
                outputs,
                f"{record.name} completed.",
            ),
            "approval_required": paused,
        }
        if not paused:
            task.status = "completed"
        checks = [
            {
                "id": "step_execution",
                "passed": bool(outputs),
                "detail": f"{len(outputs)} workflow steps completed.",
            },
            {
                "id": "approval_gate",
                "passed": not paused
                or any(
                    value.get("status") == "pending"
                    for value in outputs.values()
                    if isinstance(value, dict)
                ),
                "detail": "External action is paused for approval."
                if paused
                else "Approval gate completed.",
            },
        ]
        passed = all(item["passed"] for item in checks)
        session.add(
            EvaluationRecord(
                task_id=task.id,
                suite=f"workflow:{workflow_id}",
                passed=passed,
                score=round(100 * sum(item["passed"] for item in checks) / len(checks)),
                checks_json=checks,
            )
        )
        save_workflow_checkpoint(
            session, task.id, "finished", _checkpoint_state(outputs, task.status)
        )
        publish_event(
            session,
            config,
            task.id,
            "task.awaiting_approval" if paused else "task.completed",
            f"{record.name} resumed and finished with {task.status}.",
            {"status": task.status, "approval_id": approval.id},
        )
        save_conversation_exchange(session, config, principal, task.context_id, task)
        task.updated_at = datetime.now(UTC)
        session.commit()
    except Exception as exc:
        session.rollback()
        task = session.get(TaskRecord, task.id)
        if task:
            task.status = "failed"
            task.result_json = {"error": "Workflow resume failed.", "detail": str(exc)[:500]}
            write_audit(
                session, principal.subject, "workflow.resume", "task", task.id, decision="failed"
            )
            publish_event(
                session,
                config,
                task.id,
                "task.failed",
                "Workflow resume failed.",
                {"workflow_id": workflow_id},
            )
            session.commit()
    return serialize_task(task)
