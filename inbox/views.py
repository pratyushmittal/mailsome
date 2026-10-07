"""Server-rendered inbox pages and explicit, CSRF-protected mail actions."""

from __future__ import annotations

import base64
import hashlib
import json
import pickle
from collections.abc import Callable
from email.utils import getaddresses, parseaddr
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from django.conf import settings
from django.core.cache import caches
from django.db import IntegrityError
from django.db.models import Max
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_http_methods
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

# Provider schemas are supplied by type stubs, not runtime modules.
if TYPE_CHECKING:
    from googleapiclient._apis.gmail.v1.schemas import Label

from accounts.models import Account
from classifications import utils as classification_utils
from classifications.models import LabelDecision
from inbox import content, gmail, sender_filters
from inbox.forms import ComposeForm, SenderForm, TabForm, UnsubscribeForm
from inbox.models import Message, Sender, Tab
from inbox.utils import (
    apply_message_update,
    format_address,
    message_from_gmail,
    outgoing_mail,
)
from jobs.pipeline import sync_requested
from mailsome.errors import APIError, gmail_denial
from mailsome.utilities import page, redirect_next, safe_next


@require_http_methods(["GET"])
def inbox(request: HttpRequest) -> HttpResponse:
    # A disconnected installation renders safely without evaluating saved Gmail queries.
    if not Account.objects.filter(pk=1).exists():
        return page(request, "inbox/list.html", {"messages": []})
    importance_threshold = classification_utils.settings()["importance_threshold"]
    # Sender searches use the same message store as tab browsing.
    if request.GET.get("sender"):
        context = sender_context(request)
    else:
        query = request.GET.get("q", "").strip()
        tab_id = request.GET.get("tab")
        configured = list(Tab.objects.all())
        labels = [] if query else ["INBOX"]
        with gmail.service() as client:
            if not query and tab_id:
                tab = next((tab for tab in configured if str(tab.pk) == tab_id), None)
                # Pins may be removed in another browser while their URL is still open.
                if tab is None:
                    raise APIError(
                        404, "This label tab no longer exists. Select Others."
                    )
                if tab.label_id:
                    labels.append(tab.label_id)
                else:
                    query = tab.query
            elif not query:
                # Gmail queries exclude by name; resolve current names so external renames stay correct.
                names = (
                    {label["id"]: label["name"] for label in gmail.list_labels(client)}
                    if configured
                    else {}
                )
                query = " ".join(
                    f"-label:{json.dumps(names[tab.label_id])}"
                    if tab.label_id in names
                    else f"-({tab.query})"
                    if tab.query
                    else ""
                    for tab in configured
                )
            try:
                result = gmail.get_message_page(
                    client,
                    query=query,
                    labels=labels,
                    page_token=request.GET.get("page"),
                )
                messages = gmail.get_messages_for_list(
                    client, [item["id"] for item in result.get("messages", [])]
                )
            except HttpError as error:
                # Bad search syntax or an expired page token needs a fresh page, not local fallback results.
                if error.resp.status == 400:
                    raise APIError(
                        400,
                        "Gmail could not run this search or page. Check the query or return to the first page.",
                    ) from None
                raise
        add_message_urls(request, messages)
        context = {
            "messages": messages,
            "query": request.GET.get("q", ""),
            **_pagination(request, result.get("nextPageToken")),
        }
    context["importance_threshold"] = importance_threshold
    return page(request, "inbox/list.html", context)


def _pagination(request: HttpRequest, next_page: str | None) -> dict[str, str]:
    params = request.GET.copy()
    params.pop("page", None)
    first_url = "/?" + params.urlencode()
    # Gmail supplies forward cursors, not page numbers or a backwards cursor.
    if next_page:
        params["page"] = next_page
    return {
        "previous_url": first_url if request.GET.get("page") else "",
        "next_url": "/?" + params.urlencode() if next_page else "",
    }


def require_label_access() -> None:
    # Old read-only credentials remain connected, but cannot create or apply labels.
    if not gmail.can_label():
        raise APIError(
            403,
            "Reconnect Gmail and grant the new Gmail modify permission to manage labels.",
        )


def sender_email(request: HttpRequest) -> str:
    email = request.GET.get("sender", "").strip().casefold()
    # Local sender preferences must use one exact address, never a Gmail query or display name.
    if (
        len(email) > 254
        or email.count("@") != 1
        or not all(email.split("@"))
        or parseaddr(email)[1] != email
        or any(c.isspace() for c in email)
    ):
        raise APIError(400, "Choose a valid sender email address.")
    return email


