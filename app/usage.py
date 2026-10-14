from __future__ import annotations

from collections import defaultdict
from typing import Any


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


def _nonnegative_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _nonnegative_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return float(value)
