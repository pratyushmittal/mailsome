"""Tab management and mail-list request behavior; ingestion belongs in test_pipeline."""

import base64
import copy
from email import message_from_bytes
from email.policy import default
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from django.test import Client as DjangoClient
from pytest_bdd import given, scenario, then, when
from test_inbox import (
    NOW,
    add_tab,
    conversation_ready,
    create_label,
    enable_ai,
    formatted_message_ready,
    http_error,
    mail,
    seed_account,
    sender_rule,
    synced,
)
from test_inbox import (
    api as api_fixture,
)
from test_inbox import (
    client as client_fixture,
)

from accounts.models import Account
from classifications import usage
from classifications import utils as classification_utils
from classifications.models import AIRequest
from inbox import gmail
from inbox import views as app
from inbox.models import Message, Tab
from inbox.utils import apply_message_update
from jobs import pipeline

api = api_fixture
client = client_fixture


@pytest.mark.parametrize(
    "name,label_id", [("Humans", "Label_humans"), ("Receipts", "Label_new")]
)
@scenario(
    "views.feature",
    "Create or pin a Gmail label and manage its tab without deleting the label",
)
def test_create_open_edit_and_unpin_tab(client, tmp_path, api, name, label_id):
    pass


@given("a Gmail account is connected for tab management")
def connected_tab_management(client, tmp_path):
    seed_account(client, tmp_path)


@when("I create or pin a label as a tab and open it", target_fixture="opened_label_tab")
def opened_label_tab(client, name, label_id):
    assert client.post("/tabs/new/", data={"name": name}).status_code == 303
    tab = Tab.objects.get()
    assert tab.label_id == label_id
    assert tab.acceptance_threshold == 0.75
    assert client.get("/", query_params={"tab": tab.pk}).status_code == 200
    return tab


@then("the tab selects its label, rejects duplicate pins, and keeps edits")
def verify_tab_selection_and_edits(client, api, name, label_id, opened_label_tab):
    tab = opened_label_tab
    assert api.messages.return_value.list.call_args.kwargs["labelIds"] == [
        "INBOX",
        label_id,
    ]
    if name == "Receipts":
        api.labels.return_value.create.assert_called_once_with(
            userId="me", body={"name": name}
        )
    else:
        api.labels.return_value.create.assert_not_called()
    assert client.post("/tabs/new/", data={"name": name.lower()}).status_code == 409
    assert Tab.objects.count() == 1
    assert (
        client.post(
            f"/tabs/{tab.pk}/edit/",
            data={
                "name": name,
                "description": "Personal conversations",
                "acceptance_threshold": "0.9",
            },
        ).status_code
        == 303
    )
    tab.refresh_from_db()
    assert (
        tab.description == "Personal conversations" and tab.acceptance_threshold == 0.9
    )
    assert (
        client.get(f"/tabs/{tab.pk}/edit/")
        .context["form"]["acceptance_threshold"]
        .value()
        == 0.9
    )


@when("I unpin the tab")
def unpin_opened_tab(client, opened_label_tab):
    tab = opened_label_tab
    assert (
        client.post(f"/tabs/{tab.pk}/edit/", data={"action": "delete"}).status_code
        == 303
    )


@then("the tab is gone but its Gmail label is not deleted")
def verify_unpinned_label_retained(client, api, opened_label_tab):
    tab = opened_label_tab
    assert not Tab.objects.exists()
    assert (
        client.post(f"/tabs/{tab.pk}/edit/", data={"action": "delete"}).status_code
        == 404
    )
    assert client.get("/", query_params={"tab": tab.pk}).status_code == 404
    api.labels.return_value.delete.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"name": ""},
        {"name": "a" * 226},
        {"name": "a", "description": "a" * 2001},
        {"name": "a", "people": ["not-an-email"]},
        {"name": "a", "auto_classify": "yes"},
        {"name": "a", "auto_classify": "on"},
    ],
)
def test_invalid_tabs_are_not_saved(
    client: DjangoClient, payload: dict[str, Any], tmp_path: Path
) -> None:
    seed_account(client, tmp_path)
    assert client.post("/tabs/new/", data=payload).status_code == 400
    assert not Tab.objects.exists()


def test_failed_label_creation_does_not_pin_a_tab(
    client: DjangoClient, tmp_path: Path, api: MagicMock
) -> None:
    synced(client, tmp_path, api)
    api.labels.return_value.create.return_value.execute.side_effect = http_error(400)
    assert create_label(client).status_code == 400
    assert not Tab.objects.exists()
    assert client.post("/tabs/new/", data={"name": "INBOX"}).status_code == 400


def test_gmail_label_rename_preserves_identity_and_deleted_labels_are_not_recreated(
    client: DjangoClient, tmp_path: Path, api: MagicMock
) -> None:
    synced(client, tmp_path, api)
    tab_id = add_tab(client)
    api.labels.return_value.list.return_value.execute.return_value["labels"][0][
        "name"
    ] = "People"
    assert client.get("/tabs/new/").status_code == 200
    assert Tab.objects.get().name == "Humans"  # Browsing must not mutate saved labels.
    assert (
        client.post(f"/tabs/{tab_id}/edit/", data={"name": "People"}).status_code == 303
    )
    assert Tab.objects.get().name == "People"
    assert client.post("/tabs/new/", data={"name": "People"}).status_code == 409
    api.labels.return_value.list.return_value.execute.return_value = {"labels": []}
    # Local preferences no longer query Gmail for an unchanged pin; no label is recreated.
    assert (
        client.post(f"/tabs/{tab_id}/edit/", data={"name": "People"}).status_code == 303
    )
    # Explicit name resolution still verifies the pinned ID rather than creating a replacement.
    assert (
        client.post(f"/tabs/{tab_id}/edit/", data={"name": "Another name"}).status_code
        == 409
    )
    api.labels.return_value.create.assert_not_called()


