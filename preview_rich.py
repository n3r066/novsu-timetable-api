"""Preview ОДНИМ rich message через Bot API 10.2 sendRichMessage.

Реальные названия предметов по ФГОС 43.03.02 "Туризм". Время/ауд./преподаватели —
моки (реального архива 5234 на портале сейчас нет, см. memory).

Эмпирически подобранные типы блоков Bot API 10.2 (14.08.2026):
  ✅ paragraph, heading (size 1-5), divider, footer, details, pre, table
  ✅ pullquote (большая красивая цитата)
  ❌ pull_quotation, block_quotation, quote, list, image, photo, video, audio,
       sticker, emoji, custom_emoji, anchor, subheader, subheading, card, code

Стили ячеек таблицы:
  шапка          → bold
  № (1. 2. 3.)   → code       (monospace)
  предмет        → marked     (выделенный фон)
  время          → code       (monospace)
  ауд.           → code
  преподаватель  → bold

Запуск:
  python3 preview_rich.py              # печатает JSON payload
  python3 preview_rich.py --send       # шлёт в канал -1004256784811
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request

_ENV_FILE = os.path.dirname(os.path.abspath(__file__)) + "/.env"
if os.path.exists(_ENV_FILE):
    for line in open(_ENV_FILE, encoding="utf-8").read().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

TOKEN = os.environ.get("TG_BOT_TOKEN", "")
if not TOKEN or TOKEN == "***":
    raise SystemExit("TG_BOT_TOKEN пуст или плейсхолдер. Положи реальный токен в .env")

CHANNEL_ID = int(os.environ.get("TG_CHANNEL_ID", "-1004256784811"))


# ============================================================================
# ДЕМО-РАСПИСАНИЕ: реалистичные полные названия предметов по ФГОС 43.03.02
# "Туризм" (бакалавриат, 1 курс, Институт экономики НовГУ).
# ============================================================================
DEMO_SCHEDULE = {
    "Понедельник": [
        ["1.", "Менеджмент туризма и гостеприимства", "08:30-10:00", "ауд. 312", "Иванов И.И."],
        ["2.", "Маркетинг туристских территорий", "10:10-11:40", "ауд. 415", "Петрова А.С."],
        ["3.", "Экономика туристского рынка", "11:50-13:20", "ауд. 207", "Сидоров В.П."],
        ["4.", "Иностранный язык (профессиональный)", "13:50-15:20", "ауд. 308", "Кузнецова Е.Н."],
    ],
    "Вторник": [
        ["1.", "География и климатические ресурсы туризма", "08:30-10:00", "ауд. 210", "Морозов Д.А."],
        ["2.", "Маркетинг туристских территорий", "10:10-11:40", "ауд. 415", "Петрова А.С."],
        ["3.", "Маркетинг туристских территорий (семинар)", "11:50-13:20", "ауд. 415", "Петрова А.С."],
        ["4.", "История России", "13:50-15:20", "ауд. 112", "Васильев К.Л."],
    ],
    "Среда": [
        ["1.", "Маркетинг туристских территорий", "08:30-10:00", "ауд. 415", "Петрова А.С."],
        ["2.", "Экономика туристского рынка", "10:10-11:40", "ауд. 308", "Кузнецова Е.Н."],
        ["3.", "Физическая культура и спорт", "11:50-13:20", "ауд. 217", "Николаев Г.Р."],
    ],
    "Четверг": [
        ["1.", "История России", "08:30-10:00", "ауд. 112", "Васильев К.Л."],
        ["2.", "Физическая культура и спорт", "10:10-11:40", "ауд. 217", "Николаев Г.Р."],
        ["3.", "Маркетинг туристских территорий", "11:50-13:20", "ауд. 415", "Петрова А.С."],
        ["4.", "Менеджмент туризма и гостеприимства", "13:50-15:20", "ауд. 312", "Иванов И.И."],
    ],
    "Пятница": [
        ["1.", "Экономика туристского рынка", "08:30-10:00", "ауд. 308", "Кузнецова Е.Н."],
        ["2.", "Маркетинг туристских территорий", "10:10-11:40", "ауд. 415", "Петрова А.С."],
    ],
}

DAYS_ORDER = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
SOURCE_URL = os.environ.get(
    "GROUP_URL",
    "https://portal.novsu.ru/univer/timetable/ochn/i.1103357/"
    "?page=EditViewGroup&name=6381&type=%D0%92%D0%9E&year=2026&instId=1786977",
)


# ============================================================================
# RichText helpers
# ============================================================================
def rt_bold(text: str) -> dict:
    return {"type": "bold", "text": text}


def rt_code(text: str) -> dict:
    return {"type": "code", "text": text}


def rt_marked(text: str) -> dict:
    return {"type": "marked", "text": text}


def rt_spoiler(text: str) -> dict:
    return {"type": "spoiler", "text": text}


_STYLE_GETTER = {
    "bold": rt_bold,
    "code": rt_code,
    "marked": rt_marked,
    "spoiler": rt_spoiler,
}


def _apply_style(text: str, style: str) -> dict:
    fn = _STYLE_GETTER.get(style, rt_marked)
    return fn(text)


def table_cell(rich_text: list) -> dict:
    return {"type": "tableCell", "text": rich_text}


def table_native(headers_with_style: list, rows: list) -> dict:
    cells = []
    cells.append([table_cell([_apply_style(h, s)]) for h, s in headers_with_style])
    for row in rows:
        cells.append([table_cell([_apply_style(c, s)]) for c, s in row])
    return {"type": "table", "cells": cells, "is_bordered": True, "is_striped": True}


# ============================================================================
# Стандартные блоки
# ============================================================================
def heading(text: str, size: int = 2) -> dict:
    return {"type": "heading", "size": size, "text": text}


def paragraph(text: str) -> dict:
    return {"type": "paragraph", "text": text}


def divider() -> dict:
    return {"type": "divider"}


def footer(text: str) -> dict:
    return {"type": "footer", "text": text}


def details_block(header: str, *blocks) -> dict:
    return {"type": "details", "header": header, "open": False, "blocks": list(blocks)}


def pullquote(text: str) -> dict:
    """InputRichBlockPullQuotation — большая красивая цитата (✅ в API 10.2)."""
    return {"type": "pullquote", "text": text}


# ============================================================================
# Сборка поста
# ============================================================================
def build_rich_message(is_demo: bool = True) -> dict:
    blocks = []

    # 1. Pull-quotation — большая красивая преамбула (✅ работает)
    blocks.append(pullquote(
        "📅 Расписание группы 6381\n\n"
        "Неделя 1 (верхняя)\n"
        "01.09.2026 — 05.09.2026"
    ))

    # 2. Демо-метка
    if is_demo:
        blocks.append(paragraph(
            "🧪 демо формата — названия предметов по ФГОС 43.03.02 Туризм, "
            "время/ауд./преподаватели — моки"
        ))

    # 3. Источник
    blocks.append(paragraph(f"источник: {SOURCE_URL}"))
    blocks.append(divider())

    # 4. Детальные блоки по дням (каждый в InputRichBlockDetails — свёрнутый)
    for day in DAYS_ORDER:
        if day not in DEMO_SCHEDULE:
            continue
        pairs = DEMO_SCHEDULE[day]
        total = len(pairs)
        rows_with_style = [
            [
                (row[0], "code"),       # №
                (row[1], "marked"),     # предмет
                (row[2], "code"),       # время
                (row[3], "code"),       # ауд.
                (row[4], "bold"),       # препод
            ]
            for row in pairs
        ]
        headers_with_style = [
            ("№", "bold"),
            ("предмет", "bold"),
            ("время", "bold"),
            ("ауд.", "bold"),
            ("преподаватель", "bold"),
        ]
        blocks.append(
            details_block(
                f"📆 {day} — {total} пар(ы)",
                table_native(headers_with_style, rows_with_style),
            )
        )

    # 5. Заметка про мониторинг
    blocks.append(divider())
    blocks.append(pullquote(
        "🔔 пост автообновляется когда меняется расписание на портале новгу"
    ))

    # 6. Footer
    blocks.append(footer(f"источник · portal.novsu.ru\n{SOURCE_URL}"))

    return {"rich_message": {"blocks": blocks}}


def send(rich_message: dict) -> dict:
    url = f"https://api.telegram.org/bot{TOKEN}/sendRichMessage"
    body = {"chat_id": CHANNEL_ID, **rich_message}
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"ok": False, "error_code": e.code, "description": e.read().decode(errors="replace")[:600]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="отправить в канал")
    args = ap.parse_args()

    rm = build_rich_message(is_demo=True)
    print("=== payload size ===")
    print(len(json.dumps(rm, ensure_ascii=False)), "bytes")

    if args.send:
        print("\n=== sendRichMessage ===")
        r = send(rm)
        print(json.dumps(r, ensure_ascii=False, indent=2)[:2000])