def save_tab(
    values: dict[str, Any], tab_id: int | None, *, delete: bool = False
) -> int | None:
    existing = Tab.objects.filter(pk=tab_id).first()
    # Another browser can remove a tab between displaying and submitting its editor.
    if tab_id is not None and existing is None:
        raise APIError(404, "This label tab no longer exists.")

    selected: Label | None = None
    # Only creation/editing needs label resolution; unpinning retains the Gmail label.
    if not delete:
        require_label_access()
        # Local preference edits do not need a Gmail label lookup.
        if existing and existing.label_id and values["name"] == existing.name:
            selected = {"id": existing.label_id, "name": existing.name}
        else:
            # New labels, legacy conversions and changed names still need Gmail resolution.
            # Resolve labels before saving local tab changes.
            with gmail.service() as client:
                available = gmail.list_labels(client)
                selected = next(
                    (
                        label
                        for label in available
                        if (
                            label["id"] == existing.label_id
                            if existing and existing.label_id
                            else label["name"].casefold() == values["name"].casefold()
                        )
                    ),
                    None,
                )
                # A changed name never rebinds an existing pin or recreates a deleted Gmail label.
                if existing and existing.label_id and selected is None:
                    raise APIError(
                        409,
                        "This label was deleted in Gmail. Remove this tab and add another label.",
                    )
                # System labels are not valid custom tabs.
                if selected and selected["type"] != "user":
                    raise APIError(
                        400, "Choose a custom label, not a reserved Gmail system label."
                    )
                # The same Gmail ID may already be pinned by another tab.
                if (
                    selected
                    and Tab.objects.filter(label_id=selected["id"])
                    .exclude(pk=tab_id)
                    .exists()
                ):
                    raise APIError(409, "This label is already added.")
                # Only explicit creation/conversion may create a missing Gmail label.
                if selected is None:
                    try:
                        selected = gmail.create_label(client, values["name"])
                    except HttpError as error:
                        # Gmail may reject a name or another request may have just created it.
                        if error.resp.status in {400, 409}:
                            raise APIError(
                                400,
                                "Gmail could not create this label. Choose another name or reload the label picker.",
                            ) from None
                        raise

    # Finish filter I/O before saving local tab changes.
    if delete and existing:
        sender_filters.replace(existing.label_id, existing.people, [])
    elif not delete:
        assert selected is not None
        values = {
            **values,
            "name": selected["name"],
            "label_id": selected["id"],
            "query": "",
        }
        previous_people = (
            existing.people if existing and existing.label_id == selected["id"] else []
        )
        # A converted tab must retire its old filter without changing historical labels.
        if existing and existing.label_id != selected["id"]:
            sender_filters.replace(existing.label_id, existing.people, [])
        sender_filters.replace(selected["id"], previous_people, values["people"])

    # Removing a tab retains applied history and the Gmail label itself.
    if delete and existing:
        LabelDecision.objects.filter(label_id=existing.label_id, applied=False).delete()
        existing.delete()
    elif not delete:
        # Updates preserve display order; only new pins append a position.
        if existing:
            Tab.objects.filter(pk=tab_id).update(**values)
            # Conversion cancels pending writes to the old label, not applied history.
            if existing.label_id != values["label_id"]:
                LabelDecision.objects.filter(
                    label_id=existing.label_id, applied=False
                ).delete()
        else:
            tab_id = Tab.objects.create(
                **values,
                position=(Tab.objects.aggregate(value=Max("position"))["value"] or 0)
                + 1,
            ).pk
    # Request history sync with the saved settings; filter work is already complete.
    sync_requested.set()
    return tab_id


def add_message_urls(
    request: HttpRequest, messages: list[Message], *, remote: bool = False
) -> None:
    """Keep the list selection and return row in every reader link."""
    for item in messages:
        params = {
            key: request.GET[key]
            for key in ("tab", "q", "sender", "page")
            if key in request.GET
        }
        params["next"] = request.get_full_path() + "#mail-" + item.id
        # Preserve existing history URLs; all reader origins now share the persistent cache.
        if remote:
            params["remote"] = "1"
        item.url = f"/messages/{item.id}/?{urlencode(params)}"


