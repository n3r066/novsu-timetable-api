"""HTML parsing helpers for the public NovSU timetable portal."""
from __future__ import annotations

import html as htmlmod
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


def expand_table(table) -> list[list[str]]:
    """Expand all rowspans/colspans into a logical grid.

    A day separator is authoritative and clears a stale day rowspan left by the
    portal.  This preserves the old stale-rowspan tolerance while also handling
    the portal's nested first lesson, normal tbody markup and rowspans in any
    column.
    """
    active: dict[int, tuple[str, int]] = {}
    logical_rows: list[dict[int, str]] = []
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
        next_active: dict[int, tuple[str, int]] = {}
        for col, (value, rows_left) in active.items():
            row[col] = value
            if rows_left > 1:
                next_active[col] = (value, rows_left - 1)
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
                if rowspan > 1:
                    next_active[col] = (value, rowspan - 1)
                col += 1
                placed += 1
        active = next_active
        max_width = max(max_width, max(row, default=-1) + 1)
        logical_rows.append(row)
        normalized = {normalize_header(value): column for column, value in row.items() if value}
        if SCHEDULE_TABLE_HEADERS.issubset(normalized):
            header_width = max(row, default=-1) + 1
            date_column = normalized["дата"]
            time_column = normalized["время"]
            current_day = None
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


def find_schedule_table(soup: BeautifulSoup):
    """Return the best schedule table instead of the first partial candidate."""
    best = None
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
            best, best_score = table, score
    return best


def _group_link_params(href: object) -> dict[str, str] | None:
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(str(href)).query)
        page = (query.get("page") or [""])[0]
        inst_id = (query.get("instId") or [""])[0].strip()
        name = (query.get("name") or [""])[0].strip()
        typ = (query.get("type") or [""])[0].strip()
        year = (query.get("year") or [""])[0].strip()
    except Exception:  # noqa: BLE001 - malformed portal link, skip it
        return None
    if page != "EditViewGroup" or not (inst_id.isdigit() and name and typ and year.isdigit()):
        return None
    return {"inst_id": inst_id, "group": name, "type": typ, "year": year}


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


def count_schedule_lessons(html: str, span=None) -> int:
    """Count semantic lesson rows using the complete logical table grid."""
    table = find_schedule_table(BeautifulSoup(html, "html.parser"))
    if table is None:
        raise ValueError("schedule table not found")
    grid = expand_table(table)
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
  tr.diff-row > td { background: #fff3c4 !important; }
  /* td.diff-cell отдельным селектором не перекрывает tr.diff-row > td по
     специфичности, поэтому правило продублировано с обоими классами. */
  td.diff-cell, tr.diff-row > td.diff-cell {
    background: #ffc233 !important; font-weight: 800; box-shadow: inset 0 0 0 3px #e65100;
  }
  .diff-legend { margin-top: 16px; font-size: 24px; line-height: 1.3; font-weight: 700; }
  .diff-legend .swatch { display: inline-block; width: 26px; height: 26px; margin-right: 10px;
                         vertical-align: -3px; background: #ffc233; border: 3px solid #e65100; }
"""

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


def _mark_diff_row(row, item: dict) -> None:
    row["class"] = [*row.get("class", []), "diff-row"]
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


def render_schedule_day_chunk_htmls(
    source_html: str,
    source_url: str,
    days_per_chunk: int = 1,
    diff_items: list[dict] | None = None,
    diff_legend: str = "",
) -> list[dict]:
    content = source_content_until_print(source_html)
    table = find_schedule_table(content)
    if table is None:
        return [render_schedule_crop_html(source_html, source_url)]

    header_parts = []
    for child in content.find_all(recursive=False):
        if child is table:
            break
        header_parts.append(str(child))

    # Keep the portal's invalid outer day rows intact for rendering (they
    # already contain the nested first lesson).  Fall back to the semantic row
    # iterator for normal thead/tbody markup.
    rows = table.find_all("tr", recursive=False)
    if not rows:
        rows = list(iter_table_rows(table))
    if not rows:
        return [render_schedule_crop_html(source_html, source_url)]

    table_header = str(rows[0])
    day_groups: list[list[str]] = []
    current: list[str] = []
    for row in rows[1:]:
        cells = [text(cell) for cell in row.find_all(["td", "th"], recursive=False)]
        is_day_row = len(cells) == 1 and day_token(cells[0]) is not None
        if is_day_row and current:
            day_groups.append(current)
            current = []
        current.append(str(row))
    if current:
        day_groups.append(current)
    if not day_groups:
        return [render_schedule_crop_html(source_html, source_url)]

    print_link = content.find(string=lambda value: value and "Распечатать" in value)
    print_html = f'<p class="print-link">{print_link.parent}</p>' if print_link and print_link.parent else ""

    chunks = []
    days_per_chunk = max(1, days_per_chunk)
    for start in range(0, len(day_groups), days_per_chunk):
        groups = day_groups[start:start + days_per_chunk]
        labels = [_day_label_from_group(group) for group in groups]
        rendered: list[list[str]] = []
        marked = 0
        for group, label in zip(groups, labels):
            group_items = [
                item for item in (diff_items or []) if day_short(item.get("day")) == label
            ]
            if group_items:
                group, hits = highlight_day_group(group, group_items)
                marked += hits
            rendered.append(group)
        table_html = '<table border="1" class="shedultable" style="border-spacing: 1px">'
        table_html += table_header
        table_html += "".join("".join(group) for group in rendered)
        table_html += "</table>"
        legend = ""
        if marked and diff_legend:
            legend = (
                '<p class="diff-legend"><span class="swatch"></span>'
                f"{htmlmod.escape(diff_legend)}</p>"
            )
        suffix = print_html if start + days_per_chunk >= len(day_groups) else ""
        chunks.append({
            "label": " + ".join(labels),
            "html": wrap_schedule_html(
                "".join(header_parts) + table_html + legend + suffix,
                source_url,
                extra_css=DIFF_HIGHLIGHT_CSS if marked else "",
            ),
            "marked": marked,
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
  html, body {{ margin: 0; padding: 0; background: #fff; }}
  body {{ font-family: Arial, Helvetica, sans-serif; color: #111827; }}
  .wrap {{ width: 1280px; padding: 20px 22px 28px; box-sizing: border-box; }}
  .block_content {{ width: 100%; }}
  h1 {{ font-size: 34px; line-height: 1.15; margin: 0 0 8px; font-weight: 800; }}
  h2 {{ font-size: 31px; line-height: 1.15; margin: 8px 0 10px; font-weight: 800; }}
  h3 {{ font-size: 28px; line-height: 1.2; margin: 12px 0 8px; }}
  h4, p {{ font-size: 24px; line-height: 1.25; margin: 8px 0; }}
  table {{ width: 100%; border-collapse: collapse; table-layout: auto; font-size: 28px; line-height: 1.22; }}
  th, td {{ border: 2px solid #cbd5e1; padding: 9px 11px; vertical-align: top; background: #fff; }}
  th {{ background: #e5e7eb; font-weight: 800; text-align: left; }}
  tr:first-child td, tr:first-child th {{ background: #dbeafe; font-weight: 800; }}
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
