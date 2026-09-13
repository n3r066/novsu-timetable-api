"""Модель учебного времени НовГУ: академические часы → реальные интервалы.

Портал печатает в колонке «время» НАЧАЛА академических часов, а не интервал
занятия. «9:00 10:00» — это два академических часа по 45 минут, то есть одна
пара 9:00–10:45. Этот модуль — единственное место в проекте, где живёт эта
арифметика; все остальные модули только форматируют его результат.

Нормативная база:

* легенда над таблицей расписания на портале: «Один академический час - 45 минут»;
* Регламент составления расписания занятий (ВО, 16.12.2021), п. 2.3:
  академический час 45 минут, перерыв между занятиями не менее 15 минут,
  занятия с 08.00 до 22.00, не более 4 академических часов в день по одной
  дисциплине;
* Положение об организации ОД по программам ВО (СМК УД 3.1.-00.02.56-22),
  п. 27: занятия продолжительностью не более 90 минут.

Отсюда блок из N подряд идущих академических часов, начинающийся в S:

* с внутренними перерывами по 15 минут — заканчивается в S + N*45 + (N-1)*15;
* без внутренних перерывов            — заканчивается в S + N*45.

Портал не сообщает, делает ли преподаватель перерыв, поэтому оба значения
равноправны: ``end`` — позднейший (официальная сетка), ``end_min`` — ранний
(пара «в одну ленту»). Гарантированно занятое время — до ``end_min``.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

#: Академический час.
ACADEMIC_HOUR = dt.timedelta(minutes=45)
#: Перерыв ВНУТРИ пары, между её двумя академическими часами (Регламент, п. 2.3).
BREAK = dt.timedelta(minutes=15)
#: Перерыв МЕЖДУ парами: пара кончается в 10:45, следующая начинается в 11:00,
#: но по факту между звонками полчаса — токены портала идут с шагом час.
BREAK_BETWEEN_PAIRS = dt.timedelta(minutes=30)
#: Шаг, с которым портал печатает начала академических часов: ровно час
#: (45 минут занятия + 15 минут перерыва).
HOUR_STEP = ACADEMIC_HOUR + BREAK
#: Максимум академических часов в одной паре.
HOURS_PER_PAIR = 2

EARLIEST = dt.time(8, 0)
LATEST = dt.time(22, 0)

_HOUR_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)")
_DASH = "–"


def _to_delta(value: dt.time) -> dt.timedelta:
    return dt.timedelta(hours=value.hour, minutes=value.minute)


def _to_time(value: dt.timedelta) -> dt.time:
    total = int(value.total_seconds() // 60) % (24 * 60)
    return dt.time(total // 60, total % 60)


def _hhmm(value: dt.time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


def _pairs_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "пара"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "пары"
    return "пар"


def hhmm(value: dt.time | None) -> str:
    """«09:05» — машинно-читаемое время (для API); пусто, если время неизвестно."""
    return _hhmm(value) if value is not None else ""


def hour_tokens(cell: object) -> list[str]:
    """Начала академических часов так, как их напечатал портал.

    Строже наивного ``r"\\d{1,2}:\\d{2}"``: «119:00» не превращается в «19:00»,
    «25:99» отбрасывается целиком. Дубли схлопываются, порядок приводится к
    хронологическому, но написание токена сохраняется — это поле ``time`` в
    распарсенном уроке, по нему считаются диффы и отпечаток расписания.
    """
    text = "" if cell is None else str(cell)
    first_spelling: dict[dt.time, str] = {}
    for match in _HOUR_RE.finditer(text):
        hour, minute = int(match.group(1)), int(match.group(2))
        first_spelling.setdefault(dt.time(hour, minute), match.group(0))
    return [first_spelling[key] for key in sorted(first_spelling)]


def parse_hours(cell: object) -> list[dt.time]:
    """Достать начала академических часов из ячейки «время».

    Возвращает отсортированный список без дублей. Мусор («—», пустая строка,
    «25:99») даёт пустой список: невалидное время не должно молча превращаться
    в валидное.
    """
    text = "" if cell is None else str(cell)
    found = {dt.time(int(h), int(m)) for h, m in _HOUR_RE.findall(text)}
    return sorted(found)


@dataclass(frozen=True)
class Block:
    """Непрерывный блок академических часов одного занятия."""

    start: dt.time
    hours: int

    @property
    def pairs(self) -> int:
        """Сколько пар в блоке (последний одиночный час считается парой)."""
        return -(-self.hours // HOURS_PER_PAIR)

    @property
    def end(self) -> dt.time:
        """Конец по сетке портала: с внутренним перерывом 15 минут.

        «9:00 10:00» — два академических часа, между ними перерыв:
        45 + 15 + 45 = 1 ч 45 мин, то есть 09:00–10:45.
        """
        span = ACADEMIC_HOUR * self.hours + BREAK * (self.hours - 1)
        return _to_time(_to_delta(self.start) + span)

    @property
    def end_min(self) -> dt.time:
        """Конец без внутреннего перерыва: пара «в одну ленту», 09:00–10:30."""
        return _to_time(_to_delta(self.start) + ACADEMIC_HOUR * self.hours)

    @property
    def next_start(self) -> dt.time:
        """Начало следующей пары: после конца ещё 15 минут, итого полчаса от 10:30."""
        return _to_time(_to_delta(self.end) + BREAK)

    @property
    def net(self) -> dt.timedelta:
        """Чистое учебное время блока."""
        return ACADEMIC_HOUR * self.hours

    def split(self) -> list["Block"]:
        """Разбить блок на пары по два академических часа."""
        return [
            Block(_to_time(_to_delta(self.start) + HOUR_STEP * offset),
                  min(HOURS_PER_PAIR, self.hours - offset))
            for offset in range(0, self.hours, HOURS_PER_PAIR)
        ]

    @property
    def label(self) -> str:
        """«09:00–10:45» — интервал по сетке портала."""
        return f"{_hhmm(self.start)}{_DASH}{_hhmm(self.end)}"

    @property
    def structure(self) -> str:
        """«45 + 15 перерыв + 45» — из чего сложен блок, минутами."""
        if self.hours == HOURS_PER_PAIR:
            return "45 + 15 перерыв + 45"
        return f"{self.hours} ак. ч."

    @property
    def structure_short(self) -> str:
        """Пометка объёма для нестандартного блока: пусто для обычной пары."""
        return "" if self.hours == HOURS_PER_PAIR else self.structure

    @property
    def label_full(self) -> str:
        """«09:00–10:45 (45 + 15 перерыв + 45)» — интервал со структурой."""
        return f"{self.label} ({self.structure})"

    @property
    def label_cell(self) -> str:
        """Короткий интервал для ячейки таблицы без технических пояснений."""
        return self.label

    @property
    def label_both(self) -> str:
        """«09:00–10:30/10:45» — оба варианта окончания."""
        if self.end_min == self.end:
            return self.label
        return f"{_hhmm(self.start)}{_DASH}{_hhmm(self.end_min)}/{_hhmm(self.end)}"

    def end_datetime(self, date: dt.date, *, strict: bool = False) -> dt.datetime:
        """Момент окончания блока. ``strict`` — вариант без перерывов."""
        return dt.datetime.combine(date, self.end_min if strict else self.end)


def blocks(cell: object) -> list[Block]:
    """Сгруппировать часы ячейки в непрерывные блоки.

    Часы считаются непрерывными, если следующий начинается ровно через час.
    «14:00 15:00 16:00 17:00» → один блок из 4 часов (две пары подряд),
    «9:00 11:00» → два блока по одному часу.
    """
    hours = parse_hours(cell)
    if not hours:
        return []
    result: list[Block] = []
    start, count = hours[0], 1
    for previous, current in zip(hours, hours[1:]):
        if _to_delta(current) - _to_delta(previous) == HOUR_STEP:
            count += 1
            continue
        result.append(Block(start, count))
        start, count = current, 1
    result.append(Block(start, count))
    return result


@dataclass(frozen=True)
class Slot:
    """Разобранная ячейка «время» целиком."""

    raw: str
    blocks: tuple[Block, ...]

    @property
    def ok(self) -> bool:
        return bool(self.blocks)

    @property
    def hours(self) -> int:
        return sum(block.hours for block in self.blocks)

    @property
    def pairs(self) -> int:
        return sum(block.pairs for block in self.blocks)

    @property
    def start(self) -> dt.time | None:
        return self.blocks[0].start if self.blocks else None

    @property
    def end(self) -> dt.time | None:
        return self.blocks[-1].end if self.blocks else None

    @property
    def end_min(self) -> dt.time | None:
        return self.blocks[-1].end_min if self.blocks else None

    def pair_blocks(self) -> list[Block]:
        """Все пары ячейки по отдельности (4 ак. ч. → две пары)."""
        return [pair for block in self.blocks for pair in block.split()]

    def _render(self, pick) -> str:
        """Собрать подпись: пары одного блока через « + », блоки через « · »."""
        if not self.blocks:
            # Мусор с портала не теряем: показываем ячейку как есть.
            return self.raw.strip() or "—"
        return " · ".join(
            " + ".join(pick(pair) for pair in block.split()) for block in self.blocks
        )

    def label(self, *, both: bool = False) -> str:
        """«09:00–10:45», «14:00–15:45 + 16:00–17:45»."""
        return self._render(lambda pair: pair.label_both if both else pair.label)

    def label_full(self) -> str:
        """«09:00–10:45 (45 + 15 перерыв + 45)» — для текстовых строк и API."""
        return self._render(lambda pair: pair.label_full)

    def label_cell(self) -> str:
        """«14:00–15:45\n16:00–17:45» — пары столбиком для ячейки таблицы."""
        if not self.blocks:
            return self.raw.strip() or "—"
        return "\n".join(pair.label_cell for pair in self.pair_blocks())

    @property
    def hours_note(self) -> str:
        """Пометка про объём: пустая для обычной пары из двух часов."""
        if not self.blocks or (len(self.blocks) == 1 and self.hours == HOURS_PER_PAIR):
            return ""
        return f"{self.hours} ак. ч."

    def sort_key(self) -> tuple[int, int]:
        """Ключ сортировки: невалидное время уходит в конец дня."""
        start = self.start
        return (start.hour * 60 + start.minute) if start else 24 * 60, self.hours

def slot(cell: object) -> Slot:
    """Разобрать ячейку «время» портала."""
    raw = "" if cell is None else str(cell)
    return Slot(raw=raw, blocks=tuple(blocks(raw)))


def label(cell: object, *, both: bool = False) -> str:
    """Короткий путь: ячейка → подпись «09:00–10:45»."""
    return slot(cell).label(both=both)


#: Пояснение для легенды поста: почему конец пары «плавает».
LEGEND = (
    "Академический час — 45 минут, пара — два часа: 45 + 15 минут перерыва + 45, "
    "то есть «9:00 10:00» на портале — это пара 09:00–10:45. Если преподаватель "
    "ведёт пару без перерыва, она кончается в 10:30. Между парами перерыв "
    "полчаса: следующая начинается в 11:00."
)

#: Короткая подпись структуры пары для легенды дня.
PAIR_STRUCTURE = "45 + 15 перерыв + 45"
