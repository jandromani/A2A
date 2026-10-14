import re
from collections.abc import Callable
from datetime import UTC, datetime
from functools import lru_cache

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from sqlalchemy import select

from app.models import AgentIdentity
from app.schemas import Principal
from app.secrets import resolve_secret

bearer = HTTPBearer(auto_error=False)


@lru_cache(maxsize=8)
def _jwks_client(issuer: str) -> PyJWKClient:
    return PyJWKClient(f"{issuer.rstrip('/')}/protocol/openid-connect/certs")


def _normalize_role(value: object) -> str:
    return re.sub(r"[^a-z0-9_-]", "", str(value).lower())


def _items(value: str) -> frozenset[str]:
    return frozenset(item.strip() for item in re.split(r"[\s,]+", value) if item.strip())


def get_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> Principal:
    mode = request.app.state.settings.auth_mode
    if mode == "dev":
        actor = request.headers.get("x-actor", "local-admin").strip()[:200] or "local-admin"
        return Principal(subject=actor, roles=frozenset({"admin"}), scopes=frozenset({"*"}))

    token = credentials.credentials if credentials else ""
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if mode == "api_key":
        import hmac

        try:
            configured = (
                resolve_secret(
                    request.app.state.settings, request.app.state.settings.api_bearer_token
                )
                or ""
            )
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail="The authentication secret is unavailable."
            ) from exc
        if not hmac.compare_digest(token, configured):
            raise HTTPException(status_code=401, detail="Invalid bearer token.")
        return Principal(
            subject=request.app.state.settings.api_key_subject,
            roles=frozenset(
                _normalize_role(role) for role in _items(request.app.state.settings.api_key_roles)
            ),
            scopes=_items(request.app.state.settings.api_key_scopes),
        )

    config = request.app.state.settings
    try:
        issuer = config.oidc_issuer_url or ""
        signing_key = _jwks_client(issuer).get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=config.oidc_audience,
            issuer=issuer,
            options={"require": ["exp", "iss", "sub"]},
        )
    except Exception as exc:
        raise HTTPException(
            status_code=401,
            detail="Token could not be verified.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    realm_access = claims.get("realm_access") or {}
    raw_roles = list(realm_access.get("roles", [])) + list(claims.get("roles", []))
    roles = frozenset(_normalize_role(role) for role in raw_roles)
    scope_claim = claims.get("agentic_scopes", [])
    if isinstance(scope_claim, str):
        scope_claim = scope_claim.split()
    agent_id = claims.get(config.agent_identity_claim)
    if not agent_id and "agent" in roles:
        agent_id = claims.get("azp") or claims.get("client_id")
    if agent_id:
        factory = request.app.state.session_factory
        with factory() as session:
            identity = session.scalar(
                select(AgentIdentity).where(AgentIdentity.id == str(agent_id))
            )
            expires_at = identity.expires_at if identity else None
            if expires_at and expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if (
                not identity
                or identity.status != "active"
                or identity.revoked_at is not None
                or (expires_at is not None and expires_at <= datetime.now(UTC))
            ):
                raise HTTPException(status_code=401, detail="Agent identity is not active.")
            return Principal(
                subject=f"agent:{identity.id}",
                email=claims.get("email"),
                roles=frozenset(identity.roles_json or []),
                scopes=frozenset(identity.scopes_json or []),
            )
    return Principal(
        subject=str(claims["sub"]),
        email=claims.get("email"),
        roles=roles,
        scopes=frozenset(str(scope) for scope in scope_claim),
    )


def require_roles(*roles: str) -> Callable:
    allowed = frozenset(_normalize_role(role) for role in roles)

    def check(principal: Principal = Depends(get_principal)) -> Principal:
        if not principal.roles.intersection(allowed):
            raise HTTPException(status_code=403, detail="Insufficient role.")
        return principal

    return check