@scenario("views.feature", "Keep tab ordering when adding or editing tabs")
def test_reordering_persists_and_new_tabs_append_without_moving_edited_tabs(
    client, tmp_path, api
):
    pass


@given("two label tabs already have a saved order", target_fixture="ordered_label_tabs")
def ordered_label_tabs(client, tmp_path, api):
    synced(client, tmp_path, api)
    ids = [
        Tab.objects.create(name="Humans", label_id="Label_humans", position=0).pk,
        Tab.objects.create(name="Receipts", label_id="Label_new", position=1).pk,
    ]
    return ids


@when(
    "I reverse their order, append a new tab, and edit an existing tab",
    target_fixture="reorder_append_and_edit_tabs",
)
def reorder_append_and_edit_tabs(client, api, ordered_label_tabs):
    ids = ordered_label_tabs
    assert (
        client.post("/tabs/order/", data={"order": list(reversed(ids))}).status_code
        == 303
    )
    assert [tab["id"] for tab in client.get("/").context["tabs"]] == list(reversed(ids))
    api.labels.return_value.list.return_value.execute.return_value["labels"].append(
        {"id": "Label_third", "name": "Third", "type": "user"}
    )
    assert client.post("/tabs/new/", data={"name": "Third"}).status_code == 303
    third = Tab.objects.get(name="Third").pk
    assert (
        client.post(
            f"/tabs/{ids[0]}/edit/", data={"name": "Humans", "description": "Updated"}
        ).status_code
        == 303
    )
    return third


@then("the saved order contains the reordered tabs followed by the new tab")
def verify_saved_tab_order(ordered_label_tabs, reorder_append_and_edit_tabs):
    ids = ordered_label_tabs
    third = reorder_append_and_edit_tabs
    assert list(Tab.objects.values_list("pk", flat=True)) == [ids[1], ids[0], third]


@pytest.mark.parametrize(
    "order, status",
    [
        ([], 400),
        (["true", "2"], 400),
        (["1", "1"], 409),
        (["[1]", "2"], 400),
        (["abc", "2"], 400),
        (["1"], 409),
        (["1", "999"], 409),
        (["", "2"], 400),
    ],
)
def test_invalid_or_stale_order_is_atomic(
    client: DjangoClient, tmp_path: Path, api: MagicMock, order: Any, status: int
) -> None:
    synced(client, tmp_path, api)
    Tab.objects.create(name="Humans", label_id="Label_humans", position=0)
    Tab.objects.create(name="Receipts", label_id="Label_new", position=1)
    # Compare every saved field: model equality alone would only compare primary keys.
    before = list(Tab.objects.values())
    assert client.post("/tabs/order/", data={"order": order}).status_code == status
    assert list(Tab.objects.values()) == before
    assert api.mock_calls == []


@pytest.mark.parametrize("legacy", [False, True])
@scenario("views.feature", "Exclude pinned labels and legacy queries from Others")
def test_others_query_excludes_only_pinned_tabs_and_updates_after_unpinning(
    client, tmp_path, api, legacy
):
    pass


@given(
    "pinned labels and optional legacy query tabs coexist with an unpinned label",
    target_fixture="pinned_others_exclusions",
)
def pinned_others_exclusions(client, tmp_path, api, legacy):
    seed_account(client, tmp_path)
    human = Tab.objects.create(name="Humans", label_id="Label_humans", position=0)
    Tab.objects.create(name="Receipts", label_id="Label_new", position=1)
    api.labels.return_value.list.return_value.execute.return_value = {
        "labels": [
            {"id": "Label_humans", "name": "Humans"},
            {"id": "Label_new", "name": "Receipts"},
            {"id": "Label_unpinned", "name": "Unpinned"},
        ]
    }
    expected = ['-label:"Humans"', '-label:"Receipts"']
    if legacy:
        Tab.objects.create(name="Old search", query="subject:hello", position=2)
        Tab.objects.create(
            name="Another search", query="from:friend@example.com", position=3
        )
        expected += ["-(subject:hello)", "-(from:friend@example.com)"]
    api.messages.return_value.list.return_value.execute.side_effect = None
    api.messages.return_value.list.return_value.execute.return_value = {"messages": []}
    return expected, human


@when("I open Others", target_fixture="open_others")
def open_others(client, api):
    assert client.get("/").status_code == 200
    request = api.messages.return_value.list.call_args.kwargs
    return request


@then("Gmail receives inbox restrictions and exclusions for only the pinned tabs")
def verify_others_query(pinned_others_exclusions, open_others):
    expected, _human = pinned_others_exclusions
    request = open_others
    assert request["labelIds"] == ["INBOX"]
    assert request["q"] == " ".join(expected)


@when("I unpin a label and reopen Others")
def unpin_and_reopen_others(client, pinned_others_exclusions):
    _expected, human = pinned_others_exclusions
    assert (
        client.post(f"/tabs/{human.pk}/edit/", data={"action": "delete"}).status_code
        == 303
    )
    assert client.get("/").status_code == 200


@then("the removed tab no longer contributes an exclusion")
def verify_removed_exclusion(api, pinned_others_exclusions):
    expected, _human = pinned_others_exclusions
    assert api.messages.return_value.list.call_args.kwargs["q"] == " ".join(
        expected[1:]
    )


