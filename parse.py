"""Parse NovSU timetable HTML into a stable structured representation."""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from bs4 import BeautifulSoup

import bells
from portal_parser import (
    count_schedule_grid_lessons,
    day_token as canonical_day,
    expand_table,
    find_schedule_table,
    find_schedule_table_grid,
    schedule_header,
    text,
)

DAY_FULL = {
    "Пн": "Понедельник", "Вт": "Вторник", "Ср": "Среда",
    "Чт": "Четверг", "Пт": "Пятница", "Сб": "Суббота", "Вс": "Воскресенье",
}

def _text(cell) -> str:
    return text(cell)


@dataclass
class ParseContext:
    soup: BeautifulSoup
    table: object | None
    grid: list[list[str]] | None


def parse_context(html: str) -> ParseContext:
    soup = BeautifulSoup(html, "html.parser")
    table, grid = find_schedule_table_grid(soup)
    return ParseContext(soup=soup, table=table, grid=grid)


def parse_calendar(html: str, *, context: ParseContext | None = None) -> list[dict]:
    soup = context.soup if context is not None else BeautifulSoup(html, "html.parser")
    weeks: list[dict] = []
    for tr in soup.find_all("tr"):
        cells = [_text(cell) for cell in tr.find_all(["td", "th"], recursive=False)]
        if len(cells) != 4 or not cells[0].isdigit() or not cells[2].isdigit():
            continue
        dates = [re.fullmatch(r"(\d{2}\.\d{2}\.\d{4})\s*[-–—]\s*(\d{2}\.\d{2}\.\d{4})", cells[i]) for i in (1, 3)]
        if not all(dates):
            continue
        for number_index, date_match, half in ((0, dates[0], "top"), (2, dates[1], "bottom")):
            try:
                start = dt.datetime.strptime(date_match.group(1), "%d.%m.%Y").date()
                end = dt.datetime.strptime(date_match.group(2), "%d.%m.%Y").date()
            except ValueError:
                continue
            if start > end:
                continue
            weeks.append({"week": int(cells[number_index]), "half": half, "start": start.strftime("%d.%m.%Y"), "end": end.strftime("%d.%m.%Y")})
    unique = {(item["week"], item["half"], item["start"], item["end"]): item for item in weeks}
    return sorted(unique.values(), key=lambda item: dt.datetime.strptime(item["start"], "%d.%m.%Y"))


def _find_schedule_table(soup: BeautifulSoup):
    return find_schedule_table(soup)


def _clean_subject(value: str) -> str:
    # Portal occasionally inserts a stray dot between the lesson type and name.
    return re.sub(r"^(\([^)]*\)\s*)\.\s*", r"\1", value).strip()


def _structured_comment(comment: str) -> dict:
    """Extract conservative metadata while preserving the raw annotation."""
    urls = [value.rstrip(".,;:!?)\"]}") for value in re.findall(r"https?://\S+", comment)]
    delivery_mode = "remote_or_hybrid" if "ДОТ" in comment.upper() else "in_person"
    location = None
    # Портал пишет место по-разному: «ИГУМ, Антоново» (145 вхождений в живых данных)
    # и «ИГУМ(Антоново)» (ещё 76). Нормализуем обе формы в одну строку.
    if re.search(r"\bИГУМ\s*[,([\s]*Антоново", comment, re.I):
        location = "ИГУМ, Антоново"
    elif re.search(r"\bАнтоново\b", comment, re.I):
        location = "Антоново"
    else:
        # Портал вставляет номер дома по-разному: «ул. Псковская, д.3»,
        # «ул. Псковская д.3» (без запятой), «ул. Псковская .д.3» (опечатка
        # с точкой), реже совсем без «ул.» перед названием — «Б.С.-
        # Петербургская, 41». Первая ветка ловит любую форму с «ул.» и
        # «д.»/без него, вторая — старый формат «Название, число» как
        # запасной вариант для адресов без явного «ул.».
        address = re.search(
            r"(?:\bул\.?\s*[А-ЯЁ][А-Яа-яЁё.\- ]*?(?:,\s*\.?\s*д\.?\s*|\s+\.?\s*д\.?\s*|,\s*)\d+[А-Яа-яA-Za-z/-]*"
            r"|[А-ЯЁA-Z][А-Яа-яЁёA-Za-z.\- ]*?,\s*\d+[А-Яа-яA-Za-z/-]*)",
            comment,
        )
        if address:
            location = address.group(0).strip(" ,")
    return {"location": location, "delivery_mode": delivery_mode, "link": urls[0] if urls else None}


