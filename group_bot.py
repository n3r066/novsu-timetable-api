"""Эфемерные команды расписания в групповом чате — текстовые вырезки дня.

Поллинг getUpdates строго в одном выделенном супергрупповом чате (без
NOVSU_GROUP_CHAT_ID процесс даже не стартует — в ЛС не отвечаем никогда).
Команды:
  /timetable, /расписание  -> вся текущая неделя
  /today, /сегодня         -> сегодня
  /tomorrow, /завтра       -> завтра
Аргументы: today|tomorrow|сегодня|завтра|вся|неделя|пн..вс.

Ответ — текст в разметке канала: шапка «СРЕДА · 23.09.2026 · 5 пар · 1 ДОТ»
и пары как в основном посте: номер, время, тип, предмет, препод, место.
Данные — те же state/last_parsed.json, что рисуют закреп: материал один,
ничего не рендерится и сервер не нагружается.

Дата всегда по Москве (сервер в UTC — иначе после полуночи МСК бот путает
день). Эфемерность эмулируется: нативных эфемерных сообщений в Bot API нет
(проверено 2026-09-22), поэтому через TTL бот-админ удаляет и свой ответ,
и саму команду.

Запуск: python3 group_bot.py            # вечный поллинг
        python3 group_bot.py --once     # один батч (отладка)
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import config
import bells
from telegram_api import call as api_call

BOT_USERNAME = "novsutimetablebot"
MSK = ZoneInfo("Europe/Moscow")
STATE_FILE = config.STATE_DIR / "last_parsed.json"
OFFSET_FILE = config.STATE_DIR / "group_bot_offset.json"

#: Через сколько секунд после ответа удаляются и ответ, и команда.
TTL_S = float(getattr(config, "GROUP_BOT_TTL_S", None) or 90)

DAY_NAMES = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
DAY_ALIASES = {
    "пн": 0, "понедельник": 0,
    "вт": 1, "вторник": 1,
    "ср": 2, "среда": 2,
    "чт": 3, "четверг": 3,
    "пт": 4, "пятница": 4,
    "сб": 5, "суббота": 5,
    "вс": 6, "воскресенье": 6,
}
TODAY_WORDS = {"today", "сегодня"}
TOMORROW_WORDS = {"tomorrow", "завтра"}
ALL_WORDS = {"all", "вся", "всё", "все", "week", "неделя"}

#: Команда -> аргумент по умолчанию, если пользователь ничего не дописал.
COMMANDS = {
    "timetable": "вся",
    "расписание": "вся",
    "today": "сегодня",
    "сегодня": "сегодня",
    "tomorrow": "завтра",
    "завтра": "завтра",
}

_TYPE_RE = re.compile(r"^\(([^)]*)\)\s*")

#: Фабрика таймеров самоудаления — в тестах подменяется на фейк.
timer_factory = threading.Timer


def today_msk(now: dt.datetime | None = None) -> dt.date:
    """Текущая дата по Москве. Сервер ходит в UTC — date.today() врёт."""
    now = now or dt.datetime.now(tz=dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    return now.astimezone(MSK).date()


def _load_parsed() -> dict | None:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    schedule = data.get("schedule") or {}
    if not schedule.get("days"):
        return None
    return data


def _esc(value: object) -> str:
    return html.escape(str(value or ""), quote=False)


def _pairs_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "пара"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "пары"
    return "пар"


def _lesson_parts(lesson: dict) -> tuple[str, str]:
    """Тип занятия и чистое название: '(пр.) История' -> ('ПР.', 'История')."""
    subject = str(lesson.get("subject_raw") or lesson.get("subject") or "").strip()
    match = _TYPE_RE.match(subject)
    if match:
        kind = match.group(1).strip().rstrip(".").upper()
        kind = f"{kind}." if kind else ""
        subject = subject[match.end():].strip()
        return kind, subject
    return "", subject.split("\n", 1)[0].strip()


def _lesson_lines(lesson: dict) -> list[str]:
    """Две строки пары как в посте канала: заголовок + препод/место."""
    kind, subject = _lesson_parts(lesson)
    time_label = bells.label(lesson.get("raw_time") or lesson.get("time") or "")
    number = lesson.get("number")
    head_parts = []
    if number:
        head_parts.append(f"<b>{_esc(number)}.</b>")
    if time_label:
        head_parts.append(f"<b>{_esc(time_label)}</b>")
    if kind:
        head_parts.append(f"<b>{_esc(kind)}</b>")
    if subject:
        head_parts.append(_esc(subject))
    lines = [" ".join(head_parts)]
    meta = []
    teacher = str(lesson.get("teacher") or "").strip()
    if teacher and teacher != "—":
        meta.append(_esc(teacher))
    if lesson.get("delivery_mode") in ("dot", "remote_or_hybrid"):
        meta.append("ДОТ")
    else:
        room = str(lesson.get("room") or "").strip().strip(".")
        if room and room != "—":
            meta.append(f"ауд. {_esc(room)}")
        location = str(lesson.get("location") or "").strip()
        if location and location != "—":
            meta.append(_esc(location))
    if meta:
        lines.append(" · ".join(meta))
    return lines


def day_caption(data: dict, target: dt.date) -> str:
    """Шапка дня: «СРЕДА · 23.09.2026 · 3 пары · 1 ДОТ»."""
    day_name = DAY_NAMES[target.weekday()].upper()
    lessons = (data.get("schedule") or {}).get("days", {}).get(DAY_NAMES[target.weekday()]) or []
    head = f"<b>{_esc(day_name)} · {target.strftime('%d.%m.%Y')}</b>"
    if not lessons:
        return f"{head} · пар нет"
    parts = [head, f"{len(lessons)} {_pairs_word(len(lessons))}"]
    dot_count = sum(1 for lesson in lessons if lesson.get("delivery_mode") in ("dot", "remote_or_hybrid"))
    if dot_count:
        parts.append(f"{dot_count} ДОТ")
    return " · ".join(parts)


def build_day_text(data: dict, target: dt.date) -> str:
    """Вырезка одного дня из поста канала."""
    day_name = DAY_NAMES[target.weekday()]
    lessons = (data.get("schedule") or {}).get("days", {}).get(day_name) or []
    caption = day_caption(data, target)
    if not lessons:
        return f"{caption}\n\nПар нет 🎉"
    blocks = ["\n".join(_lesson_lines(lesson)) for lesson in lessons]
    return caption + "\n\n" + "\n\n".join(blocks)


def _week_block(data: dict, target: dt.date) -> dict | None:
    """Неделя из data['weeks'], в которую попадает target."""
    for week in (data.get("weeks") or []):
        try:
            start = dt.datetime.strptime(week.get("start", ""), "%d.%m.%Y").date()
            end = dt.datetime.strptime(week.get("end", ""), "%d.%m.%Y").date()
        except (TypeError, ValueError):
            continue
        if start <= target <= end:
            return week
    return None


def build_week_text(data: dict, target: dt.date, group_name: str = "6381") -> str:
    """Неделя с Monday целевой даты: шапка недели + вырезки всех дней."""
    week = _week_block(data, target)
    half = {"top": "верхняя", "bottom": "нижняя"}.get((week or {}).get("half", ""), "")
    if week:
        title = f"НЕДЕЛЯ {week.get('week')}{f' · {half}' if half else ''} · {_esc(week.get('start'))}—{_esc(week.get('end'))}"
    else:
        title = f"НЕДЕЛЯ · группа {_esc(group_name)}"
    days = (data.get("schedule") or {}).get("days", {})
    monday = target - dt.timedelta(days=target.weekday())
    chunks = [f"<b>{title}</b>"]
    total = len(chunks[0])
    for offset in range(7):
        day = monday + dt.timedelta(days=offset)
        day_name = DAY_NAMES[offset]
        if not days.get(day_name):
            continue
        block = build_day_text(data, day)
        if total + len(block) > 3500:
            chunks.append("\n\n…дальше в закрепе канала")
            break
        chunks.append(block)
        total += len(block)
    if len(chunks) == 1:
        return f"{chunks[0]}\n\nПар нет 🎉"
    return "\n\n".join(chunks)


def extract_command(text: object, bot_username: str = BOT_USERNAME) -> tuple[str, str] | None:
    """«/today@bot завтра» -> ("today", "завтра"); не команды -> None."""
    if not isinstance(text, str) or not text.startswith("/"):
        return None
    token, _, rest = text[1:].partition(" ")
    command, _, mentioned = token.partition("@")
    if mentioned and mentioned.casefold() != bot_username.casefold():
        return None
    return command.strip().casefold(), rest.strip()


def resolve_target(args: str, today: dt.date) -> dt.date | str | None:
    """Аргумент -> дата, 'week' или None (не разобрали)."""
    word = args.strip().casefold()
    if word in TODAY_WORDS:
        return today
    if word in TOMORROW_WORDS:
        return today + dt.timedelta(days=1)
    if word in ALL_WORDS:
        return "week"
    if word in DAY_ALIASES:
        wanted = DAY_ALIASES[word]
        delta = (wanted - today.weekday()) % 7
        return today + dt.timedelta(days=delta)
    return None


def _is_allowed(update: dict, allowed_chat: int | None, allowed_user: int | None) -> bool:
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    if allowed_chat is None or chat.get("id") != allowed_chat:
        return False
    if chat.get("type") not in ("group", "supergroup"):
        return False
    user = message.get("from")
    if user and allowed_user is not None and user.get("id") == allowed_user:
        return True
    # Анонимный админ: from отсутствует, sender_chat равен чату.
    sender_chat = message.get("sender_chat") or {}
    if user is None and sender_chat.get("id") == chat.get("id"):
        return True
    return False


def plan_response(
    update: dict,
    *,
    allowed_chat: int | None,
    allowed_user: int | None,
    today: dt.date | None = None,
) -> dict:
    """Чистая логика: update -> план 'reply' | 'ignore'."""
    message = update.get("message") or {}
    if not _is_allowed(update, allowed_chat, allowed_user):
        return {"kind": "ignore"}
    parsed = extract_command(message.get("text"))
    if parsed is None:
        return {"kind": "ignore"}
    command, args = parsed
    if command not in COMMANDS:
        return {"kind": "ignore"}
    today = today or today_msk()
    target = resolve_target(args or COMMANDS[command], today)
    if target is None:
        return {"kind": "ignore"}
    data = _load_parsed()
    if data is None:
        return {
            "kind": "reply",
            "chat_id": message["chat"]["id"],
            "text": "Расписание ещё не подгрузилось, попробуй через пару минут.",
            "command_message_id": message.get("message_id"),
        }
    if target == "week":
        text = build_week_text(data, today)
    else:
        text = build_day_text(data, target)
    return {
        "kind": "reply",
        "chat_id": message["chat"]["id"],
        "text": text,
        "command_message_id": message.get("message_id"),
    }


def _delete_later(chat_id: int, *message_ids: int) -> None:
    def _worker() -> None:
        for message_id in message_ids:
            if not message_id:
                continue
            try:
                api_call(config.TG_BOT_TOKEN, "deleteMessage",
                         {"chat_id": chat_id, "message_id": message_id}, timeout=15)
            except Exception as exc:  # noqa: BLE001 - удаление лучшее из возможного
                print(f"[group_bot] delete {message_id} failed: {exc}", file=sys.stderr)

    timer_factory(TTL_S, _worker).start()


def _send_text(plan: dict) -> list[int]:
    payload: dict[str, Any] = {
        "chat_id": plan["chat_id"],
        "text": plan["text"],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_to_message_id": plan.get("command_message_id"),
    }
    result = api_call(config.TG_BOT_TOKEN, "sendMessage", payload, timeout=30)
    if not result.get("ok") and plan.get("command_message_id"):
        # Команда могла успеть удалиться/протухнуть — шлём без reply,
        # иначе весь ответ теряется из-за одной ссылки на сообщение.
        description = str(result.get("description", ""))
        if "replied" in description or "reply" in description:
            payload.pop("reply_to_message_id", None)
            result = api_call(config.TG_BOT_TOKEN, "sendMessage", payload, timeout=30)
    if not result.get("ok"):
        print(f"[group_bot] send failed: {result}", file=sys.stderr)
        return []
    message_id = (result.get("result") or {}).get("message_id")
    return [message_id] if message_id else []


def process_update(update: dict) -> None:
    plan = plan_response(
        update,
        allowed_chat=getattr(config, "TG_GROUP_CHAT_ID", None),
        allowed_user=config.TG_DM_TARGET,
    )
    if plan.get("kind") != "reply":
        return
    sent_ids = _send_text(plan)
    if not sent_ids:
        return
    # Эфемерность: через TTL исчезают и ответ, и сама команда.
    _delete_later(plan["chat_id"], *sent_ids, plan.get("command_message_id"))


def _read_offset() -> int:
    try:
        return int(json.loads(OFFSET_FILE.read_text(encoding="utf-8")).get("offset", 0))
    except (OSError, ValueError):
        return 0


def _write_offset(offset: int) -> None:
    temporary = OFFSET_FILE.with_name(f".{OFFSET_FILE.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps({"offset": offset}), encoding="utf-8")
    os.replace(temporary, OFFSET_FILE)


def poll_once(offset: int) -> int:
    result = api_call(
        config.TG_BOT_TOKEN,
        "getUpdates",
        {"offset": offset, "timeout": 25, "allowed_updates": ["message"]},
        timeout=40,
    )
    if not result.get("ok"):
        raise RuntimeError(f"getUpdates failed: {result}")
    updates = result.get("result") or []
    for update in updates:
        try:
            process_update(update)
        except Exception as exc:  # noqa: BLE001 - одно плохое обновление не роняет цикл
            print(f"[group_bot] update {update.get('update_id')} failed: {exc}", file=sys.stderr)
        offset = max(offset, int(update["update_id"]) + 1)
    if updates:
        _write_offset(offset)
    return offset


def _alert(text: str) -> None:
    if not config.TG_DM_TARGET:
        return
    try:
        api_call(config.TG_BOT_TOKEN, "sendMessage",
                 {"chat_id": config.TG_DM_TARGET, "text": text}, timeout=15)
    except Exception as exc:  # noqa: BLE001
        print(f"[group_bot] alert failed: {exc}", file=sys.stderr)


def run_forever() -> None:
    offset = _read_offset()
    failures = 0
    alerted = False
    while True:
        try:
            offset = poll_once(offset)
            if alerted:
                _alert("✅ group_bot: поллинг восстановился")
            failures = 0
            alerted = False
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[group_bot ERR {failures}] {exc}", file=sys.stderr)
            if failures >= 3 and not alerted:
                _alert(f"group_bot: {failures} подряд неудачных поллингов\nпоследняя ошибка: {exc}")
                alerted = True
            time.sleep(min(30, 2 ** failures))


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if getattr(config, "TG_GROUP_CHAT_ID", None) is None:
        # Fail-closed: без привязки к группе бот не должен отвечать в ЛС.
        print("[group_bot] NOVSU_GROUP_CHAT_ID не задан — старт отменён", file=sys.stderr)
        return 1
    if "--once" in argv:
        poll_once(_read_offset())
        return 0
    run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
