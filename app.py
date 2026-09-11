"""Single-user, loopback-only Gmail client. Do not expose this server publicly."""

import asyncio
import json
import secrets
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from oauthlib.oauth2 import OAuth2Error
from requests.exceptions import RequestException
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import gmail
import labeling
import usage
from store import cache_message, database, initialize, message_summary

ROOT = Path(__file__).parent
ORIGIN = "http://localhost:8002"
REDIRECT_URI = f"{ORIGIN}/auth/callback"


def oauth_flow(directory: Path, **kwargs: str) -> Flow:
    path = directory / "credentials.json"
    # OAuth credentials must be supplied by the owner, not committed to the repo.
    if not path.exists():
        raise HTTPException(
            503,
            "Add your Google web OAuth credentials to data/credentials.json. See README.md.",
        )
    return Flow.from_client_secrets_file(
        str(path),
        scopes=gmail.SCOPES,
        redirect_uri=REDIRECT_URI,
        **kwargs,
    )


def home(request: Request) -> Response:
    # The OAuth callback and API requests must share the canonical localhost origin.
    if request.url.hostname != "localhost":
        return RedirectResponse(ORIGIN)
    return FileResponse(ROOT / "static" / "index.html")


def connect(request: Request) -> Response:
    # Use one host consistently so the session cookie reaches the OAuth callback.
    if request.url.hostname != "localhost":
        return RedirectResponse(f"{ORIGIN}/auth/connect")
    # First-time setup belongs in the browser, before Google authorization starts.
    if not (request.app.state.directory / "credentials.json").exists():
        return FileResponse(ROOT / "static" / "auth-setup.html")
    flow = oauth_flow(request.app.state.directory)
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    request.session["oauth_state"] = state
    request.session["oauth_verifier"] = flow.code_verifier
    return RedirectResponse(url)


async def upload_credentials(request: Request) -> Response:
    content = bytearray()
    async for chunk in request.stream():
        # Bound the actual stream, including uploads without a Content-Length header.
        if len(content) + len(chunk) > 64 * 1024:
            raise HTTPException(
                413, "Choose the Google client JSON file (maximum 64 KB)."
            )
        content.extend(chunk)
    return await run_in_threadpool(save_credentials, request, content)


def save_credentials(request: Request, content: bytearray) -> Response:
    try:
        config = json.loads(content)
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(
            400,
            "This is not a valid JSON file. Upload the file downloaded from Google Cloud.",
        ) from None

    web = config.get("web") if isinstance(config, dict) else None
    # Desktop clients, service-account keys, and token files cannot configure this web app.
    if not isinstance(web, dict) or not all(
        isinstance(web.get(key), str) and web[key].strip()
        for key in ("client_id", "client_secret", "auth_uri", "token_uri")
    ):
        raise HTTPException(
            400,
            "Choose a Web application OAuth client JSON with its client ID and secret, not a Desktop client, service-account key, or token file.",
        )
    # Untrusted uploads must never redirect users or send authorization codes to another host.
    if (
        web["auth_uri"]
        not in {
            "https://accounts.google.com/o/oauth2/auth",
            "https://accounts.google.com/o/oauth2/v2/auth",
        }
        or web["token_uri"] != "https://oauth2.googleapis.com/token"
    ):
        raise HTTPException(
            400,
            "The file must use Google's official OAuth authorization and token endpoints. Download it again from Google Cloud.",
        )
    # Google requires the callback to be registered on this particular client.
    if (
        not isinstance(web.get("redirect_uris"), list)
        or REDIRECT_URI not in web["redirect_uris"]
    ):
        raise HTTPException(
            400,
            f"Add {REDIRECT_URI} under Authorized redirect URIs in Google Cloud, save, and download the JSON again.",
        )

    with request.app.state.sync_lock:
        path = request.app.state.directory / "credentials.json"
        # A stale setup tab or concurrent upload must not overwrite the active OAuth client.
        if path.exists():
            raise HTTPException(
                409,
                "OAuth credentials are already configured. Return to Connect Gmail; to replace them, remove the existing local credentials.json first.",
            )
        gmail.private_write(path, json.dumps({"web": web}))
    return JSONResponse({"saved": True}, status_code=201)


