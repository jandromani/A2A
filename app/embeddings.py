import hashlib
import math
import re
from typing import Any

import httpx

from app.config import Settings

DIMENSIONS = 384


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
    if not (config.embedding_base_url and config.embedding_model):
        return local_embedding(text)
    headers = {"Content-Type": "application/json"}
    if config.embedding_api_key:
        headers["Authorization"] = f"Bearer {config.embedding_api_key}"
    try:
        response = httpx.post(
            f"{config.embedding_base_url.rstrip('/')}/embeddings",
            headers=headers,
            json={"model": config.embedding_model, "input": text, "dimensions": DIMENSIONS},
            timeout=20.0,
        )
        response.raise_for_status()
        value: Any = response.json()["data"][0]["embedding"]
        if len(value) == DIMENSIONS:
            return [float(item) for item in value]
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
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