@pytest.mark.parametrize("selected_tab", [False, True])
@scenario(
    "views.feature", "Search all matching Gmail mail regardless of the selected tab"
)
def test_search_and_pagination_pass_query_without_tab_or_inbox_restrictions(
    client, tmp_path, api, selected_tab
):
    pass


@given(
    "a search query is opened with or without a selected label tab",
    target_fixture="global_search_context",
)
def global_search_context(client, tmp_path, api, selected_tab):
    seed_account(client, tmp_path)
    tab = Tab.objects.create(name="Humans", label_id="Label_humans")
    query = 'from:human@example.com -subject:"Weekly update"'
    params = {"q": query, **({"tab": tab.pk} if selected_tab else {})}
    api.messages.return_value.list.return_value.execute.side_effect = None
    api.messages.return_value.list.return_value.execute.return_value = {"messages": []}
    return params, query


@then("the first and older pages forward the query without tab or inbox restrictions")
def verify_global_search_pages(client, api, global_search_context):
    params, query = global_search_context
    for page in [None, "next"]:
        response = client.get(
            "/", query_params={**params, **({"page": page} if page else {})}
        )
        assert response.status_code == 200
        request = api.messages.return_value.list.call_args.kwargs
        assert request["q"] == query and request["labelIds"] == []
        assert request["pageToken"] == page


def test_invalid_gmail_query_has_an_actionable_error(
    client: DjangoClient, tmp_path: Path, api: MagicMock
) -> None:
    synced(client, tmp_path, api)
    api.messages.return_value.list.return_value.execute.side_effect = http_error(400)
    response = client.get("/", query_params={"q": "bad query"})
    assert response.status_code == 400
    assert "Check the query" in response.text


@scenario(
    "views.feature", "Browse a deferred preview of previous mail from the actual sender"
)
def test_reader_defers_sender_history_and_reuses_five_message_preview(
    client, tmp_path, api
):
    pass


@given("sender history includes older mail and a misleading display-name match")
def mail_for_sender_preview(client, tmp_path, api):
    synced(client, tmp_path, api)
    for i in range(7):
        api.mailbox[f"history-{i}"] = mail(f"history-{i}", days=30)
    api.mailbox["lookalike"] = mail("lookalike")
    api.mailbox["lookalike"]["payload"]["headers"][0]["value"] = (
        '"human@example.com" <other@example.com>'
    )
    api.messages.return_value.list.return_value.execute.side_effect = None
    api.messages.return_value.list.return_value.execute.return_value = {
        "messages": [
            {"id": key}
            for key in ["a", "lookalike", *[f"history-{i}" for i in range(7)]]
        ]
    }
    api.messages.return_value.list.reset_mock()


@when(
    "I open the reader and request its sender-history preview",
    target_fixture="requested_sender_preview",
)
def requested_sender_preview(client, api):
    reader = client.get("/messages/a/")
    api.messages.return_value.list.assert_not_called()
    history = client.get(reader.context["history_url"])
    return history, reader


@then("five other messages from the actual address are shown and the preview is reused")
def verify_exact_sender_preview(client, api, requested_sender_preview):
    history, reader = requested_sender_preview
    assert [item.id for item in history.context["history"]] == [
        f"history-{i}" for i in range(5)
    ]
    client.get(reader.context["history_url"])
    assert api.messages.return_value.list.call_count == 1


@scenario("views.feature", "Edit local tab preferences without contacting Gmail")
def test_local_tab_edits_survive_gmail_outage_and_preserve_bound_errors(
    client, tmp_path, api
):
    pass


@given(
    "a saved tab exists while Gmail label requests are unavailable",
    target_fixture="tab_during_gmail_outage",
)
def tab_during_gmail_outage(client, tmp_path, api):
    synced(client, tmp_path, api)
    tab_id = add_tab(client)
    api.reset_mock()
    api.labels.return_value.list.return_value.execute.side_effect = http_error(503)
    return tab_id


@when("I open, validate, edit, and unpin the local tab")
def edit_local_tab(client, tab_during_gmail_outage):
    tab_id = tab_during_gmail_outage
    assert client.get(f"/tabs/{tab_id}/edit/").status_code == 200
    invalid = client.post(
        f"/tabs/{tab_id}/edit/", data={"name": "Humans", "people": "not an address"}
    )
    assert invalid.status_code == 400
    assert invalid.context["form"].is_bound
    assert "people" in invalid.context["form"].errors
    assert (
        client.post(
            f"/tabs/{tab_id}/edit/",
            data={"name": "Humans", "description": "Local change"},
        ).status_code
        == 303
    )
    assert (
        client.post(f"/tabs/{tab_id}/edit/", data={"action": "delete"}).status_code
        == 303
    )


@then("the local operations never request Gmail labels")
def verify_no_gmail_label_requests(api):
    api.labels.return_value.list.assert_not_called()


def test_thread_failure_keeps_selected_message_and_reports_missing_context(
    client, tmp_path, api
) -> None:
    synced(client, tmp_path, api)
    api.threads.return_value.get.return_value.execute.side_effect = http_error(404)
    api.threads.return_value.get.side_effect = None
    response = client.get("/messages/a/")
    assert response.status_code == 200
    assert "Conversation unavailable" in response.text
    assert response.context["message"].id == "a"


