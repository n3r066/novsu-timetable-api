"""Schedule JSON -> Telegram InputRichMessage (Bot API 10.2)."""
from __future__ import annotations

import datetime as dt
import html as _html
import json
import re
import urllib.error
import urllib.request
import zoneinfo
from pathlib import Path

import bells
import opd as opd_module
from locations import resolve_location
from schedule_logic import (
    calendar_bounds,
    day_is_live,
    day_view,
    find_week as find_schedule_week,
    lesson_has_expired,
    week_view,
)

WEEK_HALF_NAME = {"top": "верхняя", "bottom": "нижняя"}
DAY_SHORT_TO_FULL = {
    "Пн": "Понедельник",
    "Вт": "Вторник",
    "Ср": "Среда",
    "Чт": "Четверг",
    "Пт": "Пятница",
    "Сб": "Суббота",
    "Вс": "Воскресенье",
}
DAY_FULL_TO_SHORT = {full: short for short, full in DAY_SHORT_TO_FULL.items()}
DAYS_ORDER = [
    "Понедельник", "Вторник", "Среда", "Четверг",
    "Пятница", "Суббота", "Воскресенье",
]

_DEFAULT_NOTE_POLICY = {
    "ordinary_locations": ["Антоново", "ИГУМ, Антоново", "Б.С.-Петербургская, 41"],
    "language_variants": ["немец. яз.", "англ. яз.", "франц. яз."],
}


def _load_note_policy() -> dict:
    path = Path(__file__).with_name("knowledge") / "schedule_notes.json"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return _DEFAULT_NOTE_POLICY
    return {
        "ordinary_locations": loaded.get("ordinary_locations") or _DEFAULT_NOTE_POLICY["ordinary_locations"],
        "language_variants": loaded.get("language_variants") or _DEFAULT_NOTE_POLICY["language_variants"],
    }


_NOTE_POLICY = _load_note_policy()


def find_current_week(weeks: list[dict], today: dt.date | None = None) -> dict | None:
    today = today or dt.datetime.now(zoneinfo.ZoneInfo("Europe/Moscow")).date()
    for week in weeks:
        try:
            start = dt.datetime.strptime(week["start"], "%d.%m.%Y").date()
            end = dt.datetime.strptime(week["end"], "%d.%m.%Y").date()
        except (KeyError, TypeError, ValueError):
            continue
        if start <= today <= end:
            return week
    return None


def _rt(text: object, style: str = "bold") -> dict:
    value = str(text or "—")
    if style == "accent":
        # Nested RichText: highlighted background with a bold subject name.
        return {"type": "marked", "text": {"type": "bold", "text": value}}
    return {"type": style, "text": value}


def _paragraph(text: str) -> dict:
    return {"type": "paragraph", "text": text}


def _rich_paragraph(parts: list[object]) -> dict:
    return {"type": "paragraph", "text": parts}


def _heading(text: str, size: int = 3) -> dict:
    return {"type": "heading", "size": size, "text": text}


def _note(subject: str, condition: str) -> dict:
    """Readable two-line note: bold subject, then marked condition."""
    return {
        "type": "paragraph",
        "text": [
            {"type": "bold", "text": f"• {subject}"},
            "\n",
            {"type": "marked", "text": condition},
        ],
    }


def _divider() -> dict:
    return {"type": "divider"}


def _pullquote(text: object) -> dict:
    return {"type": "pullquote", "text": text}


def _photo(media: str, caption: object = "") -> dict:
    block = {"type": "photo", "photo": {"type": "photo", "media": media}}
    if caption:
        block["caption"] = {"text": caption}
    return block


def _slideshow(photos: list[dict], caption: object = "") -> dict:
    block = {"type": "slideshow", "blocks": photos}
    if caption:
        block["caption"] = {"text": caption}
    return block


def _table_cell(
    text: object,
    style: str,
    *,
    is_header: bool = False,
    rowspan: int | None = None,
) -> dict:
    """Build a RichBlockTableCell using the Bot API wire schema.

    Table cells are not blocks and therefore have no ``type`` field.  The API
    requires both alignment fields; ``is_header`` is emitted only for actual
    heading cells.
    """
    if isinstance(text, list):
        text_items = text
    elif style == "plain":
        text_items = [str(text or "\u2014")]
    else:
        text_items = [_rt(text, style)]
    cell = {
        "text": text_items,
        "align": "left",
        "valign": "middle",
    }
    if is_header:
        cell["is_header"] = True
    if rowspan is not None:
        cell["rowspan"] = rowspan
    return cell


def _table(rows: list[list[tuple[object, str]]]) -> dict:
    return {
        "type": "table",
        "cells": [
            [_table_cell(text, style, is_header=row_index == 0) for text, style in row]
            for row_index, row in enumerate(rows)
        ],
        "is_bordered": True,
        "is_striped": True,
    }


def _details(header: object, *blocks: dict) -> dict:
    return {"type": "details", "summary": header, "blocks": list(blocks)}


def _details_open(header: object, *blocks: dict) -> dict:
    return {"type": "details", "summary": header, "is_open": True, "blocks": list(blocks)}


def _pairs_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "пара"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "пары"
    return "пар"


def _week_summary(dropped_past: bool = False) -> list[object]:
    # Прошедшие дни из закрепа исчезают, поэтому «полным» расписание бывает
    # только в начале недели — иначе подпись врёт читателю.
    label = "Расписание до конца недели" if dropped_past else "Полное расписание"
    return [
        {"type": "marked", "text": {"type": "bold", "text": label}},
    ]


def _notes_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "примечание"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "примечания"
    return "примечаний"


def _normalise_pair(row: object, index: int) -> list[str]:
    """Return [number, subject, time, room, teacher] from parser or demo rows."""
    if isinstance(row, dict):
        return [
            str(row.get("number") or row.get("pair") or index),
            str(row.get("subject") or row.get("name") or "—"),
            str(row.get("time") or "—"),
            str(row.get("room") or row.get("auditorium") or "—"),
            str(row.get("teacher") or "—"),
        ]
    values = [str(value).strip() for value in row] if isinstance(row, (list, tuple)) else [str(row)]
    if len(values) >= 5:
        return values[:5]
    if len(values) == 4:
        # Legacy parser shape: number, time, room, teacher.
        return [values[0], "—", values[1], values[2], values[3]]
    return (values + ["—"] * 5)[:5]


def _time_display(lesson: dict) -> str:
    """Интервалы пар для вывода: одна пара — одна строка.

    Портал печатает в колонке «время» начала академических часов, поэтому
    «9:00 10:00» — это пара 09:00–10:45, а «14:00 15:00 16:00 17:00» — две
    пары. Арифметику делает bells.py; нормализованные уроки уже несут готовые
    подписи, сырые строки парсера разбираются на месте.
    """
    prepared = lesson.get("time_cell")
    if prepared:
        return re.sub(r"\s*\(\d+\s+ак\.\s*ч\.\)\s*$", "", str(prepared), flags=re.I)
    return bells.slot(lesson.get("time")).label_cell()


def _lesson_fields(pair: dict, index: int) -> dict:
    """Extract display fields from a parser lesson record."""
    full = pair.get("subject") or pair.get("subject_raw") or pair.get("name") or ""
    subject = full.split("\n", 1)[0] if full else "—"
    return {
        "number": str(pair.get("number") or index),
        "subject": subject,
        "time": _time_display(pair),
        "time_plain": bells.slot(pair.get("time")).label(),
        "room": pair.get("room") or "—",
        "teacher": pair.get("teacher") or "—",
        "note": pair.get("note") or pair.get("raw_comment") or "",
    }


_PARITY_STRIP_RE = re.compile(r"\s*по\s+(?:верхней|нижней)\s+недел[еи]\s*", re.I)


def _parity_of(note: str) -> str:
    low = note.lower()
    if "верхн" in low:
        return "upper"
    if "нижн" in low:
        return "lower"
    return "every"


