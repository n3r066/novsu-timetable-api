"""HTML parsing helpers for the public NovSU timetable portal."""
from __future__ import annotations

import html as htmlmod
import copy
import re
import urllib.parse
from typing import Any

from bs4 import BeautifulSoup


DAY_ABBRS = {"Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"}
_DAY_CANONICAL = {"пн": "Пн", "вт": "Вт", "ср": "Ср", "чт": "Чт", "пт": "Пт", "сб": "Сб", "вс": "Вс"}


def day_token(value: object) -> str | None:
    """Каноническая аббревиатура дня из вариантов разделителя ('пн', 'Пн.', 'ПН ')."""
    token = re.sub(r"\s+", "", str(value or "")).casefold().strip(".")
    return _DAY_CANONICAL.get(token)
_DAY_FULL_TO_SHORT = {
    "понедельник": "Пн", "вторник": "Вт", "среда": "Ср", "четверг": "Чт",
    "пятница": "Пт", "суббота": "Сб", "воскресенье": "Вс",
}


def norm_text(value: object) -> str:
    """Один пробел, без NBSP, без регистра — для сравнения текста ячеек."""
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip().casefold()


def day_short(value: object) -> str | None:
    """Короткий день из аббревиатуры («Ср») или полного имени («Среда»)."""
    token = day_token(value)
    if token:
        return token
    return _DAY_FULL_TO_SHORT.get(norm_text(value).replace("ё", "е"))


SCHEDULE_TABLE_HEADERS = {"дата", "время", "предмет", "преподаватель", "ауд."}
SCHEDULE_OPTIONAL_HEADERS = {"под гр.", "комм."}
CONTENT_ID = "npe_instance_1103357_npe_content"

_HEADER_ALIASES = {
    "дата": "дата",
    "день": "дата",
    "время": "время",
    "подгр": "под гр.",
    "под гр": "под гр.",
    "под группа": "под гр.",
    "подгруппа": "под гр.",
    "предмет": "предмет",
    "преподаватель": "преподаватель",
    "препод": "преподаватель",
    "ауд": "ауд.",
    "аудитория": "ауд.",
    "комм": "комм.",
    "комментарий": "комм.",
    "примечание": "комм.",
    "примечания": "комм.",
    "комментарии": "комм.",
    "преподаватели": "преподаватель",
    "предметы": "предмет",
    "аудитории": "ауд.",
    "подгруппы": "под гр.",
    "день недели": "дата",
    "время занятий": "время",
}


def text(node: Any) -> str:
    return " ".join(node.stripped_strings).strip()


def normalize_header(value: object) -> str:
    """Normalize visual header variants (NBSP, extra spaces and punctuation)."""
    value = re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip().casefold()
    compact = value.replace(".", "").strip()
    return _HEADER_ALIASES.get(compact, value)


def iter_table_rows(table):
    """Yield every row owned by *table*, including tbody and invalid nested tr.

    The live portal emits ``<tr><td>Пн</td><tr>first lesson</tr></tr>``.
    BeautifulSoup keeps the lesson row nested when using ``html.parser``.  A
    nearest-table check includes that row while excluding rows of child tables.
    """
    for row in table.find_all("tr"):
        if row.find_parent("table") is table:
            yield row


def _span(value: object) -> int:
    try:
        return min(max(int(str(value)), 1), 100)
    except (TypeError, ValueError):
        return 1


def expand_table(table, *, cell_rows: list | None = None) -> list[list[str]]:
    """Expand all rowspans/colspans into a logical grid.

    A day separator is authoritative and clears a stale day rowspan left by the
    portal.  This preserves the old stale-rowspan tolerance while also handling
    the portal's nested first lesson, normal tbody markup and rowspans in any
    column.
    """
    active: dict[int, tuple[str, Any, int]] = {}
    logical_rows: list[dict[int, str]] = []
    owners: list[dict[int, Any]] = []
    max_width = 0
    header_width: int | None = None
    date_column: int | None = None
    time_column: int | None = None
    current_day: str | None = None
    for tr in iter_table_rows(table):
        cells = tr.find_all(["td", "th"], recursive=False)
        first_value = text(cells[0]) if cells else ""
        starts_day_abbr = day_token(first_value)
        starts_day = starts_day_abbr is not None
        if starts_day:
            # A new day is an authoritative boundary. The live portal can leave
            # stale rowspans not only in the date column, but also in time,
            # teacher and room columns.
            active.clear()
            current_day = starts_day_abbr

        row: dict[int, str] = {}
        cells_by_column: dict[int, Any] = {}
        next_active: dict[int, tuple[str, Any, int]] = {}
        for col, (value, cell, rows_left) in active.items():
            row[col] = value
            cells_by_column[col] = cell
            if rows_left > 1:
                next_active[col] = (value, cell, rows_left - 1)
        if header_width is not None and date_column is not None and current_day and not starts_day:
            row.setdefault(date_column, current_day)

        # Some alternative lessons omit the time cell altogether when no
        # rowspan is active.  Reserve that logical column before placing the
        # remaining cells.  Do this only when the first physical cell is not a
        # time, so a missing trailing comment cannot shift the whole record.
        physical_slots = sum(_span(cell.get("colspan")) for cell in cells)
        if header_width is not None and time_column is not None:
            free_slots = header_width - sum(1 for column in row if column < header_width)
            first_is_time = bool(cells and re.search(r"(?<!\d)\d{1,2}:\d{2}(?!\d)", text(cells[0])))
            if free_slots - physical_slots == 1 and time_column not in row and not first_is_time:
                row[time_column] = ""

        col = 0
        for cell_index, cell in enumerate(cells):
            value = text(cell)
            rowspan, colspan = _span(cell.get("rowspan")), _span(cell.get("colspan"))
            if starts_day and cell_index == 0:
                # Храним день канонически ('Пн'), чтобы 'пн'/'Пн.' не теряли пары.
                value = starts_day_abbr
                col = date_column if date_column is not None else 0
                row.pop(col, None)
                # A visual day separator can span the whole table; it is still
                # one semantic date cell, not a lesson repeated in every column.
                colspan = 1
            else:
                while col in row:
                    col += 1
            placed = 0
            while placed < colspan:
                while col in row:
                    col += 1
                row[col] = value
                cells_by_column[col] = cell
                if rowspan > 1:
                    next_active[col] = (value, cell, rowspan - 1)
                col += 1
                placed += 1
        active = next_active
        max_width = max(max_width, max(row, default=-1) + 1)
        logical_rows.append(row)
        if cell_rows is not None:
            owners.append(cells_by_column)
        normalized = {normalize_header(value): column for column, value in row.items() if value}
        if SCHEDULE_TABLE_HEADERS.issubset(normalized):
            header_width = max(row, default=-1) + 1
            date_column = normalized["дата"]
            time_column = normalized["время"]
            current_day = None
    if cell_rows is not None:
        cell_rows.extend([[row.get(col) for col in range(max_width)] for row in owners])
    return [[row.get(col, "") for col in range(max_width)] for row in logical_rows]


