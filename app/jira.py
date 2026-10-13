from typing import Any

import httpx

from app.config import Settings


class JiraConnectorError(RuntimeError):
    pass


class JiraConnector:
    def __init__(self, config: Settings):
        self.config = config

    @property
    def configured(self) -> bool:
        return bool(
            self.config.jira_base_url and self.config.jira_email and self.config.jira_api_token
        )

    def _client(self) -> httpx.Client:
        if not self.configured:
            raise JiraConnectorError("Jira is not configured.")
        return httpx.Client(
            base_url=self.config.jira_base_url.rstrip("/"),
            auth=(self.config.jira_email, self.config.jira_api_token),
            headers={"Accept": "application/json"},
            timeout=10.0,
        )

    def read_issue(self, issue_key: str) -> dict[str, Any]:
        try:
            with self._client() as client:
                response = client.get(
                    f"/rest/api/3/issue/{issue_key}",
                    params={"fields": "summary,status,assignee,priority"},
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as exc:
            raise JiraConnectorError(f"Jira issue read failed: {exc}") from exc
        fields = data.get("fields", {})
        status = fields.get("status") or {}
        assignee = fields.get("assignee") or {}
        priority = fields.get("priority") or {}
        return {
            "key": data.get("key", issue_key),
            "summary": fields.get("summary", ""),
            "status": status.get("name", "unknown"),
            "assignee": assignee.get("displayName"),
            "priority": priority.get("name"),
            "source": "jira",
        }

    def add_approved_comment(self, issue_key: str, comment: str) -> None:
        # Jira Cloud expects Atlassian Document Format for new comments.
        body = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": comment}],
                    }
                ],
            }
        }
        try:
            with self._client() as client:
                response = client.post(
                    f"/rest/api/3/issue/{issue_key}/comment",
                    json=body,
                    headers={"Content-Type": "application/json"},
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise JiraConnectorError(f"Jira comment write failed: {exc}") from exc
