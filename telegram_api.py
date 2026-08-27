"""Small Telegram Bot API transport helpers."""
from __future__ import annotations

import json
import mimetypes
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def call(token: str, method: str, payload: dict[str, Any], *, timeout: int = 30) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "error_code": exc.code,
            "description": exc.read().decode(errors="replace")[:1000],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def call_multipart(
    token: str,
    method: str,
    payload: dict[str, Any],
    files: dict[str, Path],
    *,
    timeout: int = 60,
) -> dict[str, Any]:
    boundary = "----novsutt"
    parts: list[bytes] = []
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        parts.append(str(value).encode("utf-8"))
        parts.append(b"\r\n")
    for field, path in files.items():
        path = Path(path)
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{field}"; filename="{path.name}"\r\n'.encode())
        parts.append(f"Content-Type: {mime}\r\n\r\n".encode())
        parts.append(path.read_bytes())
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error_code": exc.code, "description": exc.read().decode(errors="replace")[:1000]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}



_RICH_TEXT_TYPES_WITH_TEXT = {
    "bold", "italic", "underline", "strikethrough", "spoiler", "subscript",
    "superscript", "marked", "code", "url", "email_address", "phone_number",
    "bank_card_number", "mention", "hashtag", "cashtag", "bot_command",
    "button", "anchor", "anchor_link", "reference", "reference_link",
    "text_mention", "custom_emoji", "datetime", "math",
}
_RICH_BLOCK_TYPES = {
    "paragraph", "heading", "pre", "footer", "divider", "math", "anchor",
    "list", "blockquote", "expandable_blockquote", "pullquote", "collage",
    "slideshow", "table", "details", "map", "buttons", "animation", "audio",
    "document", "photo", "video", "voice_note", "thinking",
}
_MAX_RICH_TEXT_CHARS = 32_768
_MAX_RICH_BLOCKS = 500
_MAX_RICH_DEPTH = 16
_MAX_RICH_MEDIA = 50


def _rich_error(path: str, message: str) -> ValueError:
    return ValueError(f"invalid rich payload at {path}: {message}")


def _validate_rich_text(value: Any, path: str, state: dict[str, int], depth: int) -> None:
    if depth > _MAX_RICH_DEPTH:
        raise _rich_error(path, f"nesting exceeds {_MAX_RICH_DEPTH} levels")
    if isinstance(value, str):
        state["text"] += len(value)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_rich_text(item, f"{path}[{index}]", state, depth)
        return
    if not isinstance(value, dict):
        raise _rich_error(path, "RichText must be a string, array, or object")
    rich_type = value.get("type")
    if rich_type not in _RICH_TEXT_TYPES_WITH_TEXT:
        raise _rich_error(f"{path}.type", f"unknown RichText type {rich_type!r}")
    if "text" not in value:
        raise _rich_error(path, f"RichText {rich_type!r} requires text")
    _validate_rich_text(value["text"], f"{path}.text", state, depth + 1)
    if rich_type == "url" and not isinstance(value.get("url"), str):
        raise _rich_error(path, "RichTextUrl requires a string url")


def _validate_table_cell(cell: Any, path: str, state: dict[str, int], depth: int) -> None:
    if not isinstance(cell, dict):
        raise _rich_error(path, "table cell must be an object")
    if "type" in cell:
        raise _rich_error(path, "RichBlockTableCell must not have a type field")
    if cell.get("align") not in {"left", "center", "right"}:
        raise _rich_error(path, "align must be left, center, or right")
    if cell.get("valign") not in {"top", "middle", "bottom"}:
        raise _rich_error(path, "valign must be top, middle, or bottom")
    if "is_header" in cell and cell["is_header"] is not True:
        raise _rich_error(path, "is_header, when present, must be true")
    for span in ("colspan", "rowspan"):
        if span in cell and (not isinstance(cell[span], int) or isinstance(cell[span], bool) or cell[span] <= 1):
            raise _rich_error(path, f"{span}, when present, must be an integer greater than 1")
    if "text" in cell:
        _validate_rich_text(cell["text"], f"{path}.text", state, depth + 1)


