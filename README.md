# Mailsome

A personal Gmail client to manage a busy inbox without missing what matters.

## Objectives

- **Gmail label tabs:** Important emails, humans, favorite newsletters, paper
  trail, OTPs, and promotions. Assign labels through sender rules or optional AI.
- **Gmail search:** Use Gmail's own search syntax through its APIs and client libraries.
- **Sender context:** Expand an email into the main reading area with a narrow
  sidebar for the sender's previous conversations, inspired by Help Scout.

## Stack

Starlette for the backend, SQLite for local email storage, and Alpine.js for the
frontend. Gmail APIs and client libraries provide email access and search.

## Development

Install Python 3.13, `uv`, and `just`, then run:

```sh
uv sync --locked
uv run pre-commit install
just run         # Start the development server
just test        # Run pytest; accepts arguments, e.g. just test -k search
```

Dependencies are managed in `pyproject.toml` and `uv.lock`. Run `uv lock` when
editing dependencies. Run checks with `uv run pre-commit run --all-files`.

Add user-provided BDD scenarios to `tests/features/*.feature` and their
pytest-bdd bindings and steps to `tests/test_*.py`. Shared fixtures belong in
`tests/conftest.py` when needed.

## Connect Gmail

1. Create a Google Cloud project and enable the **Gmail API**.
2. Configure the OAuth consent screen for your personal app. While it is in
   **Testing**, add your Gmail address as a test user and request the
   `https://www.googleapis.com/auth/gmail.modify` scope.
3. Create an OAuth client of type **Web application** with this exact authorized
   redirect URI: `http://localhost:8002/auth/callback`.
4. Run `just run`, open `http://localhost:8002`, and click **Connect Gmail**.
   The page includes these setup steps and an upload form for the downloaded JSON.
5. Choose the file (no renaming needed) and click **Save and continue to Google**.
   Mailsome validates and saves it privately as `data/credentials.json`, then starts
   Google sign-in. You can also place the file there manually. Never commit it.

**Already connected with read-only access?** Add the `gmail.modify` scope to your
Google Cloud consent configuration, then use **Settings → Reconnect Gmail** and
approve the new permission. Your cache and existing account are preserved. Read-only
connections can still read mail and edit local notes, but cannot archive or assign labels. Gmail's modify
permission is broader than labeling; Mailsome does not implement sending or deleting.

Uploads are limited to 64 KB and must contain a Web application client with the
correct callback and Google OAuth endpoints. Existing credentials are not replaced.

Port 8002 avoids the existing local applications on ports 8000 and 8001. Use
`localhost`, not `127.0.0.1`, so sign-in and API requests share the same origin.
Google's Testing-mode Gmail refresh tokens expire after seven days; reconnect
when prompted. This token expiry is separate from Gmail's history retention.

### What gets downloaded

- Initial sync: inbox messages from the last 14 days, headers and labels only.
- Opening the app or clicking **Refresh**: changes since the last saved Gmail
  history ID, including read-status changes, archiving, and deletion.
- Expired history ID: rebuild only the same bounded inbox cache.
- Opening a message: fetch and cache its body. The reader is text-only; HTML is
  converted to inert text and remote images are not loaded. Attachments are not
  requested separately; Gmail can include inline parts in a full message response.
- Messages that leave the inbox or age out are removed from the local cache,
  never from Gmail. Older inbox messages are outside this view, not handled.

Cached mail appears before refreshing. Live progress shows the current phase,
completed header counts, elapsed time, and time since the last update. The browser
polls local progress once a second without making extra Gmail requests. Totals are
counted from listed IDs, not Gmail's estimated result count; the inbox updates only
after the sync commits. Failed syncs preserve the previous cache
and cursor, so retrying cannot skip changes. Opening mail here does not mark it
read in Gmail.

### Labels and interface

- Compact, colored label tabs sit above a single-column inbox. Each tab keeps its
  accent when moved, and the reader sidebar picks up the selected label's color.
  On narrow screens, the tabs scroll horizontally rather than wrapping.
