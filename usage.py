"""Persistent request metadata and USD estimates, never prompts or email content."""

import json
import time
from pathlib import Path
from typing import Any

from store import database

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


def start(directory: Path, config: dict[str, Any], message_count: int) -> int:
    with database(directory) as db:
        result = db.execute(
            """INSERT INTO ai_requests
               (started_at, model, reasoning, message_count, status, pricing)
               VALUES (?, ?, ?, ?, 'running', ?) RETURNING id""",
            (
                int(time.time() * 1000),
                config["model"],
                config["reasoning"],
                message_count,
                json.dumps(PRICING.get(config["model"])),
            ),
        )
        return result.fetchone()[0]


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


def finish(
    directory: Path, request_id: int, status: str, response: Any, error_kind: str | None
) -> None:
    tokens = (
        response.usage.model_dump() if response is not None and response.usage else {}
    )
    with database(directory) as db:
        pricing = json.loads(
            db.execute(
                "SELECT pricing FROM ai_requests WHERE id = ?", (request_id,)
            ).fetchone()[0]
        )
        db.execute(
            """UPDATE ai_requests SET finished_at = ?, status = ?, response_id = ?,
               input_tokens = ?, cached_tokens = ?, cache_write_tokens = ?, output_tokens = ?,
               reasoning_tokens = ?, cost_usd = ?, error_kind = ? WHERE id = ?""",
            (
                int(time.time() * 1000),
                status,
                response.id if response is not None else None,
                tokens.get("input_tokens"),
                (tokens.get("input_tokens_details") or {}).get("cached_tokens"),
                (tokens.get("input_tokens_details") or {}).get("cache_write_tokens"),
                tokens.get("output_tokens"),
                (tokens.get("output_tokens_details") or {}).get("reasoning_tokens"),
                estimate(tokens, pricing),
                error_kind,
                request_id,
            ),
        )


def recover(directory: Path) -> None:
    with database(directory) as db:
        # A process can stop after sending a request but before receiving its usage.
        db.execute(
            "UPDATE ai_requests SET status = 'interrupted', error_kind = 'process_stopped' WHERE status = 'running'"
        )


def history(directory: Path, before: int | None = None) -> dict[str, Any]:
    with database(directory) as db:
        summary = dict(
            db.execute(
                """SELECT COALESCE(SUM(cost_usd), 0) AS total_usd, COUNT(*) AS request_count,
               COALESCE(SUM(status = 'running'), 0) AS running_count,
               COALESCE(SUM(cost_usd IS NULL AND status != 'running'), 0) AS unknown_cost_count
               FROM ai_requests"""
            ).fetchone()
        )
        summary["tracking_started_at"] = db.execute(
            "SELECT started_at FROM ai_tracking WHERE id = 1"
        ).fetchone()[0]
        records = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM ai_requests WHERE (? IS NULL OR id < ?) ORDER BY id DESC LIMIT ?",
                (before, before, PAGE_SIZE + 1),
            )
        ]
    for record in records:
        record["pricing"] = json.loads(record["pricing"])
    return {
        "summary": summary,
        "requests": records[:PAGE_SIZE],
        "next_before": records[PAGE_SIZE - 1]["id"]
        if len(records) > PAGE_SIZE
        else None,
    }
