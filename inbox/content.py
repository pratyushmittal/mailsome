"""Email MIME content and a deliberately restricted HTML presentation."""

from __future__ import annotations

import base64
import ipaddress
import re
from collections.abc import Callable, Iterator
from email.message import Message as EmailMessage
from html import escape
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

import html2text
import nh3

if TYPE_CHECKING:
    from googleapiclient._apis.gmail.v1.schemas import MessagePart

IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
IMAGE_BYTES = 2 * 1024 * 1024
DOWNLOAD_BYTES = 25 * 1024 * 1024


def parts(payload: MessagePart) -> Iterator[MessagePart]:
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    # Attached documents/emails aren't the message body, even if they contain HTML.
    if (
        headers.get("content-disposition", "").lower().startswith("attachment")
        or payload.get("mimeType") == "message/rfc822"
        or (payload.get("filename") and payload.get("mimeType") not in IMAGE_TYPES)
    ):
        return
    yield payload
    for child in payload.get("parts", []):
        yield from parts(child)


def decode_text(part: MessagePart) -> str:
    content_type = EmailMessage()
    content_type["Content-Type"] = next(
        (
            h["value"]
            for h in part.get("headers", [])
            if h["name"].lower() == "content-type"
        ),
        part.get("mimeType", "text/plain"),
    )
    data = part.get("body", {}).get("data", "")
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    try:
        return raw.decode(
            content_type.get_content_charset() or "utf-8", errors="replace"
        )
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def message_text(payload: MessagePart) -> str:
    """Keep the plain-text representation for search, fallback and AI."""
    texts: dict[str, list[str]] = {"text/plain": [], "text/html": []}
    for part in parts(payload):
        # Text attachments and attachment-backed bodies are not downloaded by classification.
        if (
            part.get("filename")
            or part.get("mimeType") not in texts
            or not part.get("body", {}).get("data")
        ):
            continue
        texts[part["mimeType"]].append(decode_text(part))
    return (
        "\n\n".join(texts["text/plain"])
        or html2text.html2text("\n\n".join(texts["text/html"]))
        or "No inline text body is available."
    )


def file_parts(
    payload: MessagePart, path: str = "0"
) -> Iterator[tuple[str, MessagePart]]:
    """Identify downloadable MIME parts; never recurse inside attached emails/documents."""
    disposition = next(
        (
            h["value"]
            for h in payload.get("headers", [])
            if h["name"].lower() == "content-disposition"
        ),
        "",
    )
    # Inline images can have filenames too; offer a download without treating them as body text.
    if (
        payload.get("filename")
        or disposition.lower().startswith("attachment")
        or payload.get("mimeType") == "message/rfc822"
    ):
        yield path, payload
        return
    for index, child in enumerate(payload.get("parts", [])):
        yield from file_parts(child, f"{path}.{index}")


def attachment_count(payload: MessagePart) -> int | None:
    """Count files, not inline CID logos; attached messages count as one file."""
    # Header-only legacy responses do not tell us whether MIME attachments exist.
    if not payload.get("mimeType"):
        return None
    count = 0
    for _, part in file_parts(payload):
        headers = {
            header["name"].lower(): header["value"]
            for header in part.get("headers", [])
        }
        # Embedded images belong to the body unless explicitly attached as a file.
        if (
            part.get("mimeType", "").startswith("image/")
            and headers.get("content-id")
            and not headers.get("content-disposition", "")
            .lower()
            .startswith("attachment")
        ):
            continue
        count += 1
    return count


