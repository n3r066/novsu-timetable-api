"""Date-aware timetable views for NovSU schedules."""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

import bells


DAYS_ORDER = [
    "Понедельник", "Вторник", "Среда", "Четверг",
    "Пятница", "Суббота", "Воскресенье",
]
DAY_BY_INDEX = {i: day for i, day in enumerate(DAYS_ORDER)}
WEEK_HALF_NAME = {"top": "верхняя", "bottom": "нижняя"}

_DATE_TOKEN = r"(?<!\d)\d{1,2}\.\d{1,2}(?:\.\d{2,4})?\.?(?!\d)"
_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\.?(?!\d)")
_START_DATE_RE = re.compile(rf"\bс\s+({_DATE_TOKEN})", re.I)
_RANGE_RE = re.compile(
    rf"(?:\bс\s+(?P<from>{_DATE_TOKEN})\s+по\s+(?P<to>{_DATE_TOKEN})"
    rf"|(?P<dash_from>{_DATE_TOKEN})\s*[–—-]\s*(?P<dash_to>{_DATE_TOKEN}))",
    re.I,
)
_CANCELLED_DATES_RE = re.compile(
    rf"(?P<dates>{_DATE_TOKEN}(?:(?:\s*[,;]\s*|\s+и\s+){_DATE_TOKEN})*)\s+занятий\s+не\s+будет",
    re.I,
)
_ONLY_DATES_RE = re.compile(
    rf"\bтолько\s+(?P<dates>{_DATE_TOKEN}(?:(?:\s*[,;]\s*|\s+и\s+){_DATE_TOKEN})*)",
    re.I,
)
_DATE_LIST_RE = re.compile(rf"(?P<dates>{_DATE_TOKEN}(?:(?:\s*[,;]\s*|\s+и\s+){_DATE_TOKEN})+)", re.I)
_START_MONTH_RE = re.compile(
    r"\bс\s+(января|февраля|марта|апреля|мая|июня|июля|августа|"
    r"сентября|октября|ноября|декабря)\b",
    re.I,
)
_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
_AFTER_WEEK_RE = re.compile(r"после\s+(\d+)\s+недел", re.I)
_UNTIL_WEEK_RE = re.compile(r"до\s+(\d+)\s+недел", re.I)


def parse_user_date(value: str | None, *, today: dt.date | None = None) -> dt.date:
    """Parse relative dates, ISO dates, and Russian DD.MM forms."""
    base = today or dt.date.today()
    if not value or str(value).strip().lower() in {"today", "сегодня"}:
        return base
    raw = str(value).strip().lower()
    if raw in {"tomorrow", "завтра"}:
        return base + dt.timedelta(days=1)

    # strptime uses 1900 before ``replace(year=...)`` for a yearless value,
    # which incorrectly rejects 29 February even when ``base.year`` is leap.
    short = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.?", raw)
    if short:
        try:
            return dt.date(base.year, int(short.group(2)), int(short.group(1)))
        except ValueError as exc:
            raise ValueError("date must be today, tomorrow, YYYY-MM-DD, DD.MM.YYYY or DD.MM") from exc

    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError("date must be today, tomorrow, YYYY-MM-DD, DD.MM.YYYY or DD.MM")


def _week_dates(week: dict) -> tuple[dt.date, dt.date] | None:
    try:
        start = dt.datetime.strptime(week["start"], "%d.%m.%Y").date()
        end = dt.datetime.strptime(week["end"], "%d.%m.%Y").date()
    except (KeyError, TypeError, ValueError):
        return None
    return start, end


def find_week(weeks: list[dict], target: dt.date) -> dict | None:
    for week in weeks:
        bounds = _week_dates(week)
        if not bounds:
            continue
        start, end = bounds
        if start <= target <= end:
            return week
    return None


def find_week_by_number(weeks: list[dict], number: int) -> dict | None:
    for week in weeks:
        if week.get("week") == number:
            return week
    return None


