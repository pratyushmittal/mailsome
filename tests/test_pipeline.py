"""Direct ingestion and periodic classification policy."""

import json
from threading import Event
from time import monotonic, sleep
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, call

import httplib2
import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from googleapiclient.errors import HttpError
from pytest_bdd import given, parsers, scenario, then, when
from test_inbox import (
    api as api_fixture,
)
from test_inbox import (
    client as client_fixture,
)
from test_inbox import (
    enable_ai,
    http_error,
    jev_response,
    mail,
    measured_ai,
    seed_account,
    sender_rule,
    synced,
)

from accounts.models import Account
from classifications import labeling, usage
from classifications import utils as classification_utils
from classifications.models import AIRequest, LabelDecision
from inbox import gmail, sender_filters, utils
from inbox.models import Message, Sender, Tab
from inbox.utils import apply_message_update, save_or_create_message
from jobs import pipeline

if TYPE_CHECKING:
    from googleapiclient._apis.gmail.v1.schemas import Message as GmailMessage

api = api_fixture
client = client_fixture


@pytest.mark.parametrize(
    "source",
    [
        "history",
        "expired_history",
        "inbox",
        "tab",
        "search",
        "sender",
        "page",
        "thread",
    ],
)
@scenario("pipeline.feature", "Load encountered mail without preloading the mailbox")
def test_download_triggers_persist_message_content(client, tmp_path, api, source):
    pass


@given(
    "a connected mailbox has a history anchor and an uncached older message",
    target_fixture="mailbox_with_uncached_message",
)
def mailbox_with_uncached_message(client, tmp_path, api):
    seed_account(client, tmp_path)
    gmail.sync(api)
    assert Account.objects.get().history_id == "100"
    assert not Message.objects.exists()
    api.messages.return_value.get.assert_not_called()
    raw = mail("incoming", days=60, labels=["INBOX", "Label_humans"])
    raw["payload"]["headers"] += [
        {"name": "To", "value": "First <first@example.com>"},
        {"name": "to", "value": "Second <second@example.com>"},
        {"name": "List-Unsubscribe", "value": "<https://example.com/unsubscribe>"},
    ]
    raw["payload"]["parts"] = [
        {
            "mimeType": "application/pdf",
            "filename": "invoice.pdf",
            "body": {"attachmentId": "attachment", "size": 40},
        }
    ]
    api.mailbox = {"incoming": raw}
    api.reset_mock()
    return raw


@when(
    "history, expired-history recovery, or an on-demand mail view encounters the message"
)
def encounter_message(client, api, source, monkeypatch):
    if source == "history":
        api.history.return_value.list.return_value.execute.return_value = {
            "historyId": "200",
            "history": [{"messagesAdded": [{"message": {"id": "incoming"}}]}],
        }
        pipeline.synchronize()
        assert Account.objects.get().history_id == "200"
    elif source == "expired_history":
        # The arrival falls inside the one-minute overlap before the last sync.
        previous_sync = int(api.mailbox["incoming"]["internalDate"]) + 30_000
        Account.objects.update(synced_at=previous_sync)
        api.mailbox["cached"] = mail("cached", days=60)
        save_or_create_message(api.mailbox["cached"])
        Message.objects.filter(pk="cached").update(
            body="Saved body",
            rich_body={"html": "Saved HTML"},
            ai_classified=True,
            ai_attempts=2,
        )
        cached = Message.objects.filter(pk="cached").values().get()
        LabelDecision.objects.create(
            message_id="cached",
            label_id="Label_saved",
            source="ai",
            reason="Saved decision",
            applied=True,
        )
        decisions = list(LabelDecision.objects.values())
        api.history.return_value.list.return_value.execute.side_effect = http_error(404)
        api.getProfile.return_value.execute.return_value["historyId"] = "200"
        api.messages.return_value.list.return_value.execute.side_effect = [
            {"messages": [{"id": "cached"}], "nextPageToken": "next"},
            {"messages": [{"id": "incoming"}]},
        ]
        persist = utils.apply_message_update
        persisted = []

        def save_before_checkpoint(message, **kwargs):
            persist(message, **kwargs)
            persisted.append(message["id"])
            assert Account.objects.values_list("history_id", "synced_at").get() == (
                "100",
                previous_sync,
            )

        monkeypatch.setattr(utils, "apply_message_update", save_before_checkpoint)
        pipeline.synchronize()
        assert persisted == ["cached", "incoming"]
        listings = api.messages.return_value.list.call_args_list
        assert [request.kwargs["q"] for request in listings] == [
            f"after:{previous_sync // 1000 - 60}",
        ] * 2
        assert [request.kwargs["pageToken"] for request in listings] == [None, "next"]
        assert api.mock_calls.index(
            call.getProfile(userId="me")
        ) < api.mock_calls.index(call.messages().list(**listings[0].kwargs))
        assert Message.objects.filter(pk="cached").values().get() == cached
        assert list(LabelDecision.objects.values()) == decisions
        account = Account.objects.get()
        assert account.history_id == "200" and account.synced_at != previous_sync
    elif source == "thread":
        gmail.get_thread_messages(api, "thread-incoming")
    else:
        query = {}
        if source == "tab":
            assert client.post("/tabs/new/", data={"name": "Humans"}).status_code == 303
            query = {"tab": Tab.objects.get().pk}
        elif source == "search":
            query = {"q": "from:human@example.com"}
        elif source == "sender":
            query = {"sender": "human@example.com"}
        elif source == "page":
            query = {"page": "next"}
            api.messages.return_value.list.return_value.execute.side_effect = None
            api.messages.return_value.list.return_value.execute.return_value = {
                "messages": [{"id": "incoming"}]
            }
        response = client.get("/", query_params=query)
        assert response.status_code == 200
        assert [message.id for message in response.context["messages"]] == ["incoming"]
        api.messages.return_value.get.assert_called_once_with(
            userId="me", id="incoming", format="full"
        )
        api.messages.return_value.get.reset_mock()
        assert client.get("/", query_params=query).status_code == 200
        api.messages.return_value.get.assert_not_called()


@then("its metadata and available bodies are stored without downloading attachments")
def verify_ingested_content(api, mailbox_with_uncached_message):
    raw = mailbox_with_uncached_message
    message = Message.objects.get(pk="incoming")
    assert message.thread_id == "thread-incoming"
    assert message.sender_email == "human@example.com"
    assert message.subject == "Subject incoming"
    assert message.received_at == int(raw["internalDate"])
    assert message.labels == raw["labelIds"]
    assert message.recipients["To"] == [
        "First <first@example.com>",
        "Second <second@example.com>",
    ]
    assert message.unsubscribe == "https://example.com/unsubscribe"
    assert message.attachment_count == 1
    assert message.body == "Hello from a human."
    assert message.rich_body["attachments"][0]["name"] == "invoice.pdf"
    api.messages.return_value.attachments.assert_not_called()


@pytest.mark.parametrize("count", [0, 1, 50, 51, 111])
def test_detail_downloads_use_batches_of_at_most_fifty(api, count):
    api.mailbox = {str(i): mail(str(i)) for i in range(count)}
    result = gmail.get_messages_for_list(api, list(api.mailbox))
    assert [item.id for item in result] == list(api.mailbox)
    assert api.batch_sizes == [min(50, count - start) for start in range(0, count, 50)]
    api.reset_mock()
    gmail.get_messages_for_list(api, list(api.mailbox))
    api.new_batch_http_request.assert_not_called()


@scenario("pipeline.feature", "Keep the history cursor when synchronization fails")
def test_failed_sync_does_not_advance_cursor(api, monkeypatch):
    pass


@given(
    parsers.parse('Gmail fails during a sync at "{failure}"'),
    target_fixture="failure",
)
def failing_history_request(api, monkeypatch, failure):
    monkeypatch.setattr(gmail.time, "sleep", lambda _: None)
    Account.objects.create(email="me@example.com", history_id="100")
    api.mailbox = {"good": mail("good"), "bad": http_error(503)}
    page = {
        "historyId": "200",
        "history": [{"messagesAdded": [{"message": {"id": "good"}}]}],
    }
    history = api.history.return_value.list.return_value.execute
    if failure == "history page":
        history.side_effect = [{**page, "nextPageToken": "next"}, http_error(503)]
    elif failure == "detail download":
        page["history"][0]["messagesAdded"].append({"message": {"id": "bad"}})
        history.return_value = page
    else:
        history.side_effect = http_error(404)
        api.getProfile.return_value.execute.side_effect = http_error(503)
    return failure