def callback(request: Request) -> Response:
    expected = request.session.pop("oauth_state", None)
    verifier = request.session.pop("oauth_verifier", None)
    # Reject unsolicited callbacks before handling denial or exchanging any code.
    if not expected or not secrets.compare_digest(
        expected, request.query_params.get("state", "")
    ):
        raise HTTPException(
            400, "Invalid or expired sign-in state. Start Connect Gmail again."
        )
    # The user can decline consent without changing the existing account or cache.
    if "error" in request.query_params:
        raise HTTPException(400, "Gmail connection was cancelled. You can try again.")
    # A successful callback must include both the code and our original PKCE verifier.
    if not request.query_params.get("code") or not verifier:
        raise HTTPException(
            400, "Incomplete sign-in response. Start Connect Gmail again."
        )

    directory = request.app.state.directory
    with request.app.state.sync_lock:
        flow = oauth_flow(directory, state=expected, code_verifier=verifier)
        # Passing the code directly keeps local HTTP out of oauthlib's token URL checks.
        flow.fetch_token(code=request.query_params["code"], timeout=30)
        # Offline access is necessary for future refreshes after the access token expires.
        if not flow.credentials.refresh_token:
            raise HTTPException(
                400,
                "Google did not grant offline access. Reconnect Gmail and grant access.",
            )
        with build(
            "gmail", "v1", credentials=flow.credentials, cache_discovery=False
        ) as client:
            profile = client.users().getProfile(userId="me").execute(num_retries=2)
        with database(directory) as db:
            account = db.execute("SELECT email FROM account WHERE id = 1").fetchone()
            # Phase one supports reconnecting the same account, not silently replacing it.
            if account and account["email"] != profile["emailAddress"]:
                raise HTTPException(
                    409,
                    "A different Gmail account is already connected. Reconnect that account.",
                )
            gmail.private_write(directory / "token.json", flow.credentials.to_json())
            db.execute(
                "INSERT INTO account (id, email) VALUES (1, ?) ON CONFLICT(id) DO NOTHING",
                (profile["emailAddress"],),
            )
    return RedirectResponse("/", status_code=303)


def inbox(request: Request) -> Response:
    with database(request.app.state.directory) as db:
        account = db.execute(
            "SELECT email, synced_at FROM account WHERE id = 1"
        ).fetchone()
        messages = [
            {
                **dict(row),
                "labels": json.loads(row["labels"]),
                "sender_email": parseaddr(row["sender"])[1].casefold(),
            }
            for row in db.execute(
                """SELECT id, thread_id, sender, subject, received_at, labels
                   FROM messages WHERE received_at >= ? ORDER BY received_at DESC, id""",
                (gmail.cutoff_time(),),
            )
        ]
    query = request.query_params.get("q", "").strip()
    tab_id = request.query_params.get("tab")
    others = not query and not tab_id
    configured = labeling.tabs(request.app.state.directory)

    # Explicit search covers the whole recent cache, regardless of the selected tab.
    if not query and tab_id:
        tab = next((tab for tab in configured if str(tab["id"]) == tab_id), None)
        # Another browser may have removed the selected tab.
        if tab is None:
            raise HTTPException(404, "This label tab no longer exists. Select Others.")
        if tab["label_id"]:
            messages = [
                message for message in messages if tab["label_id"] in message["labels"]
            ]
        else:
            query = tab["query"]

    if others:
        pinned = {tab["label_id"] for tab in configured if tab["label_id"]}
        messages = [
            message
            for message in messages
            if not pinned.intersection(message["labels"])
        ]
        # Legacy queries still define membership. Empty Others needs no network evaluation.
        if messages:
            query = " OR ".join(
                f"({tab['query']})" for tab in configured if not tab["label_id"]
            )

    # Both search and legacy membership use Gmail's parser, never a local search dialect.
    if query:
        if account is None:
            raise HTTPException(401, "Connect Gmail first.")
        with (
            request.app.state.sync_lock,
            gmail.service(request.app.state.directory) as client,
        ):
            try:
                matching = gmail.inbox_ids(client, gmail.cutoff_time(), query)
            except HttpError as error:
                # A broken legacy query must not silently miscategorize mail as Others.
                if error.resp.status == 400:
                    raise HTTPException(
                        400,
                        "A saved query tab could not be evaluated. Edit or remove it to load Others."
                        if others
                        else "Gmail could not run this search. Check your Gmail query.",
                    ) from None
                raise
        # Others needs the complement of legacy matches, still bounded to the recent cache.
        if others:
            matching = {message["id"] for message in messages} - matching
        messages = [message for message in messages if message["id"] in matching]
    return JSONResponse(
        {"account": dict(account) if account else None, "messages": messages}
    )