def test_failed_formatted_content_can_show_a_safe_retry_message(
    client, tmp_path, api
) -> None:
    synced(client, tmp_path, api)
    # This fixture exercises a missing body rather than the content already cached by sync.
    Message.objects.filter(pk="a").update(body=None, rich_body=None)
    api.mailbox["a"] = http_error(503)
    response = client.get("/messages/a/body/")
    assert response.status_code == 502
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert "sandbox" in response.headers["Content-Security-Policy"]
    assert "Plain text" in response.text


@scenario(
    "views.feature",
    "Reuse conversation details until an archive or account change invalidates them",
)
def test_reader_cache_reuse_and_invalidation_lifecycle(client, tmp_path, api):
    pass


@given(
    "a stored conversation has an older reply",
    target_fixture="conversation_with_older_reply",
)
def conversation_with_older_reply(client, tmp_path, api):
    conversation_ready(client, tmp_path, api)
    url = "/messages/old/?remote=1"
    return url


@when("I revisit the older reply twice")
def revisit_older_reply(client, conversation_with_older_reply):
    url = conversation_with_older_reply
    for _ in range(2):
        assert client.get(url).status_code == 200


@then("the conversation is fetched only once")
def verify_reused_conversation(api):
    assert api.threads.return_value.get.call_count == 1


@when("archiving fails and I revisit the reader")
def fail_archive_and_revisit(client, api, conversation_with_older_reply):
    url = conversation_with_older_reply
    api.threads.return_value.modify.side_effect = http_error(403)
    assert client.post("/messages/a/archive/").status_code == 403
    assert client.get(url).status_code == 200


@then("the failed archive leaves the reader cache usable")
def verify_cache_after_failed_archive(api):
    assert api.threads.return_value.get.call_count == 1


@when("archiving the conversation succeeds")
def archive_cached_conversation(client, api):
    api.threads.return_value.modify.side_effect = None
    api.threads.return_value.modify.reset_mock()
    # The reader preloads the next mail, from another conversation.
    assert client.get("/messages/b/").status_code == 200
    assert client.post("/messages/a/archive/").status_code == 303


@then(
    "the archived conversation is fetched again while other conversations and syncs keep the cache"
)
def verify_archive_invalidates_only_its_conversation(
    client, api, conversation_with_older_reply
):
    # The stored conversation ID avoids downloading the message before archiving.
    assert all(
        request.kwargs["id"] != "a"
        for request in api.messages.return_value.get.call_args_list
    )
    api.threads.return_value.modify.assert_called_once_with(
        userId="me", id="thread-a", body={"removeLabelIds": ["INBOX"]}
    )
    assert client.get("/messages/b/").status_code == 200
    assert api.threads.return_value.get.call_count == 2
    assert client.get(conversation_with_older_reply).status_code == 200
    assert api.threads.return_value.get.call_count == 3
    Account.objects.filter(pk=1).update(synced_at=NOW + 1, history_id="999")
    assert client.get(conversation_with_older_reply).status_code == 200
    assert api.threads.return_value.get.call_count == 3


@when("the connected account changes")
def change_account():
    Account.objects.filter(pk=1).update(email="another@example.com")


@then("the conversation is fetched again and a disconnected account cannot read it")
def verify_account_invalidation(client, api, conversation_with_older_reply):
    assert client.get(conversation_with_older_reply).status_code == 200
    assert api.threads.return_value.get.call_count == 4
    Account.objects.all().delete()
    assert client.get(conversation_with_older_reply).status_code == 401


def test_archive_rejects_mail_removed_from_the_cache(client, tmp_path, api):
    synced(client, tmp_path, api)
    assert client.post("/messages/gone/archive/").status_code == 404
    api.threads.return_value.modify.assert_not_called()


def test_reader_returns_not_found_for_missing_messages(client, tmp_path, api):
    synced(client, tmp_path, api)
    assert client.get("/messages/not-cached/").status_code == 404
    Message.objects.filter(pk="a").update(body=None, rich_body=None)
    del api.mailbox["a"]
    assert client.get("/messages/a/").status_code == 404


@pytest.mark.parametrize("missing", ["body", "recipients", "unsubscribe"])
@scenario("views.feature", "Reuse reader content after downloading missing fields")
def test_reader_downloads_missing_content_or_headers_once(
    client, tmp_path, api, missing
):
    pass


@given("a stored message is missing body or action-header data")
def incomplete_reader_message(client, tmp_path, api, missing):
    synced(client, tmp_path, api)
    Message.objects.filter(pk="a").update(**{missing: None})


@when("I open that message twice")
def open_incomplete_message_twice(client):
    assert client.get("/messages/a/").status_code == 200
    assert client.get("/messages/a/").status_code == 200


@then("full details are downloaded only once")
def verify_downloaded_reader_content(api):
    api.messages.return_value.get.assert_called_once_with(
        userId="me", id="a", format="full"
    )


@pytest.mark.parametrize(
    "oversized", [False, True], ids=["empty-result", "oversized-result"]
)
def test_reader_cache_reuses_small_results_with_ttl_but_skips_oversized_results(
    client, tmp_path, api, monkeypatch, oversized
):
    synced(client, tmp_path, api)
    cache = app.caches["reader"]
    store = MagicMock(wraps=cache.set)
    monkeypatch.setattr(cache, "set", store)
    load = MagicMock(return_value="x" * (2 * 1024 * 1024 + 1) if oversized else [])
    for _ in range(2):
        app.reader_cached("test", "message", load)
    assert load.call_count == (2 if oversized else 1)
    if oversized:
        store.assert_not_called()
    else:
        assert store.call_count == 1
        assert store.call_args.kwargs["timeout"] == 60