@when("history synchronization is attempted")
def attempt_failing_sync(api):
    with pytest.raises(HttpError):
        gmail.sync(api)


@then("the cursor and sync timestamp do not advance and successful downloads remain")
def verify_unchanged_cursor(failure):
    account = Account.objects.get()
    assert account.history_id == "100"
    assert account.synced_at is None
    if failure != "cursor replacement":
        assert Message.objects.filter(pk="good").exists()


def test_batch_missing_message_does_not_abort_other_downloads(api):
    api.mailbox = {"a": mail("a")}
    result = gmail.get_messages_for_list(api, ["gone", "a"])
    assert [item.id for item in result] == ["a"]
    assert api.batch_sizes == [2]


@pytest.mark.parametrize("kind", ["full", "headers", "labels", "delete"])
def test_message_persistence_updates_only_owned_fields(kind):
    apply_message_update(mail("a"))
    # Empty text is saved content too, not a request to download it again.
    rich = {"html": "Saved HTML", "attachments": []}
    Message.objects.filter(pk="a").update(
        body="",
        rich_body={"html": "Saved HTML"} if kind == "full" else rich,
        ai_classified=True,
        ai_attempts=2,
        attachment_count=3,
    )
    LabelDecision.objects.create(
        message_id="a",
        label_id="Label_humans",
        source="ai",
        reason="Saved",
        applied=True,
    )
    response: GmailMessage = mail("a", labels=[])
    response["payload"]["headers"][1]["value"] = "Updated subject"
    if kind == "headers":
        response["payload"] = {"headers": response["payload"]["headers"]}
    elif kind in {"labels", "delete"}:
        response = {"id": "a", "labelIds": []}
    apply_message_update(
        response, labels_only=kind == "labels", deleted=kind == "delete"
    )
    if kind == "delete":
        assert not Message.objects.filter(pk="a").exists()
    else:
        message = Message.objects.get(pk="a")
        assert message.labels == []
        assert message.body == ""
        assert message.rich_body["html"] == "Saved HTML"
        assert message.rich_body["attachments"] == []
        assert message.ai_classified and message.ai_attempts == 2
        assert LabelDecision.objects.get().applied
        assert message.subject == (
            "Subject a" if kind == "labels" else "Updated subject"
        )
        assert message.attachment_count == (0 if kind == "full" else 3)


@pytest.mark.parametrize(
    "status,reason,attempts",
    [
        (503, "backendError", 3),
        (429, "rateLimitExceeded", 3),
        (403, "rateLimitExceeded", 3),
        (403, "userRateLimitExceeded", 3),
        (400, "badRequest", 1),
        (403, "insufficientPermissions", 1),
    ],
)
def test_batch_retries_only_transient_failed_reads(
    api, monkeypatch, status, reason, attempts
):
    monkeypatch.setattr(gmail.time, "sleep", lambda _: None)
    error = HttpError(
        httplib2.Response({"status": str(status)}),
        json.dumps(
            {"error": {"message": "Fake Gmail error", "errors": [{"reason": reason}]}}
        ).encode(),
    )
    api.mailbox = {"good": mail("good"), "bad": error}
    with pytest.raises(HttpError):
        gmail.update_messages(api, ["good", "bad"])
    assert Message.objects.filter(pk="good").exists()
    assert api.batch_sizes == [2] + [1] * (attempts - 1)


def test_batch_propagates_transport_errors_without_retrying(api, monkeypatch):
    monkeypatch.setattr(gmail.time, "sleep", lambda _: None)
    batch = MagicMock()
    batch.execute.side_effect = TimeoutError()
    api.new_batch_http_request.side_effect = None
    api.new_batch_http_request.return_value = batch
    with pytest.raises(TimeoutError):
        gmail.update_messages(api, ["a"])
    api.new_batch_http_request.assert_called_once()
    batch.execute.assert_called_once()


@pytest.mark.parametrize("failed_requests", [0, 1, 2, 3])
def test_ai_automatically_retries_without_resetting_attempts_on_recovery(
    client, tmp_path, api, monkeypatch, failed_requests
):
    seed_account(client, tmp_path)
    enable_ai(client)
    gmail.get_messages_for_list(api, ["a"])
    _, provider = measured_ai(monkeypatch)
    original = provider.system_one.side_effect

    def respond(**kwargs):
        assert Message.objects.get(pk="a").ai_attempts == provider.system_one.call_count
        if provider.system_one.call_count <= failed_requests:
            raise TimeoutError("SECRET provider content")
        return original(**kwargs)

    provider.system_one.side_effect = respond
    for attempt in range(6):
        if attempt < min(failed_requests, 3):
            with pytest.raises(TimeoutError):
                labeling.process()
        else:
            labeling.process()
        usage.recover()
    message = Message.objects.get(pk="a")
    assert message.ai_attempts == min(failed_requests + 1, 3)
    assert message.ai_classified == (failed_requests < 3)
    assert provider.system_one.call_count == min(failed_requests + 1, 3)
    assert AIRequest.objects.filter(cost_usd=None).count() == min(failed_requests, 3)
    assert all("SECRET" not in str(row) for row in AIRequest.objects.values())


def test_interrupted_ai_attempt_is_counted_before_call_and_retried_after_restart(
    client, tmp_path, api, monkeypatch
):
    from test_inbox import enable_ai, measured_ai, seed_account

    from classifications import labeling, usage
    from classifications.models import AIRequest

    seed_account(client, tmp_path)
    enable_ai(client)
    gmail.get_messages_for_list(api, ["a"])
    _, provider = measured_ai(monkeypatch)
    provider.system_one.side_effect = KeyboardInterrupt
    # Simulate a process stopping before request accounting can finish.
    with monkeypatch.context() as interrupted:
        interrupted.setattr(usage, "finish", MagicMock())
        with pytest.raises(KeyboardInterrupt):
            labeling.process()
    request_id = AIRequest.objects.get(status="running").pk
    assert Message.objects.get(pk="a").ai_attempts == 1
    assert AIRequest.objects.get(pk=request_id).message_count == 1
    usage.recover()
    measured_ai(monkeypatch)
    labeling.process()
    assert Message.objects.get(pk="a").ai_attempts == 2
    assert Message.objects.get(pk="a").ai_classified
    previous = AIRequest.objects.get(pk=request_id)
    assert previous.status == "interrupted" and previous.cost_usd is None


def test_actual_pipeline_recovers_failed_classification_and_stops_at_retry_limit(
    client, tmp_path, api, monkeypatch, settings, caplog
):
    from time import monotonic, sleep

    from test_inbox import enable_ai, seed_account

    from classifications.models import AIRequest

    seed_account(client, tmp_path)
    enable_ai(client)
    save_or_create_message(mail("a"))

    _, provider = measured_ai(monkeypatch)
    provider.system_one.side_effect = TimeoutError("SECRET_PROVIDER_DIAGNOSTICS")
    settings.SYNC_INTERVAL = 0.02
    with pipeline.run():
        deadline = monotonic() + 5
        while (
            AIRequest.objects.filter(status="failed").count() < 3
            and monotonic() < deadline
        ):
            sleep(0.01)
        assert Message.objects.get(pk="a").ai_attempts == 3
        assert AIRequest.objects.count() == 3
    # Restarting the worker must not reset exhausted attempts.
    with pipeline.run():
        sleep(0.1)
        assert AIRequest.objects.count() == 3
    assert "SECRET_PROVIDER_DIAGNOSTICS" not in caplog.text


@scenario("pipeline.feature", "Keep message ingestion responsive during classification")
def test_direct_ingestion_continues_while_periodic_ai_finishes_its_batch(
    client, tmp_path, api, monkeypatch, settings
):
    pass


