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
Состояние последнего alert-поста хранится в state/last_post.json.
Состояние dashboard-поста хранится отдельно в state/last_dashboard_post.json:
  { hash, fingerprint, ts, message_ids: [int,...] }
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html as _html
import json
import os
import sys
from pathlib import Path
from typing import Any
import zoneinfo

import config  # noqa: E402
import bells  # noqa: E402
from fetch import content_fingerprint, fetch_html, hash_html  # noqa: E402
from parse import parse_all, parse_schedule  # noqa: E402
from format import (  # noqa: E402
    DAY_SHORT_TO_FULL,
    DAY_TO_SHORT,
    build_dashboard_rich_message,
    changed_days,
    format_changes_only,
    format_schedule_post,
    render_schedule_html,
)
from schedule_logic import (  # noqa: E402
    calendar_bounds,
    dashboard_presentation_fingerprint,
    find_week,
    lesson_has_expired,
    lesson_non_applicability_reason,
    lesson_non_applicability_struct,
    resolve_dashboard_view,
)
from screenshot import browser_session, screenshot_html, screenshot_schedule_day_crop_items  # noqa: E402
from portal_parser import day_short, norm_text, render_schedule_day_chunk_htmls  # noqa: E402
from telegram_api import edit_rich_message  # noqa: E402
import opd as opd_module  # noqa: E402

SCHEDULE_SCREEN_STYLE_VERSION = 7
COMPARISON_SCREEN_STYLE_VERSION = 2


def _dashboard_screen_status_key(target_date: dt.date) -> str:
    return f"v{SCHEDULE_SCREEN_STYLE_VERSION}:{target_date.isoformat()}"


def _today_msk() -> dt.date:
    """Сегодня по Москве: сервер живёт в UTC, а ролловер закрепа — в 00:00 Мск."""
    return dt.datetime.now(zoneinfo.ZoneInfo("Europe/Moscow")).date()


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


def _write_dashboard_post_state(data: dict) -> None:
    """Состояние последнего успешного обновления дашборда отдельно от алертов."""
    path = config.STATE_DIR / "last_dashboard_post.json"
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


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


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
            # Старые скрины заменяются новым, а не копятся на диске.
            for stale in (
                *config.STATE_DIR.glob("6381_schedule_*.png"),
                *config.STATE_DIR.glob("6381_schedule_*.html"),
            ):
                try:
                    stale.unlink()
                except OSError:
                    pass
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


def _schedule_screenshot_statuses(data: dict, target_date: dt.date) -> dict[str, list[dict]]:
    week = find_week(data.get("weeks") or [], target_date)
    if not week:
        return {}
    full_bounds = calendar_bounds(data.get("weeks") or [])
    try:
        start = dt.datetime.strptime(week["start"], "%d.%m.%Y").date()
        end = dt.datetime.strptime(week["end"], "%d.%m.%Y").date()
    except (KeyError, TypeError, ValueError):
        return {}
    day_index = {day: index for index, day in enumerate((
        "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье",
    ))}
    statuses: dict[str, list[dict]] = {}
    for day, lessons in ((data.get("schedule") or {}).get("days") or {}).items():
        if day not in day_index:
            continue
        day_date = start + dt.timedelta(days=(day_index[day] - start.weekday()) % 7)
        if day_date > end:
            continue
        items = []
        for lesson in lessons or []:
            is_dot = str(lesson.get("delivery_mode") or "").casefold().startswith("remote") or "дот" in str(lesson.get("note") or "").casefold()
            expired = lesson_has_expired(lesson, day_date, week, full_bounds)
            struct = lesson_non_applicability_struct(lesson, day_date, week, full_bounds)
            applies = struct is None
            if applies and not is_dot and not expired:
                continue
            if applies:
                status = "dot"
                tag = "ДОТ"
            else:
                kind = struct.get("kind")
                status = "inactive" if kind != "cancelled" else "cancelled"
                tag = struct.get("display_text") or struct.get("machine_reason") or "НЕ АКТУАЛЬНО"
            items.append({
                **lesson,
                "_schedule_dot": is_dot,
                "_schedule_expired": expired,
                "_schedule_status": status,
                "_schedule_tag": tag,
                "_schedule_struct": struct,
            })
        if items:
            statuses[DAY_TO_SHORT.get(day, day)] = items
    return statuses