def calendar_bounds(weeks: list[dict]) -> tuple[dt.date, dt.date] | None:
    bounds = [_week_dates(week) for week in weeks]
    valid = [item for item in bounds if item]
    if not valid:
        return None
    starts, ends = zip(*valid)
    return min(starts), max(ends)


def _parse_note_date(
    token: str,
    default_year: int,
    bounds: tuple[dt.date, dt.date] | None = None,
) -> dt.date | None:
    m = _DATE_RE.fullmatch(token.strip())
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
    if year:
        full_year = int(year)
        if full_year < 100:
            full_year += 2000
        try:
            return dt.date(full_year, month, day)
        except ValueError:
            return None

    if bounds:
        # Academic calendars can cross New Year. Select the year whose month
        # lies inside the published calendar instead of blindly using the
        # target's year (October for a January target is the common failure).
        candidates: list[dt.date] = []
        first_month = (bounds[0].year, bounds[0].month)
        last_month = (bounds[1].year, bounds[1].month)
        for candidate_year in range(bounds[0].year, bounds[1].year + 1):
            try:
                candidate = dt.date(candidate_year, month, day)
            except ValueError:
                continue
            if first_month <= (candidate_year, month) <= last_month:
                candidates.append(candidate)
        if candidates:
            return min(candidates, key=lambda item: abs(item.year - default_year))

    try:
        return dt.date(default_year, month, day)
    except ValueError:
        return None