def rich_body(payload: MessagePart) -> dict[str, Any]:
    """Extract a formatted-reader cache from an already downloaded Gmail MIME tree.

    Return HTML text, references to HTML body parts stored separately by Gmail,
    inline images indexed by Content-ID, and attachment descriptors (part path,
    name, MIME type, and size). Ordinary file attachments are not used as HTML.
    Inline image data is retained within the limits below; separately stored
    image bytes and HTML bodies are left as references for the reader to load.
    Attachment IDs must be resolved against the message this payload came from.

    This function does not fetch data or write to the database. The returned HTML
    is not yet sanitized; formatted_html() sanitizes it before browser display.
    """
    html = []
    html_parts = []
    images = {}
    remaining = 8 * 1024 * 1024
    for part in parts(payload):
        headers = {h["name"].lower(): h["value"] for h in part.get("headers", [])}
        # Inline image filenames are normal; a Content-ID associates them with the body.
        if (
            part.get("mimeType") in IMAGE_TYPES
            and headers.get("content-id")
            and len(images) < 20
            and part.get("body", {}).get("size", 0) <= IMAGE_BYTES
            and len(part.get("body", {}).get("data", ""))
            <= min(IMAGE_BYTES * 4 // 3, remaining)
        ):
            # Keep inline data bounded in the cache; attachment bytes still load only when viewed.
            remaining -= len(part.get("body", {}).get("data", ""))
            images[headers["content-id"].strip().strip("<>")] = {
                "mime": part["mimeType"],
                **part.get("body", {}),
            }
        # Ordinary attachments never become formatted message content.
        if part.get("mimeType") == "text/html" and not part.get("filename"):
            html.append(decode_text(part))
            # Gmail can return the actual HTML body via an attachment ID without a filename.
            if part.get("body", {}).get("attachmentId") and len(html_parts) < 5:
                html_parts.append(part)
    return {
        "html": "\n\n".join(html),
        "html_parts": html_parts,
        "images": images,
        # Store descriptors only, never document bytes or arbitrary download URLs.
        "attachments": [
            {
                "id": path,
                "name": part.get("filename") or "attachment",
                "mime": part.get("mimeType", "application/octet-stream"),
                "size": part.get("body", {}).get("size", 0),
            }
            for path, part in file_parts(payload)
        ],
    }


def _attribute(attributes: str, name: str) -> str:
    """Read one attribute's value from a raw tag's attribute text, quoted or not."""
    found = re.search(
        rf"""(?<![\w-]){name}\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""",
        attributes,
        re.IGNORECASE,
    )
    return "".join(filter(None, found.groups())) if found else ""


def _mark_hidden(html: str) -> str:
    """Turn the email's own hiding into `hidden`; sanitizing drops display and <style>.

    Hidden means inline display:none, or a class hidden by a top-level rule such as
    `.hidden{display:none}`. An element's own inline display wins, as in CSS.
    """
    # Most emails never hide anything.
    if not re.search(r"display\s*:\s*none", html, re.IGNORECASE):
        return html
    css = " ".join(
        re.findall(r"<style[^>]*>(.*?)</style>", html, re.DOTALL | re.IGNORECASE)
    )
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    # @media and similar blocks target other screens or features; keep top-level rules.
    css = re.sub(r"@[^{;]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", css)
    # Only plain `.class` selectors count; text before ";" is a stray @import.
    hidden = {
        match[1]
        for selectors, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        if re.search(r"display\s*:\s*none", declarations, re.IGNORECASE)
        for selector in selectors.rsplit(";", 1)[-1].split(",")
        if (match := re.fullmatch(r"\s*\.([\w-]+)\s*", selector))
    }

    def mark(tag: re.Match[str]) -> str:
        inline = re.findall(
            r"display\s*:\s*([\w-]+)", _attribute(tag[2], "style"), re.IGNORECASE
        )
        # The last inline display overrides class rules.
        if inline:
            hide = inline[-1].casefold() == "none"
        else:
            hide = bool(hidden.intersection(_attribute(tag[2], "class").split()))
        return f"<{tag[1]} hidden{tag[2]}>" if hide else tag[0]

    return re.sub(r"<([a-zA-Z][\w-]*)(\s[^>]*)>", mark, html)


def formatted_html(
    body: dict[str, Any],
    text: str,
    load_part: Callable[[str], str],
    *,
    external: bool = False,
    document: bool = True,
) -> str:
    """Only allow inert formatting: no app selectors, controls, scripts, or positioned overlays."""
    images: dict[str, str | None] = {}
    remaining = 8 * 1024 * 1024

    def attribute(tag: str, name: str, value: str) -> str | None:
        nonlocal remaining
        # URL-bearing attributes are limited to explicit links and raster image sources.
        if name not in {"href", "src"}:
            return value
        try:
            url = urlsplit(value)
        except ValueError:
            return None
        # Relative links must never resolve against the local app; no scripts or file URLs.
        if name == "href":
            return value if url.scheme in {"https", "http", "mailto"} else None
        # Remote images are a per-view opt-in; CSS backgrounds and srcset aren't allowed.
        if url.scheme == "https":
            host = url.hostname or ""
            # Consent is for external images, not requests to local services or credentialed URLs.
            if (
                not external
                or url.username
                or url.password
                or "." not in host
                # Browsers also accept shorthand, octal and hexadecimal IPv4 hosts.
                or re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", host.rsplit(".", 1)[-1])
                or host.endswith((".local", ".localhost", ".internal", "."))
            ):
                return None
            try:
                ipaddress.ip_address(host)
            except ValueError:
                return value
            return None
        # Only MIME-associated inline images are trusted, never sender-supplied data/SVG URLs.
        if url.scheme != "cid":
            return None
        cid = unquote(value[4:])
        # Repeated references share one bounded fetch, including failures.
        if cid in images:
            return images[cid]
        images[cid] = None
        part = body.get("images", {}).get(cid, {})
        # Bound both provider calls and decoded bytes, including misleading size metadata.
        if (
            len(images) > 20
            or part.get("mime") not in IMAGE_TYPES
            or not 0 <= part.get("size", 0) <= min(IMAGE_BYTES, remaining)
        ):
            return None
        try:
            data = part.get("data") or load_part(part["attachmentId"])
            # Check before decoding a potentially oversized provider response.
            if len(data) > (min(IMAGE_BYTES, remaining) + 2) * 4 // 3:
                return None
            raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
        except Exception:  # noqa: BLE001 — never expose provider errors from the sanitizer callback.
            return None
        # Validate raster signatures too; a sender-provided MIME label alone is not sufficient.
        raster = (
            (part["mime"] == "image/png" and raw.startswith(b"\x89PNG\r\n\x1a\n"))
            or (part["mime"] == "image/jpeg" and raw.startswith(b"\xff\xd8\xff"))
            or (part["mime"] == "image/gif" and raw[:6] in {b"GIF87a", b"GIF89a"})
            or (
                part["mime"] == "image/webp"
                and raw.startswith(b"RIFF")
                and raw[8:12] == b"WEBP"
            )
        )
        if not raster or len(raw) > min(IMAGE_BYTES, remaining):
            return None
        remaining -= len(raw)
        images[cid] = f"data:{part['mime']};base64," + base64.b64encode(raw).decode(
            "ascii"
        )
        return images[cid]

    html = body.get("html", "")
    for part in body.get("html_parts", []):
        # These are body parts, not document attachments; fetch only during explicit reading.
        try:
            if part["body"].get("size", 0) > IMAGE_BYTES or len(html) > IMAGE_BYTES:
                raise ValueError("Formatted body exceeds the display limit")
            data = load_part(part["body"]["attachmentId"])
            if len(data) > (IMAGE_BYTES + 2) * 4 // 3:
                raise ValueError("Formatted body exceeds the display limit")
            html += decode_text({**part, "body": {"data": data}})
        except Exception:  # noqa: BLE001 — show a safe fallback, never attachment/provider diagnostics.
            html += (
                "<p>Some formatted content could not be loaded. Reload to retry.</p>"
            )

    clean = nh3.clean(
        _mark_hidden(html),
        tags={
            "a",
            "abbr",
            "b",
            "blockquote",
            "br",
            "caption",
            "center",
            "code",
            "col",
            "colgroup",
            "dd",
            "del",
            "div",
            "dl",
            "dt",
            "em",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "hr",
            "i",
            "img",
            "li",
            "ol",
            "p",
            "pre",
            "s",
            "small",
            "span",
            "strong",
            "sub",
            "sup",
            "table",
            "tbody",
            "td",
            "th",
            "thead",
            "tr",
            "u",
            "ul",
        },
        clean_content_tags={
            "script",
            "style",
            "iframe",
            "object",
            "embed",
            "svg",
            "math",
            "form",
            "template",
            "noscript",
        },
        attributes={
            "*": {"style", "title", "dir", "lang", "hidden"},
            "a": {"href"},
            "img": {"src", "alt", "width", "height"},
            "table": {
                "width",
                "cellpadding",
                "cellspacing",
                "border",
                "align",
                "bgcolor",
            },
            "td": {
                "width",
                "height",
                "colspan",
                "rowspan",
                "align",
                "valign",
                "bgcolor",
            },
            "th": {"colspan", "rowspan", "align"},
        },
        attribute_filter=attribute,
        set_tag_attribute_values={"a": {"target": "_blank"}},
        url_schemes={"https", "http", "mailto", "cid", "data"},
        url_relative="deny",
        filter_style_properties={
            "color",
            "background-color",
            "font-family",
            "font-size",
            "font-weight",
            "font-style",
            "line-height",
            "text-align",
            "text-decoration",
            "vertical-align",
            "white-space",
            "word-break",
            "overflow-wrap",
            "width",
            "max-width",
            "min-width",
            "height",
            "max-height",
            # Emails clip preview text with these; display:none becomes `hidden`.
            "overflow",
            "visibility",
            "margin",
            "margin-top",
            "margin-bottom",
            "margin-left",
            "margin-right",
            "padding",
            "padding-top",
            "padding-bottom",
            "padding-left",
            "padding-right",
            "border",
            "border-width",
            "border-style",
            "border-color",
            "border-radius",
            "border-collapse",
            "border-spacing",
            "table-layout",
        },
    )
    fragment = clean if html else "<pre>" + escape(text) + "</pre>"
    # The enhanced reader embeds only this sanitized fragment, never the source email HTML.
    if not document:
        return fragment
    return (
        '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><style>body{margin:16px;color:#252821;background:white;font:15px/1.6 sans-serif;overflow-wrap:anywhere}img{max-width:100%;height:auto}pre{white-space:pre-wrap}a{color:#28634e}</style></head><body>'
        + fragment
        + "</body></html>"
    )