def _dashboard_screens(
    html: str,
    fingerprint: str,
    target_date: dt.date,
    data: dict | None = None,
) -> list[dict]:
    """Cache dashboard images by content plus target-date and rendering policy."""
    cache_file = config.STATE_DIR / "dashboard_screens.json"
    shot_dir = config.STATE_DIR / "day_screens"
    shot_dir.mkdir(parents=True, exist_ok=True)

    cache = _read_json(cache_file)
    status_key = _dashboard_screen_status_key(target_date)
    cached = (
        cache.get("items")
        if cache.get("fingerprint") == fingerprint and cache.get("status_key") == status_key
        else None
    )
    if cached is not None and all(Path(item.get("path", "")).exists() for item in cached):
        return cached

    try:
        parsed = data or parse_all(html)
        raw = _cached_day_screens(
            html,
            schedule_statuses=_schedule_screenshot_statuses(parsed, target_date),
        )
    except Exception as exc:  # noqa: BLE001
        # Скриншот — украшение поста, а не его суть. Если chromium не отработал
        # (снап-песочница, нет памяти, портал отдал кривую вёрстку), закреплённый
        # пост всё равно должен получить свежий текст: берём прошлый набор картинок,
        # а если и его нет — обновляем пост вообще без медиа.
        print(f"[dashboard screens] render failed: {exc}", file=sys.stderr)
        if cached:
            print("[dashboard screens] reusing cached screenshots", file=sys.stderr)
            return cached
        _write_json_atomic(cache_file, {"fingerprint": "", "status_key": "", "items": []})
        return []
    items = [{"label": item["label"], "path": str(item["path"])} for item in raw]
    _write_json_atomic(cache_file, {"fingerprint": fingerprint, "status_key": status_key, "items": items})
    return items


def _prune_day_screen_cache(protected: set[Path], *, max_age_days: int = 14, max_bytes: int = 200_000_000) -> None:
    directory = config.STATE_DIR / "day_screens"
    if not directory.exists():
        return
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    files = sorted(directory.glob("*.png"), key=lambda path: path.stat().st_mtime, reverse=True)
    total = 0
    for path in files:
        size = path.stat().st_size
        age = now - path.stat().st_mtime
        keep = path in protected or (age <= max_age_days * 86400 and total + size <= max_bytes)
        if keep:
            total += size
            continue
        try:
            path.unlink()
        except OSError:
            pass


def _cached_day_screens(
    html: str,
    only_labels: set[str] | None = None,
    *,
    schedule_statuses: dict[str, list[dict]] | None = None,
) -> list[dict]:
    """Render clean portal days once per day-HTML hash, across full snapshots."""
    directory = config.STATE_DIR / "day_screens"
    directory.mkdir(parents=True, exist_ok=True)
    items = []
    render_kwargs = {"schedule_statuses": schedule_statuses} if schedule_statuses is not None else {}
    chunks = render_schedule_day_chunk_htmls(html, config.GROUP_URL, days_per_chunk=1, **render_kwargs)
    # Один Chrome на всю неделю: дни, которых нет в кеше, рендерятся в одном процессе.
    with browser_session():
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            label = str(chunk.get("label") or "")
            if only_labels is not None and label not in only_labels:
                continue
            rendered = str(chunk.get("html") or "")
            digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
            path = directory / f"{digest}.png"
            if not path.exists() or path.stat().st_size < 1000:
                screenshot_html(rendered, path, width=1480, height=3200, scale_factor=2, bg=(255, 255, 255))
                try:
                    path.with_suffix(".html").unlink()
                except OSError:
                    pass
            items.append({"label": label, "path": str(path), "cache_key": digest})
    _prune_day_screen_cache({Path(item["path"]) for item in items})
    return items


