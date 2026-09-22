"""Эфемерные команды расписания в групповом чате.

Поллинг getUpdates строго в одном выделенном супергрупповом чате (без
NOVSU_GROUP_CHAT_ID процесс даже не стартует — в ЛС не отвечаем никогда).
Команды:
  /timetable, /расписание  -> вся неделя (альбом тех же скринов, что висят
                            в закрепе канала; без рендера, файлы берутся
                            из state/dashboard_screens.json)
  /today, /сегодня         -> сегодня (текст)
  /tomorrow, /завтра       -> завтра (текст)
Аргументы: today|tomorrow|сегодня|завтра|вся|неделя|пн..вс.

Дата всегда по Москве (сервер в UTC — иначе после полуночи МСК бот путает
день). Эфемерность эмулируется: нативных эфемерных сообщений в Bot API нет
(проверено 2026-09-22: методов ephemeral/auto-delete нет, лишние поля
молча игнорируются), поэтому через TTL бот-админ удаляет и свой ответ,
и саму команду.

Запуск: python3 group_bot.py            # вечный поллинг
        python3 group_bot.py --once     # один батч (отладка)
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import config
import bells
from telegram_api import call as api_call
from telegram_api import call_multipart as api_multipart

BOT_USERNAME = "novsutimetablebot"
MSK = ZoneInfo("Europe/Moscow")
STATE_FILE = config.STATE_DIR / "last_parsed.json"
OFFSET_FILE = config.STATE_DIR / "group_bot_offset.json"
SCREENS_INDEX = config.STATE_DIR / "dashboard_screens.json"

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


def _lesson_line(lesson: dict) -> str:
    time_label = bells.label(lesson.get("raw_time") or lesson.get("time") or "")
    subject = str(lesson.get("subject") or "").split("\n", 1)[0].strip()
    room = str(lesson.get("room") or "").strip().strip(".")
    parts = []
    if time_label:
        parts.append(f"<b>{_esc(time_label)}</b>")
    parts.append(_esc(subject))
    if room and room != "—":
        parts.append(f"ауд. {_esc(room)}")
    return " · ".join(parts)


def _week_block(data: dict, target: dt.date) -> dict | None:
    """Неделя из data["weeks"], в которую попадает target."""
    for week in (data.get("weeks") or []):
        try:
            start = dt.datetime.strptime(week.get("start", ""), "%d.%m.%Y").date()
            end = dt.datetime.strptime(week.get("end", ""), "%d.%m.%Y").date()
        except (TypeError, ValueError):
            continue
        if start <= target <= end:
            return week
    return None


def _week_title(data: dict, target: dt.date, group_name: str = "6381") -> str:
    week = _week_block(data, target)
    if not week:
        return f"<b>Расписание · группа {_esc(group_name)}</b>"
    half = {"top": "верхняя", "bottom": "нижняя"}.get(week.get("half", ""), "")
    half = f" · {half}" if half else ""
    return (
        f"<b>Расписание · группа {_esc(group_name)}</b>\n"
        f"Неделя {week.get('week')}{half} · "
        f"{_esc(week.get('start'))}—{_esc(week.get('end'))}"
    )


def build_day_text(data: dict, target: dt.date, group_name: str = "6381") -> str:
    """Компактный HTML одного дня; пустой день — честная заглушка."""
    day_name = DAY_NAMES[target.weekday()]
    lessons = (data.get("schedule") or {}).get("days", {}).get(day_name) or []
    head = f"<b>{_esc(day_name)} · {target.strftime('%d.%m.%Y')}</b> · группа {_esc(group_name)}"
    if not lessons:
        return f"{head}\n\nПар нет 🎉"
    lines = [_lesson_line(lesson) for lesson in lessons]
    return head + "\n" + "\n".join(lines)


def build_week_text(data: dict, target: dt.date, group_name: str = "6381") -> str:
    """Неделя с Monday целевой даты; длина ограничена 3500."""
    days = (data.get("schedule") or {}).get("days", {})
    monday = target - dt.timedelta(days=target.weekday())
    chunks = [_week_title(data, target, group_name)]
    total = 0
    for offset in range(7):
        day = monday + dt.timedelta(days=offset)
        day_name = DAY_NAMES[offset]
        lessons = days.get(day_name) or []
        if not lessons:
            continue
        lines = [f"\n<b>{_esc(day_name)} · {day.strftime('%d.%m')}</b>"]
        lines += [_lesson_line(lesson) for lesson in lessons]
        block = "\n".join(lines)
        if total + len(block) > 3500:
            chunks.append("\n…дальше в закрепе канала")
            break
        chunks.append(block)
        total += len(block)
    if len(chunks) == 1:
        return f"{chunks[0]}\n\nПар нет 🎉"
    return "\n".join(chunks)


def build_week_caption(data: dict, target: dt.date, group_name: str = "6381") -> str:
    """Короткая подпись под альбомом скринов — подробности уже на картинках."""
    return _week_title(data, target, group_name)


SCREEN_LABELS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def ready_screens(target: dt.date) -> list[tuple[str, str]]:
    """Готовые скрины дней — те же файлы, что ушли в закреп канала.

    Только читаем state/dashboard_screens.json и проверяем, что файлы на
    месте и кеш относится к той же неделе, что и target — иначе чужие
    скрины не подсовываем и падаем в текст. Ничего не рендерим: материалы
    не дублируются и сервер не нагружается.
    """
    try:
        index = json.loads(SCREENS_INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw_key = str(index.get("status_key") or "")
    try:
        cached_day = dt.datetime.strptime(raw_key.rsplit(":", 1)[-1], "%Y-%m-%d").date()
    except (IndexError, ValueError):
        return []
    monday = lambda day: day - dt.timedelta(days=day.weekday())  # noqa: E731
    if monday(cached_day) != monday(target):
        return []
    items = []
    for item in index.get("items") or []:
        label = str(item.get("label") or "")
        path = str(item.get("path") or "")
        if label and path and Path(path).is_file():
            items.append((label, path))
    return items


def _pairs_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "пара"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "пары"
    return "пар"


def day_caption(data: dict, target: dt.date) -> str:
    """Заголовок дня в стиле канала: СРЕДА · 23.09.2026 · 3 пары · 1 ДОТ."""
    day_name = DAY_NAMES[target.weekday()].upper()
    lessons = (data.get("schedule") or {}).get("days", {}).get(DAY_NAMES[target.weekday()]) or []
    head = f"<b>{_esc(day_name)} · {target.strftime('%d.%m.%Y')}</b>"
    if not lessons:
        return f"{head} · пар нет"
    parts = [head, f"{len(lessons)} {_pairs_word(len(lessons))}"]
    dot_count = sum(1 for lesson in lessons if str(lesson.get("room") or "").strip().upper() == "ДОТ")
    if dot_count:
        parts.append(f"{dot_count} ДОТ")
    return " · ".join(parts)


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
    """Чистая логика: update -> план 'reply' | 'reply_media' | 'ignore'."""
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
    base = {
        "chat_id": message["chat"]["id"],
        "command_message_id": message.get("message_id"),
    }
    if target == "week":
        screens = ready_screens(today)
        if screens:
            return {
                "kind": "reply_media",
                "caption": build_week_caption(data, today),
                "screens": screens,
                **base,
            }
        return {"kind": "reply", "text": build_week_text(data, today), **base}
    screens = ready_screens(target)
    if screens:
        label = SCREEN_LABELS[target.weekday()]
        day_screens = [screen for screen in screens if screen[0] == label]
        if day_screens:
            return {
                "kind": "reply_media",
                "caption": day_caption(data, target),
                "screens": day_screens,
                **base,
            }
    return {"kind": "reply", "text": build_day_text(data, target), **base}


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
    result = api_call(
        config.TG_BOT_TOKEN,
        "sendMessage",
        {
            "chat_id": plan["chat_id"],
            "text": plan["text"],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_to_message_id": plan.get("command_message_id"),
        },
        timeout=30,
    )
    if not result.get("ok"):
        print(f"[group_bot] send failed: {result}", file=sys.stderr)
        return []
    message_id = (result.get("result") or {}).get("message_id")
    return [message_id] if message_id else []


def _send_album(plan: dict) -> list[int]:
    """Альбом из готовых скринов закрепа; подпись — на первой картинке."""
    files = {}
    media = []
    for idx, (_label, path) in enumerate(plan["screens"]):
        field = f"p{idx}"
        files[field] = Path(path)
        item: dict[str, Any] = {"type": "photo", "media": f"attach://{field}"}
        if idx == 0:
            item["caption"] = plan.get("caption") or ""
            item["parse_mode"] = "HTML"
        media.append(item)
    result = api_multipart(
        config.TG_BOT_TOKEN,
        "sendMediaGroup",
        {"chat_id": plan["chat_id"], "media": media},
        files,
        timeout=60,
    )
    if not result.get("ok"):
        print(f"[group_bot] album failed: {result}", file=sys.stderr)
        return []
    return [m.get("message_id") for m in (result.get("result") or []) if m.get("message_id")]


def process_update(update: dict) -> None:
    plan = plan_response(
        update,
        allowed_chat=getattr(config, "TG_GROUP_CHAT_ID", None),
        allowed_user=config.TG_DM_TARGET,
    )
    kind = plan.get("kind")
    if kind == "reply":
        sent_ids = _send_text(plan)
    elif kind == "reply_media":
        sent_ids = _send_album(plan)
    else:
        return
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