def _strip_parity(note: str) -> str:
    """Drop the redundant «по верхней/нижней неделе» phrase (the group label
    already states the parity) and tidy spacing."""
    s = _PARITY_STRIP_RE.sub(" ", note)
    s = re.sub(r"\s+,", ",", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip(" ,;")


def _parity_label(parity: str, count: int | None = None) -> dict:
    suffix = f" — {count} {_pairs_word(count)}" if count is not None else ""
    if parity == "upper":
        return _rich_paragraph([
            {"type": "bold", "text": f"Верхняя неделя{suffix}"},
            "  ·  1, 3, 5, 7… нечётные",
        ])
    if parity == "lower":
        return _rich_paragraph([
            {"type": "bold", "text": f"Нижняя неделя{suffix}"},
            "  ·  2, 4, 6, 8… чётные",
        ])
    return _rich_paragraph([{"type": "bold", "text": f"Каждую неделю{suffix}"}])


def _parity_word(parity: str) -> str:
    return {"upper": "верхней", "lower": "нижней", "every": "каждую"}[parity]


#: Короткие названия недели для строки «где» внутри одной карточки правки.
_WEEK_SHORT = {"upper": "верхняя неделя", "lower": "нижняя неделя", "every": "каждую неделю"}


def _lesson_place(item: dict) -> str:
    """Место пары для таблицы дня: аудитория, а у дистанта — сам формат."""
    room = str(item.get("room") or "").strip().strip(".").strip()
    if room.casefold() not in _EMPTY_ROOM:
        return room
    mode = str(item.get("delivery_mode") or "").strip().casefold()
    if mode.startswith("remote") or _DOT_RE.search(str(item.get("note") or "")):
        return "дистанционно"
    return "—"


def _day_note(item: dict) -> str:
    """Примечание дня без фразы про ДОТ — формат уже показан в «место»."""
    note = str(item.get("note") or "").strip()
    note = _DOT_RE.sub(" ", note)
    return re.sub(r"\s{2,}", " ", note).strip(" ,;.")


def _lesson_table(lessons: list[dict]) -> dict:
    body = [[
        ("№", "bold"), ("предмет", "bold"), ("время", "bold"),
        ("место", "bold"), ("преподаватель", "bold"),
    ]]
    for index, lesson in enumerate(lessons, 1):
        subject = lesson.get("subject") or "—"
        if _day_note(lesson):
            subject += " *"
        body.append([
            (lesson.get("number") or index, "code"),
            (subject, "accent"),
            (_time_display(lesson), "code"),
            (_lesson_place(lesson), "code"),
            (lesson.get("teacher") or "—", "bold"),
        ])
    return _table(body)


def _day_blocks(
    day: dict,
    *,
    open_details: bool = False,
    screenshot_media: str | None = None,
    is_today: bool = False,
    source_url: str = "",
) -> dict:
    lessons = day.get("lessons") or []
    date = dt.date.fromisoformat(day["date"])
    title = f"{day['day']} — {date.strftime('%d.%m')}: {len(lessons)} {_pairs_word(len(lessons))}"
    blocks: list[dict] = []
    if not lessons:
        blocks.append(_paragraph("Пар нет."))
        if screenshot_media:
            blocks.append(_screenshot_block(day["day"], screenshot_media, is_today, source_url))
    else:
        blocks.append(_lesson_table(lessons))
        notes = [
            (item.get("subject") or "—", _day_note(item))
            for item in lessons
            if _day_note(item)
        ]
        note_blocks = [_note(subject, note) for subject, note in notes]
        if screenshot_media:
            scr = _screenshot_block(day["day"], screenshot_media, is_today, source_url)
            if notes:
                note_blocks.append(scr)
            else:
                blocks.append(scr)
        if note_blocks:
            blocks.append(_details(
                f"Примечания — {len(notes)} {_notes_word(len(notes))}",
                *note_blocks,
            ))
    return (_details_open if open_details else _details)(title, *blocks)


def _screenshot_block(day_label: str, media: str, is_today: bool, source_url: str) -> dict:
    caption = [
        {"type": "bold", "text": day_label},
        " · оригинальное ",
        {"type": "url", "text": "расписание", "url": source_url},
    ]
    details = _details_open if is_today else _details
    return details(f"Скрин: {day_label}", _photo(media, caption))


def _screenshot_photo(day_label: str, media: str, source_url: str, *, hidden_expired: bool = False) -> dict:
    """Подпись к скрину дня. Про скрытые прошедшие разовые занятия говорим только
    когда на этом дне скрин действительно что-то скрыл (см. _day_hides_expired)."""
    caption: list[object] = [{"type": "bold", "text": day_label}]
    caption.append(" · прошедшие разовые занятия скрыты · " if hidden_expired else " · ")
    caption.append({"type": "url", "text": "полный оригинал", "url": source_url})
    return _photo(media, caption)


def _day_hides_expired(schedule: dict | None, weeks: list[dict], day_date: dt.date) -> bool:
    """Есть ли у дня строки, которые скрин закрепа опускает как окончательно
    прошедшие. Тот же критерий, что у фильтра скриншотов: lesson_has_expired()
    с границами всего учебного календаря."""
    week = find_schedule_week(weeks, day_date)
    if not week:
        return False
    bounds = calendar_bounds(weeks)
    weekday = DAYS_ORDER[day_date.weekday()]
    rows = ((schedule or {}).get("days") or {}).get(weekday) or []
    return any(lesson_has_expired(lesson, day_date, week, bounds) for lesson in rows)


def _dashboard_title(
    group_name: str,
    target_date: dt.date,
    target_week: dict | None,
    last_updated: str,
    schedule_changed: str = "",
    sleep_note: str = "",
) -> dict:
    parts: list[object] = [
        {"type": "bold", "text": f"Расписание · группа {group_name}"},
    ]
    if target_week:
        half = WEEK_HALF_NAME.get(target_week.get("half"), target_week.get("half", ""))
        parity = "чётная" if target_week.get("half") == "bottom" else "нечётная"
        start = dt.datetime.strptime(target_week["start"], "%d.%m.%Y").date()
        end = dt.datetime.strptime(target_week["end"], "%d.%m.%Y").date()
        month_names = (
            "января", "февраля", "марта", "апреля", "мая", "июня",
            "июля", "августа", "сентября", "октября", "ноября", "декабря",
        )
        if start.month == end.month:
            date_range = f"{start.day}–{end.day} {month_names[start.month - 1]}"
        else:
            date_range = (
                f"{start.day} {month_names[start.month - 1]} – "
                f"{end.day} {month_names[end.month - 1]}"
            )
        parts += [
            "\n",
            {"type": "marked", "text": {
                "type": "bold",
                "text": f"НЕДЕЛЯ {target_week['week']} · {half.upper()}",
            }},
            "\n",
            f"{date_range} · {parity} учебная неделя",
        ]
    else:
        parts += ["\n", target_date.strftime("%d.%m.%Y")]
    if last_updated:
        parts += ["\n", {"type": "italic", "text": f"Обновлено: {last_updated} МСК"}]
    if schedule_changed and schedule_changed != last_updated:
        parts += ["\n", {"type": "italic", "text": f"Расписание: {schedule_changed} МСК"}]
    if sleep_note:
        parts += ["\n", {"type": "marked", "text": sleep_note}]
    return _pullquote(parts)


def _dashboard_is_dot(lesson: dict) -> bool:
    mode = str(lesson.get("delivery_mode") or "").casefold()
    return mode.startswith("remote") or bool(_DOT_RE.search(str(lesson.get("note") or "")))


def _dashboard_counts(days: list[dict]) -> tuple[int, int]:
    lessons = [lesson for day in days for lesson in (day.get("lessons") or [])]
    dot = sum(_dashboard_is_dot(lesson) for lesson in lessons)
    return len(lessons) - dot, dot


def _dashboard_count_line(
    week_days: list[dict],
    live_days: list[dict] | None = None,
    *,
    dropped_past: bool = False,
) -> dict:
    """«На неделе: 16 пар · 2 ДОТ»; когда прошедшие дни уже ушли из закрепа —
    ещё и «осталось: 7 пар», чтобы недельный итог не пропадал к четвергу."""
    in_person, dot = _dashboard_counts(week_days)
    total = in_person + dot
    parts: list[object] = [
        {"type": "bold", "text": "На неделе: "},
        {"type": "bold", "text": f"{total} {_pairs_word(total)}"},
    ]
    if dot:
        parts += ["  ·  ", {"type": "marked", "text": {"type": "bold", "text": f"{dot} ДОТ"}}]
    if dropped_past and live_days is not None:
        left_in_person, left_dot = _dashboard_counts(live_days)
        left = left_in_person + left_dot
        parts += [
            "  ·  ",
            {"type": "bold", "text": "осталось: "},
            {"type": "bold", "text": f"{left} {_pairs_word(left)}"},
        ]
        if left_dot:
            parts += [f" ({left_dot} ДОТ)"]
    return _rich_paragraph(parts)


def _dashboard_day_summary(day: dict) -> list[object]:
    lessons = day.get("lessons") or []
    in_person, dot = _dashboard_counts([day])
    date = dt.date.fromisoformat(day["date"])
    parts: list[object] = [
        {"type": "marked", "text": {
            "type": "bold",
            "text": f"{day['day'].upper()} · {date.strftime('%d.%m')}",
        }},
        f"  ·  {len(lessons)} {_pairs_word(len(lessons))}",
    ]
    if dot:
        parts += [f"  ·  {dot} ДОТ"]
    return parts


_SIMPLE_DATE_CONDITION_RE = re.compile(
    r"^(?:с|по|только)\s+\d{1,2}\.\d{1,2}(?:\.\d{2,4})?\.?$",
    re.I,
)
_SUBJECT_DATE_SUFFIX_RE = re.compile(
    r"^(.*?)\s+(с\s+\d{1,2}\.\d{1,2}(?:\.\d{2,4})?\.?)$",
    re.I,
)


def _dashboard_note_display(lesson: dict) -> tuple[list[str], str]:
    """Return short inline facts and the exceptional remainder of a note."""
    note = _strip_parity(_day_note(lesson))
    location = str(lesson.get("location") or "").strip()
    ordinary_locations = {str(item).casefold() for item in _NOTE_POLICY["ordinary_locations"]}
    exceptional_location = location if location and location.casefold() not in ordinary_locations else ""
    if location:
        note = re.sub(rf"(?:^|[,;]\s*){re.escape(location)}(?=$|[,;])", " ", note, flags=re.I)
    note = re.sub(r"\s{2,}", " ", note).strip(" ,;.")
    if not note:
        return [], exceptional_location

    language_variants = {str(item).casefold().strip(" .") for item in _NOTE_POLICY["language_variants"]}
    if note.casefold().strip(" .") in language_variants:
        return [note.rstrip(".")], exceptional_location
    if _SIMPLE_DATE_CONDITION_RE.fullmatch(note):
        return [note.rstrip(".")], exceptional_location
    # Lists of dates, cancellations, ranges and unknown portal prose are kept
    # visible, but only in the exceptional section for that day.
    exceptional = "; ".join(item for item in (exceptional_location, note) if item)
    return [], exceptional


#: Портальная пара ОПД — общий слот на всю группу; реальное место в разделе по ВГ.
_OPD_SUBJECT_RE = re.compile(r"проектн\w*\s+деятельност", re.I)


def _dashboard_lesson_table(lessons: list[dict], *, opd_hint: bool = False) -> dict:
    table = _table([[
        ("№", "bold"), ("занятие", "bold"), ("место / время", "bold"),
    ]])
    rows = table["cells"]
    for index, lesson in enumerate(lessons, 1):
        subject = str(lesson.get("subject") or "—")
        teacher = str(lesson.get("teacher") or "—")
        kind = ""
        match = re.match(r"^\(([^)]+)\)\s*(.*)$", subject)
        if match:
            kind, subject = match.group(1).upper(), match.group(2)
        inline_facts: list[str] = []
        date_suffix = _SUBJECT_DATE_SUFFIX_RE.match(subject)
        if date_suffix:
            subject, condition = date_suffix.groups()
            inline_facts.append(condition.rstrip("."))
        note_facts, _ = _dashboard_note_display(lesson)
        inline_facts.extend(fact for fact in note_facts if fact not in inline_facts)
        subject_cell: list[object] = []
        if kind:
            subject_cell += [{"type": "code", "text": kind}, "\n"]
        subject_cell.append({"type": "bold", "text": subject})
        if inline_facts:
            subject_cell += [
                "  ·  ",
                {"type": "code", "text": " · ".join(inline_facts)},
            ]
        subject_cell += ["\n", {"type": "italic", "text": teacher}]
        if _dashboard_is_dot(lesson):
            format_line: list[object] = [
                {"type": "marked", "text": {"type": "bold", "text": "ДОТ"}},
            ]
        else:
            place = _lesson_place(lesson)
            if place == "—" and opd_hint and _OPD_SUBJECT_RE.search(subject):
                # Слот ОПД без аудитории: кто куда идёт — в разделе по ВГ ниже.
                format_line = [
                    {"type": "bold", "text": "по ВГ"},
                    "\n",
                    {"type": "italic", "text": "см. раздел ОПД ниже"},
                ]
            else:
                if place == "—":
                    place = str(lesson.get("location") or "место уточняется")
                format_line = [{"type": "bold", "text": place}]
                location = str(lesson.get("location") or "").strip()
                if location and location.casefold() not in place.casefold():
                    format_line += ["\n", {"type": "italic", "text": location}]
        rows.append([
            _table_cell(index, "code", rowspan=2),
            _table_cell(subject_cell, "plain", rowspan=2),
            _table_cell(format_line, "plain"),
        ])
        rows.append([_table_cell([{"type": "code", "text": _time_display(lesson)}], "plain")])
    return table


def _opd_summary(view: dict) -> list[object]:
    date = dt.date.fromisoformat(view["date"])
    counts = view["counts"]
    total = len(view["rows"])
    return [
        {"type": "marked", "text": {"type": "bold", "text": "ОПД · ПО ВИРТУАЛЬНЫМ ГРУППАМ"}},
        f"  ·  {date.strftime('%d.%m')}  ·  идут {counts['session']} из {total}",
    ]


def _opd_person_summary(row: dict, building: str) -> list[object]:
    """Заголовок раздела студента: кто, ВГ, слот, аудитория, преподаватель."""
    parts: list[object] = [
        {"type": "bold", "text": row["student"]},
        f"  ·  ВГ {row['vg']}  ·  ",
        {"type": "code", "text": f"{row['block_start']}–{row['block_end']}"},
    ]
    if row.get("note"):
        parts += [" ", {"type": "marked", "text": row["note"]}]
    where = f"ауд. {row['room']}" if row.get("room") else "аудитория уточняется"
    hint = opd_module.place_hint(row.get("place", ""), building)
    parts += ["  ·  ", {"type": "bold", "text": where + (f" · {hint}" if hint else "")}]
    if row.get("teacher_short"):
        parts += ["  ·  ", {"type": "italic", "text": row["teacher_short"]}]
    return parts


def _opd_group_sort_key(group: object) -> tuple:
    """Учебные группы: числовые по значению, затем текстовые, пустые в конец.

    «02» и «2» — разные подписи и никогда не сливаются: сортировка по точной
    строке, а не по одному числу.
    """
    label = str(group or "").strip()
    if label.isdigit():
        return (0, int(label), label)
    if label:
        return (1, label)
    return (2, "")


def _opd_mates_table(people: list[dict]) -> dict:
    """Таблица «ФИО · Группа»: одногруппники идут подряд по алфавиту, их номер
    группы — одна объединённая ячейка (rowspan); пустые группы не сливаются."""
    ordered = sorted(
        people,
        key=lambda mate: (_opd_group_sort_key(mate.get("group")), str(mate.get("student") or "")),
    )
    cells = [[
        _table_cell("ФИО", "bold", is_header=True),
        _table_cell("Группа", "bold", is_header=True),
    ]]
    index = 0
    while index < len(ordered):
        label = str(ordered[index].get("group") or "").strip()
        end = index + 1
        if label:
            while end < len(ordered) and str(ordered[end].get("group") or "").strip() == label:
                end += 1
        run = ordered[index:end]
        cells.append([
            _table_cell(run[0]["student"], "plain"),
            _table_cell(run[0].get("group"), "plain", rowspan=len(run) if len(run) > 1 else None),
        ])
        cells.extend([[_table_cell(extra["student"], "plain")] for extra in run[1:]])
        index = end
    return {"type": "table", "cells": cells, "is_bordered": True, "is_striped": True}


def _opd_mates_blocks(row: dict, *, open_mates: bool = True) -> list[dict]:
    """Состав ВГ: раздел «Вместе в ВГ N», внутри — раскрытый раздел на
    каждый институт с таблицей «ФИО · Группа»; крупные институты первыми,
    каждый одногруппник занимает отдельную строку. Канал раскрывает составы
    сразу; /opd в груп-боте держит их свёрнутыми (open_mates=False)."""
    mates = row.get("mates") or []
    if not mates:
        return [_paragraph("Состав виртуальной группы в таблице кафедры пока не найден.")]
    by_institute: dict[str, list[dict]] = {}
    for mate in mates:
        by_institute.setdefault(str(mate.get("institute") or ""), []).append(mate)
    ordered = sorted(by_institute.items(), key=lambda item: (item[0] == "", -len(item[1]), item[0]))
    sections: list[dict] = []
    for short, people in ordered:
        title = short or "институт не определён"
        full = opd_module.INSTITUTE_NAMES.get(short)
        building = opd_module.INSTITUTE_BUILDINGS.get(short)
        if full:
            title += f" · {full}"
        if building:
            title += f" · {building}"
        sections.append(_details_open(title, _opd_mates_table(people)))
    wrap = _details_open if open_mates else _details
    return [wrap(f"Вместе в ВГ {row['vg']}  ·  {len(mates)} чел. из других групп", *sections)]


def _opd_room_sort_key(row: dict) -> tuple:
    """Порядок людей внутри корпуса: по кабинету (106, 106хк, 216 — числом,
    суффикс следом, без номера — в конец), в одном кабинете раньше тот, кому
    к 14:00, дальше по алфавиту."""
    room = str(row.get("room") or "").strip()
    digits = re.match(r"^(\d+)", room)
    start = str(row.get("block_start") or "")
    student = str(row.get("student") or "").casefold()
    if digits:
        return (0, int(digits.group(1)), room[digits.end():].strip(), start, student)
    return (1, 0, room, start, student)


def _opd_building_section(building: str, people: list[dict], *, markers: bool = True,
                          open_mates: bool = True) -> dict:
    """Раздел корпуса: внутри по кабинетам (_opd_room_sort_key) —
    однокабинетники рядом; у каждого студента свой раздел с составом его ВГ."""
    ordered = sorted(people, key=_opd_room_sort_key)
    person_blocks = [
        _details(_opd_person_summary(row, building),
                 *_opd_mates_blocks(row, open_mates=open_mates))
        for row in ordered
    ]
    title = f"📍 {building}" if markers else building
    return _details(
        [{"type": "bold", "text": title}, f"  ·  {len(people)} чел."],
        *person_blocks,
    )


def _opd_people_line(label: str, rows: list[dict]) -> dict:
    names = ", ".join(f"{row['student']} (ВГ {row['vg']})" for row in rows)
    return _rich_paragraph([{"type": "bold", "text": f"{label}: "}, names])


_URL_ONLY_RE = re.compile(r"^\s*https?://\S+\s*$", re.I)


def _opd_note_is_redundant(lesson: dict, exceptional: str, opd_view: dict | None) -> bool:
    """«Важно» для слота ОПД не нужно: ссылка на объявление кафедры уже есть в
    разделе по ВГ, а голый URL без раздела читателю ничего не говорит.
    Настоящую текстовую пометку портала без раздела по ВГ оставляем."""
    if not _OPD_SUBJECT_RE.search(str(lesson.get("subject") or "")):
        return False
    return bool(opd_view) or bool(_URL_ONLY_RE.match(exceptional))


def _opd_sources(view: dict) -> dict:
    sources = view.get("sources") or {}
    parts: list[object] = [{"type": "italic", "text": "Источники: "}]
    links = [
        ("таблица ВГ", sources.get("sheet")),
        ("расписание ВГ", sources.get("doc")),
        ("объявление кафедры", sources.get("announcement")),
    ]
    first = True
    for text, url in links:
        if not url:
            continue
        if not first:
            parts.append(" · ")
        parts.append({"type": "url", "text": text, "url": str(url)})
        first = False
    return _rich_paragraph(parts)


def _opd_summary_text(view: dict) -> str:
    """Сводка раздела ОПД одной строкой — заголовок для /opd в груп-боте."""
    date = dt.date.fromisoformat(view["date"])
    counts = view["counts"]
    total = len(view["rows"])
    return (f"ОПД · ПО ВИРТУАЛЬНЫМ ГРУППАМ  ·  {date.strftime('%d.%m')}"
            f"  ·  идут {counts['session']} из {total}")


def _opd_section(view: dict, *, sources: bool = True, markers: bool = True,
                 open_mates: bool = True, open_section: bool = False) -> list[dict]:
    """Раздел «ОПД по виртуальным группам» для дня закрепа.

    Только про этот день и без пояснений: кто идёт — сворачиваемыми разделами
    по корпусам (внутри сначала 14:00, потом 16:00, далее по алфавиту), у каждого
    студента свой раздел с составом его ВГ; у кого занятие отменено; ссылки. Кого нет в таблицах, у того ОПД на этой неделе нет;
    чужие даты и штампы времени в закреп не попадают.

    Корпуса — по числу идущих (крупные первыми). Канал зовёт с дефолтами:
    свёрнутый details одним блоком (корпуса и источники внутри), таблица
    «Занятий не будет» ложится рядом вызовом _opd_cancelled_table —
    rich-заголовки внутри details API не принимает. /opd в груп-боте
    просит open_section=True: настоящий rich-заголовок и корпуса россыпью,
    раздел всегда раскрыт; sources=False, markers=False — без ссылок и
    эмодзи.
    """
    rows = view["rows"]
    going = [row for row in rows if row["status"] == "session"]
    blocks: list[dict] = []
    if going:
        # По корпусам (крупные первыми), внутри корпуса — сначала 14:00, потом 16:00.
        by_building: dict[str, list[dict]] = {}
        for row in going:
            by_building.setdefault(row.get("building") or "адрес уточняется", []).append(row)
        ordered = sorted(by_building.items(), key=lambda item: (-len(item[1]), item[0]))
        for building, people in ordered:
            blocks.append(_opd_building_section(
                building, people, markers=markers, open_mates=open_mates))
    else:
        blocks.append(_paragraph(
            "У группы в этот день ОПД нет: все её виртуальные группы занимаются в другие четверги."
        ))
    if sources:
        blocks.append(_opd_sources(view))
    if open_section:
        return [_heading(_opd_summary_text(view)), *blocks]
    return [_details(_opd_summary(view), *blocks)]


def _opd_cancelled_table(
    view: dict, *, markers: bool = True, title: str = "Занятий не будет",
) -> list[dict]:
    """«Занятий не будет» таблицей: заголовок + ФИО и ВГ, по алфавиту.

    Общая таблица для канала и /opd; title задаёт подпись, markers — маркер ❌.
    Пусто — когда отмен в этот день нет.
    """
    cancelled = [row for row in view["rows"] if row["status"] == "cancelled"]
    if not cancelled:
        return []
    ordered = sorted(cancelled, key=lambda row: str(row.get("student") or "").casefold())
    cells = [[
        _table_cell("ФИО", "bold", is_header=True),
        _table_cell("ВГ", "bold", is_header=True),
    ]]
    for row in ordered:
        cells.append([
            _table_cell(row["student"], "plain"),
            _table_cell(row["vg"], "plain"),
        ])
    title = f"❌ {title}" if markers else title
    return [
        _heading(title),
        {"type": "table", "cells": cells, "is_bordered": True, "is_striped": True},
    ]


def _dashboard_day_section(
    day: dict,
    *,
    screenshot_media: str | None = None,
    source_url: str = "",
    opd_view: dict | None = None,
    hidden_expired: bool = False,
) -> dict:
    lessons = day.get("lessons") or []
    blocks: list[dict] = []
    if not lessons:
        blocks.append(_paragraph("Пар нет."))
    else:
        blocks.append(_dashboard_lesson_table(lessons, opd_hint=bool(opd_view)))
    if opd_view:
        blocks += _opd_section(opd_view)
        blocks += _opd_cancelled_table(opd_view)
    notes = [
        (item.get("subject") or "—", exceptional)
        for item in lessons
        for _, exceptional in [_dashboard_note_display(item)]
        if exceptional and not _opd_note_is_redundant(item, exceptional, opd_view)
    ]
    if notes:
        blocks.append(_details(
            f"Важно · {len(notes)}",
            *[_note(subject, note) for subject, note in notes],
        ))
    if screenshot_media:
        blocks.append(_details(
            "Скрин с портала · " + dt.date.fromisoformat(day["date"]).strftime("%d.%m"),
            _screenshot_photo(day["day"], screenshot_media, source_url, hidden_expired=hidden_expired),
        ))
    return _details(_dashboard_day_summary(day), *blocks)


# Портал пишет аудитории неряшливо: «402ИГУМ», «310 ИГУМ», «1321ИГ» (опечатка),
# «1313ИГУМ313» (склейка двух номеров), «323а», «1100(1)», «3поточная».
_GUIDE_ROOM_RE = re.compile(
    r"^(\d{3,4})\s*"                                  # номер аудитории
    r"(ИГУМ|ИГ|ИЭ|ПИ|ПТИ|ХТИ|ИНПО|ИМО|МИ|ЮИ)?\s*"       # владелец/подразделение
    r"(?:\(\d{1,2}\)\s*)?"                            # «1100(1)» — вариант или подгруппа
    r"([а-яА-ЯёЁ])?\s*"                                 # «323а» — литера кабинета
    r"(?:\d{3,4})?$"                                    # хвост склейки «1313ИГУМ313»
)
_GUIDE_IGUM_MARKS = {"ИГУМ", "ИГ"}
# Пометка подразделения важнее цифр: у этих адрес официально другой.
_GUIDE_MARK_PLACES = {
    "ИНПО": "ул. Чудинцева, 6",
    "ПИ": "ул. Псковская, 3",
}

ANTONOVO_COORDS = {"latitude": 58.541187, "longitude": 31.288116}


def _guide_korpus_place(korpus: str) -> str:
    """Корпус по первой цифре четырёхзначного номера."""
    if korpus == "3":
        return "корпус 3 — Б. Санкт-Петербургская, 41"
    return f"корпус {korpus} (адрес смотри в примечаниях)"


def _guide_digits(digits: str) -> tuple[str, str]:
    """(этаж, номер кабинета) из 3- или 4-значного номера."""
    if len(digits) == 3:
        return digits[0], digits[1:]
    return digits[1], digits[2:]


def _guide_building(digits: str) -> str:
    """Здание по цифрам номера. Правило публикует сам НовГУ:
    3 цифры = у здания нет корпусов, 4 цифры = первая указывает на корпус.
    В Антоново четыре цифры имеют только аудитории старого корпуса (первая 1),
    в новом корпусе нумерация трёхзначная.
    """
    if len(digits) == 3:
        return "новый корпус — кампус Антоново"
    korpus = digits[0]
    if korpus == "1":
        return "старый корпус — кампус Антоново"
    return _guide_korpus_place(korpus)


def _guide_decode_room(room: str) -> dict | None:
    """Разбирает номера вида 1318 / 303 / 402ИГУМ.

    Суффикс (ИГУМ, ИЭ, ИНПО, ПИ…) — это владелец аудитории, подразделение, за которым
    она числится, а не институт студента. ИГУМ = Институт гуманитарный (Гуманитарный
    институт НовГУ), его корпуса стоят в Антоново, поэтому 218ИГУМ у группы Института
    экономики — это тот же кампус. Но полагаться только на цифры нельзя: «310 ИГУМ» —
    Антоново, а «310 ИНПО» — ул. Чудинцева, 6. Надёжный адрес — колонка «комм.»
    расписания, поэтому при чужой пометке мы её и предлагаем проверить.
    """
    room = (room or "").strip()
    if not room:
        return None
    # Поточная аудитория — особый случай, resolve_location не обрабатывает
    stream = re.match(r"^(\d{1,4})\s*поточн", room, re.I)
    if stream:
        digits = stream.group(1)
        place = _guide_korpus_place(digits[0]) if len(digits) == 1 else _guide_building(digits)
        return {"floor": "—", "num": f"{digits} поточная", "place": place}
    if "поточн" in room.lower():
        return {"floor": "—", "num": "—", "place": "поточная аудитория — адрес в колонке «комм.»"}
    # Для гида первокурсника используем resolve_location с контекстом Антоново,
    # потому что гайд explicitly про кампус Антоново и его корпуса.
    result = resolve_location(room, context_hints=["Антоново"])
    if result["confidence"] == "low" and not result["building"]:
        return None
    # Маппинг обратно в legacy-формат для совместимости с _guide_rooms_table
    place = result["building"]
    if result["campus"] == "antonovo":
        if "ИГУМ" in room.upper() or "ИГ" in room.upper():
            place = f"корпус Гуманитарного института (ИГУМ), {place}"
    elif result["campus"] == "other":
        # ИНПО/ПИ — сохраняем оригинальный текст про "не Антоново"
        mark = ""
        if "ИНПО" in room.upper():
            mark = "ИНПО"
        elif "ПИ" in room.upper():
            mark = "ПИ"
        if mark:
            place = f"пометка {mark}: {result['address']} — не Антоново, сверься с примечаниями"
    elif result["campus"] == "main" and result["confidence"] == "medium":
        # 4-значный 1xxx без контекста Антоново — но в гиде мы всегда с контекстом
        # Этот случай не должен произойти, но на всякий случай fallback
        place = result["building"]
    # Неизвестные маркеры (ИЭ, ПТИ, ХТИ и т.д.) — добавляем пометку
    mark_match = re.search(r"(ИГУМ|ИГ|ИЭ|ПИ|ПТИ|ХТИ|ИНПО|ИМО|МИ|ЮИ)", room.upper())
    if mark_match:
        mark = mark_match.group(1)
        if mark not in {"ИГУМ", "ИГ", "ИНПО", "ПИ"}:
            place = f"пометка {mark}: по цифрам {place}, адрес сверь с примечаниями"
    return {"floor": result["floor"], "num": result["room_number"], "place": place}


def _guide_rooms_table(schedule: dict | None) -> tuple[dict, int]:
    """Живая шпаргалка: все аудитории из расписания группы с расшифровкой."""
    rooms: dict[str, int] = {}
    days = (schedule or {}).get("days") or {}
    for day in DAYS_ORDER:
        for pair in days.get(day) or []:
            room = str(pair.get("room") or "").strip()
            if room and room != "—":
                rooms[room] = rooms.get(room, 0) + 1
    rows: list[list[tuple[object, str]]] = [[
        ("аудитория", "bold"), ("куда идти", "bold"), ("этаж", "bold"), ("кабинет", "bold"),
    ]]
    for room in sorted(rooms):
        info = _guide_decode_room(room)
        if info is None:
            rows.append([(room, "code"), ("Антоново (смотри примечания)", "plain"), ("—", "plain"), ("—", "plain")])
        else:
            rows.append([(room, "code"), (info["place"], "plain"), (info["floor"], "plain"), (info["num"], "plain")])
    return _table(rows), len(rooms)


def _guide_teacher_blocks(schedule: dict | None) -> tuple[list[dict], int]:
    """Живой список преподавателей группы: имя + предметы."""
    by_teacher: dict[str, set[str]] = {}
    days = (schedule or {}).get("days") or {}
    for day in DAYS_ORDER:
        for pair in days.get(day) or []:
            teacher = str(pair.get("teacher") or "").strip()
            if not teacher or teacher == "—":
                continue
            subject = str(pair.get("subject") or "").split("\n", 1)[0].strip()
            subject = re.sub(r"^\([^)]*\)\s*", "", subject)
            if subject:
                by_teacher.setdefault(teacher, set()).add(subject)
    blocks = []
    for teacher in sorted(by_teacher):
        blocks.append(_rich_paragraph([
            {"type": "bold", "text": teacher},
            "\n",
            {"type": "italic", "text": ", ".join(sorted(by_teacher[teacher]))},
        ]))
    return blocks, len(by_teacher)


def _freshman_guide(schedule: dict | None, source_url: str) -> dict:
    """Раскрытый раздел-гид для первокурсников: аудитории, корпуса, недели, справки."""
    rooms_table, rooms_count = _guide_rooms_table(schedule)

    algorithm = _details(
        "Шаг за шагом: как понять, куда идти",
        _rich_paragraph([
            {"type": "bold", "text": "Шаг 1. Считай цифры в номере аудитории"},
        ]),
        _rich_paragraph([
            {"type": "marked", "text": {"type": "bold", "text": "3 цифры"}},
            ": первая = этаж, две другие = кабинет.",
        ]),
        _rich_paragraph([
            "Пример: ", {"type": "code", "text": "303"}, " = этаж 3, кабинет 03.",
        ]),
        _rich_paragraph([
            {"type": "marked", "text": {"type": "bold", "text": "4 цифры"}},
            ": первая = корпус, вторая = этаж, две последние = аудитория.",
        ]),
        _rich_paragraph([
            "Пример: ", {"type": "code", "text": "1318"}, " = корпус 1, этаж 3, аудитория 18.",
        ]),
        _rich_paragraph([
            "• Если у пары есть примечание с адресом — оно главнее номера аудитории.",
        ]),
        _divider(),
        _rich_paragraph([
            {"type": "bold", "text": "Шаг 2. Пойми, в каком ты корпусе (Антоново)"},
        ]),
        _rich_paragraph([
            "• 3 цифры → новый корпус в кампусе Антоново.",
        ]),
        _rich_paragraph([
            "• 4 цифры, первая 1 → старый корпус в кампусе Антоново.",
        ]),
        _rich_paragraph([
            "• 4 цифры, первая не 1 → это уже не Антоново: первая цифра = корпус адреса "
            "Б. Санкт-Петербургская, 41. Пример: ", {"type": "code", "text": "2312"}, " = корпус 2, этаж 3, аудитория 12.",
        ]),
        _rich_paragraph([
            "• Суффикс ", {"type": "code", "text": "ИГУМ"}, " (218ИГУМ, 402ИГУМ) → аудитория числится за Гуманитарным "
            "институтом, а он тоже в Антоново. Суффикс — это владелец кабинета, а не твой институт.",
        ]),
        _rich_paragraph([
            "• Суффикс решает не всегда: ", {"type": "code", "text": "310 ИГУМ"}, " — Антоново, а ",
            {"type": "code", "text": "310 ИНПО"}, " — ул. Чудинцева, 6. Адрес всегда смотри в колонке «комм.» расписания.",
        ]),
        _rich_paragraph([
            "Правило нумерации публикует сам университет: ",
            {"type": "url", "text": "Навигация в корпусах НовГУ",
             "url": "https://www.novsu.ru/study/campus_navigation/navigation_in_buildings/"}, ".",
        ]),
        _divider(),
        _rich_paragraph([
            {"type": "bold", "text": "Шаг 3. Сверься со шпаргалкой ниже"},
            " — там аудитории с расшифровкой, куда идти.",
        ]),
    )

    rooms_section = _details(
        f"Шпаргалка: аудитории ({rooms_count})",
        rooms_table,
        _paragraph("Список собран по расписанию на портале НовГУ."),
    )

    antonovo_section = _details(
        "Антоново: адреса, карта, как добраться",
        _rich_paragraph([
            {"type": "bold", "text": "Старый корпус"},
            " — кампус Антоново. Здесь аудитории с четырьмя цифрами, первая 1 (1306, 1318, 1331).",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Новый корпус"},
            " — кампус Антоново, рядом со старым. Здесь аудитории с тремя цифрами (214, 216, 303, 418).",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "ИГУМ = Институт гуманитарный, "},
            "то есть Гуманитарный институт НовГУ. Так в официальном «Положении об Институте гуманитарном»: "
            "сокращённое название — ИГУМ НовГУ, местонахождение — Великий Новгород, Антоново. "
            "Расписание аудиторий этого института на портале так и называется — «Расписание аудиторий (ИГУМ)».",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Суффикс у номера — метка владельца аудитории: "},
            {"type": "code", "text": "218ИГУМ"}, " и ", {"type": "code", "text": "402ИГУМ"},
            " — кабинеты Гуманитарного института в Антоново. Это не твой институт, а подразделение, "
            "за которым числится кабинет, и голый номер место не определяет: ",
            {"type": "code", "text": "310 ИГУМ"}, " — это Антоново, а ",
            {"type": "code", "text": "310 ИНПО"}, " — ул. Чудинцева, 6, другой конец города. "
            "Этаж и номер читай по цифрам, как в шаге 1, а адрес сверяй с колонкой «комм.».",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Что это значит для нас: "},
            "мы — Институт экономики (ИЭ), направление 43.03.02 «Туризм». До 1 января 2025 года он назывался ИЦЭУС, "
            "так что в старых постах и документах это он же. Институт сидит в кампусе «территория Антоново», "
            "и часть наших пар стоит в корпусах Гуманитарного института — это тот же кампус, ехать в другой район не надо: "
            "видишь ИГУМ в номере — идёшь в Антоново. ",
            {"type": "bold", "text": "Спортзал"}, " — тоже Антоново.",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Осторожно со старым адресом: "},
            "в интернете до сих пор попадается «Институт экономики, ул. Псковская, 3» — это адрес института до переезда, "
            "сейчас там Педагогический институт. Не езжай туда.",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Наши мастерские — один этаж нового корпуса: "},
            "«Туризм» (ауд. 418: 27 мест, 10 компьютеров, проектор, экран, телевизор, МФУ), "
            "«Гостеприимство» (ауд. 415) и «Гостиничный номер» с настоящей кроватью (ауд. 414). "
            "Так их называет официальный реестр помещений НовГУ.",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Цифру после «Антоново» не читай: "},
            "официальные страницы НовГУ противоречат сами себе — одна пишет «Антоново, 1» про новый "
            "корпус, другая «Антоново, 2» про тот же кабинет 423. Ориентир один — номер аудитории.",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Пометка «ИГУМ, Антоново» в комментариях: "},
            "это не номер кабинета, а место проведения (например, спортзал или актовый зал Гуманитарного института). "
            "Бот выносит её в отдельное поле «место», чтобы не путать с аудиторией.",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Переход: "},
            "из нового корпуса в старый (и наоборот) есть крытый переход — "
            "можно пройти между зданиями, не выходя на улицу.",
        ]),
        _photo("attach://antonovo_transition", "Переход между корпусами Антоново"),
        _rich_paragraph([
            {"type": "bold", "text": "Столовая: "},
            "при входе в новый корпус — справа.",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Важно: "},
            "кампус стоит на территории бывшего Антониева монастыря (основан в 1106 году), "
            "но метка на карте ниже — у входа в старый корпус, а не у собора.",
        ]),
        {"type": "map",
         "location": dict(ANTONOVO_COORDS),
         "zoom": 17, "width": 800, "height": 450,
         "caption": {"text": [
             {"type": "bold", "text": "Кампус «территория Антоново»"},
             " — старый и новый корпуса рядом, метка у входа в старый",
         ]}},
        _rich_paragraph([
            {"type": "bold", "text": "Как добраться: "},
            "автобусы ", {"type": "code", "text": "№ 2, 5, 8а"},
            ", остановка «Студенческая улица, 1» (около 350 м до кампуса). "
            "Для навигатора: Великий Новгород, территория Антоново.",
        ]),
    )

    weeks_section = _details(
        "Как читать недели и пометки",
        _rich_paragraph([
            {"type": "marked", "text": {"type": "bold", "text": "ВЕРХНЯЯ"}},
            "  1, 3, 5, 7… нечётные учебные недели.  ",
            {"type": "marked", "text": {"type": "bold", "text": "НИЖНЯЯ"}},
            "  2, 4, 6, 8… чётные учебные недели. "
            "Считаются от 01.09 по учебному календарю НовГУ.",
        ]),
        _divider(),
        _rich_paragraph([
            {"type": "marked", "text": {"type": "bold", "text": "ДОТ"}},
            " — дистанционные образовательные технологии: пара онлайн, идти никуда не надо. ",
            {"type": "code", "text": "*"}, " у предмета — к нему есть примечание (уточнение по аудитории или формату).",
        ]),
        _rich_paragraph([
            {"type": "code", "text": "лек."}, " — лекция   ",
            {"type": "code", "text": "пр."}, " — практическое занятие   ",
            {"type": "code", "text": "лаб."}, " — лабораторная работа   ",
            {"type": "code", "text": "подгр."}, " — подгруппа",
        ]),
    )

    certificates_section = _details(
        "Справки студентам",
        _rich_paragraph([
            {"type": "bold", "text": "Справка об обучении"},
            " — с цветной печатью и подписью. Можно получить в электронном виде и распечатать самостоятельно.",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Как получить онлайн:"},
        ]),
        _rich_paragraph(["1. Зайти на портал НовГУ под личным логином и паролем;"]),
        _rich_paragraph(["2. Открыть свой профайл;"]),
        _rich_paragraph(["3. Найти раздел «Справки студентам»;"]),
        _rich_paragraph(["4. Выбрать справку и скачать её."]),
        _rich_paragraph([
            "Также можно получить в ",
            {"type": "bold", "text": "МФЦО"},
            " (каб. 3105/5, главный корпус) при наличии студенческого билета.",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Важно: "},
            "скачанную справку принимают не всегда — некоторые организации требуют оригинал с печатью.",
        ]),
        _divider(),
        _rich_paragraph([
            {"type": "bold", "text": "Справка о доходах"},
            " — нужно заранее заказать: написать ФИО, номер группы, период, за который нужны доходы, "
            "и количество экземпляров в журнале на столе у каб. 2207.",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Где: "},
            "главный корпус НовГУ, Б. Санкт-Петербургская, 41, ",
            {"type": "bold", "text": "кабинет 2207"},
            " (2 этаж).",
        ]),
        _rich_paragraph([
            "Также по электронной почте: ",
            {"type": "code", "text": "Svetlana.Seliverstova@novsu.ru"},
            ", в копию ",
            {"type": "code", "text": "Galina.Vasyunova@novsu.ru"},
            ".",
        ]),
        _rich_paragraph([
            "Оригиналы готовы на следующий день после 14:00 в кабинете 2207 (главный корпус).",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "МФЦО по Институту экономики "},
            "(Б. Санкт-Петербургская, 41, кабинеты 3105/1…3105/8): Романова Юлия Анатольевна — "
            "каб. 3105/3, тел. 97-42-92; Бородюк Ирина Ивановна — каб. 3105/5, тел. 97-42-47. "
            "Часы приёма МФЦО официально не опубликованы, поэтому перед походом лучше позвонить.",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Если что-то не так с расписанием: "},
            "по Институту экономики отвечает Бадалова Татьяна Анатольевна — Б. Санкт-Петербургская, 41, "
            "каб. 3105/2, тел. ", {"type": "code", "text": "33-88-36"},
            ", пн–чт 9:00–18:00 и пт 9:00–17:00, обед 13:00–13:50.",
        ]),
    )

    money_section = _details(
        "Деньги: стипендии, матпомощь, касса",
        _rich_paragraph([
            {"type": "bold", "text": "Стипендии платят 4 числа"},
            " каждого месяца за предыдущий. Если 4-е выпадает на выходной — "
            "в последний рабочий день перед ним. В декабре платят дважды, "
            "январскую стипендию — к 25 января (Татьянин день).",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Академическая — 2545 ₽."},
            " Назначается сразу после зачисления. Чтобы сохранить после сессии, "
            "все зачёты и экзамены надо сдать без троек и долгов.",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Социальная — 3818 ₽"},
            " (малообеспеченным студентам и льготным категориям).",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "ПГАС (повышенная академическая) — 7400 ₽"},
            " за достижения в учёбе, науке, спорте, творчестве или общественной деятельности. "
            "Сессия на 4 и 5; можно подать сразу на несколько номинаций — максимум до 20 000 ₽.",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "ПГАС оформляют НЕ через ООД: "},
            "документы несут лично, по номинациям, в разные кабинеты. "
            "Осенний приём 2026 — с 1 по 18 сентября.",
        ]),
        _table([
            [("номинация", "bold"), ("куда и кому", "bold"), ("часы", "bold")],
            [("Учебная", "plain"),
             ("Б. Санкт-Петербургская, 41, каб. 3105 · Евдокимова М.Н.", "plain"),
             ("1–18.09", "plain")],
            [("Научная (НИР)", "plain"),
             ("Б. Санкт-Петербургская, 41, каб. 1304 · Волошина Г.В., тел. 33-20-48", "plain"),
             ("1–18.09, строго 16:00–18:00", "plain")],
            [("Спортивная", "plain"),
             ("Б. Санкт-Петербургская, 41, каб. 1322 · Астахов И.В.", "plain"),
             ("1–18.09, 14:00–17:30", "plain")],
            [("Культурно-творческая", "plain"),
             ("Антоново, каб. 125 · Осипова Т.О.", "plain"),
             ("1–18.09, 14:00–17:30", "plain")],
            [("Общественная", "plain"),
             ("Б. Санкт-Петербургская, 41, каб. 3216 · Маслякова А.А.", "plain"),
             ("1–18.09, 14:00–17:30", "plain")],
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Ловушки ПГАС: "},
            "3, 8, 9 и 10 сентября спорт, творчество и общественную не принимают — идёт мероприятие "
            "Молодёжной политики. Нумерация приложений в описи разная для учебной/научной и для "
            "остальных номинаций, поэтому формы бери из своего блока. Адрес культурно-творческой "
            "точки в объявлении противоречит сам себе — перед походом напиши Осиповой Т.О. и уточни. "
            "Объявление со всеми формами: ",
            {"type": "url", "text": "ПГАС, осень 2026", "url": "https://www.novsu.ru/university/press/advertisement/292791/"},
            ".",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Матпомощь — до 15 000 ₽"},
            " (очно, бюджет), не чаще одного раза в семестр. Несут лично в ООД своего института "
            "(нам — каб. 423) с 1 по 10 число, кроме января, июня, июля и августа; "
            "в МФЦО заявления на матпомощь не принимают.",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Пакет на матпомощь: "},
            "заявление по форме, вписанное в него основание из официального перечня, "
            "копии всех страниц паспорта, ИНН и документы, подтверждающие основание. "
            "Решает Стипендиальная комиссия (заседание как правило не позднее 20 числа), "
            "деньги приходят со стипендией в следующем месяце.",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Рабочие основания первокурснику: "},
            "иногородним — справка с места проживания родителей и регистрация за пределами "
            "Великого Новгорода и Новгородского района; участие в конференциях, олимпиадах, сборах "
            "и практиках — ходатайство подразделения плюс приказ или приглашение; "
            "лечение и оздоровление — нужно согласование Медицинского центра НовГУ.",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Касса (главный корпус, 2 этаж, 14:00–18:00): "},
            "каб. 2207 — стипендии, матпомощь, справки; каб. 2209 — оплата обучения; "
            "каб. 2210 — оплата общежития.",
        ]),
    )

    ood_section = _details(
        "ООД: куда идти по вопросам учёбы",
        _rich_paragraph([
            "По любым вопросам учёбы (переводы, пересдачи, задолженности, индивидуальный план) "
            "обращаются на свою кафедру или в ООД — отдел обеспечения деятельности.",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "ООД Института экономики (наш институт): "},
            "Антоново, каб. 423 — это общий приём. Телефон ",
            {"type": "code", "text": "8 (8162) 97-45-61, доб. 1612"},
            ". Отдел работает 09:00–17:45, обед 13:00–14:00, студентов принимают 10:00–13:00.",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Кто там сидит: "},
            "начальник отдела Одинокова Татьяна — каб. 413, вн. 2710, ",
            {"type": "code", "text": "Tatyana.Odinokova@novsu.ru"},
            "; администратор Третьяк Елизавета Сергеевна — каб. 428а, вн. 2708, ",
            {"type": "code", "text": "Elizaveta.Tretyak@novsu.ru"},
            ". Учебной частью заведует Логутова Светлана Вениаминовна. "
            "Все кабинеты рядом, в новом корпусе.",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Бланки заявлений "},
            "(академ, перевод, восстановление, отчисление, справка о периоде обучения, "
            "индивидуальный план и ещё полтора десятка форм) лежат на странице ",
            {"type": "url", "text": "ООД Института экономики", "url": "https://portal.novsu.ru/doc/study/dept/35493702/?id=1748739"},
            ".",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Выше ООД: "},
            "директор Института экономики — Трифонов Владимир Александрович, ",
            {"type": "code", "text": "tva@novsu.ru"},
            ". Выпускающая кафедра «Туризма» — кафедра цифровой экономики и управления, "
            "зав. кафедрой Иванова Ольга Петровна.",
        ]),
        _rich_paragraph([
            "ООД других институтов и колледжей — на официальной странице ",
            {"type": "url", "text": "Отделы обеспечения деятельности НовГУ", "url": "https://www.novsu.ru/study/ood/"}, ".",
        ]),
    )

    library_section = _details(
        "Библиотека в Антоново",
        _rich_paragraph([
            "Отдел обслуживания пользователей Научной библиотеки НовГУ — для Гуманитарного, Юридического институтов "
            "и Института экономики, то есть и для нас. Он в ауд. 1109 в кампусе Антоново.",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Для записи нужны: "},
            "студенческий билет, 2 фотографии и паспорт. Единый читательский билет "
            "действует во всех абонементах и читальных залах НовГУ.",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Режим: "},
            "пн–чт 09:00–17:30, пт 09:00–17:00. Последняя пятница месяца — санитарный день.",
        ]),
    )

    transport_section = _details(
        "Автобусы: Антоново → западный район",
        _rich_paragraph([
            {"type": "bold", "text": "У кампуса: "},
            "остановки «Студенческая ул.» и «Парковая ул., 2». Прямо отсюда в западный район идут два маршрута — №8а и №2. "
            "Там же садится №5, но он в другую сторону: центр (Розважа, Кооперативная, Колмово) и дальше до «Акрона».",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "№8а — главный, идёт кольцом: "},
            "Парковая → Студенческая → Колмово → Ломоносова → Кочетова → Свободы → Зелинского → Попова → "
            "Нехинская, 61 → пр. Мира → Нехинская, 1 → Вокзальная площадь → центр → Б. Санкт-Петербургская → "
            "Колмово → Парковая → снова Антоново. Часть рейсов делает заезд по пр. Корсунова до ул. Коровникова "
            "(там общежитие №5).",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "№2 — на Псковскую сторону: "},
            "Студенческая → Колмово → Б. Санкт-Петербургская → пл. Победы-Софийская → Псковская ул., 1 → 32 → 40 → 50, "
            "дальше Мостищи и Панковка. Обратно в Антоново тот же №2 идёт через Парковую и Б. Московскую, 53.",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "7–10 минут пешком — и вариантов в разы больше: "},
            "остановки «Б.Московская ул., 53» и «Московская ул.»: ",
            {"type": "bold", "text": "№1 "}, "(пр. Корсунова, запад — садиться на «Б.Московская ул., 53»), ",
            {"type": "bold", "text": "№1а "}, "(Псковская ул. до Мостищей), ",
            {"type": "bold", "text": "№4 "}, "(Вокзальная пл. → Нехинская → Попова → Зелинского → пр. Корсунова), ",
            {"type": "bold", "text": "№6 "}, "и ", {"type": "bold", "text": "№44М "},
            "(Ломоносова → пр. Мира → Зелинского → Попова), ",
            {"type": "bold", "text": "№19 "}, "(Вокзальная пл. → Нехинская → пр. Мира → Попова → Зелинского).",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "В пути: "},
            "от кампуса до Зелинского/Попова на №8а — 11 остановок и около 7 км, минут 25–35. "
            "Обратно из западного района тем же №8а по кольцу через Вокзальную и центр — примерно 14 остановок. "
            "№4 от Б. Московской до Зелинского короче: 11 остановок, около 6 км.",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Где общежития: "},
            "№2, №3 и №6 на Парковой, 7/9/10 — пешком от кампуса; №7 на ул. Свободы, 4 и №5 на пр. Корсунова, 36 к3 — "
            "западный район, туда №8а. По заселению: ЦДОД «ДНК им. Ковалевской», Б. Санкт-Петербургская, 41, ауд. 1225, ",
            {"type": "code", "text": "8 (8162) 33-20-44"}, ", zaselenie@novsu.ru.",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Движение в реальном времени: "},
            "Яндекс Карты или 2ГИС — там видно, где автобус сейчас.",
        ]),
    )

    life_section = _details(
        "Вне учёбы: студсовет, студии, спорт",
        _rich_paragraph([
            {"type": "bold", "text": "Студсовет Института экономики (наш): "},
            "мероприятия, квизы, благотворительные акции. Активность даёт баллы на повышенную стипендию. "
            "Группа ВКонтакте: ",
            {"type": "url", "text": "Студсовет ИЭ НовГУ", "url": "https://vk.com/studsovet.ie_novsu"}, ".",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Ещё два студенческих канала: "},
            "общеуниверситетский ",
            {"type": "url", "text": "Студсовет НовГУ", "url": "https://vk.com/studsovet.novsu"},
            " и ", {"type": "url", "text": "Студсовет Гуманитарного института", "url": "https://vk.com/studsovet.igum"},
            " — мы занимаемся в его корпусах, часть событий в Антоново анонсирует он.",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Профсоюз студентов: "},
            "защита прав, скидки по программе «ПрофПлюс» и РЖД Бонус. Вступление — каб. 3216 главного корпуса. "
            "Группа ВКонтакте: ",
            {"type": "url", "text": "Студенческий профком НовГУ", "url": "https://vk.com/profkomnovgu"}, ".",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Волонтёрство: "},
            "часы волонтёрства конвертируются в баллы на повышенную стипендию; "
            "есть конкурсы «Волонтёр месяца» и «Волонтёр года».",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Творческие студии (бесплатно): "},
            "актовый зал Антоново — вокал «MIX» и «КАЛЕЙДОСКОП», хор «РЕНЕССАНС», театр «АМПЛУА», "
            "хореография, гитара, школа диджеев. Расписание и анонсы — в группе ВКонтакте ",
            {"type": "url", "text": "Культура и творчество НовГУ (ЦСПИ)", "url": "https://vk.com/novsu.event"}, ".",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Спортклуб «Новгородские Рыси» (бесплатно, 18 секций): "},
            "в Антоново — флорбол (зал 1: пн 19:00–20:30, пт 19:30–21:00), вольная борьба (зал 2: пн/ср/пт 20:00–22:00), "
            "большой теннис (зал 1: вс 10:00–17:00) и бадминтон (зал 1: вс 20:00–22:00). "
            "Полное расписание — в группе ВКонтакте ",
            {"type": "url", "text": "Новгородские Рыси | ССК НовГУ", "url": "https://vk.com/sport_novsu"}, ".",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Адаптеры: "},
            "студент старшего курса, который координирует группу первокурсников, — «человек, к которому можно "
            "обратиться по любому вопросу». Главный адаптер Института экономики — Леонид Морковин, "
            "по направлению «Туризм» — Ксения Мехоношина и Леонид Морковин. Список ИЭ (а с ним Политеха, "
            "Медицинского, ХТИ и колледжей) — в посте ",
            {"type": "url", "text": "ВКонтакте", "url": "https://vk.com/wall-34755757_44442"},
            ". Адаптеры Гуманитарного, Юридического и Педагогического институтов живут в другом посте ",
            {"type": "url", "text": "vk.com/wall-34755757_44440", "url": "https://vk.com/wall-34755757_44440"},
            " — он нам не нужен. Официальная страница: ",
            {"type": "url", "text": "«Адаптеры» НовГУ", "url": "https://www.novsu.ru/study/freshman/adapters/"}, ".",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Где поесть: "},
            "в Антоново — столовая в новом корпусе на 2 этаже (официально «Столовая Института "
            "гуманитарного», но корпус наш): 120 мест, 10:00–16:00, есть диет-стол. "
            "В главном корпусе на Б. Санкт-Петербургской, 41 — кафе университета "
            "(ауд. 1110, 10:00–17:00 без перерыва).",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Если заболел: "},
            "кабинет терапевта в Антоново — каб. 1222 и 1223, это старый корпус (номера 1xxx). "
            "Согласование Медицинского центра НовГУ нужно и для матпомощи на лечение.",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Базы отдыха НовГУ со скидкой студентам: "},
            "«Песочки» (Солецкий район, 61 км от города) и «Шуя» (Валдайский район, 150 км) — вариант на выходные и каникулы.",
        ]),
        _rich_paragraph([
            {"type": "bold", "text": "Если дозвониться не удалось: "},
            "есть ",
            {"type": "url", "text": "официальная форма письменного обращения в НовГУ",
             "url": "https://portal.novsu.ru/support/portal/i.4638/?id=4636"},
            " — на неё ссылаются прямо из папок документов ООД и МФЦО. Ещё есть телеграм-бот "
            "навигации «НовГУ ГИД»: подскажет корпуса, общежития и медцентры со ссылками на 2ГИС, "
            "но кабинеты и дедлайны всегда перепроверяй по официальным страницам.",
        ]),
    )

    return _details_open(
        "Гид первокурсника: аудитории, корпуса, недели, деньги, справки",
        algorithm,
        rooms_section,
        antonovo_section,
        weeks_section,
        certificates_section,
        money_section,
        ood_section,
        library_section,
        transport_section,
        life_section,
    )