async def tabs(request: Request) -> Response:
    values = {}
    # Only create/edit requests carry form values.
    if request.method in {"POST", "PUT"}:
        try:
            values = await request.json()
        except ValueError:
            raise HTTPException(400, "Provide label settings as JSON.") from None
        if (
            not isinstance(values, dict)
            or not isinstance(values.get("name"), str)
            or not 0 < len(values["name"].strip()) <= 225
        ):
            raise HTTPException(400, "Enter a label name (up to 225 characters).")
        # Bound settings before they become part of the classification prompt.
        if (
            not isinstance(values.get("description", ""), str)
            or len(values.get("description", "")) > 2000
            or not isinstance(values.get("auto_classify", False), bool)
        ):
            raise HTTPException(
                400,
                "Use a description up to 2,000 characters and an auto-classify checkbox.",
            )
        people = values.get("people", [])
        if (
            not isinstance(people, list)
            or len(people) > 100
            or any(
                not isinstance(email, str)
                or len(email) > 254
                or parseaddr(email)[1] != email
                or email.count("@") != 1
                or not all(email.split("@"))
                or any(char.isspace() for char in email)
                for email in people
            )
        ):
            raise HTTPException(
                400, "Enter up to 100 sender email addresses, without display names."
            )
        if values.get("auto_classify") and not values.get("description", "").strip():
            raise HTTPException(
                400, "Describe which messages AI should assign to this label."
            )
    return await run_in_threadpool(tab_records, request, values)


def require_label_access(directory: Path) -> None:
    # Old read-only credentials remain connected, but cannot create or apply labels.
    if not gmail.can_label(directory):
        raise HTTPException(
            403,
            "Reconnect Gmail and grant the new Gmail modify permission to manage labels.",
        )


def labels(request: Request) -> Response:
    directory = request.app.state.directory
    if not (
        directory / "token.json"
    ).exists():  # The picker needs the account's own labels.
        raise HTTPException(401, "Connect Gmail first.")
    with request.app.state.sync_lock, gmail.service(directory) as client:
        available = gmail.list_labels(client)
        with database(directory) as db:
            # Gmail owns label names; a rename must not create a second local identity.
            db.executemany(
                "UPDATE tabs SET name = ? WHERE label_id = ?",
                [(label["name"], label["id"]) for label in available],
            )
    return JSONResponse(
        {
            "labels": [label for label in available if label["type"] == "user"],
            "can_label": gmail.can_label(directory),
        }
    )


