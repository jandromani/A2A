from typing import Any

import httpx

from app.config import Settings


class ModelGateway:
    """Small OpenAI-compatible gateway; deterministic workflows remain available offline."""

    def __init__(self, config: Settings):
        self.config = config

    @property
    def configured(self) -> bool:
        return bool(self.config.model_base_url and self.config.model_name)

    def complete(self, system_prompt: str, user_prompt: str) -> str | None:
        if not self.configured:
            return None
        headers = {"Content-Type": "application/json"}
        if self.config.model_api_key:
            headers["Authorization"] = f"Bearer {self.config.model_api_key}"
        url = f"{self.config.model_base_url.rstrip('/')}/chat/completions"
        try:
            response = httpx.post(
                url,
                headers=headers,
                json={
                    "model": self.config.model_name,
                    "temperature": 0.1,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                },
                timeout=25.0,
            )
            response.raise_for_status()
            content: Any = response.json()["choices"][0]["message"]["content"]
            if isinstance(content, str) and content.strip():
                return content.strip()
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
            return None
        return None


def gateway_status(config: Settings) -> dict[str, str | bool]:
    configured = bool(config.model_base_url and config.model_name)
    return {
        "configured": configured,
        "provider": "OpenAI-compatible" if configured else "local deterministic",
        "model": config.model_name if configured else "evidence rules",
        "secrets_loaded": bool(config.model_api_key),
    }