def _time_legend() -> dict:
    """Легенда времени: портал печатает начала академических часов, не пары.

    Один раз на пост объясняет сетку 45 + 15 + 45 и вариант «препод ведёт без
    перерыва», чтобы в таблицах не повторять это у каждой пары.
    """
    # Три короткие строки вместо одного абзаца на пять строк: легенду
    # читают по диагонали, сплошной текст в конце поста просто пролистывают.
    return _rich_paragraph([
        {"type": "bold", "text": "Время: "},
        f"пара — это {bells.PAIR_STRUCTURE}, то есть ",
        {"type": "code", "text": "09:00–10:45"},
        ". Если препод ведёт без перерыва, конец на 15 минут раньше: ",
        {"type": "code", "text": "09:00–10:30"}, ".",
        "\n",
        "Между парами перерыв полчаса, поэтому следующая начинается в ",
        {"type": "code", "text": "11:00"}, ".",
        "\n",
        "На портале в колонке «время» стоят начала академических часов: ",
        {"type": "code", "text": "14:00 15:00 16:00 17:00"},
        " — это две пары, ", {"type": "code", "text": "14:00–15:45"}, " и ",
        {"type": "code", "text": "16:00–17:45"}, ".",
    ])


def _day_date(day: dict) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(day.get("date") or ""))
    except ValueError:
        return None


