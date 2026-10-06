"""Rendered tab/search controls and keyboard behavior (not ingestion)."""

import base64

import pytest
from django.conf import settings
from pytest_bdd import given, scenario, then, when
from test_inbox import (
    INLINE_PNG,
    conversation_ready,
    formatted_message_ready,
    mail,
    seed_account,
    synced,
)
from test_inbox import (
    api as api_fixture,
)
from test_inbox import (
    client as client_fixture,
)

from classifications.models import AIRequest
from inbox.models import Message

api = api_fixture
client = client_fixture


def test_tab_keyboard_order_search_and_native_navigation(mail_keyboard) -> None:
    mail_keyboard("""
        press('Tab'); assert.equal(navigations.at(-1), '/?tab=2');
        press('Tab', document, {shiftKey: true}); assert.equal(navigations.at(-1), '/');
        press('3'); assert.equal(navigations.at(-1), '/');
        press('2'); assert.equal(navigations.at(-1), '/?tab=2');
        assert(!press('Tab', new Element('button')).defaultPrevented);
        assert(!press('1', new Element('input')).defaultPrevented);
        tabs[0].focus(); press('Escape'); assert.equal(document.activeElement, searchToggle);
        press('/'); assert(!search.hidden); assert.equal(document.activeElement, searchInput);
        press('Escape'); assert(search.hidden); assert.equal(document.activeElement, searchToggle);
        boot({url: 'http://localhost:8002/?q=hello'});
        searchInput.focus(); press('Escape'); assert.equal(navigations.at(-1), '/');
        boot(); press('ArrowRight', tabs[0], {altKey: true});
        const order = document.getElementById('tab-order-form');
        assert(order.submitted); assert.deepEqual(order.children.map(input => input.value), ['2', '1']);
    """)


@scenario(
    "ui.feature",
    "Use reader action shortcuts without conflicting with editing or sender history",
)
def test_reader_action_shortcuts_and_editor_suppression(mail_keyboard):
    pass


@then(
    "reader shortcuts map to their actions, ignore editors, and distinguish reply from sender history"
)
def reader_shortcuts(mail_keyboard):
    mail_keyboard("""
        boot({reading: true});
        press('d', new Element('textarea'));
        assert(!document.querySelector('[data-archive]').submitted);
        press('d'); assert(document.querySelector('[data-archive]').submitted);
        // Without a saved list, archiving returns to the list and nothing is preloaded.
        assert.equal(document.querySelector('[data-archive] input').value, '/#mail-a');
        assert.equal(requests.length, 0);
        for (const [key, selector] of [['m', '[data-sender-labels]'], ['n', '[data-sender-note]'], ['u', '[data-unsubscribe]']]) {
            press(key); assert(document.querySelector(selector).clicked);
        }
        const reply = document.querySelector('[data-reply]'), dialog = document.querySelector('[data-reply-dialog]');
        press('g'); press('r'); assert(!reply.clicked);
        press('r'); assert(document.querySelector('[data-sender-all]').clicked); assert(!reply.clicked);
        press('O', document, {shiftKey: true}); assert(document.querySelector('[data-reply-gmail]').clicked);
        // r replies inline instead of opening the standalone reply page.
        const before = navigations.length, textarea = dialog.querySelector('textarea');
        press('r'); assert('open' in dialog.attrs); assert.equal(document.activeElement, textarea);
        assert.equal(navigations.length, before);
        // The open reply keeps keys: Escape belongs to the dialog, not the back link.
        assert(!press('Escape', textarea).defaultPrevented); assert(!back.clicked);
        dialog.querySelector('[data-cancel]').click(); assert(!('open' in dialog.attrs));
        assert.equal(navigations.length, before);
        // Sending disables the button so a second submit cannot send a duplicate.
        dialog.querySelector('form').dispatch('submit'); assert(dialog.querySelector('button').disabled);
        boot(); press('c'); assert.equal(navigations.at(-1), '/compose/?next=/');
    """)


@scenario(
    "ui.feature", "Read a conversation and select a reply with its original recipients"
)
def test_read_message_select_reply_and_load_formatted_body(client, tmp_path, api):
    pass