- **Others** stays rightmost and shows recent inbox mail outside every configured tab.
  Unpinned Gmail labels do not exclude messages. Legacy query-tab matches also
  count, so Others needs Gmail access while those tabs remain. New mail can leave
  Others as sender rules or AI apply labels; uncategorized mail stays there.
- Drag label tabs to reorder; their positions survive reloads. Keyboard users can
  focus a tab and press **Alt + Left/Right**, or use **Edit label → Move left/right**
  (also available on touch screens). Reordering doesn't change labels or rerun AI.
- **+ Add** offers Label, Description, People, and Auto-classify.
- Choose an existing custom Gmail label or create one. Already pinned labels show
  an error. Removing a tab removes its local rules, not the Gmail label or messages.
  Label identities use Gmail IDs; rename labels in Gmail. The picker refreshes names.
- Sender addresses are exact, case-normalized matches. Rules run on sync and when
  saved, including existing recent inbox mail—not the full mailbox. These are local
  rules, not Gmail filters that run while the app is closed.
- In the inbox or reader, **Tab / Shift+Tab** cycles through mail tabs, including
  Others last. **1–9** jumps to that position in the current displayed order.
  **/** opens and focuses search. Shortcuts leave typing, label forms, and Settings
  alone. **Escape** from a focused mail tab moves to the toolbar for normal keyboard
  navigation.
- **Search** reveals and focuses the input. Submit to search the **whole recent
  inbox**, regardless of the selected label or Others. **Escape** or closing search
  clears it and restores the previous tab; choosing a tab also exits search.
  Gmail's own syntax is preserved. Label tabs themselves filter the cache without
  a network search. Older saved query tabs remain readable; saving one converts it
  to a label.
- Expand a message into the reader; **Back to mail** restores the list position.
  The sidebar shows compact labels/decisions, the sender address and note, actions,
  and up to five other individual emails, including messages in the same conversation.
  **View all** opens sender results across all dates, including archived mail (not
  spam/trash), in pages of 20. Older headers/bodies never expand the inbox cache.

### Mail actions

With an email open, use the sidebar buttons or keyboard shortcuts:

| Key | Action |
| --- | --- |
| `d` | Archive only this email, without deleting it or marking it read. |
| `grr` | View all emails from this sender (type the sequence without pausing). |
| `m` | Choose pinned labels to always apply to this exact sender, without AI. |
| `n` | Add/edit a local sender note; notes survive inbox cache pruning. |
| `u` | Show the unsubscribe option, when the email advertises a supported link. |

Shortcuts do not intercept typing, Settings, or open editors. Escape closes the
sender editor. Sender rules apply to recent and future inbox mail; unchecking a
label stops its sender rule but does not remove existing labels or disable AI.
Archiving updates SQLite only after Gmail confirms success, without changing the
history cursor.

Unsubscribe opens the sender's advertised HTTPS page or a prefilled `mailto:` in
your mail client, only when you click the link. Mailsome does not fetch these
untrusted URLs server-side or send unsubscribe emails. Finish the unsubscribe
externally, then choose **I've unsubscribed — label sender** to create/reuse the
`unsubscribed` Gmail label, pin its tab, and add the sender rule. Opening a link
alone is never recorded as a successful unsubscribe. Some lists require extra
steps on their website; unsupported links are not offered.

### Optional AI classification

1. Describe each label you want AI to assign and select **Auto-classify**.
2. In **Settings**, add your OpenAI API key and explicitly enable classification.
   This allows recent inbox text to be fetched before you open it and sent to OpenAI.
   Usage is billed to your OpenAI account; no live requests are made by the tests.
3. Start with `gpt-5.6-luna` at **medium** reasoning, or choose **high**. Model access
   depends on your OpenAI account. Refresh retries failed work.

Classification runs in the background, independently of inbox synchronization.
Sender labels apply without AI; AI adds zero or more enabled labels and never removes
labels. The classifier receives no tools. Attachments and older sender-history mail
are excluded; message text is treated as untrusted data. Requests use `store=False`.

Batches contain up to 25 messages, with an 80 KB JSON message budget, up to 24 KB of
encoded body text per message, and up to 32 KB of label metadata. Long text is
truncated, not fetched from attachments. Structured output is validated against the
allowed labels and the exact submitted message IDs before anything is applied.

Decisions—including empty classifications—are saved before additive Gmail writes.
A failed write retries without another paid classification. Changes to AI label
names/descriptions, enabled labels, or AI settings cause recent mail to be considered
again. Applied decisions are retained so another pass does not undo manual removals
within the cache's lifetime. Removing the local database also removes that memory.

Progress shows completed batch counts. Disabling AI stops subsequent downloads,
requests, and pending AI writes; an already sent request cannot be unsent. Sender
rules continue to work. Failures leave the inbox and history cursor available;
check permissions/key/model access and Refresh to retry.

### AI usage and request history

Open **Settings → AI usage** to see the cumulative **estimated USD cost** of
recorded classification requests. **Request history** shows newest requests first,
with explicit pagination for older entries: time, model/reasoning, batch size,
status, duration, response ID, input/cache/output/reasoning tokens, and cost.
Reopen Settings or use its usage Refresh button to update the total and latest page.

Tracking begins when this update initializes the database. Earlier costs were not
saved and cannot be reconstructed from the cached classifications. This is a local
estimate, **not your OpenAI invoice or your account-wide spending**. Requests that
have no returned usage, including some timeouts, parse failures, and interrupted
requests, show **Unknown** rather than zero; their unreported cost is excluded from
the displayed total. A failed request can still have a cost when usage was returned.

Estimates use reported tokens and a per-request snapshot of the model's standard
text rates in USD, including cached reads, cache writes, and long-context pricing.
Reasoning tokens are included in output tokens, not charged twice. Unknown models
have unknown cost. The tracker does not use callable-ai's fixed INR conversion;
account discounts, taxes, currency conversion, or future price changes can differ
from these estimates. Update the price table in `usage.py` when rates change;
historical entries keep their recorded estimates.

Each classification attempt is logged before the request. Hidden SDK retries are
disabled; use **Refresh** to retry a failed classification. Retrying an already
saved decision's Gmail write adds no AI request or cost. A completed log entry
means a structured API response was received, not that Gmail labels were applied.

Logs contain **metadata only**—no API keys, prompts, email text, sender addresses,
message IDs, full responses, or raw provider errors. They survive inbox pruning
and restarts; deleting `data/mail.sqlite3` removes them and resets the tracked total.

### Local data and security

This is a **single-account, local-only** app, not a hosted service. Keep one server
worker bound to loopback; do not expose it to the network or use a public tunnel.
There is no separate app login: anyone with access to this machine's local
server can use the connected account.

`data/` contains the SQLite cache, label settings/decisions and usage history, OAuth tokens,
client credentials, the OpenAI key/settings in `ai.json`, and the session signing key. The directory is restricted to its owner and token/cache files have
owner-only permissions. Data is **not encrypted at rest**; use a trusted machine
and disk encryption. OAuth tokens and saved OpenAI keys never go to browser storage or cookies.
The settings API returns only whether an OpenAI key is saved, not the key.
The development command disables access logs to avoid logging callback codes.

Reconnect uses the same account and preserves its cache. To start over or switch
accounts, stop the server and remove `data/mail.sqlite3`, `data/token.json`, and `data/ai.json` (if present).
This only removes local state. To revoke access, also remove Mailsome's access in
your Google account's third-party connections settings.

Tests use fake Gmail responses and temporary databases; they don't access your
mailbox. `tests/features/inbox.feature` covers the agreed phase-one flows.
Alpine.js is vendored under `static/vendor/` with its MIT license, so rendering
mail does not load scripts from a third-party CDN.


### UI font

The UI and Gmail setup page use the supplied **AT Name Sans variable** font,
served locally as `static/fonts/ATNameSansVariableTrial.woff2`. It retains the
weight (1–1000), optical-size (12–72), and italic axes. System fonts provide a
fallback while it loads and for unsupported characters; no font CDN is used.

The WOFF2 asset was compressed from
`at-name-sans-font-family/ATNameSansVariableTrial-Regular.ttf`; the supplied files
are unchanged. The bundled license note is copied alongside the web font and
identifies this as **Demo / Trial**. Confirm the appropriate web-use license before
publishing or distributing the app with this font.