def schedule_header(grid: list[list[str]]) -> tuple[int, dict[str, int]] | None:
    for index, row in enumerate(grid):
        mapping: dict[str, int] = {}
        for column, value in enumerate(row):
            header = normalize_header(value)
            if header:
                mapping.setdefault(header, column)
        if SCHEDULE_TABLE_HEADERS.issubset(mapping):
            return index, mapping
    return None


def find_schedule_table_grid(soup: BeautifulSoup):
    """Return the best schedule table together with its expanded grid."""
    best = None
    best_grid = None
    best_score = (-1, -1)
    for table in soup.find_all("table"):
        grid = expand_table(table)
        header = schedule_header(grid)
        if header is None:
            continue
        header_index, mapping = header
        known = len((SCHEDULE_TABLE_HEADERS | SCHEDULE_OPTIONAL_HEADERS) & mapping.keys())
        nonempty_rows = sum(bool(row[mapping["предмет"]].strip()) for row in grid[header_index + 1:] if len(row) > mapping["предмет"])
        score = (known, nonempty_rows)
        if score > best_score:
            best, best_grid, best_score = table, grid, score
    return best, best_grid


def find_schedule_table(soup: BeautifulSoup):
    """Return the best schedule table instead of the first partial candidate."""
    return find_schedule_table_grid(soup)[0]


def _group_link_params(href: object) -> dict[str, str] | None:
    try:
        split = urllib.parse.urlsplit(str(href))
        query = urllib.parse.parse_qs(split.query)
        page = (query.get("page") or [""])[0]
        inst_id = (query.get("instId") or [""])[0].strip()
        name = (query.get("name") or [""])[0].strip()
        typ = (query.get("type") or [""])[0].strip()
        year = (query.get("year") or [""])[0].strip()
    except Exception:  # noqa: BLE001 - malformed portal link, skip it
        return None
    if page != "EditViewGroup" or not (inst_id.isdigit() and name and typ and year.isdigit()):
        return None
    result = {"inst_id": inst_id, "group": name, "type": typ, "year": year}
    # Preserve the route path so the resolver knows which timetable route
    # (ochn/zaochn/session) this group link came from.
    path = split.path.strip("/")
    if path:
        result["route_path"] = path
        # Extract route key from known path patterns like
        # "univer/timetable/ochn/i.1103357" or "univer/timetable/zaochn".
        parts = path.split("/")
        if "timetable" in parts:
            idx = parts.index("timetable")
            if idx + 1 < len(parts):
                result["route"] = parts[idx + 1]
    return result


def parse_institutes(html: str) -> list[dict]:
    """Associate institute names with group links through the DOM.

    This deliberately avoids searching for BeautifulSoup's re-serialized tags
    in the original byte string, which breaks on harmless case/attribute/entity
    normalization.
    """
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, str] = {}
    for link in soup.find_all("a", href=True):
        params = _group_link_params(link["href"])
        if params is None:
            continue
        section = link.find_parent("table")
        heading = section.find("th") if section is not None else link.find_previous("th")
        name = " ".join(heading.get_text(" ", strip=True).split()) if heading else ""
        if name and normalize_header(name) not in SCHEDULE_TABLE_HEADERS | SCHEDULE_OPTIONAL_HEADERS:
            found.setdefault(params["inst_id"], name)
    return [{"name": name, "inst_id": inst_id} for inst_id, name in found.items()]


def parse_groups(html: str, inst_id: str | None = None) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    institute_by_id = {item["inst_id"]: item["name"] for item in parse_institutes(html)}
    groups: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()
    for link in soup.find_all("a", href=True):
        params = _group_link_params(link["href"])
        if params is None:
            continue
        current_inst_id = params["inst_id"]
        if inst_id and str(inst_id) != current_inst_id:
            continue
        key = (current_inst_id, params["group"], params["type"], params["year"])
        if key in seen:
            continue
        seen.add(key)
        groups.append({
            **params,
            "institute": institute_by_id.get(current_inst_id),
        })
    return groups


