"""Persistent request metadata and USD estimates, never prompts or email content."""

import time
from typing import Any

from django.db.models import Count, Q, Sum
from typesafe_sdk import SystemOneResponse

from classifications.models import AIRequest

# https://docs.typesafe.ai/models — USD per million input tokens; output is free.
PRICING = {"jev-1.13.0": {"input": 0.042}}
PAGE_SIZE = 20


def start(config: dict[str, Any], message_count: int) -> int:
    """Create a usage record before calling Jev."""
    return AIRequest.objects.create(
        started_at=int(time.time() * 1000),
        model=config["model"],
        message_count=message_count,
        status="running",
        pricing=PRICING.get(config["model"]),
    ).pk


def finish(
    request_id: int,
    status: str,
    response: SystemOneResponse | None,
    error_kind: str | None,
) -> None:
    values = {
        "finished_at": int(time.time() * 1000),
        "status": status,
        "input_tokens": None,
        "output_tokens": None,
        "pricing": None,
        "cost_usd": None,
        "error_kind": error_kind,
    }
    if response is not None:
        incoming = response.usage.input_tokens
        pricing = PRICING.get(response.model)
        values.update(
            model=response.model,
            input_tokens=incoming,
            output_tokens=response.usage.output_tokens,
            pricing=pricing,
            cost_usd=incoming * pricing["input"] / 1_000_000
            if incoming is not None and pricing
            else None,
        )
    AIRequest.objects.filter(pk=request_id).update(**values)


def recover() -> None:
    """Mark unfinished paid attempts when the app starts, before its AI worker."""
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
