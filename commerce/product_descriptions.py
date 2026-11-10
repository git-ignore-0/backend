"""Store-only Tiptap contract and compatibility with legacy descriptions."""

import json

from rest_framework.exceptions import ValidationError

from content.validators import ALLOWED_MARKS, allowed_link_href

MAX_DESCRIPTION_UNITS = 10_000
MAX_DOCUMENT_BYTES = 128 * 1024
MAX_DOCUMENT_NODES = 2000
MAX_DOCUMENT_DEPTH = 30
BLOCKS = {"paragraph", "heading", "bulletList", "orderedList"}
INLINE = {"text", "hardBreak"}
ALLOWED_NODES = BLOCKS | INLINE | {"doc", "listItem"}


def _invalid(message):
    raise ValidationError({"description_document": message})


def _validate_marks(marks):
    if not isinstance(marks, list):
        _invalid("Marks must be a list.")
    seen = set()
    for mark in marks:
        if not isinstance(mark, dict) or set(mark) - {"type", "attrs"}:
            _invalid("Invalid text formatting.")
        kind = mark.get("type")
        if not isinstance(kind, str) or kind not in ALLOWED_MARKS or kind in seen:
            _invalid("Unsupported or duplicate text formatting.")
        seen.add(kind)
        attrs = mark.get("attrs", {})
        if not isinstance(attrs, dict):
            _invalid("Formatting attributes must be an object.")
        if kind != "link":
            if attrs:
                _invalid("Text formatting does not accept attributes.")
            continue
        if set(attrs) - {"href", "target", "rel", "class"}:
            _invalid("Unsupported link attributes.")
        href = attrs.get("href")
        try:
            safe = (
                isinstance(href, str)
                and not any(ord(character) < 32 or ord(character) == 127 for character in href)
                and href == href.strip()
                and allowed_link_href(href)
            )
        except ValueError:
            safe = False
        if not safe:
            _invalid("Only HTTPS and email links are allowed.")
        if attrs.get("target") not in (None, "_blank", "_self"):
            _invalid("Unsupported link target.")
        if any(
            not isinstance(attrs[key], (str, type(None)))
            for key in ("rel", "class")
            if key in attrs
        ):
            _invalid("Invalid link attributes.")


def validate_description_document(document):
    if not isinstance(document, dict) or document.get("type") != "doc":
        _invalid("Expected a Tiptap document object.")
    try:
        size = len(json.dumps(document, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, RecursionError, UnicodeError):
        _invalid("Invalid document JSON.")
    if size > MAX_DOCUMENT_BYTES:
        _invalid("Document exceeds 128 KiB.")

    stack = [(document, 0)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if depth > MAX_DOCUMENT_DEPTH or count > MAX_DOCUMENT_NODES:
            _invalid("Document is too complex.")
        if not isinstance(node, dict):
            _invalid("Document nodes must be objects.")
        kind = node.get("type")
        if not isinstance(kind, str) or kind not in ALLOWED_NODES:
            _invalid("Unsupported document node.")
        if kind == "doc" and depth != 0:
            _invalid("A document cannot contain another document.")
        if set(node) - {"type", "attrs", "content", "text", "marks"}:
            _invalid("Unsupported document properties.")
        attrs = node.get("attrs", {})
        if not isinstance(attrs, dict):
            _invalid("Node attributes must be an object.")
        if kind == "heading":
            if (
                set(attrs) != {"level"}
                or type(attrs["level"]) is not int
                or attrs["level"] not in {2, 3, 4}
            ):
                _invalid("Only heading levels 2–4 are allowed.")
        elif kind == "orderedList":
            start = attrs.get("start", 1)
            # Tiptap 2 emits type:null for its default decimal list format.
            if set(attrs) - {"start", "type"} or attrs.get("type") is not None:
                _invalid("Unsupported ordered-list attributes.")
            if type(start) is not int or not 0 <= start <= 2_147_483_647:
                _invalid("Invalid ordered-list start.")
        elif attrs:
            _invalid("Unsupported node attributes.")
        if kind in INLINE:
            _validate_marks(node.get("marks", []))
            if "content" in node:
                _invalid("Inline nodes cannot have children.")
            if kind == "text":
                if not isinstance(node.get("text"), str) or not node["text"]:
                    _invalid("Text nodes require nonempty text.")
            elif "text" in node:
                _invalid("A line break cannot contain text.")
            continue
        if "text" in node or "marks" in node:
            _invalid("Block nodes cannot contain text or marks directly.")
        children = node.get("content", [])
        if not isinstance(children, list):
            _invalid("Node content must be a list.")
        allowed_children = (
            INLINE
            if kind in {"paragraph", "heading"}
            else {"listItem"}
            if kind in {"bulletList", "orderedList"}
            else BLOCKS
        )
        if any(
            not isinstance(child, dict)
            or not isinstance(child.get("type"), str)
            or child["type"] not in allowed_children
            for child in children
        ):
            _invalid("Invalid parent/child document structure.")
        if kind in {"bulletList", "orderedList", "listItem"} and not children:
            _invalid("Lists and list items cannot be empty.")
        if kind == "listItem" and children[0]["type"] != "paragraph":
            _invalid("A list item must start with a paragraph.")
        stack.extend((child, depth + 1) for child in children)
    text = description_text(document)
    if len(text.encode("utf-16-le")) // 2 > MAX_DESCRIPTION_UNITS:
        _invalid("Product information must not exceed 10,000 characters.")
    return text


def description_text(node):
    if node["type"] == "text":
        return node["text"]
    if node["type"] == "hardBreak":
        return "\n"
    separator = "\n" if node["type"] in {"doc", "bulletList", "orderedList", "listItem"} else ""
    return separator.join(description_text(child) for child in node.get("content", []))


def prepare_product_translations(submitted, *, instance=None):
    if submitted is None:
        return None
    previous = {row.locale: row for row in instance.translations.all()} if instance else {}
    prepared = []
    for index, item in enumerate(submitted):
        row = item.copy()
        if "locale" not in row:
            raise ValidationError({"translations": {index: {"locale": "This field is required."}}})
        original = previous.get(row["locale"])
        if "name" not in row:
            if original is None:
                raise ValidationError(
                    {"translations": {index: {"name": "This field is required."}}}
                )
            row["name"] = original.name
        if "description_document" in row:
            if row["description_document"] is not None:
                try:
                    row["description"] = validate_description_document(row["description_document"])
                except ValidationError as error:
                    raise ValidationError({"translations": {index: error.detail}}) from error
        elif "description" in row and (
            original is None or row["description"] != original.description
        ):
            # Legacy clients changing text must not leave stale formatted content behind.
            row["description_document"] = None
        prepared.append(row)
    return prepared
