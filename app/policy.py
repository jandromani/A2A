from typing import Any

import httpx

from app.config import Settings
from app.schemas import Principal

ROLE_ACTIONS = {
    "task.create": {"employee", "operator", "admin"},
    "task.cancel": {"operator", "admin"},
    "knowledge.read": {"employee", "operator", "approver", "admin"},
    "knowledge.write": {"admin"},
    "catalog.manage": {"admin"},
    "connector.invoke": {"approver", "admin"},
    "approval.decide": {"approver", "admin"},
    "audit.read": {"operator", "approver", "admin"},
    "evaluation.read": {"operator", "approver", "admin"},
}


def local_decision(
    principal: Principal,
    action: str,
    scope: str | None = None,
) -> tuple[bool, str]:
    if "admin" not in principal.roles and not principal.roles.intersection(
        ROLE_ACTIONS.get(action, set())
    ):
        return False, f"Role does not grant {action}."
    if scope and "*" not in principal.scopes and scope not in principal.scopes:
        return False, f"Scope {scope} is not granted."
    return True, "Allowed by local policy."


def authorize(
    principal: Principal,
    action: str,
    resource: dict[str, Any],
    config: Settings,
) -> tuple[bool, str]:
    if not config.opa_url:
        return local_decision(principal, action, resource.get("scope"))
    try:
        response = httpx.post(
            f"{config.opa_url.rstrip('/')}/v1/data/agentic/allow",
            json={
                "input": {
                    "actor": {
                        "sub": principal.subject,
                        "roles": sorted(principal.roles),
                        "scopes": sorted(principal.scopes),
                    },
                    "action": action,
                    "resource": resource,
                }
            },
            timeout=3.0,
        )
        response.raise_for_status()
        allowed = response.json().get("result") is True
        return allowed, "Allowed by OPA." if allowed else "Denied by OPA."
    except Exception:
        # An explicitly configured OPA service is authoritative; fail closed.
        return False, "OPA is unavailable; the policy check failed closed."