def tab_records(request: Request, values: dict[str, Any]) -> Response:
    directory = request.app.state.directory
    if request.method == "GET":  # Preferences remain readable during synchronization.
        return JSONResponse({"tabs": labeling.tabs(directory)})
    with request.app.state.sync_lock, database(directory) as db:
        tab_id = request.path_params.get("tab_id")
        existing = db.execute("SELECT * FROM tabs WHERE id = ?", (tab_id,)).fetchone()
        if request.method != "POST" and existing is None:
            raise HTTPException(404, "This label tab no longer exists.")
        if (
            request.method == "DELETE"
        ):  # Unpin locally; never delete the Gmail label or mail.
            db.execute(
                "DELETE FROM label_decisions WHERE label_id = ? AND applied = 0",
                (existing["label_id"],),
            )
            db.execute("DELETE FROM tabs WHERE id = ?", (tab_id,))
        else:
            require_label_access(directory)
            with gmail.service(directory) as client:
                available = gmail.list_labels(client)
                selected = next(
                    (
                        label
                        for label in available
                        if label["name"].casefold() == values["name"].strip().casefold()
                    ),
                    None,
                )
                # A pinned label's identity is immutable; rename it in Gmail, not by retargeting rules.
                if existing and existing["label_id"]:
                    selected = next(
                        (
                            label
                            for label in available
                            if label["id"] == existing["label_id"]
                        ),
                        None,
                    )
                    if selected is None:
                        raise HTTPException(
                            409,
                            "This label was deleted in Gmail. Remove this tab and add another label.",
                        )
                if selected and selected["type"] != "user":
                    raise HTTPException(
                        400, "Choose a custom label, not a reserved Gmail system label."
                    )
                if (
                    selected
                    and db.execute(
                        "SELECT 1 FROM tabs WHERE label_id = ? AND id != ?",
                        (selected["id"], tab_id or -1),
                    ).fetchone()
                ):
                    raise HTTPException(409, "This label is already added.")
                if (
                    selected is None
                ):  # Gmail validates new names; save locally only after creation succeeds.
                    try:
                        selected = dict(
                            client.users()
                            .labels()
                            .create(userId="me", body={"name": values["name"].strip()})
                            .execute()
                        )
                    except HttpError as error:
                        if error.resp.status in {400, 409}:
                            raise HTTPException(
                                400,
                                "Gmail could not create this label. Choose another name or reload the label picker.",
                            ) from None
                        raise
            fields = (
                selected["name"],
                selected["id"],
                values.get("description", "").strip(),
                json.dumps(
                    sorted({email.casefold() for email in values.get("people", [])})
                ),
                values.get("auto_classify", False),
            )
            if request.method == "POST":
                tab_id = db.execute(
                    "INSERT INTO tabs (name, label_id, description, people, auto_classify, query, position) VALUES (?, ?, ?, ?, ?, '', (SELECT COALESCE(MAX(position), 0) + 1 FROM tabs))",
                    fields,
                ).lastrowid
            else:
                db.execute(
                    "UPDATE tabs SET name = ?, label_id = ?, description = ?, people = ?, auto_classify = ?, query = '' WHERE id = ?",
                    (*fields, tab_id),
                )
                # Re-evaluate pending sender matches, without losing completed AI decisions on save.
                db.execute(
                    "DELETE FROM label_decisions WHERE label_id = ? AND source = 'sender' AND applied = 0",
                    (selected["id"],),
                )
    labeling.wake(request.app.state)
    return JSONResponse(
        {"id": tab_id}, status_code=201 if request.method == "POST" else 200
    )


async def reorder_tabs(request: Request) -> Response:
    try:
        ids = await request.json()
    except ValueError:
        raise HTTPException(400, "Provide the ordered list of tab IDs.") from None
    # Only complete permutations are accepted; booleans are not integer tab IDs.
    if (
        not isinstance(ids, list)
        or any(type(value) is not int for value in ids)
        or len(ids) != len(set(ids))
    ):
        raise HTTPException(400, "Provide each tab ID exactly once.")

    def save() -> Response:
        with request.app.state.sync_lock, database(request.app.state.directory) as db:
            # A different browser may have added or removed a tab since this drag began.
            if set(ids) != {row[0] for row in db.execute("SELECT id FROM tabs")}:
                raise HTTPException(
                    409, "Your tabs changed. Reload their order and try again."
                )
            db.executemany("UPDATE tabs SET position = ? WHERE id = ?", enumerate(ids))
        return JSONResponse({"tabs": labeling.tabs(request.app.state.directory)})

    return await run_in_threadpool(save)


async def ai_settings(request: Request) -> Response:
    values = None
    if (
        request.method == "PUT"
    ):  # Secrets arrive only in explicit same-origin settings saves.
        try:
            values = await request.json()
        except ValueError:
            raise HTTPException(400, "Provide AI settings as JSON.") from None
        if (
            not isinstance(values, dict)
            or not isinstance(values.get("enabled"), bool)
            or not isinstance(values.get("reasoning"), str)
            or values["reasoning"] not in {"medium", "high"}
            or not isinstance(values.get("api_key", ""), str)
            or len(values.get("api_key", "")) > 512
        ):
            raise HTTPException(
                400, "Choose medium or high reasoning and a valid API key."
            )

    def save() -> dict[str, Any]:
        with request.app.state.sync_lock:
            config = labeling.settings(request.app.state.directory)
            if values is not None:
                config.update(enabled=values["enabled"], reasoning=values["reasoning"])
                if values.get(
                    "api_key", ""
                ).strip():  # Blank means retain the server-side secret.
                    config["api_key"] = values["api_key"].strip()
                if config["enabled"] and not config["api_key"]:
                    raise HTTPException(
                        400, "Add your OpenAI API key before enabling AI."
                    )
                gmail.private_write(
                    request.app.state.directory / "ai.json", json.dumps(config)
                )
        return {key: value for key, value in config.items() if key != "api_key"} | {
            "has_key": bool(config["api_key"]),
            "can_label": gmail.can_label(request.app.state.directory),
        }

    result = await run_in_threadpool(save)
    if values is not None:
        labeling.wake(request.app.state)
    return JSONResponse(result)


