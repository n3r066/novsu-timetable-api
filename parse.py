"""Parse NovSU timetable HTML into a stable structured representation."""
from __future__ import annotations

import json
import re
from pathlib import Path
from bs4 import BeautifulSoup

DAY_FULL = {
    "Пн": "Понедельник", "Вт": "Вторник", "Ср": "Среда",
    "Чт": "Четверг", "Пт": "Пятница", "Сб": "Суббота", "Вс": "Воскресенье",
}
_TIME_RE = re.compile(r"(?:^|\s)\d{1,2}:\d{2}(?:\s|$)")


def _text(cell) -> str:
    return " ".join(cell.stripped_strings).strip()


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
            weeks.append({"week": int(cells[number_index]), "half": half, "start": date_match.group(1), "end": date_match.group(2)})
    return weeks


def _find_schedule_table(soup: BeautifulSoup):
    required = {"дата", "время", "предмет", "преподаватель", "ауд."}
    for table in soup.find_all("table"):
        # Ignore tables nested inside layout cells; they are not timetable candidates.
        if table.find_parent("table") is not None:
            continue
        first = table.find("tr", recursive=False)
        if not first:
            continue
        headers = {_text(c).casefold() for c in first.find_all(["td", "th"], recursive=False)}
        if required.issubset(headers):
            return table
    return None


def _span(value: object) -> int:
    try:
        return min(max(int(str(value)), 1), 100)
    except (TypeError, ValueError):
        return 1


def _expand_table(table) -> list[list[str]]:
    """Expand HTML rowspan/colspan into a rectangular logical grid."""
    active: dict[int, tuple[str, int]] = {}
    logical_rows: list[dict[int, str]] = []
    max_width = 0
    for tr in table.find_all("tr", recursive=False):
        row: dict[int, str] = {}
        next_active: dict[int, tuple[str, int]] = {}
        for col, (value, rows_left) in active.items():
            row[col] = value
            if rows_left > 1:
                next_active[col] = (value, rows_left - 1)
        col = 0
        for cell in tr.find_all(["td", "th"], recursive=False):
            while col in row:
                col += 1
            value = _text(cell)
            rowspan, colspan = _span(cell.get("rowspan")), _span(cell.get("colspan"))
            for offset in range(colspan):
                target = col + offset
                while target in row:
                    target += 1
                row[target] = value
                if rowspan > 1:
                    next_active[target] = (value, rowspan - 1)
            col = max(row) + 1 if row else col + colspan
        active = next_active
        max_width = max(max_width, max(row, default=-1) + 1)
        logical_rows.append(row)
    return [[row.get(col, "") for col in range(max_width)] for row in logical_rows]


def _clean_subject(value: str) -> str:
    # Portal occasionally inserts a stray dot between the lesson type and name.
    return re.sub(r"^(\([^)]*\)\s*)\.\s*", r"\1", value).strip()


def _structured_comment(comment: str) -> dict:
    """Extract only high-confidence metadata; preserve the raw comment."""
    urls = re.findall(r"https?://\S+", comment)
    delivery_mode = "remote_or_hybrid" if "ДОТ" in comment.upper() else "in_person"
    location = None
    address = re.search(r"(?:ул\.[^,]+(?:,[^,]+)*?\d+[А-Яа-яA-Za-z/-]*)", comment, re.I)
    if address:
        location = address.group(0).strip(" ,")
    elif re.search(r"\bИГУМ,\s*Антоново\b", comment, re.I):
        location = "ИГУМ, Антоново"
    return {"location": location, "delivery_mode": delivery_mode, "link": urls[0] if urls else None}


def parse_schedule(html: str) -> dict | None:
    """Parse the timetable, tolerating NovSU's stale/invalid day rowspans.

    Day separator rows are authoritative. Lesson time rowspans are expanded
    independently; this avoids column shifts when the portal leaves a day
    rowspan larger than the number of currently rendered lesson rows.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = _find_schedule_table(soup)
    if table is None:
        return None
    physical = table.find_all("tr", recursive=False)
    if not physical:
        return None
    header_cells = [_text(c).casefold().strip() for c in physical[0].find_all(["td", "th"], recursive=False)]
    required_order = ["время", "под гр.", "предмет", "преподаватель", "ауд.", "комм."]
    if not all(name in header_cells for name in required_order):
        return None

    days: dict[str, list[dict]] = {}
    raw: list[list[str]] = [header_cells]
    current_day: str | None = None
    inherited_time: str | None = None
    inherited_left = 0

    for source_row, tr in enumerate(physical[1:], 1):
        tags = tr.find_all(["td", "th"], recursive=False)
        cells = [_text(cell) for cell in tags]
        if len(cells) == 1 and cells[0] in DAY_FULL:
            current_day = DAY_FULL[cells[0]]
            days.setdefault(current_day, [])
            inherited_time = None
            inherited_left = 0
            continue
        if current_day is None:
            continue
        if len(cells) == 6:
            time_text, subgroup, subject, teacher, room, comment = cells
            inherited_time = time_text or None
            inherited_left = max(0, _span(tags[0].get("rowspan")) - 1)
        elif len(cells) == 5 and inherited_left > 0:
            time_text = inherited_time or ""
            subgroup, subject, teacher, room, comment = cells
            inherited_left -= 1
            if inherited_left == 0:
                inherited_time = None
        else:
            # Never guess shifted columns from an unknown row shape.
            continue
        if not subject:
            continue
        time_text = " ".join(re.findall(r"\d{1,2}:\d{2}", time_text)) or "—"
        note_parts = ([f"подгруппа: {subgroup}"] if subgroup else []) + ([comment] if comment else [])
        subject_raw = subject
        subject = _clean_subject(subject_raw)
        full_subject = subject + (("\n" + " · ".join(note_parts)) if note_parts else "")
        raw_room = room
        room = raw_room.strip().strip(".") or "—"
        metadata = _structured_comment(comment)
        record = {
            "number": len(days[current_day]) + 1, "subject": full_subject,
            "subject_raw": subject_raw, "time": time_text,
            "room": room, "raw_room": raw_room, "teacher": teacher or "—",
            "note": " · ".join(note_parts), "raw_comment": comment,
            **metadata, "source_row": source_row,
        }
        days[current_day].append(record)
        raw.append([current_day, time_text, subgroup, subject, teacher, room, comment])
    return {"days": days, "raw": raw}

def is_stub_page(html: str) -> bool:
    return _find_schedule_table(BeautifulSoup(html, "html.parser")) is None


def parse_all(html: str) -> dict:
    schedule = parse_schedule(html)
    return {"stub": schedule is None, "weeks": parse_calendar(html), "schedule": schedule}


if __name__ == "__main__":
    import sys
    source = Path(sys.argv[1]).read_text(encoding="utf-8")
    print(json.dumps(parse_all(source), ensure_ascii=False, indent=2))