def sender_context(request: HttpRequest) -> dict[str, Any]:
    email = sender_email(request)
    with gmail.service() as client:
        messages, next_page = gmail.get_sender_history(
            client, email, request.GET.get("page"), all_mail=True
        )
    add_message_urls(request, messages, remote=True)
    return {
        "messages": messages,
        "sender": email,
        "next_page": next_page,
        **_pagination(request, next_page),
    }


def operation_error(error: Exception) -> tuple[int, str]:
    """Share safe provider errors between bound editors and retryable mail actions."""
    # Only application-owned messages and recognized provider categories may reach HTML.
    if isinstance(error, APIError):
        return error.status_code, error.detail
    if isinstance(error, IntegrityError):
        return 409, "This label is already added. Reload and try again."
    if isinstance(error, RefreshError) or (
        isinstance(error, HttpError) and error.resp.status == 401
    ):
        return 401, "Gmail access expired or was revoked. Reconnect the same account."
    if isinstance(error, HttpError) and error.resp.status == 403:
        return 403, gmail_denial(error)
    return (
        502,
        "Gmail could not complete this request. Check your connection and try again.",
    )


def form_error(
    form: TabForm | SenderForm | UnsubscribeForm | ComposeForm,
    error: Exception,
    field: str | None = None,
) -> int:
    # Django omits cleaned_data on unbound GET forms; provider errors must still render safely.
    if not form.is_bound:
        form.cleaned_data = {}
    status, detail = operation_error(error)
    form.add_error(
        field if isinstance(error, (APIError, IntegrityError)) else None, detail
    )
    return status


@require_http_methods(["GET", "POST"])
def tab_edit(request: HttpRequest, tab_id: int | None = None) -> HttpResponse:
    tab = Tab.objects.filter(pk=tab_id).first()
    # Stale editor links cannot accidentally create a replacement tab.
    if tab_id is not None and tab is None:
        raise APIError(404, "This label tab no longer exists.")
    initial = (
        {
            "name": tab.name,
            "description": tab.description,
            "people": "\n".join(tab.people),
            "auto_classify": tab.auto_classify,
            "acceptance_threshold": tab.acceptance_threshold,
            "feed": tab.feed,
        }
        if tab
        else {}
    )
    form = TabForm(request.POST if request.method == "POST" else None, initial=initial)
    status = 200
    available: list[Label] = []
    # Unpinning keeps the Gmail label; associated sender filters are removed.
    if request.method == "POST" and request.POST.get("action") == "delete":
        if tab is None:
            form.add_error(None, "Choose an existing tab to remove.")
            status = 400
        else:
            save_tab({}, tab_id, delete=True)
            return redirect_next(request)
    elif request.method == "POST":
        status = 400
        if form.is_valid():
            try:
                save_tab(dict(form.cleaned_data), tab_id)
            except (
                APIError,
                HttpError,
                RefreshError,
                OSError,
                IntegrityError,
            ) as error:
                status = form_error(form, error, "name")
            else:
                return redirect_next(request)
    # Pinned editors are local forms. Only adding/converting needs the Gmail label picker.
    # Invalid POSTs retain validation errors without making another provider request.
    if request.method == "GET" and (tab is None or not tab.label_id):
        try:
            # A fresh installation has no credentials to consult for a label picker.
            if not Account.objects.filter(pk=1).exists():
                raise APIError(
                    401, "Connect Gmail before adding or editing label tabs."
                )
            with gmail.service() as client:
                available = [
                    label
                    for label in gmail.list_labels(client)
                    if label["type"] == "user"
                ]
        except (APIError, HttpError, RefreshError, OSError) as error:
            status = form_error(form, error)
    return page(
        request,
        "inbox/tab_form.html",
        {
            "form": form,
            "tab": tab,
            "gmail_labels": available,
            "can_manage_filters": gmail.can_manage_filters(),
        },
        status=status,
    )