def build_dashboard_rich_message(
    schedule: dict | None,
    weeks: list[dict],
    source_url: str,
    target_date: dt.date,
    *,
    group_name: str = "6381",
    screenshot_media: str | list[str] | list[dict] | None = None,
    current_date: dt.date | None = None,
    now: dt.datetime | None = None,
    last_updated: str = "",
    schedule_changed: str = "",
    opd: dict | None = None,
    sleep_note: str = "",
) -> dict:
    """Build pinned dashboard: diary + current week, date-aware.

    ``opd`` — данные виртуальных групп ОПД (``opd.load_opd``): для дней с датой
    из документа добавляется сворачиваемый раздел, а блок 16:00 держит день в
    закрепе после конца портальной пары.
    """
    target_week = find_schedule_week(weeks, target_date)
    # Сервер живёт в UTC, поэтому «сегодня» берём именно по Москве: иначе
    # между 00:00 и 03:00 Мск вчерашний день оставался бы в посте. В варианте
    # «день исчезает после последней пары» нужен и момент, а не только дата;
    # когда тест передаёт лишь дату, граница по умолчанию остаётся 00:00 Мск.
    if now is not None:
        now_msk = now.astimezone(zoneinfo.ZoneInfo("Europe/Moscow")) if now.tzinfo else now
    elif current_date is not None:
        now_msk = dt.datetime.combine(current_date, dt.time(0, 0))
    else:
        now_msk = dt.datetime.now(zoneinfo.ZoneInfo("Europe/Moscow"))
    current_date = current_date or now_msk.date()

    blocks: list[dict] = [_dashboard_title(group_name, target_date, target_week, last_updated, schedule_changed, sleep_note)]

    if target_week:
        week = week_view(schedule, weeks, target_week, group=group_name, source_url=source_url)
        day_media: dict[str, str] = {}
        if screenshot_media:
            if isinstance(screenshot_media, str):
                media_items = [{"label": "с сайта", "media": screenshot_media}]
            else:
                media_items = [item if isinstance(item, dict) else {"label": "с сайта", "media": item} for item in screenshot_media]
            for item in media_items:
                label = str(item.get("label") or "с сайта")
                day_label = DAY_SHORT_TO_FULL.get(label, label)
                day_media[day_label] = item["media"]
        # Прошедшие дни из закрепа исчезают, а сегодняшний доживает до конца
        # своей последней пары: в пятницу после пар читатель видит только сб.
        late_ends = opd_module.late_ends(opd)
        lesson_days = [day for day in week["days"] if day["lessons"]]
        live_days = [
            day for day in lesson_days
            if day_is_live(day, now_msk, late_end=late_ends.get(_day_date(day)))
        ]
        dropped_past = len(live_days) < len(lesson_days)
        if live_days:
            blocks.append(_dashboard_count_line(lesson_days, live_days, dropped_past=dropped_past))
        for day in live_days:
            day_date = _day_date(day)
            blocks.append(_dashboard_day_section(
                day,
                screenshot_media=day_media.get(day["day"]),
                source_url=source_url,
                opd_view=opd_module.day_view(opd, day_date) if (opd and day_date) else None,
                hidden_expired=bool(day_date) and _day_hides_expired(schedule, weeks, day_date),
            ))
        if not live_days:
            blocks.append(_paragraph(
                "Пары этой учебной недели уже прошли — впереди следующая."
                if dropped_past
                else "На эту учебную неделю пары не найдены."
            ))

    return {"rich_message": {"blocks": blocks}}


def build_rich_message(
    schedule: dict | None,
    weeks: list[dict],
    source_url: str,
    today: dt.date | None = None,
    changes_summary: str = "",
    is_demo: bool = False,
    group_name: str = "6381",
) -> dict:
    """Build the single canonical InputRichMessage used by preview and post.py."""
    today = today or dt.date.today()
    week = find_current_week(weeks, today)
    if week:
        title = (
            f"Расписание группы {group_name}\n\n"
            f"Неделя {week['week']} ({WEEK_HALF_NAME.get(week.get('half'), week.get('half', ''))})\n"
            f"{week['start']} — {week['end']}"
        )
    else:
        title = f"Расписание группы {group_name}"

    blocks: list[dict] = [_pullquote(title), _time_legend()]
    if is_demo:
        blocks.append(_paragraph("Демо формата: данные занятий используются только для предпросмотра."))
    # Один раскрывающийся раздел с определениями — пометки по дням не трогаем
    blocks.append(
        _details(
            "Как читать недели и пометки",
            _rich_paragraph([
                {"type": "marked", "text": {"type": "bold", "text": "ВЕРХНЯЯ"}},
                "  1, 3, 5, 7…  нечётные учебные недели\n",
                {"type": "marked", "text": {"type": "bold", "text": "НИЖНЯЯ"}},
                "  2, 4, 6, 8…  чётные учебные недели\n",
                "Считаются от 01.09 (неделя 1), а не от 1 января. Пример: 01–05.09 — неделя 1 (верхняя), 08–12.09 — неделя 2 (нижняя).\n",
                {"type": "code", "text": "*"}, " у предмета — смотри «Примечания» под таблицей на этот день.",
            ]),
            _divider(),
            _rich_paragraph([
                {"type": "marked", "text": {"type": "bold", "text": "ДОТ"}},
                " — дистанционные образовательные технологии (дистант полностью/частично; способ подключения — у препода).",
            ]),
            _rich_paragraph([
                {"type": "code", "text": "лек."}, " — лекция   ",
                {"type": "code", "text": "пр."}, " — практическое занятие\n",
                {"type": "code", "text": "лаб."}, " — лабораторная работа   ",
                {"type": "code", "text": "ауд."}, " — аудитория\n",
                {"type": "code", "text": "подгр."}, " — подгруппа",
            ]),
        )
    )
    blocks.append(_divider())

    days = (schedule or {}).get("days") or {}
    rendered_days = 0
    for day in DAYS_ORDER:
        pairs = days.get(day) or []
        if not pairs:
            continue
        rendered_days += 1
        count = len(pairs)
        groups: dict[str, list[dict]] = {"upper": [], "lower": [], "every": []}
        for index, pair in enumerate(pairs, 1):
            fields = _lesson_fields(pair, index)
            groups[_parity_of(fields["note"])].append(fields)
        # Для первокурсника: в день не бывает 5 пар одновременно — часть по верхней, часть по нижней.
        # Заголовок показывает сколько реально идти (max в одну неделю), в скобках — всего уникальных.
        every_n = len(groups["every"])
        upper_n = len(groups["upper"])
        lower_n = len(groups["lower"])
        max_per_day = max(every_n + upper_n, every_n + lower_n, every_n if upper_n == 0 and lower_n == 0 else 0)
        if max_per_day == 0:
            max_per_day = count

        # Для первокурсника показываем готовые недельные наборы: верхняя+каждую и нижняя+каждую
        day_inner: list[dict] = []
        up = groups["upper"]
        low = groups["lower"]
        every = groups["every"]
        views: list[tuple[str, list[dict]]] = []
        if up and low:
            views = [("upper", up + every), ("lower", low + every)]
        elif up:
            views = [("upper", up + every)]
        elif low:
            views = [("lower", low + every)]
        elif every:
            views = [("every", every)]
        for parity, items in views:
            # Сортируем по номеру пары для читаемости
            items_sorted = sorted(items, key=lambda x: int(x["number"]) if str(x["number"]).isdigit() else 999)
            day_inner.append(_parity_label(parity, count=len(items_sorted)))
            body = [[
                ("№", "bold"), ("предмет", "bold"), ("время", "bold"),
                ("ауд.", "bold"), ("преподаватель", "bold"),
            ]]
            notes: list[tuple[str, str]] = []
            for fields in items_sorted:
                condition = _strip_parity(fields["note"])
                marker = " *" if condition else ""
                body.append([
                    (fields["number"], "code"), (fields["subject"] + marker, "accent"),
                    (fields["time"], "code"), (fields["room"], "code"),
                    (fields["teacher"], "bold"),
                ])
                if condition:
                    notes.append((fields["subject"], condition))
            day_inner.append(_table(body))
            if notes:
                day_inner.append(_details(
                    f"Примечания — {len(notes)} {_notes_word(len(notes))}",
                    *[_note(subject, condition) for subject, condition in notes],
                ))
        # День недели — заголовок без смайлика (как просил)
        header = f"{day}"
        blocks.append(_details(header, *day_inner))
        blocks.append(_divider())

    if not rendered_days:
        blocks.append(_paragraph("На портале пока нет опубликованных занятий на эту неделю."))
    if changes_summary:
        blocks.extend([_divider(), _pullquote(f"Что изменилось\n{changes_summary}")])

    # No boilerplate/footer: keep the post focused on the timetable itself.
    return {"rich_message": {"blocks": blocks}}


def fallback_text(schedule: dict | None, weeks: list[dict], source_url: str, today: dt.date | None = None) -> str:
    """Plain text fallback when sendRichMessage is temporarily unavailable."""
    week = find_current_week(weeks, today)
    title = f"Расписание группы 6381 — неделя {week['week']}" if week else "Расписание группы 6381"
    lines = [f"{title}"]
    days = (schedule or {}).get("days") or {}
    for day in DAYS_ORDER:
        if days.get(day):
            lines.append(f"📆 {day}: {len(days[day])} {_pairs_word(len(days[day]))}")
    if len(lines) == 1:
        lines.append("Расписание пока не опубликовано.")
    lines.append(source_url)
    return "\n".join(lines)


