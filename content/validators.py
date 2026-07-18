from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ValidationError

ALLOWED_NODES = {
    "doc",
    "paragraph",
    "heading",
    "bulletList",
    "orderedList",
    "listItem",
    "blockquote",
    "hardBreak",
    "text",
    "image",
}
ALLOWED_MARKS = {"bold", "italic", "link"}


def allowed_image_source(source):
    path = urlparse(source)
    if path.scheme == "https":
        return True
    if not path.scheme and source.startswith("/") and not source.startswith("//"):
        return True
    return bool(
        settings.DEBUG
        and path.scheme == "http"
        and path.hostname in {"127.0.0.1", "localhost", "::1"}
    )


def allowed_link_href(href):
    parsed = urlparse(href)
    if parsed.scheme == "https" and parsed.hostname:
        return True
    if parsed.scheme == "mailto" and "@" in parsed.path:
        return True
    return bool(
        settings.DEBUG
        and parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    )


def validate_tiptap_document(document):
    if not isinstance(document, dict) or document.get("type") != "doc":
        raise ValidationError({"body": "Invalid rich-text document."})

    def visit(node, depth=0):
        if depth > 30 or not isinstance(node, dict) or node.get("type") not in ALLOWED_NODES:
            raise ValidationError({"body": "Rich-text document contains an unsupported node."})
        attrs = node.get("attrs") or {}
        if node.get("type") == "heading" and attrs.get("level") not in {2, 3, 4}:
            raise ValidationError({"body": "Only heading levels 2–4 are allowed."})
        if node.get("type") == "image":
            source = attrs.get("src", "")
            if not isinstance(source, str) or not source or not allowed_image_source(source):
                raise ValidationError({"body": "Image URL is not allowed."})
        for mark in node.get("marks") or []:
            if not isinstance(mark, dict) or mark.get("type") not in ALLOWED_MARKS:
                raise ValidationError({"body": "Rich-text document contains an unsupported mark."})
            if mark.get("type") == "link":
                href = (mark.get("attrs") or {}).get("href", "")
                if not isinstance(href, str) or not allowed_link_href(href):
                    raise ValidationError({"body": "Only HTTPS and email links are allowed."})
        if len(node.get("text", "")) > 20000:
            raise ValidationError({"body": "A text node is too long."})
        for child in node.get("content") or []:
            visit(child, depth + 1)

    visit(document)


def tiptap_has_content(document):
    """Return true when the document contains visible text or an image."""

    def visit(node):
        if node.get("type") == "image" or node.get("text", "").strip():
            return True
        return any(visit(child) for child in node.get("content") or [])

    return isinstance(document, dict) and visit(document)