@pytest.mark.parametrize(
    "filename,mime",
    [
        ("../../evil.html", "text/html"),
        ("..\\evil.svg", "image/svg+xml"),
        ("résumé.pdf", "application/pdf"),
        ("evil\r\nHeader.txt", "text/plain"),
    ],
)
def test_inline_attachment_is_forced_to_download(client, tmp_path, api, filename, mime):
    synced(client, tmp_path, api)
    api.mailbox["old"]["payload"]["parts"] = [
        {
            "filename": filename,
            "mimeType": mime,
            "body": {"size": 8, "data": base64.urlsafe_b64encode(b"<script>").decode()},
        }
    ]
    response = client.get("/messages/old/attachments/0.0/")
    assert response.status_code == 200
    assert response.content == b"<script>"
    assert response.headers["Content-Disposition"].startswith("attachment;")
    assert "../" not in response.headers["Content-Disposition"]
    assert "\r" not in response.headers["Content-Disposition"]
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]
    api.messages.return_value.attachments.return_value.get.assert_not_called()


def test_download_rejects_unknown_body_parts_oversize_and_invalid_data(
    client, tmp_path, api, monkeypatch
):
    from inbox import content

    formatted_message_ready(client, tmp_path, api)
    assert client.get("/messages/a/attachments/provider-id/").status_code == 404
    assert client.get("/messages/a/attachments/0.1/").status_code == 404
    monkeypatch.setattr(content, "DOWNLOAD_BYTES", 10)
    assert client.get("/messages/a/attachments/0.3/").status_code == 413
    api.messages.return_value.attachments.return_value.get.assert_not_called()
    api.mailbox["a"]["payload"]["parts"][3]["body"]["size"] = 1
    for data, status in (
        ("!invalid", 502),
        (base64.urlsafe_b64encode(b"x" * 12).decode(), 413),
    ):
        api.messages.return_value.attachments.return_value.get.return_value.execute.return_value = {
            "data": data
        }
        assert client.get("/messages/a/attachments/0.3/").status_code == status


@scenario("views.feature", "Download only the selected attachment")
def test_selected_attachment_downloads_provider_bytes(client, tmp_path, api):
    pass


@given("an email has a provider-backed PDF attachment")
def message_with_pdf_attachment(client, tmp_path, api):
    formatted_message_ready(client, tmp_path, api)
    api.messages.return_value.attachments.return_value.get.return_value.execute.return_value = {
        "data": base64.urlsafe_b64encode(b"%PDF-test").decode()
    }


@when("I request the selected attachment", target_fixture="downloaded_attachment")
def downloaded_attachment(client):
    response = client.get("/messages/a/attachments/0.3/")
    return response


@then("the response contains the bytes from that attachment request")
def verify_attachment_response(api, downloaded_attachment):
    response = downloaded_attachment
    assert response.status_code == 200 and response.content == b"%PDF-test"
    api.messages.return_value.attachments.return_value.get.assert_called_once_with(
        userId="me", messageId="a", id="never-fetch"
    )


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/",
        "//evil.example/",
        "/\\evil.example/",
        "/%2fevil.example/",
        "/%0d%0aLocation:evil",
    ],
)
def test_plain_form_return_targets_stay_local(
    client: DjangoClient, target: str
) -> None:
    response = client.post(
        "/senders/edit/?sender=human@example.com&field=note",
        data={"note": "safe", "next": target},
    )
    assert response.status_code == 303
    assert response.headers["Location"].startswith("/")
    assert "evil" not in response.headers["Location"]


@scenario("views.feature", "Confirm an unsubscribe before labeling the sender")
def test_unsubscribe_labels_message_and_reuses_sender_rule(client, tmp_path, api):
    pass


@given(
    "a connected message advertises an unsubscribe destination",
    target_fixture="message_with_unsubscribe_link",
)
def message_with_unsubscribe_link(client, tmp_path, api):
    seed_account(client, tmp_path)
    api.mailbox["a"]["payload"]["headers"].append(
        {"name": "List-Unsubscribe", "value": "<https://example.com/unsubscribe>"}
    )
    url = "/messages/a/unsubscribe/"
    return url


@when("I open its unsubscribe page")
def open_unsubscribe_page(client, message_with_unsubscribe_link):
    url = message_with_unsubscribe_link
    assert client.get(url).status_code == 200


@then("opening the page makes no label changes")
def verify_no_unconfirmed_label_changes(api):
    assert not Tab.objects.exists()
    api.labels.return_value.create.assert_not_called()
    api.messages.return_value.modify.assert_not_called()


@when("I confirm the unsubscribe twice")
def confirm_unsubscribe_twice(client, api, message_with_unsubscribe_link):
    url = message_with_unsubscribe_link
    api.labels.return_value.create.return_value.execute.return_value = {
        "id": "Label_unsubscribed",
        "name": "unsubscribed",
        "type": "user",
    }
    for _ in range(2):
        assert client.post(url, data={"confirmed": "on"}).status_code == 303
        tab = Tab.objects.get()
        assert tab.label_id == "Label_unsubscribed"
        assert tab.people == ["human@example.com"] and not tab.auto_classify


@then("the same label and sender rule are reused without sending mail")
def verify_reused_unsubscribe_rule(api):
    api.labels.return_value.create.assert_called_once()
    api.messages.return_value.modify.assert_called_with(
        userId="me", id="a", body={"addLabelIds": ["Label_unsubscribed"]}
    )
    api.messages.return_value.send.assert_not_called()