def _comparison_marks(schedule: dict, diff: dict, *, before: bool) -> list[dict]:
    """Resolve each diff against its own snapshot, then use exact source rows."""
    fields = {"предмет": "subject", "ауд.": "room", "преподаватель": "teacher",
              "примечание": "note", "место": "location", "формат": "delivery_mode"}
    portal_columns = {"примечание": "комм.", "место": "комм.", "формат": "комм."}
    lessons = [{**lesson, "day": day} for day, rows in schedule.get("days", {}).items() for lesson in rows]
    marks = []
    used = set()
    kind = "removed" if before else "added"
    items = [(kind, item) for item in (diff.get(kind) or [])]
    items += [("changed", item) for item in (diff.get("changed") or [])]
    for kind, item in items:
        target = dict(item)
        columns = []
        for label, old, new in item.get("fields", []):
            if label in fields:
                target[fields[label]] = old if before else new
                columns.append(portal_columns.get(label, label))
        candidates = []
        for lesson in lessons:
            if lesson["source_row"] in used or day_short(lesson["day"]) != day_short(target.get("day")):
                continue
            if norm_text(lesson["subject"].split("\n", 1)[0]) != norm_text(str(target.get("subject") or "").split("\n", 1)[0]):
                continue
            if bells.parse_hours(lesson["time"]) != bells.parse_hours(target.get("time")):
                continue
            if any(
                key in target and target[key] is not None and norm_text(lesson.get(key)) != norm_text(target[key])
                for key in ("subgroup", "room", "note", "location", "delivery_mode")
            ):
                continue
            if target.get("teacher") is not None:
                teacher, expected = norm_text(lesson.get("teacher")), norm_text(target["teacher"])
                # The notification expands bare surnames to full names; the
                # screenshot still contains the original portal spelling.
                if teacher != expected and not (teacher and " " not in teacher and expected.startswith(teacher + " ")):
                    continue
            candidates.append(lesson)
        exact = [lesson for lesson in candidates if lesson["source_row"] == target.get("source_row")]
        candidates = exact or candidates
        # Do not guess between indistinguishable parallel lessons.
        if len(candidates) != 1:
            continue
        source_row = candidates[0]["source_row"]
        used.add(source_row)
        marks.append({"source_row": source_row, "kind": kind, "columns": sorted(set(columns))})
    return marks


def comparison_day_screens(old_html: str | None, new_html: str | None, diff: dict) -> list[dict]:
    """Pair annotated snapshots with fixed columns and equal image canvases."""
    from PIL import Image

    wanted = {DAY_TO_SHORT.get(day, day) for day in changed_days(diff)}
    sides = []
    for html, before, title in ((old_html, True, "Было"), (new_html, False, "Стало")):
        schedule = parse_schedule(html) if html else None
        chunks = render_schedule_day_chunk_htmls(
            html, config.GROUP_URL, comparison_title=title,
            comparison_marks=_comparison_marks(schedule, diff, before=before),
        ) if schedule is not None else []
        sides.append({chunk["label"]: chunk for chunk in chunks if chunk["label"] in wanted})
    directory = config.STATE_DIR / "day_screens"
    directory.mkdir(parents=True, exist_ok=True)
    order = {day: index for index, day in enumerate(("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"))}
    items = []
    protected = set()
    # Обе стороны сравнения (Было / Стало) рендерятся одним процессом Chrome.
    with browser_session():
        for label in sorted(wanted, key=lambda item: order.get(item, 9)):
            pair = [side.get(label) for side in sides]
            if not any(pair):
                continue
            signature = json.dumps([COMPARISON_SCREEN_STYLE_VERSION, 1480, 3200, 2,
                                    [chunk["html"] if chunk else None for chunk in pair]], ensure_ascii=False)
            digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()
            item = {"label": label}
            paths = []
            for side, chunk in zip(("before", "after"), pair):
                item[f"{side}_path"] = None
                item[f"{side}_kinds"] = chunk.get("comparison_kinds", []) if chunk else []
                if chunk is None:
                    continue
                path = directory / f"{digest}_{side}.png"
                try:
                    with Image.open(path) as image:
                        image.verify()
                    cached = True
                except (OSError, ValueError, SyntaxError):
                    cached = False
                if not cached:
                    staging = path.with_name(path.stem + ".render.png")
                    try:
                        staging.unlink(missing_ok=True)
                        screenshot_html(chunk["html"], staging, width=1480, height=3200, scale_factor=2, bg=(255, 255, 255))
                        with Image.open(staging) as image:
                            image.verify()
                        staging.replace(path)
                    finally:
                        staging.unlink(missing_ok=True)
                        staging.with_suffix(".html").unlink(missing_ok=True)
                item[f"{side}_path"] = str(path)
                paths.append(path)
            if len(paths) == 2:
                sizes = []
                for path in paths:
                    with Image.open(path) as image:
                        sizes.append(image.size)
                canvas_size = (max(size[0] for size in sizes), max(size[1] for size in sizes))
                for path, size in zip(paths, sizes):
                    if size != canvas_size:
                        with Image.open(path) as image:
                            canvas = Image.new("RGB", canvas_size, "white")
                            canvas.paste(image, (0, 0))
                        staging = path.with_name(path.stem + ".padding.png")
                        try:
                            canvas.save(staging)
                            staging.replace(path)
                        finally:
                            staging.unlink(missing_ok=True)
            protected.update(paths)
            items.append(item)
        _prune_day_screen_cache(protected)
    return items


