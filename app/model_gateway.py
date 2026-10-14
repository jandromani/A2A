from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from app.config import Settings
from app.secrets import resolve_secret

_PROVIDER_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "groq": "https://api.groq.com/openai/v1",
}


@dataclass(frozen=True, slots=True)
class ModelResult:
    text: str | None
    provider: str
    model: str | None
    attempted: bool
    succeeded: bool
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None
    fallback_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ModelGateway:
    """Multi-provider model gateway with deterministic workflow fallback."""

    def __init__(self, config: Settings):
        self.config = config

    @property
    def provider(self) -> str:
        return self.config.model_provider

    @property
    def base_url(self) -> str | None:
        if self.provider == "openai-compatible":
            return self.config.model_base_url
        if self.provider == "ollama":
            return self.config.model_base_url or self.config.ollama_base_url
        return self.config.model_base_url or _PROVIDER_URLS[self.provider]

    @property
    def api_key(self) -> str | None:
        key_setting = {
            "openai": self.config.openai_api_key,
            "anthropic": self.config.anthropic_api_key,
            "groq": self.config.groq_api_key,
        }.get(self.provider)
        if key_setting:
            return resolve_secret(self.config, key_setting)
        secret = self.config.model_api_key
        return resolve_secret(self.config, secret)

    @property
    def configured(self) -> bool:
        if not self.config.model_name or not self.base_url:
            return False
        return self.provider in {"openai-compatible", "ollama"} or bool(self.api_key)

    @property
    def configuration_reason(self) -> str | None:
        if not self.config.model_name:
            return "model_not_set"
        if not self.base_url:
            return "base_url_not_set"
        if self.provider in {"openai", "anthropic", "groq"} and not self.api_key:
            return "api_key_not_set"
        return None

    def _cost(self, input_tokens: int | None, output_tokens: int | None) -> float | None:
        input_rate = self.config.model_input_cost_per_million
        output_rate = self.config.model_output_cost_per_million
        if (
            input_tokens is None
            or output_tokens is None
            or input_rate is None
            or output_rate is None
        ):
            return None
        return round((input_tokens * input_rate + output_tokens * output_rate) / 1_000_000, 8)

    @staticmethod
    def _text_content(value: Any) -> str | None:
        if isinstance(value, str):
            text = value.strip()
            return text or None
        if isinstance(value, list):
            parts = [item.get("text", "") for item in value if isinstance(item, dict)]
            text = "".join(part for part in parts if isinstance(part, str)).strip()
            return text or None
        return None

    def _request(
        self, system_prompt: str, user_prompt: str
    ) -> tuple[str | None, int | None, int | None]:
        assert self.base_url and self.config.model_name
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            if self.provider == "anthropic":
                headers["x-api-key"] = self.api_key
                headers["anthropic-version"] = "2023-06-01"
            else:
                headers["Authorization"] = f"Bearer {self.api_key}"

        if self.provider == "anthropic":
            url = self.base_url.rstrip("/")
            if not url.endswith("/messages"):
                url += "/messages"
            body = {
                "model": self.config.model_name,
                "max_tokens": self.config.model_max_tokens,
                "temperature": 0.1,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            }
        else:
            url = self.base_url.rstrip("/")
            if not url.endswith("/chat/completions"):
                url += "/chat/completions"
            body = {
                "model": self.config.model_name,
                "max_tokens": self.config.model_max_tokens,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
            if self.provider in {"openai", "groq"}:
                body["max_completion_tokens"] = body.pop("max_tokens")
            if self.provider != "openai":
                body["temperature"] = 0.1

        response = httpx.post(url, headers=headers, json=body, timeout=25.0)
        response.raise_for_status()
        data = response.json()
        if self.provider == "anthropic":
            content = self._text_content(data.get("content"))
            usage = data.get("usage") or {}
            return content, _as_int(usage.get("input_tokens")), _as_int(usage.get("output_tokens"))
        choices = data.get("choices") or []
        content = self._text_content(choices[0].get("message", {}).get("content"))
        usage = data.get("usage") or {}
        return (
            content,
            _as_int(usage.get("prompt_tokens", usage.get("input_tokens"))),
            _as_int(usage.get("completion_tokens", usage.get("output_tokens"))),
        )

    def generate(self, system_prompt: str, user_prompt: str) -> ModelResult:
        if not self.configured:
            return ModelResult(
                text=None,
                provider=self.provider,
                model=self.config.model_name,
                attempted=False,
                succeeded=False,
                fallback_reason=self.configuration_reason or "not_configured",
            )

        attributes = {
            "gen_ai.system": self.provider,
            "gen_ai.request.model": self.config.model_name,
        }
        try:
            try:
                from opentelemetry import trace

                span_context = trace.get_tracer("agentic.model_gateway").start_as_current_span(
                    "model.generate", attributes=attributes
                )
            except ImportError:
                span_context = nullcontext()
            with span_context as span:
                text, input_tokens, output_tokens = self._request(system_prompt, user_prompt)
                if span is not None:
                    span.set_attribute("gen_ai.response.model", self.config.model_name or "")
                    if input_tokens is not None:
                        span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
                    if output_tokens is not None:
                        span.set_attribute("gen_ai.usage.output_tokens", output_tokens)
                    estimated_cost = self._cost(input_tokens, output_tokens)
                    if estimated_cost is not None:
                        span.set_attribute("gen_ai.usage.cost_usd", estimated_cost)
            if text:
                return ModelResult(
                    text=text,
                    provider=self.provider,
                    model=self.config.model_name,
                    attempted=True,
                    succeeded=True,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    estimated_cost_usd=self._cost(input_tokens, output_tokens),
                )
            reason = "invalid_response"
        except httpx.TimeoutException:
            reason = "timeout"
        except httpx.HTTPStatusError as exc:
            reason = f"http_{exc.response.status_code}"
        except httpx.InvalidURL:
            reason = "invalid_endpoint"
        except httpx.RequestError:
            reason = "transport_error"
        except (AttributeError, KeyError, IndexError, TypeError, ValueError):
            reason = "invalid_response"

        return ModelResult(
            text=None,
            provider=self.provider,
            model=self.config.model_name,
            attempted=True,
            succeeded=False,
            fallback_reason=reason,
        )

    def complete(self, system_prompt: str, user_prompt: str) -> str | None:
        """Compatibility helper for callers that only need generated text."""
        return self.generate(system_prompt, user_prompt).text


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def gateway_status(config: Settings) -> dict[str, str | bool | None]:
    gateway = ModelGateway(config)
    return {
        "configured": gateway.configured,
        "provider": gateway.provider,
        "model": config.model_name or "evidence rules",
        "secrets_loaded": bool(gateway.api_key),
        "configuration_reason": gateway.configuration_reason,
        "usage_pricing_configured": (
            config.model_input_cost_per_million is not None
            and config.model_output_cost_per_million is not None
        ),
    }