@given(
    "a conversation contains older mail, sent replies, and original recipient headers"
)
def conversation_with_original_recipients(client, tmp_path, api):
    conversation_ready(client, tmp_path, api)
    Message.objects.filter(pk="a").update(
        body="<script>mail text</script>", recipients=None
    )
    api.mailbox["a"]["payload"]["headers"] += [
        {"name": "To", "value": "<img src=x onerror=alert(1)> <to@example.com>"},
        {"name": "To", "value": "second@example.com"},
        {"name": "Cc", "value": "copy@example.com"},
        {"name": "Bcc", "value": "hidden@example.com"},
        {"name": "Delivered-To", "value": "owner@example.com"},
    ]
    # The selected reply has no recipient headers and keeps its HTML in a separate MIME part.
    api.mailbox["sent"]["payload"].update(
        mimeType="text/html",
        body={"attachmentId": "html-body", "size": 50},
        headers=[{"name": "From", "value": "Me <me@example.com>"}],
    )
    api.messages.return_value.attachments.return_value.get.return_value.execute.return_value = {
        "data": base64.urlsafe_b64encode(
            b"<p><strong>Sent reply body</strong></p>"
        ).decode()
    }


@when("I open the conversation from Others", target_fixture="opened_conversation")
def opened_conversation(client):
    response = client.get("/messages/a/?tab=others")
    return response


@then(
    "the conversation and original recipients are displayed safely",
    target_fixture="verify_conversation_recipients",
)
def verify_conversation_recipients(opened_conversation):
    response = opened_conversation
    assert response.status_code == 200
    conversation = response.context["conversation"]
    assert [message.id for message in conversation] == ["old", "a", "sent", "reply"]
    assert "4 messages" in response.text and 'id="selected-message"' in response.text
    assert "Sent" in response.text and "Outside inbox" in response.text
    assert "&lt;script&gt;mail text&lt;/script&gt;" in response.text
    assert "<script>mail text</script>" not in response.text
    for heading, address in [
        ("To", "to@example.com"),
        ("Cc", "copy@example.com"),
        ("Bcc", "hidden@example.com"),
        ("Delivered-To", "owner@example.com"),
    ]:
        assert f"<dt>{heading}</dt>" in response.text and address in response.text
    assert "second@example.com" in response.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in response.text
    assert "<img src=x" not in response.text
    return conversation


@when("I select the sent reply", target_fixture="selected_sent_reply")
def selected_sent_reply(client, verify_conversation_recipients):
    conversation = verify_conversation_recipients
    sent = next(message for message in conversation if message.id == "sent")
    selected = client.get(sent.url)
    return selected


@then(
    "the reply is selected without inventing missing recipients or loading its HTML attachment"
)
def verify_selected_reply(api, selected_sent_reply):
    selected = selected_sent_reply
    assert selected.status_code == 200 and selected.context["message"].id == "sent"
    assert "Me &lt;me@example.com&gt;" in selected.text
    assert (
        "Not provided in message headers" in selected.text
        and "<dt>Bcc</dt>" not in selected.text
    )
    assert selected.context["back_url"] == "/?tab=others"
    assert 'action="/messages/sent/archive/"' in selected.text

    assert selected.context["formatted"]
    api.messages.return_value.attachments.return_value.get.assert_not_called()


@when(
    "I request the selected reply formatted body", target_fixture="requested_reply_body"
)
def requested_reply_body(client, selected_sent_reply):
    selected = selected_sent_reply
    body = client.get(selected.context["body_url"])
    return body


@then("only its HTML body attachment is downloaded and displayed")
def verify_lazy_reply_body(api, requested_reply_body):
    body = requested_reply_body
    assert body.status_code == 200 and "<strong>Sent reply body</strong>" in body.text
    api.messages.return_value.attachments.return_value.get.assert_called_once_with(
        userId="me", messageId="sent", id="html-body"
    )


@scenario(
    "ui.feature",
    "Read formatted email without running sender content or loading trackers",
)
def test_formatted_email_sanitization_embedded_image_and_per_view_consent(
    client, tmp_path, api
):
    pass


@given("an email contains formatted HTML, an embedded image, and a tracker")
def mail_with_images_and_tracker(client, tmp_path, api):
    formatted_message_ready(client, tmp_path, api)


@when(
    "I open the reader and its formatted body", target_fixture="opened_formatted_email"
)
def opened_formatted_email(client):
    reader = client.get("/messages/a/")
    assert reader.status_code == 200 and "Load external images" in reader.text
    rendered = client.get(reader.context["body_url"])
    return rendered


