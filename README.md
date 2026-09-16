# Mailsome

A personal Gmail client to manage a busy inbox without missing what matters.

- Custom classifications
- Add sender based rules
- Tell LLMs about your classification rules
- Custom tabs
- Sender Bundling
- View list in open mode [upcoming]

## Stack

Django 6 templates/forms, SQLite, and small vanilla JavaScript enhancements.
Gmail APIs provide mail access; two supervised management-command loops handle
sync and classification. No task broker or external scheduler.

## Development

Install Python 3.13, `uv`, and `just`. Install Node.js to run keyboard tests.

```sh
uv sync --locked
uv run pre-commit install
just run                  # Migrate and start web + both workers
just test                 # Or: just test -k search
uv run pre-commit run --all-files
```

Open `http://localhost:8002`. Ctrl+C stops all processes. There is no hot reload;
restart after code changes. Run `uv lock` after changing dependencies.

BDD scenarios belong in `tests/features/`, with bindings in `tests/test_*.py`.
Tests use fake providers and temporary databases, not your mailbox.

## Workflow

- **Connect Gmail:** save a history cursor, which marks where to start checking
  for changes. This does not preload existing emails.
- **Message list:** selecting a tab asks Gmail for inbox emails with that label.
  **Others** shows inbox emails outside your pinned tabs. Search and sender
  history cover all dates, including archived mail. **Load more** fetches the
  next page. Missing messages are downloaded and saved; cached ones are reused.
- **Individual message:** opening an email reuses saved content and fetches any
  missing content and thread details needed for the view. Full-detail downloads
  save current labels, headers, text, and formatted content. Attachments download
  only when requested. Opening mail does not mark it read.
- **Labels:** sender rules and AI write labels to Gmail. History sync picks up
  those changes; the local display may briefly lag behind a successful write.
- **Storage:** downloaded mail stays in `data/mailsome.sqlite3`, including older
  and archived messages. There is no fixed message-count window or background
  inbox preload. Sender rules and optional AI operate on locally cached inbox
  mail; browsing can make previously unloaded inbox mail eligible.
- **Changes:** background updates offer a reload link rather than taking you away
  from your open email or editor. **Refresh** requests an earlier sync.

## Background workers

- **Two workers** run alongside the web server with `just run`: sync and AI
  classification. Both check for queued work about once a second when idle.
  Opening a page does not start workers; downloaded mail is picked up on a later pass.
- **Sync — on connection, startup, and roughly every minute:** read changes since
  the saved cursor. Fetch details for newly encountered emails and update labels
  for cached emails affected by changes. Do not list or preload existing inbox mail.
  Refresh, rule edits, and successful label writes can request an earlier pass.
- **Expired history:** save a fresh cursor and keep cached mail and AI decisions.
  Changes in the missed interval are not rebuilt. Cached labels may stay stale
  until a later detail read or history event updates that message.
- **Sender rules — after sync:** compare cached inbox emails with sender rules
  and add missing labels in bulk. This runs even when AI is disabled or paused.
- **AI — queued after successful sync and sender labeling:** if enabled,
  classify unprocessed cached inbox mail using your label descriptions and mail
  context, then apply labels in bulk. Continue batches without waiting for the
  next scheduled sync. Archived mail, spam, trash, and drafts are excluded.
- **Reclassify cached inbox:** explicitly reset the selected AI-enabled labels,
  preserving assignments justified by current sender rules. Other assignments of
  those labels, including manual ones, are removed before AI runs again. The sync
  worker retains interrupted resets for retry; AI waits until cached labels are refreshed.
- **Overlapping work:** message fetches and saves take turns with other mailbox
  operations. Sync releases the lock between messages, so a reader does not wait
  for an entire sync pass. Sync can run while an AI response is pending.
- **Retries:** keep successful downloads but advance the history cursor only when
  the pass succeeds. Gmail quota limits cause a wait before retrying. Failed or
  interrupted AI requests pause until **Refresh**, since they may have incurred a charge.

The database is `data/mailsome.sqlite3`. Stop the app and its workers before
applying migrations.

## Keyboard shortcuts

Mail navigation shortcuts leave typing, Settings, and editors alone. The explicit
**Alt+Shift+C** shortcut opens mail context from any app page, including editors.
On Settings or the context editor, it focuses the existing field without discarding its draft.

| Context | Key | Action |
| --- | --- | --- |
| Any app page | `Alt+Shift+C` | Edit mail context |
| Inbox/reader | `Tab` / `Shift+Tab` | Cycle label tabs |
| Inbox/reader | `1`–`9` | Jump to a tab by position |
| Inbox/reader | `/` | Open search |
| Mail list | `j` / `k`, `Enter` | Select next/previous mail, then open |
| Reader | `Escape` | Return to the list and restore position |
| Reader | `d` | Archive the entire thread; do not mark it read |
| Reader | `r` | Open the message in Gmail to reply there |
| Reader | `grr` | View all mail from the selected sender |
| Reader | `m` | Edit sender label rules |
| Reader | `n` | Add/edit a sender note |
| Reader | `u` | Show the advertised unsubscribe option |

Reply opens Gmail, not an in-app composer or automatically focused reply box.
Unsubscribe is an external handoff: finish it, then confirm in Mailsome to add
its `unsubscribed` label and sender rule.