def ai_usage(request: Request) -> Response:
    before = request.query_params.get("before")
    # Keyset pagination remains stable when new requests arrive while browsing older ones.
    if before is not None and (
        len(before) > 19
        or not before.isascii()
        or not before.isdecimal()
        or not 0 < int(before) <= 2**63 - 1
    ):
        raise HTTPException(400, "Provide a valid request-history cursor.")
    return JSONResponse(
        usage.history(request.app.state.directory, int(before) if before else None)
    )


async def label_progress(request: Request) -> Response:
    return JSONResponse(request.app.state.label_progress)


def sender_history(request: Request) -> Response:
    sender = parseaddr(request.query_params.get("sender", ""))[1].casefold()
    # Malformed or absent From headers cannot form a meaningful sender-history search.
    if not sender or "@" not in sender:
        raise HTTPException(400, "This message has no usable sender address.")
    directory = request.app.state.directory
    # Sender history is on-demand and still requires this machine's connected account.
    if not (directory / "token.json").exists():
        raise HTTPException(401, "Connect Gmail first.")
    with request.app.state.sync_lock, gmail.service(directory) as client:
        return JSONResponse(
            gmail.sender_history(
                client,
                sender,
                request.query_params.get("page"),
                individual=request.query_params.get("view") == "messages",
                all_mail=request.query_params.get("all") == "1",
            )
        )


def history_message(request: Request) -> Response:
    directory = request.app.state.directory
    # Older history is read on demand, never inserted into the bounded inbox cache.
    if not (directory / "token.json").exists():
        raise HTTPException(401, "Connect Gmail first.")
    with request.app.state.sync_lock, gmail.service(directory) as client:
        message = gmail.get_message(
            client, request.path_params["message_id"], full=True
        )
        # A previously listed message can disappear before the user opens it.
        if message is None:
            raise HTTPException(404, "This message is no longer available in Gmail.")
        return JSONResponse(
            {
                "body": gmail.message_text(message.get("payload", {})),
                **message_summary(message),
            }
        )


def refresh(request: Request) -> Response:
    directory = request.app.state.directory
    # Until OAuth succeeds there are no credentials with which to sync.
    if not (directory / "token.json").exists():
        raise HTTPException(401, "Connect Gmail first.")
    # Serialize sync and body fetches; neither can overwrite the other's cache changes.
    with request.app.state.sync_lock:
        started_at = int(time.time() * 1000)

        def report(stage: str, completed: int, total: int | None) -> None:
            # Replace the snapshot atomically so polling needs neither this lock nor SQLite.
            request.app.state.sync_progress = {
                "status": "complete" if stage == "complete" else "running",
                "stage": stage,
                "completed": completed,
                "total": total,
                "started_at": started_at,
                "updated_at": int(time.time() * 1000),
            }

        report("connecting", 0, None)
        try:
            with gmail.service(directory) as client:
                gmail.sync(directory, client, report=report)
        except Exception:
            # Preserve the last real counts on failure; never imply that a rollback completed.
            request.app.state.sync_progress = {
                **request.app.state.sync_progress,
                "status": "failed",
            }
            raise
        report("complete", 0, None)
    labeling.wake(request.app.state)
    return inbox(request)


async def sync_progress(request: Request) -> Response:
    # Polling is local-only and stays responsive while the sync worker waits on Gmail.
    return JSONResponse(request.app.state.sync_progress)


