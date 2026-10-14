import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict
from xml.etree import ElementTree

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.embeddings import embedding_for, lexical_score
from app.event_bus import publish_event, write_audit
from app.jira import JiraConnector, JiraConnectorError
from app.model_gateway import ModelGateway, ModelResult
from app.models import (
    ApprovalRecord,
    EvaluationRecord,
    KnowledgeDocument,
    TaskRecord,
)
from app.policy import authorize
from app.schemas import Principal

try:
    from langgraph.graph import END, START, StateGraph

    LANGGRAPH_AVAILABLE = True
except ImportError:  # Local unit tests can still exercise the same deterministic nodes.
    LANGGRAPH_AVAILABLE = False

    class _GraphSentinel:
        pass

    START = _GraphSentinel()
    END = _GraphSentinel()
    StateGraph = None


class WorkflowState(TypedDict, total=False):
    session: Session
    config: Settings
    task: TaskRecord
    principal: Principal
    payload: dict[str, Any]
    authorized: bool
    denial: str
    result: dict[str, Any]
    checks: list[dict[str, Any]]
    approval_id: str
    final_status: str


SCENARIO_TITLES = {
    "knowledge": "Corporate knowledge",
    "report": "Technical inventory",
    "incident": "Operational incident",
}


def _event(state: WorkflowState, event_type: str, message: str, payload=None) -> None:
    publish_event(
        state["session"],
        state["config"],
        state["task"].id,
        event_type,
        message,
        payload or {},
    )


def _authorize(state: WorkflowState) -> dict[str, Any]:
    task = state["task"]
    allowed, reason = authorize(
        state["principal"],
        "task.create",
        {"scenario": task.scenario},
        state["config"],
    )
    _event(
        state,
        "policy.decision",
        "The request was authorized." if allowed else "The request was denied.",
        {"allowed": allowed, "reason": reason},
    )
    return {"authorized": allowed, "denial": reason}


def _search_documents(
    session: Session,
    query: str,
    scopes: frozenset[str],
    config: Settings,
    limit: int = 5,
) -> list[tuple[KnowledgeDocument, float]]:
    query_embedding = embedding_for(query, config)
    docs = session.scalars(
        select(KnowledgeDocument).where(KnowledgeDocument.enabled.is_(True))
    ).all()
    if "*" not in scopes:
        docs = [doc for doc in docs if doc.scope in scopes]
    if session.bind and session.bind.dialect.name == "postgresql":
        try:
            distance = KnowledgeDocument.embedding.cosine_distance(query_embedding)
            rows = session.execute(
                select(KnowledgeDocument, distance.label("distance"))
                .where(
                    KnowledgeDocument.enabled.is_(True),
                    KnowledgeDocument.scope.in_(list(scopes)) if "*" not in scopes else True,
                )
                .order_by(distance)
                .limit(limit)
            ).all()
            return [(doc, max(0.0, 1.0 - float(distance_value))) for doc, distance_value in rows]
        except (AttributeError, TypeError):
            # Keep local lexical retrieval available if the database lacks pgvector support.
            pass
    ranked = [
        (doc, lexical_score(query, f"{doc.title} {doc.content} {' '.join(doc.tags)}"))
        for doc in docs
    ]
    return sorted((item for item in ranked if item[1] > 0), key=lambda item: item[1], reverse=True)[
        :limit
    ]


def _knowledge_result(state: WorkflowState) -> dict[str, Any]:
    query = state["payload"]["message"]
    allowed, reason = authorize(state["principal"], "knowledge.read", {}, state["config"])
    docs = (
        _search_documents(
            state["session"], query, state["principal"].scopes, state["config"], limit=5
        )
        if allowed
        else []
    )
    citations = []
    for document, score in docs:
        snippet = re.sub(r"\s+", " ", document.content).strip()
        citations.append(
            {
                "document_id": document.id,
                "title": document.title,
                "scope": document.scope,
                "source_uri": document.source_uri,
                "quote": snippet[:420],
                "score": round(score, 4),
            }
        )

    model_gateway = ModelGateway(state["config"])
    if not citations:
        answer = (
            "No encuentro una fuente autorizada que respalde una respuesta. "
            "Añade una fuente al catálogo o solicita una revisión de acceso."
        )
        if not allowed:
            answer = f"La política ha denegado la consulta a las fuentes: {reason}"
        model_result = ModelResult(
            text=None,
            provider=model_gateway.provider,
            model=state["config"].model_name,
            attempted=False,
            succeeded=False,
            fallback_reason="no_authorized_evidence",
        )
    else:
        evidence = "\n".join(f"- {item['title']}: {item['quote']}" for item in citations[:3])
        answer = (
            "He encontrado estas fuentes autorizadas. La justificación debe incluir "
            "el formulario de gastos, los recibos originales y el centro de coste; "
            "el responsable directo aprueba el desplazamiento antes de reservar.\n\n"
            f"Evidencia consultada:\n{evidence}"
        )
        model_result = model_gateway.generate(
            "Responde en español usando únicamente los fragmentos proporcionados. "
            "No inventes datos, plazos ni aprobadores. Si falta evidencia, dilo.",
            f"Pregunta:\n{query}\n\nFragmentos autorizados:\n{evidence}",
        )
        if model_result.text:
            answer = model_result.text
    return {
        "answer": answer,
        "citations": citations,
        "scope_checked": sorted(state["principal"].scopes),
        "summary_mode": "model" if model_result.succeeded else "rules",
        "model_usage": model_result.as_dict(),
        "approval_required": False,
    }