@require_http_methods(["POST"])
def reorder_tabs(request: HttpRequest) -> HttpResponse:
    current = list(Tab.objects.values_list("id", flat=True))
    try:
        ids = [int(value) for value in request.POST.getlist("order")]
        # Native move buttons use the currently stored order.
        if not ids:
            selected = current.index(int(request.POST.get("tab", "")))
            direction = request.POST.get("direction")
            if direction not in {"left", "right"}:
                raise ValueError
            destination = selected + (-1 if direction == "left" else 1)
            ids = current.copy()
            # Edge buttons are harmless no-ops; Others is never part of the stored order.
            if 0 <= destination < len(ids):
                ids[selected], ids[destination] = ids[destination], ids[selected]
    except (ValueError, TypeError):
        raise APIError(400, "Choose a tab and a valid move direction.") from None
    # Reject partial/duplicate permutations when another browser changed the tab set.
    if len(ids) != len(set(ids)) or set(ids) != set(current):
        raise APIError(409, "Your tabs changed. Reload their order and try again.")
    for position, tab_id in enumerate(ids):
        Tab.objects.filter(pk=tab_id).update(position=position)
    return redirect_next(request)


def reader_key(kind: str, identifier: str) -> str:
    """Key reader entries by account; syncs keep them so preloaded mail survives."""
    email = Account.objects.filter(pk=1).values_list("email", flat=True).first()
    # Never serve content left over from a disconnected account.
    if email is None:
        raise APIError(401, "Connect Gmail first.")
    return hashlib.sha256(
        json.dumps([str(settings.DATA_DIR), email, kind, identifier]).encode()
    ).hexdigest()


def reader_cached[T](kind: str, identifier: str, load: Callable[[], T]) -> T:
    """A small, process-local reader cache; entries expire after 60 seconds."""
    key = reader_key(kind, identifier)
    cached = caches["reader"].get(key)
    # None represents a miss; empty lists and dictionaries are valid results.
    if cached is not None:
        return cached
    result = load()
    # Large messages remain readable, but cannot fill the bounded in-memory cache.
    # Match the local cache serializer now that results may contain Message instances.
    if len(pickle.dumps(result, pickle.HIGHEST_PROTOCOL)) <= 2 * 1024 * 1024:
        caches["reader"].set(key, result, timeout=60)
    return result


def read_message(message_id: str, *, remote: bool, formatted: bool = False) -> Message:
    """Read and persist one body at any age; page origin does not affect AI eligibility."""
    # A remote-reader URL can be opened before setup; do not consult Gmail without an account.
    if not Account.objects.filter(pk=1).exists():
        raise APIError(401, "Connect Gmail first.")
    cached = Message.objects.filter(pk=message_id).first()
    # Any real Gmail message may be opened; cache bodies once regardless of age or URL origin.
    if (
        cached is None
        or cached.body is None
        or (
            formatted
            and (cached.rich_body is None or "attachments" not in cached.rich_body)
        )
    ):
        with gmail.service() as client:
            gmail.update_messages(client, [message_id])
    elif cached.unsubscribe is None or cached.recipients is None:
        # Fetch missing legacy headers; persistence keeps already cached body content.
        with gmail.service() as client:
            gmail.update_messages(client, [message_id])
    result = Message.objects.filter(pk=message_id).first()
    if result is None or result.body is None:
        # A listed message may have been permanently deleted in Gmail.
        raise APIError(404, "This message is no longer available in Gmail.")
    return result


