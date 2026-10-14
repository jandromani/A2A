"""Append-only per-call model usage accounting."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import ModelUsageLedger, TaskRecord


def record_model_usage(
    session: Session,
    config: Settings,
    task: TaskRecord,
    call_key: str,
    usage: dict[str, Any] | None,
) -> ModelUsageLedger | None:
    if not isinstance(usage, dict) or usage.get("attempted") is not True:
        return None
    existing = session.scalar(select(ModelUsageLedger).where(ModelUsageLedger.call_key == call_key))
    if existing:
        return existing
    row = ModelUsageLedger(
        call_key=call_key,
        task_id=task.id,
        actor=task.actor,
        provider=str(usage.get("provider") or "unknown")[:80],
        model=str(usage.get("model") or "unknown")[:180],
        attempted=True,
        succeeded=usage.get("succeeded") is True,
        input_tokens=_valid_count(usage.get("input_tokens")),
        output_tokens=_valid_count(usage.get("output_tokens")),
        input_rate_per_million=config.model_input_cost_per_million,
        output_rate_per_million=config.model_output_cost_per_million,
        estimated_cost_usd=_valid_cost(usage.get("estimated_cost_usd")),
        error_code=(
            str(usage.get("fallback_reason"))[:80] if usage.get("fallback_reason") else None
        ),
    )
    session.add(row)
    session.flush()
    return row


def _valid_count(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _valid_cost(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return None
