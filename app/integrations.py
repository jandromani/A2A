"""First-party GitHub and Gmail connectors with approval-gated writes."""

import base64
import re
import threading
import time
from email.message import EmailMessage
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import Settings
from app.event_bus import publish_event, write_audit
from app.models import ApprovalRecord, TaskRecord
from app.policy import authorize
from app.schemas import GitHubCommentCreate, GmailMessageCreate, Principal
from app.secrets import resolve_secret

_GITHUB_OWNER = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_GITHUB_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_GITHUB_ISSUE = re.compile(r"^[1-9][0-9]{0,8}$")
_GMAIL_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_google_token_lock = threading.Lock()
_google_token: tuple[str, float, str] | None = None


def _require(config: Settings, principal: Principal, action: str, scope: str) -> None:
    allowed, reason = authorize(principal, action, {"scope": scope}, config)
    if not allowed:
        raise HTTPException(status_code=403, detail=reason)


def _github_token(config: Settings, *, required: bool) -> str | None:
    value = resolve_secret(config, config.github_token)
    if required and not value:
        raise HTTPException(status_code=424, detail="GITHUB_TOKEN is required for GitHub writes.")
    return value


def _github_headers(config: Settings, *, write: bool = False) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = _github_token(config, required=write)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if write:
        headers["Content-Type"] = "application/json"
    return headers


def _github_get(config: Settings, path: str, params: dict[str, Any] | None = None) -> Any:
    try:
        response = httpx.get(
            f"https://api.github.com/{path.lstrip('/')}",
            headers=_github_headers(config),
            params=params,
            timeout=15.0,
            follow_redirects=False,
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        raise HTTPException(status_code=502, detail=f"GitHub API returned HTTP {status}.") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="GitHub API request failed.") from exc