def day_screens(html: str, fingerprint: str, target_date: dt.date) -> list[dict]:
    """Публичный доступ к дневным скринам расписания (кеш по отпечатку).

    Монитор зовёт её перед дифф-постом, чтобы приложить скриншоты изменившихся
    дней, а потом тот же кеш использует обновление закреплённого поста:
    chromium на одно изменение расписания запускается один раз.
    """
    return _dashboard_screens(html, fingerprint, target_date)


DIFF_SCREEN_LEGEND = (
    "ЗЕЛЁНАЯ строка с меткой «НОВАЯ ПАРА» — пары раньше не было. "
    "ОРАНЖЕВАЯ строка с меткой «ИЗМЕНИЛИ» — пара была, но её поправили; "
    "яркая ячейка внутри — то самое поле, которое поменяли. "
    "Убранных пар на скрине уже нет."
)


def _diff_signature(diff: dict) -> str:
    """Подпись диффа: подсветка зависит от конкретного изменения, а не только от отпечатка."""
    payload = json.dumps(
        {key: diff.get(key) or [] for key in ("added", "removed", "changed")},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def diff_day_screens(
    html: str,
    diff: dict,
    target_date: dt.date,
    *,
    fingerprint: str = "",
) -> list[dict]:
    """Скрины изменившихся дней с машинной подсветкой правок.

    Подсветка ставится в саму портальную таблицу (классы diff-row/diff-cell),
    поэтому скрин остаётся оригиналом с портала, а правки видно глазом. Кеш
    отдельный — state/diff_screens.json по отпечатку расписания и подписи
    диффа: чистые скрины закреплённого поста для этого не годятся, а
    перерисовывать свои на каждый ретрай незачем. Ошибки летят наружу:
    монитор сам решает, падать на чистые скрины или на текст.
    """
    shot_dir = config.STATE_DIR / "dashboard_screens"
    shot_dir.mkdir(parents=True, exist_ok=True)
    cache_file = config.STATE_DIR / "diff_screens.json"
    signature = _diff_signature(diff)
    cache = _read_json(cache_file)
    cached = (
        cache.get("items")
        if cache.get("fingerprint") == fingerprint and cache.get("signature") == signature
        else None
    )
    if cached and all(Path(str(item.get("path", ""))).exists() for item in cached):
        return cached

    wanted = {DAY_TO_SHORT.get(day, day) for day in changed_days(diff)}
    # Изменённые раньше добавленных: при равном счёте строку забирает запись
    # с полями, а значит подсветится не только строка, но и конкретная ячейка.
    # _diff_kind нужен подсветке, чтобы новая пара и правка существующей
    # отличались цветом и подписью, а не сливались в один жёлтый.
    marks = [
        *({**item, "_diff_kind": "changed"} for item in (diff.get("changed") or [])),
        *({**item, "_diff_kind": "added"} for item in (diff.get("added") or [])),
    ]
    base = shot_dir / f"6381_diff_{target_date.isoformat()}_{signature[:8]}.png"
    raw = screenshot_schedule_day_crop_items(
        html,
        config.GROUP_URL,
        base,
        diff_items=marks,
        diff_legend=DIFF_SCREEN_LEGEND,
        only_labels=wanted,
        diff_removed_note=bool(diff.get("removed")),
    )
    keep = {str(item["path"]) for item in raw}
    keep |= {str(Path(str(item["path"])).with_suffix(".html")) for item in raw}
    for stale in (*shot_dir.glob("6381_diff_*.png"), *shot_dir.glob("6381_diff_*.html")):
        if str(stale) not in keep:
            try:
                stale.unlink()
            except OSError:
                pass
    items = [
        {
            "label": item["label"],
            "path": str(item["path"]),
            "marked": int(item.get("marked") or 0),
            # Какие правки легли на этот скрин: подпись в посте обещает только
            # реально помеченные цвета.
            **({"marked_kinds": list(item["marked_kinds"])} if item.get("marked_kinds") else {}),
        }
        for item in raw
    ]
    _write_json_atomic(cache_file, {"fingerprint": fingerprint, "signature": signature, "items": items})
    return items


def _view_dates(view: dict) -> list[dt.date]:
    """Даты учебной недели закрепа (для отпечатка раздела ОПД)."""
    week = view.get("week") or {}
    try:
        start = dt.datetime.strptime(str(week["start"]), "%d.%m.%Y").date()
        end = dt.datetime.strptime(str(week["end"]), "%d.%m.%Y").date()
    except (KeyError, TypeError, ValueError):
        target = view.get("target_date")
        return [target] if isinstance(target, dt.date) else []
    if end < start:
        return [start]
    return [start + dt.timedelta(days=offset) for offset in range((end - start).days + 1)]


def edit_dashboard_post(
    message_id: int,
    target_date: dt.date | None = None,
    html: str | None = None,
    fingerprint: str | None = None,
    data: dict | None = None,
    now: dt.datetime | None = None,
    render_screens: bool = True,
) -> dict:
    """Обновить закреплённый rich-пост. html — уже скачанная страница
    (монитор передаёт её, чтобы не фетчить дважды за цикл). fingerprint —
    отпечаток распарсенного расписания (content_fingerprint); если он совпал
    с прошлым рендером, скриншоты берутся из кеша и chromium не гоняется —
    скрины обновляются только при реальном изменении расписания."""
    sleepy = False
    if data is None:
        if html is None:
            try:
                html = fetch_html()
            except Exception as exc:  # noqa: BLE001 - ролловер не должен
                #  умирать вместе с порталом: если порталь лёг ровно
                # в полночь, закреп всё равно переключаем по сохранённой
                # копии, а в заголовке честно пишем, что данные из кеша.
                cached = config.STATE_DIR / "6381.html"
                if not cached.exists():
                    raise
                html = cached.read_text(encoding="utf-8")
                sleepy = True
                print(f"[dashboard] fetch failed, cached html used: {exc}", file=sys.stderr)
        data = parse_all(html)
    if html is None:
        raise ValueError("html is required for dashboard screenshots")
    fp = fingerprint or content_fingerprint(data)
    schedule = data.get("schedule")
    weeks = data.get("weeks", [])
    tz_msk = zoneinfo.ZoneInfo("Europe/Moscow")
    if now is None:
        clock_now = dt.datetime.now(tz_msk)
        now_msk = dt.datetime.combine(_today_msk(), clock_now.timetz())
    elif now.tzinfo is None:
        now_msk = now.replace(tzinfo=tz_msk)
    else:
        now_msk = now.astimezone(tz_msk)
    # ОПД по виртуальным группам: кэш state/opd_cache.json, сеть — по TTL.
    # Без данных закреп рисуется как раньше; блок 16:00 продлевает «живой» день.
    opd_data = opd_module.load_opd(now=now_msk)
    late_ends = opd_module.late_ends(opd_data)
    view = resolve_dashboard_view(schedule, weeks, now_msk, late_ends=late_ends)
    if target_date is not None and target_date != now_msk.date():
        manual_now = dt.datetime.combine(target_date, dt.time.min, tzinfo=tz_msk)
        view = resolve_dashboard_view(schedule, weeks, manual_now, late_ends=late_ends)
    target_date = view["target_date"]
    current_date = now_msk.date()
    live_days = set(view["live_days"])
    presentation_fp = dashboard_presentation_fingerprint(
        fp, view,
        extra=opd_module.presentation_digest(opd_data, _view_dates(view))
        + ("|sleepy" if sleepy else ""),
    )
    previous_dashboard = _read_json(config.STATE_DIR / "last_dashboard_post.json")
    expects_screens = bool(schedule and live_days)
    screen_cache = _read_json(config.STATE_DIR / "dashboard_screens.json")
    screen_status_key = _dashboard_screen_status_key(target_date)
    cached_items = (
        screen_cache.get("items")
        if screen_cache.get("fingerprint") == fp
        and screen_cache.get("status_key") == screen_status_key
        else None
    )
    media_cache_valid = cached_items is not None and all(
        Path(str(item.get("path") or "")).exists() for item in cached_items
    )
    media_delivery_complete = bool(previous_dashboard.get("screens_complete")) and media_cache_valid
    if (
        previous_dashboard.get("message_id") == message_id
        and previous_dashboard.get("presentation_fingerprint") == presentation_fp
        and (media_delivery_complete or not expects_screens or not render_screens)
    ):
        return {"ok": True, "skipped": True, "reason": "same_presentation"}

    def _day_still_live(item: dict) -> bool:
        label = str(item.get("label") or "")
        day_name = DAY_SHORT_TO_FULL.get(label)
        # Подпись без известного дня («с сайта») не фильтруем: иначе пост
        # остался бы совсем без картинок.
        return day_name is None or day_name in live_days

    shot_items = (
        [item for item in _dashboard_screens(html, fp, target_date, data=data) if _day_still_live(item)]
        if render_screens and expects_screens
        else []
    )
    screenshot_media = [
        {"label": item["label"], "media": f"attach://site_screenshot_{index}"}
        for index, item in enumerate(shot_items, 1)
    ]
    # «Обновлено» в закрепе — это время фактического редактирования поста,
    # а не время изменения расписания на портале. При ролловере (новая
    # неделя, новые дни) пост обновляется, и дата в заголовке должна это
    # отражать. Время последнего изменения самого расписания показываем
    # отдельно, если оно отличается от времени поста.
    last_updated = now_msk.strftime("%d.%m.%Y %H:%M")
    schedule_changed = ""
    monitor_state_path = config.STATE_DIR / "monitor_state.json"
    if monitor_state_path.exists():
        try:
            monitor_data = json.loads(monitor_state_path.read_text(encoding="utf-8"))
            # event — последний опрос; last_change_event — последнее
            # семантическое изменение расписания. Старые state-файлы
            # совместимы: при отсутствии нового поля используем event.
            event = monitor_data.get("last_change_event") or monitor_data.get("event", {})
            ts = event.get("ts", "")
            if ts:
                try:
                    dt_obj = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    dt_obj = dt_obj.astimezone(zoneinfo.ZoneInfo("Europe/Moscow"))
                    schedule_changed = dt_obj.strftime("%d.%m.%Y %H:%M")
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
        current_date=current_date,
        now=now_msk,
        last_updated=last_updated,
        schedule_changed=schedule_changed,
        opd=opd_data,
        sleep_note="😴 Сайт в спячке — сохранённое расписание" if sleepy else "",
    )
    files = {f"site_screenshot_{index}": item["path"] for index, item in enumerate(shot_items, 1)}
    result = edit_rich_message(
        rich,
        token=config.TG_BOT_TOKEN,
        chat_id=config.TG_CHANNEL_ID,
        message_id=message_id,
        files=files,
        timeout=120,
    )
    if result.get("ok"):
        _write_dashboard_post_state({
            "message_id": message_id,
            "fingerprint": fp,
            "presentation_fingerprint": presentation_fp,
            "target_date": target_date.isoformat(),
            "screens_complete": render_screens or not expects_screens,
            "sleepy": sleepy,
            "ts": dt.datetime.now(tz_msk).isoformat(timespec="seconds"),
        })
    return result


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="постить даже если hash не менялся")
    ap.add_argument("--alert", help="короткий пост-уведомление об изменениях")
    ap.add_argument("--edit-rich", type=int, metavar="MESSAGE_ID", help="отредактировать существующий пост dashboard-rich форматом")
    ap.add_argument("--date", default="2026-09-02", help="дата фокуса для --edit-rich, YYYY-MM-DD")
    _env_roll = os.environ.get("NOVSU_DASHBOARD_POST_ID", "").strip()
    _default_roll = int(_env_roll) if _env_roll and _env_roll not in ("0", "None") else None
    ap.add_argument(
        "--roll",
        nargs="?",
        const=_default_roll,
        default=None,
        type=int,
        metavar="MESSAGE_ID",
        help="ролловер закрепа на сегодняшнюю дату Мск: прошедшие дни недели исчезают "
             "(в 00:00 Мск в посте остаются только будущие дни, в воскресенье — уже следующая неделя). "
             "Без значения берёт NOVSU_DASHBOARD_POST_ID из окружения",
    )
    args = ap.parse_args()

    # nargs="?" даёт None и когда флага нет, и когда он без значения, —
    # поэтому смотрим в argv: ролловер без id закрепа делать некуда.
    if "--roll" in sys.argv:
        if not args.roll:
            print("нужен MESSAGE_ID: --roll 3 или NOVSU_DASHBOARD_POST_ID в окружении")
            raise SystemExit(2)
        today_msk = _today_msk()
        r = edit_dashboard_post(args.roll, today_msk)
        print(json.dumps({"rolled_to": today_msk.isoformat(), **r}, ensure_ascii=False))
    elif args.edit_rich:
        target_date = dt.datetime.strptime(args.date, "%Y-%m-%d").date()
        r = edit_dashboard_post(args.edit_rich, target_date)
        print(json.dumps(r, ensure_ascii=False))
    elif args.alert:
        r = post_changes_alert(args.alert)
        print(json.dumps(r, ensure_ascii=False))
    else:
        r = post_to_channel(force=args.force)
        print(json.dumps(r, ensure_ascii=False))