def _unique_parts(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        cleaned = value.strip()
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def _condition_parts(lesson: dict) -> list[str]:
    """Return actual annotation fields, not the lesson's display name."""
    parts = [str(lesson.get(key) or "") for key in ("note", "raw_comment")]
    # parse_schedule appends annotations after the first subject line. Treat
    # those continuation lines as conditions, while leaving the title alone.
    for key in ("subject", "subject_raw"):
        value = str(lesson.get(key) or "")
        if "\n" in value:
            parts.append(value.split("\n", 1)[1])
    return _unique_parts(parts)


def _date_condition_parts(lesson: dict) -> list[str]:
    # Date restrictions are occasionally embedded inline in the title (for
    # example, "Практикум с 10.09"). Keep supporting those without using the
    # title to infer upper/lower-week parity.
    parts = _condition_parts(lesson)
    parts.extend(str(lesson.get(key) or "") for key in ("subject", "subject_raw"))
    return _unique_parts(parts)


def _condition_text(lesson: dict) -> str:
    return " ".join(_condition_parts(lesson))


def _parsed_dates(
    text: str,
    default_year: int,
    bounds: tuple[dt.date, dt.date] | None,
) -> list[dt.date]:
    return [
        parsed
        for parsed in (
            _parse_note_date(match.group(0), default_year, bounds)
            for match in _DATE_RE.finditer(text)
        )
        if parsed is not None
    ]


def _explicit_lesson_dates(
    lesson: dict,
    target: dt.date,
    bounds: tuple[dt.date, dt.date] | None,
) -> tuple[bool, set[dt.date]]:
    """Return explicit occurrence dates, excluding ranges and cancellations."""
    dates: set[dt.date] = set()
    has_context = False
    for value in _date_condition_parts(lesson):
        text = _CANCELLED_DATES_RE.sub(" ", _RANGE_RE.sub(" ", value)).strip()
        for match in _ONLY_DATES_RE.finditer(text):
            has_context = True
            dates.update(_parsed_dates(match.group("dates"), target.year, bounds))
        list_match = _DATE_LIST_RE.search(text)
        if list_match:
            has_context = True
            dates.update(_parsed_dates(list_match.group("dates"), target.year, bounds))
    return has_context, dates


def lesson_parity(lesson: dict) -> str:
    low = _condition_text(lesson).lower()
    if "верхн" in low:
        return "top"
    if "нижн" in low:
        return "bottom"
    return "every"


def lesson_applies_on(
    lesson: dict,
    target: dt.date,
    week: dict | None,
    bounds: tuple[dt.date, dt.date] | None = None,
) -> bool:
    if week is None:
        return False
    if bounds is None:
        bounds = _week_dates(week)

    condition_text = _condition_text(lesson)
    low = condition_text.lower()
    half = week.get("half")
    parity = lesson_parity(lesson)
    if parity in {"top", "bottom"} and half != parity:
        return False

    week_number = int(week.get("week") or 0)
    after_match = _AFTER_WEEK_RE.search(low)
    if after_match and week_number <= int(after_match.group(1)):
        return False
    until_match = _UNTIL_WEEK_RE.search(low)
    if until_match and week_number >= int(until_match.group(1)):
        return False

    date_parts = _date_condition_parts(lesson)
    ranges: list[tuple[dt.date, dt.date]] = []
    for text in date_parts:
        for match in _RANGE_RE.finditer(text):
            start_token = match.group("from") or match.group("dash_from")
            end_token = match.group("to") or match.group("dash_to")
            start = _parse_note_date(start_token, target.year, bounds)
            end = _parse_note_date(end_token, target.year, bounds)
            if start is None or end is None:
                continue
            end_match = _DATE_RE.fullmatch(end_token.strip())
            if end < start and end_match and end_match.group(3) is None:
                try:
                    end = end.replace(year=start.year + 1)
                except ValueError:
                    pass
            ranges.append((start, end))
    if ranges and not any(start <= target <= end for start, end in ranges):
        return False

    cancelled_dates: set[dt.date] = set()
    for text in date_parts:
        for match in _CANCELLED_DATES_RE.finditer(text):
            cancelled_dates.update(_parsed_dates(match.group("dates"), target.year, bounds))
    if target in cancelled_dates:
        return False

    # A range's opening "с DD.MM" is not a separate lower-bound condition.
    without_ranges = [_RANGE_RE.sub(" ", text) for text in date_parts]
    start_dates = [
        parsed
        for text in without_ranges
        for parsed in (
            _parse_note_date(match.group(1), target.year, bounds)
            for match in _START_DATE_RE.finditer(text)
        )
        if parsed is not None
    ]
    for text in without_ranges:
        for match in _START_MONTH_RE.finditer(text):
            month = _MONTHS[match.group(1).lower()]
            parsed = _parse_note_date(f"01.{month:02d}", target.year, bounds)
            if parsed is not None:
                start_dates.append(parsed)
    if start_dates and target < max(start_dates):
        return False

    has_exact_context, exact_dates = _explicit_lesson_dates(lesson, target, bounds)
    if has_exact_context:
        return target in exact_dates

    return True


def _lesson_subject(lesson: dict) -> str:
    value = str(lesson.get("subject") or lesson.get("subject_raw") or "—")
    return value.split("\n", 1)[0].strip() or "—"


def _lesson_start(lesson: dict) -> dt.time:
    start = bells.slot(lesson.get("time")).start
    # Keep malformed upstream rows visible, but sort them after valid rows.
    return start or dt.time(23, 59)


def time_labels(source: object) -> dict:
    """Подписи времени для вывода: интервалы, структура, начало и конец.

    Принимает словарь урока, сырую ячейку «время» или готовый Slot/Block из
    bells.py. Само поле ``time`` не трогается: это идентификатор пары для
    диффов и снапшотов, поэтому там остаётся портальное написание.
    """
    if isinstance(source, dict):
        source = source.get("time")
    cell = source if isinstance(source, (bells.Slot, bells.Block)) else bells.slot(source)
    if isinstance(cell, bells.Block):
        return {
            "time_label": cell.label,
            "time_cell": cell.label_cell,
            "time_full": cell.label_full,
            "time_short": cell.label_both,
            "time_start": bells.hhmm(cell.start),
            "time_end": bells.hhmm(cell.end),
            "time_end_min": bells.hhmm(cell.end_min),
        }
    return {
        "time_label": cell.label(),
        "time_cell": cell.label_cell(),
        "time_full": cell.label_full(),
        "time_short": cell.label(both=True),
        "time_start": bells.hhmm(cell.start),
        "time_end": bells.hhmm(cell.end),
        "time_end_min": bells.hhmm(cell.end_min),
    }


def lesson_pairs(lesson: dict) -> list[tuple[dt.time, dict]]:
    """Разложить урок на пары: (начало, урок с подписями этой пары).

    Ячейка «14:00 15:00 16:00 17:00» — это две пары. Без разложения фильтр по
    текущему времени терял вторую: в 15:50 бот звал сразу на 19:00. Урок с
    нечитаемым временем отдаётся целиком, чтобы мусор с портала не исчезал.
    """
    slot = bells.slot(lesson.get("time"))
    pairs = slot.pair_blocks()
    if not pairs:
        return [(slot.start or dt.time(23, 59), lesson)]
    return [(pair.start, {**lesson, **time_labels(pair)}) for pair in pairs]


def _lesson_number(lesson: dict) -> int:
    value = lesson.get("number")
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 999


def _clean_note(lesson: dict) -> str:
    note = str(lesson.get("note") or lesson.get("raw_comment") or "").strip()
    note = re.sub(r"\bпо\s+(?:верхней|нижней)\s+недел[еи]\b", "", note, flags=re.I)
    note = re.sub(r"\s+,", ",", note)
    note = re.sub(r"\s{2,}", " ", note)
    return note.strip(" ,;")


def normalise_lesson(lesson: dict) -> dict:
    note = _clean_note(lesson)
    subject = _lesson_subject(lesson)
    # Пара с использованием ДОТ — помечаем [ДОТ] в начале названия предмета
    if "ДОТ" in note and not subject.startswith("[ДОТ]"):
        subject = f"[ДОТ] {subject}"
    return {
        "number": lesson.get("number"),
        "subject": subject,
        "time": lesson.get("time") or "—",
        **time_labels(lesson),
        "room": lesson.get("room") or "—",
        "teacher": lesson.get("teacher") or "—",
        "note": note,
        "raw_note": lesson.get("note") or lesson.get("raw_comment") or "",
        "parity": lesson_parity(lesson),
    }


# Даты без регулярных пар (День знаний и т.п.)
HOLIDAYS = {
    dt.date(2026, 9, 1),   # 1 сентября — День знаний, регулярных пар нет
}


def lessons_for_date(schedule: dict | None, weeks: list[dict], target: dt.date) -> tuple[dict | None, list[dict]]:
    if target in HOLIDAYS:
        return find_week(weeks, target), []
    week = find_week(weeks, target)
    if week is None:
        return None, []
    day = DAY_BY_INDEX[target.weekday()]
    all_days = (schedule or {}).get("days", {})
    rows = list(all_days.get(day, []))
    bounds = calendar_bounds(weeks)
    # Explicit portal dates are authoritative even when the source row sits in
    # a different weekday bucket (real pages contain such mismatches).
    for source_day, source_rows in all_days.items():
        if source_day == day:
            continue
        for item in source_rows:
            has_exact_dates, exact_dates = _explicit_lesson_dates(item, target, bounds)
            if has_exact_dates and target in exact_dates:
                rows.append(item)
    lessons = [normalise_lesson(item) for item in rows if lesson_applies_on(item, target, week, bounds)]
    lessons.sort(key=lambda item: (_lesson_start(item), _lesson_number(item)))
    return week, lessons


def day_view(schedule: dict | None, weeks: list[dict], target: dt.date, *, group: str, source_url: str) -> dict:
    week, lessons = lessons_for_date(schedule, weeks, target)
    day = DAY_BY_INDEX[target.weekday()]
    bounds = calendar_bounds(weeks)
    if week is None:
        reason = "дата вне учебного календаря"
        if bounds:
            reason += f" {bounds[0].strftime('%d.%m.%Y')}–{bounds[1].strftime('%d.%m.%Y')}"
        summary = f"Расписание группы {group} на {target.strftime('%d.%m.%Y')}: пар нет ({reason})."
        return {"group": group, "date": target.isoformat(), "day": day, "week": None, "lessons": [], "summary": summary, "source_url": source_url}

    half = WEEK_HALF_NAME.get(week.get("half"), str(week.get("half") or ""))
    lines = [
        f"Расписание группы {group} на {day.lower()}, {target.strftime('%d.%m.%Y')}",
        f"Учебная неделя {week['week']}: {half}",
        bells.LEGEND,
        "",
    ]
    if not lessons:
        lines.append("Пар нет.")
    for index, lesson in enumerate(lessons, 1):
        display_time = lesson.get("time_label") or "—"
        if display_time == "—":
            display_time = "время не указано"
        lines.append(f"{index}. {display_time} — {lesson['subject']}")
        details = []
        if lesson["room"] and lesson["room"] != "—":
            details.append(f"ауд. {lesson['room']}")
        if lesson["teacher"] and lesson["teacher"] != "—":
            details.append(lesson["teacher"])
        if details:
            lines.append("   " + "; ".join(details))
        if lesson["note"]:
            lines.append(f"   примечание: {lesson['note']}")
    return {
        "group": group,
        "date": target.isoformat(),
        "day": day,
        "week": week,
        "lessons": lessons,
        "summary": "\n".join(lines),
        "source_url": source_url,
    }


def week_view(schedule: dict | None, weeks: list[dict], week: dict, *, group: str, source_url: str) -> dict:
    bounds = _week_dates(week)
    if not bounds:
        raise ValueError("invalid week")
    start, end = bounds
    days: list[dict[str, Any]] = []
    cur = start
    while cur <= end:
        days.append(day_view(schedule, weeks, cur, group=group, source_url=source_url))
        cur += dt.timedelta(days=1)
    half = WEEK_HALF_NAME.get(week.get("half"), str(week.get("half") or ""))
    lines = [f"Расписание группы {group}: неделя {week['week']} ({half})", f"{week['start']} — {week['end']}", ""]
    for day in days:
        lessons = day["lessons"]
        lines.append(f"{day['day']}, {dt.date.fromisoformat(day['date']).strftime('%d.%m')}: {len(lessons)}")
        for lesson in lessons:
            lines.append(f"  {lesson.get('time_label') or lesson['time']} — {lesson['subject']}")
    return {"group": group, "week": week, "days": days, "summary": "\n".join(lines), "source_url": source_url}


def next_lessons(
    schedule: dict | None,
    weeks: list[dict],
    start_date: dt.date,
    *,
    start_time: str | None,
    group: str,
    source_url: str,
    limit: int = 3,
) -> dict:
    limit = min(max(int(limit or 3), 1), 20)
    if start_time:
        try:
            current_time = dt.datetime.strptime(str(start_time), "%H:%M").time()
        except (TypeError, ValueError) as exc:
            raise ValueError("start_time must be HH:MM") from exc
    else:
        current_time = dt.time.min
    bounds = calendar_bounds(weeks)
    if not bounds:
        return {"group": group, "lessons": [], "summary": "Учебный календарь не найден.", "source_url": source_url}
    cur = max(start_date, bounds[0])
    found: list[dict] = []
    while cur <= bounds[1] and len(found) < limit:
        view = day_view(schedule, weeks, cur, group=group, source_url=source_url)
        lessons = sorted(view["lessons"], key=lambda item: (_lesson_start(item), _lesson_number(item)))
        for lesson in lessons:
            # Одна ячейка «время» может содержать две пары — показываем обе.
            for pair_start, pair_lesson in lesson_pairs(lesson):
                if cur == start_date and pair_start < current_time:
                    continue
                found.append({"date": cur.isoformat(), "day": view["day"], **pair_lesson})
                if len(found) >= limit:
                    break
            if len(found) >= limit:
                break
        cur += dt.timedelta(days=1)
    lines = [f"Ближайшие пары группы {group}"]
    if not found:
        lines.append("Не нашёл будущих пар в опубликованном календаре.")
    for item in found:
        date_label = dt.date.fromisoformat(item["date"]).strftime("%d.%m")
        lines.append(
            f"{date_label}, {item['day'].lower()} — {item.get('time_label') or item['time']} — {item['subject']}"
        )
    return {"group": group, "lessons": found, "summary": "\n".join(lines), "source_url": source_url}
