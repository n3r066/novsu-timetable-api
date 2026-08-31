"""Parse NovSU timetable HTML into a stable structured representation."""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from bs4 import BeautifulSoup
from portal_parser import expand_table, find_schedule_table, schedule_header, text

DAY_FULL = {
    "Пн": "Понедельник", "Вт": "Вторник", "Ср": "Среда",
    "Чт": "Четверг", "Пт": "Пятница", "Сб": "Суббота", "Вс": "Воскресенье",
}

def _text(cell) -> str:
    return text(cell)


def parse_calendar(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    weeks: list[dict] = []
    for tr in soup.find_all("tr"):
        cells = [_text(cell) for cell in tr.find_all(["td", "th"], recursive=False)]
        if len(cells) != 4 or not cells[0].isdigit() or not cells[2].isdigit():
            continue
        dates = [re.fullmatch(r"(\d{2}\.\d{2}\.\d{4})\s*-\s*(\d{2}\.\d{2}\.\d{4})", cells[i]) for i in (1, 3)]
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
    if re.search(r"\bИГУМ,\s*Антоново\b", comment, re.I):
        location = "ИГУМ, Антоново"
    elif re.search(r"\bАнтоново\b", comment, re.I):
        location = "Антоново"
    else:
        address = re.search(r"(?:ул\.\s*)?[А-ЯЁA-Z][А-Яа-яЁёA-Za-z.\- ]*,\s*\d+[А-Яа-яA-Za-z/-]*", comment)
        if address:
            location = address.group(0).strip(" ,")
    return {"location": location, "delivery_mode": delivery_mode, "link": urls[0] if urls else None}


def parse_schedule(html: str) -> dict | None:
    """Parse the timetable through a complete logical rowspan/colspan grid.

    The live NovSU source contains invalid nested ``tr`` elements for the first
    lesson of every day.  ``portal_parser.expand_table`` deliberately walks all
    rows owned by the table, including those nested rows and normal tbody rows.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = _find_schedule_table(soup)
    if table is None:
        return None
    grid = expand_table(table)
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
        day_token = value(row, "дата")
        if day_token in DAY_FULL:
            current_day = DAY_FULL[day_token]
            days.setdefault(current_day, [])
            non_date = [
                cell.strip()
                for column, cell in enumerate(row[:header_width])
                if column != columns["дата"]
            ]
            if all(not cell or cell == day_token for cell in non_date):
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
        valid_times = []
        for token in re.findall(r"\d{1,2}:\d{2}", raw_time):
            hour, minute = (int(part) for part in token.split(":"))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                valid_times.append(token)
        time_text = " ".join(valid_times) or "—"

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




def extract_teacher_ids(html: str) -> dict[str, str]:
    """Extract teacher_text → teacherId mapping from schedule HTML links.

    Each teacher cell in the portal contains an <a> link with teacherId=ora_XXXXX.
    Returns a mapping of the link text (teacher name as shown) to the teacherId.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = find_schedule_table(soup)
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
    schedule = parse_schedule(html)
    return {"stub": schedule is None, "weeks": parse_calendar(html), "schedule": schedule}


if __name__ == "__main__":
    import sys
    source = Path(sys.argv[1]).read_text(encoding="utf-8")
    print(json.dumps(parse_all(source), ensure_ascii=False, indent=2))