@require_http_methods(["GET"])
def message(request: HttpRequest, message_id: str) -> HttpResponse:
    item = read_message(message_id, remote=request.GET.get("remote") == "1")
    email = item.sender_email
    names = dict(Tab.objects.exclude(label_id=None).values_list("label_id", "name"))
    label_names = [names.get(label, label) for label in item.labels]
    reasons = [
        {
            "name": names[item.label_id],
            "source": item.source,
            "reason": item.reason,
            "ai_score": item.ai_score,
            "applied": int(item.applied),
        }
        for item in LabelDecision.objects.filter(message_id=message_id).order_by(
            "label_id", "source"
        )
        if item.label_id in names
    ]
    params = {
        key: request.GET[key]
        for key in ("tab", "q", "sender", "page")
        if key in request.GET
    }
    back_url = safe_next(request, "/" + ("?" + urlencode(params) if params else ""))
    params.update(remote="1", next=back_url)
    conversation_error = ""
    try:

        def load_conversation() -> list[Message]:
            with gmail.service() as client:
                return gmail.get_thread_messages(client, item.thread_id)

        conversation = reader_cached("thread", item.thread_id, load_conversation)
    except (APIError, HttpError, RefreshError, OSError):
        conversation = []
        conversation_error = "Conversation unavailable. Reload to check for replies."
    # Keep the selected message readable even when it disappears from the thread response.
    conversation = [other for other in conversation if other.id != message_id] + [item]
    conversation.sort(key=lambda other: (other.received_at, other.id))
    for other in conversation:
        other.url = (
            f"/messages/{other.id}/?"
            + urlencode({**params, "format": request.GET.get("format", "html")})
            + "#selected-message"
        )
    later = conversation[conversation.index(item) + 1 :]
    presentation = request.GET.copy()
    presentation.pop("images", None)
    formatted_url = "?" + urlencode({**presentation.dict(), "format": "html"})
    text_url = "?" + urlencode({**presentation.dict(), "format": "text"})
    blocked_url = "?" + urlencode(
        {**presentation.dict(), "format": "html", "images": "0"}
    )
    has_html = item.rich_body is None or bool(
        item.rich_body.get("html") or item.rich_body.get("html_parts")
    )
    formatted = has_html and request.GET.get("format") != "text"
    sender_query = urlencode({"sender": email, "next": request.get_full_path()})
    return page(
        request,
        "inbox/reader.html",
        {
            "message": item,
            "label_names": label_names,
            "reasons": reasons,
            "conversation": conversation,
            "conversation_error": conversation_error,
            "later_count": len(later),
            "later_anchor": f"#thread-{later[0].id}" if later else "",
            "formatted": formatted,
            "has_html": has_html,
            "formatted_url": formatted_url,
            "text_url": text_url,
            "blocked_url": blocked_url,
            "body_url": f"/messages/{message_id}/body/?"
            + urlencode(
                {
                    "remote": request.GET.get("remote", ""),
                    "images": request.GET.get("images", ""),
                }
            ),
            "attachments_url": f"/messages/{message_id}/attachments/?"
            + urlencode({"remote": request.GET.get("remote", "")}),
            "history_url": f"/messages/{message_id}/history/?"
            + urlencode({"remote": request.GET.get("remote", ""), "next": back_url}),
            "sender_note": Sender.objects.filter(pk=email)
            .values_list("note", flat=True)
            .first()
            or "",
            "sender_note_url": "/senders/edit/?field=note&" + sender_query,
            "sender_labels_url": "/senders/edit/?field=labels&" + sender_query,
            "sender_all_url": "/?" + urlencode({"sender": email}),
            "unsubscribe_url": f"/messages/{message_id}/unsubscribe/?"
            + urlencode({"next": request.get_full_path()}),
            "reply_url": f"/messages/{message_id}/reply/?"
            + urlencode({"next": request.get_full_path()}),
            "reply_form": ComposeForm(initial=reply_initial(item)),
            "back_url": back_url,
        },
    )


def reply_initial(item: Message) -> dict[str, str]:
    """Reply to all: Reply-To or the sender plus the other To and Cc recipients."""
    account = (Account.objects.values_list("email", flat=True).first() or "").casefold()
    recipients = item.recipients or {}
    # A follow-up on mail we sent continues to the same people, as Gmail does.
    if item.sender_email == account:
        to = recipients.get("To", [])
    else:
        to = (recipients.get("Reply-To") or [item.sender]) + recipients.get("To", [])
    seen = {account}
    fields: dict[str, list[str]] = {"to": [], "cc": []}
    for field, headers in (("to", to), ("cc", recipients.get("Cc", []))):
        for name, address in getaddresses(headers):
            # List each address once, To before Cc, never our own; malformed headers parse empty.
            if address and address.casefold() not in seen:
                seen.add(address.casefold())
                fields[field].append(format_address(name, address))
    # Keep one "Re:" prefix on longer exchanges.
    subject = item.subject
    if not subject.casefold().startswith("re:"):
        subject = "Re: " + subject
    return {
        "to": ", ".join(fields["to"]),
        "cc": ", ".join(fields["cc"]),
        "subject": subject,
    }


def send_mail(values: dict[str, str], item: Message | None) -> None:
    """Send new mail, or a reply into the conversation of `item`."""
    # Sending uses the same Gmail modify permission as label changes.
    if not gmail.can_label():
        raise APIError(
            403, "Reconnect Gmail and grant the Gmail modify permission to send mail."
        )
    with gmail.service() as client:
        original = None
        # Replies need the original's Message-ID; new mail has no original.
        if item:
            original = gmail.get_message_details(client, item.id)
            # Gmail can delete the original while the reply is being written.
            if original is None:
                raise APIError(404, "The original message is no longer in Gmail.")
        gmail.send_message(
            client, outgoing_mail(values, original), item.thread_id if item else None
        )
    sync_requested.set()
    # Show the sent reply when the reader reloads its conversation.
    if item:
        caches["reader"].delete(reader_key("thread", item.thread_id))


