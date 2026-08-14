"""Демо-пост: как сообщения будут выглядеть когда появится реальное расписание.

Bot API не поддерживает <details>, поэтому формат = серия отдельных сообщений:
  msg 0: преамбула (неделя, диапазон дат, ссылка на источник)
  msg 1: Понедельник
  msg 2: Вторник
  ...
В канале это читается естественно: каждый день — отдельный пост, в ленте
видно список "Пн/Вт/Ср/Чт/Пт", открываешь нужный — там таблица.

Запуск:
  python3 preview_demo.py            # печатает preview в stdout
  python3 preview_demo.py --post     # шлёт в канал -1004256784811
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.parse
import urllib.request

import config  # noqa: E402

# Демо-структура: дни × пары (без предметов — только время, ауд., преподаватель)
DEMO_SCHEDULE = {
    "Понедельник": [
        ["1", "08:30-10:00", "ауд. 312", "Иванов И.И."],
        ["2", "10:10-11:40", "ауд. 415", "Петрова А.С."],
        ["3", "11:50-13:20", "ауд. 207", "Сидоров В.П."],
        ["4", "13:50-15:20", "ауд. 308", "Кузнецова Е.Н."],
    ],
    "Вторник": [
        ["1", "08:30-10:00", "ауд. 210", "Морозов Д.А."],
        ["2", "10:10-11:40", "ауд. 415", "Петрова А.С."],
        ["3", "11:50-13:20", "ауд. 415", "Петрова А.С."],
        ["4", "13:50-15:20", "ауд. 112", "Васильев К.Л."],
    ],
    "Среда": [
        ["1", "08:30-10:00", "ауд. 415", "Петрова А.С."],
        ["2", "10:10-11:40", "ауд. 308", "Кузнецова Е.Н."],
        ["3", "11:50-13:20", "ауд. 217", "Николаев Г.Р."],
    ],
    "Четверг": [
        ["1", "08:30-10:00", "ауд. 112", "Васильев К.Л."],
        ["2", "10:10-11:40", "ауд. 217", "Николаев Г.Р."],
        ["3", "11:50-13:20", "ауд. 415", "Петрова А.С."],
        ["4", "13:50-15:20", "ауд. 312", "Иванов И.И."],
    ],
    "Пятница": [
        ["1", "08:30-10:00", "ауд. 308", "Кузнецова Е.Н."],
        ["2", "10:10-11:40", "ауд. 415", "Петрова А.С."],
    ],
}

DAYS_ORDER = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


def build_intro(week: dict, source_url: str, is_demo: bool = True) -> str:
    tag = "🧪 <b>демо формата</b> — предметы убраны специально.\n\n" if is_demo else ""
    return (
        tag
        + f"📅 <b>Расписание 6381 — неделя {week['week']} (верхняя)</b>\n"
        + f"<i>{week['start']} — {week['end']}</i>\n"
        + f'<a href="{source_url}">источник · portal.novsu.ru</a>'
    )


def build_day(day: str, pairs: list[list[str]]) -> str:
    """Один пост = один день. Заголовок жирный, таблица monospace."""
    if not pairs:
        return f"<b>📆 {day}</b>\n<i>пар нет</i>"
    lines = [f"<b>📆 {day}</b>"]
    lines.append("<pre>")
    lines.append("№   время         ауд.      преподаватель")
    for row in pairs:
        n, t, room, who = (row + [""] * 4)[:4]
        lines.append(f"{n:<3} {t:<13} {room:<9} {who}")
    lines.append("</pre>")
    return "\n".join(lines)


def build_preview(week: dict | None = None, source_url: str = config.GROUP_URL, is_demo: bool = True) -> list[str]:
    week = week or {"week": 1, "half": "top", "start": "01.09.2026", "end": "05.09.2026"}
    msgs = [build_intro(week, source_url, is_demo)]
    for day in DAYS_ORDER:
        if day in DEMO_SCHEDULE:
            msgs.append(build_day(day, DEMO_SCHEDULE[day]))
    return msgs


def send_to_channel(msgs: list[str]) -> list[dict]:
    results = []
    for m in msgs:
        url = f"https://api.telegram.org/bot{config.TG_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": str(config.TG_CHANNEL_ID),
            "text": m,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                results.append(json.loads(r.read()))
        except urllib.error.HTTPError as e:  # noqa: PERF203
            body = e.read().decode(errors="replace")
            results.append({"ok": False, "error": f"HTTP {e.code}: {body[:300]}"})
        except Exception as e:  # noqa: BLE001
            results.append({"ok": False, "error": str(e)})
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--post", action="store_true", help="отправить в канал")
    args = ap.parse_args()

    msgs = build_preview()
    for i, m in enumerate(msgs, 1):
        print(f"--- message {i} ({len(m)} chars) ---")
        print(m)
        print()

    if args.post:
        print("=== отправляю в канал ===")
        results = send_to_channel(msgs)
        for r in results:
            print(json.dumps(r, ensure_ascii=False))