def count_schedule_grid_lessons(grid: list[list[str]]) -> int:
    """Count semantic lesson rows using the complete logical table grid."""
    header = schedule_header(grid)
    if header is None:
        raise ValueError("schedule header not found")
    header_index, mapping = header
    subject_column = mapping["предмет"]
    date_column = mapping["дата"]
    header_width = max(mapping.values()) + 1
    current_day = None
    count = 0
    for row_index, row in enumerate(grid[header_index + 1:], header_index + 1):
        if any(cell.strip() for cell in row[header_width:]):
            raise ValueError(f"unrecognized schedule row shape at source row {row_index}")
        day = row[date_column].strip() if len(row) > date_column else ""
        if day in DAY_ABBRS:
            current_day = day
            non_date = [value.strip() for column, value in enumerate(row[:header_width]) if column != date_column]
            if all(not value or value == day for value in non_date):
                continue
        subject = row[subject_column].strip() if len(row) > subject_column else ""
        if current_day and subject:
            count += 1
    return count


def count_schedule_lessons(html: str, span=None) -> int:
    """Count semantic lesson rows using the complete logical table grid."""
    table, grid = find_schedule_table_grid(BeautifulSoup(html, "html.parser"))
    if table is None or grid is None:
        raise ValueError("schedule table not found")
    return count_schedule_grid_lessons(grid)


def source_content_until_print(source_html: str):
    soup = BeautifulSoup(source_html, "html.parser")
    content = soup.find(id=CONTENT_ID)
    if content is None:
        content = find_schedule_table(soup)
    if content is None:
        raise RuntimeError("таблица расписания не найдена")

    for tag in content.find_all(["script", "style"]):
        tag.decompose()

    print_link = content.find(string=lambda value: value and "Распечатать" in value)
    if print_link:
        cutoff = print_link.parent
        while cutoff and cutoff.parent is not content:
            cutoff = cutoff.parent
        if cutoff:
            for sibling in list(cutoff.find_next_siblings()):
                sibling.extract()
    return content


def render_schedule_crop_html(source_html: str, source_url: str) -> str:
    content = source_content_until_print(source_html)
    return wrap_schedule_html(str(content), source_url)


DIFF_HIGHLIGHT_CSS = """
  /* Бледная заливка на скрине почти не отличалась от белого фона, а тип
     правки был не виден вообще. Поэтому: насыщенный фон, толстая цветная
     полоса слева у строки и текстовая метка в первой ячейке. */
  tr.diff-row > td { background: #ffe9a8 !important; }
  tr.diff-row.diff-added > td { background: #e4eee7 !important; }
  /* Полосу рисуем inset-тенью, а не border: border добавляет 12px к ширине
     таблицы, и правая колонка «комм.» уезжала за край скрина. */
  tr.diff-row > td:first-child {
    box-shadow: inset 12px 0 0 0 #c1440e;
  }
  tr.diff-row.diff-added > td:first-child {
    box-shadow: inset 12px 0 0 0 #14682c;
  }
  /* td.diff-cell отдельным селектором не перекрывает tr.diff-row > td по
     специфичности, поэтому правило продублировано с обоими классами. */
  td.diff-cell, tr.diff-row > td.diff-cell {
    background: #ffb02e !important; font-weight: 800;
    box-shadow: inset 0 0 0 4px #c1440e;
  }
  tr.diff-row.diff-added td.diff-cell {
    background: #56d364 !important; box-shadow: inset 0 0 0 4px #14682c;
  }
  .diff-tag {
    display: block; margin: 0 0 4px 14px; padding: 2px 8px; border-radius: 5px;
    background: #f3e1dc; color: #8e5147; color: #fff; font-size: 20px; font-weight: 800;
    letter-spacing: 0.3px; white-space: nowrap; width: fit-content;
  }
  tr.diff-row.diff-added .diff-tag { background: #14682c; }
  .diff-legend { margin-top: 16px; font-size: 24px; line-height: 1.45; font-weight: 700; }
  .diff-legend .row { margin-bottom: 6px; }
  .diff-legend .swatch { display: inline-block; width: 26px; height: 26px; margin-right: 10px;
                         vertical-align: -3px; border: 3px solid #c1440e; background: #ffb02e; }
  .diff-legend .swatch.added { border-color: #14682c; background: #56d364; }
"""

SCHEDULE_STATUS_CSS = """
  /* Обычные очные строки остаются ровно такими, как на портале. */
  tr.schedule-dot > td, td.schedule-dot {
    background: #eef7f4 !important;
  }
  tr.schedule-dot > td:first-child, td.schedule-dot:first-child {
    box-shadow: inset 3px 0 0 #7ea99c;
  }
  tr.schedule-inactive > td, td.schedule-inactive {
    background: #e7edf4 !important;
    color: #4d627a !important;
  }
  tr.schedule-inactive > td:first-child, td.schedule-inactive:first-child {
    box-shadow: inset 3px 0 0 #9fb6cf;
  }
  tr.schedule-inactive .schedule-status-content, td.schedule-inactive > .schedule-status-content {
    opacity: 1;
    text-decoration: line-through;
    text-decoration-thickness: 2px;
    text-decoration-color: #7b8da3;
  }
  tr.schedule-cancelled > td, td.schedule-cancelled {
    background: #fef2f2 !important;
    color: #991b1b !important;
  }
  tr.schedule-cancelled > td:first-child, td.schedule-cancelled:first-child {
    box-shadow: inset 7px 0 0 #c1440e;
  }
  tr.schedule-cancelled .schedule-status-content, td.schedule-cancelled > .schedule-status-content {
    opacity: .72;
    text-decoration: line-through;
    text-decoration-thickness: 2px;
  }
  .schedule-status-tag {
    display: block; width: fit-content; margin: 0 0 7px; padding: 4px 8px;
    border-radius: 5px; color: #416b60; background: #dfeee8;
    font-size: 18px; line-height: 1.25; font-weight: 600; letter-spacing: 0;
    text-decoration: none !important; white-space: normal; max-width: 100%;
  }
  tr.schedule-inactive .schedule-status-tag, td.schedule-inactive .schedule-status-tag {
    background: #d8e2ee; color: #3f5873;
  }
  tr.schedule-cancelled .schedule-status-tag, td.schedule-cancelled .schedule-status-tag {
    background: #f3e1dc; color: #8e5147;
  }
"""