@then(
    "formatted content and the embedded image appear while scripts and trackers are blocked"
)
def verify_default_image_policy(api, opened_formatted_email):
    rendered = opened_formatted_email
    assert rendered.status_code == 200
    assert "Welcome" in rendered.text and "<script" not in rendered.text
    assert "data:image/png;base64," + INLINE_PNG in rendered.text
    assert "tracker.example" not in rendered.text
    assert "img-src data:;" in rendered.headers["Content-Security-Policy"]
    api.messages.return_value.attachments.return_value.get.assert_called_once_with(
        userId="me", messageId="a", id="logo-attachment"
    )


@when(
    "I explicitly allow external images for one view",
    target_fixture="formatted_view_with_images",
)
def formatted_view_with_images(client):
    allowed = client.get("/messages/a/body/?images=1")
    return allowed


@then("that view allows the remote image")
def verify_allowed_external_image(formatted_view_with_images):
    allowed = formatted_view_with_images
    assert 'src="https://tracker.example/pixel"' in allowed.text
    assert "img-src data: https:;" in allowed.headers["Content-Security-Policy"]


@when(
    "I open the formatted body again without image consent",
    target_fixture="formatted_view_without_consent",
)
def formatted_view_without_consent(client):
    blocked = client.get("/messages/a/body/")
    return blocked


@then("external images are blocked again")
def verify_blocked_external_image(formatted_view_without_consent):
    blocked = formatted_view_without_consent
    assert "tracker.example" not in blocked.text
    assert "img-src data:;" in blocked.headers["Content-Security-Policy"]


@scenario(
    "ui.feature", "Edit sender notes with plain forms independently of cached mail"
)
def test_sender_note_edit_render_retention_and_clear(client, tmp_path, api):
    pass


@given(
    "a stored sender has a note editor in the reader",
    target_fixture="stored_sender_for_notes",
)
def stored_sender_for_notes(client, tmp_path, api):
    synced(client, tmp_path, api)
    url = "/senders/edit/?field=note&sender=HUMAN@example.com&next=/messages/a/"
    return url


@when("I create and edit a sender note using its plain form")
def edited_sender_note(client, stored_sender_for_notes):
    url = stored_sender_for_notes
    for note in ("Met at a conference", "Follow up next week"):
        response = client.post(url, data={"note": note, "labels": ["missing"]})
        assert response.status_code == 303
        assert response.headers["Location"] == "/messages/a/"
        assert note in client.get(response.headers["Location"]).text


@then("the note survives removal of cached mail and is cleared without Gmail writes")
def verify_independent_sender_note(client, api, stored_sender_for_notes):
    url = stored_sender_for_notes
    Message.objects.all().delete()
    form = client.get("/senders/edit/?field=note&sender=human@example.com").context[
        "form"
    ]
    assert form["note"].value() == "Follow up next week"
    assert "labels" not in form.fields
    assert client.post(url, data={"note": ""}).status_code == 303
    assert client.get(url).context["form"]["note"].value() == ""
    api.messages.return_value.modify.assert_not_called()


@scenario(
    "ui.feature", "Navigate the mail list and return from the reader with the keyboard"
)
def test_mail_navigation_restores_selection_and_handles_list_boundaries(mail_keyboard):
    pass


@then(
    "mail navigation restores selection after opening and returning, preloads and opens the next mail after archiving, and respects list boundaries"
)
def mail_navigation(mail_keyboard):
    mail_keyboard("""
        press('k'); press('k'); assert.equal(highlighted(), 'mail-a');
        press('j'); press('j'); press('j'); assert.equal(highlighted(), 'mail-c');
        press('k'); press('k'); press('Enter'); assert.equal(navigations.at(-1), '/messages/a/');
        boot({reading: true, url: 'http://localhost:8002/messages/a/'});
        assert.equal(requests.at(-1)[0], '/messages/b/');
        press('d'); assert.equal(document.querySelector('[data-archive] input').value, '/messages/b/');
        press('Escape'); assert.equal(navigations.at(-1), '/#mail-a');
        boot({url: 'http://localhost:8002/#mail-a'});
        assert.equal(highlighted(), 'mail-a');
        assert.equal(document.activeElement, rows.get('mail-a'));
        assert.equal(window.scrollY, 180);
        boot({ids: []}); press('j'); press('Enter'); assert.equal(highlighted(), null);
        // Only the reader's preload of the next mail reaches the network.
        assert.deepEqual(requests.map(([url]) => url), ['/messages/b/']);
    """)