def send(rich_message: dict, *, token: str, channel_id: int, timeout: int = 20) -> dict:
    url = f"https://api.telegram.org/bot{token}/sendRichMessage"
    body = {"chat_id": channel_id, **rich_message}
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error_code": exc.code, "description": exc.read().decode(errors="replace")[:600]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def render_schedule_html(
    schedule: dict | None,
    weeks: list[dict],
    source_url: str,
    today: dt.date | None = None,
    group_name: str = "6381",
) -> str:
    """Render the timetable as a clean standalone HTML page (for screenshot).

    Subjects are grouped by week parity inside each day, with colour-coded
    labels, so it's visually clear which subjects are upper/lower week.
    """
    today = today or dt.date.today()
    week = find_current_week(weeks, today)
    days = (schedule or {}).get("days") or {}

    parts: list[str] = [
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<style>",
        "body{font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif;background:#eef1f5;margin:0;padding:22px}",
        ".wrap{max-width:840px;margin:0 auto;background:#fff;border-radius:14px;padding:22px 24px;box-shadow:0 6px 24px rgba(0,0,0,.08)}",
        "h1{font-size:23px;margin:0 0 2px}",
        ".sub{color:#475569;font-size:14px;margin-bottom:14px}",
        ".week{display:inline-block;background:#2563eb;color:#fff;font-size:12px;font-weight:700;border-radius:7px;padding:2px 9px;margin-left:8px}",
        "h2{font-size:17px;margin:16px 0 4px;border-bottom:2px solid #cbd5e1;padding-bottom:3px}",
        ".tag{display:inline-block;font-size:12px;font-weight:700;border-radius:6px;padding:2px 9px;margin:8px 0 3px}",
        ".up{background:#dbeafe;color:#1e3a8a}", ".lo{background:#fef3c7;color:#92400e}", ".ev{background:#e5e7eb;color:#374151}",
        "table{border-collapse:collapse;width:100%;font-size:13px;margin:0 0 4px}",
        "th,td{border:1px solid #e2e8f0;padding:5px 7px;text-align:left;vertical-align:top}",
        "th{background:#f8fafc;font-weight:600}",
        ".n,.t,.r{white-space:nowrap}",
        ".star{color:#b45309;font-weight:700}",
        ".note{font-size:12px;color:#475569;margin:2px 0 3px 12px;line-height:1.35}",
        ".foot{color:#64748b;font-size:12px;margin-top:16px;border-top:1px solid #e2e8f0;padding-top:10px}",
        "</style></head><body><div class='wrap'>",
    ]
    title = f"Расписание группы {_html.escape(group_name)}"
    if week:
        half = WEEK_HALF_NAME.get(week.get("half"), week.get("half", ""))
        title += f"<span class='week'>Неделя {week['week']} · {_html.escape(half)}</span>"
        parts.append(f"<h1>{title}</h1>")
        parts.append(f"<div class='sub'>{_html.escape(week['start'])} — {_html.escape(week['end'])}</div>")
    else:
        parts.append(f"<h1>{title}</h1>")
        parts.append("<div class='sub'>учебная неделя не определена — расписание на семестр</div>")

    rendered = 0
    for day in DAYS_ORDER:
        pairs = days.get(day) or []
        if not pairs:
            continue
        rendered += 1
        parts.append(f"<h2>{_html.escape(day)} — {len(pairs)} {_pairs_word(len(pairs))}</h2>")
        groups: dict[str, list[dict]] = {"upper": [], "lower": [], "every": []}
        for index, pair in enumerate(pairs, 1):
            fields = _lesson_fields(pair, index)
            groups[_parity_of(fields["note"])].append(fields)
        for parity in ("upper", "lower", "every"):
            items = groups[parity]
            if not items:
                continue
            label = {"upper": "Верхняя неделя · 1, 3, 5…", "lower": "Нижняя неделя · 2, 4, 6…", "every": "Каждую неделю"}[parity]
            cls = {"upper": "up", "lower": "lo", "every": "ev"}[parity]
            parts.append(f"<div class='tag {cls}'>{_html.escape(label)}</div>")
            parts.append("<table><thead><tr><th>№</th><th>предмет</th><th>время</th><th>ауд.</th><th>преподаватель</th></tr></thead><tbody>")
            notes: list[tuple[str, str]] = []
            for fields in items:
                condition = _strip_parity(fields["note"])
                star = " <span class='star'>*</span>" if condition else ""
                parts.append(
                    "<tr>"
                    f"<td class='n'>{_html.escape(fields['number'])}</td>"
                    f"<td>{_html.escape(fields['subject'])}{star}</td>"
                    f"<td class='t'>{_html.escape(fields['time']).replace(chr(10), '<br>')}</td>"
                    f"<td class='r'>{_html.escape(fields['room'])}</td>"
                    f"<td>{_html.escape(fields['teacher'])}</td>"
                    "</tr>"
                )
                if condition:
                    notes.append((fields["subject"], condition))
            parts.append("</tbody></table>")
            for subject, condition in notes:
                parts.append(f"<div class='note'>• {_html.escape(subject)} — {_html.escape(condition)}</div>")
    if not rendered:
        parts.append("<div class='sub'>На портале пока нет опубликованных занятий.</div>")
    parts.append(f"<div class='foot'>источник · portal.novsu.ru · {_html.escape(source_url)}</div>")
    parts.append("</div></body></html>")
    return "".join(parts)


def _plain(value: object) -> str:
    return str(value if value is not None and value != "" else "—")


def _esc(value: object) -> str:
    return _html.escape(_plain(value))


def _escape_truncated(value: object, max_encoded_length: int) -> str:
    """Escape text without ever cutting an HTML entity.

    The limit applies to the serialized HTML, not only to visible characters.
    """
    if max_encoded_length <= 0:
        return ""
    raw = _plain(value)
    escaped = _html.escape(raw)
    if len(escaped) <= max_encoded_length:
        return escaped
    if max_encoded_length == 1:
        return "…"
    out: list[str] = []
    length = 0
    for character in raw:
        unit = _html.escape(character)
        if length + len(unit) + 1 > max_encoded_length:
            break
        out.append(unit)
        length += len(unit)
    return "".join(out) + "…"


def _split_plain_for_html(value: object, max_encoded_length: int) -> list[str]:
    """Split plain text into escaped pieces without splitting HTML entities."""
    if max_encoded_length < 1:
        raise ValueError("max_encoded_length must be positive")
    raw = _plain(value)
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for character in raw:
        unit = _html.escape(character)
        if current and current_length + len(unit) > max_encoded_length:
            chunks.append("".join(current))
            current = []
            current_length = 0
        # A single escaped character is currently at most 6 characters.  Keep
        # this guard so the helper remains correct if html.escape changes.
        if len(unit) > max_encoded_length:
            if current:
                chunks.append("".join(current))
                current = []
                current_length = 0
            chunks.append(_escape_truncated(character, max_encoded_length))
            continue
        current.append(unit)
        current_length += len(unit)
    if current:
        chunks.append("".join(current))
    return chunks or [""]


def _header_message(group_name: str, week: dict | None, source_url: str) -> str:
    lines = [f"<b>Расписание группы {_escape_truncated(group_name, 120)}</b>"]
    if week:
        half = WEEK_HALF_NAME.get(week.get("half"), week.get("half") or "")
        lines.append(f"<b>Неделя {_escape_truncated(week['week'], 30)} ({_escape_truncated(half, 60)})</b>")
        lines.append(
            f"<i>{_escape_truncated(week['start'], 40)} — "
            f"{_escape_truncated(week['end'], 40)}</i>"
        )
    lines.extend([
        "",
        "<b>ВЕРХНЯЯ</b> 1, 3, 5, 7…  нечётные учебные недели",
        "<b>НИЖНЯЯ</b> 2, 4, 6, 8…  чётные учебные недели",
        "Важно: недели считаются по учебному календарю НовГУ, а не по календарным неделям года.",
        f"<b>Время</b> {_escape_truncated(bells.LEGEND, 240)}",
        "<b>*</b> у предмета означает, что условие раскрыто в разделе «Примечания».",
        "<b>ДОТ</b> — дистанционные образовательные технологии.",
        "<b>лек.</b> — лекция   <b>пр.</b> — практическое занятие   <b>лаб.</b> — лабораторная работа",
        "<b>ауд.</b> — аудитория   <b>подгр.</b> — подгруппа",
        "",
        f'<a href="{_escape_truncated(source_url, 240)}">открыть на портале</a>',
    ])
    message = "\n".join(lines)
    # The fixed glossary plus bounded dynamic fields stays below the caption
    # limit.  Keep this as a contract assertion instead of slicing HTML.
    if len(message) > 1024:
        compact = [lines[0], *(lines[1:3] if week else []), lines[-1]]
        message = "\n".join(compact)
    if len(message) > 1024:  # Defensive: only possible with future edits.
        raise ValueError("schedule header exceeds Telegram's 1024-character caption limit")
    return message


def _day_chunks(day: str, pairs: list) -> list[str]:
    """Render one day as self-contained HTML chunks of at most 4096 chars."""
    count = len(pairs)
    header = f"<b>{_esc(day)} — {count} {_pairs_word(count)}</b>"
    continued_header = f"<b>{_esc(day)} — продолжение</b>"
    table_head = "№ | предмет | время | ауд. | преподаватель"
    raw_rows: list[str] = []
    raw_notes: list[str] = []
    for index, pair in enumerate(pairs, 1):
        number = pair.get("number") or index
        full_subject = pair.get("subject") or pair.get("subject_raw") or pair.get("name") or ""
        subject = full_subject.split("\n", 1)[0] if full_subject else "—"
        pair_time = bells.slot(pair.get("time")).label()
        room = pair.get("room") or "—"
        teacher = pair.get("teacher") or "—"
        note = pair.get("note") or pair.get("raw_comment") or ""
        marker = " *" if note else ""
        raw_rows.append(
            f"{_plain(number)} | {_plain(subject)}{marker} | {_plain(pair_time)} | "
            f"{_plain(room)} | {_plain(teacher)}"
        )
        if note:
            raw_notes.append(f"• {_plain(subject)} — {_plain(note)}")

    chunks: list[str] = []
    current_rows: list[str] = []
    current_header = header

    def render_rows(rows: list[str], heading: str) -> str:
        return heading + "\n<pre>\n" + table_head + "\n" + "\n".join(rows) + "\n</pre>"

    def flush() -> None:
        nonlocal current_header
        if not current_rows:
            return
        chunk = render_rows(current_rows, current_header)
        if len(chunk) > 4096:
            raise ValueError("internal error: oversized schedule HTML chunk")
        chunks.append(chunk)
        current_rows.clear()
        current_header = continued_header

    # Reserve the largest wrapper.  Escaped pieces can then always be wrapped
    # in a complete <pre> block without cutting an entity or a tag.
    row_budget = 4096 - len(continued_header + "\n<pre>\n" + table_head + "\n\n</pre>")
    for raw_row in raw_rows:
        for row_piece in _split_plain_for_html(raw_row, row_budget):
            projected = render_rows(current_rows + [row_piece], current_header)
            if current_rows and len(projected) > 4096:
                flush()
            current_rows.append(row_piece)
    flush()

    for raw_note in raw_notes:
        note_pieces = _split_plain_for_html(raw_note, 4096)
        for piece in note_pieces:
            if chunks and len(chunks[-1]) + 2 + len(piece) <= 4096:
                chunks[-1] += "\n\n" + piece
            else:
                chunks.append(piece)
    return chunks or [header]


def _changes_summary_chunks(changes_summary: object) -> list[str]:
    header = "Что изменилось\n"
    budget = 4096 - len(header)
    return [header + piece for piece in _split_plain_for_html(changes_summary, budget)]


def format_schedule_post(schedule, weeks, source_url, today=None, changes_summary="", group_name="6381") -> list[str]:
    """Render the timetable as one-or-more valid HTML messages.

    Message 0 is suitable for a photo caption (at most 1024 characters).  All
    messages stay at or below Telegram's 4096-character text limit, including
    pathological single lessons and notes.
    """
    today = today or dt.date.today()
    week = find_current_week(weeks, today)
    messages: list[str] = [_header_message(group_name, week, source_url)]
    days = (schedule or {}).get("days") or {}
    buf = ""
    for day in DAYS_ORDER:
        pairs = days.get(day) or []
        if not pairs:
            continue
        for chunk in _day_chunks(day, pairs):
            if not buf:
                buf = chunk
            elif len(buf) + 2 + len(chunk) <= 4096:
                buf = buf + "\n\n" + chunk
            else:
                messages.append(buf)
                buf = chunk
    if buf:
        messages.append(buf)
    if not any(days.get(day) for day in DAYS_ORDER):
        messages.append("На портале пока нет опубликованных занятий на эту неделю.")
    if changes_summary:
        messages.extend(_changes_summary_chunks(changes_summary))
    if not all(len(message) <= 4096 for message in messages):
        raise ValueError("internal error: oversized Telegram fallback message")
    return messages


DAY_TO_SHORT = {full: short for short, full in DAY_SHORT_TO_FULL.items()}


def changed_days(diff: dict) -> list[str]:
    """Полные имена дней из диффа в порядке недели (Понедельник … Воскресенье)."""
    found: list[str] = []
    for key in ("added", "removed", "changed"):
        for item in diff.get(key) or []:
            day = str(item.get("day") or "").strip()
            if day and day not in found:
                found.append(day)
    order = {name: index for index, name in enumerate(DAYS_ORDER)}
    return sorted(found, key=lambda day: order.get(day, len(order)))


def pick_day_screens(diff: dict, items: list[dict] | None) -> list[dict]:
    """Оставить только скрины тех дней, которые реально поменялись.

    *items* — [{label, path}] от скриншоттера, где label это короткий день
    портала («Ср») либо склейка «Пн + Вт» при days_per_chunk > 1. Скрин берём,
    если хотя бы один день из его метки есть в диффе: картинка всё равно
    показывает день целиком.
    """
    wanted = set(changed_days(diff))
    if not wanted or not items:
        return []
    picked = []
    for item in items:
        if not isinstance(item, dict) or not item.get("path"):
            continue
        label = str(item.get("label") or "").strip()
        parts = [DAY_SHORT_TO_FULL.get(part.strip(), part.strip()) for part in label.split("+")]
        if any(part in wanted for part in parts):
            picked.append(item)
    return picked


def _changes_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "изменение"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "изменения"
    return "изменений"


def _truncate_rich_text(value: object, limit: int) -> str:
    text = _plain(value)
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _diff_lesson_row(item: dict) -> list[tuple[object, str]]:
    day_value = str(item.get("day") or "—")
    day = DAY_TO_SHORT.get(day_value, day_value)
    subject = str(item.get("subject") or "—").split("\n", 1)[0]
    return [
        (_truncate_rich_text(day, 24), "code"),
        (_truncate_rich_text(bells.slot(item.get("time")).label(), 48), "code"),
        (_truncate_rich_text(subject, 240), "accent"),
        (_truncate_rich_text(item.get("room"), 80), "code"),
        (_truncate_rich_text(item.get("teacher"), 160), "bold"),
    ]


_DIFF_TABLE_HEAD = [
    ("день", "bold"), ("время", "bold"), ("предмет", "bold"),
    ("ауд.", "bold"), ("преподаватель", "bold"),
]
_RICH_DIFF_TABLE_CHUNK = 25
_RICH_DIFF_MAX_PER_KIND = 120
_RICH_DIFF_TEXT_LIMIT = 30_000
_RICH_DIFF_BLOCK_LIMIT = 450
_MSK = zoneinfo.ZoneInfo("Europe/Moscow")


def _as_msk(value: dt.datetime) -> dt.datetime:
    """Одна точка нормировки «наивная дата → московское время»."""
    if value.tzinfo is not None:
        return value.astimezone(_MSK)
    return value.replace(tzinfo=_MSK)


def _rich_payload_stats(value: object) -> tuple[int, int]:
    """Return conservative (display-text chars, counted blocks) for limits."""
    text_chars = 0
    blocks = 0

    def walk(node: object, parent_key: str | None = None) -> None:
        nonlocal text_chars, blocks
        if isinstance(node, str):
            if parent_key not in {"type", "align", "valign", "url", "media"}:
                text_chars += len(node)
            return
        if isinstance(node, list):
            for child in node:
                walk(child, parent_key)
            return
        if not isinstance(node, dict):
            return
        block_type = node.get("type")
        if block_type in {
            "paragraph", "heading", "pre", "footer", "divider", "anchor",
            "list", "blockquote", "expandable_blockquote", "pullquote",
            "collage", "slideshow", "table", "details", "map", "buttons",
            "animation", "audio", "document", "photo", "video", "voice_note",
            "thinking",
        }:
            blocks += 1
            if block_type == "table":
                # Telegram explicitly counts table rows toward the 500-block limit.
                blocks += len(node.get("cells") or [])
        for key, child in node.items():
            walk(child, key)

    walk(value)
    return text_chars, blocks


def _chunked(values: list, size: int) -> list[list]:
    return [values[index:index + size] for index in range(0, len(values), size)]


_KIND_MARK = {"added": "+", "removed": "−", "changed": "~", "moved": "↔"}
#: Больше строк в таблице дня не показываем: остальное — на портале.
_RICH_DIFF_MAX_ROWS_PER_DAY = 40
_KIND_RANK = {"added": 0, "removed": 1, "changed": 2}

# Человеческие названия правок: читателю нужен глагол, а не значок.
_KIND_TITLE = {
    "moved": "Перенесли",
    "changed": "Изменили",
    "added": "Добавили",
    "removed": "Убрали",
}
_KIND_ORDER = {"moved": 0, "changed": 1, "added": 2, "removed": 3}
# Поля, по которым сравниваем «убрали» и «добавили», чтобы поймать перенос
# одной и той же пары на другое время.
_MOVE_KEY_FIELDS = ("subject", "subgroup", "teacher")


def _move_key(item: dict) -> tuple:
    """Ключ «та же самая пара» без времени: предмет, подгруппа, преподаватель."""
    subject = str(item.get("subject") or "").split("\n", 1)[0].strip().casefold()
    return (
        subject,
        str(item.get("subgroup") or "").strip().casefold(),
        str(item.get("teacher") or "").strip().casefold(),
    )


def _pair_moves(
    removed: list[dict], added: list[dict]
) -> tuple[list[tuple[dict, dict]], list[dict], list[dict]]:
    """Склеить «убрали в 11:00» + «добавили в 17:00» в один перенос.

    Портал не сообщает о переносе: он показывает пару на новом месте и убирает
    со старого. Две несвязанные строки читаются как две разные правки, поэтому
    внутри дня одинаковые пары (предмет, подгруппа, препод) сводим в пару
    «было → стало». Остальное остаётся честным добавлением или убиранием.
    """
    removed_groups: dict[tuple, list[int]] = {}
    added_groups: dict[tuple, list[int]] = {}
    for items, groups in ((removed, removed_groups), (added, added_groups)):
        for index, item in enumerate(items):
            key = (
                _move_key(item), str(item.get("day") or ""),
                _parity_of(str(item.get("note") or "")),
            )
            groups.setdefault(key, []).append(index)

    moves: list[tuple[dict, dict]] = []
    used_removed: set[int] = set()
    used_added: set[int] = set()
    for key, gone_indices in removed_groups.items():
        arrived_indices = added_groups.get(key, [])
        # Repeated lessons have no stable row identity: never guess a pairing.
        if len(gone_indices) != 1 or len(arrived_indices) != 1:
            continue
        gone_index, arrived_index = gone_indices[0], arrived_indices[0]
        gone, arrived = removed[gone_index], added[arrived_index]
        old_hours = bells.parse_hours(gone.get("time"))
        new_hours = bells.parse_hours(arrived.get("time"))
        if not old_hours or not new_hours or old_hours == new_hours:
            continue
        used_removed.add(gone_index)
        used_added.add(arrived_index)
        moves.append((gone, arrived))
    return (
        moves,
        [item for index, item in enumerate(removed) if index not in used_removed],
        [item for index, item in enumerate(added) if index not in used_added],
    )