def _status_label(item: dict) -> str:
    """Short display only; applicability still comes from schedule_logic."""
    struct = item.get("_schedule_struct") or {}
    if struct.get("kind") == "after_week" and struct.get("data", {}).get("week_number"):
        return f"После {struct['data']['week_number']}‑й недели"
    label = str(struct.get("display_text") or item.get("_schedule_tag") or "ДОТ")
    label = re.sub(r"^НЕ (?:НА ЭТОЙ НЕДЕЛЕ|В ЭТУ ДАТУ)\s*·\s*", "", label)
    return label if label == "ДОТ" else label[:1].upper() + label[1:].lower()



COMPARISON_SCREEN_CSS = """
  .wrap { width: 1480px; }
  table.comparison-table { table-layout: fixed; }
  .comparison-table td, .comparison-table th {
    width: auto !important; min-width: 0 !important;
    white-space: normal !important; overflow-wrap: anywhere;
  }
  .comparison-heading { font-size: 30px; font-weight: 800; margin: 0 0 12px; }
  .comparison-table .comparison-changed {
    background: #f3ead5 !important; box-shadow: inset 0 0 0 2px #c5ad79;
  }
  .comparison-table .comparison-added { background: #e4eee7 !important; }
  .comparison-table .comparison-removed { background: #f4e5e1 !important; }
  .comparison-tag {
    display: block; width: fit-content; margin: 0 0 7px; padding: 4px 8px;
    border-radius: 4px; font-size: 20px; font-weight: 700; line-height: 1.25;
    background: #e1eee5; color: #416651;
  }
  .comparison-removed .comparison-tag { background: #efded9; color: #8c5349; }
"""


def _mark_comparison_table(table, marks: list[dict]) -> None:
    """Annotate source cells, including inherited rowspans, without text matching."""
    cell_rows: list = []
    grid = expand_table(table, cell_rows=cell_rows)
    header = schedule_header(grid)
    if header is None:
        return
    header_index, columns = header
    by_row = {mark["source_row"]: mark for mark in marks}
    users: dict[int, set[int]] = {}
    for index, row in enumerate(grid):
        if index <= header_index or not row[columns["предмет"]] or day_token(row[columns["предмет"]]):
            continue
        for cell in cell_rows[index]:
            if cell is not None:
                users.setdefault(id(cell), set()).add(index)
    soup = BeautifulSoup("", "html.parser")
    for index, mark in by_row.items():
        if index not in range(header_index + 1, len(cell_rows)):
            continue
        kind = mark["kind"]
        row = cell_rows[index]
        names = mark.get("columns", []) if kind == "changed" else columns.keys()
        tagged = []
        for name in names:
            column = columns.get(name)
            cell = row[column] if column is not None else None
            if cell is None or name == "дата":
                continue
            # Shared cells stay neutral if an unchanged alternative uses them.
            if kind != "changed" and any(by_row.get(other, {}).get("kind") != kind for other in users.get(id(cell), set())):
                continue
            cell["class"] = list(dict.fromkeys([*cell.get("class", []), f"comparison-{kind}"]))
            tagged.append(cell)
        if kind in {"added", "removed"} and tagged:
            subject = row[columns["предмет"]]
            target = next((cell for cell in tagged if cell is subject), tagged[0])
            if target.select_one(".comparison-tag") is None:
                tag = soup.new_tag("span", attrs={"class": "comparison-tag"})
                tag.string = "Добавили" if kind == "added" else "Убрали"
                target.insert(0, tag)


_DIFF_TIME_RE = re.compile(r"\d{1,2}:\d{2}")
_DIFF_MIN_SCORE = 3.0


def _diff_row_score(row_norm: str, item: dict) -> float:
    """Насколько строка портала похожа на запись диффа.

    Предмет обязателен (3 балла), время и аудитория добавляют уверенности.
    Время в портале живёт в rowspan на несколько строк, поэтому его отсутствие
    в конкретной строке не отменяет совпадение. Все начала часов на месте —
    сильное совпадение; часть часов даёт почти ничего, иначе соседняя пара
    с общим часом («16:00 17:00» против «17:00 18:00») перетянет строку.
    У изменённых записей нет room/teacher — только поля, поэтому их время
    обязано совпасть целиком.
    """
    subject = norm_text(str(item.get("subject") or "").split("\n", 1)[0])
    if not subject or subject not in row_norm:
        return 0.0
    score = 3.0
    tokens = _DIFF_TIME_RE.findall(str(item.get("time") or ""))
    if tokens:
        hits = sum(1 for token in tokens if token in row_norm)
        if 0 < hits < len(tokens):
            # Совпал только один из часов — это соседняя пара («16:00 17:00»
            # против «17:00 18:00»), а не наша. Нулевые совпадения при этом
            # допустимы: в портале время живёт в rowspan на несколько строк.
            return 0.0
        score += 2.0 if hits == len(tokens) else 0.0
    room = norm_text(item.get("room"))
    if room and room != "—" and room in row_norm:
        score += 1.0
    teacher = norm_text(item.get("teacher"))
    if teacher and teacher in row_norm:
        score += 0.5
    return score


