from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.config import Settings, settings
from app.embeddings import embedding_for
from app.models import Capability, KnowledgeDocument

CAPABILITIES = [
    {
        "id": "agent.knowledge",
        "kind": "agent",
        "name": "Knowledge Agent",
        "description": "Busca en fuentes con permisos, prepara una respuesta y conserva citas.",
        "tags": ["knowledge", "retrieval", "evidence"],
    },
    {
        "id": "agent.report",
        "kind": "agent",
        "name": "Technical Report Agent",
        "description": "Inspecciona archivos de configuración y separa hechos de observaciones.",
        "tags": ["repository", "inventory", "evidence"],
    },
    {
        "id": "agent.incident",
        "kind": "agent",
        "name": "Incident Agent",
        "description": "Reúne contexto y prepara un cambio para aprobación humana.",
        "tags": ["incident", "approval", "jira"],
    },
    {
        "id": "tool.knowledge.search",
        "kind": "tool",
        "name": "Knowledge search",
        "description": "Retrieve relevant documents within the caller's authorized scopes.",
        "tags": ["read", "knowledge"],
    },
    {
        "id": "tool.repository.inspect",
        "kind": "tool",
        "name": "Repository inspector",
        "description": "Extract declared versions, dependencies, images, and pipeline steps.",
        "tags": ["read", "repository"],
    },
    {
        "id": "tool.jira.read",
        "kind": "tool",
        "name": "Jira issue reader",
        "description": "Read an issue from a configured Jira Cloud project.",
        "tags": ["read", "jira"],
    },
    {
        "id": "tool.jira.comment",
        "kind": "tool",
        "name": "Jira comment writer",
        "description": "Write an approved incident draft as a Jira comment.",
        "tags": ["write", "approval", "jira"],
    },
    {
        "id": "workflow.knowledge",
        "kind": "workflow",
        "name": "Corporate knowledge",
        "description": "Authorize, retrieve evidence, evaluate, and return a cited answer.",
        "tags": ["knowledge", "read-only"],
    },
    {
        "id": "workflow.report",
        "kind": "workflow",
        "name": "Technical inventory",
        "description": "Inspect supplied files, evaluate evidence coverage, and return a report.",
        "tags": ["repository", "read-only"],
    },
    {
        "id": "workflow.incident",
        "kind": "workflow",
        "name": "Operational incident",
        "description": "Read incident context, draft a response, and wait for approval.",
        "tags": ["incident", "human-approval"],
    },
]

DOCUMENTS = [
    {
        "title": "Manual de gastos de viaje",
        "scope": "expenses",
        "source_uri": "seed://expenses/travel",
        "tags": ["gastos", "viajes", "justificantes"],
        "content": (
            "Los gastos de viaje se justifican entregando el formulario de gastos y los "
            "recibos originales dentro de los diez días laborables siguientes al viaje. "
            "El empleado debe indicar el centro de coste y el motivo del desplazamiento. "
            "Los gastos de transporte requieren billete o factura. Las dietas deben "
            "respetar los límites de la política de viajes."
        ),
    },
    {
        "title": "Política de viajes y aprobaciones",
        "scope": "travel",
        "source_uri": "seed://travel/approval",
        "tags": ["viajes", "aprobación", "responsables"],
        "content": (
            "El responsable directo aprueba el desplazamiento antes de realizar la "
            "reserva. Las excepciones de presupuesto requieren aprobación del responsable "
            "del área. El reembolso se tramita después de la revisión de los recibos y "
            "del centro de coste."
        ),
    },
    {
        "title": "Política de acceso a nóminas",
        "scope": "payroll",
        "source_uri": "seed://payroll/access",
        "tags": ["nóminas", "acceso restringido"],
        "content": (
            "La información de nóminas contiene datos personales y solo es accesible "
            "para el empleado titular y el equipo autorizado de Recursos Humanos."
        ),
    },
]


def seed_database(session: Session, config: Settings | None = None) -> None:
    config = config or settings
    if session.query(Capability).count() == 0:
        for item in CAPABILITIES:
            session.add(Capability(**item))
    if session.query(KnowledgeDocument).count() == 0:
        for item in DOCUMENTS:
            session.add(
                KnowledgeDocument(
                    **item,
                    embedding=embedding_for(f"{item['title']} {item['content']}", config),
                    updated_at=datetime.now(UTC),
                )
            )
    session.commit()
