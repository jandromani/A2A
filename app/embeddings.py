import hashlib
import math
import re
from typing import Any

import httpx

from app.config import Settings
from app.secrets import resolve_secret

DIMENSIONS = 384


def _first_secret(config: Settings, *values: Any) -> str | None:
    for value in values:
        secret = resolve_secret(config, value)
        if secret:
            return secret
    return None


def terms(text: str) -> list[str]:
    return re.findall(r"[a-z0-9áéíóúüñ]{2,}", text.lower())


def local_embedding(text: str) -> list[float]:
    """Create a deterministic feature-hash vector for offline lexical ranking."""
    vector = [0.0] * DIMENSIONS
    for term in terms(text):
        digest = hashlib.blake2b(term.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % DIMENSIONS
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def embedding_for(text: str, config: Settings) -> list[float]:
    if not config.embedding_model:
        return local_embedding(text)
    if config.embedding_provider == "openai":
        base_url = config.embedding_base_url or config.model_base_url or "https://api.openai.com/v1"
        api_key = _first_secret(
            config, config.embedding_api_key, config.openai_api_key, config.model_api_key
        )
    elif config.embedding_provider == "ollama":
        base_url = config.embedding_base_url or config.ollama_base_url
        api_key = _first_secret(config, config.embedding_api_key)
    else:
        base_url = config.embedding_base_url or config.model_base_url
        api_key = _first_secret(config, config.embedding_api_key, config.model_api_key)
    if not base_url:
        return local_embedding(text)
    headers = {"Content-Type": "application/json"}
    if config.embedding_provider == "openai" and not api_key:
        return local_embedding(text)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    url = base_url.rstrip("/")
    if not url.endswith("/embeddings"):
        url += "/embeddings"
    try:
        response = httpx.post(
            url,
            headers=headers,
            json={"model": config.embedding_model, "input": text, "dimensions": DIMENSIONS},
            timeout=20.0,
        )
        response.raise_for_status()
        value: Any = response.json()["data"][0]["embedding"]
        if len(value) == DIMENSIONS:
            vector = [float(item) for item in value]
            if all(math.isfinite(item) for item in vector):
                return vector
    except (httpx.HTTPError, httpx.InvalidURL, KeyError, IndexError, TypeError, ValueError):
        pass
    return local_embedding(text)


def lexical_score(query: str, content: str) -> float:
    query_terms = set(terms(query))
    if not query_terms:
        return 0.0
    content_terms = terms(content)
    counts = {term: content_terms.count(term) for term in query_terms}
    matched = sum(1 for count in counts.values() if count)
    return matched / len(query_terms) + sum(min(count, 3) for count in counts.values()) * 0.025