@given(
    "the first periodic AI batch pauses while more mail arrives",
    target_fixture="paused_classification_batch",
)
def paused_classification_batch(client, tmp_path, monkeypatch, settings):
    seed_account(client, tmp_path)
    enable_ai(client)
    save_or_create_message(mail("a"))
    entered, release = Event(), Event()
    calls = []
    _, provider = measured_ai(monkeypatch)

    def classify(**kwargs):
        calls.append([kwargs["state"]["email"]["subject"].removeprefix("Subject ")])
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
        return jev_response(**kwargs)

    provider.system_one.side_effect = classify
    monkeypatch.setattr(pipeline, "synchronize", lambda: None)
    settings.SYNC_INTERVAL = 0.01
    return calls, entered, release


@then("new mail is saved immediately and classified after the current batch finishes")
def verify_independent_ingestion(paused_classification_batch):
    calls, entered, release = paused_classification_batch
    with pipeline.run():
        try:
            assert entered.wait(3)
            apply_message_update(mail("b"))
            assert Message.objects.filter(pk="b").exists()
            assert calls == [["a"]]
            release.set()
            deadline = monotonic() + 3
            while (
                Message.objects.filter(ai_classified=True).count() < 2
                and monotonic() < deadline
            ):
                sleep(0.01)
            assert calls == [["a"], ["b"]]
            assert Message.objects.filter(ai_classified=True).count() == 2
        finally:
            release.set()


def test_sync_resumes_after_quota_cooldown(client, tmp_path, api, monkeypatch):
    from threading import Event

    from test_inbox import seed_account

    seed_account(client, tmp_path)
    save_or_create_message(mail("a"))
    Account.objects.update(history_id="100")
    error = http_error(429)
    error.resp["retry-after"] = "300"
    api.history.return_value.list.return_value.execute.side_effect = error
    stopped = Event()
    clock = iter([1000, 1000, 1299, 1300])
    monkeypatch.setattr(pipeline, "monotonic", lambda: next(clock))
    waits = []

    def tick(timeout):
        waits.append(timeout)
        if len(waits) == 3:
            stopped.set()

    monkeypatch.setattr(pipeline.sync_requested, "wait", tick)
    # Gmail is tried again at cooldown expiry.
    api.history.return_value.list.return_value.execute.side_effect = [
        error,
        {"historyId": "200"},
    ]
    pipeline._sync(stopped)
    assert api.history.return_value.list.call_count == 2
    assert Account.objects.get().history_id == "200"


def test_browser_polls_only_at_the_sync_interval_and_preserves_last_success():
    import subprocess

    script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const scheduled = [], requests = [];
