import re
from collections.abc import Callable
from functools import lru_cache

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from app.schemas import Principal

bearer = HTTPBearer(auto_error=False)


@lru_cache(maxsize=8)
def _jwks_client(issuer: str) -> PyJWKClient:
    return PyJWKClient(f"{issuer.rstrip('/')}/protocol/openid-connect/certs")


def _normalize_role(value: object) -> str:
    return re.sub(r"[^a-z0-9_-]", "", str(value).lower())


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

        configured = request.app.state.settings.api_bearer_token or ""
        if not hmac.compare_digest(token, configured):
            raise HTTPException(status_code=401, detail="Invalid bearer token.")
        return Principal(
            subject="api-key",
            roles=frozenset({"admin"}),
            scopes=frozenset({"*"}),
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
    scope_claim = claims.get("agentic_scopes", [])
    if isinstance(scope_claim, str):
        scope_claim = scope_claim.split()
    return Principal(
        subject=str(claims["sub"]),
        email=claims.get("email"),
        roles=frozenset(_normalize_role(role) for role in raw_roles),
        scopes=frozenset(str(scope) for scope in scope_claim),
    )


def require_roles(*roles: str) -> Callable:
    allowed = frozenset(_normalize_role(role) for role in roles)

    def check(principal: Principal = Depends(get_principal)) -> Principal:
        if not principal.roles.intersection(allowed):
            raise HTTPException(status_code=403, detail="Insufficient role.")
        return principal

    return check