def test_feed_loads_bodies_on_approach_and_marks_focused_mail_read(mail_keyboard):
    mail_keyboard("""
        boot({feed: true});
        const [lazy, reading] = observers, [a, b] = document.querySelectorAll('.feed-mail');
        // Bodies load only as they approach the viewport.
        assert.equal(requests.length, 0);
        lazy.cross(a.querySelector('[data-fragment]'));
        assert.deepEqual(requests.map(([url]) => url), ['/messages/a/body/?fragment=1']);
        // Scrolling quickly past a marks nothing; b stays in focus for a second and is marked read.
        reading.cross(a); assert(a.classList.contains('focused'));
        reading.cross(b); assert(!a.classList.contains('focused')); assert(b.classList.contains('focused'));
        assert.deepEqual(timers.filter(timer => !timer.cleared).map(timer => timer.delay), [1000]);
        timers.forEach(timer => timer.cleared || timer.callback());
        assert(a.classList.contains('unread')); assert(!b.classList.contains('unread'));
        assert.deepEqual(requests.at(-1), ['/messages/b/read/', {method: 'POST', headers: {'X-CSRFToken': 'token'}}]);
        // This harness fails requests; a failed write leaves the email unread for its next focus.
        setImmediate(() => assert(b.classList.contains('unread')));
    """)


def test_feed_j_k_scroll_to_mail_and_mark_the_mail_left_read(mail_keyboard):
    mail_keyboard("""
        boot({feed: true});
        const [a, b] = document.querySelectorAll('.feed-mail');
        press('j'); assert(a.classList.contains('focused')); assert(a.scrolled);
        assert.equal(requests.length, 0);
        // Leaving a with j marks it read at once; its pending timer no longer matters.
        press('j'); assert(b.classList.contains('focused')); assert(!a.classList.contains('focused'));
        assert.deepEqual(requests.map(([url]) => url), ['/messages/a/read/']);
        assert(timers[0].cleared);
        // k moves back and marks b read the same way.
        press('k'); assert(a.classList.contains('focused'));
        assert.deepEqual(requests.map(([url]) => url), ['/messages/a/read/', '/messages/b/read/']);
    """)


def test_context_shortcut_opens_editor_or_focuses_existing_draft(mail_keyboard):
    mail_keyboard("""
        boot({reading: true});
        press('C', document, {altKey: true, shiftKey: true, code: 'KeyC'});
        assert.equal(navigations.at(-1), '/settings/context/?next=/messages/a/');
        boot({editing: true});
        const context = new Element('textarea', {id: 'id_user_context', value: 'Unsaved context'});
        editor.append(context);
        const before = navigations.length;
        press('Ç', document, {altKey: true, shiftKey: true, code: 'KeyC'});
        assert.equal(document.activeElement, context);
        assert.equal(context.value, 'Unsaved context');
        assert.equal(navigations.length, before);
    """)


def test_mail_and_usage_display_ist_across_midnight_without_changing_timestamps(
    client, tmp_path, api
):
    from datetime import UTC, datetime

    seed_account(client, tmp_path)
    timestamp = int(datetime(2026, 9, 13, 21, 15, tzinfo=UTC).timestamp() * 1000)
    api.mailbox = {"a": mail("a")}
    api.mailbox["a"]["internalDate"] = str(timestamp)
    AIRequest.objects.create(
        started_at=timestamp,
        finished_at=timestamp + 25_000,
        model="test",
        message_count=1,
        status="completed",
    )
    inbox = client.get("/")
    assert 'datetime="2026-09-14T02:45:00+05:30"' in inbox.text
    assert "Sep 14, 2026, 02:45 IST" in inbox.text
    settings_page = client.get("/settings/")
    assert "Sep 14, 2026, 02:45:00 IST" in settings_page.text
    assert settings_page.context["usage"]["requests"][0]["duration"] == 25
    assert Message.objects.get(pk="a").received_at == timestamp
    assert AIRequest.objects.get().started_at == timestamp