def _validate_rich_block(block: Any, path: str, state: dict[str, int], depth: int) -> None:
    if depth > _MAX_RICH_DEPTH:
        raise _rich_error(path, f"nesting exceeds {_MAX_RICH_DEPTH} levels")
    if not isinstance(block, dict):
        raise _rich_error(path, "block must be an object")
    block_type = block.get("type")
    if block_type not in _RICH_BLOCK_TYPES:
        raise _rich_error(f"{path}.type", f"unknown block type {block_type!r}")
    state["blocks"] += 1

    if block_type in {"paragraph", "heading", "pre", "footer", "blockquote", "expandable_blockquote", "pullquote"}:
        if "text" not in block:
            raise _rich_error(path, f"{block_type!r} block requires text")
        _validate_rich_text(block["text"], f"{path}.text", state, depth + 1)
    if block_type == "heading":
        size = block.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= 6:
            raise _rich_error(path, "heading size must be an integer from 1 to 6")
    if block_type == "pullquote" and "credit" in block:
        _validate_rich_text(block["credit"], f"{path}.credit", state, depth + 1)

    if block_type == "details":
        if "summary" not in block:
            raise _rich_error(path, "details block requires summary")
        _validate_rich_text(block["summary"], f"{path}.summary", state, depth + 1)
        nested = block.get("blocks")
        if not isinstance(nested, list):
            raise _rich_error(path, "details block requires a blocks array")
        if "is_open" in block and block["is_open"] is not True:
            raise _rich_error(path, "is_open, when present, must be true")
        for index, child in enumerate(nested):
            _validate_rich_block(child, f"{path}.blocks[{index}]", state, depth + 1)

    if block_type == "table":
        rows = block.get("cells")
        if not isinstance(rows, list):
            raise _rich_error(path, "table block requires a cells array")
        state["blocks"] += len(rows)  # Table rows count toward the API block limit.
        for row_index, row in enumerate(rows):
            if not isinstance(row, list):
                raise _rich_error(f"{path}.cells[{row_index}]", "table row must be an array")
            if len(row) > 20:
                raise _rich_error(f"{path}.cells[{row_index}]", "table cannot have more than 20 columns")
            for cell_index, cell in enumerate(row):
                _validate_table_cell(cell, f"{path}.cells[{row_index}][{cell_index}]", state, depth + 1)
        for flag in ("is_bordered", "is_striped", "is_compact"):
            if flag in block and block[flag] is not True:
                raise _rich_error(path, f"{flag}, when present, must be true")
        if "caption" in block:
            _validate_rich_text(block["caption"], f"{path}.caption", state, depth + 1)

    if block_type == "photo":
        photo = block.get("photo")
        if not isinstance(photo, dict) or photo.get("type") != "photo" or not isinstance(photo.get("media"), str):
            raise _rich_error(path, "photo block requires an InputMediaPhoto")
        state["media"] += 1
        caption = block.get("caption")
        if caption is not None:
            if not isinstance(caption, dict) or "text" not in caption:
                raise _rich_error(path, "photo caption must be a RichBlockCaption object")
            _validate_rich_text(caption["text"], f"{path}.caption.text", state, depth + 1)
            if "credit" in caption:
                _validate_rich_text(caption["credit"], f"{path}.caption.credit", state, depth + 1)


def validate_rich_message(rich_message: dict[str, Any]) -> None:
    """Validate the InputRichMessage subset used by this project.

    The validator is intentionally local and dependency-free.  It catches wire
    schema errors before an HTTP request and enforces Telegram's documented rich
    message limits.
    """
    if not isinstance(rich_message, dict):
        raise _rich_error("rich_message", "InputRichMessage must be an object")
    variants = [name for name in ("blocks", "html", "markdown") if name in rich_message]
    if len(variants) != 1:
        raise _rich_error("rich_message", "exactly one of blocks, html, or markdown is required")
    variant = variants[0]
    if variant in {"html", "markdown"}:
        if not isinstance(rich_message[variant], str) or not rich_message[variant]:
            raise _rich_error(f"rich_message.{variant}", "must be a non-empty string")
        return
    blocks = rich_message["blocks"]
    if not isinstance(blocks, list):
        raise _rich_error("rich_message.blocks", "must be an array")
    state = {"text": 0, "blocks": 0, "media": 0}
    for index, block in enumerate(blocks):
        _validate_rich_block(block, f"rich_message.blocks[{index}]", state, 1)
    if state["text"] > _MAX_RICH_TEXT_CHARS:
        raise _rich_error("rich_message", f"text exceeds {_MAX_RICH_TEXT_CHARS} characters")
    if state["blocks"] > _MAX_RICH_BLOCKS:
        raise _rich_error("rich_message", f"content exceeds {_MAX_RICH_BLOCKS} counted blocks")
    if state["media"] > _MAX_RICH_MEDIA:
        raise _rich_error("rich_message", f"content exceeds {_MAX_RICH_MEDIA} media attachments")


def validate_rich_payload(payload: dict[str, Any]) -> None:
    """Validate a send/edit method payload containing ``rich_message``."""
    if not isinstance(payload, dict) or "rich_message" not in payload:
        raise _rich_error("payload", "rich_message is required")
    validate_rich_message(payload["rich_message"])


# Descriptive alias for callers/tests that use the full name.
validate_rich_message_payload = validate_rich_payload

def send_rich_message(rich_message: dict[str, Any], *, token: str, chat_id: int, timeout: int = 30) -> dict[str, Any]:
    payload = {"chat_id": chat_id, **rich_message}
    validate_rich_payload(payload)
    return call(token, "sendRichMessage", payload, timeout=timeout)


def edit_rich_message(
    rich_message: dict[str, Any],
    *,
    token: str,
    chat_id: int,
    message_id: int,
    files: dict[str, Path] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    payload = {"chat_id": chat_id, "message_id": message_id, **rich_message}
    validate_rich_payload(payload)
    if files:
        return call_multipart(token, "editMessageText", payload, files, timeout=timeout)
    return call(token, "editMessageText", payload, timeout=timeout)
