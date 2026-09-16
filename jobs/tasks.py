"""Two durable mailbox workflows; no serialized jobs or task backend."""

import random
import time

from django.conf import settings
from django.db import transaction

from accounts.models import Account
from classifications import labeling, usage
from classifications.models import AIRequest
from inbox import gmail
from jobs.models import Work
from jobs.runtime import set_progress
from mailsome.errors import APIError, error_response, gmail_retry_delay


def enqueue(kind: str, *, explicit: bool = False) -> str | None:
    # Only these two mailbox workflows are supported; no arbitrary jobs are persisted.
    if kind not in {"sync", "labeling"}:
        raise ValueError("Unknown mailbox workflow")
    with transaction.atomic():
        work, _ = Work.objects.get_or_create(kind=kind)
        # Auto-refresh must not replay a possibly billed/interrupted classification request.
        if kind == "labeling" and work.progress.get("needs_retry") and not explicit:
            return None
        work.pending = True
        work.retry_ai = work.retry_ai or explicit
        # Requests during a run remain pending without hiding its live progress.
        if work.progress.get("status") != "running" and work.retry_at <= int(
            time.time() * 1000
        ):
            work.progress = {
                "status": "queued",
                "revision": work.progress.get("revision", 0),
                "stage": "queued",
                "completed": 0,
                "total": None,
                "error": "",
                "needs_retry": False,
            }
        work.save()
        return kind


def periodic_sync() -> None:
    # Setup/disconnected accounts have no mailbox; active/manual syncs already cover this tick.
    if (
        Account.objects.filter(pk=1).exists()
        and (settings.DATA_DIR / "token.json").exists()
        and not Work.objects.filter(kind="sync", pending=True).exists()
        and not Work.objects.filter(kind="sync", progress__status="running").exists()
    ):
        enqueue("sync")


def run_work(kind: str) -> None:
    """Run at most one pass. The command owns this workflow's exclusive process lock."""
    with transaction.atomic():
        work = Work.objects.filter(kind=kind, pending=True).first()
        # Idle or paused workflows must not start provider calls.
        if (
            work is None
            or work.progress.get("needs_retry")
            or work.retry_at > int(time.time() * 1000)
        ):
            return
        work.pending = False
        work.progress = {
            "status": "running",
            "revision": work.progress.get("revision", 0),
            "stage": "connecting" if kind == "sync" else "classifying",
            "completed": 0,
            "total": None,
            "error": "",
            "needs_retry": False,
            "started_at": int(time.time() * 1000),
        }
        work.save()
    failure = {}
    cooldown = None
    more = False
    try:
        if kind == "sync":

            def report(stage: str, completed: int, total: int | None) -> None:
                set_progress(
                    "sync",
                    {
                        "status": "running",
                        "stage": stage,
                        "completed": completed,
                        "total": total,
                        "updated_at": int(time.time() * 1000),
                    },
                )

            with gmail.service() as client:
                gmail.sync(client, report=report)
                # The claimed selection stays durable until removals and their label refresh succeed.
                if work.reclassification:
                    labeling.reset_classifications(client, work.reclassification)
            report("sender rules", 0, None)
            labeling.apply_sender_rules()
            set_progress(
                "sync",
                {
                    "status": "complete",
                    "stage": "complete",
                    "updated_at": int(time.time() * 1000),
                },
            )
        else:
            more = labeling.process()
    except Exception as error:  # noqa: BLE001 — upstream diagnostics can expose mail or credentials.
        # Provider diagnostics may contain credentials or email content.
        # Store only a safe category; usage.py independently records each paid attempt.
        if kind == "sync":
            cooldown = gmail_retry_delay(error)
            import json

            response = error_response(None, error)
            detail = json.loads(response.content)["error"]
        elif isinstance(error, APIError):
            # APIError contains app-authored public guidance, never raw provider diagnostics.
            detail = error.detail
        elif isinstance(error, TimeoutError):
            # A timed-out request may still be billed; never imply a free automatic retry.
            detail = "Labeling timed out. The AI request may have been billed. Saved decisions are retained; Refresh to retry."
        else:
            detail = "Labeling paused. Saved decisions are retained; check settings and click Refresh to retry."
        failure = {
            "status": "failed",
            "error": detail,
            "error_status": response.status_code if kind == "sync" else 502,
            "error_kind": type(error).__name__
            if isinstance(error, APIError)
            else "workflow_failed",
            "needs_retry": kind == "labeling",
        }

    # IMMEDIATE transactions make completion, retry consent and follow-up requests atomic.
    with transaction.atomic():
        work = Work.objects.get(kind=kind)
        work.progress = {**work.progress, **failure}
        # Quota waits survive restarts and Refresh without holding the mailbox lock.
        if cooldown is not None:
            work.retry_delay = max(cooldown, min(work.retry_delay * 2, 300))
            work.retry_at = int(
                (time.time() + work.retry_delay + random.uniform(0, 1)) * 1000
            )
            work.pending = True
        elif not failure:
            work.retry_at = work.retry_delay = 0
        # A failed paid pass invalidates consent, including Refresh during an overlapping sync.
        if failure and kind == "labeling":
            work.pending = False
            Work.objects.filter(kind="sync").update(retry_ai=False)
        elif not failure and kind == "labeling":
            work.pending = work.pending or more
        # Successful sync offers fresh mail to classification without overriding a newer failure.
        if not failure and kind == "sync":
            enqueue("labeling", explicit=work.retry_ai)
        work.retry_ai = False
        # Refresh during this pass survives completion as one more pass, not another queue row.
        if work.pending:
            work.progress = {**work.progress, "status": "queued", "stage": "queued"}
        # Preserve a useful waiting state instead of disguising the cooldown as active work.
        if cooldown is not None:
            work.progress.update(
                stage="quota cooldown (automatic retry)",
                error=f"{failure['error']} Downloaded messages are saved; sync will retry automatically after the cooldown.",
                retry_at=work.retry_at,
            )
        # Only completed background changes offer a reload, not each individual progress update.
        if not failure:
            work.progress["revision"] = int(time.time() * 1000)
        work.save()


def recover_worker(kind: str) -> None:
    """Recover only this workflow, while its command holds the exclusive process lock."""
    with transaction.atomic():
        uncertain_ai = (
            kind == "labeling" and AIRequest.objects.filter(status="running").exists()
        )
        work, _ = Work.objects.get_or_create(kind=kind)
        # A stopped provider request may already be billed; never replay it automatically.
        if work.progress.get("status") == "running" or uncertain_ai:
            work.pending = False
            work.progress = {
                **work.progress,
                "status": "failed",
                "needs_retry": kind == "labeling",
                "error": "Work was interrupted. Click Refresh to retry."
                if kind == "sync"
                else "Work was interrupted. AI cost may be unknown; click Refresh to explicitly retry.",
            }
        # Consent from before a process restart cannot authorize an uncertain paid retry.
        work.retry_ai = False
        work.save()
        # The sync process must never recover a live classification process's usage.
        if kind == "labeling":
            # A queued sync cannot carry old retry consent across an interrupted AI request.
            if uncertain_ai:
                Work.objects.filter(kind="sync").update(retry_ai=False)
            usage.recover()