def _diff_cell_values(item: dict) -> list[str]:
    """Новые значения полей изменённой пары — их подсвечиваем точечно."""
    values: list[str] = []
    for field in item.get("fields") or []:
        if not isinstance(field, (list, tuple)) or len(field) != 3:
            continue
        new_value = norm_text(field[2])
        if new_value and new_value != "—":
            values.append(new_value)
    return values


_DIFF_TAG_TEXT = {"added": "НОВАЯ ПАРА", "changed": "ИЗМЕНИЛИ"}


def _mark_diff_row(row, item: dict) -> None:
    kind = "added" if item.get("_diff_kind") == "added" else "changed"
    row["class"] = [*row.get("class", []), "diff-row", f"diff-{kind}"]
    # Цвет читается только если знать легенду, поэтому пишем словами прямо в
    # строке: на скрине сразу видно, добавили пару или поправили существующую.
    cells = row.find_all(["td", "th"], recursive=False)
    if cells:
        soup = BeautifulSoup("", "html.parser")
        tag = soup.new_tag("div")
        tag["class"] = ["diff-tag"]
        tag.string = _DIFF_TAG_TEXT[kind]
        cells[0].insert(0, tag)
    values = _diff_cell_values(item)
    if not values:
        return
    for cell in row.find_all(["td", "th"], recursive=False):
        cell_norm = norm_text(cell.get_text(" ", strip=True))
        if not cell_norm:
            continue
        for value in values:
            # Короткие значения («415», «2») совпадают только целиком: иначе
            # номер аудитории находится внутри времени или чужой ячейки.
            if cell_norm == value or (len(value) >= 6 and value in cell_norm):
                cell["class"] = [*cell.get("class", []), "diff-cell"]
                break


def highlight_day_group(group: list[str], items: list[dict]) -> tuple[list[str], int]:
    """Подсветить в HTML дня строки и ячейки из диффа.

    Портальную таблицу не перерисовываем: добавляем только классы, поэтому скрин
    остаётся оригиналом с портала, а правки видно глазом. Возвращает
    (строки дня, сколько строк помечено). Убранные пары в новом HTML уже не
    существуют, поэтому помечаются только добавленные и изменённые.
    """
    marked_total = 0
    out: list[str] = []
    for row_html in group:
        soup = BeautifulSoup(row_html, "html.parser")
        rows = soup.find_all("tr")
        if not rows:
            out.append(row_html)
            continue
        best: dict[int, tuple[dict, float]] = {}
        for row in rows:
            row_norm = norm_text(row.get_text(" ", strip=True))
            top_item, top_score = None, 0.0
            for item in items:
                score = _diff_row_score(row_norm, item)
                if score > top_score:
                    top_item, top_score = item, score
            if top_item is not None and top_score >= _DIFF_MIN_SCORE:
                best[id(row)] = (top_item, top_score)
        # Порталь вложен в строку дня ещё и первую пару: внешнюю строку не красим,
        # если совпала вложенная.
        hits = 0
        for row in rows:
            if id(row) not in best:
                continue
            if any(id(child) in best for child in row.find_all("tr")):
                continue
            _mark_diff_row(row, best[id(row)][0])
            hits += 1
        marked_total += hits
        out.append(str(soup) if hits else row_html)
    return out, marked_total


def _status_row_score(row_norm: str, item: dict) -> float:
    score = _diff_row_score(row_norm, item)
    if score < _DIFF_MIN_SCORE:
        return 0.0
    row_is_dot = "дот" in row_norm
    item_is_dot = bool(item.get("_schedule_dot"))
    if item_is_dot != row_is_dot:
        return 0.0
    room = norm_text(item.get("room"))
    if room and room not in {"—", "."}:
        if room not in row_norm:
            return 0.0
        score += 2.0
    return score


def _mark_schedule_status(row, item: dict) -> None:
    status = str(item.get("_schedule_status") or "")
    struct = item.get("_schedule_struct") or {}
    kind = struct.get("kind")
    if status not in {"dot", "inactive"} and kind != "cancelled":
        return

    # Determine CSS class: cancelled gets its own class, dot stays dot, rest are inactive
    if kind == "cancelled":
        css_class = "schedule-cancelled"
    elif status == "dot":
        css_class = "schedule-dot"
    else:
        css_class = "schedule-inactive"

    row["class"] = [*row.get("class", []), css_class]
    cells = row.find_all(["td", "th"], recursive=False)
    if css_class in {"schedule-inactive", "schedule-cancelled"}:
        soup = BeautifulSoup("", "html.parser")
        for cell in cells:
            wrapper = soup.new_tag("span")
            wrapper["class"] = ["schedule-status-content"]
            for child in list(cell.contents):
                wrapper.append(child.extract())
            cell.append(wrapper)

    subject = norm_text(str(item.get("subject") or "").split("\n", 1)[0])
    target = next((cell for cell in cells if subject and subject in norm_text(cell.get_text(" ", strip=True))), None)
    if target is None and cells:
        target = cells[min(2, len(cells) - 1)]
    if target is not None:
        soup = BeautifulSoup("", "html.parser")
        tag = soup.new_tag("span")
        tag["class"] = ["schedule-status-tag"]
        # Use display_text from struct if available, otherwise fall back to _schedule_tag
        display_text = struct.get("display_text") or item.get("_schedule_tag") or ("ДОТ" if status == "dot" else "НЕ АКТУАЛЬНО")
        tag.string = _status_label(item)
        target.insert(0, tag)


