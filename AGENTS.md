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

- `inbox/gmail.py` owns Gmail API calls and client construction. Callers retain policy and transaction boundaries. Expose only externally used, operation-named functions; keep internal helpers private.
- Preserve provider response types (including `Label` and `Profile`) instead of copying them into generic dictionaries. Use the provider's `Message`/`MessagePart` types at the Gmail boundary and `inbox.models.Message` in application code, not a third summary-dictionary representation.
- Fetch full message/thread details without field masks and populate missing body caches when persisting them. Preserve existing bodies; use label-only reads for mutable state of known messages.
- `message_from_gmail()` returns an unsaved model. Persist explicit metadata fields; never save that instance over downloaded bodies or AI completion.
- Load mail only through browsing and history events; no recurring inbox listing, preload, fixed message window, or per-message label-trust state. Missing/expired history captures a fresh cursor without deleting mail or rebuilding the missed interval.
- Persist labels from received message/thread details as well as history reads. Serialize fetch/save operations with the mailbox lock, releasing it between history messages. Callers must not nest this lock around helpers that acquire it.
- Successful Gmail label writes acknowledge AI decisions but do not edit cached labels; request history sync to observe the writes.

## Sender rules

- `Tab.people` is authoritative. Manage Gmail filters only on sender/label edits or unpinning, matching the previous complete criteria and actions exactly. No filter ownership model or reconciliation during mail sync.
- Apply sender labels to locally cached inbox mail, including after rule edits. Recompute missing labels without a backfill queue or sender pagination cursor.
- Reapply manually removed sender labels on cached inbox mail, but preserve existing labels when a rule is removed.

## AI classification

- Classify unprocessed locally cached inbox mail without an age/count window. Trust cached labels; fetch only missing bodies during preparation. Exclude archived mail, spam, trash, and drafts. Bulk-add labels and acknowledge decisions only after successful writes.
- Use short, deterministic batch-local message aliases, stable across correction attempts. Validate responses before mapping aliases back to real IDs for persistence.
- Budget tokens during batch preparation only; do not repeat checks for requests or correction history.
- Put optional user mail context in the system prompt, using the same settings snapshot for preparation and corrections. Context edits must preserve consent and completed classifications.
- Explicit reclassification resets selected AI-enabled labels on selected cached inbox mail, preserving current `Tab.people` matches per message/label pair. Manual assignments/removals of those labels may be replaced; unrelated labels and mail stay untouched.
- Keep a reset selection in the sync workflow until removals and label refresh succeed. Block AI meanwhile; only then clear selected decision history (including applied/legacy decisions) and reopen classification. Retry removals without another paid request.

## Database

- Use only `data/mailsome.sqlite3`; no import or backward-compatibility support for the previous app’s SQLite database is needed.
