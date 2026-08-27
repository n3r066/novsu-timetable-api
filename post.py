"""Оркестратор: парсит расписание и постит в канал.

Пайплайн:
1. fetch_html + parse_all → JSON (weeks, schedule)
2. render_schedule_html → чистая HTML-таблица (предметы сгруппированы
   по чётности недели: верхняя / нижняя / каждую)
3. screenshot_html → PNG через headless chromium
4. sendPhoto в канал + раскрывающаяся подпись (<blockquote expandable>)
   с легендой по неделям и ссылкой на портал
5. фоллбек на format_schedule_post (текст ≤4096), если скрин не получился
6. заглушка портала → stub-сообщение через sendMessage

Идемпотентность: не постить если с прошлого поста ничего не изменилось.
Состояние "последний пост" хранится в state/last_post.json:
  { hash, fingerprint, ts, message_ids: [int,...] }
"""

from __future__ import annotations

import datetime as dt
import html as _html
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import zoneinfo

import config  # noqa: E402
from fetch import content_fingerprint, fetch_html, hash_html  # noqa: E402
from parse import parse_all  # noqa: E402
from format import build_dashboard_rich_message, format_changes_only, format_schedule_post, render_schedule_html  # noqa: E402
from screenshot import screenshot_html, screenshot_schedule_day_crop_items  # noqa: E402
from telegram_api import edit_rich_message  # noqa: E402


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


def _write_last_post(data: dict) -> None:
    path = config.STATE_DIR / "last_post.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _semantic_fingerprint(data: dict) -> str:
    """Compatibility alias for the single canonical fingerprint implementation."""
    return content_fingerprint(data)


def _photo_caption(source_url: str) -> str:
    """Build a complete HTML caption; never cut markup at 1024 chars."""
    escaped_url = _html.escape(str(source_url), quote=True)
    link = f'<a href="{escaped_url}">открыть на портале</a>'
    # An unexpectedly huge configured URL is safer to omit than to slice in
    # the middle of an attribute or entity.
    if len(link) > 400:
        link = "открыть на портале НовГУ"
    caption = (
        "<b>Расписание группы 6381</b>\n"
        "<blockquote expandable>"
        "ВЕРХНЯЯ — 1, 3, 5… нечётные учебные недели\n"
        "НИЖНЯЯ — 2, 4, 6… чётные учебные недели\n"
        "Недели считаются по учебному календарю НовГУ.\n"
        "* у предмета — условие раскрыто под таблицей.\n"
        f"{link}"
        "</blockquote>"
    )
    if len(caption) > 1024:
        raise ValueError("photo caption exceeds Telegram's 1024-character limit")
    return caption


def _message_id(response: dict) -> int | None:
    result = response.get("result") if isinstance(response, dict) else None
    message_id = result.get("message_id") if isinstance(result, dict) else None
    return message_id if isinstance(message_id, int) else None


