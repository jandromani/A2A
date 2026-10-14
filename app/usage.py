from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import case, distinct, func, or_, select
from sqlalchemy.orm import Session

from app.models import ModelUsageLedger, TaskRecord


def aggregate_model_usage(
    task_results: list[dict[str, Any] | None], *, pricing_configured: bool
) -> dict[str, Any]:
    """Aggregate persisted model metadata without exposing prompts or responses."""
    calls: list[dict[str, Any]] = []
    for result in task_results:
        usage = result.get("model_usage") if isinstance(result, dict) else None
        if isinstance(usage, dict) and usage.get("attempted") is True:
            calls.append(usage)

    def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
        successful = [item for item in items if item.get("succeeded") is True]
        inputs = [_nonnegative_int(item.get("input_tokens")) for item in successful]
        outputs = [_nonnegative_int(item.get("output_tokens")) for item in successful]
        costs = [_nonnegative_number(item.get("estimated_cost_usd")) for item in successful]
        has_unknown_call = len(successful) != len(items)
        cost_complete = (
            pricing_configured
            and not has_unknown_call
            and all(value is not None for value in costs)
        )
        return {
            "calls": len(items),
            "successful_calls": len(successful),
            "fallback_calls": sum(item.get("succeeded") is not True for item in items),
            "input_tokens": sum(value or 0 for value in inputs),
            "output_tokens": sum(value or 0 for value in outputs),
            "token_counts_complete": not has_unknown_call
            and all(value is not None for value in inputs + outputs),
            "estimated_cost_usd": round(sum(value or 0 for value in costs), 8)
            if cost_complete
            else None,
        }

    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for call in calls:
        provider = call.get("provider")
        model = call.get("model")
        provider_name = provider if isinstance(provider, str) else "unknown"
        model_name = model if isinstance(model, str) else "unknown"
        groups[(provider_name, model_name)].append(call)

    summary = summarize(calls)
    summary.update(
        {
            "sampled_tasks": len(task_results),
            "tasks_with_model_usage": sum(
                isinstance(result, dict)
                and isinstance(result.get("model_usage"), dict)
                and result["model_usage"].get("attempted") is True
                for result in task_results
            ),
            "pricing_configured": pricing_configured,
            "providers": [
                {"provider": provider, "model": model, **summarize(items)}
                for (provider, model), items in sorted(groups.items())
            ],
        }
    )
    return summary


def aggregate_usage_ledger(session: Session) -> dict[str, Any]:
    """Aggregate the complete immutable call ledger in SQL, independent of task volume."""
    rows = session.execute(
        select(ModelUsageLedger.provider, ModelUsageLedger.model)
        .group_by(ModelUsageLedger.provider, ModelUsageLedger.model)
        .order_by(ModelUsageLedger.provider, ModelUsageLedger.model)
    ).all()
    groups = [_ledger_group(session, provider, model) for provider, model in rows]
    total = _ledger_group(session)
    total["providers"] = groups
    total["sampled_tasks"] = session.scalar(select(func.count(TaskRecord.id))) or 0
    total["tasks_with_model_usage"] = (
        session.scalar(select(func.count(distinct(ModelUsageLedger.task_id)))) or 0
    )
    total["ledger_rows"] = total["calls"]
    total["period"] = "lifetime"
    return total


def list_usage_calls(session: Session, *, limit: int, cursor: str | None = None) -> dict[str, Any]:
    query = select(ModelUsageLedger).order_by(
        ModelUsageLedger.created_at.desc(), ModelUsageLedger.id.desc()
    )
    if cursor:
        boundary = session.get(ModelUsageLedger, cursor)
        if not boundary:
            raise ValueError("Usage cursor does not exist.")
        query = query.where(
            or_(
                ModelUsageLedger.created_at < boundary.created_at,
                (ModelUsageLedger.created_at == boundary.created_at)
                & (ModelUsageLedger.id < boundary.id),
            )
        )
    items = session.scalars(query.limit(limit + 1)).all()
    has_more = len(items) > limit
    items = items[:limit]
    return {
        "items": [
            {
                "id": item.id,
                "call_key": item.call_key,
                "task_id": item.task_id,
                "actor": item.actor,
                "provider": item.provider,
                "model": item.model,
                "attempted": item.attempted,
                "succeeded": item.succeeded,
                "input_tokens": item.input_tokens,
                "output_tokens": item.output_tokens,
                "estimated_cost_usd": item.estimated_cost_usd,
                "error_code": item.error_code,
                "created_at": item.created_at.isoformat(),
            }
            for item in items
        ],
        "next_cursor": items[-1].id if has_more and items else None,
    }


def _ledger_group(
    session: Session,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    condition = ModelUsageLedger.provider == provider if provider is not None else True
    if model is not None:
        condition = condition & (ModelUsageLedger.model == model)
    success = ModelUsageLedger.succeeded.is_(True)
    failed = ModelUsageLedger.succeeded.is_(False)
    result = session.execute(
        select(
            func.count(ModelUsageLedger.id),
            func.coalesce(func.sum(case((success, 1), else_=0)), 0),
            func.coalesce(func.sum(case((failed, 1), else_=0)), 0),
            func.coalesce(
                func.sum(case((success, func.coalesce(ModelUsageLedger.input_tokens, 0)), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(
                    case((success, func.coalesce(ModelUsageLedger.output_tokens, 0)), else_=0)
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (
                            success
                            & (
                                ModelUsageLedger.input_tokens.is_(None)
                                | ModelUsageLedger.output_tokens.is_(None)
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    case((success, func.coalesce(ModelUsageLedger.estimated_cost_usd, 0)), else_=0)
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    case((success & ModelUsageLedger.estimated_cost_usd.is_(None), 1), else_=0)
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (
                            ModelUsageLedger.input_rate_per_million.is_(None)
                            | ModelUsageLedger.output_rate_per_million.is_(None),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
        )
        .select_from(ModelUsageLedger)
        .where(condition)
    ).one()
    calls, successful, fallback, inputs, outputs, unknown_tokens = (
        int(result[index] or 0) for index in range(6)
    )
    costs = float(result[6] or 0)
    unknown_costs = int(result[7] or 0)
    unknown_rates = int(result[8] or 0)
    return {
        "provider": provider,
        "model": model,
        "calls": calls,
        "successful_calls": successful,
        "fallback_calls": fallback,
        "input_tokens": inputs,
        "output_tokens": outputs,
        "token_counts_complete": fallback == 0 and unknown_tokens == 0,
        "estimated_cost_usd": round(float(costs), 8)
        if calls > 0 and fallback == 0 and unknown_costs == 0 and unknown_rates == 0
        else None,
        "pricing_configured": calls > 0 and unknown_rates == 0,
    }


def _nonnegative_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _nonnegative_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return float(value)