const label = {textContent: 'Not yet'};
const classifications = {textContent: ''};
const status = {dataset: {interval: '60000'}, querySelector: selector => ({'[data-last-synced]': label, '[data-classifications-due]': classifications})[selector]};
let fail = false;
const context = {
  document: {
    addEventListener: (event, callback) => { if (event === 'DOMContentLoaded') callback(); },
    querySelectorAll: () => [], getElementById: () => null,
    querySelector: selector => selector === '[data-sync-status]' ? status : null,
  },
  window: {addEventListener: () => {}},
  location: {hash: '', pathname: '/', search: ''},
  setTimeout: (callback, interval) => scheduled.push({callback, interval}),
  AbortSignal: {timeout: () => null},
  fetch: async (url) => {
    requests.push(url);
    if (fail) throw new Error('offline');
    return {ok: true, json: async () => ({synced_at: 1800000000000, classifications_due: 7})};
  },
};
(async () => {
  vm.runInNewContext(fs.readFileSync('static/app.js', 'utf8'), context);
  assert.equal(requests.length, 0);
  assert.equal(scheduled.length, 1);
  assert.equal(scheduled[0].interval, 60000);
  await scheduled.shift().callback();
  assert.deepEqual(requests, ['/api/sync']);
  assert.match(label.textContent, /IST$/);
  assert.equal(classifications.textContent, 7);
  const successful = label.textContent;
  fail = true;
  await scheduled.shift().callback();
  assert.equal(label.textContent, successful);
  assert.equal(scheduled.length, 1);
  assert.equal(scheduled[0].interval, 60000);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=10
    )


def test_sync_requests_during_a_download_schedule_one_followup_without_blocking_writes(
    monkeypatch, settings
):
    from threading import Event

    entered, release, repeated = Event(), Event(), Event()
    calls = []

    def synchronize():
        calls.append(None)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
        else:
            repeated.set()

    monkeypatch.setattr(pipeline, "synchronize", synchronize)
    settings.SYNC_INTERVAL = 60
    with pipeline.run():
        try:
            assert entered.wait(2)
            apply_message_update(mail("a"))
            for _ in range(10):
                pipeline.sync_requested.set()
            release.set()
            assert repeated.wait(2)
            assert len(calls) == 2
        finally:
            release.set()


def test_status_reports_last_sync_and_eligible_stored_mail_without_gmail(client, api):
    Account.objects.create(email="me@example.com", synced_at=1_800_000_000_000)
    for message_id in ["eligible", "classified", "exhausted", "missing", "archived"]:
        save_or_create_message(mail(message_id))
    Message.objects.filter(pk="classified").update(ai_classified=True)
    Message.objects.filter(pk="exhausted").update(ai_attempts=3)
    Message.objects.filter(pk="missing").update(body=None)
    Message.objects.filter(pk="archived").update(labels=[])
    response = client.get("/api/sync")
    assert response.status_code == 200
    assert response.json() == {
        "synced_at": 1_800_000_000_000,
        "classifications_due": 1,
    }
    assert api.mock_calls == []


@pytest.mark.parametrize("source", ["history", "browse"])
@scenario(
    "pipeline.feature", "Resume ingestion after a failed save without skipping history"
)
def test_persistence_failure_propagates_without_advancing_history(
    api, monkeypatch, source
):
    pass


@given("history and browsing expose the same new message")
def mail_available_for_recovery(api):
    Account.objects.create(email="me@example.com", history_id="100")
    api.history.return_value.list.return_value.execute.return_value = {
        "historyId": "200",
        "history": [{"messagesAdded": [{"message": {"id": "a"}}]}],
    }


@when("saving the downloaded message fails")
def fail_message_save(api, monkeypatch, source):
    with monkeypatch.context() as patch:
        patch.setattr(
            utils,
            "save_or_create_message",
            MagicMock(side_effect=ValueError("save failed")),
        )
        with pytest.raises(ValueError, match="save failed"):
            if source == "history":
                gmail.sync(api)
            else:
                gmail.get_messages_for_list(api, ["a"])


@then("the failed save leaves the cursor and sync timestamp unchanged")
def verify_failed_save_state():
    account = Account.objects.get()
    assert account.history_id == "100" and account.synced_at is None


@when("the same history or browsing download is retried")
def retry_message_download(api, source):
    if source == "history":
        gmail.sync(api)
    else:
        gmail.get_messages_for_list(api, ["a"])


@then("the message is saved and only history synchronization advances the cursor")
def verify_recovered_message(source):
    message = Message.objects.get(pk="a")
    assert message.body == "Hello from a human."
    assert Account.objects.get().history_id == ("200" if source == "history" else "100")


@scenario(
    "pipeline.feature",
    "Classify available stored inbox mail in successive bounded batches",
)
def test_periodic_ai_skips_empty_preparation_then_drains_bounded_batches(
    client, tmp_path, api, monkeypatch, settings
):
    pass


@given(
    "an idle periodic classifier receives more than two batches of stored mail",
    target_fixture="mail_for_periodic_batches",
)
def mail_for_periodic_batches(client, tmp_path, api, monkeypatch, settings):
    seed_account(client, tmp_path)
    enable_ai(client)
    settings.SYNC_INTERVAL = 60
    stopped = Event()
    prepare = MagicMock(wraps=labeling._message_state)
    calls, provider = measured_ai(monkeypatch)
    monkeypatch.setattr(labeling, "_message_state", prepare)
    waits = []

    def tick(interval):
        waits.append(interval)
        if len(waits) == 1:
            prepare.assert_not_called()
            provider.system_one.assert_not_called()
            for index in range(205):
                api.mailbox[str(index)] = mail(str(index))
                apply_message_update(api.mailbox[str(index)])
        else:
            assert len(calls) == 205
            assert Message.objects.filter(ai_classified=True).count() == 205
            assert calls[0]["questions"] is calls[99]["questions"]
            assert calls[100]["questions"] is not calls[99]["questions"]
            if len(waits) == 3:
                stopped.set()

    monkeypatch.setattr(stopped, "wait", tick)
    return prepare, stopped, waits


@when("the periodic classifier runs through idle and populated passes")
def run_periodic_classifier(mail_for_periodic_batches):
    _prepare, stopped, _waits = mail_for_periodic_batches
    pipeline._classify(stopped)


@then("empty passes skip preparation and populated passes drain all bounded batches")
def verify_bounded_periodic_batches(mail_for_periodic_batches):
    prepare, _stopped, waits = mail_for_periodic_batches
    assert waits == [60, 60, 60]
    assert prepare.call_count == 205


@pytest.mark.parametrize("failures", [1, 2])
def test_transient_subrequest_recovers_without_repeating_successful_reads(
    api, monkeypatch, failures
):
    monkeypatch.setattr(gmail.time, "sleep", lambda _: None)
    get = api.messages.return_value.get.side_effect
    attempts = []

    def download(**kwargs):
        request = get(**kwargs)
        if kwargs["id"] == "b":
            attempts.append(None)
            if len(attempts) <= failures:
                request.execute.side_effect = http_error(503)
        return request

    api.messages.return_value.get.side_effect = download
    gmail.update_messages(api, ["a", "b"])
    assert api.batch_sizes == [2] + [1] * failures
    assert set(Message.objects.values_list("id", flat=True)) == {"a", "b"}


def test_periodic_ai_retries_saved_label_writes_without_unclassified_mail(
    client, tmp_path, monkeypatch, settings
):
    from time import monotonic, sleep

    from test_inbox import enable_ai, seed_account

    from classifications.models import LabelDecision

    seed_account(client, tmp_path)
    enable_ai(client)
    save_or_create_message(mail("a"))
    Message.objects.update(ai_classified=True)
    decision = LabelDecision.objects.create(
        message_id="a",
        label_id="Label_humans",
        source="ai",
        reason="Saved",
        applied=False,
    )
    _, provider = measured_ai(monkeypatch)
    classify = provider.system_one
    settings.SYNC_INTERVAL = 0.01
    assert not Message.objects.classifiable().exists()
    with pipeline.run():
        deadline = monotonic() + 3
        while (
            not LabelDecision.objects.get(pk=decision.pk).applied
            and monotonic() < deadline
        ):
            sleep(0.01)
        assert LabelDecision.objects.get(pk=decision.pk).applied
    classify.assert_not_called()


@pytest.mark.parametrize(
    "reason,minimum",
    [
        ("rateLimitExceeded", 60),
        ("RATE_LIMIT_EXCEEDED", 60),
        ("dailyLimitExceeded", 3600),
        ("accessNotConfigured", None),
        ("insufficientPermissions", None),
        ("domainPolicy", None),
    ],
)
def test_only_quota_denials_have_automatic_cooldowns(reason, minimum):
    from mailsome.errors import gmail_retry_delay

    error = HttpError(
        httplib2.Response({"status": "403"}),
        json.dumps(
            {
                "error": {
                    "message": "secret",
                    "errors": [{"reason": reason}],
                }
            }
        ).encode(),
    )
    assert gmail_retry_delay(error) == minimum


def test_quota_retry_after_accepts_http_date_and_ignores_malformed_hints(monkeypatch):
    from email.utils import formatdate

    from mailsome.errors import gmail_retry_delay

    monkeypatch.setattr("time.time", lambda: 1000)
    error = http_error(429)
    error.resp["retry-after"] = formatdate(1300, usegmt=True)
    assert gmail_retry_delay(error) == 301
    error.resp["retry-after"] = "not a date"
    assert gmail_retry_delay(error) == 60


@pytest.mark.parametrize("remote_succeeded", [False, True])
def test_sender_bulk_failure_recomputes_after_refresh(
    client, tmp_path, api, remote_succeeded
):
    synced(client, tmp_path, api)
    sender_rule(client)
    original = api.messages.return_value.batchModify.side_effect

    def interrupted(**kwargs):
        if remote_succeeded:
            original(**kwargs).execute()
        return MagicMock(execute=MagicMock(side_effect=http_error(503)))

    api.messages.return_value.batchModify.side_effect = interrupted
    with pytest.raises(HttpError):
        sender_filters.apply_sender_rules()
    assert "Label_humans" not in Message.objects.get(pk="a").labels
    assert not LabelDecision.objects.exists()
    api.messages.return_value.batchModify.side_effect = original
    # History reports successful writes even if the response/cache commit was lost.
    api.history.return_value.list.return_value.execute.return_value["historyId"] = "120"
    gmail.sync(api)
    api.reset_mock()
    sender_filters.apply_sender_rules()
    assert ("Label_humans" in Message.objects.get(pk="a").labels) is remote_succeeded
    assert "Label_humans" in api.mailbox["a"]["labelIds"]
    assert api.messages.return_value.batchModify.call_count == (
        0 if remote_succeeded else 1
    )


@scenario(
    "pipeline.feature", "Apply sender rules to stored inbox mail independently of AI"
)
def test_sender_rules_match_cached_inbox_mail_and_reapply_missing_labels(
    client, tmp_path, api
):
    pass


@given(
    "an exact sender rule covers older stored mail with exhausted AI attempts",
    target_fixture="stored_sender_rule_matches",
)
def stored_sender_rule_matches(client, tmp_path, api):
    seed_account(client, tmp_path)
    gmail.sync(api)
    api.mailbox = {}
    for identifier, sender, labels in [
        ("match", "Human <human@example.com>", ["INBOX", "UNREAD"]),
        ("alias", "human+news@example.com", ["INBOX"]),
        ("display", '"human@example.com" <other@example.com>', ["INBOX"]),
        ("archive", "human@example.com", []),
        ("spam", "human@example.com", ["INBOX", "SPAM"]),
        ("trash", "human@example.com", ["INBOX", "TRASH"]),
        ("draft", "human@example.com", ["INBOX", "DRAFT"]),
        ("uncached", "human@example.com", ["INBOX"]),
    ]:
        raw = mail(identifier, days=365, labels=labels)
        raw["payload"]["headers"][0]["value"] = sender
        api.mailbox[identifier] = raw
        if identifier != "uncached":
            apply_message_update(raw)
    tab = Tab.objects.create(
        name="Humans", label_id="Label_humans", people=["human@example.com"]
    )
    Message.objects.update(ai_attempts=3)
    api.history.return_value.list.return_value.execute.side_effect = None
    api.history.return_value.list.return_value.execute.return_value = {
        "historyId": "110"
    }
    api.reset_mock()
    return tab


@when("the background sync applies sender rules")
def apply_stored_sender_rules():
    pipeline.synchronize()


@then(
    "only exact stored inbox matches receive labels without body downloads or filter reconciliation"
)
def verify_exact_sender_labels(api):
    api.messages.return_value.batchModify.assert_called_once_with(
        userId="me",
        body={"ids": ["match"], "addLabelIds": ["Label_humans"]},
    )
    api.messages.return_value.get.assert_not_called()
    api.messages.return_value.list.assert_not_called()
    api.settings.assert_not_called()
    gmail.sync(api)
    api.reset_mock()
    sender_filters.apply_sender_rules()
    api.messages.return_value.batchModify.assert_not_called()


@when("a matching sender label is manually removed and rules run again")
def remove_and_reapply_sender_label(api):
    api.mailbox["match"]["labelIds"].remove("Label_humans")
    gmail.update_messages(api, ["match"], labels_only=True)
    sender_filters.apply_sender_rules()


@then("the missing sender label is reapplied")
def verify_reapplied_sender_label(api):
    api.messages.return_value.batchModify.assert_called_once_with(
        userId="me",
        body={"ids": ["match"], "addLabelIds": ["Label_humans"]},
    )


@when("the sender rule is removed and rules run again")
def remove_sender_rule(api, stored_sender_rule_matches):
    tab = stored_sender_rule_matches
    Tab.objects.filter(pk=tab.pk).update(people=[])
    api.reset_mock()
    sender_filters.apply_sender_rules()


@then("existing sender labels remain untouched")
def verify_preserved_sender_labels(api):
    api.messages.return_value.batchModify.assert_not_called()
    assert set(api.mailbox["match"]["labelIds"]) == {"INBOX", "UNREAD", "Label_humans"}


def test_sender_bulk_writes_chunk_each_label_without_a_message_window(
    client, tmp_path, api
):
    seed_account(client, tmp_path)
    api.mailbox = {f"message-{i}": mail(f"message-{i}", days=365) for i in range(1001)}
    for raw in api.mailbox.values():
        apply_message_update(raw)
    for label in ("Label_humans", "Label_other"):
        Tab.objects.create(name=label, label_id=label, people=["human@example.com"])
    api.reset_mock()
    sender_filters.apply_sender_rules()
    calls = api.messages.return_value.batchModify.call_args_list
    assert [len(call.kwargs["body"]["ids"]) for call in calls] == [1000, 1, 1000, 1]
    for label in ("Label_humans", "Label_other"):
        written = [
            identifier
            for call in calls
            if call.kwargs["body"]["addLabelIds"] == [label]
            for identifier in call.kwargs["body"]["ids"]
        ]
        assert len(written) == len(set(written)) == 1001
        assert set(written) == set(api.mailbox)


def test_ai_bulk_retry_preserves_successful_groups_and_acknowledges_cached_labels(
    client, tmp_path, api, monkeypatch
):
    synced(client, tmp_path, api)
    enable_ai(client)
    Tab.objects.create(name="Other", label_id="Label_other", auto_classify=True)
    Message.objects.update(ai_classified=True, body="Cached body")
    for label_id in ("Label_humans", "Label_other"):
        for message_id in ("a", "during"):
            LabelDecision.objects.create(
                message_id=message_id,
                label_id=label_id,
                source="ai",
                reason="Saved match",
            )
    # A prior confirmed write or history event already satisfied this decision.
    Message.objects.filter(pk="b").update(
        labels=["INBOX", "Label_humans", "Label_other"]
    )
    for label_id in ("Label_humans", "Label_other"):
        LabelDecision.objects.create(
            message_id="b", label_id=label_id, source="ai", reason="Already present"
        )
    _, provider = measured_ai(monkeypatch)
    classifier = provider.system_one
    original = api.messages.return_value.batchModify.side_effect

    def write(**kwargs):
        if api.messages.return_value.batchModify.call_count == 2:
            raise http_error(
                503
            )  # Fail the second group after the first was committed.
        return original(**kwargs)

    api.messages.return_value.batchModify.side_effect = write
    with pytest.raises(HttpError):
        labeling.process()
    calls = api.messages.return_value.batchModify.call_args_list
    successful = calls[0].kwargs["body"]["addLabelIds"][0]
    failed = calls[1].kwargs["body"]["addLabelIds"][0]
    assert LabelDecision.objects.filter(label_id=successful, applied=True).count() == 3
    assert not LabelDecision.objects.filter(label_id=failed, applied=True).exists()
    assert set(Message.objects.get(pk="a").labels) == {"INBOX", "UNREAD"}

    api.messages.return_value.batchModify.side_effect = original
    api.reset_mock()
    assert not labeling.process()
    api.messages.return_value.batchModify.assert_called_once()
    assert api.messages.return_value.batchModify.call_args.kwargs["body"] == {
        "ids": ["a", "during"],
        "addLabelIds": [failed],
    }
    assert LabelDecision.objects.filter(applied=True).count() == 6
    assert set(Message.objects.get(pk="a").labels) == {"INBOX", "UNREAD"}
    classifier.assert_not_called()
    api.messages.return_value.get.assert_not_called()


@scenario(
    "pipeline.feature",
    "Save classifications before Gmail writes and retry labels without another paid request",
)
def test_classification_saves_before_writes_and_retries_without_reclassifying(
    client, tmp_path, api, monkeypatch
):
    pass


@given(
    "classification succeeds but applying its Gmail labels fails",
    target_fixture="failing_classification_label_write",
)
def failing_classification_label_write(client, tmp_path, api, monkeypatch):
    synced(client, tmp_path, api)
    enable_ai(client)
    calls, _ = measured_ai(monkeypatch)
    original = api.messages.return_value.batchModify.side_effect

    def failed_write(**kwargs):
        assert Message.objects.filter(ai_classified=True).count() == 3
        assert list(LabelDecision.objects.values_list("ai_score", flat=True)) == [
            0.9,
            0.9,
        ]
        raise http_error(503)

    api.messages.return_value.batchModify.side_effect = failed_write
    return calls, original


@when("the classifier attempts to save and apply the decisions")
def attempt_classification_label_write():
    with pytest.raises(HttpError):
        labeling.process()


@then("each paid email response is saved but no failed Gmail write is acknowledged")
def verify_saved_unapplied_decisions(failing_classification_label_write):
    calls, _original = failing_classification_label_write
    assert len(calls) == 3
    assert not LabelDecision.objects.filter(applied=True).exists()


@when("Gmail recovers and the classifier runs again")
def retry_saved_label_writes(api, failing_classification_label_write):
    _calls, original = failing_classification_label_write
    api.messages.return_value.batchModify.side_effect = original
    api.reset_mock()
    labeling.process()


@then("saved decisions are applied without downloading the messages again")
def verify_applied_saved_decisions(api):
    api.messages.return_value.batchModify.assert_called_once_with(
        userId="me",
        body={"ids": ["a", "during"], "addLabelIds": ["Label_humans"]},
    )
    assert LabelDecision.objects.filter(applied=True).count() == 2
    assert "Label_humans" not in Message.objects.get(pk="a").labels
    api.messages.return_value.get.assert_not_called()


@when("a label is manually removed and its description is edited")
def edit_completed_mail_labels(api):
    api.mailbox["a"]["labelIds"].remove("Label_humans")
    gmail.update_messages(api, ["a"], labels_only=True)
    Tab.objects.update(description="Updated description")
    api.reset_mock()
    labeling.process()


@then("completed mail is not reclassified and the manual removal is preserved")
def verify_no_reclassification(api, failing_classification_label_write):
    calls, _original = failing_classification_label_write
    assert len(calls) == 3
    api.messages.return_value.batchModify.assert_not_called()


@pytest.mark.parametrize(
    "state,eligible",
    [
        ({}, True),
        ({"body": ""}, True),
        ({"body": None}, False),
        ({"ai_classified": True}, False),
        ({"ai_attempts": 2}, True),
        ({"ai_attempts": 3}, False),
        ({"ai_attempts": 4}, False),
        ({"labels": ["UNREAD"]}, False),
        ({"labels": ["INBOX", "DRAFT"]}, False),
        ({"labels": ["INBOX", "SPAM"]}, False),
        ({"labels": ["INBOX", "TRASH"]}, False),
    ],
    ids=[
        "old-inbox",
        "empty-body",
        "missing-body",
        "completed",
        "last-attempt",
        "exhausted",
        "previously-exhausted",
        "archived",
        "draft",
        "spam",
        "trash",
    ],
)
def test_ai_classifies_only_eligible_stored_mail(
    client, tmp_path, api, monkeypatch, state, eligible
):
    seed_account(client, tmp_path)
    enable_ai(client)
    apply_message_update(mail("a", days=365))
    if state:
        Message.objects.filter(pk="a").update(**state)
    if "labels" in state:
        LabelDecision.objects.create(
            message_id="a", label_id="Label_humans", source="ai", reason="Pending"
        )
    _, provider = measured_ai(monkeypatch)
    classify = provider.system_one
    api.reset_mock()
    labeling.process()
    assert classify.call_count == int(eligible)
    if eligible:
        assert Message.objects.get(pk="a").ai_classified
    else:
        api.messages.return_value.batchModify.assert_not_called()
        assert not LabelDecision.objects.filter(applied=True).exists()
    api.messages.return_value.get.assert_not_called()
    api.messages.return_value.list.assert_not_called()


@pytest.mark.parametrize(
    "change", ["description", "disable-label", "remove-label", "disable-ai"]
)
def test_ai_finishes_snapshot_pass_but_checks_current_policy_for_label_writes(
    client, tmp_path, api, monkeypatch, change
):
    synced(client, tmp_path, api)
    enable_ai(client)
    _, provider = measured_ai(monkeypatch)

    def respond(**kwargs):
        if change == "description":
            Tab.objects.update(description="Updated")
        elif change == "disable-label":
            Tab.objects.update(auto_classify=False)
        elif change == "remove-label":
            Tab.objects.all().delete()
        else:
            monkeypatch.setattr(
                classification_utils, "settings", lambda: {"enabled": False}
            )
        return jev_response(**kwargs)

    provider.system_one.side_effect = respond
    labeling.process()
    assert Message.objects.filter(ai_classified=True).count() == 3
    assert provider.system_one.call_count == 3
    assert LabelDecision.objects.count() == 2
    assert LabelDecision.objects.filter(applied=True).count() == (
        2 if change == "description" else 0
    )
    assert api.messages.return_value.batchModify.call_count == int(
        change == "description"
    )


@pytest.mark.parametrize("sender_label", ["Label_humans", "Label_paper"])
def test_sender_labeled_mail_still_receives_ai_decisions(
    client, tmp_path, api, monkeypatch, sender_label
):
    synced(client, tmp_path, api)
    enable_ai(client)
    if sender_label == "Label_humans":
        Tab.objects.update(people=["human@example.com"])
    else:
        Tab.objects.create(
            name="Paper", label_id=sender_label, people=["human@example.com"]
        )
    decision = LabelDecision.objects.create(
        message_id="a", label_id="Label_humans", source="ai", ai_score=0.9
    )
    sender_filters.apply_sender_rules()
    decision.refresh_from_db()
    assert not decision.applied
    gmail.sync(api)
    api.reset_mock()
    measured_ai(monkeypatch)
    labeling.process()
    assert Message.objects.filter(ai_classified=True).count() == 3
    assert set(
        LabelDecision.objects.filter(applied=True).values_list("message_id", flat=True)
    ) == {"a", "during"}

    assert api.messages.return_value.batchModify.call_count == int(
        sender_label != "Label_humans"
    )


@scenario(
    "pipeline.feature", "Reset selected AI labels before reclassifying the cached inbox"
)
def test_reclassification_resets_confirmed_scope_then_classifies_again(
    client, tmp_path, api, monkeypatch
):
    pass


@given(
    "completed mail has selected AI labels, unrelated labels, and stale label snapshots",
    target_fixture="mail_selected_for_reset",
)
def mail_selected_for_reset(client, tmp_path, api, monkeypatch):
    seed_account(client, tmp_path)
    enable_ai(client)
    gmail.sync(api)
    cursor = Account.objects.get().history_id
    Tab.objects.filter(label_id="Label_humans").update(people=["human@example.com"])
    Tab.objects.create(name="Other", label_id="Label_other", auto_classify=True)
    Tab.objects.create(name="Keep", label_id="Label_keep")
    api.mailbox = {
        "a": mail(
            "a", labels=["INBOX", "UNREAD", "Label_humans", "Label_other", "Label_keep"]
        ),
        "b": mail("b"),
        "archived": mail("archived", labels=["Label_other"]),
    }
    api.mailbox["b"]["payload"]["headers"][0]["value"] = "other@example.com"
    for raw in api.mailbox.values():
        apply_message_update(raw)
    Message.objects.update(ai_classified=True, ai_attempts=3, importance=0.3)
    # Stale snapshots in both directions: an unseen assignment and a manual removal.
    Message.objects.filter(pk="a").update(
        labels=["INBOX", "UNREAD", "Label_humans", "Label_keep"]
    )
    Message.objects.filter(pk="b").update(labels=["INBOX", "UNREAD", "Label_humans"])
    for message_id, label_id, source in [
        ("a", "Label_humans", "ai"),
        ("a", "Label_other", "sender"),
        ("a", "Label_keep", "ai"),
        ("b", "Label_humans", "ai"),
    ]:
        LabelDecision.objects.create(
            message_id=message_id,
            label_id=label_id,
            source=source,
            reason="Previous",
            applied=True,
        )
    _, provider = measured_ai(monkeypatch)

    def respond(**kwargs):
        response = jev_response(**kwargs)
        for tab in Tab.objects.filter(auto_classify=True):
            key = f"tab_{tab.pk}"
            response.answers[key] = response.answers[key].model_copy(
                update={
                    "noul": float(
                        (kwargs["state"]["email"]["subject"], tab.name)
                        in {("Subject a", "Other"), ("Subject b", "Humans")}
                    )
                }
            )
        return response

    provider.system_one.side_effect = respond
    classifier = provider.system_one
    api.reset_mock()
    return cursor, classifier


@when("I confirm reclassification of the selected cached inbox mail")
def confirm_reclassification(client):
    assert (
        client.post("/settings/reclassify/", data={"confirm": "on"}).status_code == 303
    )


@then("selected labels are reset and the messages await the regular classifier")
def verify_reset_eligibility(api, mail_selected_for_reset):
    cursor, classifier = mail_selected_for_reset
    assert {
        call.kwargs["body"]["removeLabelIds"][0]: call.kwargs["body"]["ids"]
        for call in api.messages.return_value.batchModify.call_args_list
    } == {"Label_humans": ["b"], "Label_other": ["a", "b"]}
    assert set(Message.objects.get(pk="a").labels) == {
        "INBOX",
        "UNREAD",
        "Label_humans",
        "Label_keep",
    }
    assert (
        Message.objects.filter(
            pk__in=["a", "b"], ai_classified=False, ai_attempts=0, importance=None
        ).count()
        == 2
    )
    assert list(LabelDecision.objects.values_list("label_id", flat=True)) == [
        "Label_keep"
    ]
    archived = Message.objects.get(pk="archived")
    assert archived.ai_classified and archived.importance == 0.3
    assert archived.labels == ["Label_other"]
    assert Account.objects.get().history_id == cursor
    api.history.return_value.list.assert_not_called()
    classifier.assert_not_called()


@when(
    "the periodic classifier runs after other completed mail arrives",
    target_fixture="classify_reset_messages",
)
def classify_reset_messages(api, mail_selected_for_reset):
    _cursor, classifier = mail_selected_for_reset
    Tab.objects.filter(label_id="Label_keep").update(auto_classify=True)
    api.mailbox["late"] = mail("late", labels=["INBOX", "Label_other"])
    apply_message_update(api.mailbox["late"])
    Message.objects.filter(pk="late").update(ai_classified=True)
    labeling.process()
    return classifier


@then(
    "only the confirmed mail is reclassified after protected and unrelated assignments are preserved"
)
def verify_reclassification_scope(api, classify_reset_messages):
    classifier = classify_reset_messages
    assert classifier.call_count == 2
    assert {
        request.kwargs["state"]["email"]["subject"]
        for request in classifier.call_args_list
    } == {"Subject a", "Subject b"}
    assert Message.objects.filter(pk__in=["a", "b"], ai_classified=True).count() == 2
    assert LabelDecision.objects.get(message_id="a", label_id="Label_other").applied
    assert LabelDecision.objects.get(message_id="b", label_id="Label_humans").applied
    assert "Label_humans" in api.mailbox["b"]["labelIds"]
    for key in ("archived", "late"):
        message = Message.objects.get(pk=key)
        assert message.ai_classified and message.labels == api.mailbox[key]["labelIds"]
        assert "Label_other" in message.labels


def test_label_removals_are_chunked_without_losing_ids():
    provider = MagicMock()
    messages = [Message(id=str(index)) for index in range(1001)]
    gmail.remove_label_from_messages(provider, messages, "Label_humans")
    calls = provider.users.return_value.messages.return_value.batchModify.call_args_list
    assert [call.kwargs for call in calls] == [
        {
            "userId": "me",
            "body": {
                "ids": [str(index) for index in range(1000)],
                "removeLabelIds": ["Label_humans"],
            },
        },
        {"userId": "me", "body": {"ids": ["1000"], "removeLabelIds": ["Label_humans"]}},
    ]


@pytest.mark.parametrize("failure_stage", ["remove", "refresh", "restored"])
def test_failed_reset_preserves_decisions_until_confirmation_is_retried(
    client, tmp_path, api, monkeypatch, failure_stage
):
    seed_account(client, tmp_path)
    enable_ai(client)
    Tab.objects.create(
        name="Other", label_id="Label_other", auto_classify=True, position=2
    )
    api.mailbox["a"]["labelIds"] += ["Label_humans", "Label_other"]
    apply_message_update(api.mailbox["a"])
    Message.objects.update(ai_classified=True, ai_attempts=3, importance=0.3)
    selection = {"message_ids": ["a"], "label_ids": ["Label_humans", "Label_other"]}
    for label in selection["label_ids"]:
        LabelDecision.objects.create(
            message_id="a", label_id=label, source="ai", reason="Saved", applied=True
        )
    _, provider = measured_ai(monkeypatch)
    classifier = provider.system_one
    original = api.messages.return_value.batchModify.side_effect
    get = api.messages.return_value.get.side_effect

    def remove(**kwargs):
        if failure_stage == "remove" and kwargs["body"]["removeLabelIds"] == [
            "Label_other"
        ]:
            raise http_error(503)
        result = original(**kwargs)
        if (
            failure_stage == "restored"
            and "Label_humans" not in api.mailbox["a"]["labelIds"]
        ):
            api.mailbox["a"]["labelIds"].append("Label_humans")
        return result

    api.messages.return_value.batchModify.side_effect = remove
    if failure_stage == "refresh":

        def fail_refresh(**kwargs):
            request = get(**kwargs)
            request.execute.side_effect = http_error(503)
            return request

        api.messages.return_value.get.side_effect = fail_refresh
        monkeypatch.setattr(gmail.time, "sleep", lambda _: None)
    response = client.post("/settings/reclassify/", data={"confirm": "on"})
    assert response.status_code == (409 if failure_stage == "restored" else 502)
    labeling.process()
    message = Message.objects.get(pk="a")
    assert message.ai_classified and message.ai_attempts == 3
    assert LabelDecision.objects.filter(applied=True).count() == 2
    classifier.assert_not_called()

    api.messages.return_value.batchModify.side_effect = original
    api.messages.return_value.get.side_effect = get
    assert (
        client.post("/settings/reclassify/", data={"confirm": "on"}).status_code == 303
    )
    assert not LabelDecision.objects.exists()
    message.refresh_from_db()
    assert not message.ai_classified and message.ai_attempts == 0
    assert set(message.labels) == {"INBOX", "UNREAD"}
    classifier.assert_not_called()


@pytest.mark.parametrize("outcome", ["completed", "timeout", "missing_answer"])
@scenario(
    "pipeline.feature",
    "Show AI request outcomes and known or unknown costs without private content",
)
def test_ai_usage_records_outcomes_without_private_content(
    client, tmp_path, api, monkeypatch, outcome
):
    pass


@given(
    "a paid classification completes, times out, or returns an incomplete answer",
    target_fixture="paid_request_outcome",
)
def paid_request_outcome(client, tmp_path, api, monkeypatch, outcome):
    seed_account(client, tmp_path)
    enable_ai(client)
    apply_message_update(api.mailbox["a"])
    _, provider = measured_ai(monkeypatch)

    def respond(**kwargs):
        running = AIRequest.objects.get()
        assert running.status == "running" and running.cost_usd is None
        assert Message.objects.get(pk="a").ai_attempts == 1
        if outcome == "timeout":
            raise TimeoutError("secret-test-key PRIVATE_EMAIL")
        response = jev_response(**kwargs)
        if outcome == "missing_answer":
            response.answers.pop("importance")
        return response

    provider.system_one.side_effect = respond
    return provider


@when("the classification request is attempted")
def attempt_paid_request(outcome):
    if outcome == "completed":
        labeling.process()
    else:
        with pytest.raises(TimeoutError if outcome == "timeout" else KeyError):
            labeling.process()
        assert not Message.objects.filter(ai_classified=True).exists()
        assert not LabelDecision.objects.exists()


@then(
    "Settings shows the request outcome and cost without exposing mail or credentials"
)
def verify_usage_display(client, monkeypatch, outcome, paid_request_outcome):
    provider = paid_request_outcome
    provider.system_one.assert_called_once()
    page = client.get("/settings/")
    data = page.context["usage"]
    record = data["requests"][0]
    assert data["summary"]["request_count"] == 1
    assert record["status"] == ("completed" if outcome == "completed" else "failed")
    cost = None if outcome == "timeout" else 0.000042
    assert record["cost_usd"] == (pytest.approx(cost) if cost is not None else None)
    assert data["summary"]["unknown_cost_count"] == int(cost is None)
    assert data["summary"]["total_usd"] == pytest.approx(cost or 0)
    for secret in (
        "secret-test-key",
        "PRIVATE_EMAIL",
        "human@example.com",
        "Hello from a human",
    ):
        assert secret not in page.text and secret not in str(record)
    if outcome == "completed":
        before = list(AIRequest.objects.values())
        Message.objects.all().delete()
        monkeypatch.setattr(usage, "PRICING", {})
        usage.recover()
        assert list(AIRequest.objects.values()) == before
        assert usage.history()["summary"]["total_usd"] == pytest.approx(cost)


@pytest.mark.parametrize(
    "model,incoming,expected",
    [
        ("jev-1.13.0", 1000, 0.000042),
        ("unknown-model", 1000, None),
        ("jev-1.13.0", None, None),
    ],
)
def test_usage_estimates_known_costs_without_treating_unknown_as_free(
    model, incoming, expected
):
    from typesafe_sdk import SystemOneResponse

    response = SystemOneResponse.model_validate(
        {
            "model": model,
            "usage": {"input_tokens": incoming, "output_tokens": 10},
        }
    )
    save_or_create_message(mail("a"))
    request_id = usage.start({"model": model}, message_count=1)
    assert Message.objects.get(pk="a").ai_attempts == 0
    usage.finish(request_id, "completed", response, None)
    cost = AIRequest.objects.get(pk=request_id).cost_usd
    assert cost == (pytest.approx(expected) if expected is not None else None)


@scenario(
    "pipeline.feature",
    "Upgrade workflow storage without losing mail or classification history",
)
def test_forward_upgrades_preserve_mail_decisions_and_costs(
    upgrade_database,
):
    pass


@given(
    "an older database contains mail, decisions, and usage",
    target_fixture="previous_workflow_database",
)
def previous_workflow_database():
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    initial = [
        ("inbox", "0001_initial"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]
    executor.migrate(initial)
    old = executor.loader.project_state(initial).apps
    old.get_model("inbox", "Account").objects.create(
        email="me@example.com", history_id="123", synced_at=1000
    )
    old.get_model("inbox", "Tab").objects.create(
        name="Humans",
        label_id="Label_humans",
        people=["human@example.com"],
        description="People",
        auto_classify=True,
        position=3,
    )
    old.get_model("inbox", "Sender").objects.create(
        email="human@example.com", note="Keep this note"
    )
    for identifier in ("matched", "no-match", "unchecked"):
        old.get_model("inbox", "Message").objects.create(
            id=identifier,
            received_at=1000,
            labels=["INBOX", "Label_existing"],
            body="Saved body",
        )
    for identifier in ("matched", "no-match"):
        old.get_model("inbox", "Classification").objects.create(
            message_id=identifier, policy="old"
        )
    for source in ("ai", "sender"):
        old.get_model("inbox", "LabelDecision").objects.create(
            message_id="matched",
            label_id="Label_humans",
            source=source,
            reason="Saved reason",
            applied=source == "sender",
        )
    for status in ("completed", "running"):
        old.get_model("inbox", "AIRequest").objects.create(
            started_at=1000,
            model="test",
            reasoning="medium",
            message_count=1,
            status=status,
            cost_usd=0.25 if status == "completed" else None,
        )
    content_types = {
        name: old.get_model("contenttypes", "ContentType")
        .objects.create(app_label="inbox", model=name)
        .pk
        for name in ("account", "labeldecision", "airequest")
    }

    # Seed body caches once those fields exist, before later completion migrations.
    rich = [
        ("inbox", "0005_message_rich_body"),
        ("classifications", "0002_classification_policy_help_text"),
        ("jobs", "0001_initial"),
        ("accounts", "0001_initial"),
    ]
    executor = MigrationExecutor(connection)
    executor.migrate(rich)
    old = executor.loader.project_state(rich).apps
    old.get_model("inbox", "Message").objects.update(
        recipients={"To": ["me@example.com"]}, rich_body={"html": "Saved HTML"}
    )
    return content_types, latest


@when("the database is upgraded to the current schema")
def upgrade_workflow_database(previous_workflow_database):
    _content_types, latest = previous_workflow_database
    MigrationExecutor(connection).migrate(latest)


@then("mail, preferences, costs, and completion state survive the schema upgrade")
def verify_preserved_workflow_data(previous_workflow_database):
    content_types, _latest = previous_workflow_database
    assert Account.objects.values("email", "history_id", "synced_at").get() == {
        "email": "me@example.com",
        "history_id": "123",
        "synced_at": 1000,
    }
    assert Tab.objects.values(
        "people", "description", "position", "auto_classify"
    ).get() == {
        "people": ["human@example.com"],
        "description": "People",
        "position": 3,
        "auto_classify": True,
    }
    assert Sender.objects.get().note == "Keep this note"
    assert set(
        Message.objects.filter(ai_classified=True).values_list("id", flat=True)
    ) == {"matched", "no-match"}
    assert (
        Message.objects.filter(
            body="Saved body",
            rich_body={"html": "Saved HTML"},
            recipients={"To": ["me@example.com"]},
            labels=["INBOX", "Label_existing"],
            ai_attempts=0,
        ).count()
        == 3
    )
    assert set(LabelDecision.objects.values_list("source", "applied", "reason")) == {
        ("ai", False, "Saved reason"),
        ("sender", True, "Saved reason"),
    }
    assert dict(AIRequest.objects.values_list("status", "cost_usd")) == {
        "completed": 0.25,
        "interrupted": None,
    }
    for name, app in [
        ("account", "accounts"),
        ("labeldecision", "classifications"),
        ("airequest", "classifications"),
    ]:
        assert ContentType.objects.get(pk=content_types[name]).app_label == app


@pytest.mark.parametrize(
    "probability,threshold,matched",
    [(0.74, 0.75, False), (0.75, 0.75, True), (0.8, 0.9, False)],
)
def test_jev_threshold_and_importance_are_saved_before_label_writes(
    client, tmp_path, api, monkeypatch, probability, threshold, matched
):
    from typesafe_sdk import Noul, Score, SystemOneResponse

    seed_account(client, tmp_path)
    enable_ai(client)
    Tab.objects.update(acceptance_threshold=threshold)
    apply_message_update(api.mailbox["a"])
    tab = Tab.objects.get()
    response = SystemOneResponse.model_validate(
        {
            "model": "jev-1.13.0",
            "usage": {"input_tokens": 100, "output_tokens": 10},
            "answers": {
                f"tab_{tab.pk}": {"type": "noul", "noul": probability},
                "importance": {
                    "type": "score",
                    "score": 1.5,
                    "confidence": 0.8,
                    "legend": {0: "Low", 1: "Normal", 2: "High"},
                    "probabilities": {0: 0, 1: 0.5, 2: 0.5},
                },
            },
        }
    )
    sdk = MagicMock()
    sdk.return_value.__enter__.return_value.system_one.return_value = response
    monkeypatch.setattr(labeling, "TypeSafeClient", sdk)
    labeling.process()
    message = Message.objects.get(pk="a")
    assert message.ai_classified and message.ai_attempts == 1
    assert message.importance == 0.75
    assert LabelDecision.objects.exists() == matched
    if matched:
        assert LabelDecision.objects.get().ai_score == probability
        assert LabelDecision.objects.get().applied
    request = sdk.return_value.__enter__.return_value.system_one.call_args.kwargs
    assert isinstance(request["questions"][f"tab_{tab.pk}"], Noul)
    assert isinstance(request["questions"]["importance"], Score)
    assert tab.description in str(request["questions"][f"tab_{tab.pk}"].instructions)
    assert request["state"]["email"]["body"] == message.body
    assert sdk.call_args.kwargs["retry"].max_retries == 0
    assert AIRequest.objects.get().cost_usd == pytest.approx(100 * 0.042 / 1_000_000)


@scenario("pipeline.feature", "Score and reclassify importance without AI-enabled tabs")
def test_importance_only_classification_and_reclassification(
    client, tmp_path, api, monkeypatch
):
    pass


@given(
    "AI is enabled without tab questions or Gmail label access",
    target_fixture="importance_only_mail",
)
def importance_only_mail(client, tmp_path, api, monkeypatch):
    seed_account(client, tmp_path)
    enable_ai(client)
    Tab.objects.update(auto_classify=False)
    apply_message_update(api.mailbox["a"])
    apply_message_update(api.mailbox["archived"])
    monkeypatch.setattr(gmail, "can_label", lambda: False)
    calls, _ = measured_ai(monkeypatch)
    api.reset_mock()
    return calls


@when("the worker scores stored mail and I request reclassification")
def rescore_importance_only_mail(client, importance_only_mail):
    labeling.process()
    message = Message.objects.get(pk="a")
    assert message.ai_classified and message.importance == 0.75
    assert client.post("/settings/reclassify/", {"confirm": "on"}).status_code == 303
    message.refresh_from_db()
    assert (
        not message.ai_classified
        and message.ai_attempts == 0
        and message.importance is None
    )
    labeling.process()


@then("inbox importance is rescored without Gmail writes and archived mail is skipped")
def verify_importance_only_mail(api, importance_only_mail):
    calls = importance_only_mail
    assert len(calls) == 2
    assert all(set(request["questions"]) == {"importance"} for request in calls)
    assert Message.objects.get(pk="a").importance == 0.75
    archived = Message.objects.get(pk="archived")
    assert (
        not archived.ai_classified
        and archived.importance is None
        and archived.ai_attempts == 0
    )
    assert not LabelDecision.objects.exists()
    assert api.mock_calls == []


def test_jev_sdk_sends_independent_questions_and_saves_their_typed_answers(
    client, tmp_path, api, monkeypatch
):
    import httpx2
    from typesafe_sdk import TypeSafeClient

    seed_account(client, tmp_path)
    enable_ai(client)
    first = Tab.objects.get()
    other = Tab.objects.create(
        name="Other",
        label_id="Label_other",
        description="Work emails",
        auto_classify=True,
    )
    apply_message_update(api.mailbox["a"])
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["questions"][f"tab_{first.pk}"]["criteria"] == {
            "true": "The email matches the described category.",
            "false": "The email does not match the described category.",
        }
        assert (
            payload["questions"][f"tab_{other.pk}"]["instructions"]["description"]
            == "Work emails"
        )
        assert (
            payload["questions"]["importance"]["criteria"]
            == classification_utils.settings()["importance_levels"]
        )
        return httpx2.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "usage": {"input_tokens": 1000, "output_tokens": 10},
                "answers": {
                    f"tab_{first.pk}": {"type": "noul", "noul": 0.2},
                    f"tab_{other.pk}": {"type": "noul", "noul": 0.9},
                    "importance": {
                        "type": "score",
                        "score": 1.5,
                        "confidence": 0.8,
                        "legend": {0: "Low", 1: "Normal", 2: "High"},
                        "probabilities": {0: 0, 1: 0.5, 2: 0.5},
                    },
                },
            },
        )

    transport = httpx2.MockTransport(respond)
    monkeypatch.setattr(
        labeling,
        "TypeSafeClient",
        lambda **kwargs: TypeSafeClient(transport=transport, **kwargs),
    )
    labeling.process()
    assert len(requests) == 1
    assert requests[0]["state"]["email"]["subject"] == "Subject a"
    assert LabelDecision.objects.get().label_id == "Label_other"
    assert Message.objects.get(pk="a").importance == 0.75