def annotate_schedule_day_group(group: list[str], items: list[dict]) -> tuple[list[str], int]:
    """Mark only active DOT and lessons not applicable to the selected date."""
    marked_total = 0
    out: list[str] = []
    for row_html in group:
        soup = BeautifulSoup(row_html, "html.parser")
        rows = soup.find_all("tr")
        best: dict[int, tuple[dict, float]] = {}
        for row in rows:
            row_norm = norm_text(row.get_text(" ", strip=True))
            top_item, top_score = None, 0.0
            for item in items:
                score = _status_row_score(row_norm, item)
                if score > top_score:
                    top_item, top_score = item, score
            if top_item is not None:
                best[id(row)] = (top_item, top_score)
        hits = 0
        for row in rows:
            if id(row) not in best:
                continue
            if any(id(child) in best for child in row.find_all("tr")):
                continue
            _mark_schedule_status(row, best[id(row)][0])
            hits += 1
        marked_total += hits
        out.append(str(soup) if hits else row_html)
    return out, marked_total


def _dashboard_schedule_table(table, statuses: dict[str, list[dict]]):
    """Project retained source cells, transferring rowspans out of expired rows."""
    owners: list = []
    grid = expand_table(table, cell_rows=owners)
    header_index, columns = schedule_header(grid)
    source_rows = list(iter_table_rows(table))
    width = max(columns.values()) + 1
    date_col, subject_col = columns["дата"], columns["предмет"]
    days: dict[str, list[int]] = {}
    for index in range(header_index + 1, len(grid)):
        row = grid[index]
        day = day_short(row[date_col])
        if day and row[subject_col] and day_token(row[subject_col]) != day:
            if any(value.strip() for value in row[width:]):
                raise ValueError("unrecognized dashboard source row")
            days.setdefault(day, []).append(index)
    marks = {}
    for day, items in statuses.items():
        for item in items:
            index = item.get("source_row")
            if type(index) is not int or index not in days.get(day, []):
                raise ValueError("dashboard status has no matching source row")
            evidence = {"subject_raw": "предмет", "raw_time": "время",
                        "raw_room": "ауд.", "raw_comment": "комм."}
            subject = item.get("subject_raw", str(item.get("subject") or "").split("\n", 1)[0])
            if norm_text(subject) != norm_text(grid[index][subject_col]):
                raise ValueError("dashboard subject provenance mismatch")
            for key, name in evidence.items():
                if key in item and name in columns and norm_text(item[key]) != norm_text(grid[index][columns[name]]):
                    raise ValueError("dashboard cell provenance mismatch")
            if index in marks:
                raise ValueError("duplicate dashboard status source row")
            marks[index] = item

    # Detach children first so malformed nested day rows cannot duplicate or
    # swallow another lesson when a source cell is copied below.
    for row in reversed(source_rows):
        row.extract()
    soup = BeautifulSoup("", "html.parser")
    result = soup.new_tag("table", attrs=dict(table.attrs))
    result.append(copy.deepcopy(source_rows[header_index]))
    for day, indices in days.items():
        kept = [index for index in indices if marks.get(index, {}).get("_schedule_expired") is not True]
        if not kept:
            continue
        day_cell = next((owners[index][date_col] for index in kept if owners[index][date_col] is not None), None)
        if day_cell is None:
            day_cell = soup.new_tag("td")
            day_cell.string = day
        matrix = []
        for index in kept:
            cells = list(owners[index][:width])
            cells[date_col] = day_cell
            for col, cell in enumerate(cells):
                if cell is None:
                    cells[col] = soup.new_tag("td")
            matrix.append(cells)
        footprints: dict[int, set[tuple[int, int]]] = {}
        for r, cells in enumerate(matrix):
            for c, cell in enumerate(cells):
                footprints.setdefault(id(cell), set()).add((r, c))
        rendered_cells = {}
        for r, index in enumerate(kept):
            row = soup.new_tag("tr", attrs=dict(source_rows[index].attrs))
            row["data-source-row"] = str(index)
            mark = marks.get(index, {})
            status = mark.get("_schedule_status")
            struct = mark.get("_schedule_struct") or {}
            kind = struct.get("kind")
            if kind == "cancelled":
                row["data-schedule-status"] = "cancelled"
            elif status in {"dot", "inactive"}:
                row["data-schedule-status"] = status
            for c, original in enumerate(matrix[r]):
                positions = footprints[id(original)]
                first_row = min(pos[0] for pos in positions)
                first_col = min(pos[1] for pos in positions)
                if (r, c) != (first_row, first_col):
                    continue
                last_row = max(pos[0] for pos in positions)
                last_col = max(pos[1] for pos in positions)
                expected = {(rr, cc) for rr in range(r, last_row + 1) for cc in range(c, last_col + 1)}
                if positions != expected:
                    raise ValueError("non-rectangular dashboard cell span")
                cell = copy.deepcopy(original)
                cell.attrs.pop("rowspan", None)
                cell.attrs.pop("colspan", None)
                if last_row > r:
                    cell["rowspan"] = str(last_row - r + 1)
                if last_col > c:
                    cell["colspan"] = str(last_col - c + 1)
                def _cell_state(rr):
                    m = marks.get(kept[rr], {})
                    s = m.get("_schedule_status", "")
                    k = (m.get("_schedule_struct") or {}).get("kind")
                    return "cancelled" if k == "cancelled" else s
                states = {_cell_state(rr) for rr, _ in positions}
                if c != date_col and len(states) == 1 and states <= {"dot", "inactive", "cancelled"}:
                    state = next(iter(states))
                    cell["class"] = [*cell.get("class", []), f"schedule-{state}"]
                    if state in {"inactive", "cancelled"}:
                        wrapper = soup.new_tag("span", attrs={"class": "schedule-status-content"})
                        for child in list(cell.contents):
                            wrapper.append(child.extract())
                        cell.append(wrapper)
                row.append(cell)
                for position in positions:
                    rendered_cells[position] = cell
            result.append(row)
        for r, index in enumerate(kept):
            mark = marks.get(index, {})
            mark_struct = mark.get("_schedule_struct") or {}
            mark_kind = mark_struct.get("kind")
            effective_status = "cancelled" if mark_kind == "cancelled" else mark.get("_schedule_status")
            if effective_status not in {"dot", "inactive", "cancelled"}:
                continue
            candidates = [subject_col, *[c for c in range(width) if c not in {date_col, subject_col}]]
            for c in candidates:
                positions = footprints[id(matrix[r][c])]
                def _tag_key(rr):
                    m = marks.get(kept[rr], {})
                    st = m.get("_schedule_struct") or {}
                    k = st.get("kind")
                    eff = "cancelled" if k == "cancelled" else m.get("_schedule_status")
                    text = st.get("display_text") or m.get("_schedule_tag")
                    return (eff, text)
                tags = {_tag_key(rr) for rr, _ in positions}
                if len(tags) != 1:
                    continue
                cell = rendered_cells[(r, c)]
                if cell.select_one(".schedule-status-tag") is None:
                    tag = soup.new_tag("span", attrs={"class": "schedule-status-tag"})
                    display = mark_struct.get("display_text") or mark.get("_schedule_tag") or "ДОТ"
                    tag.string = _status_label(mark)
                    cell.insert(0, tag)
                break
            else:
                raise ValueError("cannot label a shared dashboard status cell")
    return result


