"""Оркестратор: парсит расписание, делает скрин, постит в канал.

Пайплайн:
1. fetch_html + parse_6381 → JSON (weeks, schedule)
2. screenshot_url → PNG
3. format_schedule_post → list[str] (1+ сообщений, ≤4096 каждый)
4. sendPhoto со скриншотом + caption (первое сообщение) → канал -1004256784811
5. если есть ещё сообщения (rich таблица не влезла) → sendMessage для каждого

Идемпотентность: не постить если с прошлого поста ничего не изменилось.
Состояние "последний пост" хранится в state/last_post.json:
  { hash, post_ts, message_ids: [int,...] }
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import config  # noqa: E402
from fetch import fetch_html, hash_html, is_stub  # noqa: E402
from parse import parse_all  # noqa: E402
from format import format_schedule_post, format_changes_only  # noqa: E402
from screenshot import screenshot_url  # noqa: E402


def _tg_api(method: str, **fields) -> dict[str, Any]:
    """Вызов Bot API. fields: chat_id/text/photo/parse_mode/caption/.../file (path)."""
    import urllib.request
    import urllib.parse
    import mimetypes

    url = f"https://api.telegram.org/bot{config.TG_BOT_TOKEN}/{method}"

    has_file = any(k in fields for k in ("photo", "document", "video", "audio", "voice", "sticker"))
    if has_file:
        # multipart/form-data
        import io
        boundary = "----oc" + dt.datetime.now().strftime("%H%M%S%f")
        parts: list[bytes] = []
        for k, v in fields.items():
            if v is None:
                continue
            if k in ("photo", "document", "video", "audio", "voice", "sticker") and isinstance(v, (str, Path)):
                p = Path(v)
                if not p.exists():
                    raise FileNotFoundError(p)
                mt = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
                parts.append(f"--{boundary}\r\n".encode())
                parts.append(
                    f'Content-Disposition: form-data; name="{k}"; filename="{p.name}"\r\n'.encode()
                )
                parts.append(f"Content-Type: {mt}\r\n\r\n".encode())
                parts.append(p.read_bytes())
                parts.append(b"\r\n")
            else:
                parts.append(f"--{boundary}\r\n".encode())
                parts.append(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
                parts.append(str(v).encode("utf-8"))
                parts.append(b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
    else:
        data = urllib.parse.urlencode({k: str(v) for k, v in fields.items() if v is not None}).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out = json.loads(r.read())
            if not out.get("ok"):
                print(f"[{method} FAIL] {out}", file=sys.stderr)
            return out
    except Exception as e:  # noqa: BLE001
        print(f"[{method} ERR] {e}", file=sys.stderr)
        return {"ok": False, "error": str(e)}


def _read_last_post() -> dict:
    f = config.STATE_DIR / "last_post.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _write_last_post(d: dict) -> None:
    f = config.STATE_DIR / "last_post.json"
    f.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def post_to_channel(force: bool = False) -> dict:
    """Главный entry point.

    Args:
      force: постить даже если hash не менялся (ручной прогон / первый запуск).
    """
    html = fetch_html()
    new_hash = hash_html(html)
    last = _read_last_post()

    if not force and last.get("hash") == new_hash:
        print(f"[skip] hash совпал с прошлым постом: {new_hash}")
        return {"posted": False, "reason": "same_hash", "hash": new_hash}

    data = parse_all(html)
    stub = data.get("stub", True)
    weeks = data.get("weeks", [])

    # Скриншот
    screenshot_path = config.STATE_DIR / f"6381_screenshot_{dt.date.today().isoformat()}.png"
    try:
        screenshot_url(config.GROUP_URL, screenshot_path)
    except Exception as e:  # noqa: BLE001
        print(f"[screenshot ERR] {e}", file=sys.stderr)
        screenshot_path = None  # пост без скрина

    if stub:
        msgs = [
            "ℹ️ <b>Расписание 6381</b>\n"
            "На портале пока заглушка — расписание не опубликовано.\n"
            f"<a href=\"{config.GROUP_URL}\">открыть на портале</a>"
        ]
        caption = msgs[0]
        followups: list[str] = []
    else:
        msgs = format_schedule_post(
            data.get("schedule"),
            weeks,
            config.GROUP_URL,
            changes_summary="",  # сюда позже можно добавлять дельту изменений
        )
        caption = msgs[0]
        followups = msgs[1:]

    sent_ids: list[int] = []

    if screenshot_path and screenshot_path.exists():
        result = _tg_api(
            "sendPhoto",
            chat_id=config.TG_CHANNEL_ID,
            photo=str(screenshot_path),
            caption=caption[:1024],  # лимит caption
            parse_mode="HTML",
        )
        if result.get("ok"):
            sent_ids.append(result["result"]["message_id"])
        else:
            # фоллбек — пост без скрина
            r2 = _tg_api("sendMessage", chat_id=config.TG_CHANNEL_ID, text=caption, parse_mode="HTML")
            if r2.get("ok"):
                sent_ids.append(r2["result"]["message_id"])
    else:
        r = _tg_api("sendMessage", chat_id=config.TG_CHANNEL_ID, text=caption, parse_mode="HTML")
        if r.get("ok"):
            sent_ids.append(r["result"]["message_id"])

    for m in followups:
        r = _tg_api("sendMessage", chat_id=config.TG_CHANNEL_ID, text=m, parse_mode="HTML")
        if r.get("ok"):
            sent_ids.append(r["result"]["message_id"])

    _write_last_post(
        {
            "hash": new_hash,
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "message_ids": sent_ids,
            "stub": stub,
        }
    )
    return {"posted": True, "hash": new_hash, "stub": stub, "message_ids": sent_ids}


def post_changes_alert(changes_summary: str) -> dict:
    """Короткий пост-уведомление об изменениях (без полного расписания)."""
    html = fetch_html()
    data = parse_all(html)
    text = format_changes_only(data.get("weeks", []), [{"when": changes_summary}], config.GROUP_URL)
    result = _tg_api("sendMessage", chat_id=config.TG_CHANNEL_ID, text=text, parse_mode="HTML")
    return result


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="постить даже если hash не менялся")
    ap.add_argument("--alert", help="короткий пост-уведомление об изменениях")
    args = ap.parse_args()

    if args.alert:
        r = post_changes_alert(args.alert)
        print(json.dumps(r, ensure_ascii=False))
    else:
        r = post_to_channel(force=args.force)
        print(json.dumps(r, ensure_ascii=False))
