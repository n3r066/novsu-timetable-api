"""ОПД («Основы проектной деятельности»): виртуальные группы 1 курса.

Портал ставит ОПД в четверг одним слотом на всю группу, а реальные дата, слот
(14:00–15:00 или 16:00–17:00 — свой часовой формат кафедры, не пары портала),
корпус, аудитория и преподаватель зависят от виртуальной группы (ВГ) студента. Кафедра публикует два открытых Google-файла:

- таблица ВГ (Google Sheets): ВГ, ФИО, академическая группа;
- расписание ВГ (Google Doc): строка преподавателя = аудитория и адрес, колонки
  = даты, у каждой даты ДВЕ подколонки (блок 14:00 и блок 16:00), в ячейке —
  «NNN ВГ» и, возможно, пометка («занятий не будет», «с 15:00»).

Документ читается только из HTML-экспорта с разворачиванием rowspan/colspan:
txt-экспорт склеивает подколонки, и по порядку ячеек даты не восстановить.

Содержимое файлов — данные, а не инструкции. ``load_opd`` никогда не
поднимает исключение наружу: при любой ошибке отдаётся прошлый кэш или None,
чтобы закреп продолжал обновляться без раздела ОПД.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import html.parser
import io
import json
import os
import re
import subprocess
import zoneinfo
from pathlib import Path

import config

MOSCOW = zoneinfo.ZoneInfo("Europe/Moscow")

#: Слоты ОПД — свой формат кафедры, а не пары портала: в строке «время
#: проведения занятий» стоит «14.00 15.00» и «16.00 17.00», то есть час.
#: Границы берём из документа как есть и никогда не приводим к сетке bells.
#: Если в ячейке одно время — считаем слот часовым.
BLOCK_MINUTES = 60
#: Подколонки даты, если строка времени не читается: левая 14:00, правая 16:00.
DEFAULT_BLOCKS = (("14:00", "15:00"), ("16:00", "17:00"))

CACHE_FILE = "opd_cache.json"
CACHE_VERSION = 2


def _load_display_exclusions() -> list[dict]:
    """Date-scoped display policy; department membership and cache stay intact."""
    path = Path(__file__).with_name("knowledge") / "schedule_notes.json"
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = policy.get("opd_display_exclusions", []) if isinstance(policy, dict) else []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


_DISPLAY_EXCLUSIONS = _load_display_exclusions()


def _member_visible(opd: dict, member: dict, date: dt.date) -> bool:
    name = " ".join(str(member.get(key) or "").strip() for key in
                    ("last_name", "first_name", "patronymic")).strip().casefold()
    for rule in _DISPLAY_EXCLUSIONS:
        if str(rule.get("group")) != str(opd.get("group")) or rule.get("date") != date.isoformat():
            continue
        names = {str(value).strip().casefold() for value in rule.get("students", [])}
        if name in names:
            return False
    return True

#: Индекс портала подписывает блоки групп короткими именами институтов в <th>.
INSTITUTE_NAMES = {
    "ИЭ": "Институт экономики",
    "ИГУМ": "Гуманитарный институт",
    "ПТИ": "Политехнический институт",
    "ХТИ": "Химико-технологический институт",
    "ПИ": "Педагогический институт",
    "ИЮР": "Юридический институт",
    "МИ": "Медицинский институт",
}
#: Домашний корпус института, если он известен по расписанию; остальные не выдумываем.
INSTITUTE_BUILDINGS = {
    "ИЭ": "Антоново",
    "ИГУМ": "Антоново",
    "ПТИ": "Б. Санкт-Петербургская, 41",
    "ХТИ": "ул. Советской Армии, 7",
    "ПИ": "ул. Псковская, 3",
}
_INDEX_TH_RE = re.compile(r"<th[^>]*>(.*?)</th>", re.S | re.I)
_INDEX_LINK_RE = re.compile(r"instId=(\d+)&(?:amp;)?name=([^&\"'<>]+)")
MAX_BYTES = 2 * 1024 * 1024
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"

_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?!\d)")
_TIME_RE = re.compile(r"(?<!\d)(\d{1,2})[.:](\d{2})(?!\d)")
_VG_RE = re.compile(r"(?<!\d)(\d{3})\s*ВГ", re.I)
_CANCEL_RE = re.compile(r"занят\w*\s+(?:не\s+будет|нет)|отмен", re.I)
_ROOM_RE = re.compile(r"ауд\.?\s*([^,;()]+)", re.I)
_NAME_RE = re.compile(r"^[А-ЯЁ][а-яё\-]+(?:\s+[А-ЯЁ][а-яё\-]+){1,3}$")

#: Корпуса для группировки таблицы: по адресу, а не по институту-владельцу
#: аудитории (ИЭ и ИГУМ сидят в одном Антоново).
_BUILDINGS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("антоново",), "Антоново"),
    (("санкт-петерб", "с.-петерб", "спб"), "Б. Санкт-Петербургская, 41"),
    (("сов. армии", "сов.армии", "советской армии"), "ул. Советской Армии, 7"),
    (("псковск",), "ул. Псковская, 3"),
    (("чудинцев",), "ул. Чудинцева, 6"),
)


# --------------------------------------------------------------------------
# HTML-таблицы Google Doc: rowspan/colspan → плоская сетка
# --------------------------------------------------------------------------

class _TableParser(html.parser.HTMLParser):
    """Собирает таблицы верхнего уровня HTML-экспорта Google Docs.

    Абзацы и переводы строк внутри ячейки разделяются ``\\n``: «101 ВГ» и
    «занятий не будет» в экспорте лежат в разных ``<p>``.
    """

    _BREAKS = {"p", "br", "div", "li", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[dict]]] = []
        self._depth = 0
        self._table: list[list[dict]] | None = None
        self._row: list[dict] | None = None
        self._cell: dict | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            if self._depth == 0:
                self._table = []
            self._depth += 1
            return
        if self._depth != 1:
            return
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            values = dict(attrs)
            self._cell = {
                "text": "",
                "rs": _span(values.get("rowspan")),
                "cs": _span(values.get("colspan")),
            }
        elif tag in self._BREAKS and self._cell is not None:
            self._cell["text"] += "\n"

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._depth > 0:
            self._depth -= 1
            if self._depth == 0 and self._table is not None:
                self.tables.append(self._table)
                self._table = None
            return
        if self._depth != 1:
            return
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if self._row:
                self._table.append(self._row)
            self._row = None
        elif tag in self._BREAKS and self._cell is not None:
            self._cell["text"] += "\n"

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell["text"] += data


def _span(value: str | None) -> int:
    try:
        return max(1, int(str(value or "1").strip()))
    except ValueError:
        return 1


def _clean_cell(text: str) -> str:
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in text.replace("\r", "").split("\n")]
    return "\n".join(line for line in lines if line)


def _expand(rows: list[list[dict]]) -> list[list[str]]:
    """Разворачивает rowspan/colspan: каждая ячейка получает свой индекс колонки."""
    grid: list[list[str]] = []
    future: dict[int, tuple[int, str]] = {}
    for row in rows:
        carry = {col: text for col, (left, text) in future.items() if left > 0}
        future = {col: (left - 1, text) for col, (left, text) in future.items() if left > 1}
        placed = dict(carry)
        col = 0
        for cell in row:
            while col in carry:
                col += 1
            text = _clean_cell(cell["text"])
            for offset in range(cell["cs"]):
                placed[col + offset] = text
                if cell["rs"] > 1:
                    future[col + offset] = (cell["rs"] - 1, text)
            col += cell["cs"]
        width = max(list(placed.keys()) + [-1]) + 1
        grid.append([placed.get(index, "") for index in range(width)])
    return grid


def parse_doc_tables(html_text: str) -> list[list[list[str]]]:
    parser = _TableParser()
    parser.feed(html_text)
    parser.close()
    return [_expand(table) for table in parser.tables]


# --------------------------------------------------------------------------
# Расписание ВГ: даты, блоки, преподаватели
# --------------------------------------------------------------------------

def _today_msk(today: dt.date | None = None) -> dt.date:
    return today or dt.datetime.now(MOSCOW).date()


def _date_with_year(day: int, month: int, year_text: str | None, today: dt.date) -> dt.date | None:
    """«10.09» без года: берём год, при котором дата ближе всего к сегодня."""
    if year_text:
        year = int(year_text)
        if year < 100:
            year += 2000
        candidates = [year]
    else:
        candidates = [today.year - 1, today.year, today.year + 1]
    best: dt.date | None = None
    for year in candidates:
        try:
            candidate = dt.date(year, month, day)
        except ValueError:
            continue
        if best is None or abs((candidate - today).days) < abs((best - today).days):
            best = candidate
    return best


def _cell_date(text: str, today: dt.date) -> dt.date | None:
    match = _DATE_RE.search(text or "")
    if not match:
        return None
    return _date_with_year(int(match.group(1)), int(match.group(2)), match.group(3), today)


def _is_date_header(row: list[str], today: dt.date) -> bool:
    first = (row[0] if row else "").casefold()
    if "дата" in first:
        return True
    if _VG_RE.search(first):
        return False
    return sum(1 for cell in row[1:] if _cell_date(cell, today)) >= 2


def _parse_date_header(row: list[str], today: dt.date) -> dict[int, dt.date]:
    dates: dict[int, dt.date] = {}
    for col, cell in enumerate(row):
        if col == 0:
            continue
        value = _cell_date(cell, today)
        if value:
            dates[col] = value
    return dates


def _repair_dates(dates: dict[int, dt.date], primary: dict[int, dt.date]) -> dict[int, dt.date]:
    """Опечатка в повторной шапке («24.10» между 17.09 и 01.10) ломает порядок дат;
    выпавшую из последовательности колонку берём из первой шапки того же столбца."""
    cols = sorted(dates)
    repaired = dict(dates)
    for index, col in enumerate(cols):
        value = dates[col]
        before = [dates[c] for c in cols[:index]]
        after = [dates[c] for c in cols[index + 1:]]
        outlier = (before and value < max(before)) or (after and value > min(after))
        if outlier and col in primary:
            repaired[col] = primary[col]
    return repaired


def _block_bounds(times: list[str]) -> tuple[str, str]:
    """«14:00 15:00» → (14:00, 15:00); одно время → часовой слот от него."""
    start = times[0]
    later = [value for value in times[1:] if value > start]
    if later:
        return start, later[0]
    hours, minutes = (int(part) for part in start.split(":"))
    end = dt.datetime(2000, 1, 1, hours, minutes) + dt.timedelta(minutes=BLOCK_MINUTES)
    return start, end.strftime("%H:%M")


def _parse_time_row(row: list[str]) -> dict[int, tuple[str, str]]:
    blocks: dict[int, tuple[str, str]] = {}
    for col, cell in enumerate(row):
        if col == 0:
            continue
        times = [f"{int(h):02d}:{m}" for h, m in _TIME_RE.findall(cell or "")]
        if times:
            blocks[col] = _block_bounds(times)
    return blocks


def _default_block(col: int, dates: dict[int, dt.date]) -> tuple[str, str]:
    same = sorted(c for c, value in dates.items() if value == dates[col])
    position = same.index(col) if col in same else 0
    return DEFAULT_BLOCKS[min(position, len(DEFAULT_BLOCKS) - 1)]


def building_of(place: str) -> str:
    low = (place or "").casefold()
    for needles, name in _BUILDINGS:
        if any(needle in low for needle in needles):
            return name
    return place.strip(" ,;") or "адрес уточняется"


def place_hint(place: str, building: str) -> str:
    """Что в «месте» не повторяет корпус: «ИЭ, Антоново» → «ИЭ»,
    «рядом с ХТИ, ул. Сов. Армии, 7» → «ХТИ» (аудитории «хк» — это здание ХТИ,
    формулировку документа «рядом с» не повторяем)."""
    needles = [needle for group, name in _BUILDINGS if name == building for needle in group]
    kept: list[str] = []
    for part in (place or "").split(","):
        part = re.sub(r"^рядом\s+с\s+", "", part.strip(" ;."), flags=re.I)
        low = part.casefold()
        if not part or part.isdigit() or any(needle in low for needle in needles):
            continue
        kept.append(part)
    return ", ".join(kept)


def _parse_teacher_cell(text: str) -> dict | None:
    """«Трезорова Ольга Юрьевна(ауд. 402, ИЭ, Антоново)» → преподаватель и место.

    Закрывающая скобка в документе иногда потеряна, а пробел перед скобкой —
    необязателен.
    """
    flat = re.sub(r"\s+", " ", (text or "").replace("\n", " ")).strip()
    if not flat or "(" not in flat and not _NAME_RE.match(flat):
        return None
    name, _, rest = flat.partition("(")
    name = name.strip(" ,;")
    if not _NAME_RE.match(name):
        return None
    location = rest.strip()
    if location.endswith(")"):
        location = location[:-1]
    location = location.strip(" ,;")
    room_match = _ROOM_RE.search(location)
    room = room_match.group(1).strip(" ,;.") if room_match else ""
    place = location
    if room_match:
        place = (location[:room_match.start()] + location[room_match.end():]).strip(" ,;")
    place = re.sub(r"\s*,\s*", ", ", place)
    return {
        "teacher": name,
        "room": room,
        "place": place,
        "building": building_of(place),
    }


_SHIFT_RE = re.compile(r"^с\s+(\d{1,2})[.:](\d{2})$", re.I)


def _apply_note_shift(start: str, end: str, note: str) -> tuple[str, str]:
    """Пометка «с 15:00» в ячейке ВГ: слот той же длины начинается позже."""
    match = _SHIFT_RE.match((note or "").strip())
    if not match:
        return start, end
    shifted = f"{int(match.group(1)):02d}:{match.group(2)}"
    try:
        old_start = dt.datetime.strptime(start, "%H:%M")
        old_end = dt.datetime.strptime(end, "%H:%M")
        new_start = dt.datetime.strptime(shifted, "%H:%M")
    except ValueError:
        return start, end
    duration = old_end - old_start if old_end > old_start else dt.timedelta(minutes=BLOCK_MINUTES)
    return shifted, (new_start + duration).strftime("%H:%M")


def _parse_vg_cell(text: str) -> list[tuple[str, str, bool]]:
    """Ячейка «118 ВГ» / «101 ВГ\\nзанятий не будет» / «121 ВГ\\nс 15:00»."""
    found = _VG_RE.findall(text or "")
    if not found:
        return []
    note = _VG_RE.sub(" ", text or "")
    note = re.sub(r"\s+", " ", note).strip(" ,;.\n")
    cancelled = bool(_CANCEL_RE.search(note))
    return [(vg, note, cancelled) for vg in found]


def parse_sessions(grids: list[list[list[str]]], *, today: dt.date | None = None) -> list[dict]:
    """Все занятия документа: дата, блок, ВГ, преподаватель, аудитория, адрес."""
    today = _today_msk(today)
    sessions: list[dict] = []
    for grid in grids:
        primary: dict[int, dt.date] | None = None
        dates: dict[int, dt.date] = {}
        blocks: dict[int, tuple[str, str]] = {}
        for row in grid:
            if not row:
                continue
            first = row[0]
            if _is_date_header(row, today):
                parsed = _parse_date_header(row, today)
                if not parsed:
                    continue
                if primary is None:
                    primary = dict(parsed)
                    dates = parsed
                else:
                    dates = _repair_dates(parsed, primary)
                blocks = {}
                continue
            if first.casefold().startswith("время"):
                blocks = _parse_time_row(row)
                continue
            teacher = _parse_teacher_cell(first)
            if teacher is None or not dates:
                continue
            for col, cell in enumerate(row):
                if col == 0 or col not in dates:
                    continue
                for vg, note, cancelled in _parse_vg_cell(cell):
                    start, end = blocks.get(col) or _default_block(col, dates)
                    if not cancelled:
                        start, end = _apply_note_shift(start, end, note)
                    sessions.append({
                        "date": dates[col].isoformat(),
                        "block_start": start,
                        "block_end": end,
                        "vg": vg,
                        "teacher": teacher["teacher"],
                        "room": teacher["room"],
                        "place": teacher["place"],
                        "building": teacher["building"],
                        "cancelled": cancelled,
                        "note": "" if cancelled else note,
                    })
    return sessions


# --------------------------------------------------------------------------
# Таблица ВГ (Google Sheets, CSV)
# --------------------------------------------------------------------------

def _title_case(value: str) -> str:
    value = (value or "").strip()
    if value in ("", ".", "-", "—"):
        return ""
    return "-".join(part[:1].upper() + part[1:].lower() for part in value.split("-"))


def _group_code(academic: str) -> str:
    match = re.search(r"\d{4}", academic or "")
    return match.group(0) if match else (academic or "").strip()


def parse_members(csv_text: str) -> list[dict]:
    """Строки «ВГ, Фамилия, Имя, Отчество, Группа, …» → список студентов."""
    members: list[dict] = []
    for raw in csv.reader(io.StringIO(csv_text)):
        cells = [cell.strip() for cell in raw]
        if len(cells) < 5:
            continue
        vg, last, first, patronymic, academic = cells[:5]
        if not re.fullmatch(r"\d{3}", vg) or not last:
            continue
        members.append({
            "vg": vg,
            "last_name": _title_case(last),
            "first_name": _title_case(first),
            "patronymic": _title_case(patronymic),
            "group": _group_code(academic),
            "academic_group": academic,
        })
    return members


def group_members(members: list[dict], group: str) -> list[dict]:
    return [member for member in members if member.get("group") == str(group)]


def parse_index_institutes(index_html: str) -> dict[str, dict]:
    """Номер группы → институт по индексу портала.

    Страница идёт блоками: ``<th>ИЭ</th>``, дальше ссылки групп с ``instId``.
    Ссылка относится к последнему заголовку перед ней. Специальности на
    портале нет, поэтому дальше института не идём.
    """
    if not index_html:
        return {}
    headings = [
        (m.start(), " ".join(html.unescape(re.sub(r"<[^>]+>", " ", m.group(1))).split()))
        for m in _INDEX_TH_RE.finditer(index_html)
    ]
    headings = [(pos, name) for pos, name in headings if re.fullmatch(r"[А-ЯЁ]{2,6}", name)]
    result: dict[str, dict] = {}
    for m in _INDEX_LINK_RE.finditer(index_html):
        inst_id, name = m.group(1), html.unescape(m.group(2)).strip()
        short = next((title for pos, title in reversed(headings) if pos < m.start()), "")
        code = _group_code(name)
        if code and short and code not in result:
            result[code] = {"inst_id": inst_id, "short": short}
    return result


# --------------------------------------------------------------------------
# Сборка данных и кэш
# --------------------------------------------------------------------------

def build_data(
    members_csv: str,
    doc_html: str,
    *,
    group: str | None = None,
    today: dt.date | None = None,
    fetched_at: str | None = None,
    index_html: str | None = None,
) -> dict:
    group = str(group or config.DEFAULT_GROUP)
    sessions = parse_sessions(parse_doc_tables(doc_html), today=today)
    everyone = parse_members(members_csv)
    members = group_members(everyone, group)
    if not sessions:
        raise ValueError("opd document has no sessions")
    if not members:
        raise ValueError(f"opd sheet has no members of group {group}")
    # Состав своей ВГ: кто из других групп ходит вместе, из какого института.
    institutes = parse_index_institutes(index_html or "")
    my_vgs = {member["vg"] for member in members}
    mates = [
        {
            "vg": member["vg"],
            "last_name": member["last_name"],
            "first_name": member["first_name"],
            "patronymic": member["patronymic"],
            "group": member["group"],
            "institute": institutes.get(member["group"], {}).get("short", ""),
        }
        for member in everyone
        if member["vg"] in my_vgs and member["group"] != group
    ]
    return {
        "version": CACHE_VERSION,
        "group": group,
        "fetched_at": fetched_at or dt.datetime.now(MOSCOW).isoformat(timespec="seconds"),
        "members": members,
        "sessions": sessions,
        "mates": mates,
        "institutes_known": bool(institutes),
        "digest": _digest({"members": members, "sessions": sessions, "mates": mates}),
        "sources": {
            "sheet": config.OPD_SHEET_VIEW_URL,
            "doc": config.OPD_DOC_VIEW_URL,
            "announcement": config.OPD_ANNOUNCEMENT_URL,
        },
    }


def _digest(payload: object) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _download(url: str) -> str:
    out = subprocess.run(
        ["curl", "-sSL", "--fail", "--max-time", "45", "--max-filesize", str(MAX_BYTES),
         "-A", USER_AGENT, url],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    text = out.stdout
    if not text.strip():
        raise ValueError("empty response")
    return text


def _cache_path() -> Path:
    return config.STATE_DIR / CACHE_FILE


def _read_cache() -> dict:
    try:
        loaded = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_cache(payload: dict) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass


def _parse_ts(value: object) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=MOSCOW)


def _has_data(cache: dict) -> bool:
    return bool(cache.get("members")) and bool(cache.get("sessions")) and cache.get("version") == CACHE_VERSION


def _fetch_index_html() -> str:
    """Индекс портала для институтов одногруппников по ВГ. Необязателен:
    без него состав ВГ показывается с номерами групп, но без институтов."""
    try:
        import fetch  # локальный импорт: fetch тянет портальный парсер

        return fetch.fetch_html(config.get_route_index_url("ochn"), page_kind="index")
    except Exception:  # noqa: BLE001 — портал медленный или лежит: обойдёмся без институтов
        return ""


def refresh(now: dt.datetime | None = None, *, group: str | None = None) -> dict:
    """Скачать оба файла и собрать данные. Исключения — наружу (для CLI)."""
    now = now or dt.datetime.now(MOSCOW)
    members_csv = _download(config.OPD_SHEET_CSV_URL)
    doc_html = _download(config.OPD_DOC_HTML_URL)
    return build_data(
        members_csv,
        doc_html,
        group=group,
        today=now.astimezone(MOSCOW).date(),
        fetched_at=now.astimezone(MOSCOW).isoformat(timespec="seconds"),
        index_html=_fetch_index_html(),
    )


def load_opd(now: dt.datetime | None = None, *, group: str | None = None, allow_fetch: bool = True) -> dict | None:
    """Данные ОПД из кэша ``state/opd_cache.json`` с обновлением по TTL.

    Свежий кэш отдаётся без сети. Просроченный — обновляется; если Google
    недоступен, остаётся прошлый кэш, а следующая попытка откладывается на
    ``OPD_RETRY_DELAY_S``, чтобы не дёргать сеть каждый трёхминутный цикл.
    """
    if not config.OPD_ENABLED:
        return None
    now = now or dt.datetime.now(MOSCOW)
    group = str(group or config.DEFAULT_GROUP)
    cache = _read_cache()
    if cache and cache.get("version") != CACHE_VERSION:
        cache = {}  # старый формат кэша не считается ни данными, ни недавней попыткой
    usable = _has_data(cache) and cache.get("group") == group
    fetched = _parse_ts(cache.get("fetched_at")) if usable else None
    if fetched is not None and (now - fetched).total_seconds() < config.OPD_CACHE_TTL_S:
        return cache
    if not allow_fetch:
        return cache if usable else None
    attempted = _parse_ts(cache.get("last_attempt"))
    if attempted is not None and (now - attempted).total_seconds() < config.OPD_RETRY_DELAY_S:
        return cache if usable else None
    try:
        fresh = refresh(now, group=group)
    except Exception as exc:  # noqa: BLE001 — сеть, curl, разбор: все одинаково не фатальны
        cache["last_attempt"] = now.isoformat(timespec="seconds")
        cache["last_error"] = f"{type(exc).__name__}: {exc}"[:500]
        _write_cache(cache)
        return cache if usable else None
    fresh["last_attempt"] = now.isoformat(timespec="seconds")
    fresh["last_error"] = ""
    _write_cache(fresh)
    return fresh


# --------------------------------------------------------------------------
# Представление дня для группы
# --------------------------------------------------------------------------

def _initials(member: dict) -> str:
    parts = [member.get("first_name", ""), member.get("patronymic", "")]
    return " ".join(f"{part[0]}." for part in parts if part)


def student_label(member: dict) -> str:
    initials = _initials(member)
    return f"{member['last_name']} {initials}".strip()


def teacher_short(name: str) -> str:
    parts = (name or "").split()
    if len(parts) < 2:
        return name or ""
    return parts[0] + " " + " ".join(f"{part[0]}." for part in parts[1:3])


def mates_of(opd: dict | None, vg: str) -> list[dict]:
    """Одногруппники по ВГ из других академических групп: институт, потом фамилия."""
    if not opd:
        return []
    rows = [
        {
            "student": student_label(mate),
            "group": mate.get("group", ""),
            "institute": mate.get("institute", ""),
        }
        for mate in opd.get("mates") or []
        if str(mate.get("vg")) == str(vg)
    ]
    return sorted(rows, key=lambda row: (row["institute"] == "", row["institute"], row["student"].casefold()))


def day_view(opd: dict | None, date: dt.date) -> dict | None:
    """Кто из группы идёт в этот день, во сколько и куда. None — даты нет в документе."""
    if not opd or not _has_data(opd):
        return None
    date_iso = date.isoformat()
    by_vg: dict[str, list[dict]] = {}
    upcoming_by_vg: dict[str, list[str]] = {}
    for session in opd["sessions"]:
        vg = str(session.get("vg"))
        session_date = str(session.get("date") or "")
        if session_date == date_iso:
            by_vg.setdefault(vg, []).append(session)
        elif session_date > date_iso and not session.get("cancelled"):
            upcoming_by_vg.setdefault(vg, []).append(session_date)
    if not by_vg:
        return None
    rows: list[dict] = []
    members = sorted(
        opd["members"],
        key=lambda member: (member["last_name"].casefold(), member["first_name"].casefold()),
    )
    for member in members:
        if not _member_visible(opd, member, date):
            continue
        items = by_vg.get(member["vg"], [])
        active = sorted((s for s in items if not s.get("cancelled")), key=lambda s: s["block_start"])
        if active:
            session, status = active[0], "session"
        elif items:
            session, status = items[0], "cancelled"
        else:
            session, status = None, "free"
        # ВГ ходят через неделю: половина группы в один четверг, половина в
        # следующий. Дата следующего занятия нужна и тем, кто сегодня свободен.
        upcoming = sorted(upcoming_by_vg.get(member["vg"], []))
        row = {
            "student": student_label(member),
            "last_name": member["last_name"],
            "vg": member["vg"],
            "status": status,
            "next_date": upcoming[0] if upcoming else "",
            "mates": mates_of(opd, member["vg"]),
        }
        if session is not None:
            row.update({
                "block_start": session["block_start"],
                "block_end": session["block_end"],
                "room": session["room"],
                "place": session["place"],
                "building": session["building"],
                "teacher": session["teacher"],
                "teacher_short": teacher_short(session["teacher"]),
                "note": session.get("note", ""),
            })
        rows.append(row)
    counts = {status: sum(1 for row in rows if row["status"] == status) for status in ("session", "cancelled", "free")}
    return {
        "date": date_iso,
        "group": opd.get("group"),
        "rows": rows,
        "counts": counts,
        "fetched_at": opd.get("fetched_at", ""),
        "sources": opd.get("sources", {}),
    }


def late_ends(opd: dict | None) -> dict[dt.date, dt.time]:
    """Конец последнего блока ОПД по датам — для продления «живого» дня в закрепе."""
    if not opd or not _has_data(opd):
        return {}
    vgs = {member["vg"] for member in opd["members"]}
    ends: dict[dt.date, dt.time] = {}
    for session in opd["sessions"]:
        if session.get("cancelled") or str(session.get("vg")) not in vgs:
            continue
        try:
            date = dt.date.fromisoformat(session["date"])
            end = dt.time.fromisoformat(session["block_end"])
        except (KeyError, ValueError):
            continue
        if not any(
            str(member["vg"]) == str(session.get("vg")) and _member_visible(opd, member, date)
            for member in opd["members"]
        ):
            continue
        if date not in ends or end > ends[date]:
            ends[date] = end
    return ends


def presentation_digest(opd: dict | None, dates: list[dt.date]) -> str:
    """Отпечаток того, что раздел ОПД покажет за эти даты: меняется — закреп перерисовывается."""
    if not opd or not _has_data(opd):
        return ""
    views = [day_view(opd, date) for date in dates]
    payload = [
        {"date": view["date"], "rows": view["rows"]} if view else None
        for view in views
    ]
    if not any(payload):
        return ""
    return _digest(payload)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="ОПД: виртуальные группы — кто, когда и куда")
    ap.add_argument("--date", help="дата YYYY-MM-DD (по умолчанию ближайший четверг)")
    ap.add_argument("--refresh", action="store_true", help="скачать файлы заново, игнорируя TTL кэша")
    ap.add_argument("--json", action="store_true", help="вывести представление дня как JSON")
    args = ap.parse_args()

    if args.date:
        focus = dt.date.fromisoformat(args.date)
    else:
        focus = dt.datetime.now(MOSCOW).date()
        focus += dt.timedelta(days=(3 - focus.weekday()) % 7)
    if args.refresh:
        data = refresh()
        data["last_attempt"] = data["fetched_at"]
        data["last_error"] = ""
        _write_cache(data)
    else:
        data = load_opd()
    if not data:
        raise SystemExit("нет данных ОПД: кэш пуст и загрузка не удалась")
    view = day_view(data, focus)
    if args.json:
        print(json.dumps(view, ensure_ascii=False, indent=1))
        raise SystemExit(0)
    print(f"ОПД группы {data['group']} · {focus:%d.%m.%Y} · данные от {data.get('fetched_at', '?')}")
    if not view:
        print("в документе нет такой даты")
        raise SystemExit(0)
    for row in view["rows"]:
        nxt = f"  далее {row['next_date'][8:10]}.{row['next_date'][5:7]}" if row.get("next_date") else "  дат больше нет"
        if row["status"] == "session":
            extra = f" · {row['note']}" if row.get("note") else ""
            print(f"  {row['student']:<28} ВГ {row['vg']}  {row['block_start']}–{row['block_end']}  "
                  f"ауд. {row['room']} · {row['place']} · {row['teacher_short']}{extra}{nxt}")
        elif row["status"] == "cancelled":
            print(f"  {row['student']:<28} ВГ {row['vg']}  занятий не будет{nxt}")
        else:
            print(f"  {row['student']:<28} ВГ {row['vg']}  —{nxt}")
    counts = view["counts"]
    print(f"идут: {counts['session']} · отменено: {counts['cancelled']} · без занятия: {counts['free']}")
