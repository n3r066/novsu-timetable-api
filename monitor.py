"""Мониторинг изменений страницы группы 6381.

Логика одного прогона:
1. fetch_html (curl, ретраи, случайный Chromium UA)
2. content_fingerprint — отпечаток распарсенного содержимого
   (иммунитет к волатильным таймстемпам .xls-ссылок портала)
3. сравнить с атомарным baseline из state/monitor_state.json
4. если изменилось:
   - дописать событие в state/changes.jsonl
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

import config  # noqa: E402
from fetch import content_fingerprint, fetch_html, hash_html, is_stub  # noqa: E402
from format import build_changes_rich_message, changes_fallback_text  # noqa: E402
from parse import parse_all  # noqa: E402
from portal_parser import count_schedule_lessons  # noqa: E402
from post import _tg_api, edit_dashboard_post  # noqa: E402
from telegram_api import send_rich_message  # noqa: E402

# Сколько подряд неудачных циклов терпим молча перед tech-алертом в личку
FAIL_ALERT_THRESHOLD = 3


def _dm(text: str) -> None:
    """Короткое сообщение Георгию в личку."""
    import urllib.request
    import urllib.parse

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


def diff_schedules(
    old: dict | None,
    new: dict | None,
    *,
    old_stub: bool | None = None,
    new_stub: bool | None = None,
) -> dict:
    """Compare schedules without collapsing simultaneous subgroup lessons."""
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
                changed.append({
                    "day": new_item["day"],
                    "time": new_item.get("time"),
                    "subject": new_item.get("subject"),
                    "subgroup": _subgroup(new_item),
                    "fields": fields,
                })
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
    transition = "published" if old_stub and not new_stub else "vanished" if not old_stub and new_stub else None
    return {
        "added": sorted(added, key=sort_key),
        "removed": sorted(removed, key=sort_key),
        "changed": sorted(changed, key=sort_key),
        "transition": transition,
    }


def _post_changes_to_channel(diff: dict, weeks: list[dict]) -> dict:
    """Rich-пост в канал; при отказе — plain-HTML фоллбек."""
    rich = build_changes_rich_message(diff, weeks, config.GROUP_URL, group_name="6381")
    try:
        result = send_rich_message(rich, token=config.TG_BOT_TOKEN, chat_id=config.TG_CHANNEL_ID)
    except Exception as exc:  # local validation/transport failure also falls back
        result = {"ok": False, "error": str(exc)}
    if result.get("ok"):
        return result
    print(f"[sendRichMessage FAIL] {result}", file=sys.stderr)
    text = changes_fallback_text(diff, config.GROUP_URL, group_name="6381")
    return _tg_api("sendMessage", chat_id=config.TG_CHANNEL_ID, text=text, parse_mode="HTML")


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


@contextmanager
def _state_lock():
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = config.STATE_DIR / ".monitor.lock"
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
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


def _save_baseline(event: dict, data: dict) -> None:
    state = {"event": event, "stub": bool(data.get("stub")), "schedule": data.get("schedule"), "weeks": data.get("weeks", [])}
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


def _try_dashboard(update_post_id: int | None, post_date: dt.date | None, html: str, fingerprint: str) -> bool | None:
    pending_file = config.STATE_DIR / "pending_dashboard.json"
    if not update_post_id:
        return None
    focus_date = post_date or dt.datetime.now(zoneinfo.ZoneInfo("Europe/Moscow")).date()
    try:
        result = edit_dashboard_post(update_post_id, focus_date, html=html)
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": str(exc)}
    if result.get("ok"):
        _unlink(pending_file)
        return True
    _write_json_atomic(pending_file, {"fingerprint": fingerprint, "message_id": update_post_id, "last_error": result})
    _dm(f"⚠️ edit_dashboard_post failed: <code>{_escape_code(json.dumps(result, ensure_ascii=False))}</code>")
    return False


def _run_once_locked(update_post_id: int | None = None, post_date: dt.date | None = None) -> dict:
    changes_log = config.STATE_DIR / "changes.jsonl"
    candidate_file = config.STATE_DIR / "candidate_change.json"
    pending_file = config.STATE_DIR / "pending_notification.json"
    baseline = _load_baseline()

    html = fetch_html()
    data = parse_all(html)
    if not data.get("stub"):
        expected = count_schedule_lessons(html)
        actual = _lesson_count(data.get("schedule"))
        if expected != actual:
            raise RuntimeError(f"parser invariant failed: physical={expected}, parsed={actual}")
    stub = bool(data.get("stub"))
    new_fp = content_fingerprint(data)
    event = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "hash": hash_html(html),
        "fingerprint": new_fp,
        "size": len(html),
        "stub": stub,
    }
    with changes_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    previous_event = baseline.get("event") or {}
    previous_fp = previous_event.get("fingerprint")
    if previous_fp is None:
        _save_baseline(event, data)
        _unlink(candidate_file)
        _unlink(pending_file)
        print(f"[baseline] fingerprint={new_fp} stub={stub}")
        return {"changed": False, "fingerprint": new_fp, "stub": stub, "posted": None, **event}

    changed = previous_fp != new_fp
    if not changed:
        _save_baseline(event, data)
        _unlink(candidate_file)
        dashboard_ok = None
        if update_post_id and (config.STATE_DIR / "pending_dashboard.json").exists():
            dashboard_ok = _try_dashboard(update_post_id, post_date, html, new_fp)
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

    diff = diff_schedules(
        old_data.get("schedule"),
        data.get("schedule"),
        old_stub=old_data.get("stub"),
        new_stub=stub,
    )
    _write_json_atomic(pending_file, {"event": event, "diff": diff})
    try:
        post_result = _post_changes_to_channel(diff, data.get("weeks", []))
    except Exception as exc:  # noqa: BLE001
        post_result = {"ok": False, "error": str(exc)}
    post_ok = bool(post_result.get("ok"))
    if not post_ok:
        _dm(f"⚠️ пост диффа в канал не прошёл: <code>{_escape_code(json.dumps(post_result, ensure_ascii=False))}</code>")
        print(f"[delivery pending] fingerprint={new_fp}")
        return {"changed": True, "fingerprint": new_fp, "stub": stub, "posted": False, "delivery_pending": True, **event}

    _save_baseline(event, data)
    _unlink(pending_file)
    dashboard_ok = _try_dashboard(update_post_id, post_date, html, new_fp) if update_post_id else None
    total = len(diff["added"]) + len(diff["removed"]) + len(diff["changed"])
    print(f"[changed] diff: +{len(diff['added'])} -{len(diff['removed'])} ~{len(diff['changed'])} "
          f"transition={diff['transition']} total={total} posted=True")
    return {"changed": True, "fingerprint": new_fp, "stub": stub, "posted": True, "dashboard_updated": dashboard_ok, **event}


def run_once(update_post_id: int | None = None, post_date: dt.date | None = None) -> dict:
    """Run one serialized monitor cycle with atomic state updates."""
    with _state_lock():
        return _run_once_locked(update_post_id=update_post_id, post_date=post_date)


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
            _dm(f"⚠️ monitor.run_once failed: <code>{_escape_code(e)}</code>")


def run_interval(
    minutes: float = 30,
    jitter_s: float = 180.0,
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
            if consecutive_failures >= FAIL_ALERT_THRESHOLD and not alerted:
                _dm(
                    f"⚠️ monitor: {consecutive_failures} подряд неудачных циклов\n"
                    f"последняя ошибка: <code>{_escape_code(e, 400)}</code>"
                )
                alerted = True
        target_period = minutes * 60 + random.uniform(-jitter_s, jitter_s)
        sleep_s = max(1.0, target_period - (time.monotonic() - started))
        print(f"[sleep {sleep_s:.0f}s]")
        time.sleep(sleep_s)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--schedule", action="store_true", help="вечный scheduler раз в день 09:00 MSK")
    ap.add_argument("--interval", type=float, metavar="MINUTES", help="вечный цикл: проверка каждые N минут")
    ap.add_argument("--jitter", type=float, default=3.0, metavar="MINUTES", help="симметричный джиттер ±N минут (по умолчанию 3)")
    ap.add_argument("--update-post", type=int, metavar="MESSAGE_ID", help="обновлять rich-закреп при изменении расписания")
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