def render_schedule_day_chunk_htmls(
    source_html: str,
    source_url: str,
    days_per_chunk: int = 1,
    diff_items: list[dict] | None = None,
    diff_legend: str = "",
    removed_note: bool = True,
    schedule_statuses: dict[str, list[dict]] | None = None,
    comparison_title: str = "",
    comparison_marks: list[dict] | None = None,
) -> list[dict]:
    content = source_content_until_print(source_html)
    table = (
        content
        if getattr(content, "name", None) == "table" and schedule_header(expand_table(content)) is not None
        else find_schedule_table(content)
    )
    if table is None:
        return [{"label": "с сайта", "html": render_schedule_crop_html(source_html, source_url), "marked": 0}]

    header_parts = []
    colgroup = ""
    if comparison_title:
        header = schedule_header(expand_table(table))
        weights = {"дата": 7, "время": 10, "под гр.": 7, "предмет": 28,
                   "преподаватель": 18, "ауд.": 10, "комм.": 20}
        names = {column: name for name, column in header[1].items()}
        widths = [weights.get(names.get(column), 10) for column in range(max(names) + 1)]
        colgroup = "<colgroup>" + "".join(
            f'<col style="width:{width / sum(widths) * 100:.4f}%">' for width in widths
        ) + "</colgroup>"
        _mark_comparison_table(table, comparison_marks or [])

    if content is not table:
        branch = table
        while branch is not content and branch.parent is not None:
            prefix = [str(sibling) for sibling in reversed(list(branch.previous_siblings))
                      if getattr(sibling, "name", None)]
            header_parts[0:0] = prefix
            branch = branch.parent

    dashboard = schedule_statuses is not None and not comparison_title
    if dashboard:
        table = _dashboard_schedule_table(table, schedule_statuses)
        if len(list(iter_table_rows(table))) <= 1:
            return []

    # Keep the portal's invalid outer day rows intact for rendering (they
    # already contain the nested first lesson).  Fall back to the semantic row
    # iterator for normal thead/tbody markup.
    # The portal nests the first lesson inside an outer day row. Serializing
    # both rows duplicates that lesson in the screenshot because the outer
    # row already contains the nested markup.
    rows = [
        row for row in iter_table_rows(table)
        if row.find_parent("tr") is None
        or row.find_parent("tr").find_parent("table") is not table
    ]
    if comparison_title:
        # Flatten owned rows once: an unclosed day row may also contain the
        # next day's separator, not just its own first lesson.
        rows = list(iter_table_rows(table))
        for row in reversed(rows):
            row.extract()
    if not rows:
        return [{"label": "с сайта", "html": render_schedule_crop_html(source_html, source_url), "marked": 0}]

    table_header = str(rows[0])
    day_groups: list[list[str]] = []
    current: list[str] = []
    for row in rows[1:]:
        cells = [text(cell) for cell in row.find_all(["td", "th"], recursive=False)]
        is_day_row = bool(cells) and day_token(cells[0]) is not None and (comparison_title or dashboard or len(cells) == 1)
        if is_day_row and current:
            day_groups.append(current)
            current = []
        current.append(str(row))
    if current:
        day_groups.append(current)
    if not day_groups:
        return [{"label": "с сайта", "html": render_schedule_crop_html(source_html, source_url), "marked": 0}]

    print_link = content.find(string=lambda value: value and "Распечатать" in value)
    print_html = f'<p class="print-link">{print_link.parent}</p>' if print_link and print_link.parent else ""

    chunks = []
    days_per_chunk = max(1, days_per_chunk)
    for start in range(0, len(day_groups), days_per_chunk):
        groups = day_groups[start:start + days_per_chunk]
        labels = [_day_label_from_group(group) for group in groups]
        rendered: list[list[str]] = []
        marked = 0
        status_marked = 0
        for group, label in zip(groups, labels):
            if dashboard:
                status_marked += sum(row.count('data-schedule-status=') for row in group)
            group_items = [
                item for item in (diff_items or []) if day_short(item.get("day")) == label
            ]
            if group_items:
                group, hits = highlight_day_group(group, group_items)
                marked += hits
            rendered.append(group)
        table_class = "shedultable comparison-table" if comparison_title else "shedultable"
        table_html = f'<table border="1" class="{table_class}" style="border-spacing: 1px">'
        table_html += colgroup + table_header
        table_html += "".join("".join(group) for group in rendered)
        table_html += "</table>"
        # Какие именно правки легли на скрин: подпись в посте обещает только
        # те цвета, которые реально помечены.
        kinds: set[str] = set()
        for group in rendered:
            joined = "".join(group)
            if "diff-added" in joined:
                kinds.add("added")
            if "diff-changed" in joined:
                kinds.add("changed")
        legend = ""
        if marked and diff_legend:
            # Два цвета — два образца, но показываем только те, которые реально
            # есть на скрине: при одном переименовании зелёный образец уводил
            # читателя искать несуществующие «новые пары».
            rows = []
            if "added" in kinds:
                rows.append(
                    '<div class="row"><span class="swatch added"></span>'
                    "НОВАЯ ПАРА — зелёная строка: этой пары раньше не было.</div>"
                )
            if "changed" in kinds:
                rows.append(
                    '<div class="row"><span class="swatch"></span>'
                    "ИЗМЕНИЛИ — оранжевая строка: пара была, поправили поле; "
                    "яркая ячейка внутри — что именно.</div>"
                )
            if removed_note:
                # Строка про убранные пары нужна, только если их правда убирали:
                # на посте с одним переименованием она пугала пропажей пары.
                rows.append(
                    '<div class="row">Убранных пар на скрине уже нет — они перечислены в посте.</div>'
                )
            legend = '<div class="diff-legend">' + "".join(rows) + "</div>"
        suffix = print_html if start + days_per_chunk >= len(day_groups) else ""
        comparison_heading = (
            '<div class="comparison-heading">'
            + htmlmod.escape(comparison_title + " · " + " + ".join(labels)) + "</div>"
            if comparison_title else ""
        )
        comparison_kinds = sorted({
            kind for kind in ("changed", "added", "removed") if f'comparison-{kind}' in table_html
        })
        chunks.append({
            "label": " + ".join(labels),
            "html": wrap_schedule_html(
                comparison_heading + "".join(header_parts) + table_html + legend + suffix,
                source_url,
                extra_css=(SCHEDULE_STATUS_CSS if status_marked else "") + (DIFF_HIGHLIGHT_CSS if marked else "")
                + (COMPARISON_SCREEN_CSS if comparison_title else ""),
            ),
            "marked": marked,
            **({"status_marked": status_marked} if status_marked else {}),
            **({"marked_kinds": sorted(kinds)} if kinds else {}),
            **({"comparison_kinds": comparison_kinds} if comparison_title else {}),
        })
    return chunks