@pytest.fixture
@given("a keyboard-driven mailbox", target_fixture="mail_keyboard")
def mail_keyboard():
    """Run the shipped vanilla script against a DOM/event boundary, never an app-state adapter."""
    import shutil
    import subprocess

    node = shutil.which("node")
    # Backend-only environments can run pytest without the optional frontend test runtime.
    if node is None:
        pytest.skip("Install Node.js to run frontend keyboard tests.")
    harness = r"""
        const assert = require('node:assert/strict');
        const source = require('node:fs').readFileSync('static/app.js', 'utf8');
        const storage = new Map(), requests = [], navigations = [], observers = [], timers = [];
        // Tests run due timers explicitly; clearTimeout marks one cancelled.
        global.setTimeout = (callback, delay) => timers.push({callback, delay});
        global.clearTimeout = id => { if (timers[id - 1]) timers[id - 1].cleared = true; };
        let document, rows, tabs, reader, editor, back, cancel, search, searchInput, searchToggle;
        class Element {
            constructor(tag = 'div', attrs = {}) {
                this.tagName = tag.toUpperCase(); this.attrs = attrs; this.children = [];
                this.listeners = {}; this.hidden = false; this.dataset = {};
                this.value = attrs.value || ''; this.id = attrs.id || '';
                this.classes = new Set((attrs.class || '').split(' ').filter(Boolean));
                this.classList = {add: name => this.classes.add(name), remove: name => this.classes.delete(name), contains: name => this.classes.has(name)};
                for (const [key, value] of Object.entries(attrs)) if (key.startsWith('data-')) this.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = value;
            }
            matches(selector) {
                return selector.split(',').some(part => {
                    part = part.trim();
                    const split = part.lastIndexOf(' ');
                    if (split >= 0) return this.matches(part.slice(split + 1)) && !!this.parent?.closest(part.slice(0, split));
                    const tag = part.match(/^[a-z]+/i)?.[0];
                    if (tag && this.tagName !== tag.toUpperCase()) return false;
                    for (const match of part.matchAll(/\.([\w-]+)/g)) if (!this.classes.has(match[1])) return false;
                    const id = part.match(/#([\w-]+)/)?.[1];
                    if (id && id !== this.id) return false;
                    for (const match of part.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)) {
                        if (!(match[1] in this.attrs) || (match[2] !== undefined && this.attrs[match[1]] !== match[2])) return false;
                    }
                    return true;
                });
            }
            closest(selector) { return this.matches(selector) ? this : this.parent?.closest(selector) || null; }
            querySelectorAll(selector) { return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]); }
            querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
            append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
            prepend(...children) { for (const child of children) { child.parent = this; this.children.unshift(child); } }
            remove() { this.parent.children = this.parent.children.filter(child => child !== this); }
            addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
            dispatch(type, event = {}) { for (const callback of this.listeners[type] || []) callback(event); }
            setAttribute(key, value) { this.attrs[key] = value; }
            getAttribute(key) { return this.attrs[key] ?? null; }
            focus() { document.activeElement = this; this.dispatch('focus'); }
            scrollIntoView() { this.scrolled = true; }
            click() {
                this.clicked = true;
                const event = {preventDefault() { this.defaultPrevented = true; }};
                this.dispatch('click', event);
                if (this.attrs.href && !event.defaultPrevented) navigations.push(this.attrs.href);
            }
            requestSubmit() { this.submitted = true; this.dispatch('submit'); }
            showModal() { this.attrs.open = ''; }
            close() { delete this.attrs.open; }
        }
        global.Element = Element;
        // Tests move targets across an observer's margin, as scrolling would.
        global.IntersectionObserver = class {
            constructor(callback) { this.callback = callback; this.targets = new Set(); observers.push(this); }
            observe(target) { this.targets.add(target); }
            unobserve(target) { this.targets.delete(target); }
            cross(target, isIntersecting = true) { if (this.targets.has(target)) this.callback([{target, isIntersecting}]); }
        };
        global.sessionStorage = {setItem: (key, value) => storage.set(key, value), getItem: key => storage.get(key) || null};
        // Record requests and fail them like an offline browser; tests assert which were made.
        global.fetch = (...args) => { requests.push(args); return Promise.reject(Error('Offline test harness')); };
        function boot({url = 'http://localhost:8002/', remote = false, reading = false, editing = false, feed = false, ids = ['a', 'b', 'c']} = {}) {
            observers.length = 0;
            global.location = new URL(url);
            global.window = {scrollY: 180, scrollTo: ({top}) => { window.scrollY = top; }};
            document = new Element('body'); global.document = document;
            document.activeElement = document;
            document.getElementById = id => document.querySelector('#' + id);
            document.createElement = tag => new Element(tag);
            const strip = new Element('nav', {class: 'tabs'});
            tabs = [new Element('a', {class: 'tab active', 'data-tab-id': '1', href: '/?tab=1'}), new Element('a', {class: 'tab', 'data-tab-id': '2', href: '/?tab=2'}), new Element('a', {class: 'tab', href: '/'})];
            strip.append(...tabs); document.append(strip);
            searchToggle = new Element('button', {class: 'search-toggle'});
            search = new Element('form', {id: 'mail-search'});
            searchInput = new Element('input', {type: 'search', value: location.searchParams.get('q') || ''});
            search.append(searchInput, new Element('a', {'data-close-search': '', href: '/'}));
            document.append(searchToggle, search, new Element('form', {id: 'tab-order-form'}));
            document.append(new Element('a', {'data-user-context': '', href: '/settings/context/?next=/messages/a/'}));
            document.append(new Element('a', {'data-compose': '', href: '/compose/?next=/'}));
            rows = new Map(ids.map(id => ['mail-' + id, new Element('a', {id: 'mail-' + id, class: 'mail', href: '/messages/' + id + '/' + (remote ? '?remote=1' : '')})]));
            if (!reading && !editing && !feed) document.append(...rows.values());
            if (feed) {
                document.append(new Element('input', {name: 'csrfmiddlewaretoken', value: 'token'}));
                for (const id of ids) {
                    const item = new Element('article', {id: 'mail-' + id, class: 'feed-mail unread', 'data-read-url': '/messages/' + id + '/read/'});
                    item.append(new Element('div', {'data-fragment': '/messages/' + id + '/body/?fragment=1', 'data-lazy': ''}));
                    document.append(item);
                }
            }
            reader = new Element('section', {'data-reader': ''});
            back = new Element('a', {'data-back': '', href: '/#mail-a'});
            if (reading) {
                const archive = new Element('form', {'data-archive': ''});
                archive.append(new Element('input', {name: 'next', value: '/#mail-a'}));
                document.append(reader, back, archive);
                for (const field of ['note', 'labels', 'all']) document.append(new Element('a', {['data-sender-' + field]: '', href: '/sender-' + field}));
                document.append(new Element('a', {'data-unsubscribe': '', href: '/unsubscribe'}));
                document.append(new Element('a', {'data-reply-gmail': '', href: 'https://mail.google.com/mail/?authuser=me%40example.com#all/a', target: '_blank'}));
                const dialog = new Element('dialog', {'data-reply-dialog': ''}), send = new Element('form', {'data-send': ''});
                send.append(new Element('textarea'), new Element('button', {type: 'submit'}), new Element('a', {'data-cancel': '', href: '/messages/a/'}));
                dialog.append(send);
                document.append(new Element('a', {'data-reply': '', href: '/messages/a/reply/'}), dialog);
            }
            editor = new Element('form', {'data-editor': ''});
            cancel = new Element('a', {'data-cancel': '', href: '/messages/a/'});
            if (editing) document.append(editor, cancel);
            eval(source);
            document.dispatch('DOMContentLoaded');
        }
        function press(key, target = document.activeElement, extra = {}) {
            const event = {key, target, preventDefault() { this.defaultPrevented = true; }, ...extra};
            document.dispatch('keydown', event);
            // Native anchor activation is a browser behavior, not an app-specific shortcut.
            if (key === 'Enter' && !event.defaultPrevented && !event.ctrlKey && !event.metaKey && !event.altKey && !event.isComposing && target.matches('a')) target.click();
            return event;
        }
        function highlighted() { return document.querySelector('.mail.highlighted')?.id || null; }
        boot();
    """

    def run(script: str) -> None:
        result = subprocess.run(
            [node, "-e", harness + script],
            check=False,
            cwd=settings.BASE_DIR,
            text=True,
            capture_output=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr

    return run
