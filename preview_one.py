"""Превью ОДНИМ rich message.

Bot API 10.2+ поддерживает expandable blockquote в MarkdownV2:
  **>Заголовок||скрытое содержимое||**
И в HTML (по docs):
  <aside><tg-spoiler>...</tg-spoiler></aside>  — не совсем то
  <blockquote expandable>...</blockquote>     — есть в Bot API 10.2+
Пробуем сначала MarkdownV2 expandable blockquote, потом HTML blockquote.

Запуск:
  python3 preview_one.py            # печатает preview
  python3 preview_one.py --send     # шлёт в канал -1004256784811
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

import config  # noqa: E402

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


def _md2_escape(s: str) -> str:
    """Экранирует спецсимволы MarkdownV2: _ * [ ] ( ) ~ ` > # + - = | { } . ! \\"""
    special = set("_*[]()~`>#+-=|{}.!\\\"")
    out = []
    for ch in s:
        if ch in special:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def build_day_md2(day: str, pairs: list[list[str]]) -> str:
    """День как expandable blockquote в MarkdownV2:
       **>📆 Понедельник||<pre>...</pre>||**
    """
    title = f"📆 {day}"
    if not pairs:
        return f"*>{_md2_escape(title)}||пар нет||"

    body_lines = []
    body_lines.append("```")
    body_lines.append("№   время         ауд.      преподаватель")
    for row in pairs:
        n, t, room, who = (row + [""] * 4)[:4]
        body_lines.append(f"{n:<3} {t:<13} {room:<9} {who}")
    body_lines.append("```")
    body = "\n".join(body_lines)
    return f"*{_md2_escape('>' + title)}||{body}||"


def build_one_md2(week: dict | None = None, source_url: str = config.GROUP_URL, is_demo: bool = True) -> str:
    """Одно сообщение в MarkdownV2 с expandable blockquote по каждому дню."""
    week = week or {"week": 1, "half": "top", "start": "01.09.2026", "end": "05.09.2026"}
    head = ""
    if is_demo:
        head += "*🧪 демо формата* — предметы убраны специально\\.\n\n"
    head += f"*📅 Расписание 6381 — неделя {week['week']} \\({_md2_escape('верхняя')}\\)*\n"
    head += f"_{week['start']} — {week['end']}_\n"
    head += f"[источник · portal\\.novsu\\.ru]({source_url})\n\n"

    days = []
    for day in DAYS_ORDER:
        if day in DEMO_SCHEDULE:
            days.append(build_day_md2(day, DEMO_SCHEDULE[day]))
    return head + "\n\n".join(days)


def build_day_html(day: str, pairs: list[list[str]]) -> str:
    """День как <blockquote expandable>...</blockquote>."""
    if not pairs:
        return f"<b>📆 {day}</b>\n<i>пар нет</i>"
    lines = [f"<b>📆 {day}</b>"]
    lines.append("<blockquote expandable>")
    lines.append("<pre>")
    lines.append("№   время         ауд.      преподаватель")
    for row in pairs:
        n, t, room, who = (row + [""] * 4)[:4]
        lines.append(f"{n:<3} {t:<13} {room:<9} {who}")
    lines.append("</pre>")
    lines.append("</blockquote>")
    return "\n".join(lines)


def build_one_html(week: dict | None = None, source_url: str = config.GROUP_URL, is_demo: bool = True) -> str:
    week = week or {"week": 1, "half": "top", "start": "01.09.2026", "end": "05.09.2026"}
    head = ""
    if is_demo:
        head += "🧪 <b>демо формата</b> — предметы убраны специально.\n\n"
    head += f"📅 <b>Расписание 6381 — неделя {week['week']} (верхняя)</b>\n"
    head += f"<i>{week['start']} — {week['end']}</i>\n"
    head += f'<a href="{source_url}">источник · portal.novsu.ru</a>\n\n'

    days = []
    for day in DAYS_ORDER:
        if day in DEMO_SCHEDULE:
            days.append(build_day_html(day, DEMO_SCHEDULE[day]))
    return head + "\n\n".join(days)


def send(text: str, parse_mode: str) -> dict:
    url = f"https://api.telegram.org/bot{config.TG_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": str(config.TG_CHANNEL_ID),
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:  # noqa: PERF203
        body = e.read().decode(errors="replace")
        return {"ok": False, "error_code": e.code, "error": body[:400]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="отправить в канал")
    args = ap.parse_args()

    print("=== MarkdownV2 ===")
    md2 = build_one_md2()
    print(f"len={len(md2)}")
    print(md2)
    print()

    print("=== HTML (blockquote expandable) ===")
    html = build_one_html()
    print(f"len={len(html)}")
    print(html)
    print()

    if args.send:
        print("=== отправляю MarkdownV2 ===")
        r1 = send(md2, "MarkdownV2")
        print(json.dumps(r1, ensure_ascii=False)[:700])
        if not r1.get("ok"):
            print()
            print("=== fallback: HTML blockquote expandable ===")
            r2 = send(html, "HTML")
            print(json.dumps(r2, ensure_ascii=False)[:700])