def _day_label_from_group(group: list[str]) -> str:
    soup = BeautifulSoup("".join(group), "html.parser")
    first = soup.find("tr")
    if not first:
        return "День"
    cells = [text(cell) for cell in first.find_all(["td", "th"], recursive=False)]
    return (day_token(cells[0]) if cells else None) or "День"


def wrap_schedule_html(content_html: str, source_url: str, extra_css: str = "") -> str:
    escaped_source_url = htmlmod.escape(source_url, quote=True)
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<base href="{escaped_source_url}">
<style>
  html, body {{ margin: 0; padding: 0; background: #fdfdfb; }}
  body {{ font-family: Arial, Helvetica, sans-serif; color: #203850; }}
  .wrap {{ width: 1280px; padding: 20px 22px 28px; box-sizing: border-box; }}
  .block_content {{ width: 100%; }}
  h1 {{ font-size: 34px; line-height: 1.15; margin: 0 0 8px; font-weight: 800; }}
  h2 {{ font-size: 31px; line-height: 1.15; margin: 8px 0 10px; font-weight: 800; }}
  h2[style*="tomato" i] {{ color: #b87565 !important; }}
  h3 {{ font-size: 28px; line-height: 1.2; margin: 12px 0 8px; }}
  h4, p {{ font-size: 24px; line-height: 1.25; margin: 8px 0; }}
  table {{ width: 100%; border-collapse: collapse; table-layout: auto; font-size: 28px; line-height: 1.22; }}
  th, td {{ border: 1px solid #cdd9e7; padding: 9px 11px; vertical-align: top; background: #fff; }}
  td {{ overflow-wrap: anywhere; }}
  th {{ background: #e5e7eb; font-weight: 800; text-align: left; }}
  tr:first-child td, tr:first-child th {{ background: #e1eaf5; font-weight: 800; }}
  b {{ font-weight: 800; }}
  a {{ color: inherit; text-decoration: none; }}
  td[style*="width:1px"], th[style*="width:1px"] {{ white-space: nowrap; }}
  td[style*="width:150px"] {{ min-width: 340px; }}
  td[style*="width:100px"] {{ min-width: 180px; }}
  .print-link {{ margin-top: 16px; font-size: 24px; font-weight: 700; }}
{extra_css}</style>
</head>
<body><div class="wrap">{content_html}</div></body>
</html>"""