_DAY_TABLE_HEAD = [
    ("что", "bold"), ("время", "bold"), ("предмет", "bold"),
    ("ауд.", "bold"), ("преподаватель", "bold"),
]


def _day_change_row(kind: str, item: dict) -> list[tuple[object, str]]:
    """Строка таблицы дня: знак правки, время парой, предмет, аудитория, препод."""
    subject = str(item.get("subject") or "—").split("\n", 1)[0]
    return [
        (_KIND_MARK.get(kind, "?"), "code"),
        (_truncate_rich_text(bells.slot(item.get("time")).label(), 48), "code"),
        (_truncate_rich_text(subject, 240), "accent"),
        (_truncate_rich_text(item.get("room"), 80), "code"),
        (_truncate_rich_text(item.get("teacher"), 160), "bold"),
    ]


#: Так портал печатает пустую аудиторию: прочерк, точка, дефис.
_EMPTY_ROOM = {"", "—", "–", "-", ".", "нет", "не указано"}

#: «с использованием ДОТ» — не примечание, а формат: пост и так пишет «дистанционно».
_DOT_RE = re.compile(r"\s*[;,]?\s*с\s+использованием\s+дот\b\.?", re.I)

#: Портал сокращает язык в примечании, и «немец. яз» без контекста пары
#: читается как шум. Раскрываем только эти сокращения, остальное не трогаем.
_LANGUAGE_ABBREV = {
    "англ": "английский язык",
    "нем": "немецкий язык",
    "кит": "китайский язык",
    "исп": "испанский язык",
    "фр": "французский язык",
    "итал": "итальянский язык",
    "япон": "японский язык",
    "кор": "корейский язык",
}
#: Портал сокращает по-разному: «нем. яз» и «немец. яз» — одно и то же.
_LANGUAGE_RE = re.compile(
    r"\b(англ|нем(?:ец)?|кит|исп|фр(?:анц)?|итал|япон|кор)\.\s*яз(?:ык)?\.?(?!\w)", re.I
)


def _expand_language(match: "re.Match[str]") -> str:
    stem = match.group(1).lower()
    if stem.startswith("нем"):
        stem = "нем"
    elif stem.startswith("фр"):
        stem = "фр"
    return _LANGUAGE_ABBREV[stem]


def _where(item: dict, *, skip_room: bool = False) -> str:
    """Keep physical location and DOT when the portal specifies both.

    ``skip_room`` убирает номер аудитории, который строка правки уже показала
    как новое значение: повтор «1331» второй раз ничего не добавляет, а корпус
    и формат остаются.
    """
    room = str(item.get("room") or "").strip().strip(".").strip()
    location = str(item.get("location") or "").strip()
    parts: list[str] = []
    if not skip_room and room.casefold() not in _EMPTY_ROOM:
        parts.append(f"ауд. {room}")
    if location and location.casefold() not in " ".join(parts).casefold():
        parts.append(location)
    mode = str(item.get("delivery_mode") or "").strip().casefold()
    if mode.startswith("remote") or _DOT_RE.search(str(item.get("note") or "")):
        parts.append("ДОТ")
    return ", ".join(parts) if parts else "место не указано"


def _subject_first(item: dict) -> str:
    """Название пары без хвоста-примечания (первая строка поля subject)."""
    return str(item.get("subject") or "—").split("\n", 1)[0].strip()


def _note_brief(item: dict) -> str:
    """Примечание пары без повторов: место и формат пост уже назвал отдельно."""
    note = _strip_parity(str(item.get("note") or ""))
    note = _DOT_RE.sub(" ", note)
    note = _LANGUAGE_RE.sub(_expand_language, note)
    location = str(item.get("location") or "").strip()
    if location:
        note = re.sub(rf"[;,]?\s*{re.escape(location)}\b", " ", note, flags=re.I)
    return re.sub(r"\s{2,}", " ", note).strip(" ,;.")


def _variants_of(item: dict) -> list[dict]:
    """Записи недели, которые схлопнулись в одну карточку (верх и низ)."""
    variants = [v for v in (item.get("_variants") or []) if isinstance(v, dict)]
    return variants or [item]


def _parities_of(item: dict) -> list[str]:
    return [str(p) for p in (item.get("_parities") or [])]


def _covers_both_weeks(item: dict) -> bool:
    """Правка приехала сразу в верхней и нижней неделе — пишем «обе недели»."""
    return {"upper", "lower"} <= set(_parities_of(item))


def _entry_brief(kind: str, item: dict) -> str:
    """Короткое «что случилось» для сводки дня: «сменили аудиторию»."""
    if kind == "moved":
        return "перенесли пару"
    if kind == "added":
        return "добавили пару"
    if kind == "removed":
        return "убрали пару"
    labels = {
        str(field[0])
        for field in (item.get("fields") or [])
        if isinstance(field, (list, tuple)) and len(field) == 3
    }
    if labels == {"предмет"}:
        return "переименовали пару"
    if labels == {"преподаватель"} and all(
        _missing_change_value(field[1]) and not _missing_change_value(field[2]) for field in item.get("fields", [])
        if isinstance(field, (list, tuple)) and len(field) == 3
    ):
        return "указали преподавателя"
    if labels == {"преподаватель"} and all(
        _missing_change_value(field[2]) for field in item.get("fields", [])
        if isinstance(field, (list, tuple)) and len(field) == 3
    ):
        return "преподаватель больше не указан"
    if labels and labels <= {"примечание", "место", "формат"}:
        return "изменили условия"
    if labels:
        words = {"предмет": "название", "преподаватель": "преподавателя",
                 "ауд.": "аудиторию"}
        parts = []
        for label, _old, _new in item["fields"]:
            word = words.get(label, label)
            if word not in parts:
                parts.append(word)
        if parts:
            return "сменили " + " и ".join(parts)
    return "обновили пару"


def _action_label(brief: str, count: int) -> str:
    if count == 1:
        return brief
    if brief == "преподаватель больше не указан":
        return "преподаватели больше не указаны"
    for singular, plural in (("преподавателя", "преподавателей"),
                             ("аудиторию", "аудитории"), ("название", "названия"),
                             ("пару", "пары")):
        brief = brief.replace(singular, plural)
    return brief


def _day_action_groups(entries: list[tuple[str, dict, dict | None]]) -> list[tuple[str, list]]:
    groups: dict[str, list] = {}
    for entry in entries:
        groups.setdefault(_entry_brief(entry[0], entry[1]), []).append(entry)
    return list(groups.items())


def _day_summary(day: str, entries: list[tuple[str, dict, dict | None]]) -> str:
    """Name each kind of edit once, using ordinary words instead of multipliers."""
    parts = []
    for brief, group in _day_action_groups(entries):
        count = len(group)
        label = _action_label(brief, count)
        if count > 1:
            label += f" ({count} {_pairs_word(count)})"
        parts.append(label)
    if len(entries) == 1:
        return _truncate_rich_text(f"{day} ({', '.join(parts)})", 180)
    return _truncate_rich_text(f"{day} · {', '.join(parts)}", 180)


#: Предел длины добавленного хвоста, который ещё удобно показать подсветкой.
_RENAME_TAIL_LIMIT = 160


def _rename_tail(item: dict) -> str | None:
    """Добавленный хвост переименования, если старое название — его префикс.

    Портал обычно не переименовывает пару целиком, а уточняет её: «Иностранные
    языки …» → «Иностранные языки … (второй иностранный язык)». Печатать оба
    названия целиком — три строки на мобильном ради одного уточнения, поэтому
    показываем название один раз и подсвечиваем добавленный хвост.
    """
    old = new = ""
    for field in item.get("fields") or []:
        if isinstance(field, (list, tuple)) and len(field) == 3 and str(field[0]) == "предмет":
            old = str(field[1] or "").split("\n", 1)[0].strip()
            new = str(field[2] or "").split("\n", 1)[0].strip()
    if not old or not new or old == new or not new.startswith(old):
        return None
    tail = new[len(old):].strip()
    if not 0 < len(tail) <= _RENAME_TAIL_LIMIT:
        return None
    # Хвост подсвечиваем только внутри того названия, которое пост печатает.
    return tail if _subject_first(item).endswith(tail) else None


_SUBJECT_KIND_RE = re.compile(r"^\(([^)]{1,16})\)\s*(.+)$", re.S)


def _subject_kind(subject: str) -> tuple[str, str]:
    """«(пр.) География туризма» → («География туризма», «пр.»); без префикса — («X», «»)."""
    match = _SUBJECT_KIND_RE.match(subject.strip())
    if match:
        return match.group(2).strip(), match.group(1).strip()
    return subject.strip(), ""


def _building_key(room: object) -> str | None:
    """Здание аудитории для сравнения «тот же корпус или другой».

    В смене аудитории портал адрес не пишет, поэтому выводим его из номера
    тем же декодером, что и гайд первокурсника. Номер не читается — молчим,
    выдумывать корпус нельзя.
    """
    room = str(room or "").strip()
    if not room:
        return None
    # Поточная аудитория — особый случай
    stream = re.match(r"^(\d{1,4})\s*поточн", room, re.I)
    if stream:
        digits = stream.group(1)
        return _guide_korpus_place(digits[0]) if len(digits) == 1 else _guide_building(digits)
    if "поточн" in room.lower():
        return "поточная аудитория"
    # Используем resolve_location с контекстом Антоново для единообразия с гидом
    # и старым поведением _guide_building: 3-значные номера в контексте гида/сравнения
    # всегда считались Антоново (новый корпус).
    result = resolve_location(room, context_hints=["Антоново"])
    if result["confidence"] == "low" and not result["building"]:
        return None
    if result["campus"] == "other":
        return result["address"]
    if result["campus"] == "antonovo" and ("ИГУМ" in room.upper() or "ИГ" in room.upper()):
        return f"ИГУМ, {result['building']}"
    return result["building"] or None


def _corpus_tail(item: dict) -> str:
    """Хвост про смену корпуса — только когда новая аудитория в другом здании."""
    for field in item.get("fields") or []:
        if not (isinstance(field, (list, tuple)) and len(field) == 3):
            continue
        label, old, new = field
        if str(label) != "ауд.":
            continue
        old_key, new_key = _building_key(old), _building_key(new)
        if old_key and new_key and old_key != new_key:
            return f" — это уже другой корпус: {_truncate_rich_text(new_key, 80)}"
    return ""


def _parity_context(item: dict) -> str:
    """«верхняя неделя» / «нижняя неделя» / «обе недели» или пусто."""
    if _covers_both_weeks(item):
        return "обе недели"
    return {"upper": "верхняя неделя", "lower": "нижняя неделя"}.get(
        _parity_of(str(item.get("note") or "")), "")


def _sentence_ctx(item: dict) -> list:
    """Сегменты контекста в скобках: «пр.», курсивом время, «верхняя неделя»."""
    _name, kind_abbrev = _subject_kind(_subject_first(item))
    segments: list = []
    if kind_abbrev:
        segments.append(_truncate_rich_text(kind_abbrev, 16))
    when = bells.slot(item.get("time")).label()
    if when and when != "—":
        segments.append({"type": "italic", "text": _truncate_rich_text(when, 48)})
    parity = _parity_context(item)
    if parity:
        segments.append(parity)
    return segments


def _join_ctx(segments: list) -> list:
    """Склеить сегменты контекста: «пр., 09:00–10:45 · верхняя неделя»."""
    out: list = []
    for index, segment in enumerate(segments):
        if index:
            previous, current = segments[index - 1], segment
            glue = ", " if isinstance(previous, str) and previous.endswith(".") \
                and isinstance(current, dict) else " · "
            out.append(glue)
        out.append(segment)
    return out


def _is_remote(item: dict) -> bool:
    mode = str(item.get("delivery_mode") or "").strip().casefold()
    return mode.startswith("remote") or bool(_DOT_RE.search(str(item.get("note") or "")))


#: Человеческие названия полей правки для многосоставного предложения.
_FIELD_CLAUSE = {
    "предмет": "название", "ауд.": "аудитория", "преподаватель": "преподаватель",
    "примечание": "условия", "место": "место", "формат": "формат",
}

_FORMAT_MODES = {"in_person": "без ДОТ", "remote": "ДОТ", "remote_or_hybrid": "с использованием ДОТ"}


def _changed_note(item: dict, *, with_day: bool = True) -> dict:
    """Заметка «что именно поменялось» в одной записи: было → стало."""
    day_value = str(item.get("day") or "—")
    day = DAY_TO_SHORT.get(day_value, day_value)
    subject = str(item.get("subject") or "—").split("\n", 1)[0]
    time_label = _truncate_rich_text(bells.slot(item.get("time")).label(), 48)
    header = f"{_truncate_rich_text(subject, 240)}"
    if with_day:
        header = f"{_truncate_rich_text(day, 24)} {time_label} — {header}"
    else:
        header = f"{time_label} — {header}"
    field_lines: list[str] = []
    fields = item.get("fields") or []
    for field in fields[:8]:
        if not isinstance(field, (list, tuple)) or len(field) != 3:
            continue
        label, old, new = field
        field_lines.append(
            f"{_truncate_rich_text(label, 60)}: "
            f"{_truncate_rich_text(old, 180)} → {_truncate_rich_text(new, 180)}"
        )
    if len(fields) > 8:
        field_lines.append(f"… ещё {len(fields) - 8} полей")
    return _note(header, "\n".join(field_lines) or "изменение")


#: Какие цвета подсветки что значат: подпись скрина обещает только реально помеченное.
_MARK_COLOR = {"added": "зелёным новые пары", "changed": "оранжевым правки"}


def _marks_caption(media: dict) -> str:
    """Часть подписи скрина про подсветку.

    Раньше подпись всегда перечисляла оба цвета, и на посте с одним
    переименованием читатель искал зелёные «новые пары», которых там нет.
    Если портал отдал скрин без информации о типах правок, оставляем общую
    формулировку — она честная, просто менее точная.
    """
    if not int(media.get("marked") or 0) or media.get("highlighted") is False:
        return ""
    known = [_MARK_COLOR[str(kind)] for kind in (media.get("kinds") or []) if str(kind) in _MARK_COLOR]
    if known:
        return " · " + ", ".join(known)
    return " · зелёным новые пары, оранжевым правки"


def _media_for_day(
    day: str,
    screenshot_media: list[dict] | None,
    used: set[str],
) -> tuple[dict | None, list[str]]:
    """Attach://-скрин дня и короткие имена дней, которым он тоже принадлежит.

    Один файл используем ровно раз: при склейке «Пн + Вт» картинка попадает в
    первый подходящий блок дня, остальным дням остаётся текстовая пометка.
    Возвращаем сам элемент медиа, чтобы подпись знала про подсветку правок.
    """
    for item in screenshot_media or []:
        if not isinstance(item, dict):
            continue
        media = str(item.get("media") or "").strip()
        if not media:
            continue
        parts = [part.strip() for part in str(item.get("label") or "").split("+") if part.strip()]
        full = [DAY_SHORT_TO_FULL.get(part, part) for part in parts]
        if day in full:
            others = [DAY_TO_SHORT.get(name, name) for name in full if name != day]
            if media in used:
                return None, others
            used.add(media)
            return item, others
    return None, []


def _comparison_for_day(
    day: str, screenshot_media: list[dict] | None, kinds: set[str],
) -> dict | None:
    """Скрины «было/стало» дня — сам блок, без обёртки: он уже внутри раздела дня."""
    for item in screenshot_media or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if DAY_SHORT_TO_FULL.get(label, label) != day:
            continue
        photos = []
        before = str(item.get("before_media") or "").strip()
        after = str(item.get("after_media") or "").strip()
        if before:
            photos.append(_photo(before, [{"type": "bold", "text": "Было"}, f" · {day}"]))
        if after:
            photos.append(_photo(after, [{"type": "bold", "text": "Стало"}, f" · {day}"]))
        if not photos:
            return None
        if len(photos) == 1:
            return photos[0]
        return _details("Сравнить расписание", _slideshow(photos, "Было → стало"))
    return None


def _blockquote(title: list[object] | str, body: list[dict], *,
                expandable: bool = False) -> dict:
    """Цитата: заголовок в ``text``, содержимое — отдельными блоками.

    Bot API отбивает пустой ``text`` (RICH_MESSAGE_CONTENT_REQUIRED) и цитату
    без ``blocks`` (RICH_MESSAGE_EMPTY) — нужны обе части непустыми.
    """
    return {
        "type": "expandable_blockquote" if expandable else "blockquote",
        "text": title,
        "blocks": body,
    }


#: Заглушки не зачёркиваем: перечёркнутое «не указано» читается как правка.
_VALUE_PLACEHOLDERS = {"", "—", "-", ".", "не указано", "не указаны", "название не указано"}


def _missing_change_value(value: object) -> bool:
    return str(value or "").strip().casefold() in _VALUE_PLACEHOLDERS


def _old_value_runs(value: str, limit: int) -> list[object]:
    """Старое значение — зачёркнуто, кроме пустых заглушек."""
    text = _truncate_rich_text(value, limit)
    if str(value).strip().casefold() in _VALUE_PLACEHOLDERS:
        return [text]
    return [{"type": "strikethrough", "text": text}]


def _field_clause(label: str, old: str, new: str) -> list:
    """«аудитория 418̶ → 415» — одно поле внутри многосоставного предложения."""
    if label == "формат":
        old, new = _FORMAT_MODES.get(old, old), _FORMAT_MODES.get(new, new)
    return [
        f"{_FIELD_CLAUSE.get(label, label)} ",
        *_old_value_runs(old or "не указано", 200), " → ",
        {"type": "bold", "text": _truncate_rich_text(new or "не указано", 200)},
    ]