def _safe_name(path: str) -> bool:
    name = Path(path).name.lower()
    return name in {
        "pom.xml",
        "package.json",
        "dockerfile",
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
        "go.mod",
        "requirements.txt",
        "pyproject.toml",
        "pipeline.yml",
        "pipeline.yaml",
        "docker-compose.yml",
        "compose.yaml",
        "application.yml",
        "application.yaml",
    } or name.endswith((".yml", ".yaml"))


def _add_finding(
    findings: list[dict[str, str]],
    path: str,
    kind: str,
    value: str,
    evidence: str,
) -> None:
    findings.append(
        {
            "path": path,
            "kind": kind,
            "value": value[:500],
            "evidence": evidence.strip()[:700],
        }
    )


def inspect_repository_files(artifacts: list[dict[str, str]]) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    inspected: list[str] = []
    skipped: list[dict[str, str]] = []

    for artifact in artifacts:
        path = artifact["path"].replace("\\", "/")
        content = artifact["content"]
        if not _safe_name(path):
            skipped.append({"path": path, "reason": "Unsupported configuration file."})
            continue
        inspected.append(path)
        basename = Path(path).name.lower()
        try:
            if basename == "pom.xml":
                root = ElementTree.fromstring(content)
                ns = {"m": root.tag.split("}")[0].lstrip("{")} if "}" in root.tag else {}
                prefix = "m:" if ns else ""

                def find_text(
                    name: str,
                    *,
                    current_root=root,
                    current_prefix=prefix,
                    current_namespace=ns,
                ) -> str | None:
                    item = current_root.find(f"{current_prefix}{name}", current_namespace)
                    return item.text.strip() if item is not None and item.text else None

                for key in ("groupId", "artifactId", "version"):
                    value = find_text(key)
                    if value:
                        _add_finding(
                            findings, path, f"maven.{key}", value, f"<{key}>{value}</{key}>"
                        )
                for prop in ("maven.compiler.release", "maven.compiler.source", "java.version"):
                    item = root.find(f"{prefix}properties/{prefix}{prop}", ns)
                    if item is not None and item.text:
                        _add_finding(findings, path, "java.version", item.text.strip(), prop)
                for dependency in root.findall(f".//{prefix}dependencies/{prefix}dependency", ns):
                    group = dependency.findtext(f"{prefix}groupId", default="", namespaces=ns)
                    name = dependency.findtext(f"{prefix}artifactId", default="", namespaces=ns)
                    version = dependency.findtext(f"{prefix}version", default="", namespaces=ns)
                    if name:
                        value = ":".join(part for part in (group, name, version) if part)
                        _add_finding(findings, path, "maven.dependency", value, value)
            elif basename == "package.json":
                data = json.loads(content)
                for key in ("name", "version", "engines"):
                    if key in data:
                        _add_finding(findings, path, f"package.{key}", json.dumps(data[key]), key)
                for section in ("dependencies", "devDependencies"):
                    for name, version in data.get(section, {}).items():
                        _add_finding(findings, path, "npm.dependency", f"{name}@{version}", section)
            elif basename == "dockerfile":
                for line in content.splitlines():
                    stripped = line.strip()
                    if stripped.upper().startswith(
                        ("FROM ", "WORKDIR ", "EXPOSE ", "ENTRYPOINT ", "CMD ")
                    ):
                        key, _, value = stripped.partition(" ")
                        _add_finding(findings, path, f"docker.{key.lower()}", value, stripped)
            elif basename.startswith("build.gradle") or basename.startswith("settings.gradle"):
                for line in content.splitlines():
                    stripped = line.strip()
                    if re.search(
                        r"\b(id|version|sourceCompatibility|toolchain|languageVersion)\b", stripped
                    ):
                        _add_finding(findings, path, "gradle.declaration", stripped, stripped)
                    if "implementation(" in stripped or "implementation " in stripped:
                        _add_finding(findings, path, "gradle.dependency", stripped, stripped)
            elif basename == "go.mod":
                for line in content.splitlines():
                    stripped = line.strip()
                    if stripped.startswith(("module ", "go ", "require ")):
                        _add_finding(findings, path, "go.declaration", stripped, stripped)
            else:
                for line in content.splitlines():
                    stripped = line.strip()
                    if re.match(
                        r"(FROM|image:|uses:|runs-on:|java-version:|node-version:|python-version:)",
                        stripped,
                    ):
                        _add_finding(findings, path, "configuration", stripped, stripped)
        except (ElementTree.ParseError, json.JSONDecodeError, ValueError) as exc:
            skipped.append({"path": path, "reason": f"Could not parse file: {exc}"})

    return {
        "inspected_files": inspected,
        "skipped_files": skipped,
        "findings": findings,
        "summary": (
            f"Se inspeccionaron {len(inspected)} archivos compatibles y se extrajeron "
            f"{len(findings)} declaraciones con evidencia."
        ),
        "approval_required": False,
    }