@require_http_methods(["GET", "POST"])
def compose(request: HttpRequest, message_id: str | None = None) -> HttpResponse:
    """Write new mail, or a reply when opened from a message; failures keep the draft."""
    item = read_message(message_id, remote=False) if message_id else None
    form = ComposeForm(
        request.POST if request.method == "POST" else None,
        initial=reply_initial(item) if item else None,
    )
    status = 200
    if request.method == "POST":
        status = 400
        if form.is_valid():
            try:
                send_mail(form.cleaned_data, item)
            except (APIError, HttpError, RefreshError, OSError) as error:
                status = form_error(form, error)
            else:
                return redirect_next(request)
    return page(
        request,
        "inbox/compose.html",
        {"form": form, "reply_to": item, "send_url": request.get_full_path()},
        status=status,
    )


@require_http_methods(["GET"])
def attachments(request: HttpRequest, message_id: str) -> HttpResponse:
    item = read_message(
        message_id, remote=request.GET.get("remote") == "1", formatted=True
    )
    return render(request, "inbox/attachments.html", {"message": item})


@require_http_methods(["GET"])
def download_attachment(
    request: HttpRequest, message_id: str, part_id: str
) -> HttpResponse:
    """Download only an actual MIME attachment of this message, never an arbitrary provider ID."""
    # Downloads require a connected account but never populate the inbox or AI cache.
    if not Account.objects.filter(pk=1).exists():
        raise APIError(401, "Connect Gmail first.")
    with gmail.service() as client:
        raw = gmail.get_message_details(client, message_id)
        # A link may outlive its message or MIME part.
        if raw is None:
            raise APIError(404, "This message is no longer available in Gmail.")
        part = next(
            (
                part
                for path, part in content.file_parts(raw.get("payload", {}))
                if path == part_id
            ),
            None,
        )
        # Reject stale or invented MIME paths before issuing any attachment request.
        if part is None:
            raise APIError(404, "This attachment is no longer available.")
        body = part.get("body", {})
        # Bound local memory use; larger files can still be downloaded directly from Gmail.
        if not 0 <= body.get("size", 0) <= content.DOWNLOAD_BYTES:
            raise APIError(413, "Attachments over 25 MiB must be downloaded in Gmail.")
        data = body.get("data", "")
        # Gmail returns small parts inline and larger parts through a separate attachment ID.
        if body.get("attachmentId"):
            data = gmail.get_attachment(client, message_id, body["attachmentId"])
    # Check encoded and decoded size; MIME size metadata is sender-controlled.
    if len(data) > (content.DOWNLOAD_BYTES + 2) * 4 // 3:
        raise APIError(413, "Attachments over 25 MiB must be downloaded in Gmail.")
    try:
        decoded = base64.b64decode(
            data + "=" * (-len(data) % 4), altchars=b"-_", validate=True
        )
    except ValueError as error:
        raise APIError(
            502, "This attachment could not be decoded. Try downloading it in Gmail."
        ) from error
    # Padding/size metadata cannot bypass the decoded byte limit.
    if len(decoded) > content.DOWNLOAD_BYTES:
        raise APIError(413, "Attachments over 25 MiB must be downloaded in Gmail.")
    # Missing provider bytes are not a successful empty-file download.
    if not decoded and body.get("size", 0):
        raise APIError(
            502, "This attachment is unavailable. Try downloading it in Gmail."
        )
    filename = (
        (part.get("filename") or "attachment").replace("\\", "/").rsplit("/", 1)[-1]
    )
    filename = "".join(char for char in filename if char.isprintable()) or "attachment"
    response = HttpResponse(decoded, content_type="application/octet-stream")
    response["Content-Disposition"] = content_disposition_header(True, filename)
    # Even a direct navigation must download rather than execute an HTML/SVG attachment.
    response["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return response


@require_http_methods(["GET"])
def message_history(request: HttpRequest, message_id: str) -> HttpResponse:
    """Nonessential sender history loads after the selected message, without blocking it."""
    item = read_message(message_id, remote=request.GET.get("remote") == "1")
    email = item.sender_email
    history: list[Message] = []
    error = ""
    # Malformed From headers cannot form an exact-sender search.
    if email and "@" in email:
        try:

            def load_history() -> tuple[list[Message], str | None]:
                with gmail.service() as client:
                    return gmail.get_sender_history(client, email, None)

            messages, _ = reader_cached("sender", email, load_history)
            history = [other for other in messages if other.id != message_id][:5]
        except (APIError, HttpError, RefreshError, OSError):
            error = "Sender history is unavailable. Reload to retry."
    for other in history:
        other.url = f"/messages/{other.id}/?" + urlencode(
            {"remote": "1", "next": safe_next(request)}
        )
    return render(
        request, "inbox/history.html", {"history": history, "history_error": error}
    )


@require_http_methods(["GET"])
def message_body(request: HttpRequest, message_id: str) -> HttpResponse:
    """The sanitized email as a sandboxed document, framed by the reader and feed."""

    def load_part(attachment_id: str) -> str:
        # Only MIME-linked body/image IDs reach this loader, never external URLs or documents.
        def load() -> str:
            with gmail.service() as client:
                return gmail.get_attachment(client, message_id, attachment_id)

        return reader_cached("inline", message_id + ":" + attachment_id, load)

    # External images load by default; a view can block them, e.g. to avoid tracking.
    external = request.GET.get("images") != "0"
    try:
        item = read_message(
            message_id, remote=request.GET.get("remote") == "1", formatted=True
        )
        response = HttpResponse(
            content.formatted_html(
                item.rich_body or {},
                item.body or "",
                load_part,
                external=external,
            )
        )
    except (APIError, HttpError, RefreshError, OSError) as error:
        # Failures keep the isolation headers too.
        response = HttpResponse(
            "Formatted email is unavailable. Choose Plain text above, or reload to retry.",
            status=error.status_code if isinstance(error, APIError) else 502,
        )
    # Without scripts, same-origin only lets the app measure the frame's height.
    response["Content-Security-Policy"] = (
        "sandbox allow-same-origin allow-popups allow-popups-to-escape-sandbox; default-src 'none'; "
        "script-src 'none'; style-src 'unsafe-inline'; img-src data:"
        + (" https:" if external else "")
        + "; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
    )
    response["X-Frame-Options"] = "SAMEORIGIN"
    response["Referrer-Policy"] = "no-referrer"
    return response


def save_sender(email: str, values: dict[str, Any]) -> None:
    changes: list[tuple[Tab, list[str]]] = []
    # Note-only edits do not need Gmail access or sender-filter changes.
    if "labels" in values:
        require_label_access()
        configured = list(Tab.objects.all())
        # A tab may have been removed while the sender editor was open.
        if not set(values["labels"]).issubset(
            {tab.label_id for tab in configured if tab.label_id}
        ):
            raise APIError(409, "A label tab changed. Reopen the sender menu.")
        for tab in configured:
            people = set(tab.people)
            wanted = tab.label_id in values["labels"]
            # Unchanged rules must not issue Gmail requests.
            if (email in people) == wanted:
                continue
            # Validate every tab before changing any remote filters.
            if wanted and len(people) >= 100:
                raise APIError(400, "This label already has 100 sender rules.")
            changes.append(
                (tab, sorted(people | {email} if wanted else people - {email}))
            )

    for tab, people in changes:
        sender_filters.replace(tab.label_id, tab.people, people)

    # Save local changes after remote filter edits succeed.
    for tab, people in changes:
        tab.people = people
        tab.save(update_fields=["people"])
    # The form may submit a note independently of label selections.
    if "note" in values:
        Sender.objects.update_or_create(email=email, defaults={"note": values["note"]})
    # Rule edits request sync and label application to stored inbox mail.
    if "labels" in values:
        sync_requested.set()


@require_http_methods(["GET", "POST"])
def sender_edit(request: HttpRequest) -> HttpResponse:
    email = sender_email(request)
    field = request.GET.get("field", "note")
    # A sender editor never accepts arbitrary model field names.
    if field not in {"note", "labels"}:
        raise APIError(400, "Choose the note or labels editor.")
    configured = list(Tab.objects.exclude(label_id=None))
    form = SenderForm(
        request.POST if request.method == "POST" else None,
        field=field,
        choices=[(tab.label_id, tab.name) for tab in configured],
        initial={
            "note": Sender.objects.filter(pk=email)
            .values_list("note", flat=True)
            .first()
            or "",
            "labels": [tab.label_id for tab in configured if email in tab.people],
        },
    )
    status = 200
    if request.method == "POST":
        status = 400
        if form.is_valid():
            try:
                save_sender(email, form.cleaned_data)
            except (APIError, HttpError, RefreshError, OSError) as error:
                status = form_error(form, error, field)
            else:
                return redirect_next(request)
    return page(
        request,
        "inbox/sender_form.html",
        {
            "form": form,
            "sender": email,
            "field": field,
            "can_manage_filters": gmail.can_manage_filters(),
        },
        status=status,
    )


@require_http_methods(["POST"])
def mark_done(request: HttpRequest, message_id: str) -> HttpResponse:
    """Feed tabs mark mail read and archive it once seen; sync refreshes cached labels."""
    require_label_access()
    with gmail.service() as client:
        # Only the seen message; the rest of its conversation stays in the inbox.
        gmail.remove_labels_from_message(client, message_id, ["UNREAD", "INBOX"])
    sync_requested.set()
    return HttpResponse(status=204)


@require_http_methods(["POST"])
def archive(request: HttpRequest, message_id: str) -> HttpResponse:
    try:
        require_label_access()
        # The reader stores every message it shows, so its conversation ID is local.
        thread_id = (
            Message.objects.filter(pk=message_id)
            .values_list("thread_id", flat=True)
            .first()
        )
        # Sync removes mail deleted in Gmail while its reader is still open.
        if thread_id is None:
            raise APIError(404, "This message is no longer available in Gmail.")
        with gmail.service() as client:
            gmail.archive_thread(client, thread_id)
        sync_requested.set()
        # Only this conversation changed; keep others, such as the preloaded next mail.
        caches["reader"].delete(reader_key("thread", thread_id))
    except (APIError, HttpError, RefreshError, OSError) as error:
        status, detail = operation_error(error)
        return page(
            request,
            "inbox/action_error.html",
            {"detail": detail, "action_url": f"/messages/{message_id}/archive/"},
            status=status,
        )
    return redirect_next(request)


def confirm_unsubscribe(message_id: str) -> None:
    require_label_access()
    with gmail.service() as client:
        message = gmail.get_message_details(client, message_id)
        # Recheck advertised metadata at confirmation time, not hidden POST values.
        if message is None:
            raise APIError(404, "This message is no longer available in Gmail.")
        summary = message_from_gmail(message)
        if not summary.unsubscribe or not summary.sender_email:
            raise APIError(400, "This message has no supported unsubscribe option.")
        label: Label | None = next(
            (
                item
                for item in gmail.list_labels(client)
                if item["name"].casefold() == "unsubscribed" and item["type"] == "user"
            ),
            None,
        )
        if label is None:
            label = gmail.create_label(client, "unsubscribed")
        apply_message_update(message)
        gmail.add_label_to_message(client, message_id, label["id"])
        tab = Tab.objects.filter(label_id=label["id"]).first() or Tab(
            label_id=label["id"],
            name=label["name"],
            position=(Tab.objects.aggregate(value=Max("position"))["value"] or 0) + 1,
        )
        people = sorted(set(tab.people) | {summary.sender_email})
        sender_filters.replace(tab.label_id, tab.people, people)
        tab.people = people
        tab.save()
        sync_requested.set()


@require_http_methods(["GET", "POST"])
def unsubscribe(request: HttpRequest, message_id: str) -> HttpResponse:
    form = UnsubscribeForm(request.POST if request.method == "POST" else None)
    status = 200
    item: Message | None = None
    try:
        # Inspect advertised unsubscribe metadata without fetching any external URL.
        with gmail.service() as client:
            raw = gmail.get_message_details(client, message_id)
        if raw is None:
            raise APIError(404, "This message is no longer available in Gmail.")
        item = message_from_gmail(raw)
        if not item.unsubscribe or not item.sender_email:
            raise APIError(400, "This message has no supported unsubscribe option.")
        if request.method == "POST":
            status = 400
            # Opening the advertised link is not proof of success; require explicit confirmation.
            if form.is_valid():
                confirm_unsubscribe(message_id)
                return redirect_next(request, default=f"/messages/{message_id}/")
    except (APIError, HttpError, RefreshError, OSError) as error:
        status = form_error(form, error)
    return page(
        request,
        "inbox/unsubscribe.html",
        {"form": form, "message": item},
        status=status,
    )