def message(request: Request) -> Response:
    directory = request.app.state.directory
    message_id = request.path_params["message_id"]
    with request.app.state.sync_lock:
        with database(directory) as db:
            row = db.execute(
                "SELECT body, unsubscribe FROM messages WHERE id = ? AND received_at >= ?",
                (message_id, gmail.cutoff_time()),
            ).fetchone()
        # Only cached inbox IDs can trigger a body download, not arbitrary mailbox IDs.
        if row is None:
            raise HTTPException(
                404, "This message is no longer in the recent inbox cache."
            )
        body = row["body"]
        # Migrate already-opened messages by fetching only their missing action headers.
        if body is not None and row["unsubscribe"] is None:
            with gmail.service(directory) as client, database(directory) as db:
                gmail.update_message(db, client, message_id, gmail.cutoff_time())
                row = db.execute(
                    "SELECT id FROM messages WHERE id = ?", (message_id,)
                ).fetchone()
            # Commit the discovered removal before returning an error to the stale reader.
            if row is None:
                raise HTTPException(
                    404, "This message is no longer in the recent inbox cache."
                )
        # An empty string is a cached body too; only NULL means not fetched yet.
        if body is None:
            with gmail.service(directory) as client:
                body = gmail.read_message(directory, client, message_id)
        # The message could have been removed in Gmail since the list was rendered.
        if body is None:
            raise HTTPException(
                404, "This message is no longer in the recent inbox cache."
            )
    with database(directory) as db:
        reasons = [
            dict(row)
            for row in db.execute(
                "SELECT t.name, d.source, d.reason, d.applied FROM label_decisions d JOIN tabs t ON t.label_id = d.label_id WHERE d.message_id = ? ORDER BY t.id, d.source",
                (message_id,),
            )
        ]
        row = db.execute(
            "SELECT labels, unsubscribe FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
    return JSONResponse(
        {
            "body": body,
            "reasons": reasons,
            "labels": json.loads(row["labels"]) if row else [],
            "unsubscribe": row["unsubscribe"] if row else "",
        }
    )


def sender_email(request: Request) -> str:
    email = request.query_params.get("sender", "").strip().casefold()
    # Local sender preferences must use one exact address, never a Gmail query or display name.
    if (
        len(email) > 254
        or "@" not in email
        or parseaddr(email)[1] != email
        or any(c.isspace() for c in email)
    ):
        raise HTTPException(400, "Choose a valid sender email address.")
    return email


async def sender_settings(request: Request) -> Response:
    email = sender_email(request)
    values = {}
    # GET is read-only; PUT edits either the note or the selected sender labels.
    if request.method == "PUT":
        try:
            values = await request.json()
        except ValueError:
            raise HTTPException(400, "Provide sender settings as JSON.") from None
        if (
            not isinstance(values, dict)
            or not values
            or set(values) - {"note", "labels"}
        ):
            raise HTTPException(400, "Provide a note or sender labels.")
        if "note" in values and (
            not isinstance(values["note"], str) or len(values["note"]) > 4000
        ):
            raise HTTPException(400, "Keep sender notes within 4000 characters.")
        if "labels" in values and (
            not isinstance(values["labels"], list)
            or len(values["labels"]) > 100
            or any(not isinstance(item, str) for item in values["labels"])
            or len(set(values["labels"])) != len(values["labels"])
        ):
            raise HTTPException(400, "Choose distinct sender labels.")
    return await run_in_threadpool(sender_records, request, email, values)


def sender_records(request: Request, email: str, values: dict[str, Any]) -> Response:
    directory = request.app.state.directory
    with request.app.state.sync_lock, database(directory) as db:
        configured = labeling.tabs(directory)
        # Rules reuse pinned label settings without changing descriptions or AI policy.
        if "labels" in values:
            require_label_access(directory)
            if not set(values["labels"]).issubset(
                {tab["label_id"] for tab in configured if tab["label_id"]}
            ):
                raise HTTPException(409, "A label tab changed. Reopen the sender menu.")
            for tab in configured:
                people = set(tab["people"])
                wanted = tab["label_id"] in values["labels"]
                if (email in people) == wanted:
                    continue  # Leave unrelated rules and queued decisions untouched.
                if wanted and len(people) >= 100:
                    raise HTTPException(400, "This label already has 100 sender rules.")
                people = people | {email} if wanted else people - {email}
                db.execute(
                    "UPDATE tabs SET people = ? WHERE id = ?",
                    (json.dumps(sorted(people)), tab["id"]),
                )
                # Only pending sender work is invalidated; completed/manual labels are preserved.
                db.execute(
                    "DELETE FROM label_decisions WHERE label_id = ? AND source = 'sender' AND applied = 0",
                    (tab["label_id"],),
                )
        if "note" in values:
            db.execute(
                "INSERT INTO senders (email, note) VALUES (?, ?) ON CONFLICT(email) DO UPDATE SET note = excluded.note",
                (email, values["note"].strip()),
            )
        row = db.execute(
            "SELECT note FROM senders WHERE email = ?", (email,)
        ).fetchone()
    if "labels" in values:
        labeling.wake(
            request.app.state
        )  # Apply deterministic rules to the recent cache too.
    return JSONResponse(
        {
            "note": row["note"] if row else "",
            "labels": [
                tab["label_id"]
                for tab in labeling.tabs(directory)
                if email in tab["people"]
            ],
        }
    )


def message_action(request: Request) -> Response:
    directory = request.app.state.directory
    action = request.path_params["action"]
    # No generic Gmail mutation endpoint: these are the only approved mail actions.
    if action not in {"archive", "unsubscribed"}:
        raise HTTPException(404, "Unknown mail action.")
    require_label_access(directory)
    with request.app.state.sync_lock, gmail.service(directory) as client:
        message_id = request.path_params["message_id"]
        message = gmail.get_message(client, message_id)
        if message is None:
            raise HTTPException(404, "This message is no longer available in Gmail.")
        summary = message_summary(message)
        if action == "archive":
            client.users().messages().modify(
                userId="me", id=message_id, body={"removeLabelIds": ["INBOX"]}
            ).execute(num_retries=2)
            # Change the cache only after Gmail acknowledges the archive; never change its history cursor.
            with database(directory) as db:
                db.execute("DELETE FROM messages WHERE id = ?", (message_id,))
            return JSONResponse({"archived": True})

        # This records the owner's confirmation, not an inferred success from opening a link.
        if not summary["unsubscribe"] or not summary["sender_email"]:
            raise HTTPException(
                400, "This message has no supported unsubscribe option."
            )
        available = gmail.list_labels(client)
        label: dict[str, Any] | None = next(
            (
                item
                for item in available
                if item["name"].casefold() == "unsubscribed" and item["type"] == "user"
            ),
            None,
        )
        if label is None:
            label = dict(
                client.users()
                .labels()
                .create(userId="me", body={"name": "unsubscribed"})
                .execute()
            )
        updated = (
            client.users()
            .messages()
            .modify(userId="me", id=message_id, body={"addLabelIds": [label["id"]]})
            .execute(num_retries=2)
        )
        message["labelIds"] = updated["labelIds"]
        with database(directory) as db:
            # Pin/reuse the label and remember the sender, rather than relabeling the whole mailbox.
            db.execute(
                "INSERT OR IGNORE INTO tabs (name, label_id, query, position) VALUES (?, ?, '', (SELECT COALESCE(MAX(position), 0) + 1 FROM tabs))",
                (label["name"], label["id"]),
            )
            tab = db.execute(
                "SELECT id, people FROM tabs WHERE label_id = ?", (label["id"],)
            ).fetchone()
            people = sorted(set(json.loads(tab["people"])) | {summary["sender_email"]})
            db.execute(
                "UPDATE tabs SET people = ? WHERE id = ?",
                (json.dumps(people), tab["id"]),
            )
            cache_message(db, message, gmail.cutoff_time())
    labeling.wake(request.app.state)
    return JSONResponse({"labels": message["labelIds"]})


def gmail_denial(error: HttpError) -> str:
    # Google's client parses both legacy errors[] and modern ErrorInfo details[].
    details = error.error_details if isinstance(error.error_details, list) else []
    for item in details:
        # ErrorInfo may be accompanied by help links or other non-reason details.
        if not isinstance(item, dict):
            continue
        reason = item.get("reason")
        if reason in ("accessNotConfigured", "SERVICE_DISABLED"):
            advice = "Enable the Gmail API in the same Google Cloud project as the uploaded OAuth client. If you just enabled it, wait a few minutes, then start Connect Gmail again."
        elif reason in ("insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"):
            advice = "Start Connect Gmail again and grant Gmail modify access on Google's consent screen. Configuring a scope in Cloud does not itself grant access to your account."
        elif reason == "domainPolicy":
            advice = "Your Google Workspace administrator has blocked this app's Gmail access. Ask them to review the app's API access policy."
        elif reason in (
            "dailyLimitExceeded",
            "rateLimitExceeded",
            "userRateLimitExceeded",
            "RATE_LIMIT_EXCEEDED",
        ):
            advice = "Gmail's quota or rate limit was exceeded. Wait and retry; check the project's Gmail API quotas if it persists."
        else:
            # Only recognized reason codes are reflected; arbitrary diagnostics may contain secrets.
            continue
        return f"Gmail returned 403 ({reason}). {advice}"
    return "Gmail returned 403 without a recognized reason. Check API access, granted consent, Workspace policy, and quota settings; then start Connect Gmail again."


async def error_response(request: Request, error: Exception) -> Response:
    status, detail = 502, "Gmail is unavailable. Your cache is unchanged; please retry."
    # Expected errors get actionable messages without leaking tokens or email payloads.
    if isinstance(error, HTTPException):
        status, detail = error.status_code, str(error.detail)
    elif isinstance(error, RefreshError) or (
        isinstance(error, HttpError) and error.resp.status == 401
    ):
        status, detail = (
            401,
            "Gmail access expired or was revoked. Reconnect the same account.",
        )
    elif isinstance(error, OAuth2Error):
        status, detail = 400, "Google sign-in failed. Start Connect Gmail again."
    elif isinstance(error, HttpError) and error.resp.status == 403:
        status, detail = 403, gmail_denial(error)
    return JSONResponse({"error": detail}, status_code=status)


async def local_security(
    request: Request, call_next: RequestResponseEndpoint
) -> Response:
    # Cross-origin pages must not trigger downloads, syncs, or inspect private data.
    if request.url.path.startswith("/api/") and (
        request.headers.get("X-Mailsome-Request") != "1"
        or request.headers.get("origin", ORIGIN) != ORIGIN
        or request.headers.get("sec-fetch-site") == "cross-site"
    ):
        return JSONResponse(
            {"error": "Use Mailsome in its local browser tab."}, status_code=403
        )
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


def create_app(directory: Path = ROOT / "data") -> Starlette:
    # Startup owns local state creation; importing the module doesn't create a database.
    @asynccontextmanager
    async def lifespan(application: Starlette) -> AsyncIterator[None]:
        initialize(directory)
        usage.recover(directory)
        application.state.loop = asyncio.get_running_loop()
        application.state.label_wake = asyncio.Event()
        task = asyncio.create_task(labeling.worker(application.state))
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    # A stable signing key keeps OAuth callbacks valid across development reloads.
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    secret_path = directory / "session-secret"
    # Generate the local secret only once, keeping it out of source and cookies.
    if not secret_path.exists():
        gmail.private_write(secret_path, secrets.token_urlsafe(32))
    application = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/", home),
            Route("/auth/connect", connect),
            Route("/auth/callback", callback),
            Route("/api/inbox", inbox),
            Route("/api/labels", labels),
            Route("/api/ai-usage", ai_usage),
            Route("/api/ai-settings", ai_settings, methods=["GET", "PUT"]),
            Route("/api/label-progress", label_progress),
            Route("/api/tabs", tabs, methods=["GET", "POST"]),
            Route("/api/tabs/order", reorder_tabs, methods=["PUT"]),
            Route("/api/tabs/{tab_id:int}", tabs, methods=["PUT", "DELETE"]),
            Route("/api/sender-history", sender_history),
            Route("/api/sender-settings", sender_settings, methods=["GET", "PUT"]),
            Route(
                "/api/messages/{message_id}/actions/{action}",
                message_action,
                methods=["POST"],
            ),
            Route("/api/history/messages/{message_id}", history_message),
            Route("/api/oauth-credentials", upload_credentials, methods=["POST"]),
            Route("/api/refresh", refresh, methods=["POST"]),
            Route("/api/sync-progress", sync_progress),
            Route("/api/messages/{message_id}", message),
            Mount("/static", StaticFiles(directory=ROOT / "static")),
        ],
        middleware=[
            Middleware(BaseHTTPMiddleware, dispatch=local_security),
            Middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"]),
            Middleware(
                SessionMiddleware, secret_key=secret_path.read_text(), max_age=600
            ),
        ],
        exception_handlers={
            HTTPException: error_response,
            HttpError: error_response,
            RefreshError: error_response,
            OAuth2Error: error_response,
            RequestException: error_response,
            OSError: error_response,
        },
    )
    application.state.directory = directory
    application.state.sync_lock = threading.Lock()
    application.state.sync_progress = {"status": "idle"}
    application.state.label_progress = {"status": "idle"}

    return application


app = create_app()