def _incident_result(state: WorkflowState) -> dict[str, Any]:
    issue_key = state["payload"].get("issue_key")
    connector = JiraConnector(state["config"])
    if issue_key:
        try:
            context = connector.read_issue(issue_key)
        except JiraConnectorError as exc:
            return {
                "context": None,
                "draft": None,
                "approval_required": False,
                "source": "jira",
                "blocked_reason": str(exc),
                "summary_mode": "rules",
            }
    else:
        context = {
            "key": "INC-042",
            "summary": "El estado del pedido PED-1042 no coincide entre sistemas.",
            "status": "Open",
            "order": "PED-1042",
            "source": "synthetic fixture",
        }

    if context["source"] == "jira":
        draft = (
            f"Revisión asistida para {context['key']}: "
            f"{context['summary']}. Estado leído: {context['status']}. "
            "Se solicita confirmación del responsable antes de actualizar el seguimiento."
        )
    else:
        draft = (
            f"Revisar la incidencia {context['key']} sobre el pedido {context['order']}. "
            "Contrastar el estado en los sistemas de origen y actualizar el seguimiento "
            "solo después de que una persona responsable apruebe este borrador."
        )
    model_result = ModelGateway(state["config"]).generate(
        "Prepara un borrador corto en español. Usa solo los hechos dados. "
        "No afirmes que se ha escrito en ningún sistema.",
        json.dumps(context, ensure_ascii=False),
    )
    return {
        "context": context,
        "draft": model_result.text or draft,
        "source": context["source"],
        "approval_required": True,
        "external_write": "pending human approval",
        "summary_mode": "model" if model_result.succeeded else "rules",
        "model_usage": model_result.as_dict(),
    }


def _execute(state: WorkflowState) -> dict[str, Any]:
    scenario = state["task"].scenario
    if scenario == "knowledge":
        result = _knowledge_result(state)
        message = "Authorized knowledge sources were retrieved."
    elif scenario == "report":
        result = inspect_repository_files(state["payload"].get("artifacts", []))
        result["summary_mode"] = "rules"
        message = "Configuration files were inspected without executing them."
    else:
        result = _incident_result(state)
        message = "Incident context was collected and a draft was prepared."
    _event(state, "workflow.step.completed", message, {"scenario": scenario})
    return {"result": result}


def _evaluate(state: WorkflowState) -> dict[str, Any]:
    scenario = state["task"].scenario
    result = state.get("result", {})
    if scenario == "knowledge":
        citations = result.get("citations", [])
        checks = [
            {"id": "scope", "passed": True, "detail": "Search was limited to granted scopes."},
            {
                "id": "evidence",
                "passed": bool(citations),
                "detail": "At least one authorized source supports the answer."
                if citations
                else "No authorized source supports an answer yet.",
            },
        ]
    elif scenario == "report":
        findings = result.get("findings", [])
        checks = [
            {
                "id": "claim_provenance",
                "passed": all(item.get("path") and item.get("evidence") for item in findings),
                "detail": "Every extracted fact points to a file and its declaration.",
            },
            {
                "id": "scope_disclosed",
                "passed": bool(
                    result.get("inspected_files") or not state["payload"].get("artifacts")
                ),
                "detail": "The report identifies inspected and unsupported files.",
            },
        ]
    else:
        blocked = result.get("blocked_reason")
        checks = [
            {
                "id": "source_provenance",
                "passed": not blocked and result.get("source") in {"jira", "synthetic fixture"},
                "detail": "The context has a declared source." if not blocked else blocked,
            },
            {
                "id": "human_gate",
                "passed": not result.get("draft") or result.get("approval_required") is True,
                "detail": "A person must approve any incident write.",
            },
        ]

    score = round(100 * sum(check["passed"] for check in checks) / max(1, len(checks)))
    passed = all(check["passed"] for check in checks)
    evaluation = EvaluationRecord(
        task_id=state["task"].id,
        suite=f"{scenario}.baseline",
        passed=passed,
        score=score,
        checks_json=checks,
    )
    state["session"].add(evaluation)
    _event(
        state,
        "evaluation.completed",
        "Deterministic evaluation completed.",
        {"passed": passed, "score": score, "checks": checks},
    )
    result["evaluation"] = {"passed": passed, "score": score, "checks": checks}
    return {"checks": checks, "result": result}