def post_to_channel(force: bool = False) -> dict:
    """Post the current schedule once per parsed semantic fingerprint."""
    html = fetch_html()
    raw_hash = hash_html(html)
    data = parse_all(html)
    fingerprint = _semantic_fingerprint(data)
    last = _read_last_post()

    same_semantics = last.get("fingerprint") == fingerprint
    legacy_exact_match = "fingerprint" not in last and last.get("hash") == raw_hash
    if not force and (same_semantics or legacy_exact_match):
        print(f"[skip] semantic fingerprint совпал с прошлым постом: {fingerprint}")
        return {
            "posted": False,
            "complete": True,
            "partial": False,
            "reason": "same_fingerprint",
            "hash": raw_hash,
            "fingerprint": fingerprint,
        }

    stub = data.get("stub", True)
    weeks = data.get("weeks", [])
    sent_ids: list[int] = []
    failures: list[dict[str, Any]] = []
    complete = False
    delivery = "stub" if stub else "fallback"

    if stub:
        text = (
            "<b>Расписание 6381</b>\n"
            "На портале пока заглушка — расписание не опубликовано.\n"
            f'<a href="{_html.escape(str(config.GROUP_URL), quote=True)}">открыть на портале</a>'
        )
        response = _tg_api("sendMessage", chat_id=config.TG_CHANNEL_ID, text=text, parse_mode="HTML")
        message_id = _message_id(response)
        if response.get("ok") and message_id is not None:
            sent_ids.append(message_id)
            complete = True
        else:
            failures.append({"stage": "stub_message", "response": response})
    else:
        sent_via_photo = False
        try:
            rendered = render_schedule_html(
                data.get("schedule"), weeks, config.GROUP_URL, group_name="6381",
            )
            today_msk = dt.datetime.now(zoneinfo.ZoneInfo("Europe/Moscow")).date()
            shot = config.STATE_DIR / f"6381_schedule_{today_msk.isoformat()}.png"
            screenshot_html(rendered, shot)
            result = _tg_api(
                "sendPhoto",
                chat_id=config.TG_CHANNEL_ID,
                photo=str(shot),
                caption=_photo_caption(config.GROUP_URL),
                parse_mode="HTML",
            )
            message_id = _message_id(result)
            if result.get("ok") and message_id is not None:
                sent_ids.append(message_id)
                sent_via_photo = True
                complete = True
                delivery = "photo"
            else:
                print(f"[sendPhoto FAIL] {result}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"[screenshot ERR] {exc}", file=sys.stderr)

        if not sent_via_photo:
            # The failed photo is only an alternative transport.  A complete
            # text fallback still counts as a complete post.
            try:
                messages = format_schedule_post(
                    data.get("schedule"), weeks, config.GROUP_URL, group_name="6381",
                )
            except Exception as exc:  # noqa: BLE001
                messages = []
                failures.append({"stage": "format_fallback", "error": str(exc)})
            fallback_successes = 0
            for index, message in enumerate(messages):
                response = _tg_api(
                    "sendMessage",
                    chat_id=config.TG_CHANNEL_ID,
                    text=message,
                    parse_mode="HTML",
                )
                message_id = _message_id(response)
                if response.get("ok") and message_id is not None:
                    sent_ids.append(message_id)
                    fallback_successes += 1
                else:
                    failures.append({
                        "stage": "fallback_message",
                        "index": index,
                        "response": response,
                    })
            complete = bool(messages) and fallback_successes == len(messages)

    posted = bool(sent_ids)
    partial = posted and not complete
    result = {
        "posted": posted,
        "complete": complete,
        "partial": partial,
        "reason": "posted" if complete else ("partial_failure" if partial else "send_failed"),
        "hash": raw_hash,
        "fingerprint": fingerprint,
        "stub": stub,
        "message_ids": sent_ids,
    }
    if failures:
        result["failures"] = failures

    # Only a complete delivery is safe to deduplicate.  Full and partial
    # failures remain retryable and must not advance last_post.json.
    if complete:
        now_msk = dt.datetime.now(zoneinfo.ZoneInfo("Europe/Moscow"))
        _write_last_post({
            "hash": raw_hash,
            "fingerprint": fingerprint,
            "ts": now_msk.isoformat(timespec="seconds"),
            "timezone": "Europe/Moscow",
            "message_ids": sent_ids,
            "stub": stub,
            "delivery": delivery,
        })
    return result


def post_changes_alert(changes_summary: str, data: dict | None = None) -> dict:
    """Короткий пост-уведомление об изменениях (без полного расписания).

    data — уже распарсенный parse_all(); если не передан, фетчим сами.
    Детальные дифф-посты делает monitor._post_changes_to_channel (rich).
    """
    if data is None:
        data = parse_all(fetch_html())
    text = format_changes_only(data.get("weeks", []), [{"when": changes_summary}], config.GROUP_URL)
    result = _tg_api("sendMessage", chat_id=config.TG_CHANNEL_ID, text=text, parse_mode="HTML")
    return result


def edit_dashboard_post(message_id: int, target_date: dt.date, html: str | None = None) -> dict:
    """Обновить закреплённый rich-пост. html — уже скачанная страница
    (монитор передаёт её, чтобы не фетчить дважды за цикл)."""
    if html is None:
        html = fetch_html()
    data = parse_all(html)
    with TemporaryDirectory(prefix=".tmp-novsu-tt-", dir=config.STATE_DIR) as tmpdir:
        shot = Path(tmpdir) / f"6381_site_{target_date.isoformat()}.png"
        shot_items = screenshot_schedule_day_crop_items(html, config.GROUP_URL, shot)
        screenshot_media = [
            {"label": item["label"], "media": f"attach://site_screenshot_{index}"}
            for index, item in enumerate(shot_items, 1)
        ]
        # Читаем дату последнего изменения из monitor_state.json
        monitor_state_path = config.STATE_DIR / "monitor_state.json"
        last_updated = ""
        if monitor_state_path.exists():
            try:
                monitor_data = json.loads(monitor_state_path.read_text(encoding="utf-8"))
                event = monitor_data.get("event", {})
                ts = event.get("ts", "")
                if ts:
                    # Формат: 2026-08-27T15:45:25+00:00 -> 27.08.2026 15:45
                    try:
                        dt_obj = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        dt_obj = dt_obj.astimezone(zoneinfo.ZoneInfo("Europe/Moscow"))
                        last_updated = dt_obj.strftime("%d.%m.%Y %H:%M")
                    except ValueError:
                        pass
            except Exception:
                pass

        rich = build_dashboard_rich_message(
            data.get("schedule"),
            data.get("weeks", []),
            config.GROUP_URL,
            target_date,
            group_name="6381",
            screenshot_media=screenshot_media,
            current_date=dt.datetime.now(zoneinfo.ZoneInfo("Europe/Moscow")).date(),
            last_updated=last_updated,
        )
        files = {f"site_screenshot_{index}": item["path"] for index, item in enumerate(shot_items, 1)}
        return edit_rich_message(
            rich,
            token=config.TG_BOT_TOKEN,
            chat_id=config.TG_CHANNEL_ID,
            message_id=message_id,
            files=files,
            timeout=120,
        )


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="постить даже если hash не менялся")
    ap.add_argument("--alert", help="короткий пост-уведомление об изменениях")
    ap.add_argument("--edit-rich", type=int, metavar="MESSAGE_ID", help="отредактировать существующий пост dashboard-rich форматом")
    ap.add_argument("--date", default="2026-09-02", help="дата фокуса для --edit-rich, YYYY-MM-DD")
    args = ap.parse_args()

    if args.edit_rich:
        target_date = dt.datetime.strptime(args.date, "%Y-%m-%d").date()
        r = edit_dashboard_post(args.edit_rich, target_date)
        print(json.dumps(r, ensure_ascii=False))
    elif args.alert:
        r = post_changes_alert(args.alert)
        print(json.dumps(r, ensure_ascii=False))
    else:
        r = post_to_channel(force=args.force)
        print(json.dumps(r, ensure_ascii=False))
