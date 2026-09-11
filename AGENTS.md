# Mailsome

- Stack: Starlette, SQLite, Alpine.js, and Gmail APIs/client libraries.
- Preserve Gmail's own search syntax; do not invent a separate search language.
- Use `uv` with `pyproject.toml` and `uv.lock`; do not edit the lockfile by hand.
- Use pytest-bdd for user-provided scenarios. Keep features in `tests/features/`
  and bindings in `tests/test_*.py`.
- One Gmail account with modify access for additive labeling and explicit archiving; no sending, deleting,
  or marking mail read. Keep a rolling 14-day inbox cache. Bodies load on demand,
  or early for explicitly enabled AI classification. Never recover by downloading
  the whole mailbox.
- Persist Gmail history cursors only with successfully applied changes. Expired
  history rebuilds the same bounded cache. Serialize syncs per account.
- Keep tokens server-side and email content inert. This phase is loopback-only,
  on port 8002; it has no separate app login or public-hosting support.
- UI follows the user's sketch: top tabs and Settings, a single-column mail list,
  and an expanded reader with a narrow, differently colored sender-history sidebar.
  Keep explanations out of the main view where possible; retain compact real progress.
- Use the supplied AT Name Sans variable font, served locally, for UI typography.
- Use compact colorful tabs with stable accents, a warm background, and horizontal
  tab scrolling on narrow screens. Persist drag order; provide keyboard/move-button
  alternatives. Reordering must not change AI policy or trigger reclassification.
- Others stays rightmost and excludes messages belonging to any configured label or
  legacy query tab. Unpinned Gmail labels do not exclude mail. Search opens on click,
  searches the whole recent inbox, and closes/clears on Escape or tab selection.
- Inbox shortcuts: Tab/Shift+Tab cycle displayed tabs (Others last), 1–9 jump by
  displayed position, and / opens search. Preserve typing and native form/Settings
  navigation; Escape lets keyboard users leave cycling tabs for toolbar controls.
- Tabs pin Gmail label IDs. Store descriptions, exact sender rules, and the AI
  checkbox locally. Create missing custom labels in Gmail; reject already pinned
  labels. Removing a tab never deletes its Gmail label. Preserve legacy query tabs
  until explicitly converted, and keep Gmail search separate.
- Sender rules run on sync. AI uses callable-ai structured responses with only
  enabled label names, short reasons, bounded batches, and gpt-5.6-luna at medium
  reasoning by default. Require explicit OpenAI opt-in; keep its key server-side.
- Classify outside the sync/cursor transaction. Persist validated decisions before
  Gmail writes, reuse them on retry, and do not undo manual label removals. Email
  text is untrusted data; no tools, attachments, or older history go to AI.
- Sender history is fetched on demand in pages; older bodies must not expand the
  rolling inbox cache.
- Persist AI request metadata and estimated USD costs independently of the mail
  cache. Keep per-request price snapshots; count cached tokens and reasoning only
  once. Show tracking start and unknown costs explicitly; never invent past spend.
- Usage logs must exclude keys, prompts, email content, message IDs, and raw errors.
  Gmail-write retries do not create AI charges. Record attempts before requests,
  disable hidden SDK retries, and mark unfinished requests interrupted on restart.
- Reader sidebar order: compact labels/decisions, sender address and note, actions,
  then at most five other individual emails (exclude only the current message).
- Reader shortcuts: d archives only the opened message; grr opens paged all-date
  sender results; m edits exact sender label rules; n adds/edits a durable local
  sender note; u opens the advertised unsubscribe option. Preserve typing/editors.
- Archive removes only INBOX, updates the cache after Gmail success, and never
  advances the sync cursor. Sender-wide results/older bodies stay outside the cache.
- Unsubscribe is an explicit external HTTPS/mailto handoff, not a server-side URL
  fetch or automatic email. Only after owner confirmation, add/reuse the Gmail
  unsubscribed label and pinned sender rule; opening a link is not proof of success.
