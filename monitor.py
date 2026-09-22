"""Мониторинг изменений страницы группы 6381.

Логика одного прогона:
1. fetch_html (curl, ретраи, случайный Chromium UA)
2. content_fingerprint — отпечаток распарсенного содержимого
   (иммунитет к волатильным таймстемпам .xls-ссылок портала)
3. сравнить с атомарным baseline из state/monitor_state.json
4. если изменилось:
   - записать наблюдение в state/changes.jsonl (журнал опросов)
   - построить дифф (добавлено/убрано/изменено, stub-переходы)
   - запостить rich-сообщение в КАНАЛ (фоллбек — plain HTML)
   - опционально обновить закреплённый rich-пост с расписанием
5. обновить baseline только после успешной доставки

Личка (_dm) — ТОЛЬКО технические алерты (фетч сломан, постинг сломан),
никакого содержимого расписания.

Запуск:
  python3 monitor.py                 # один прогон
  python3 monitor.py --interval 30   # вечный цикл: каждые 30 ± 3 мин
  python3 monitor.py --schedule      # раз в день в 09:00 Europe/Moscow (для cron)
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import html as html_lib
import json
import os
import random
import re
import sys
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
import zoneinfo

import bells  # noqa: E402
import api as timetable_api  # noqa: E402
import config  # noqa: E402
import extra_posts  # noqa: E402
import opd as opd_module  # noqa: E402
from fetch import content_fingerprint, fetch_html, hash_html, is_stub, save_html  # noqa: E402
from format import build_changes_rich_message, changes_fallback_text, pick_day_screens  # noqa: E402
from parse import parse_all  # noqa: E402
from post import _tg_api, comparison_day_screens, edit_dashboard_post  # noqa: E402
from telegram_api import edit_rich_message, send_rich_message  # noqa: E402

# Сколько подряд неудачных циклов терпим молча перед tech-алертом в личку
FAIL_ALERT_THRESHOLD = 3


def _dm(text: str) -> None:
    """Короткое сообщение Георгию в личку.

    Только аварии реальной работы монитора: упавшая правка закрепа, пустой
    дифф при новом отпечатке, мёртвый фетч, упавший цикл. Ручные прогоны при
    разработке глушим переменной NOVSU_DM_SILENT=1, иначе отладка засоряет
    личку алертами, которых в бою не было.
    """
    import urllib.request
    import urllib.parse

    if os.environ.get("NOVSU_DM_SILENT", "").strip() not in ("", "0", "false", "False"):
        print(f"[dm silent] {text[:200]}", file=sys.stderr)
        return

    url = f"https://api.telegram.org/bot{config.TG_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": str(config.TG_DM_TARGET),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read())
            if not body.get("ok"):
                print(f"[dm FAIL] {body}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[dm ERR] {e}", file=sys.stderr)


def _change_time_msk(event: dict) -> dt.datetime:
    """Время обнаружения события как московское время (для штампа в посте)."""
    raw = event.get("ts") if isinstance(event, dict) else None
    if isinstance(raw, str) and raw:
        try:
            value = dt.datetime.fromisoformat(raw)
            if value.tzinfo is None:
                value = value.replace(tzinfo=dt.timezone.utc)
            return value.astimezone(zoneinfo.ZoneInfo("Europe/Moscow"))
        except ValueError:
            pass
    return dt.datetime.now(zoneinfo.ZoneInfo("Europe/Moscow"))


def _subject(lesson: dict) -> str:
    return str(lesson.get("subject") or lesson.get("subject_raw") or "").split("\n", 1)[0].strip()


def _subgroup(lesson: dict) -> str:
    value = str(lesson.get("subgroup") or "").strip()
    if value:
        return value
    match = re.search(r"подгруппа:\s*([^·,;]+)", str(lesson.get("note") or ""), re.I)
    return match.group(1).strip() if match else ""


def _lesson_key(day: str, lesson: dict) -> tuple:
    """Stable identity for matching changed records, including subgroup."""
    return (day, str(lesson.get("time") or ""), _subject(lesson), _subgroup(lesson))


def _group_lessons(schedule: dict | None) -> dict[tuple, list[dict]]:
    out: dict[tuple, list[dict]] = defaultdict(list)
    for day, rows in ((schedule or {}).get("days") or {}).items():
        for lesson in rows:
            out[_lesson_key(day, lesson)].append({**lesson, "day": day})
    return out


# Fields shown as an in-place change. A different subject/time is deliberately
# represented as remove+add because it changes the lesson identity.
_DIFF_FIELDS = [
    ("room", "ауд."),
    ("teacher", "преподаватель"),
    ("note", "примечание"),
    ("location", "место"),
    ("delivery_mode", "формат"),
]


def _change_signature(lesson: dict) -> tuple:
    return tuple(str(lesson.get(field) or "").strip() for field, _ in _DIFF_FIELDS)


#: Поля пары, которые пост показывает как контекст правки (место, кто ведёт).
_CONTEXT_FIELDS = ("room", "teacher", "location", "note", "delivery_mode")


def _change_item(item: dict, fields: list[tuple[str, str, str]]) -> dict:
    """Запись «изменили» вместе с контекстом пары.

    Раньше в записи были только день, время, предмет и список полей, поэтому
    пост не мог показать, где именно стоит правленая пара и кто её ведёт, —
    приходилось печатать «ауд. —» вместо настоящего места.
    """
    record = {
        "day": item.get("day"),
        "time": item.get("time"),
        "subject": item.get("subject"),
        "subgroup": _subgroup(item),
        "fields": fields,
    }
    for field in _CONTEXT_FIELDS:
        record[field] = item.get(field)
    return record


def _slot_key(lesson: dict) -> tuple:
    """Место пары в сетке без предмета: день, академические часы, подгруппа, препод.

    Часы берём через ``bells.parse_hours``, а не по сырой строке «9:00 10:00»:
    портал может перепечатать то же время иначе, и это не делает пару другой.
    """
    return (
        str(lesson.get("day") or "").strip(),
        tuple(bells.parse_hours(lesson.get("time"))),
        _subgroup(lesson),
        str(lesson.get("teacher") or "").strip().casefold(),
    )


def _rename_score(gone: dict, arrived: dict) -> int:
    """Сколько полей пары совпало: чем больше, тем ближе запись к переименованию."""
    return sum(
        1
        for field, _ in _DIFF_FIELDS
        if str(gone.get(field) or "").strip() == str(arrived.get(field) or "").strip()
    )


def _rename_fields(gone: dict, arrived: dict) -> list[tuple[str, str, str]]:
    """Что именно поменялось в паре, которая осталась на своём месте."""
    fields: list[tuple[str, str, str]] = []
    old_subject, new_subject = _subject(gone), _subject(arrived)
    if old_subject != new_subject:
        fields.append(("предмет", old_subject, new_subject))
    for field, label in _DIFF_FIELDS:
        old_value = str(gone.get(field) or "").strip()
        new_value = str(arrived.get(field) or "").strip()
        if old_value != new_value:
            fields.append((label, old_value, new_value))
    return fields


def _match_renames(
    removed: list[dict], added: list[dict]
) -> tuple[list[dict], list[dict], list[dict]]:
    """Склеить «убрали» + «добавили» на том же месте сетки в одну правку.

    Портал не умеет сообщать о переименовании: он печатает новое название в
    той же строке, и для наивного диффа это выглядит как исчезнувшая пара и
    появившаяся пара. Читатель видит «добавили 2, убрали 2» там, где ровно
    одна пара осталась на месте и просто сменила название. Поэтому записи с
    одинаковым местом (день, часы, подгруппа, преподаватель) сводим в одну
    правку «изменили», а настоящие добавления и убирания оставляем как есть.
    """
    renamed: list[dict] = []
    free_added = list(added)
    rest_removed: list[dict] = []
    for gone in removed:
        key = _slot_key(gone)
        if not key[1]:
            rest_removed.append(gone)
            continue
        candidates = [
            (index, candidate)
            for index, candidate in enumerate(free_added)
            if _slot_key(candidate) == key
        ]
        if not candidates:
            rest_removed.append(gone)
            continue
        index, arrived = max(candidates, key=lambda pair: _rename_score(gone, pair[1]))
        fields = _rename_fields(gone, arrived)
        if not fields:
            rest_removed.append(gone)
            continue
        free_added.pop(index)
        renamed.append(_change_item(arrived, fields))
    return renamed, rest_removed, free_added


def diff_schedules(
    old: dict | None,
    new: dict | None,
    *,
    old_stub: bool | None = None,
    new_stub: bool | None = None,
    teacher_lookup: dict[str, str] | None = None,
) -> dict:
    """Compare schedules without collapsing simultaneous subgroup lessons.

    When *teacher_lookup* is provided (or built from both schedules), bare
    last names in teacher fields are enriched to full names so that the
    "Изменено" section shows "Барышева Ангелина Алексеевна → Иванова …"
    instead of just "Барышева → Иванова".
    """
    old_groups = _group_lessons(old)
    new_groups = _group_lessons(new)
    added: list[dict] = []
    removed: list[dict] = []
    changed: list[dict] = []

    for key in old_groups.keys() | new_groups.keys():
        before = list(old_groups.get(key, []))
        after = list(new_groups.get(key, []))

        # Remove exact matches first. This turns each key into a multiset and
        # preserves duplicates instead of overwriting them in a dict.
        unmatched_before: list[dict] = []
        for item in before:
            signature = _change_signature(item)
            match = next((i for i, candidate in enumerate(after) if _change_signature(candidate) == signature), None)
            if match is None:
                unmatched_before.append(item)
            else:
                after.pop(match)

        pair_count = min(len(unmatched_before), len(after))
        for index in range(pair_count):
            old_item, new_item = unmatched_before[index], after[index]
            fields = [
                (label, str(old_item.get(field) or ""), str(new_item.get(field) or ""))
                for field, label in _DIFF_FIELDS
                if str(old_item.get(field) or "").strip() != str(new_item.get(field) or "").strip()
            ]
            if fields:
                changed.append(_change_item(new_item, fields))
        removed.extend(unmatched_before[pair_count:])
        added.extend(after[pair_count:])

    order = {day: i for i, day in enumerate(("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"))}

    def sort_key(item: dict) -> tuple:
        match = re.search(r"\d{1,2}:\d{2}", str(item.get("time") or ""))
        minutes = 24 * 60
        if match:
            hour, minute = (int(part) for part in match.group().split(":"))
            if hour < 24 and minute < 60:
                minutes = hour * 60 + minute
        return (order.get(str(item.get("day")), 9), minutes, _subject(item), _subgroup(item))

    if old_stub is None:
        old_stub = not any(old_groups.values())
    if new_stub is None:
        new_stub = not any(new_groups.values())

    # Enrich bare last names to full names in teacher fields.
    lookup = teacher_lookup or _build_teacher_lookup(
        _collect_teacher_names(old) | _collect_teacher_names(new)
    )
    if lookup:
        for item in added + removed:
            enriched = _enrich_teacher(str(item.get("teacher") or ""), lookup)
            if enriched != str(item.get("teacher") or ""):
                item["teacher"] = enriched

    # Переименованная пара остаётся на месте: это одна правка, а не
    # «убрали» + «добавили». Сводим после обогащения ФИО, чтобы совпали
    # записи, где фамилия с одной стороны напечатана сокращённо.
    renamed, removed, added = _match_renames(removed, added)
    changed.extend(renamed)

    if lookup:
        for item in changed:
            new_fields = []
            for field in (item.get("fields") or []):
                if not isinstance(field, (list, tuple)) or len(field) != 3:
                    new_fields.append(field)
                    continue
                label, old_val, new_val = field
                if label == "преподаватель":
                    old_val = _enrich_teacher(old_val, lookup)
                    new_val = _enrich_teacher(new_val, lookup)
                new_fields.append([label, old_val, new_val])
            item["fields"] = new_fields

    transition = "published" if old_stub and not new_stub else "vanished" if not old_stub and new_stub else None
    return {
        "added": sorted(added, key=sort_key),
        "removed": sorted(removed, key=sort_key),
        "changed": sorted(changed, key=sort_key),
        "transition": transition,
    }


def _comparison_screen_media(
    diff: dict,
    old_html: str | None,
    new_html: str | None,
) -> tuple[list[dict], dict[str, Path]]:
    if not old_html and not new_html:
        return [], {}
    pairs = comparison_day_screens(old_html, new_html, diff)
    media: list[dict] = []
    files: dict[str, Path] = {}
    for index, item in enumerate(pairs, 1):
        entry = {
            "label": item.get("label", ""), "source_url": config.GROUP_URL,
            "before_kinds": item.get("before_kinds", []),
            "after_kinds": item.get("after_kinds", []),
        }
        if item.get("before_path"):
            key = f"changes_before_{index}"
            entry["before_media"] = f"attach://{key}"
            files[key] = Path(item["before_path"])
        if item.get("after_path"):
            key = f"changes_after_{index}"
            entry["after_media"] = f"attach://{key}"
            files[key] = Path(item["after_path"])
        media.append(entry)
    return media, files


def _change_notifications_silent(now: dt.datetime | None = None) -> bool:
    """Quiet hours follow actual send time in Moscow, not the source timestamp."""
    timezone = zoneinfo.ZoneInfo("Europe/Moscow")
    now = now or dt.datetime.now(timezone)
    now = now.replace(tzinfo=timezone) if now.tzinfo is None else now.astimezone(timezone)
    return now.hour >= 22 or now.hour < 7


def _post_changes_to_channel(
    diff: dict,
    weeks: list[dict],
    *,
    html: str | None = None,
    old_html: str | None = None,
    fingerprint: str | None = None,
    focus_date: dt.date | None = None,
    change_time: dt.datetime | None = None,
) -> dict:
    """Send the durable text notification before any optional media work."""
    rich = build_changes_rich_message(
        diff, weeks, config.GROUP_URL, group_name="6381", now=change_time,
    )
    try:
        result = send_rich_message(
            rich, token=config.TG_BOT_TOKEN, chat_id=config.TG_CHANNEL_ID,
            disable_notification=_change_notifications_silent(),
        )
    except Exception as exc:  # local validation/transport failure also falls back
        result = {"ok": False, "error": str(exc)}
    if result.get("ok"):
        result["_rich"] = True
        return result
    print(f"[sendRichMessage FAIL] {result}", file=sys.stderr)
    text = changes_fallback_text(
        diff, config.GROUP_URL, group_name="6381", now=change_time,
    )
    return _tg_api(
        "sendMessage", chat_id=config.TG_CHANNEL_ID, text=text, parse_mode="HTML",
        disable_notification=_change_notifications_silent(),
    )


def _queue_media_enhancement(
    result: dict,
    diff: dict,
    weeks: list[dict],
    old_snapshot_id: int | None,
    new_snapshot_id: int | None,
    change_time: dt.datetime | None = None,
) -> None:
    message_id = ((result.get("result") or {}).get("message_id") if isinstance(result.get("result"), dict) else None)
    if not result.get("_rich") or not isinstance(message_id, int) or not (old_snapshot_id or new_snapshot_id):
        return
    path = config.STATE_DIR / "pending_media.json"
    state = _read_json(path)
    items = state.get("items") if isinstance(state.get("items"), list) else []
    task = {
        "message_id": message_id,
        "diff": diff,
        "weeks": weeks,
        "old_snapshot_id": old_snapshot_id,
        "new_snapshot_id": new_snapshot_id,
    }
    if change_time is not None:
        task["change_time"] = change_time.isoformat(timespec="seconds")
    items = [item for item in items if item.get("message_id") != message_id]
    items.append(task)
    _write_json_atomic(path, {"items": items})


# Ошибки редактирования, при которых повторять бессмысленно: пост удалили,
# его нельзя редактировать или чат недоступен. Без классификации одна такая
# задача в голове очереди вечно блокировала скрины всех следующих постов.
_TERMINAL_MEDIA_ERRORS = (
    "message to edit not found",
    "message can't be edited",
    "message identifier is not specified",
    "chat not found",
    "bot was blocked",
)


def _try_pending_media() -> bool | None:
    path = config.STATE_DIR / "pending_media.json"
    state = _read_json(path)
    items = state.get("items") if isinstance(state.get("items"), list) else []
    if not items or not items[0].get("message_id") or items[0].get("diff") is None:
        return None
    progressed = False
    while items:
        pending = items[0]
        db_path = config.STATE_DIR / "timetable.sqlite3"
        old_id, new_id = pending.get("old_snapshot_id"), pending.get("new_snapshot_id")
        old_html = timetable_api.load_snapshot_html(int(old_id), db_path=db_path) if old_id else None
        new_html = timetable_api.load_snapshot_html(int(new_id), db_path=db_path) if new_id else None
        try:
            screenshot_media, files = _comparison_screen_media(pending["diff"], old_html, new_html)
            if not files:
                items.pop(0)
                progressed = True
                continue
            change_time = None
            raw_change_time = pending.get("change_time")
            if isinstance(raw_change_time, str) and raw_change_time:
                try:
                    change_time = dt.datetime.fromisoformat(raw_change_time)
                except ValueError:
                    change_time = None
            rich = build_changes_rich_message(
                pending["diff"], pending.get("weeks") or [], config.GROUP_URL,
                group_name="6381", screenshot_media=screenshot_media, now=change_time,
            )
            result = edit_rich_message(
                rich,
                token=config.TG_BOT_TOKEN,
                chat_id=config.TG_CHANNEL_ID,
                message_id=int(pending["message_id"]),
                files=files,
                timeout=120,
            )
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": str(exc)}
        if result.get("ok") or "not modified" in str(result.get("description", "")):
            items.pop(0)
            progressed = True
            continue
        failure_text = str(result.get("description", "") or result.get("error", "")).casefold()
        if any(marker in failure_text for marker in _TERMINAL_MEDIA_ERRORS):
            # Мёртвый пост не должен душить очередь: снимаем задачу одним
            # алертом и идём к следующей, transient-ошибки ретраим как раньше.
            items.pop(0)
            progressed = True
            _dm(
                "pending media: пост "
                f"<code>{_escape_code(int(pending.get('message_id')))}</code> недоступен, "
                "задача снята, скрины не добавлены: "
                f"<code>{_escape_code(failure_text, 200)}</code>"
            )
            print(f"[pending media] terminal error, task dropped: {failure_text}", file=sys.stderr)
            continue
        print(f"[pending media] enhancement failed: {result}", file=sys.stderr)
        break
    _write_json_atomic(path, {"items": items}) if items else _unlink(path)
    return progressed if progressed else False


def _read_json(path: Path) -> dict:
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _touch_state_file() -> None:
    """Update mtime of monitor_state.json so healthcheck sees we're alive.

    Called from the error path in run_interval when fetch keeps failing
    (e.g. portal 502) — the normal write path never runs, so without this
    the healthcheck would false-alarm "monitor not running".
    """
    try:
        path = config.STATE_DIR / "monitor_state.json"
        if path.exists():
            os.utime(path)
        else:
            # File missing: create a minimal stub so mtime is fresh.
            _write_json_atomic(path, {"event": {}, "stub": True, "schedule": None, "weeks": []})
    except Exception:
        pass


@contextmanager
def _state_lock():
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = config.STATE_DIR / ".monitor.lock"
    with lock_path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another monitor cycle is running; lock busy") from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_baseline() -> dict:
    combined = _read_json(config.STATE_DIR / "monitor_state.json")
    if combined.get("event") and "schedule" in combined:
        return combined
    # Backward-compatible import of the previous two-file state. An incomplete
    # pair is unsafe and is treated as a first run rather than publishing all
    # lessons as a fake change.
    event = _read_json(config.STATE_DIR / "last.json")
    parsed = _read_json(config.STATE_DIR / "last_parsed.json")
    if event.get("fingerprint") and "schedule" in parsed:
        return {"event": event, **parsed, "stub": bool(event.get("stub"))}
    return {}


def _save_baseline(event: dict, data: dict, *, last_change_event: dict | None = None) -> None:
    # ``event`` описывает последний опрос. Отдельно сохраняем событие,
    # которое действительно сдвинуло семантический baseline, чтобы UI не
    # выдавал время очередного poll за время изменения расписания.
    if last_change_event is None:
        last_change_event = event
    state = {
        "event": event,
        "last_change_event": last_change_event,
        "stub": bool(data.get("stub")),
        "schedule": data.get("schedule"),
        "weeks": data.get("weeks", []),
    }
    _write_json_atomic(config.STATE_DIR / "monitor_state.json", state)
    # Compatibility for existing tooling. The combined file above is the
    # authoritative atomic baseline.
    _write_json_atomic(config.STATE_DIR / "last.json", event)
    _write_json_atomic(config.STATE_DIR / "last_parsed.json", {"schedule": data.get("schedule"), "weeks": data.get("weeks", [])})


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _collect_teacher_names(schedule: dict | None) -> set[str]:
    """Extract all non-empty teacher names from a schedule."""
    names: set[str] = set()
    for lessons in ((schedule or {}).get("days") or {}).values():
        for lesson in lessons:
            teacher = str(lesson.get("teacher") or "").strip()
            if teacher and teacher != "—":
                names.add(teacher)
    return names


def _build_teacher_lookup(names: set[str]) -> dict[str, str]:
    """Map last name → full name when the mapping is unambiguous.

    A name is considered full when it has at least two words. The last name
    is the first word. If two different full names share the same last name,
    the mapping is skipped to avoid guessing.
    """
    by_last: dict[str, set[str]] = defaultdict(set)
    for name in names:
        parts = name.split()
        if len(parts) >= 2:
            by_last[parts[0]].add(name)
    return {last: fulls.pop() for last, fulls in by_last.items() if len(fulls) == 1}


def _enrich_teacher(name: str, lookup: dict[str, str]) -> str:
    """Replace a bare last name with the full name when the lookup is sure."""
    name = str(name or "").strip()
    if not name or name == "—":
        return name
    if " " in name:
        return name  # already has first/middle name or initials
    return lookup.get(name, name)


def _load_teacher_cache() -> dict[str, str]:
    try:
        data = _read_json(config.STATE_DIR / "teacher_names.json")
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError):
        return {}


def _save_teacher_cache(lookup: dict[str, str]) -> None:
    _write_json_atomic(config.STATE_DIR / "teacher_names.json", lookup)


def _load_teacher_id_cache() -> dict[str, str]:
    """Load teacherId → full_name cache (from portal teacher directory)."""
    try:
        data = _read_json(config.STATE_DIR / "teacher_ids.json")
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError):
        return {}


def _lesson_count(schedule: dict | None) -> int:
    return sum(len(rows) for rows in ((schedule or {}).get("days") or {}).values())


def _requires_confirmation(old_data: dict, new_data: dict) -> bool:
    old_stub, new_stub = bool(old_data.get("stub")), bool(new_data.get("stub"))
    if old_stub != new_stub:
        return True
    old_count = _lesson_count(old_data.get("schedule"))
    new_count = _lesson_count(new_data.get("schedule"))
    return old_count >= 4 and new_count * 2 < old_count


def _escape_code(value: object, limit: int = 500) -> str:
    return html_lib.escape(str(value)[:limit], quote=False)


def _try_dashboard(
    update_post_id: int | None,
    post_date: dt.date | None,
    html: str,
    fingerprint: str,
    data: dict,
    *,
    render_screens: bool = True,
) -> bool | None:
    pending_file = config.STATE_DIR / "pending_dashboard.json"
    if not update_post_id:
        return None
    focus_date = post_date or dt.datetime.now(zoneinfo.ZoneInfo("Europe/Moscow")).date()
    try:
        result = edit_dashboard_post(
            update_post_id, focus_date, html=html, fingerprint=fingerprint, data=data,
            render_screens=render_screens,
        )
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": str(exc)}
    if result.get("ok"):
        _unlink(pending_file)
        return True
    # "message is not modified" — content already current, treat as success
    desc = str(result.get("description", ""))
    if "not modified" in desc:
        _unlink(pending_file)
        return True
    previous = _read_json(pending_file)
    prev_error = previous.get("last_error")
    _write_json_atomic(pending_file, {"fingerprint": fingerprint, "message_id": update_post_id, "last_error": result})
    # Only DM on first failure or when the error changes.
    if prev_error != result:
        _dm(f"edit_dashboard_post failed: <code>{_escape_code(json.dumps(result, ensure_ascii=False))}</code>")
    return False


def _run_once_locked(update_post_id: int | None = None, post_date: dt.date | None = None) -> dict:
    changes_log = config.STATE_DIR / "changes.jsonl"
    candidate_file = config.STATE_DIR / "candidate_change.json"
    pending_file = config.STATE_DIR / "pending_notification.json"
    baseline = _load_baseline()

    html = fetch_html()
    # Держим последний успешный сырой ответ доступным для ручной диагностики
    # и screenshot CLI, а не оставляем state/6381.html старым ручным снапшотом.
    save_html(html)
    data = parse_all(html)
    if not data.get("stub"):
        expected = int(data.get("physical_lesson_count", -1))
        actual = _lesson_count(data.get("schedule"))
        if expected != actual:
            raise RuntimeError(f"parser invariant failed: physical={expected}, parsed={actual}")
    stub = bool(data.get("stub"))
    new_fp = content_fingerprint(data)
    with timetable_api.connect(config.STATE_DIR / "timetable.sqlite3") as db:
        snapshot_id = timetable_api.persist(db, html, config.GROUP_URL, data)
    event = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "hash": hash_html(html),
        "fingerprint": new_fp,
        "size": len(html),
        "stub": stub,
        "snapshot_id": snapshot_id,
    }
    change_time = _change_time_msk(event)
    with changes_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    # Rotate if the log grows too large.
    try:
        if changes_log.stat().st_size > 1_000_000:
            lines = changes_log.read_text(encoding="utf-8").splitlines()
            temporary = changes_log.with_name(f".{changes_log.name}.{os.getpid()}.tmp")
            temporary.write_text("\n".join(lines[-500:]) + "\n", encoding="utf-8")
            os.replace(temporary, changes_log)
    except Exception:  # noqa: BLE001
        pass

    previous_event = baseline.get("event") or {}
    previous_fp = previous_event.get("fingerprint")
    old_snapshot_id = previous_event.get("snapshot_id")
    old_html = (
        timetable_api.load_snapshot_html(
            int(old_snapshot_id), db_path=config.STATE_DIR / "timetable.sqlite3",
        )
        if old_snapshot_id
        else None
    )
    if previous_fp is None:
        _save_baseline(event, data, last_change_event=event)
        _unlink(candidate_file)
        _unlink(pending_file)
        dashboard_ok = _try_dashboard(update_post_id, post_date, html, new_fp, data) if update_post_id else None
        print(f"[baseline] fingerprint={new_fp} stub={stub}")
        return {"changed": False, "fingerprint": new_fp, "stub": stub, "posted": None,
                "dashboard_updated": dashboard_ok, **event}

    changed = previous_fp != new_fp

    # Недоставленный дифф нельзя терять, но и дублировать нельзя. База при
    # отказе не сдвигается, поэтому свежий дифф «база → сейчас» уже включает
    # ту правку. Значит устаревший pending выбрасываем как поглощённый, а
    # добиваем его только когда портал стоит на месте и свежего поста не будет.
    pending_notif = _read_json(pending_file)
    if pending_notif.get("diff") is not None and changed:
        _unlink(pending_file)
        pending_notif = {}
        print("[retry pending] поглощён свежим диффом", file=sys.stderr)
    if pending_notif.get("diff") is not None:
        try:
            retry_result = _post_changes_to_channel(
                pending_notif["diff"],
                data.get("weeks", []),
                html=html,
                old_html=old_html,
                fingerprint=new_fp,
                focus_date=post_date,
                change_time=_change_time_msk(pending_notif.get("event") or event),
            )
        except Exception as exc:  # noqa: BLE001
            retry_result = {"ok": False, "error": str(exc)}
        if retry_result.get("ok"):
            _queue_media_enhancement(
                retry_result,
                pending_notif["diff"],
                data.get("weeks", []),
                pending_notif.get("old_snapshot_id") or old_snapshot_id,
                pending_notif.get("new_snapshot_id") or snapshot_id,
                change_time=_change_time_msk(pending_notif.get("event") or event),
            )
            _unlink(pending_file)
            print("[retry pending] diff posted successfully")
        else:
            print(f"[retry pending] still failing: {retry_result.get('error', '?')}", file=sys.stderr)

    if not changed:
        _save_baseline(
            event,
            data,
            last_change_event=baseline.get("last_change_event") or previous_event or event,
        )
        _unlink(candidate_file)
        dashboard_ok = None
        if update_post_id:
            dashboard_ok = _try_dashboard(update_post_id, post_date, html, new_fp, data)
        _try_pending_media()
        print(f"[no change] fingerprint={new_fp} stub={stub}")
        return {"changed": False, "fingerprint": new_fp, "stub": stub, "posted": None, "dashboard_updated": dashboard_ok, **event}

    old_data = {
        "stub": bool(baseline.get("stub")),
        "schedule": baseline.get("schedule"),
        "weeks": baseline.get("weeks", []),
    }
    if _requires_confirmation(old_data, data):
        candidate = _read_json(candidate_file)
        if candidate.get("fingerprint") != new_fp:
            _write_json_atomic(candidate_file, {"fingerprint": new_fp, "seen": 1, "event": event})
            print(f"[candidate] suspicious transition fingerprint={new_fp}; waiting for confirmation")
            return {"changed": False, "pending_confirmation": True, "fingerprint": new_fp, "stub": stub, "posted": None, **event}
    _unlink(candidate_file)

    # Build a teacher name lookup from the current and cached schedules so
    # that bare last names in the "Изменено" section are enriched to full FIO.
    teacher_cache = _load_teacher_cache()
    all_names = _collect_teacher_names(old_data.get("schedule")) | _collect_teacher_names(data.get("schedule"))
    schedule_lookup = _build_teacher_lookup(all_names)
    # Detect ambiguous last names across BOTH schedule and cache
    _by_last: dict[str, set[str]] = defaultdict(set)
    for _name in all_names:
        _parts = _name.split()
        if len(_parts) >= 2:
            _by_last[_parts[0]].add(_name)
    for _clast, _cfull in teacher_cache.items():
        _by_last[_clast].add(_cfull)
    _ambiguous = {k for k, v in _by_last.items() if len(v) > 1}
    # Cache entries for ambiguous surnames are unsafe — skip them
    _safe_cache = {k: v for k, v in teacher_cache.items() if k not in _ambiguous}
    # Teacher ID-based enrichment: teacher_text → teacherId → full_name
    # This is unambiguous (no oneфамилец problem) and works for bare last names
    _id_cache = _load_teacher_id_cache()
    _id_lookup: dict[str, str] = {}
    _teacher_id_map = data.get("teacher_ids") or {}
    for _ttext, _tid in _teacher_id_map.items():
        _full = _id_cache.get(_tid)
        if _full and _ttext != _full:
            _id_lookup[_ttext] = _full
    lookup = {**_safe_cache, **schedule_lookup, **_id_lookup}
    diff = diff_schedules(
        old_data.get("schedule"),
        data.get("schedule"),
        old_stub=old_data.get("stub"),
        new_stub=stub,
        teacher_lookup=lookup,
    )
    # Persist any new full names for future enrichment.
    new_cache = {**_build_teacher_lookup(all_names), **teacher_cache}
    if new_cache != teacher_cache:
        _save_teacher_cache(new_cache)

    # Отпечаток изменился, а дифф пустой — значит поменялось поле, которого
    # нет в _DIFF_FIELDS (например link), или парсер перестал видеть правку.
    # Раньше монитор в этом случае молча сдвигал базу и в канал не писал:
    # снаружи это выглядит как «расписание поменяли, а бот не сказал».
    # Теперь база всё равно сдвигается (иначе цикл зацикливается на одном и
    # том же отпечатке), но улика сохраняется и приходит в личку.
    if not (diff["added"] or diff["removed"] or diff["changed"] or diff["transition"]):
        unexplained = config.STATE_DIR / "unexplained_change.json"
        _write_json_atomic(unexplained, {
            "event": event,
            "previous_fingerprint": previous_fp,
            "old_schedule": old_data.get("schedule"),
            "new_schedule": data.get("schedule"),
        })
        _save_baseline(event, data, last_change_event=event)
        _unlink(pending_file)
        dashboard_ok = _try_dashboard(update_post_id, post_date, html, new_fp, data) if update_post_id else None
        _dm(
            "⚠️ отпечаток расписания изменился, но дифф пустой\n"
            f"было: <code>{_escape_code(previous_fp, 80)}</code>\n"
            f"стало: <code>{_escape_code(new_fp, 80)}</code>\n"
            f"улика: <code>{_escape_code(unexplained)}</code>"
        )
        print(f"[unexplained] fingerprint={new_fp} diff empty", file=sys.stderr)
        return {"changed": True, "fingerprint": new_fp, "stub": stub, "posted": None,
                "unexplained": True, "dashboard_updated": dashboard_ok, **event}

    _write_json_atomic(pending_file, {
        "event": event,
        "diff": diff,
        "old_snapshot_id": old_snapshot_id,
        "new_snapshot_id": snapshot_id,
    })
    try:
        post_result = _post_changes_to_channel(
            diff,
            data.get("weeks", []),
            html=html,
            old_html=old_html,
            fingerprint=new_fp,
            focus_date=post_date,
            change_time=change_time,
        )
    except Exception as exc:  # noqa: BLE001
        post_result = {"ok": False, "error": str(exc)}
    post_ok = bool(post_result.get("ok"))
    if not post_ok:
        _dm(f"пост диффа в канал не прошёл: <code>{_escape_code(json.dumps(post_result, ensure_ascii=False))}</code>")
        print(f"[delivery pending] fingerprint={new_fp}")
        return {"changed": True, "fingerprint": new_fp, "stub": stub, "posted": False, "delivery_pending": True, **event}

    _queue_media_enhancement(
        post_result, diff, data.get("weeks", []), old_snapshot_id, snapshot_id,
        change_time=change_time,
    )
    _save_baseline(event, data, last_change_event=event)
    _unlink(pending_file)
    dashboard_ok = (
        _try_dashboard(update_post_id, post_date, html, new_fp, data, render_screens=False)
        if update_post_id
        else None
    )
    _try_pending_media()
    total = len(diff["added"]) + len(diff["removed"]) + len(diff["changed"])
    print(f"[changed] diff: +{len(diff['added'])} -{len(diff['removed'])} ~{len(diff['changed'])} "
          f"transition={diff['transition']} total={total} posted=True")
    return {"changed": True, "fingerprint": new_fp, "stub": stub, "posted": True, "dashboard_updated": dashboard_ok, **event}


def run_once(update_post_id: int | None = None, post_date: dt.date | None = None) -> dict:
    """Run one serialized monitor cycle with atomic state updates."""
    with _state_lock():
        result = _run_once_locked(update_post_id=update_post_id, post_date=post_date)
        try:
            extra_posts.sync(opd_module.load_opd())
        except Exception as exc:  # noqa: BLE001
            print(f"[extra posts] {exc}", file=sys.stderr)
            _dm(f"extra_posts.sync failed: <code>{_escape_code(exc)}</code>")
        return result


def schedule_daily(hour: int = 9, minute: int = 0, update_post_id: int | None = None, post_date: dt.date | None = None):
    """Простой scheduler: спим до 09:00 Europe/Moscow каждый день."""
    import zoneinfo
    tz = zoneinfo.ZoneInfo("Europe/Moscow")
    while True:
        now = dt.datetime.now(tz)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += dt.timedelta(days=1)
        sleep_s = (target - now).total_seconds()
        print(f"[sleep {sleep_s:.0f}s until {target.isoformat()}]")
        time.sleep(sleep_s)
        try:
            run_once(update_post_id=update_post_id, post_date=post_date)
        except Exception as e:  # noqa: BLE001
            print(f"[run ERR] {e}", file=sys.stderr)
            _dm(f"monitor.run_once failed: <code>{_escape_code(e)}</code>")


def run_interval(
    minutes: float = 3,
    jitter_s: float = 15.0,
    update_post_id: int | None = None,
    post_date: dt.date | None = None,
):
    """Run at a start-to-start interval of ``minutes ± jitter_s`` seconds."""
    if minutes <= 0:
        raise ValueError("interval must be positive")
    if jitter_s < 0 or jitter_s >= minutes * 60:
        raise ValueError("jitter must be non-negative and shorter than the interval")
    consecutive_failures = 0
    alerted = False
    while True:
        started = time.monotonic()
        try:
            run_once(update_post_id=update_post_id, post_date=post_date)
            if alerted:
                _dm("✅ monitor: фетч восстановился")
            consecutive_failures = 0
            alerted = False
        except Exception as e:  # noqa: BLE001
            consecutive_failures += 1
            print(f"[run ERR {consecutive_failures}] {e}", file=sys.stderr)
            # Touch state file so healthcheck knows monitor is alive even
            # when the portal is down and fetch keeps failing.
            _touch_state_file()
            if consecutive_failures >= FAIL_ALERT_THRESHOLD and not alerted:
                _dm(
                    f"monitor: {consecutive_failures} подряд неудачных циклов\n"
                    f"последняя ошибка: <code>{_escape_code(e, 400)}</code>"
                )
                alerted = True
        # Back off portal/network failures, but keep the normal start-to-start
        # period short enough to leave time for text delivery under five minutes.
        multiplier = min(2 ** max(0, consecutive_failures - 1), max(1.0, 20.0 / minutes))
        target_period = minutes * 60 * multiplier + random.uniform(-jitter_s, jitter_s)
        sleep_s = max(1.0, target_period - (time.monotonic() - started))
        print(f"[sleep {sleep_s:.0f}s]")
        time.sleep(sleep_s)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--schedule", action="store_true", help="вечный scheduler раз в день 09:00 MSK")
    ap.add_argument("--interval", type=float, metavar="MINUTES", help="вечный цикл: проверка каждые N минут")
    ap.add_argument("--jitter", type=float, default=0.25, metavar="MINUTES", help="симметричный джиттер ±N минут (по умолчанию 0.25)")
    _env_post = os.environ.get("NOVSU_DASHBOARD_POST_ID", "").strip()
    _default_post = int(_env_post) if _env_post and _env_post not in ("0", "None") else None
    ap.add_argument(
        "--update-post",
        type=int,
        metavar="MESSAGE_ID",
        default=_default_post,
        help="обновлять rich-закреп при изменении расписания (по умолчанию берётся из NOVSU_DASHBOARD_POST_ID)",
    )
    ap.add_argument("--date", default=None, help="дата фокуса для обновляемого поста, YYYY-MM-DD")
    args = ap.parse_args()
    post_date = dt.datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else None

    if args.interval:
        run_interval(minutes=args.interval, jitter_s=args.jitter * 60, update_post_id=args.update_post, post_date=post_date)
    elif args.schedule:
        schedule_daily(update_post_id=args.update_post, post_date=post_date)
    else:
        result = run_once(update_post_id=args.update_post, post_date=post_date)
        print(json.dumps(result, ensure_ascii=False))
