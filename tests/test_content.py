"""App-owned MIME extraction, sanitization, and inline-image policies."""

import base64
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from inbox import content

if TYPE_CHECKING:
    from googleapiclient._apis.gmail.v1.schemas import MessagePart


@pytest.mark.parametrize(
    "source",
    [
        "/settings/",
        "//tracker.example/pixel",
        "http://tracker.example/pixel",
        "https://127.0.0.1/pixel",
        "https://127.1/pixel",
        "https://0x7f.1/pixel",
        "https://host.local/pixel",
        "https://localhost/pixel",
        "data:image/svg+xml,<svg onload=alert(1)>",
        "data:image/png;base64,abc",
        "javascript:alert(1)",
    ],
)
def test_sender_supplied_image_urls_are_blocked_even_after_opt_in(source) -> None:
    from inbox.content import formatted_html

    html = formatted_html(
        {"html": f'<img src="{source}" srcset="https://tracker.example/pixel 2x">'},
        "",
        MagicMock(),
        external=True,
    )
    assert "src=" not in html and "srcset=" not in html


def test_inline_email_cannot_impersonate_app_controls_or_escape_layout():
    from inbox.content import formatted_html

    rendered = formatted_html(
        {
            "html": '<div id="selected-message" class="sender-actions" data-archive name="x" tabindex="0" contenteditable="true" style="position:fixed;z-index:9999;transform:scale(10);display:contents;float:left"><a href="https://example.com" data-sender-note>Link</a><style>body{display:none}</style><form><input autofocus></form></div>'
        },
        "",
        MagicMock(),
        document=False,
    )
    for forbidden in (
        "id=",
        "class=",
        "data-",
        "name=",
        "tabindex=",
        "contenteditable",
        "position",
        "z-index",
        "transform",
        "display:",
        "float:",
        "<style",
        "<form",
        "<input",
        "autofocus",
        "<html",
    ):
        assert forbidden not in rendered
    assert "Link" in rendered


def test_email_hiding_survives_sanitizing_without_other_display_changes():
    from inbox.content import formatted_html

    rendered = formatted_html(
        {
            "html": """<style>/* .note{display:none} */ .hidden, .x .y {display:none}
            @media (max-width: 600px) { .wide {display:none} }</style>
            <div style="display:none;max-height:0;overflow:hidden">Preview</div>
            <div style="color:red; display: none">Spaced</div>
            <div class="box hidden" style="margin:4px">Upgrade your client</div>
            <p class="hidden" style="display:block">Shown</p>
            <p class=wide>Wide</p><p class="note" data-class="hidden">Note</p>
            <span style="display:block">Block</span>"""
        },
        "",
        MagicMock(),
        document=False,
    )
    # Inline preview text and top-level class rules stay hidden.
    assert '<div hidden="" style="max-height:0;overflow:hidden">' in rendered
    assert '<div hidden="" style="color:red">' in rendered
    assert '<div hidden="" style="margin:4px">' in rendered
    # An element's own inline display overrides class rules, as in CSS.
    assert '<p style="">Shown</p>' in rendered
    # Media queries, comments, and attributes merely containing "class" hide nothing.
    assert "<p>Wide</p>" in rendered and "<p>Note</p>" in rendered
    # Other display values could restructure the page layout.
    assert "display:" not in rendered and "Block" in rendered


def test_attachment_count_handles_nested_files_and_ignores_inline_logos():
    from inbox.content import attachment_count

    assert attachment_count({"headers": []}) is None
    assert attachment_count({"mimeType": "text/plain"}) == 0
    assert (
        attachment_count(
            {
                "mimeType": "multipart/mixed",
                "parts": [
                    {
                        "mimeType": "multipart/mixed",
                        "parts": [
                            {"mimeType": "application/pdf", "filename": "report.pdf"},
                            {
                                "mimeType": "image/png",
                                "filename": "logo.png",
                                "headers": [{"name": "Content-ID", "value": "<logo>"}],
                            },
                            {
                                "mimeType": "image/png",
                                "filename": "photo.png",
                                "headers": [
                                    {"name": "Content-ID", "value": "<photo>"},
                                    {
                                        "name": "Content-Disposition",
                                        "value": "attachment",
                                    },
                                ],
                            },
                        ],
                    },
                    {
                        "mimeType": "message/rfc822",
                        "parts": [{"filename": "nested.pdf"}],
                    },
                    {
                        "mimeType": "application/octet-stream",
                        "headers": [
                            {"name": "Content-Disposition", "value": "attachment"}
                        ],
                    },
                ],
            }
        )
        == 4
    )


@pytest.mark.parametrize("failure", ["oversized", "provider-error", "disguised-svg"])
def test_invalid_inline_images_are_omitted_without_repeated_downloads(failure):
    image = {"mime": "image/png", "attachmentId": "image", "size": 1}
    if failure == "oversized":
        image["size"] = content.IMAGE_BYTES + 1
    elif failure == "disguised-svg":
        image["data"] = base64.b64encode(b'<svg onload="alert(1)"></svg>').decode()
    loader = MagicMock(side_effect=RuntimeError("PRIVATE_PROVIDER_ERROR"))
    rendered = content.formatted_html(
        {
            "html": '<img src="cid:image"><img src="cid:image">',
            "images": {"image": image},
        },
        "",
        loader,
    )
    assert "src=" not in rendered and "PRIVATE_PROVIDER_ERROR" not in rendered
    if failure == "provider-error":
        loader.assert_called_once_with("image")
    else:
        loader.assert_not_called()


def test_body_extraction_omits_text_and_nested_attachments():
    attached = base64.urlsafe_b64encode(b"PRIVATE_ATTACHMENT").decode()
    payload: MessagePart = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "text/html",
                "body": {"data": base64.urlsafe_b64encode(b"<p>Hello</p>").decode()},
            },
            {
                "mimeType": "text/plain",
                "filename": "attachment.txt",
                "body": {"data": attached},
            },
            {
                "mimeType": "multipart/mixed",
                "filename": "attached-mail",
                "parts": [{"mimeType": "text/html", "body": {"data": attached}}],
            },
        ],
    }
    assert "PRIVATE_ATTACHMENT" not in content.message_text(payload)
    assert "PRIVATE_ATTACHMENT" not in content.rich_body(payload)["html"]


@pytest.mark.parametrize(
    "header,expected",
    [
        ("<javascript:alert(1)>", ""),
        ("<http://example.com/unsubscribe>", ""),
        ("<https://localhost/unsubscribe>", ""),
        ("<https://127.0.0.1/unsubscribe>", ""),
        ("<https://192.168.0.1/unsubscribe>", ""),
        ("<https://example.local/unsubscribe>", ""),
        ("<https://user:password@example.com/unsubscribe>", ""),
        ("<https://example.com/\nunsafe>", ""),
        ("<https://[broken>", ""),
        ("<mailto:evil@example.com?bcc=victim@example.com>", ""),
        ("<mailto://[>", ""),
        ("<mailto:user\n@example.com>", ""),
        (
            "<mailto:user@example.com?subject=unsubscribe%0ABcc%3Aevil%40example.com>",
            "",
        ),
        (
            "<mailto:leave@example.com?subject=Unsubscribe&body=Please%20remove%20me>",
            "mailto:leave@example.com?subject=Unsubscribe&body=Please+remove+me",
        ),
    ],
)
def test_unsubscribe_target_validation(header, expected):
    from inbox.utils import _unsubscribe_link

    assert _unsubscribe_link(header) == expected
