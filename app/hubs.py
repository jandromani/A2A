"""Unified, read-only indexes for the platform's reusable capability hubs."""

from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings
from app.connectors import list_connectors
from app.data_sources import list_data_sources
from app.services import list_capabilities


def list_hubs(session: Session, config: Settings) -> dict[str, list[dict[str, Any]]]:
    capabilities = list_capabilities(session)
    connectors = list_connectors(session, config)
    sources = list_data_sources(session, config)
    return {
        "agents": [item for item in capabilities if item["kind"] == "agent"]
        + [
            {
                "id": f"remote-agent.{item['id']}",
                "kind": "agent",
                "name": item["name"],
                "description": item["description"],
                "version": item["protocol_version"],
                "endpoint": item["endpoint"],
                "status": "active" if item["enabled"] else "disabled",
                "source": "remote-connector",
            }
            for item in connectors
            if item["kind"] == "a2a"
        ],
        "mcps": [item for item in capabilities if item["kind"] == "mcp"]
        + [
            {
                "id": f"remote-mcp.{item['id']}",
                "kind": "mcp",
                "name": item["name"],
                "description": item["description"],
                "version": item["protocol_version"],
                "endpoint": item["endpoint"],
                "status": "active" if item["enabled"] else "disabled",
                "source": "remote-connector",
            }
            for item in connectors
            if item["kind"] == "mcp"
        ],
        "tools": [item for item in capabilities if item["kind"] == "tool"],
        "apis": [item for item in capabilities if item["kind"] == "api"]
        + [
            {
                **item,
                "source": "data-source",
                "status": "active" if item["enabled"] else "disabled",
            }
            for item in sources
        ],
        "skills": [item for item in capabilities if item["kind"] == "skill"],
    }