def _change_sentence(kind: str, item: dict, partner: dict | None = None) -> list[dict]:
    """Одна правка — одно предложение.

    «У пары «География туризма» (пр., 09:00–10:45) поменяли аудиторию:
    418̶ → 415.» Старое значение зачёркнуто, новое выделено жирным; место
    называем, только когда пара переехала в другой корпус.
    """
    current = dict(partner or item)
    name, _abbrev = _subject_kind(_subject_first(current))
    quoted: list = ["«", {"type": "bold", "text": _truncate_rich_text(name, 400)}, "»"]

    def with_ctx(prefix: list) -> list:
        ctx = _join_ctx(_sentence_ctx(current))
        return [*prefix, " (", *ctx, ")"] if ctx else list(prefix)

    if kind == "moved" and partner is not None:
        runs: list = [
            "Пару ", *quoted, " перенесли: ",
            *_old_value_runs(bells.slot(item.get("time")).label(), 48), " → ",
            {"type": "bold", "text": _truncate_rich_text(bells.slot(partner.get("time")).label(), 48)},
        ]
        parity = _parity_context(current)
        if parity:
            runs.append(f", {parity}")
        old_place, new_place = _place_line(item, ""), _place_line(partner, "")
        if old_place != new_place:
            runs += [", место тоже: ", *_old_value_runs(old_place, 200), " → ",
                     {"type": "bold", "text": _truncate_rich_text(new_place, 200)}]
        runs.append(".")
        return [_rich_paragraph(runs)]

    if kind == "added":
        segments = _sentence_ctx(current)
        room = str(current.get("room") or "").strip()
        if room.casefold() not in _EMPTY_ROOM:
            segments.append(f"ауд. {_truncate_rich_text(room, 40)}")
        if _is_remote(current):
            segments.append("ДОТ")
        runs = ["Добавили пару ", *quoted]
        if segments:
            runs += [" (", *_join_ctx(segments), ")"]
        runs.append(".")
        blocks = [_rich_paragraph(runs)]
        note = _note_line(current)
        if note:
            blocks.append(_paragraph(_truncate_rich_text(note, 600)))
        return blocks

    if kind == "removed":
        gone: list[str] = []
        when = bells.slot(current.get("time")).label()
        if when and when != "—":
            gone.append(f"в {_truncate_rich_text(when, 48)}")
        parity_gone = {"верхняя неделя": "на верхней неделе",
                       "нижняя неделя": "на нижней неделе",
                       "обе недели": "на обеих неделях"}.get(_parity_context(current))
        if parity_gone:
            gone.append(parity_gone)
        room = str(current.get("room") or "").strip()
        if room.casefold() not in _EMPTY_ROOM:
            gone.append(f"ауд. {_truncate_rich_text(room, 40)}")
        runs = [*quoted, " убрали совсем"]
        if gone:
            runs += [": была ", {"type": "strikethrough",
                                 "text": _truncate_rich_text(", ".join(gone), 700)}]
        runs.append(".")
        return [_rich_paragraph(runs)]

    labels: list[str] = []
    fields: list[tuple[str, str, str]] = []
    for field in item.get("fields") or []:
        if not (isinstance(field, (list, tuple)) and len(field) == 3):
            continue
        label, old, new = str(field[0]), str(field[1] or ""), str(field[2] or "")
        if label not in labels:
            labels.append(label)
        fields.append((label, old, new))

    tail = _rename_tail(item)
    rooms = {str(variant.get("room") or "").strip() for variant in _variants_of(current)}
    rooms = {value for value in rooms if value.casefold() not in _EMPTY_ROOM}
    # «ауд. X прежняя» только когда место одно и то же на всех неделях: иначе
    # за неделю с ДОТ фраза соврёт, а разбор мест и так идёт строкой ниже.
    wheres = [_where(variant) for variant in _variants_of(current)]
    weeks_differ = len(dict.fromkeys(wheres)) > 1
    room_steady = f", ауд. {_truncate_rich_text(next(iter(rooms)), 40)} прежняя" \
        if len(rooms) == 1 and not weeks_differ and "ауд." not in labels else ""

    if labels == ["предмет"]:
        if tail:
            # Хвост подсвечен прямо в названии: «База ==(уточнение)==».
            base = name[: max(0, len(name) - len(tail))].rstrip()
            shown = ["«", {"type": "bold", "text": _truncate_rich_text(base, 400)}, " ",
                     {"type": "marked", "text": _truncate_rich_text(tail, _RENAME_TAIL_LIMIT)}, "»"]
            runs = with_ctx(["Пару ", *shown])
            runs.append(" дополнили в названии")
        else:
            old_name = fields[0][1].split("\n", 1)[0].strip()
            runs = with_ctx(["Пару «", *_old_value_runs(old_name or "название не указано", 400), "»"])
            runs += [" переименовали: теперь «",
                     {"type": "bold", "text": _truncate_rich_text(name, 400)}, "»"]
        runs.append(room_steady)
        runs.append(".")
        blocks = [_rich_paragraph(runs)]
    elif labels:
        # «место» и «формат» выводятся из того же примечания — не дублируем.
        shown_fields = [field for field in fields
                        if not (field[0] in {"место", "формат"} and "примечание" in labels)]
        if len(shown_fields) == 1:
            label, old, new = shown_fields[0]
            verb = {
                "ауд.": "поменяли аудиторию", "преподаватель": "сменился преподаватель",
                "примечание": "изменились условия", "место": "сменилось место",
                "формат": "изменился формат",
            }.get(label, f"изменилось поле «{_FIELD_CLAUSE.get(label, label)}»")
            runs = with_ctx(["У пары ", *quoted])
            runs.append(f" {verb}: ")
            runs += _field_clause(label, old, new)[1:]
            corpus = _corpus_tail(item) if label == "ауд." else ""
            if corpus:
                runs.append(corpus)
            runs.append(".")
        else:
            clauses: list = []
            for index, (label, old, new) in enumerate(shown_fields[:6]):
                if index:
                    clauses.append(", ")
                clauses.extend(_field_clause(label, old, new))
            if len(shown_fields) > 6:
                clauses.append(f", ещё {len(shown_fields) - 6} полей — см. портал")
            runs = with_ctx(["У пары ", *quoted])
            runs.append(" поменялось сразу несколько: ")
            runs += clauses
            corpus = _corpus_tail(item)
            if corpus:
                runs.append(corpus)
            runs.append(".")
        blocks = [_rich_paragraph(runs)]
    else:
        runs = with_ctx(["Пару ", *quoted])
        runs.append(" обновили на портале.")
        blocks = [_rich_paragraph(runs)]

    # Недели с разным местом или форматом показываем построчно, без слэша.
    if weeks_differ:
        blocks.append(_paragraph(_truncate_rich_text(_place_line(current, ""), 600)))
    if "примечание" not in labels:
        note = _note_line(current)
        if note:
            blocks.append(_paragraph(_truncate_rich_text(note, 600)))
    return blocks


def _change_card(kind: str, item: dict, partner: dict | None = None) -> list[dict]:
    """Compact notification: subject, occurrence context, then the actual delta.

    Keep notes attached to their lesson and preserve distinct week variants.
    Complex renames retain the existing lossless prefix highlighting.
    """
    fields = [tuple(field) for field in item.get("fields", [])
              if isinstance(field, (list, tuple)) and len(field) == 3]
    labels = {str(field[0]) for field in fields}
    current = partner or item
    name, _ = _subject_kind(_subject_first(current))
    if "предмет" in labels:
        return _change_sentence(kind, item, partner)
    runs: list = [{"type": "bold", "text": _truncate_rich_text(name, 400)}]
    segments = _sentence_ctx(current)
    if kind == "moved":
        segments = [segment for segment in segments if not isinstance(segment, dict)]
    if kind == "removed":
        segments = [{**segment, "type": "strikethrough"} if isinstance(segment, dict) else segment
                    for segment in segments]
    context = _join_ctx(segments)
    if context:
        runs += ["\n", *context]
    lines: list[list] = []
    if kind == "moved" and partner is not None:
        lines.append(["Перенесли: ", *_old_value_runs(bells.slot(item.get("time")).label(), 48),
                      " → ", {"type": "bold", "text": bells.slot(partner.get("time")).label()}])
        before_place, after_place = _place_line(item, ""), _place_line(partner, "")
        if before_place != after_place:
            lines.append(["Место: ", *_old_value_runs(before_place, 200), " → ",
                          {"type": "bold", "text": _truncate_rich_text(after_place, 200)}])
    elif kind in {"added", "removed"}:
        lines.append([{"type": "bold", "text": "Добавили пару" if kind == "added" else "Убрали пару"}])
        place = _place_line(current, "")
        if place != "место не указано":
            lines.append([{"type": "strikethrough", "text": _truncate_rich_text(place, 600)}]
                         if kind == "removed" else [_truncate_rich_text(place, 600)])
    else:
        shown = [f for f in fields if not (f[0] in {"место", "формат"} and "примечание" in labels)]
        for label, old, new in shown[:6]:
            label = str(label)
            noun = _FIELD_CLAUSE.get(label, label).capitalize()
            if label == "формат":
                old, new = _FORMAT_MODES.get(old, old), _FORMAT_MODES.get(new, new)
            if _missing_change_value(old) and not _missing_change_value(new):
                prefix = "Указали преподавателя" if label == "преподаватель" else noun
                line = [prefix + ": ", {"type": "bold", "text": _truncate_rich_text(new or "не указано", 200)}]
            else:
                line = [noun + ": "]
                if not _missing_change_value(old):
                    line += [*_old_value_runs(old, 200), " → "]
                line += [{"type": "bold", "text": _truncate_rich_text("не указано" if _missing_change_value(new) else new, 200)}]
            lines.append(line)
        if len(shown) > 6:
            lines.append([f"Ещё {len(shown) - 6} полей — на портале"])
        corpus = _corpus_tail(item)
        if corpus:
            lines.append(["Другой корпус: " + corpus.split("корпус: ", 1)[-1]])
        if not shown:
            lines.append(["Обновили на портале"])
        # A teacher-only edit must not repeat unchanged rooms and addresses.
        if labels & {"ауд.", "место", "формат", "примечание"}:
            places = {_where(variant) for variant in _variants_of(current)}
            if len(places) > 1:
                lines.append([_truncate_rich_text(_place_line(current, ""), 600)])
    if "примечание" not in labels:
        note = _note_line(current)
        if note:
            lines.append([{"type": "italic", "text": _truncate_rich_text(note, 600)}])
    for line in lines:
        runs += ["\n", *line]
    return [_rich_paragraph(runs)]


def _variant_signature(kind: str, item: dict, partner: dict | None) -> tuple:
    """Подпись правки без указания верхней/нижней недели.

    Портал хранит пару двумя строками — верх и низ, — и правка приезжает сразу
    в обеих. Без склейки пост печатает две одинаковые карточки вместо одной
    правки «на обеих неделях».
    """
    fields = tuple(
        (str(field[0]), _strip_parity(str(field[1])), _strip_parity(str(field[2])))
        for field in (item.get("fields") or [])
        if isinstance(field, (list, tuple)) and len(field) == 3
    )
    return (
        kind,
        tuple(bells.parse_hours(item.get("time"))),
        tuple(bells.parse_hours(partner.get("time"))) if partner is not None else (),
        _strip_parity(_subject_first(item)),
        str(item.get("subgroup") or "").strip().casefold(),
        str(item.get("teacher") or "").strip().casefold(),
        _strip_parity(_subject_first(partner)) if partner else "",
        fields,
    )


def _merge_week_variants(
    entries: list[tuple[str, dict, dict | None]],
) -> list[tuple[str, dict, dict | None]]:
    """Свести верх и низ одной недели в одну карточку.

    Варианты уезжают в ``_variants`` первой записи, поэтому пост показывает
    правку один раз, а место и формат каждой недели печатает строкой ниже.
    Записи с одинаковой подписью, но одной и той же неделей не склеиваем:
    это не верх/низ одной пары, а две отдельные строки портала.
    """
    merged: list[tuple[str, dict, dict | None]] = []
    positions: dict[tuple, list[int]] = {}
    for kind, item, partner in entries:
        signature = _variant_signature(kind, item, partner)
        parity = _parity_of(str(item.get("note") or ""))
        candidates = positions.setdefault(signature, [])
        target = next(
            (index for index in candidates if parity not in _parities_of(merged[index][1])),
            None,
        )
        if target is None:
            candidates.append(len(merged))
            if partner is not None:
                partner = {
                    **partner,
                    "_variants": [partner],
                    "_parities": [_parity_of(str(partner.get("note") or ""))],
                }
            merged.append((kind, {**item, "_variants": [item], "_parities": [parity]}, partner))
            continue
        first_kind, first_item, first_partner = merged[target]
        if first_partner is not None and partner is not None:
            first_partner = {
                **first_partner,
                "_variants": [*_variants_of(first_partner), partner],
                "_parities": [*_parities_of(first_partner), _parity_of(str(partner.get("note") or ""))],
            }
        merged[target] = (
            first_kind,
            {
                **first_item,
                "_variants": [*_variants_of(first_item), item],
                "_parities": [*_parities_of(first_item), parity],
            },
            first_partner,
        )
    return merged


def _place_line(item: dict, prefix: str = "где", *, skip_room: bool = False) -> str:
    """Never join different week variants with an ambiguous slash."""
    variants = _variants_of(item)
    places = list(dict.fromkeys(_where(variant, skip_room=skip_room) for variant in variants))
    if len(places) == 1:
        text = places[0]
    else:
        text = "\n".join(
            f"{_WEEK_SHORT[_parity_of(str(variant.get('note') or ''))].capitalize()}: "
            f"{_where(variant, skip_room=skip_room)}"
            for variant in variants
        )
    return f"{prefix}: {text}" if prefix else text


def _note_line(item: dict) -> str:
    """Строка «примечание» без повторов места, формата и недели."""
    notes: list[str] = []
    for variant in _variants_of(item):
        brief = _note_brief(variant)
        if brief and brief not in notes:
            notes.append(brief)
    if len({_note_brief(variant) for variant in _variants_of(item)}) > 1:
        return "\n".join(
            f"{_WEEK_SHORT[_parity_of(str(variant.get('note') or ''))].capitalize()}: {_note_brief(variant)}"
            for variant in _variants_of(item) if _note_brief(variant)
        )
    return " · ".join(notes)


def _group_by_day(
    added: list[dict],
    removed: list[dict],
    changed: list[dict],
) -> dict[str, list[tuple[str, dict, dict | None]]]:
    """Правки, разложенные по дням и отсортированные для чтения."""
    # Field deltas are authoritative even if a caller supplies stale context.
    # Normalize before merging so all week variants and both renderers agree.
    field_keys = {"предмет": "subject", "ауд.": "room", "преподаватель": "teacher",
                  "примечание": "note", "место": "location", "формат": "delivery_mode"}
    changed = [
        {**item, **{
            field_keys[field[0]]: field[2] for field in item.get("fields") or []
            if isinstance(field, (list, tuple)) and len(field) == 3 and field[0] in field_keys
        }} for item in changed
    ]
    by_day: dict[str, list[tuple[str, dict, dict | None]]] = {}
    days = {str(item.get("day") or "").strip() or "—"
            for item in (*added, *removed, *changed)}
    for day in days:
        day_added = [item for item in added if (str(item.get("day") or "").strip() or "—") == day]
        day_removed = [item for item in removed if (str(item.get("day") or "").strip() or "—") == day]
        day_changed = [item for item in changed if (str(item.get("day") or "").strip() or "—") == day]
        moves, rest_removed, rest_added = _pair_moves(day_removed, day_added)
        entries: list[tuple[str, dict, dict | None]] = []
        for gone, arrived in moves:
            entries.append(("moved", gone, arrived))
        for item in day_changed:
            entries.append(("changed", item, None))
        for item in rest_added:
            entries.append(("added", item, None))
        for item in rest_removed:
            entries.append(("removed", item, None))
        # Сначала по важности правки, потом по времени: читатель видит
        # переносы и изменения раньше мелких добавлений. Пара без времени
        # («Проектный день», в сетке «—») уходит в конец дня, а не роняет
        # сортировку сравнением None с временем.
        entries.sort(key=lambda triple: (
            _KIND_ORDER.get(triple[0], 9),
            bells.slot(triple[1].get("time")).start or dt.time.max,
            _subject_first(triple[1]).casefold(),
        ))
        by_day[day] = _merge_week_variants(entries)
    return by_day


def _summary_subjects(entries: list[tuple[str, dict, dict | None]]) -> list[object]:
    names: dict[str, int] = {}
    for _kind, item, partner in entries:
        name, _ = _subject_kind(_subject_first(partner or item))
        names[name] = names.get(name, 0) + 1
    runs: list[object] = []
    for name, count in names.items():
        if runs:
            runs.append(", ")
        runs.append({"type": "bold", "text": _truncate_rich_text(name, 400)})
        if count > 1:
            runs.append(f" ({count} {_pairs_word(count)})")
    return runs


def _summary_delta(kind: str, item: dict, partner: dict | None) -> tuple[tuple, list[object]]:
    """Overview values only; the lossless old/new context stays in details."""
    fields = [tuple(field) for field in item.get("fields", [])
              if isinstance(field, (list, tuple)) and len(field) == 3]
    labels = {str(field[0]) for field in fields}
    if kind == "changed" and labels == {"преподаватель"}:
        value = str(fields[0][2] or "")
        if _missing_change_value(value):
            return ("teacher_missing",), []
        # Different former teachers can share a new one. The heading identifies
        # a replacement; exact former names remain attached to each occurrence.
        return ("teacher", value), [_truncate_rich_text(value, 200)]
    if kind == "changed" and labels == {"ауд."}:
        old, new = str(fields[0][1] or ""), str(fields[0][2] or "")
        old_text = "не указана" if _missing_change_value(old) else old
        new_text = "не указана" if _missing_change_value(new) else new
        return ("room", old, new), [_truncate_rich_text(old_text, 200), " → ", _truncate_rich_text(new_text, 200)]
    if kind == "moved" and partner is not None:
        old = bells.slot(item.get("time")).label()
        new = bells.slot(partner.get("time")).label()
        return ("time", old, new), [old, " → ", new]
    # Free-form portal notes and multi-field edits cannot be shortened by
    # guessing which substring matters. List the affected subjects together;
    # their complete deltas remain in the closed details section.
    return ("subjects",), []


def _fields_of(item: dict) -> list[tuple[str, str, str]]:
    return [
        (str(field[0]), str(field[1] or ""), str(field[2] or ""))
        for field in (item.get("fields") or [])
        if isinstance(field, (list, tuple)) and len(field) == 3
    ]


def _time_runs(item: dict) -> list[object]:
    label = bells.slot(item.get("time")).label()
    if not label or label == "—":
        return []
    return [{"type": "code", "text": _truncate_rich_text(label, 48)}]


def _old_new_runs(old: str, new: str, limit: int, *, missing: str = "не указано") -> list[object]:
    """«старое → новое»: старое зачёркнуто, новое жирным; пустое старое не печатаем."""
    runs: list[object] = []
    if not _missing_change_value(old):
        runs += [*_old_value_runs(old, limit), " → "]
    runs.append({"type": "bold", "text": _truncate_rich_text(missing if _missing_change_value(new) else new, limit)})
    return runs


def _place_runs(text: str, *, struck: bool = False) -> list[object]:
    if not text or text == "место не указано":
        return ["—"]
    text = _truncate_rich_text(text, 300)
    return [{"type": "strikethrough", "text": text}] if struck else [text]


