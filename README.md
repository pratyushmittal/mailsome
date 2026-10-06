# Mailsome

A personal Gmail client to manage a busy inbox without missing what matters.

- Custom classifications
- Add sender based rules
- Describe label categories and email importance for Jev
- Custom tabs
- Sender Bundling
- View list in open mode [upcoming]

## Stack

Django 6 templates/forms, SQLite, and small vanilla JavaScript enhancements.
Gmail APIs provide mail access; two periodic worker threads handle
sync and classification alongside the web server. No task broker or external scheduler.

## Development

Install Python 3.13, `uv`, and `just`. Install Node.js to run keyboard tests.

```sh
uv sync --locked
uv run pre-commit install
just run                  # Migrate and start web + both workers
just test                 # Or: just test -k search
uv run pre-commit run --all-files
```

Open `http://localhost:8002`. Ctrl+C stops the server and both workers. There is no hot reload;
restart after code changes. Run `uv lock` after changing dependencies.
`just run` prints request logs and local error tracebacks; browser errors stay sanitized.

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
  only when requested. Opening mail does not mark it read. The reader preloads
  the next mail in the list, so moving on or archiving with `d` opens it quickly.
- **Labels:** sender rules and AI write labels to Gmail. History sync picks up
  those changes; the local display may briefly lag behind a successful write.
- **Storage:** downloaded mail stays in `data/mailsome.sqlite3`, including older
  and archived messages. There is no fixed message-count window or background
  inbox preload. Sender rules and optional AI operate on locally cached inbox
  mail; browsing can make previously unloaded inbox mail eligible.
- **Changes:** status shows the last successful sync and pending classifications,
  polling roughly once a minute. Reload to see changed mail; **Refresh** requests
  an earlier sync.

## AI settings

Enable AI in **Settings**, using a TypeSafe API key entered there or supplied through
`TYPESAFE_API_KEY`. Local configuration lives in `data/typesafe.json`.

- Each label has an editable description and acceptance threshold (default **0.75**).
- **Edit importance settings** defines ordered scoring levels and the **Important**
  list badge threshold (default: score **>0.7**). The reader sidebar shows the **0–1**
  score. Threshold changes apply immediately; inbox sorting is unchanged.
- Classification criteria edits affect future classifications; use **Reclassify
  cached inbox** to revisit stored scores and labels.
- Usage history records outcomes, tokens, and estimated costs—not mail or credentials.
  Unknown costs remain unknown.

## Background workers

- **Two workers** run alongside the web server with `just run`: sync and AI
  classification. Each runs independently at roughly one-minute intervals.
  Opening a page does not start workers; downloaded mail is picked up on a later pass.
- **Sync — on connection, startup, and roughly every minute:** read changes since
  the saved cursor. Fetch details for newly encountered emails and update labels
  for cached emails affected by changes. Do not list or preload existing inbox mail.
  Refresh, rule edits, and successful label writes can request an earlier pass.
  Browsing and sync persist downloaded messages directly through `inbox/utils.py`;
  downloads use batches of up to 50.
- **Expired history:** capture a fresh cursor, then fetch arrivals since the last
  successful sync minus one minute. Older label changes and deletions are not
  recovered. Save the replacement cursor and timestamp after persistence succeeds.
- **Sender rules — after sync:** compare cached inbox emails with sender rules
  and add missing labels in bulk. This runs even when AI is disabled or paused.
- **AI — independently scans stored mail:** if enabled, process up to 100 emails
  per pass with TypeSafe's SDK and `jev-1.13.0`, one email request at a time. Each
  request includes a Noul per enabled label and an importance Score, even without
  enabled labels. Send stored text, metadata, recipients, attachment names, and
  mail context; exclude HTML and attachment contents. Continue passes while
  eligible mail remains. Archived mail, spam, trash, drafts, and messages without
  stored bodies are excluded.
- **Reclassify cached inbox:** explicitly reset the selected AI-enabled labels,
  preserving assignments justified by current sender rules. Other assignments of
  those labels, including manual ones, are removed before AI runs again. Confirmation
  resets labels and scores for the regular worker, including importance-only setups.
  Failed or interrupted resets require confirmation again.
- **Retries:** keep successful downloads and advance the history cursor after each
  saved history page, so a failed pass resumes where it stopped. Downloads pause
  between batches to stay within Gmail's per-user quota, and quota limits cause
  a wait before retrying. Failed or interrupted AI requests retry automatically,
  up to three total attempts per email across restarts; retries can incur charges.
  Saved decisions retry label writes without another model call, including on
  idle passes.

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
| Reader | `d` | Archive the entire thread and open the next mail in the list; do not mark it read |
| Reader | `r` | Open the message in Gmail to reply there |
| Reader | `grr` | View all mail from the selected sender |
| Reader | `m` | Edit sender label rules |
| Reader | `n` | Add/edit a sender note |
| Reader | `u` | Show the advertised unsubscribe option |

Reply opens Gmail, not an in-app composer or automatically focused reply box.
Unsubscribe is an external handoff: finish it, then confirm in Mailsome to add
its `unsubscribed` label and sender rule.
