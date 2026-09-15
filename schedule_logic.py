"""Date-aware timetable views for NovSU schedules."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import zoneinfo

DASHBOARD_PRESENTATION_VERSION = 11
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
_END_DATE_RE = re.compile(rf"(?<!с\s)\bпо\s+({_DATE_TOKEN})", re.I)
_WEEK_ORDINAL_SUFFIX = r"(?:-?(?:й|ой))?"
_AFTER_WEEK_RE = re.compile(rf"после\s+(\d+){_WEEK_ORDINAL_SUFFIX}\s+недел", re.I)
_UNTIL_WEEK_RE = re.compile(rf"до\s+(\d+){_WEEK_ORDINAL_SUFFIX}\s+недел", re.I)
# «с 10 недели» — портал так отмечает пары, которые начинаются не с первой
# учебной недели (реальный пример: «с 10 недели», «с 9 недели, с
# использованием ДОТ»). До этого фикса такие пары молча показывались с 1
# недели — сравнение было строгое неравенство (>) вместо (>=), потому что
# сама неделя N уже включена в диапазон действия пары.
_FROM_WEEK_RE = re.compile(rf"\bс\s+(\d+){_WEEK_ORDINAL_SUFFIX}\s+недел", re.I)


def parse_user_date(value: str | None, *, today: dt.date | None = None) -> dt.date:
    """Parse relative dates, ISO dates, and Russian DD.MM forms."""
    base = today or dt.datetime.now(zoneinfo.ZoneInfo("Europe/Moscow")).date()
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


def find_current_or_next_week(weeks: list[dict], target: dt.date) -> dict | None:
    """Select by portal calendar dates, independent of lessons and list order."""
    upcoming = None
    upcoming_start = None
    for week in weeks:
        bounds = _week_dates(week)
        if not bounds or bounds[0] > bounds[1]:
            continue
        start, end = bounds
        if start <= target <= end:
            return week
        if start > target and (upcoming_start is None or start < upcoming_start):
            upcoming, upcoming_start = week, start
    return upcoming


def find_week_by_number(weeks: list[dict], number: int) -> dict | None:
    for week in weeks:
        if week.get("week") == number:
            return week
    return None


def calendar_bounds(weeks: list[dict]) -> tuple[dt.date, dt.date] | None:
    bounds = [_week_dates(week) for week in weeks]
    valid = [item for item in bounds if item and item[0] <= item[1]]
    if not valid:
        return None
    starts, ends = zip(*valid)
    return min(starts), max(ends)


def visible_week_days(
    weeks: list[dict],
    target_date: dt.date,
    current_date: dt.date | None = None,
    *,
    schedule: dict | None = None,
    now: dt.datetime | None = None,
) -> list[str]:
    """Дни учебной недели фокуса, которые ещё видны в закрепе.

    Прошедшие дни скрыты. Сегодняшний день держится до конца последней пары,
    а не до полуночи (вариант 2): когда переданы ``schedule`` и ``now``, после
    конца последней пары день тоже исчезает. Без этих данных граница — 00:00 Мск.
    Неделю без дат не фильтруем: лучше показать всё, чем пустой пост.
    """
    current_date = current_date or dt.datetime.now(zoneinfo.ZoneInfo("Europe/Moscow")).date()
    week = find_week(weeks, target_date)
    bounds = _week_dates(week) if week else None
    if not bounds:
        return list(DAYS_ORDER)
    start = bounds[0]
    live: list[str] = []
    for index in range(7):
        day_date = start + dt.timedelta(days=index)
        if day_date > bounds[1]:
            break
        # Неделя не обязана начинаться в понедельник (01.09.2026 — вторник),
        # поэтому имя дня берём из реальной даты, а не из номера смещения.
        if day_date > current_date:
            live.append(DAY_BY_INDEX[day_date.weekday()])
            continue
        if day_date < current_date:
            continue
        # Сегодня: вариант 2 — после конца последней пары день тоже уезжает.
        if schedule is not None and now is not None:
            _, lessons = lessons_for_date(schedule, weeks, day_date)
            end = day_last_pair_end(lessons)
            if end is not None and now.time() >= end:
                continue
        live.append(DAY_BY_INDEX[day_date.weekday()])
    return live


def resolve_dashboard_view(
    schedule: dict | None,
    weeks: list[dict],
    now: dt.datetime | None = None,
) -> dict:
    """Resolve the next useful dashboard date and its still-live lesson days."""
    tz = zoneinfo.ZoneInfo("Europe/Moscow")
    if now is None:
        current = dt.datetime.now(tz)
    elif now.tzinfo is None:
        current = now.replace(tzinfo=tz)
    else:
        current = now.astimezone(tz)
    bounds = calendar_bounds(weeks)
    if not bounds:
        return {"target_date": current.date(), "week": None, "live_days": []}

    first, last = bounds
    cursor = max(first, current.date())
    target_date = None
    while cursor <= last:
        week, lessons = lessons_for_date(schedule, weeks, cursor)
        if week is not None and lessons:
            if cursor != current.date():
                target_date = cursor
                break
            end = day_last_pair_end(lessons)
            if end is None or current.time() < end:
                target_date = cursor
                break
        cursor += dt.timedelta(days=1)

    if target_date is None:
        return {"target_date": current.date(), "week": None, "live_days": []}
    week = find_week(weeks, target_date)
    live_days = visible_week_days(
        weeks,
        target_date,
        current.date(),
        schedule=schedule,
        now=current,
    )
    return {"target_date": target_date, "week": week, "live_days": live_days}


def dashboard_presentation_fingerprint(content_fingerprint: str, view: dict) -> str:
    """Hash only values whose change requires editing the pinned post."""
    week = view.get("week") or {}
    payload = {
        "version": DASHBOARD_PRESENTATION_VERSION,
        "content": content_fingerprint,
        "target_date": str(view.get("target_date") or ""),
        "week": {key: week.get(key) for key in ("week", "half", "start", "end")},
        "live_days": list(view.get("live_days") or []),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse_note_date(
    token: str,
    default_year: int,
    bounds: tuple[dt.date, dt.date] | None = None,
    *,
    strict: bool = False,
) -> dt.date | None:
    """Resolve a note date; strict mode forbids missing or ambiguous years."""
    m = _DATE_RE.fullmatch(token.strip())
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
    if year:
        if strict and len(year) not in {2, 4}:
            return None
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
        if strict:
            return candidates[0] if len(candidates) == 1 else None
        if candidates:
            return min(candidates, key=lambda item: abs(item.year - default_year))

    if strict:
        return None
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
    return lesson_non_applicability_reason(lesson, target, week, bounds) is None


def lesson_has_expired(
    lesson: dict,
    target: dt.date,
    week: dict | None,
    bounds: tuple[dt.date, dt.date] | None = None,
) -> bool:
    """Prove that explicit positive deadlines have passed, without editing a row.

    This is not non-applicability: parity, starts and cancellations never prove
    expiry. Yearless dates require unambiguous *full academic calendar* bounds,
    never just the selected week. Unknown/partial conditions keep the row.
    """
    if bounds is not None and (
        len(bounds) != 2
        or any(type(value) is not dt.date for value in bounds)
        or bounds[0] > bounds[1]
    ):
        return False

    expired: list[bool] = []
    date_parts = _date_condition_parts(lesson)
    condition_text = " ".join(date_parts)
    for pattern in (_UNTIL_WEEK_RE, _FROM_WEEK_RE, _AFTER_WEEK_RE):
        for match in pattern.finditer(condition_text):
            number = int(match.group(1))
            raw_week = str((week or {}).get("week") or "")
            if number < 1 or not re.fullmatch(r"[0-9]+", raw_week) or int(raw_week) < 1:
                return False
            current = int(raw_week)
            if pattern is _UNTIL_WEEK_RE:
                expired.append(current >= number)
            elif current < number or (pattern is _AFTER_WEEK_RE and current == number):
                return False

    for value in date_parts:
        # Do not silently discard an invalid member of an otherwise valid list.
        dates = {}
        for match in _DATE_RE.finditer(value):
            token = match.group(0)
            if re.match(r"\.?\w", value[match.end():]):
                return False
            parsed = _parse_note_date(token, target.year, bounds, strict=True)
            if parsed is None:
                return False
            dates[token] = parsed

        text = _CANCELLED_DATES_RE.sub(" ", value)
        # Unsupported negations/cancellation wording cannot supply a positive
        # deadline. In particular, a cancelled range is not an occurrence range.
        if re.search(r"\b(?:не|нет|без|кроме|отмен\w*|перен\w*|исключ\w*|или)\b", text, re.I):
            return False

        for match in _RANGE_RE.finditer(text):
            start = dates[match.group("from") or match.group("dash_from")]
            end = dates[match.group("to") or match.group("dash_to")]
            # Calendar-based resolution already handles Dec-Jan. Do not turn
            # a reversed/malformed range into an invented extra year.
            if end < start:
                return False
            expired.append(target > end)
        text = _RANGE_RE.sub(" ", text)

        for match in _START_DATE_RE.finditer(text):
            if target < dates[match.group(1)]:
                return False
        text = _START_DATE_RE.sub(" ", text)
        for match in _END_DATE_RE.finditer(text):
            expired.append(target > dates[match.group(1)])
        text = _END_DATE_RE.sub(" ", text)

        for pattern in (_ONLY_DATES_RE, _DATE_LIST_RE):
            for match in pattern.finditer(text):
                expired.extend(
                    target > dates[token.group(0)]
                    for token in _DATE_RE.finditer(match.group("dates"))
                )
            text = pattern.sub(" ", text)

        for match in _START_MONTH_RE.finditer(text):
            month = _MONTHS[match.group(1).lower()]
            start = _parse_note_date(f"01.{month:02d}", target.year, bounds, strict=True)
            if start is None or target < start:
                return False
        text = _START_MONTH_RE.sub(" ", text)
        for pattern in (_UNTIL_WEEK_RE, _FROM_WEEK_RE, _AFTER_WEEK_RE):
            text = pattern.sub(" ", text)
        text = re.sub(r"\bпо\s+(?:верхней|нижней)\s+недел[еи]\b", " ", text, flags=re.I)
        # This known delivery phrase is not a date-start condition.
        text = re.sub(r"\bс\s+использованием\s+ДОТ\b", " ", text, flags=re.I)
        # Leftover dates or condition markers mean a list/range was only partly
        # understood. Ordinary titles and location prose do not imply expiry.
        if re.search(
            r"\d[./-]|(?:[,;]|\bи\b)\s*\d|"
            r"\b(?:только|с|по|до|после|недел\w*|уточн\w*|далее|позже|ежегодно)\b",
            text, re.I,
        ):
            return False

    return bool(expired) and all(expired)


def _short_date(value: dt.date) -> str:
    return value.strftime("%d.%m")


def _week_condition_tag(prefix: str, number: int) -> str:
    # Non-breaking hyphen keeps ``9‑Й`` together in narrow screenshot cells.
    return f"НЕ НА ЭТОЙ НЕДЕЛЕ · {prefix} {number}‑Й НЕДЕЛИ"


def lesson_non_applicability_struct(
    lesson: dict,
    target: dt.date,
    week: dict | None,
    bounds: tuple[dt.date, dt.date] | None = None,
) -> dict | None:
    """Return structured non-applicability reason for UI rendering.

    Returns None if lesson is applicable. Otherwise returns dict:
    {
        "kind": "parity" | "from_week" | "after_week" | "until_week" | "starts_date" | "ends_date" | "cancelled" | "only_dates" | "outside_calendar",
        "display_text": str,  # Short human-readable label for screenshot badge
        "machine_reason": str,  # Existing full string from lesson_non_applicability_reason()
        "data": {...}  # Structured data (week_number, date, parity, etc.)
    }
    """
    machine_reason = lesson_non_applicability_reason(lesson, target, week, bounds)
    if machine_reason is None:
        return None

    if week is None:
        return {
            "kind": "outside_calendar",
            "display_text": "ВНЕ КАЛЕНДАРЯ",
            "machine_reason": machine_reason,
            "data": {},
        }

    if bounds is None:
        bounds = _week_dates(week)

    condition_text = _condition_text(lesson)
    low = condition_text.lower()
    week_number = int(week.get("week") or 0)

    # Check week-based conditions first
    after_match = _AFTER_WEEK_RE.search(low)
    if after_match and week_number <= int(after_match.group(1)):
        n = int(after_match.group(1))
        return {
            "kind": "after_week",
            "display_text": f"С {n + 1}-Й НЕДЕЛИ",
            "machine_reason": machine_reason,
            "data": {"week_number": n, "effective_week": n + 1},
        }

    until_match = _UNTIL_WEEK_RE.search(low)
    if until_match and week_number >= int(until_match.group(1)):
        n = int(until_match.group(1))
        return {
            "kind": "until_week",
            "display_text": f"ДО {n}-Й НЕДЕЛИ",
            "machine_reason": machine_reason,
            "data": {"week_number": n},
        }

    from_match = _FROM_WEEK_RE.search(low)
    if from_match and week_number < int(from_match.group(1)):
        n = int(from_match.group(1))
        return {
            "kind": "from_week",
            "display_text": f"С {n}-Й НЕДЕЛИ",
            "machine_reason": machine_reason,
            "data": {"week_number": n},
        }

    # Check parity
    half = week.get("half")
    parity = lesson_parity(lesson)
    if parity in {"top", "bottom"} and half != parity:
        label = "ВЕРХНЯЯ НЕДЕЛЯ" if parity == "top" else "НИЖНЯЯ НЕДЕЛЯ"
        return {
            "kind": "parity",
            "display_text": label,
            "machine_reason": machine_reason,
            "data": {"parity": parity, "week_half": half},
        }

    # Check date-based conditions
    date_parts = _date_condition_parts(lesson)

    # Check cancelled dates
    cancelled_dates: set[dt.date] = set()
    for text in date_parts:
        for match in _CANCELLED_DATES_RE.finditer(text):
            cancelled_dates.update(_parsed_dates(match.group("dates"), target.year, bounds))
    if target in cancelled_dates:
        return {
            "kind": "cancelled",
            "display_text": f"ОТМЕНЕНО {_short_date(target)}",
            "machine_reason": machine_reason,
            "data": {"date": target.isoformat()},
        }

    # Check date ranges
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
        start, end = ranges[0]
        return {
            "kind": "starts_date",
            "display_text": f"С {_short_date(start)}",
            "machine_reason": machine_reason,
            "data": {"start_date": start.isoformat(), "end_date": end.isoformat()},
        }

    # Check start dates
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
        return {
            "kind": "starts_date",
            "display_text": f"С {_short_date(max(start_dates))}",
            "machine_reason": machine_reason,
            "data": {"start_date": max(start_dates).isoformat()},
        }

    # Check end dates
    end_dates = [
        parsed
        for text in without_ranges
        for parsed in (
            _parse_note_date(match.group(1), target.year, bounds)
            for match in _END_DATE_RE.finditer(text)
        )
        if parsed is not None
    ]
    if end_dates and target > min(end_dates):
        return {
            "kind": "ends_date",
            "display_text": f"ПО {_short_date(min(end_dates))}",
            "machine_reason": machine_reason,
            "data": {"end_date": min(end_dates).isoformat()},
        }

    # Check exact dates
    has_exact_context, exact_dates = _explicit_lesson_dates(lesson, target, bounds)
    if has_exact_context:
        if target in exact_dates:
            return None
        return {
            "kind": "only_dates",
            "display_text": "ТОЛЬКО ПО ДАТАМ",
            "machine_reason": machine_reason,
            "data": {"dates": sorted(d.isoformat() for d in exact_dates)},
        }

    # Fallback: unknown reason
    return {
        "kind": "outside_calendar",
        "display_text": "ВНЕ КАЛЕНДАРЯ",
        "machine_reason": machine_reason,
        "data": {},
    }


def lesson_non_applicability_reason(
    lesson: dict,
    target: dt.date,
    week: dict | None,
    bounds: tuple[dt.date, dt.date] | None = None,
) -> str | None:
    """Explain why a lesson is inactive on ``target``; ``None`` means active.

    This is the same evaluator used by ``lesson_applies_on``. Keeping the
    contextual reason here prevents screenshot code from guessing whether an
    inactive row failed parity, an academic-week boundary, or a calendar date.
    """
    if week is None:
        return "ВНЕ УЧЕБНОГО КАЛЕНДАРЯ"
    if bounds is None:
        bounds = _week_dates(week)

    condition_text = _condition_text(lesson)
    low = condition_text.lower()

    week_number = int(week.get("week") or 0)
    after_match = _AFTER_WEEK_RE.search(low)
    if after_match and week_number <= int(after_match.group(1)):
        return _week_condition_tag("ПОСЛЕ", int(after_match.group(1)))
    until_match = _UNTIL_WEEK_RE.search(low)
    if until_match and week_number >= int(until_match.group(1)):
        return _week_condition_tag("ДО", int(until_match.group(1)))
    from_match = _FROM_WEEK_RE.search(low)
    if from_match and week_number < int(from_match.group(1)):
        return _week_condition_tag("С", int(from_match.group(1)))

    half = week.get("half")
    parity = lesson_parity(lesson)
    if parity in {"top", "bottom"} and half != parity:
        label = "ВЕРХНЯЯ" if parity == "top" else "НИЖНЯЯ"
        return f"НЕ НА ЭТОЙ НЕДЕЛЕ · ТОЛЬКО {label}"

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
        start, end = ranges[0]
        return f"НЕ В ЭТУ ДАТУ · {_short_date(start)}–{_short_date(end)}"

    cancelled_dates: set[dt.date] = set()
    for text in date_parts:
        for match in _CANCELLED_DATES_RE.finditer(text):
            cancelled_dates.update(_parsed_dates(match.group("dates"), target.year, bounds))
    if target in cancelled_dates:
        return f"ЗАНЯТИЯ НЕ БУДЕТ · {_short_date(target)}"

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
        return f"НЕ В ЭТУ ДАТУ · С {_short_date(max(start_dates))}"

    # A bare "по DD.MM" (no matching "с DD.MM" range) marks the last date the
    # lesson happens — portal example: «По 30.09», «ул. Псковская д.3 по
    # нижней неделе по 21.10». Ranges are already stripped from
    # ``without_ranges``, and «по верхней/нижней неделе» never matches
    # because it has no digits, so this only catches the standalone form.
    end_dates = [
        parsed
        for text in without_ranges
        for parsed in (
            _parse_note_date(match.group(1), target.year, bounds)
            for match in _END_DATE_RE.finditer(text)
        )
        if parsed is not None
    ]
    if end_dates and target > min(end_dates):
        return f"НЕ В ЭТУ ДАТУ · ПО {_short_date(min(end_dates))}"

    has_exact_context, exact_dates = _explicit_lesson_dates(lesson, target, bounds)
    if has_exact_context:
        if target in exact_dates:
            return None
        dates = sorted(exact_dates)
        suffix = ", ".join(_short_date(value) for value in dates[:4])
        if len(dates) > 4:
            suffix += "…"
        return f"ТОЛЬКО ПО ДАТАМ · {suffix}" if suffix else "ТОЛЬКО ПО УКАЗАННЫМ ДАТАМ"

    return None


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


def day_last_pair_end(lessons: list[dict]) -> dt.time | None:
    """Конец последней пары дня по сетке портала.

    Ячейка «14:00 15:00 16:00 17:00» — две пары, последняя кончается в 17:45.
    По этому моменту день уезжает из закрепа, а не в полночь.
    """
    ends: list[dt.time] = []
    for lesson in lessons:
        for _, pair in lesson_pairs(lesson):
            raw = pair.get("time_end")
            try:
                ends.append(dt.time.fromisoformat(str(raw)))
            except (TypeError, ValueError):
                continue
    return max(ends) if ends else None


def day_is_live(day: dict, now: dt.datetime) -> bool:
    """День ещё виден: прошедшие скрыты, сегодня — пока идёт последняя пара.

    ``now`` — локальное московское время (naive) или осведомлённое о зоне.
    """
    try:
        when = dt.date.fromisoformat(str(day.get("date") or ""))
    except ValueError:
        return True
    if when < now.date():
        return False
    if when > now.date():
        return True
    end = day_last_pair_end(day.get("lessons") or [])
    if end is None:
        return True  # без читаемых пар день не скрываем: иначе пустой пост
    return now.time() < end


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
    return {
        "number": lesson.get("number"),
        "subject": subject,
        "time": lesson.get("time") or "—",
        **time_labels(lesson),
        "room": lesson.get("room") or "—",
        "teacher": lesson.get("teacher") or "—",
        "location": lesson.get("location") or "",
        "delivery_mode": lesson.get("delivery_mode") or "",
        "note": note,
        "raw_note": lesson.get("note") or lesson.get("raw_comment") or "",
        "parity": lesson_parity(lesson),
    }


def _load_holidays() -> set[dt.date]:
    """Load academic holidays from versioned knowledge data.

    Falls back to a known default if the file is missing or malformed.
    This keeps year-specific exceptions out of the core algorithm while
    preserving the 2026-09-01 regression behavior.
    """
    import pathlib
    path = pathlib.Path(__file__).resolve().parent / "knowledge" / "holidays.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        dates: set[dt.date] = set()
        for entry in data.get("holidays", []):
            raw = str(entry.get("date") or "").strip()
            if raw:
                dates.add(dt.date.fromisoformat(raw))
        return dates
    except (OSError, ValueError, TypeError, KeyError):
        return {dt.date(2026, 9, 1)}


# Даты без регулярных пар (День знаний и т.п.). Загружаются из
# knowledge/holidays.json; fallback сохраняет поведение 01.09.2026.
HOLIDAYS = _load_holidays()


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