def github_issues(
    config: Settings,
    principal: Principal,
    owner: str,
    repository: str,
    state: str = "open",
    limit: int = 30,
) -> dict[str, Any]:
    if not _GITHUB_OWNER.fullmatch(owner) or not _GITHUB_REPO.fullmatch(repository):
        raise HTTPException(status_code=422, detail="Invalid GitHub owner or repository name.")
    if state not in {"open", "closed", "all"} or not 1 <= limit <= 100:
        raise HTTPException(status_code=422, detail="Invalid GitHub issue filter.")
    _require(config, principal, "integration.github.read", f"github:{owner}/{repository}")
    path = f"repos/{quote(owner, safe='')}/{quote(repository, safe='')}/issues"
    items = _github_get(config, path, {"state": state, "per_page": limit})
    return {
        "items": [
            {
                "number": item.get("number"),
                "title": item.get("title"),
                "state": item.get("state"),
                "html_url": item.get("html_url"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
                "user": (item.get("user") or {}).get("login"),
                "labels": [label.get("name") for label in item.get("labels", [])],
                "pull_request": "pull_request" in item,
                "body": (item.get("body") or "")[:4_000],
            }
            for item in items
            if isinstance(item, dict)
        ]
    }


def request_github_comment(
    session: Session,
    config: Settings,
    principal: Principal,
    owner: str,
    repository: str,
    issue_number: str,
    body: GitHubCommentCreate,
) -> dict[str, Any]:
    if not _GITHUB_OWNER.fullmatch(owner) or not _GITHUB_REPO.fullmatch(repository):
        raise HTTPException(status_code=422, detail="Invalid GitHub owner or repository name.")
    if not _GITHUB_ISSUE.fullmatch(issue_number):
        raise HTTPException(status_code=422, detail="Invalid GitHub issue number.")
    _require(config, principal, "integration.github.request_write", f"github:{owner}/{repository}")
    task = TaskRecord(
        scenario="integration",
        title=f"GitHub comment · {owner}/{repository}#{issue_number}",
        request_text=body.body[:12_000],
        status="awaiting_approval",
        actor=principal.subject,
        payload_json={"integration": "github", "owner": owner, "repository": repository},
        result_json={"external_write": "pending human approval"},
    )
    session.add(task)
    session.flush()
    approval = ApprovalRecord(
        task_id=task.id,
        status="pending",
        requested_by=principal.subject,
        decision_json={
            "source": "github",
            "draft": body.body,
            "owner": owner,
            "repository": repository,
            "issue_number": int(issue_number),
            "integration_scope": f"github:{owner}/{repository}",
        },
    )
    session.add(approval)
    session.flush()
    publish_event(
        session,
        config,
        task.id,
        "approval.requested",
        "A GitHub comment is waiting for human approval.",
        {"approval_id": approval.id, "source": "github"},
    )
    write_audit(
        session,
        principal.subject,
        "integration.github.comment.requested",
        "approval",
        approval.id,
        details={"owner": owner, "repository": repository, "issue_number": int(issue_number)},
    )
    session.commit()
    return {"approval_id": approval.id, "task_id": task.id, "status": approval.status}


def _gmail_access_token(config: Settings) -> str:
    global _google_token
    if not (
        config.google_client_id and config.google_client_secret and config.google_refresh_token
    ):
        raise HTTPException(
            status_code=424,
            detail="GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and GOOGLE_REFRESH_TOKEN are required.",
        )
    client_id = resolve_secret(config, config.google_client_id)
    client_secret = resolve_secret(config, config.google_client_secret)
    refresh_token = resolve_secret(config, config.google_refresh_token)
    if not client_id or not client_secret or not refresh_token:
        raise HTTPException(status_code=424, detail="Google OAuth secrets could not be resolved.")
    cache_key = f"{client_id}:{refresh_token}"
    with _google_token_lock:
        if _google_token and _google_token[1] > time.monotonic() and _google_token[2] == cache_key:
            return _google_token[0]
        try:
            response = httpx.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=15.0,
                follow_redirects=False,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(
                status_code=502, detail="Google OAuth token refresh failed."
            ) from exc
        token = payload.get("access_token")
        expires_in = payload.get("expires_in", 3600)
        if not isinstance(token, str) or not token:
            raise HTTPException(status_code=502, detail="Google OAuth returned no access token.")
        lifetime = max(60, int(expires_in) - 60) if isinstance(expires_in, int) else 3_540
        _google_token = (token, time.monotonic() + lifetime, cache_key)
        return token


def _gmail_request(
    config: Settings,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> Any:
    url = f"https://gmail.googleapis.com/gmail/v1/users/me/{path.lstrip('/')}"
    try:
        response = httpx.request(
            method,
            url,
            headers={"Authorization": f"Bearer {_gmail_access_token(config)}"},
            params=params,
            json=json_body,
            timeout=15.0,
            follow_redirects=False,
        )
        response.raise_for_status()
        return response.json() if response.content else {}
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Gmail API returned HTTP {exc.response.status_code}.",
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Gmail API request failed.") from exc


def gmail_search(
    config: Settings,
    principal: Principal,
    query: str,
    limit: int = 20,
) -> dict[str, Any]:
    if not 1 <= limit <= 100 or len(query) > 500:
        raise HTTPException(status_code=422, detail="Invalid Gmail search limit or query.")
    _require(config, principal, "integration.gmail.read", "gmail:read")
    payload = _gmail_request(
        config,
        "GET",
        "messages",
        params={"q": query, "maxResults": limit},
    )
    result = []
    for item in payload.get("messages", [])[:limit]:
        if not isinstance(item, dict) or not _GMAIL_ID.fullmatch(str(item.get("id", ""))):
            continue
        message = _gmail_request(
            config,
            "GET",
            f"messages/{quote(item['id'], safe='')}",
            params={"format": "metadata", "metadataHeaders": ["From", "To", "Subject", "Date"]},
        )
        headers = {
            header.get("name", "").lower(): header.get("value", "")
            for header in (message.get("payload") or {}).get("headers", [])
        }
        result.append(
            {
                "id": message.get("id"),
                "thread_id": message.get("threadId"),
                "from": headers.get("from"),
                "to": headers.get("to"),
                "subject": headers.get("subject"),
                "date": headers.get("date"),
                "snippet": (message.get("snippet") or "")[:1_000],
            }
        )
    return {"messages": result}


def _decode_gmail_body(payload: dict[str, Any]) -> str:
    parts = [payload]
    output: list[tuple[int, str]] = []
    while parts:
        part = parts.pop()
        data = (part.get("body") or {}).get("data")
        mime_type = part.get("mimeType", "")
        if data and mime_type in {"text/plain", "text/html"}:
            try:
                decoded = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
                    "utf-8", errors="replace"
                )
                output.append((0 if mime_type == "text/plain" else 1, decoded))
            except (ValueError, UnicodeError):
                continue
        parts.extend(item for item in part.get("parts", []) if isinstance(item, dict))
    return min(output, key=lambda item: item[0])[1][:30_000] if output else ""


def gmail_message(config: Settings, principal: Principal, message_id: str) -> dict[str, Any]:
    if not _GMAIL_ID.fullmatch(message_id):
        raise HTTPException(status_code=422, detail="Invalid Gmail message ID.")
    _require(config, principal, "integration.gmail.read", "gmail:read")
    message = _gmail_request(
        config,
        "GET",
        f"messages/{quote(message_id, safe='')}",
        params={"format": "full"},
    )
    headers = {
        header.get("name", "").lower(): header.get("value", "")
        for header in (message.get("payload") or {}).get("headers", [])
    }
    return {
        "id": message.get("id"),
        "thread_id": message.get("threadId"),
        "from": headers.get("from"),
        "to": headers.get("to"),
        "subject": headers.get("subject"),
        "date": headers.get("date"),
        "body": _decode_gmail_body(message.get("payload") or {}),
        "snippet": (message.get("snippet") or "")[:1_000],
    }


def request_gmail_send(
    session: Session,
    config: Settings,
    principal: Principal,
    body: GmailMessageCreate,
) -> dict[str, Any]:
    _require(config, principal, "integration.gmail.request_send", "gmail:send")
    task = TaskRecord(
        scenario="integration",
        title=f"Correo Gmail · {body.subject[:140]}",
        request_text=f"Enviar correo a {', '.join(body.to)}: {body.subject}"[:12_000],
        status="awaiting_approval",
        actor=principal.subject,
        payload_json={"integration": "gmail"},
        result_json={"external_write": "pending human approval"},
    )
    session.add(task)
    session.flush()
    approval = ApprovalRecord(
        task_id=task.id,
        status="pending",
        requested_by=principal.subject,
        decision_json={
            "source": "gmail",
            "draft": body.body,
            "to": body.to,
            "subject": body.subject,
            "integration_scope": "gmail:send",
        },
    )
    session.add(approval)
    session.flush()
    publish_event(
        session,
        config,
        task.id,
        "approval.requested",
        "A Gmail message is waiting for human approval.",
        {"approval_id": approval.id, "source": "gmail"},
    )
    write_audit(
        session,
        principal.subject,
        "integration.gmail.send.requested",
        "approval",
        approval.id,
        details={"recipients": len(body.to), "subject": body.subject[:120]},
    )
    session.commit()
    return {"approval_id": approval.id, "task_id": task.id, "status": approval.status}


def execute_approved_integration(
    session: Session,
    config: Settings,
    approval: ApprovalRecord,
) -> dict[str, Any]:
    """Perform a previously approved write in the caller's SQL transaction."""
    decision = approval.decision_json or {}
    source = decision.get("source")
    task = session.get(TaskRecord, approval.task_id)
    if not task:
        raise ValueError("Approved action task no longer exists.")

    if source == "github":
        owner = str(decision.get("owner", ""))
        repository = str(decision.get("repository", ""))
        issue_number = str(decision.get("issue_number", ""))
        if not _GITHUB_OWNER.fullmatch(owner) or not _GITHUB_REPO.fullmatch(repository):
            raise ValueError("Approved GitHub target is invalid.")
        if not _GITHUB_ISSUE.fullmatch(issue_number):
            raise ValueError("Approved GitHub issue number is invalid.")
        marker = f"<!-- agentic-approval:{approval.id} -->"
        comments_path = (
            f"repos/{quote(owner, safe='')}/{quote(repository, safe='')}"
            f"/issues/{issue_number}/comments"
        )
        existing = _github_get(config, comments_path, {"per_page": 100})
        if not any(
            marker in str(item.get("body", "")) for item in existing if isinstance(item, dict)
        ):
            comment = f"{decision.get('draft', '')}\n\n{marker}"
            try:
                response = httpx.post(
                    f"https://api.github.com/{comments_path}",
                    headers=_github_headers(config, write=True),
                    json={"body": comment},
                    timeout=15.0,
                    follow_redirects=False,
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise RuntimeError("Approved GitHub comment could not be published.") from exc
        effect = f"GitHub comment published to {owner}/{repository}#{issue_number}."
    elif source == "gmail":
        message = EmailMessage()
        message["To"] = ", ".join(str(item) for item in decision.get("to", []))
        message["Subject"] = str(decision.get("subject", ""))
        message["Message-ID"] = f"<agentic-{approval.id}@agentic.local>"
        message["X-Agentic-Approval-ID"] = approval.id
        message.set_content(str(decision.get("draft", "")))
        search = _gmail_request(
            config,
            "GET",
            "messages",
            params={
                "q": f"in:sent rfc822msgid:agentic-{approval.id}@agentic.local",
                "maxResults": 1,
            },
        )
        if not search.get("messages"):
            raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
            _gmail_request(config, "POST", "messages/send", json_body={"raw": raw})
        effect = f"Gmail message sent to {len(decision.get('to', []))} recipient(s)."
    else:
        raise ValueError("Unknown approved integration action.")

    approval.status = "approved"
    task.status = "completed"
    task.result_json = {
        **(task.result_json or {}),
        "approval": "approved",
        "external_effect": effect,
    }
    publish_event(
        session,
        config,
        task.id,
        "integration.action.completed",
        effect,
        {"approval_id": approval.id, "source": source},
    )
    write_audit(
        session,
        approval.reviewed_by or "system:integration-worker",
        "integration.action.completed",
        source,
        approval.id,
        details={"task_id": task.id},
    )
    return {"status": "completed", "task_id": task.id, "external_effect": effect}
