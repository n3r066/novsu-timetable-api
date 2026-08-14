"""Schedule JSON -> Telegram InputRichMessage (Bot API 10.2)."""
from __future__ import annotations

import datetime as dt
import json
import urllib.error
import urllib.request

WEEK_HALF_NAME = {"top": "верхняя", "bottom": "нижняя"}
DAYS_ORDER = [
    "Понедельник", "Вторник", "Среда", "Четверг",
    "Пятница", "Суббота", "Воскресенье",
]


def find_current_week(weeks: list[dict], today: dt.date | None = None) -> dict | None:
    today = today or dt.date.today()
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


def _footer(text: str) -> dict:
    return {"type": "footer", "text": text}


def _pullquote(text: str) -> dict:
    return {"type": "pullquote", "text": text}


def _table_cell(text: object, style: str) -> dict:
    return {"type": "tableCell", "text": [_rt(text, style)]}


def _table(rows: list[list[tuple[object, str]]]) -> dict:
    return {
        "type": "table",
        "cells": [[_table_cell(text, style) for text, style in row] for row in rows],
        "is_bordered": True,
        "is_striped": True,
    }


def _details(header: str, *blocks: dict) -> dict:
    return {"type": "details", "header": header, "open": False, "blocks": list(blocks)}


def _pairs_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "пара"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "пары"
    return "пар"


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

    blocks: list[dict] = [_pullquote(title)]
    if is_demo:
        blocks.append(_paragraph("Демо формата: данные занятий используются только для предпросмотра."))
    blocks.append(
        _details(
            "Как читать недели и пометки",
            _rich_paragraph([
                {"type": "marked", "text": {"type": "bold", "text": "ВЕРХНЯЯ"}},
                "  1, 3, 5, 7…  нечётные учебные недели\n",
                {"type": "marked", "text": {"type": "bold", "text": "НИЖНЯЯ"}},
                "  2, 4, 6, 8…  чётные учебные недели\n\n",
                {"type": "bold", "text": "Важно: "},
                "недели считаются по учебному календарю НовГУ, а не по календарным неделям года.\n",
                {"type": "code", "text": "*"},
                " у предмета означает, что условие раскрыто в разделе «Примечания»."
            ]),
            _divider(),
            _rich_paragraph([
                {"type": "marked", "text": {"type": "bold", "text": "ДОТ"}},
                " — дистанционные образовательные технологии. Занятие проходит дистанционно полностью или частично; способ подключения уточняется в указаниях преподавателя или НовГУ."
            ]),
            _rich_paragraph([
                {"type": "code", "text": "лек."}, " — лекция   ",
                {"type": "code", "text": "пр."}, " — практическое занятие\n",
                {"type": "code", "text": "лаб."}, " — лабораторная работа   ",
                {"type": "code", "text": "ауд."}, " — аудитория\n",
                {"type": "code", "text": "подгр."}, " — подгруппа"
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
        body = [[
            ("№", "bold"), ("предмет", "bold"), ("время", "bold"),
            ("ауд.", "bold"), ("преподаватель", "bold"),
        ]]
        notes: list[tuple[str, str]] = []
        for index, raw_pair in enumerate(pairs, 1):
            number, subject, pair_time, room, teacher = _normalise_pair(raw_pair, index)
            subject_lines = [line.strip() for line in subject.splitlines() if line.strip()]
            subject_name = subject_lines[0] if subject_lines else "—"
            subject_notes = subject_lines[1:]
            if subject_notes:
                subject_name += " *"
                notes.extend((subject_name.removesuffix(" *"), note) for note in subject_notes)
            body.append([
                (number, "code"), (subject_name, "accent"), (pair_time, "code"),
                (room, "code"), (teacher, "bold"),
            ])
        count = len(pairs)
        day_blocks: list[dict] = [_table(body)]
        if notes:
            note_blocks = [_note(subject, condition) for subject, condition in notes]
            day_blocks.append(
                _details(f"Примечания — {len(notes)} {_notes_word(len(notes))}", *note_blocks)
            )
        blocks.append(_heading(f"{day} — {count} {_pairs_word(count)}"))
        blocks.append(_details("Показать расписание", *day_blocks))
        blocks.append(_divider())

    if not rendered_days:
        blocks.append(_paragraph("ℹ️ На портале пока нет опубликованных занятий на эту неделю."))
    if changes_summary:
        blocks.extend([_divider(), _pullquote(f"⚠️ Что изменилось\n{changes_summary}")])

    # No boilerplate/footer: keep the post focused on the timetable itself.
    return {"rich_message": {"blocks": blocks}}


def fallback_text(schedule: dict | None, weeks: list[dict], source_url: str, today: dt.date | None = None) -> str:
    """Plain text fallback when sendRichMessage is temporarily unavailable."""
    week = find_current_week(weeks, today)
    title = f"Расписание группы 6381 — неделя {week['week']}" if week else "Расписание группы 6381"
    lines = [f"📅 {title}"]
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


def format_schedule_post(schedule, weeks, source_url, today=None, changes_summary="") -> dict:
    return build_rich_message(schedule, weeks, source_url, today, changes_summary)


def format_changes_only(weeks: list[dict], changes: list[dict], source_url: str) -> str:
    week = find_current_week(weeks)
    head = f"🔔 Расписание 6381 обновилось — неделя {week['week']}" if week else "🔔 Расписание 6381 обновилось"
    lines = [head, *[f"• {item.get('when', '')}" for item in changes[:20]], source_url]
    return "\n".join(lines)