def parse_schedule(html: str, *, context: ParseContext | None = None) -> dict | None:
    """Parse the timetable through a complete logical rowspan/colspan grid.

    The live NovSU source contains invalid nested ``tr`` elements for the first
    lesson of every day.  ``portal_parser.expand_table`` deliberately walks all
    rows owned by the table, including those nested rows and normal tbody rows.
    """
    if context is None:
        context = parse_context(html)
    table = context.table
    if table is None:
        return None
    grid = context.grid if context.grid is not None else expand_table(table)
    header = schedule_header(grid)
    if header is None:
        return None
    header_index, columns = header

    def value(row: list[str], name: str) -> str:
        column = columns.get(name)
        return row[column].strip() if column is not None and column < len(row) else ""

    days: dict[str, list[dict]] = {}
    raw: list[list[str]] = [grid[header_index]]
    current_day: str | None = None

    header_width = max(columns.values()) + 1
    for source_row, row in enumerate(grid[header_index + 1:], header_index + 1):
        if any(cell.strip() for cell in row[header_width:]):
            raise ValueError(f"unrecognized schedule row shape at source row {source_row}")
        date_value = value(row, "дата")
        day_abbr = canonical_day(date_value) or (date_value if date_value in DAY_FULL else None)
        if day_abbr is not None:
            current_day = DAY_FULL[day_abbr]
            days.setdefault(current_day, [])
            non_date = [
                cell.strip()
                for column, cell in enumerate(row[:header_width])
                if column != columns["дата"]
            ]
            if all(not cell or cell == date_value or canonical_day(cell) == day_abbr for cell in non_date):
                continue
        if current_day is None:
            continue

        subject_raw = value(row, "предмет")
        if not subject_raw:
            continue
        subgroup = value(row, "под гр.")
        teacher = value(row, "преподаватель")
        raw_room = value(row, "ауд.")
        comment = value(row, "комм.")
        raw_time = value(row, "время")
        # Единственный разбор времени в проекте живёт в bells.py: там же
        # строгий regex (мусор вроде «119:00» не чинится молча) и порядок.
        time_text = " ".join(bells.hour_tokens(raw_time)) or "—"

        note_parts = ([f"подгруппа: {subgroup}"] if subgroup else []) + ([comment] if comment else [])
        subject = _clean_subject(subject_raw)
        full_subject = subject + (("\n" + " · ".join(note_parts)) if note_parts else "")
        room = raw_room.strip().strip(".") or "—"
        metadata = _structured_comment(comment)
        record = {
            "number": len(days[current_day]) + 1,
            "subject": full_subject,
            "subject_raw": subject_raw,
            "time": time_text,
            "raw_time": raw_time,
            "room": room,
            "raw_room": raw_room,
            "teacher": teacher or "—",
            "subgroup": subgroup,
            "note": " · ".join(note_parts),
            "raw_comment": comment,
            **metadata,
            "source_row": source_row,
        }
        days[current_day].append(record)
        raw.append([current_day, time_text, subgroup, subject, teacher, room, comment])
    return {"days": days, "raw": raw}




def extract_teacher_ids(html: str, *, context: ParseContext | None = None) -> dict[str, str]:
    """Extract teacher_text → teacherId mapping from schedule HTML links.

    Each teacher cell in the portal contains an <a> link with teacherId=ora_XXXXX.
    Returns a mapping of the link text (teacher name as shown) to the teacherId.
    """
    if context is None:
        context = parse_context(html)
    table = context.table
    if table is None:
        return {}
    mapping: dict[str, str] = {}
    for a in table.find_all("a", href=True):
        m = re.search(r"teacherId=((?:ora|ext)_\d+)", a["href"])
        if m:
            text = a.get_text(strip=True)
            if text:
                mapping[text] = m.group(1)
    return mapping

def is_stub_page(html: str) -> bool:
    return _find_schedule_table(BeautifulSoup(html, "html.parser")) is None


def parse_all(html: str) -> dict:
    context = parse_context(html)
    schedule = parse_schedule(html, context=context)
    physical_count = 0 if schedule is None else count_schedule_grid_lessons(context.grid or [])
    return {
        "stub": schedule is None,
        "weeks": parse_calendar(html, context=context),
        "schedule": schedule,
        "physical_lesson_count": physical_count,
        "teacher_ids": extract_teacher_ids(html, context=context),
    }


if __name__ == "__main__":
    import sys
    source = Path(sys.argv[1]).read_text(encoding="utf-8")
    print(json.dumps(parse_all(source), ensure_ascii=False, indent=2))