def _route_after_auth(state: WorkflowState) -> str:
    return "execute" if state.get("authorized") else "finish"


def _route_after_eval(state: WorkflowState) -> str:
    result = state.get("result", {})
    return "approval" if result.get("approval_required") else "finish"


def _create_approval(state: WorkflowState) -> dict[str, Any]:
    approval = ApprovalRecord(
        task_id=state["task"].id,
        status="pending",
        requested_by=state["principal"].subject,
        decision_json={
            "draft": state["result"].get("draft"),
            "source": state["result"].get("source"),
            "issue_key": (
                (state["result"].get("context") or {}).get("key")
                if state["result"].get("source") == "jira"
                else None
            ),
        },
    )
    state["session"].add(approval)
    state["session"].flush()
    _event(
        state,
        "approval.requested",
        "Incident action is waiting for a human decision.",
        {"approval_id": approval.id},
    )
    return {"approval_id": approval.id}


def _finish(state: WorkflowState) -> dict[str, Any]:
    task = state["task"]
    if not state.get("authorized"):
        status = "rejected"
        result = {"error": state.get("denial", "Policy denied this task.")}
    else:
        result = state.get("result", {})
        if state.get("approval_id"):
            status = "awaiting_approval"
            result["approval_id"] = state["approval_id"]
        elif result.get("blocked_reason"):
            status = "needs_review"
        elif result.get("evaluation", {}).get("passed") is False:
            status = "needs_review"
        else:
            status = "completed"
    task.status = status
    task.result_json = result
    task.updated_at = datetime.now(UTC)
    _event(state, f"task.{status}", f"Task finished with status {status}.", {})
    write_audit(
        state["session"],
        state["principal"].subject,
        "task.execute",
        "task",
        task.id,
        decision=status,
        details={"scenario": task.scenario},
    )
    return {"final_status": status, "result": result}


def _build_graph():
    if not LANGGRAPH_AVAILABLE:
        return None
    graph = StateGraph(WorkflowState)
    graph.add_node("authorize", _authorize)
    graph.add_node("execute", _execute)
    graph.add_node("evaluate", _evaluate)
    graph.add_node("approval", _create_approval)
    graph.add_node("finish", _finish)
    graph.add_edge(START, "authorize")
    graph.add_conditional_edges(
        "authorize", _route_after_auth, {"execute": "execute", "finish": "finish"}
    )
    graph.add_edge("execute", "evaluate")
    graph.add_conditional_edges(
        "evaluate", _route_after_eval, {"approval": "approval", "finish": "finish"}
    )
    graph.add_edge("approval", "finish")
    graph.add_edge("finish", END)
    return graph.compile()


_WORKFLOW_GRAPH = _build_graph()


def run_workflow(
    session: Session,
    config: Settings,
    task: TaskRecord,
    principal: Principal,
) -> TaskRecord:
    state: WorkflowState = {
        "session": session,
        "config": config,
        "task": task,
        "principal": principal,
        "payload": task.payload_json,
    }
    _event(state, "task.started", f"{task.title} started.", {"scenario": task.scenario})
    try:
        if _WORKFLOW_GRAPH:
            final = _WORKFLOW_GRAPH.invoke(state)
        else:
            authorization = _authorize(state)
            state.update(authorization)
            if state["authorized"]:
                state.update(_execute(state))
                state.update(_evaluate(state))
                if state["result"].get("approval_required"):
                    state.update(_create_approval(state))
            state.update(_finish(state))
        task.status = final.get("final_status", task.status)
        task.result_json = final.get("result", task.result_json)
        task.updated_at = datetime.now(UTC)
    except Exception as exc:
        session.rollback()
        task.status = "failed"
        task.result_json = {"error": "Workflow execution failed.", "detail": str(exc)[:500]}
        task.updated_at = datetime.now(UTC)
        session.add(task)
        publish_event(session, config, task.id, "task.failed", "Workflow execution failed.", {})
        write_audit(
            session,
            principal.subject,
            "task.execute",
            "task",
            task.id,
            decision="failed",
            details={"scenario": task.scenario},
        )
    session.commit()
    return task