def _change_what_cell(kind: str, item: dict, partner: dict | None) -> list[object]:
    """Знак правки и время пары; у переноса — старое время зачёркнуто, новое жирным."""
    mark = {"type": "code", "text": _KIND_MARK.get(kind, "?")}
    if kind == "moved" and partner is not None:
        old = bells.slot(item.get("time")).label()
        new = bells.slot(partner.get("time")).label()
        return [mark, "\n", *_old_value_runs(old, 48), " → ", {"type": "bold", "text": _truncate_rich_text(new, 48)}]
    runs: list[object] = [mark]
    time = _time_runs(partner or item)
    if time and kind == "removed":
        time = [{"type": "strikethrough", "text": time[0]["text"]}]
    if time:
        runs += ["\n", *time]
    return runs


def _change_lesson_cell(kind: str, item: dict, partner: dict | None) -> list[object]:
    """Название (у переименования — старое зачёркнуто или подсвечен хвост),
    вид пары и неделя, преподаватель (у смены — старый зачёркнут, новый жирным)."""
    current = partner or item
    fields = _fields_of(item)
    labels = {label for label, _, _ in fields}
    name, abbrev = _subject_kind(_subject_first(current))
    runs: list[object] = []
    if "предмет" in labels:
        tail = _rename_tail(item)
        old_raw = next((old for label, old, _ in fields if label == "предмет"), "")
        old_name, _ = _subject_kind(old_raw.split("\n", 1)[0].strip())
        if tail:
            base = name[: max(0, len(name) - len(tail))].rstrip()
            runs += [{"type": "bold", "text": _truncate_rich_text(base, 400)}, " ",
                     {"type": "marked", "text": _truncate_rich_text(tail, _RENAME_TAIL_LIMIT)}]
        else:
            runs += [*_old_value_runs(old_name or "название не указано", 400), "\n",
                     {"type": "bold", "text": _truncate_rich_text(name, 400)}]
    else:
        runs.append({"type": "bold", "text": _truncate_rich_text(name, 400)})
    context: list[str] = []
    if abbrev:
        context.append(_truncate_rich_text(abbrev, 16))
    parity = _parity_context(current)
    if parity:
        context.append(parity)
    if context:
        runs += ["\n", {"type": "italic", "text": " · ".join(context)}]
    teacher_field = next((field for field in fields if field[0] == "преподаватель"), None)
    if teacher_field:
        runs += ["\n", *_old_new_runs(teacher_field[1], teacher_field[2], 160)]
    else:
        teacher = str(current.get("teacher") or "").strip()
        if teacher and not _missing_change_value(teacher):
            runs += ["\n", {"type": "italic", "text": _truncate_rich_text(teacher, 160)}]
    return runs


def _change_where_cell(kind: str, item: dict, partner: dict | None) -> list[object]:
    """Аудитория, корпус и формат: у правок старое зачёркнуто, новое жирным,
    у убранной пары всё зачёркнуто, у переноса — место только если сменилось."""
    current = partner or item
    fields = _fields_of(item)
    labels = {label for label, _, _ in fields}
    if kind == "moved" and partner is not None:
        before, after = _place_line(item, ""), _place_line(partner, "")
        if before != after:
            return [*_old_value_runs(before, 200), "\n", {"type": "bold", "text": _truncate_rich_text(after, 200)}]
        return _place_runs(after)
    if kind in {"added", "removed"}:
        return _place_runs(_place_line(current, ""), struck=kind == "removed")
    place_fields = [
        field for field in fields
        if field[0] in {"ауд.", "место", "формат"} and not (field[0] in {"место", "формат"} and "примечание" in labels)
    ]
    if not place_fields:
        return _place_runs(_place_line(current, ""))
    runs: list[object] = []
    for index, (label, old, new) in enumerate(place_fields):
        if index:
            runs.append("\n")
        if label == "формат":
            old, new = _FORMAT_MODES.get(old, old), _FORMAT_MODES.get(new, new)
        runs += _old_new_runs(old, new, 200)
        if label == "ауд.":
            corpus = _corpus_tail(item)
            if corpus:
                runs += ["\n", {"type": "italic", "text": "другой корпус: " + corpus.split("корпус: ", 1)[-1]}]
    wheres = [_where(variant) for variant in _variants_of(current)]
    if len(dict.fromkeys(wheres)) > 1:
        runs += ["\n", {"type": "italic", "text": _truncate_rich_text(_place_line(current, ""), 300)}]
    return runs


def _details_when_cell(current: dict) -> list[object]:
    """Время и неделя; без времени портала — так и пишем, дефис не выдумываем."""
    runs = _time_runs(current)
    parity = _parity_context(current)
    if parity:
        runs += (["\n"] if runs else []) + [parity]
    return runs or ["время не указано"]


def _details_note_cell(kind: str, item: dict, partner: dict | None) -> list[object]:
    current = partner or item
    note_field = next((field for field in _fields_of(item) if field[0] == "примечание"), None)
    if note_field:
        return _old_new_runs(note_field[1], note_field[2], 300)
    note = _note_line(current)
    if not note:
        return ["—"]
    text = _truncate_rich_text(note, 300)
    return [{"type": "strikethrough", "text": text}] if kind == "removed" else [text]


def _day_change_table(entries: list[tuple[str, dict, dict | None]]) -> dict:
    table = _table([[("что", "bold"), ("занятие", "bold"), ("где", "bold")]])
    for kind, item, partner in entries:
        table["cells"].append([
            _table_cell(_change_what_cell(kind, item, partner), "plain"),
            _table_cell(_change_lesson_cell(kind, item, partner), "plain"),
            _table_cell(_change_where_cell(kind, item, partner), "plain"),
        ])
    return table


def _day_details_table(entries: list[tuple[str, dict, dict | None]]) -> dict:
    table = _table([[("занятие", "bold"), ("время · недели", "bold"), ("примечание", "bold")]])
    for kind, item, partner in entries:
        current = partner or item
        name, abbrev = _subject_kind(_subject_first(current))
        lesson: list[object] = [{"type": "bold", "text": _truncate_rich_text(name, 400)}]
        if abbrev:
            lesson += ["\n", {"type": "italic", "text": _truncate_rich_text(abbrev, 16)}]
        table["cells"].append([
            _table_cell(lesson, "plain"),
            _table_cell(_details_when_cell(current), "plain"),
            _table_cell(_details_note_cell(kind, item, partner), "plain"),
        ])
    return table


def _day_change_blocks(entries: list[tuple[str, dict, dict | None]]) -> list[dict]:
    """Тело раздела дня: таблица правок.

    Основная таблица: знак и время, занятие с преподавателем, где. Старые
    значения зачёркнуты, новые жирным. Больше _RICH_DIFF_MAX_ROWS_PER_DAY строк
    не печатаем: при переопубликовании всего расписания пост остался бы читаемым.
    """
    shown = entries[:_RICH_DIFF_MAX_ROWS_PER_DAY]
    blocks: list[dict] = [_day_change_table(shown)]
    hidden = len(entries) - len(shown)
    if hidden > 0:
        blocks.append(_paragraph(f"Ещё {hidden} {_changes_word(hidden)} этого дня не поместились — смотри портал."))
    return blocks


def _day_sections(
    added: list[dict],
    removed: list[dict],
    changed: list[dict],
    screenshot_media: list[dict] | None,
    source_url: str,
) -> list[dict]:
    """Каждый день — раскрывающийся раздел: в сводке, что именно поменялось,
    внутри — объединённая сводка, закрытые подробности и сравнение со скринами."""
    by_day = _group_by_day(added, removed, changed)
    order = {name: index for index, name in enumerate(DAYS_ORDER)}
    used: set[str] = set()
    blocks: list[dict] = []
    for day in sorted(by_day, key=lambda name: order.get(name, len(order))):
        entries = by_day[day]
        content = _day_change_blocks(entries)
        kinds = {kind for kind, _, _ in entries}
        if "moved" in kinds:
            kinds |= {"added", "removed"}
        comparison = _comparison_for_day(day, screenshot_media, kinds)
        media, shared = _media_for_day(day, screenshot_media, used)
        if comparison is not None:
            content.append(comparison)
        elif media:
            caption: list[object] = [
                {"type": "bold", "text": f"Новое расписание: {day}"},
                " · оригинал на ", {"type": "url", "text": "портале", "url": source_url},
            ]
            marks = _marks_caption(media)
            if marks:
                caption.append(marks)
            content.append(_photo(str(media.get("media")), caption))
        elif shared:
            content.append(_paragraph(
                f"Скрин этого дня общий с {', '.join(shared)} — он в разделе выше."
            ))
        blocks.append(_details(_day_summary(day, entries), *content))
    return blocks


def build_changes_rich_message(
    diff: dict,
    weeks: list[dict],
    source_url: str,
    *,
    group_name: str = "6381",
    now: dt.datetime | None = None,
    screenshot_media: list[dict] | None = None,
) -> dict:
    """Action-first notification, bounded independently of optional screenshots.

    ``now`` — московское время обнаружения правки; печатается дословно сверху
    поста. Это не дата занятия: текущую календарную неделю нельзя выдавать за
    неделю, на которой произошло изменение.
    """
    added = list(diff.get("added") or [])
    removed = list(diff.get("removed") or [])
    changed = list(diff.get("changed") or [])
    transition = diff.get("transition")
    # Count presentation entries, not the two raw records of a time move.
    total = sum(
        len(entries) for entries in _group_by_day(added, removed, changed).values()
    )
    raw_total = len(added) + len(removed) + len(changed)

    limits = [
        min(len(added), _RICH_DIFF_MAX_PER_KIND),
        min(len(removed), _RICH_DIFF_MAX_PER_KIND),
        min(len(changed), _RICH_DIFF_MAX_PER_KIND),
    ]
    def assemble() -> dict:
        blocks: list[dict] = []
        if now is not None:
            blocks.append(_heading(
                _as_msk(now).strftime("Поменяли расписание в %H:%M"), 3,
            ))
        if transition == "published":
            blocks.append(_heading("Расписание опубликовано на портале", 3))
        elif transition == "vanished":
            blocks.extend([
                _heading("Расписание пропало с портала", 3),
                _paragraph("Портал показывает заглушку. Это не подтверждение отмены всех занятий."),
            ])
        shown = (added[:limits[0]], removed[:limits[1]], changed[:limits[2]])
        if added or removed or changed:
            blocks.extend(_day_sections(*shown, screenshot_media, source_url))
            # Лимиты режут записи портала, а не карточки: считаем в записях.
            omitted = raw_total - sum(len(part) for part in shown)
            if omitted > 0:
                blocks.append(_paragraph(
                    f"Показаны не все правки: ещё {omitted} не поместились в лимит сообщения."
                ))
        if not (added or removed or changed or transition):
            blocks.append(_paragraph("Содержимое страницы изменилось, но состав пар прежний."))
        return {"rich_message": {"blocks": blocks}}

    payload = assemble()
    while True:
        text_chars, block_count = _rich_payload_stats(payload)
        if text_chars <= _RICH_DIFF_TEXT_LIMIT and block_count <= _RICH_DIFF_BLOCK_LIMIT:
            break
        removable = [index for index, limit in enumerate(limits) if limit > 1]
        if not removable:
            break
        # Reduce the currently largest section first.  This keeps at least one
        # example from every non-empty kind instead of letting one kind starve.
        largest = max(removable, key=lambda index: limits[index])
        limits[largest] = max(1, limits[largest] - max(1, limits[largest] // 8))
        payload = assemble()
    return payload


def _runs_to_html(runs: object) -> str:
    """Те же рaны одним HTML-рядком для фоллбека: bold/italic/strike/marked → b/i/s/u."""
    if isinstance(runs, str):
        runs = [runs]
    out: list[str] = []
    for run in runs or []:
        if isinstance(run, str):
            out.append(_html.escape(run))
            continue
        if not isinstance(run, dict):
            continue
        text = _html.escape(str(run.get("text") or ""))
        kind = run.get("type")
        if kind == "bold":
            out.append(f"<b>{text}</b>")
        elif kind == "italic":
            out.append(f"<i>{text}</i>")
        elif kind == "strikethrough":
            out.append(f"<s>{text}</s>")
        elif kind == "marked":
            out.append(f"<u>{text}</u>")
        elif kind == "url":
            out.append(f'<a href="{_html.escape(str(run.get("url") or ""), quote=True)}">{text}</a>')
        else:
            out.append(text)
    return "".join(out)


def _table_rows_html(table: dict) -> list[str]:
    """Строки таблицы одной строкой HTML каждая: ячейки через « · », без шапки."""
    lines: list[str] = []
    for row in table.get("cells") or []:
        if any(cell.get("is_header") for cell in row):
            continue
        cells = [_runs_to_html(cell.get("text")).replace("\n", " ") for cell in row]
        lines.append(" · ".join(cell for cell in cells if cell))
    return lines


def changes_fallback_text(
    diff: dict, source_url: str, *, group_name: str = "6381", now: dt.datetime | None = None,
) -> str:
    """Build one valid plain-HTML fallback of at most 4096 characters."""
    if now is not None:
        ts = _html.escape(_as_msk(now).strftime("%H:%M"))
        header = f"<b>Поменяли расписание в {ts}</b>"
    else:
        header = "<b>Поменяли расписание</b>"
    lines = [header]
    transition = diff.get("transition")
    if transition == "published":
        lines.append("Расписание опубликовано на портале")
    elif transition == "vanished":
        lines.append("Расписание пропало с портала (заглушка)")

    day_chunks: list[str] = []
    by_day = _group_by_day(diff.get("added") or [], diff.get("removed") or [], diff.get("changed") or [])
    order = {day: index for index, day in enumerate(DAYS_ORDER)}
    for day in sorted(by_day, key=lambda name: order.get(name, len(order))):
        entries = by_day[day]
        chunk = [f"<b>{_escape_truncated(_day_summary(day, entries), 200)}</b>"]
        for block in _day_change_blocks(entries):
            if block.get("type") == "paragraph":
                chunk.append(_runs_to_html(block.get("text")))
            elif block.get("type") == "table":
                chunk.extend(_table_rows_html(block))
        day_chunks.append("\n".join(chunk))

    shown = 0
    for chunk in day_chunks:
        remaining = len(day_chunks) - shown - 1
        omission = f"… Ещё {remaining + 1} дней не поместились." if remaining >= 0 else ""
        tail = [omission] if omission else []
        candidate = "\n".join([*lines, chunk, *tail])
        if len(candidate) > 4096:
            break
        lines.append(chunk)
        shown += 1
    if shown < len(day_chunks):
        lines.append(f"… Ещё {len(day_chunks) - shown} дней не поместились.")
    text = "\n".join(lines)
    if len(text) > 4096:
        # Dynamic pieces above are individually bounded, so only a future
        # change to the fixed copy can reach this contract failure.
        raise ValueError("changes fallback exceeds Telegram's 4096-character limit")
    return text



def build_changes_html(
    diff: dict,
    weeks: list[dict],
    source_url: str,
    *,
    group_name: str = "6381",
    now: dt.datetime | None = None,
) -> str:
    """Build standard Telegram HTML for the changes post (≤4096 chars).

    Uses <blockquote expandable> for collapsible sections, <pre> for tables,
    and <i> for highlighted change details — no rich-message format needed.
    """
    if now is None:
        now = dt.datetime.now(_MSK)
    elif now.tzinfo is not None:
        now = now.astimezone(_MSK)
    week = find_current_week(weeks, now.date())
    added = list(diff.get("added") or [])
    removed = list(diff.get("removed") or [])
    changed = list(diff.get("changed") or [])
    transition = diff.get("transition")
    total = len(added) + len(removed) + len(changed)

    # --- Title block ---
    title_lines = ["<b>Поменяли расписание</b>"]
    if transition == "published":
        title_lines.append("Расписание опубликовано на портале")
    elif transition == "vanished":
        title_lines.append("Расписание пропало с портала (заглушка)")
    elif total:
        title_lines.append(f"{total} {_changes_word(total)}")
    title_lines.append(_html.escape(now.strftime("%d.%m.%Y в %H:%M")))
    if week:
        half = WEEK_HALF_NAME.get(week.get("half"), week.get("half", ""))
        title_lines.append(_html.escape(
            f"Неделя {week['week']} ({half}) · {week['start']} — {week['end']}"
        ))
    parts: list[str] = ["<blockquote>" + "\n".join(title_lines) + "</blockquote>"]

    # --- Helper: render a table as <pre> text ---
    def _diff_table_html(items: list[dict]) -> str:
        header = "день | время | предмет | ауд. | преподаватель"
        rows: list[str] = []
        for item in items:
            day_value = str(item.get("day") or "—")
            day = DAY_TO_SHORT.get(day_value, day_value)
            time_val = str(item.get("time") or "—")
            subject = str(item.get("subject") or "—").split("\n", 1)[0]
            room = str(item.get("room") or "—")
            teacher = str(item.get("teacher") or "—")
            rows.append(f"{day} | {time_val} | {subject} | {room} | {teacher}")
        body = _html.escape(header) + "\n" + "\n".join(_html.escape(r) for r in rows)
        return f"<pre>{body}</pre>"

    # --- Added section ---
    if added:
        inner = f"<b>Добавлено — {len(added)}</b>\n\n" + _diff_table_html(added)
        parts.append(f"<blockquote expandable>{inner}</blockquote>")

    # --- Removed section ---
    if removed:
        inner = f"<b>Убрано — {len(removed)}</b>\n\n" + _diff_table_html(removed)
        parts.append(f"<blockquote expandable>{inner}</blockquote>")

    # --- Changed section ---
    if changed:
        blocks: list[str] = [f"<b>Изменено — {len(changed)}</b>"]
        for item in changed:
            day_value = str(item.get("day") or "—")
            day = DAY_TO_SHORT.get(day_value, day_value)
            time_val = str(item.get("time") or "—")
            subject = str(item.get("subject") or "—").split("\n", 1)[0]
            header = f"<b>• {_html.escape(day)} {_html.escape(time_val)} — {_html.escape(subject)}</b>"
            field_lines: list[str] = []
            for field in (item.get("fields") or [])[:8]:
                if not isinstance(field, (list, tuple)) or len(field) != 3:
                    continue
                label, old_val, new_val = field
                field_lines.append(
                    f"{_html.escape(str(label))}: {_html.escape(str(old_val))} → {_html.escape(str(new_val))}"
                )
            if len(item.get("fields") or []) > 8:
                field_lines.append(f"… ещё {len(item['fields']) - 8} полей")
            detail = "\n".join(field_lines) or "изменение"
            blocks.append(header + "\n<i>" + detail + "</i>")
        parts.append("<blockquote expandable>" + "\n\n".join(blocks) + "</blockquote>")

    if not (added or removed or changed or transition):
        parts.append("Содержимое страницы изменилось, но состав пар прежний.")

    text = "\n\n".join(parts)
    if len(text) > 4096:
        raise ValueError("changes HTML exceeds Telegram's 4096-character limit")
    return text


def format_changes_only(weeks: list[dict], changes: list[dict], source_url: str) -> str:
    week = find_current_week(weeks)
    head = f"🔔 Расписание 6381 обновилось — неделя {week['week']}" if week else "🔔 Расписание 6381 обновилось"
    lines = [head, *[f"• {item.get('when', '')}" for item in changes[:20]], source_url]
    return "\n".join(lines)
