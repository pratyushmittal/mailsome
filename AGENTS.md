# Persistent project memory

Keep durable user preferences, coding choices, and non-obvious constraints here
so they survive compaction and future sessions. This is not a changelog, task
tracker, or general project guide. Keep entries concise, replace obsolete choices,
and leave feature descriptions and implementation walkthroughs in README or code.

## Time zones

- Use `Asia/Kolkata` (IST) as the default and frontend display time zone. Keep stored timestamps and internal calculations timezone-safe; localize only for display.

## Utility modules

- Keep app-specific helpers in each Django app's `utils.py`; put helpers shared across apps in `mailsome/utilities.py`.

## Gmail and message boundaries

- `inbox/gmail.py` owns Gmail API calls and client construction. Callers retain policy. Keep operation-named functions, including standalone filter list/create/delete wrappers; keep internal helpers private.
- Preserve provider response types (including `Label` and `Profile`) instead of copying them into generic dictionaries. Use the provider's `Message`/`MessagePart` types at the Gmail boundary and `inbox.models.Message` in application code, not a third summary-dictionary representation.
- Fetch full message/thread details without field masks and populate missing body caches when persisting them. Preserve existing bodies; use label-only reads for mutable state of known messages.
- `message_from_gmail()` returns an unsaved model. Persist explicit metadata fields; never save that instance over downloaded bodies or AI completion.
- Load mail only through browsing and history events; no recurring inbox listing, preload, fixed message window, or per-message label-trust state. First connection captures a cursor without preloading. On expiry, anchor first, then fetch arrivals since the saved timestamp minus one minute; older changes/deletions are not recovered.
- Persist labels from received message/thread details as well as history reads. Producers persist directly through `inbox/utils.py`, with ordinary ORM calls. No queues, executors, futures, or explicit persistence/download transactions. Only background sync advances the cursor/timestamp, after saves succeed.
- Label writes request history sync; never edit cached labels directly. Sender writes never acknowledge AI decisions; the AI path acknowledges only its own writes, writing even labels the cache shows (it can lag). Resets remove labels and rely on sync; no confirmation reads.
- Download batches ≤50; retry at most twice for 500/502/503/504, 429, or 403 rateLimitExceeded/userRateLimitExceeded. No transport/DNS/SSL retries.
- Accept incomplete rows after interrupted initial saves; no special body-repair reads. Cursor replay or reloading browsing results provides recovery.

## Sender rules

- Sender policy belongs in `inbox/sender_filters.py`; provider operations stay in `inbox/gmail.py`.
- `Tab.people` is authoritative. Manage Gmail filters only on sender/label edits or unpinning, matching the previous complete criteria and actions exactly. No filter ownership model or reconciliation during mail sync.
- Apply sender labels to locally cached inbox mail, including after rule edits. Recompute missing labels without a backfill queue or sender pagination cursor.
- Reapply manually removed sender labels on cached inbox mail, but preserve existing labels when a rule is removed.

## AI classification

- Use TypeSafe's official SDK and live docs; load `typesafe-ai`. Keep one periodic worker, one email request in flight, and ≤100 emails per pass.
- Each request includes independent Nouls for enabled tabs plus importance Score. Put tab descriptions in instructions and explicit true/false criteria in `NoulCriteria`.
- Default tab acceptance to ≥0.75, editable per tab. Score importance even without enabled tabs; store normalized 0–1.
- Importance has an editable ordered rubric: OTP/login mail low; expiring subscriptions/meetings highest. List badges show “Important” above a global threshold (default >0.7); numeric scores belong in the reader sidebar.
- Check consent at pass start; let active passes finish. Saving rechecks only message existence/inbox eligibility; Gmail writes check current consent/tab eligibility.
- Save decisions before completion flags with ordinary ORM calls. Label updates and context/rubric/threshold edits never reopen completed classification.
- Increment attempts at the request boundary in `labeling.py`; `usage.py` only accounts for requests. Three total attempts, persisted across restarts; SDK retries disabled. Accept duplicate charges; unknown cost stays unknown.
- Classify unprocessed locally cached inbox mail without an age/count window. Require opt-in and stored bodies; empty text is valid. Trust cached labels and bodies; AI never downloads mail. Exclude archived mail, spam, trash, and drafts. Bulk-add labels and acknowledge decisions only after successful writes.
- Trust SDK parsing; resolve all answers before saving. No generated message IDs, dynamic schemas, or correction calls.
- Build questions once per pass. Pass content unchanged: no truncation or local size budgeting; SDK handles serialization.
- Pass optional mail context as named JSON user context, using the pass settings snapshot. Context edits must preserve consent and completed classifications.
- Explicit reclassification resets selected AI-enabled labels, decisions, completion, attempts, and importance on selected cached inbox mail (including importance-only setups), preserving current `Tab.people` matches per message/label pair. Manual assignments/removals of those labels may be replaced; unrelated labels and mail stay untouched.
- Reset directly on confirmation; failures require confirmation again. No reset workflow model, legacy backfill, failed-count dashboard, or per-email retry UI.

## Database

- Use only `data/mailsome.sqlite3`; no import or backward-compatibility support for the previous app’s SQLite database is needed.

- Use ordinary ORM writes for tabs, ordering, sender rules/notes, unsubscribe, and resets; no explicit transactions.
- `save_tab()` uses its initial tab lookup; no rereading or overlapping-edit detection after Gmail calls.

## Workers and reporting

- Independent sequential Gmail and AI loops; no workflow locks, queues, ingestion handoffs, or persisted progress flags. AI independently selects stored eligible mail; idle passes retry pending label writes.
- Poll at sync cadence (~one minute) for last successful sync and eligible classification count. Share the eligibility predicate; no stage/queue counts.
- Single-instance launch on port 8002: keep the port check, no process file lock.
- `just run` shows request logs and local error tracebacks; browser errors stay sanitized.

## Tests

- Test app behavior, not Django/SDK internals. Prefer compact integration flows and parameterized policy cases over exhaustive validation/security matrices.
- Keep executable BDD features grouped by test module; use direct present tense in every clause. Preserve cleanup; don't restore duplicate coverage.
- `test_pipeline.py`: ingestion triggers, cursor-on-failure, persistence, workers, retries, sync, and status. Fold mapping/body assertions into ingestion flows.
- `test_views.py`: tab/search/reader requests. `test_ui.py`: rendered controls and keyboard behavior. Don't repeat pipeline coverage there.

## Code and documentation

- Use `has_more_pages` for pagination, not `while True` plus a break.
- Keep README about current usage, not refactor history. Keep documentation edits concise and localized; preserve unrelated wording and structure.