@pytest.mark.parametrize("change", ["criteria", "action", "deleted"])
def test_filter_edits_leave_nonmatching_remote_rules_alone(
    client, tmp_path, api, change
):
    seed_account(client, tmp_path)
    sender_rule(client)
    identifier = next(iter(api.filter_store))
    if change == "deleted":
        del api.filter_store[identifier]
    elif change == "criteria":
        api.filter_store[identifier]["criteria"]["subject"] = "Private"
    else:
        api.filter_store[identifier]["action"]["removeLabelIds"] = ["INBOX"]
    before = copy.deepcopy(api.filter_store)
    app.save_sender("other@example.com", {"labels": ["Label_humans"]})
    assert all(api.filter_store[key] == value for key, value in before.items())
    api.settings.return_value.filters.return_value.delete.assert_not_called()


@pytest.mark.parametrize("operation", ["create", "delete"])
@pytest.mark.parametrize("remote_succeeded", [False, True])
def test_filter_edit_retry_reuses_exact_matches_after_interruption(
    client, tmp_path, api, operation, remote_succeeded
):
    seed_account(client, tmp_path)
    sender_rule(client)
    tab = Tab.objects.get()
    method = getattr(api.settings.return_value.filters.return_value, operation)
    original = method.side_effect

    def interrupted(**kwargs):
        def execute(**_):
            if remote_succeeded:
                original(**kwargs).execute()
            raise TimeoutError("lost acknowledgement")

        return MagicMock(execute=MagicMock(side_effect=execute))

    method.side_effect = interrupted
    with pytest.raises(TimeoutError):
        app.save_sender("other@example.com", {"labels": [tab.label_id]})
    assert Tab.objects.get().people == ["human@example.com"]
    method.side_effect = original
    api.reset_mock()
    app.save_sender("other@example.com", {"labels": [tab.label_id]})
    assert len(api.filter_store) == 1
    assert Tab.objects.get().people == ["human@example.com", "other@example.com"]
    if operation == "delete" or remote_succeeded:
        api.settings.return_value.filters.return_value.create.assert_not_called()


def test_duplicate_exact_filters_block_edit_without_remote_or_local_mutation(
    client, tmp_path, api
):
    from mailsome.errors import APIError

    seed_account(client, tmp_path)
    sender_rule(client)
    api.filter_store["duplicate"] = {
        **copy.deepcopy(next(iter(api.filter_store.values()))),
        "id": "duplicate",
    }
    api.reset_mock()
    with pytest.raises(APIError, match="Multiple matching"):
        app.save_tab({}, Tab.objects.get().pk, delete=True)
    assert Tab.objects.exists()
    assert len(api.filter_store) == 2
    api.settings.return_value.filters.return_value.delete.assert_not_called()
    api.settings.return_value.filters.return_value.create.assert_not_called()


@scenario(
    "views.feature",
    "Group sender rules into Gmail filters only when their rules change",
)
def test_sender_filter_create_edit_and_unpin_preserve_unrelated_state(
    client, tmp_path, api
):
    pass


@given(
    "two sender addresses share a proposed label beside an unrelated Gmail filter",
    target_fixture="proposed_grouped_sender_rule",
)
def proposed_grouped_sender_rule(client, tmp_path, api):
    seed_account(client, tmp_path)
    unrelated = {
        "id": "user-filter",
        "criteria": {"subject": "Private"},
        "action": {"addLabelIds": ["Label_humans"]},
    }
    api.filter_store = {"user-filter": unrelated.copy()}
    values = {
        "name": "Humans",
        "description": "Personal mail",
        "auto_classify": "on",
        "people": "USER+news@example.com\nuser+news@example.com\nuser.name@example.com",
    }
    return unrelated, values


@when("I save the sender-label tab", target_fixture="saved_sender_label_tab")
def saved_sender_label_tab(client, proposed_grouped_sender_rule):
    _unrelated, values = proposed_grouped_sender_rule
    assert client.post("/tabs/new/", data=values).status_code == 303
    tab = Tab.objects.get()
    return tab


@then(
    "one additive Gmail filter groups the distinct full addresses",
    target_fixture="verify_grouped_sender_filter",
)
def verify_grouped_sender_filter(api, saved_sender_label_tab):
    tab = saved_sender_label_tab
    assert tab.people == ["user+news@example.com", "user.name@example.com"]
    filters = api.settings.return_value.filters.return_value
    filters.create.assert_called_once_with(
        userId="me",
        body={
            "criteria": {
                "query": '{from:"user+news@example.com" from:"user.name@example.com"}'
            },
            "action": {"addLabelIds": ["Label_humans"]},
        },
    )
    return filters


@when(
    "I edit only the description and reopen its editor",
    target_fixture="edit_sender_tab_description",
)
def edit_sender_tab_description(
    client, api, proposed_grouped_sender_rule, saved_sender_label_tab
):
    _unrelated, values = proposed_grouped_sender_rule
    tab = saved_sender_label_tab
    previous = next(key for key in api.filter_store if key != "user-filter")
    api.reset_mock()
    values["description"] = "Updated description"
    assert client.post(f"/tabs/{tab.pk}/edit/", data=values).status_code == 303
    assert client.get(f"/tabs/{tab.pk}/edit/").status_code == 200
    return previous


@then("the description edit does not reconcile Gmail filters")
def verify_no_filter_reconciliation(api):
    api.settings.assert_not_called()


@when("I remove one sender from the label")
def remove_grouped_sender(client):
    assert (
        client.post(
            "/senders/edit/?field=labels&sender=user%2Bnews@example.com",
            data={"labels": []},
        ).status_code
        == 303
    )


