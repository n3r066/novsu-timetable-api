"""Schedule JSON -> Telegram InputRichMessage (Bot API 10.2)."""
from __future__ import annotations

import datetime as dt
import html as _html
import json
import re
import urllib.error
import urllib.request
import zoneinfo
from schedule_logic import day_view, find_week as find_schedule_week, week_view

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


def _pullquote(text: str) -> dict:
    return {"type": "pullquote", "text": text}


def _photo(media: str, caption: object = "") -> dict:
    block = {"type": "photo", "photo": {"type": "photo", "media": media}}
    if caption:
        block["caption"] = {"text": caption}
    return block


def _table_cell(text: object, style: str, *, is_header: bool = False) -> dict:
    """Build a RichBlockTableCell using the Bot API wire schema.

    Table cells are not blocks and therefore have no ``type`` field.  The API
    requires both alignment fields; ``is_header`` is emitted only for actual
    heading cells.
    """
    cell = {
        "text": [_rt(text, style)],
        "align": "left",
        "valign": "middle",
    }
    if is_header:
        cell["is_header"] = True
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


def _week_summary(start: str, end: str) -> list[object]:
    return [
        {"type": "marked", "text": {"type": "bold", "text": "Полное расписание"}},
        f" · вся неделя: {start} — {end}",
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


def _lesson_fields(pair: dict, index: int) -> dict:
    """Extract display fields from a parser lesson record."""
    full = pair.get("subject") or pair.get("subject_raw") or pair.get("name") or ""
    subject = full.split("\n", 1)[0] if full else "—"
    return {
        "number": str(pair.get("number") or index),
        "subject": subject,
        "time": pair.get("time") or "—",
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


def _lesson_table(lessons: list[dict]) -> dict:
    body = [[
        ("№", "bold"), ("предмет", "bold"), ("время", "bold"),
        ("место", "bold"), ("преподаватель", "bold"),
    ]]
    for index, lesson in enumerate(lessons, 1):
        subject = lesson.get("subject") or "—"
        if lesson.get("note"):
            subject += " *"
        body.append([
            (lesson.get("number") or index, "code"),
            (subject, "accent"),
            (lesson.get("time") or "—", "code"),
            (lesson.get("room") or "—", "code"),
            (lesson.get("teacher") or "—", "bold"),
        ])
    return _table(body)


def _day_blocks(day: dict, *, open_details: bool = False) -> dict:
    lessons = day.get("lessons") or []
    date = dt.date.fromisoformat(day["date"])
    title = f"{day['day']} — {date.strftime('%d.%m')}: {len(lessons)} {_pairs_word(len(lessons))}"
    blocks: list[dict] = []
    if not lessons:
        blocks.append(_paragraph("Пар нет."))
    else:
        blocks.append(_lesson_table(lessons))
        notes = [(item.get("subject") or "—", item.get("note") or "") for item in lessons if item.get("note")]
        if notes:
            blocks.append(_details(
                f"Примечания — {len(notes)} {_notes_word(len(notes))}",
                *[_note(subject, note) for subject, note in notes],
            ))
    return (_details_open if open_details else _details)(title, *blocks)


def _reading_help(source_url: str) -> dict:
    return _details(
        "Как читать недели и пометки",
        _rich_paragraph([
            {"type": "marked", "text": {"type": "bold", "text": "ВЕРХНЯЯ"}},
            "  1, 3, 5, 7…  нечётные учебные недели\n",
            {"type": "marked", "text": {"type": "bold", "text": "НИЖНЯЯ"}},
            "  2, 4, 6, 8…  чётные учебные недели\n",
            "Недели считаются от 01.09 по учебному календарю НовГУ.",
        ]),
        _divider(),
        _rich_paragraph([
            {"type": "marked", "text": {"type": "bold", "text": "ДОТ"}},
            " — дистанционные образовательные технологии. ",
            {"type": "code", "text": "*"},
            " у предмета — смотри примечания под таблицей.",
        ]),
        _rich_paragraph([
            {"type": "code", "text": "лек."}, " — лекция   ",
            {"type": "code", "text": "пр."}, " — практическое занятие\n",
            {"type": "code", "text": "лаб."}, " — лабораторная работа   ",
            {"type": "code", "text": "ауд."}, " — аудитория",
        ]),
        _divider(),
        _rich_paragraph([
            {"type": "bold", "text": "Источник: "},
            {"type": "url", "text": "портал НовГУ", "url": source_url},
        ]),
    )


def build_dashboard_rich_message(
    schedule: dict | None,
    weeks: list[dict],
    source_url: str,
    target_date: dt.date,
    *,
    group_name: str = "6381",
    screenshot_media: str | list[str] | list[dict] | None = None,
    current_date: dt.date | None = None,
    last_updated: str = "",
) -> dict:
    """Build pinned dashboard: diary + current week, date-aware."""
    target_week = find_schedule_week(weeks, target_date)
    if target_week is None and weeks:
        target_week = weeks[0]
    if target_week:
        half = WEEK_HALF_NAME.get(target_week.get("half"), target_week.get("half", ""))
        title = (
            f"Расписание группы {group_name}\n\n"
            f"Фокус: {target_date.strftime('%d.%m.%Y')}\n"
            f"Неделя {target_week['week']} ({half})\n"
            f"{target_week['start']} — {target_week['end']}"
        )
        if last_updated:
            title += f"\n\nПоследние изменения: {last_updated}"
    else:
        title = f"Расписание группы {group_name}\n\nФокус: {target_date.strftime('%d.%m.%Y')}"
        if last_updated:
            title += f"\n\nПоследние изменения: {last_updated}"

    today = day_view(schedule, weeks, target_date, group=group_name, source_url=source_url)
    tomorrow = day_view(schedule, weeks, target_date + dt.timedelta(days=1), group=group_name, source_url=source_url)
    current_date = current_date or dt.date.today()

    blocks: list[dict] = [_pullquote(title)]

    if target_week:
        week = week_view(schedule, weeks, target_week, group=group_name, source_url=source_url)
        week_blocks = [_day_blocks(day, open_details=False) for day in week["days"]]
        if screenshot_media:
            current_day = None if current_date.weekday() == 6 else DAYS_ORDER[current_date.weekday()]
            if isinstance(screenshot_media, str):
                media_items = [{"label": "с сайта", "media": screenshot_media}]
            else:
                media_items = [item if isinstance(item, dict) else {"label": "с сайта", "media": item} for item in screenshot_media]
            screenshot_blocks = []
            for item in media_items:
                label = str(item.get("label") or "с сайта")
                day_label = DAY_SHORT_TO_FULL.get(label, label)
                caption = [
                    {"type": "bold", "text": day_label},
                    " · оригинальное ",
                    {"type": "url", "text": "расписание", "url": source_url},
                ]
                details = _details_open if day_label == current_day else _details
                screenshot_blocks.append(details(f"Скрин: {day_label}", _photo(item["media"], caption)))
            week_blocks.append(_details("Скрины · оригинал по дням", *screenshot_blocks))
        if not week_blocks:
            week_blocks = [_paragraph("На эту учебную неделю пары не найдены.")]
        blocks.append(_details(_week_summary(target_week["start"], target_week["end"]), *week_blocks))
        blocks.append(_divider())

    blocks.append(
        _details(
            f"Дневник: {target_date.strftime('%d.%m')} и {(target_date + dt.timedelta(days=1)).strftime('%d.%m')}",
            _day_blocks(today),
            _day_blocks(tomorrow),
        )
    )
    blocks.append(_divider())
    blocks.append(_reading_help(source_url))
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

    blocks: list[dict] = [_pullquote(title)]
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
                    f"<td class='t'>{_html.escape(fields['time'])}</td>"
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
        parts.append("<div class='sub'>ℹ️ На портале пока нет опубликованных занятий.</div>")
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
        pair_time = pair.get("time") or "—"
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
    header = "⚠️ Что изменилось\n"
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
        messages.append("ℹ️ На портале пока нет опубликованных занятий на эту неделю.")
    if changes_summary:
        messages.extend(_changes_summary_chunks(changes_summary))
    if not all(len(message) <= 4096 for message in messages):
        raise ValueError("internal error: oversized Telegram fallback message")
    return messages


DAY_TO_SHORT = {full: short for short, full in DAY_SHORT_TO_FULL.items()}


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
        (_truncate_rich_text(item.get("time"), 48), "code"),
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


def _diff_table_section(label: str, selected: list[dict], total: int) -> dict:
    content: list[dict] = []
    chunks = _chunked(selected, _RICH_DIFF_TABLE_CHUNK)
    for index, chunk in enumerate(chunks):
        if len(chunks) > 1:
            first = index * _RICH_DIFF_TABLE_CHUNK + 1
            last = first + len(chunk) - 1
            content.append(_paragraph(f"Записи {first}–{last}"))
        content.append(_table([_DIFF_TABLE_HEAD] + [_diff_lesson_row(item) for item in chunk]))
    omitted = total - len(selected)
    if omitted:
        content.append(_paragraph(f"Показаны первые {len(selected)}. Ещё {omitted} не поместились в лимит сообщения."))
    return _details_open(f"{label} — {total}", *content)


def _changed_section(selected: list[dict], total: int) -> dict:
    content: list[dict] = []
    chunks = _chunked(selected, _RICH_DIFF_TABLE_CHUNK)
    for chunk_index, chunk in enumerate(chunks):
        if len(chunks) > 1:
            first = chunk_index * _RICH_DIFF_TABLE_CHUNK + 1
            last = first + len(chunk) - 1
            content.append(_paragraph(f"Записи {first}–{last}"))
        for item in chunk:
            day_value = str(item.get("day") or "—")
            day = DAY_TO_SHORT.get(day_value, day_value)
            subject = str(item.get("subject") or "—").split("\n", 1)[0]
            header = (
                f"{_truncate_rich_text(day, 24)} "
                f"{_truncate_rich_text(item.get('time'), 48)} — "
                f"{_truncate_rich_text(subject, 240)}"
            )
            field_lines = []
            for field in (item.get("fields") or [])[:8]:
                if not isinstance(field, (list, tuple)) or len(field) != 3:
                    continue
                label, old, new = field
                field_lines.append(
                    f"{_truncate_rich_text(label, 60)}: "
                    f"{_truncate_rich_text(old, 180)} → {_truncate_rich_text(new, 180)}"
                )
            if len(item.get("fields") or []) > 8:
                field_lines.append(f"… ещё {len(item['fields']) - 8} полей")
            content.append(_note(header, "\n".join(field_lines) or "изменение"))
    omitted = total - len(selected)
    if omitted:
        content.append(_paragraph(f"Показаны первые {len(selected)}. Ещё {omitted} не поместились в лимит сообщения."))
    return _details_open(f"✏️ Изменено — {total}", *content)


def build_changes_rich_message(
    diff: dict,
    weeks: list[dict],
    source_url: str,
    *,
    group_name: str = "6381",
    now: dt.datetime | None = None,
) -> dict:
    """Build a bounded rich diff that stays within Bot API rich limits.

    Large sections are split into tables of 25 rows.  If the whole diff still
    exceeds the 32768-character or 500-block API limits, a representative
    prefix from every non-empty section is kept and the omitted count is shown.
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

    title_lines = [f"Обновление расписания {_truncate_rich_text(group_name, 80)}"]
    if transition == "published":
        title_lines.append("📅 Расписание опубликовано на портале")
    elif transition == "vanished":
        title_lines.append("⚠️ Расписание пропало с портала (заглушка)")
    elif total:
        title_lines.append(f"{total} {_changes_word(total)}")
    title_lines.append(now.strftime("%d.%m.%Y %H:%M MSK"))
    if week:
        half = WEEK_HALF_NAME.get(week.get("half"), week.get("half", ""))
        title_lines.append(
            f"Неделя {_truncate_rich_text(week['week'], 20)} "
            f"({_truncate_rich_text(half, 40)}) · "
            f"{_truncate_rich_text(week['start'], 40)} — {_truncate_rich_text(week['end'], 40)}"
        )

    limits = [
        min(len(added), _RICH_DIFF_MAX_PER_KIND),
        min(len(removed), _RICH_DIFF_MAX_PER_KIND),
        min(len(changed), _RICH_DIFF_MAX_PER_KIND),
    ]

    def assemble() -> dict:
        blocks: list[dict] = [_pullquote("\n".join(title_lines))]
        if added:
            blocks.append(_diff_table_section("➕ Добавлено", added[:limits[0]], len(added)))
        if removed:
            blocks.append(_diff_table_section("➖ Убрано", removed[:limits[1]], len(removed)))
        if changed:
            blocks.append(_changed_section(changed[:limits[2]], len(changed)))
        if not (added or removed or changed or transition):
            blocks.append(_paragraph("Содержимое страницы изменилось, но состав пар прежний."))
        blocks.append(_divider())
        blocks.append(_rich_paragraph([
            {"type": "bold", "text": "Источник: "},
            {"type": "url", "text": "портал НовГУ", "url": source_url},
        ]))
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


def changes_fallback_text(diff: dict, source_url: str, *, group_name: str = "6381") -> str:
    """Build one valid plain-HTML fallback of at most 4096 characters."""
    header = f"🔔 <b>Расписание {_escape_truncated(group_name, 160)} обновилось</b>"
    lines = [header]
    transition = diff.get("transition")
    if transition == "published":
        lines.append("📅 Расписание опубликовано на портале")
    elif transition == "vanished":
        lines.append("⚠️ Расписание пропало с портала (заглушка)")

    item_lines: list[str] = []
    for prefix, key in (("➕", "added"), ("➖", "removed")):
        for item in diff.get(key) or []:
            day_value = str(item.get("day") or "—")
            day = DAY_TO_SHORT.get(day_value, day_value)
            subject = str(item.get("subject") or "—").split("\n", 1)[0]
            item_lines.append(
                f"{prefix} {_escape_truncated(day, 60)} "
                f"{_escape_truncated(item.get('time'), 100)} — "
                f"{_escape_truncated(subject, 1500)}, ауд. "
                f"{_escape_truncated(item.get('room'), 300)}"
            )
    for item in diff.get("changed") or []:
        day_value = str(item.get("day") or "—")
        day = DAY_TO_SHORT.get(day_value, day_value)
        subject = str(item.get("subject") or "—").split("\n", 1)[0]
        field_parts = []
        fields = item.get("fields") or []
        for field in fields[:4]:
            if not isinstance(field, (list, tuple)) or len(field) != 3:
                continue
            label, old, new = field
            field_parts.append(
                f"{_escape_truncated(label, 80)}: {_escape_truncated(old, 220)} → "
                f"{_escape_truncated(new, 220)}"
            )
        if len(fields) > 4:
            field_parts.append(f"… ещё {len(fields) - 4} полей")
        details = "; ".join(field_parts) or "изменение"
        item_lines.append(
            f"✏️ {_escape_truncated(day, 60)} {_escape_truncated(item.get('time'), 100)} — "
            f"{_escape_truncated(subject, 1200)} ({details})"
        )

    source_line = f'<a href="{_escape_truncated(source_url, 700)}">открыть на портале</a>'
    shown = 0
    for line in item_lines:
        remaining = len(item_lines) - shown - 1
        omission = f"… Ещё {remaining + 1} записей не поместились." if remaining >= 0 else ""
        tail = [omission, source_line] if omission else [source_line]
        candidate = "\n".join([*lines, line, *tail])
        if len(candidate) > 4096:
            break
        lines.append(line)
        shown += 1
    if shown < len(item_lines):
        lines.append(f"… Ещё {len(item_lines) - shown} записей не поместились.")
    lines.append(source_line)
    text = "\n".join(lines)
    if len(text) > 4096:
        # Dynamic pieces above are individually bounded, so only a future
        # change to the fixed copy can reach this contract failure.
        raise ValueError("changes fallback exceeds Telegram's 4096-character limit")
    return text


def format_changes_only(weeks: list[dict], changes: list[dict], source_url: str) -> str:
    week = find_current_week(weeks)
    head = f"🔔 Расписание 6381 обновилось — неделя {week['week']}" if week else "🔔 Расписание 6381 обновилось"
    lines = [head, *[f"• {item.get('when', '')}" for item in changes[:20]], source_url]
    return "\n".join(lines)
