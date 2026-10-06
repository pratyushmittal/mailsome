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


def test_framed_email_keeps_its_own_styles_but_nothing_active():
    from inbox.content import formatted_html

    rendered = formatted_html(
        {
            "html": """<html><head><title>Digest</title>
            <style>.date{font-size:64px} .hidden{display:none}</style></head>
            <body><div class="date" id="day" style="display:flex">12</div>
            <script>alert(1)</script><form><input autofocus></form><iframe></iframe>
            <a href="/settings/">Relative</a><a href="https://example.com">Link</a></body></html>"""
        },
        "",
        MagicMock(),
    )
    # Layouts depend on their own stylesheets, classes, and display values.
    assert "<style>.date{font-size:64px} .hidden{display:none}</style>" in rendered
    assert '<div class="date" id="day" style="display:flex">12</div>' in rendered
    # Titles would show as stray text; active content and app-relative links go.
    assert "Digest" not in rendered and "Link" in rendered
    for forbidden in (
        "<script",
        "<form",
        "<input",
        "autofocus",
        "<iframe",
        "/settings/",
    ):
        assert forbidden not in rendered


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