@then("the exact previous filter is replaced while other tab settings remain")
def verify_exact_filter_replacement(
    saved_sender_label_tab, verify_grouped_sender_filter, edit_sender_tab_description
):
    tab = saved_sender_label_tab
    filters = verify_grouped_sender_filter
    previous = edit_sender_tab_description
    tab.refresh_from_db()
    assert tab.people == ["user.name@example.com"]
    assert tab.description == "Updated description" and tab.auto_classify
    filters.create.assert_called_once_with(
        userId="me",
        body={
            "criteria": {"query": '{from:"user.name@example.com"}'},
            "action": {"addLabelIds": ["Label_humans"]},
        },
    )
    filters.delete.assert_called_once_with(userId="me", id=previous)


@when("I unpin the sender-label tab")
def unpin_sender_label_tab(client, saved_sender_label_tab):
    tab = saved_sender_label_tab
    assert (
        client.post(f"/tabs/{tab.pk}/edit/", data={"action": "delete"}).status_code
        == 303
    )


@then("the unrelated filter and historical Gmail labels remain untouched")
def verify_unrelated_filters_preserved(api, proposed_grouped_sender_rule):
    unrelated, _values = proposed_grouped_sender_rule
    assert not Tab.objects.exists()
    assert api.filter_store == {"user-filter": unrelated}
    api.labels.return_value.delete.assert_not_called()
    api.messages.return_value.batchModify.assert_not_called()
    api.messages.return_value.modify.assert_not_called()


def test_reclassification_rejects_unavailable_resets(
    client, tmp_path, api, monkeypatch
):
    synced(client, tmp_path, api)
    enable_ai(client)
    Message.objects.update(ai_classified=True)
    config = classification_utils.settings()
    monkeypatch.setattr(
        classification_utils, "settings", lambda: {**config, "enabled": False}
    )
    assert (
        client.post("/settings/reclassify/", data={"confirm": "on"}).status_code == 400
    )
    assert Message.objects.filter(ai_classified=True).count() == 3


def test_ai_settings_and_context_preserve_keys_consent_and_completed_mail(
    client, tmp_path, api
):
    seed_account(client, tmp_path)
    page = client.get("/settings/")
    assert page.context["form"]["enabled"].value() is False
    assert page.context["usage"]["requests"] == []
    assert client.post("/settings/", data={"enabled": "on"}).status_code == 400
    gmail.get_messages_for_list(api, ["a"])
    Message.objects.update(ai_classified=True, importance=0.4)
    assert (
        client.post(
            "/settings/",
            data={
                "enabled": "on",
                "api_key": "secret-test-key",
                "user_context": "Family mail",
            },
        ).status_code
        == 303
    )
    # An omitted context or blank key must not erase existing configuration.
    assert (
        client.post("/settings/", data={"enabled": "on", "api_key": ""}).status_code
        == 303
    )
    saved = classification_utils.settings()
    assert (
        saved["api_key"] == "secret-test-key" and saved["user_context"] == "Family mail"
    )
    page = client.get("/settings/")
    assert page.context["has_key"] and page.context["form"]["api_key"].value() in (
        None,
        "",
    )
    assert "secret-test-key" not in page.text
    for context in ("Business mail", ""):
        response = client.post(
            "/settings/context/",
            data={
                "user_context": context,
                "next": "/messages/a/",
                "enabled": "",
                "api_key": "replace-key",
            },
        )
        assert (
            response.status_code == 303
            and response.headers["Location"] == "/messages/a/"
        )
        assert classification_utils.settings() == {**saved, "user_context": context}
        assert (
            client.get("/settings/context/").context["form"]["user_context"].value()
            == context
        )
        assert Message.objects.get(pk="a").ai_classified
    assert classification_utils.settings()["importance_threshold"] == 0.7
    levels = ["OTP and login emails", "Expiring subscriptions and meetings"]
    response = client.post(
        "/settings/importance/",
        data={
            "importance_levels": "\n".join(levels),
            "importance_threshold": "0.8",
            "enabled": "",
            "api_key": "not-saved",
        },
    )
    assert response.status_code == 303
    assert classification_utils.settings() == {
        **saved,
        "user_context": "",
        "importance_levels": levels,
        "importance_threshold": 0.8,
    }
    assert client.get("/settings/importance/").context["form"][
        "importance_levels"
    ].value() == "\n".join(levels)
    message = Message.objects.get(pk="a")
    assert message.ai_classified and message.importance == 0.4
    assert client.post("/settings/", data={}).status_code == 303
    assert not classification_utils.settings()["enabled"]


def test_usage_history_pagination_stays_stable_when_new_requests_arrive(client):
    records = AIRequest.objects.bulk_create(
        AIRequest(started_at=NOW, model="test", message_count=1)
        for _ in range(usage.PAGE_SIZE + 1)
    )
    ids = [record.pk for record in records]
    first = client.get("/settings/").context["usage"]
    AIRequest.objects.create(started_at=NOW, model="test", message_count=1)
    second = client.get(
        "/settings/", query_params={"before": first["next_before"]}
    ).context["usage"]
    assert [row["id"] for page in (first, second) for row in page["requests"]] == ids[
        ::-1
    ]
    assert len(first["requests"]) == usage.PAGE_SIZE
    assert second["next_before"] is None
    assert second["summary"]["request_count"] == len(ids) + 1
    assert client.get("/settings/", query_params={"before": "bad"}).status_code == 400


