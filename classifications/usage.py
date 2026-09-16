"""Persistent request metadata and USD estimates, never prompts or email content."""

import time
from typing import Any

from django.db.models import Count, Q, Sum

from classifications.models import AIRequest

# Standard text pricing, verified 2026-09-11 against OpenAI's model documentation.
# https://developers.openai.com/api/docs/models/gpt-5.6-luna
PRICING = {
    "gpt-5.6-luna": {
        "input": 0.20,
        "cached_input": 0.02,
        "cache_write": 0.25,
        "output": 1.20,
        "long_context_threshold": 272_000,
        "long_input_multiplier": 2,
        "long_output_multiplier": 1.5,
    }
}
PAGE_SIZE = 20


def start(config: dict[str, Any], message_count: int) -> int:
    return AIRequest.objects.create(
        started_at=int(time.time() * 1000),
        model=config["model"],
        reasoning=config["reasoning"],
        message_count=message_count,
        status="running",
        pricing=PRICING.get(config["model"]),
    ).pk


def estimate(tokens: dict[str, Any], pricing: dict[str, Any] | None) -> float | None:
    # Unknown models or absent usage must never look like a free request.
    if not pricing or not tokens:
        return None
    counts: list[Any] = [
        tokens.get("input_tokens"),
        tokens.get("output_tokens"),
        (tokens.get("input_tokens_details") or {}).get("cached_tokens", 0),
        (tokens.get("input_tokens_details") or {}).get("cache_write_tokens", 0),
    ]
    # Incomplete/inconsistent usage cannot support a reliable cost estimate.
    if any(type(value) is not int or value < 0 for value in counts):
        return None
    incoming, outgoing, cached, written = counts
    if cached + written > incoming:
        return None
    long = incoming > pricing["long_context_threshold"]
    # Reasoning is already included in output_tokens; cache reads/writes are subsets of input.
    return (
        (
            (incoming - cached - written) * pricing["input"]
            + cached * pricing["cached_input"]
            + written * pricing["cache_write"]
        )
        * (pricing["long_input_multiplier"] if long else 1)
        + outgoing
        * pricing["output"]
        * (pricing["long_output_multiplier"] if long else 1)
    ) / 1_000_000


def finish(request_id: int, status: str, response: Any, error_kind: str | None) -> None:
    tokens = (
        response.usage.model_dump() if response is not None and response.usage else {}
    )
    pricing = AIRequest.objects.values_list("pricing", flat=True).get(pk=request_id)
    AIRequest.objects.filter(pk=request_id).update(
        finished_at=int(time.time() * 1000),
        status=status,
        response_id=response.id if response is not None else None,
        input_tokens=tokens.get("input_tokens"),
        cached_tokens=(tokens.get("input_tokens_details") or {}).get("cached_tokens"),
        cache_write_tokens=(tokens.get("input_tokens_details") or {}).get(
            "cache_write_tokens"
        ),
        output_tokens=tokens.get("output_tokens"),
        reasoning_tokens=(tokens.get("output_tokens_details") or {}).get(
            "reasoning_tokens"
        ),
        cost_usd=estimate(tokens, pricing),
        error_kind=error_kind,
    )


def recover() -> None:
    """Mark unfinished attempts only when the dedicated worker restarts."""
    # A worker can stop after sending a request but before receiving its usage.
    AIRequest.objects.filter(status="running").update(
        status="interrupted", error_kind="process_stopped"
    )


def history(before: int | None = None) -> dict[str, Any]:
    summary = AIRequest.objects.aggregate(
        total_usd=Sum("cost_usd", default=0.0),
        request_count=Count("pk"),
        running_count=Count("pk", filter=Q(status="running")),
        unknown_cost_count=Count(
            "pk", filter=Q(cost_usd__isnull=True) & ~Q(status="running")
        ),
    )
    records = AIRequest.objects.order_by("-id")
    # Subsequent pages exclude newer requests without changing the global summary.
    if before is not None:
        records = records.filter(id__lt=before)
    rows = list(records.values()[: PAGE_SIZE + 1])
    return {
        "summary": summary,
        "requests": rows[:PAGE_SIZE],
        "next_before": rows[PAGE_SIZE - 1]["id"] if len(rows) > PAGE_SIZE else None,
    }