def test_local_request_guards_and_private_response_headers(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Frame-Options"] == "DENY"
    for path, headers in [
        ("/api/sync", {"X-Mailsome-Request": ""}),
        ("/api/sync", {"Origin": "https://foreign.example"}),
        ("/", {"Sec-Fetch-Site": "cross-site"}),
    ]:
        assert client.get(path, headers=headers).status_code == 403


def test_refresh_requests_sync_and_returns_to_current_view(
    client, tmp_path, monkeypatch
):
    seed_account(client, tmp_path)
    requested = MagicMock()
    monkeypatch.setattr("jobs.views.sync_requested.set", requested)
    response = client.post("/refresh/", data={"next": "/?q=hello"})
    assert response.status_code == 303 and response.headers["Location"] == "/?q=hello"
    requested.assert_called_once_with()


def test_local_static_assets_are_buffered_for_asgi(client):
    from django.conf import settings

    response = client.get("/static/style.css")
    assert response.status_code == 200 and not response.streaming
    assert response.content == (settings.BASE_DIR / "static/style.css").read_bytes()
    assert response.headers["Content-Type"].startswith("text/css")


@pytest.mark.parametrize("reply", [True, False], ids=["reply", "compose"])
def test_send_plain_text_reply_or_new_mail(client, tmp_path, api, reply):
    conversation_ready(client, tmp_path, api)
    api.mailbox["a"]["payload"]["headers"] += [
        {"name": "Message-ID", "value": "<a@example.com>"},
        {"name": "References", "value": "<root@example.com>"},
        {"name": "Reply-To", "value": "List <list@example.com>"},
        {
            "name": "To",
            "value": 'Me <ME@example.com>, "Doe, John" <john@example.com>',
        },
        {"name": "Cc", "value": "LIST@example.com, Boss <boss@example.com>"},
    ]
    apply_message_update(api.mailbox["a"])
    if reply:
        # Reply to all: Reply-To first, each address once, never our own address.
        assert client.get("/messages/a/reply/").context["form"].initial == {
            "to": 'List <list@example.com>, "Doe, John" <john@example.com>',
            "cc": "Boss <boss@example.com>",
            "subject": "Re: Subject a",
        }
        # Follow-ups on our own mail go to its recipients; the reader form matches.
        form = client.get("/messages/sent/").context["reply_form"]
        assert (form.initial["to"], form.initial["cc"]) == (
            "Human <human@example.com>",
            "",
        )

    response = client.post(
        "/messages/a/reply/" if reply else "/compose/",
        {
            "to": "List <list@example.com>, b@example.com",
            "cc": "",
            "subject": "Re: Subject a\r\nBcc: x@example.com",
            "body": "Thanks!\nSee you.",
            "next": "/messages/a/",
        },
    )
    assert response.status_code == 303 and response["Location"] == "/messages/a/"
    request = api.messages.return_value.send.call_args.kwargs
    sent = message_from_bytes(
        base64.urlsafe_b64decode(request["body"]["raw"]), policy=default
    )
    assert sent["To"] == "List <list@example.com>, b@example.com"
    assert "Cc" not in sent and "Bcc" not in sent
    # Pasted line breaks cannot inject headers into the subject.
    assert sent["Subject"] == "Re: Subject a Bcc: x@example.com"
    assert sent.get_content().strip() == "Thanks!\nSee you."
    if reply:
        assert request["body"]["threadId"] == "thread-a"
        assert sent["In-Reply-To"] == "<a@example.com>"
        assert sent["References"] == "<root@example.com> <a@example.com>"
    else:
        assert "threadId" not in request["body"] and "In-Reply-To" not in sent
    # Sends are never retried automatically; a retry could deliver a duplicate.
    api.messages.return_value.send.return_value.execute.assert_called_once_with()


@pytest.mark.parametrize("failure", ["invalid address", "gmail"])
def test_failed_send_keeps_the_draft(client, tmp_path, api, failure):
    synced(client, tmp_path, api)
    api.messages.return_value.send.return_value.execute.side_effect = http_error(503)
    response = client.post(
        "/compose/",
        {
            "to": "not an address" if failure == "invalid address" else "b@example.com",
            "subject": "Hello",
            "body": "Draft text",
        },
    )
    assert response.status_code == (400 if failure == "invalid address" else 502)
    assert "Draft text" in response.text
    assert api.messages.return_value.send.called == (failure == "gmail")


def test_feed_tab_shows_full_mail_and_marks_focused_mail_read(client, tmp_path, api):
    synced(client, tmp_path, api)
    assert (
        client.post("/tabs/new/", {"name": "Humans", "feed": "on"}).status_code == 303
    )
    tab = Tab.objects.get(name="Humans")
    assert tab.feed
    api.mailbox["a"]["labelIds"] = ["INBOX", "UNREAD", tab.label_id]
    apply_message_update(api.mailbox["a"])

    # Feed tabs render each email with a lazily loaded body; searches stay ordinary lists.
    feed = client.get(f"/?tab={tab.pk}").text
    assert 'class="feed-mail unread" id="mail-a"' in feed
    assert 'data-fragment="/messages/a/body/?fragment=1" data-lazy' in feed
    assert "data-feed" not in client.get(f"/?tab={tab.pk}&q=hello").text

    # Marking read removes UNREAD in Gmail and leaves cached labels to sync.
    pipeline.sync_requested.clear()
    assert client.post("/messages/a/read/").status_code == 204
    api.messages.return_value.modify.assert_called_with(
        userId="me", id="a", body={"removeLabelIds": ["UNREAD"]}
    )
    assert pipeline.sync_requested.is_set()
    assert "UNREAD" in Message.objects.get(pk="a").labels
