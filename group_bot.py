"""Эфемерные команды расписания в групповом чате — текстовые вырезки дня.

Поллинг getUpdates строго в одном выделенном супергрупповом чате (без
NOVSU_GROUP_CHAT_ID процесс даже не стартует — в ЛС не отвечаем никогда).
Команды:
  /timetable, /расписание  -> вся текущая неделя
  /today, /сегодня         -> сегодня
  /tomorrow, /завтра       -> завтра
Аргументы: today|tomorrow|сегодня|завтра|вся|неделя|пн..вс.

Ответ на день — тот же развёрнутый раздел, что в закрепе канала
(sendRichMessage): таблица пар, ОПД-раздел и сворачиваемый скрин с портала.
Скрин берётся готовым из кеша закрепа state/dashboard_screens.json —
рендер ноль, парсинг ноль. Если дня нет в расписании или rich-отправка
не прошла — ответ деградирует до текстовой вырезки. /timetable — текст.
Данные — те же state/last_parsed.json, что рисуют закреп: материал один,
ничего не рендерится и сервер не нагружается.

Дата всегда по Москве (сервер в UTC — иначе после полуночи МСК бот путает
день).

Эфемерность нативная (Bot API 10.2+, формат 10.3): ответ уходит с
ephemeral_message_parameters.receiver_user_id — его видит только автор
команды, Telegram сам прячет его от остальных и сам удаляет со временем.
Бот — админ группы, поэтому отвечать можно в любой момент: без
15-секундного окна и без callback_query_id (это ограничение не-админов).
Кирилличные псевдонимы (/сегодня) парсятся, но в меню не регистрируются
(BotCommand допускает только латиницу) — при отправке такая команда
остаётся видна группе, ответ всё равно эфемерный. Анонимный админ
(from отсутствует) не имеет user_id для адресации — ему уходит обычный
публичный ответ. Доставка эфемерного сообщения не гарантирована
(офлайн-клиент может его не получить) — это куртиз, а не канал доставки.

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
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import config
import bells
import opd as opd_module
import telegram_api
from schedule_logic import find_week, resolve_dashboard_view, week_view
from format import (
    DAY_FULL_TO_SHORT,
    DAY_SHORT_TO_FULL,
    _dashboard_day_section,
    _day_hides_expired,
    build_dashboard_rich_message,
)
from telegram_api import call as api_call

BOT_USERNAME = "novsutimetablebot"
MSK = ZoneInfo("Europe/Moscow")
STATE_FILE = config.STATE_DIR / "last_parsed.json"
OFFSET_FILE = config.STATE_DIR / "group_bot_offset.json"
#: Кеш готовых скринов дней недели, которыми живёт закреп канала.
SCREENS_FILE = config.STATE_DIR / "dashboard_screens.json"
DASHBOARD_POST_FILE = config.STATE_DIR / "last_dashboard_post.json"
MONITOR_STATE_FILE = config.STATE_DIR / "monitor_state.json"

#: Команды, регистрируемые в меню группы как эфемерные (BotCommand — только
#: латиница; is_ephemeral=true прячет саму команду от остальных участников).
EPHEMERAL_COMMANDS = (
    {"command": "timetable", "description": "Расписание недели — видно только тебе"},
    {"command": "today", "description": "Пары на сегодня — видно только тебе"},
    {"command": "tomorrow", "description": "Пары на завтра — видно только тебе"},
)

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


def _cached_day_screen(data: dict, target: dt.date, today: dt.date) -> Path | None:
    """Готовый скрин дня из кеша закрепа — рендера ноль.

    Скрин годится только для дня той же учебной недели, что сейчас в закрепе:
    кеш подписан короткими именами дней, и «Пн» следующей недели — другой
    файл. Иначе молча отдаём None и ответ уходит без картинки.
    """
    week_today = _week_block(data, today)
    week_target = _week_block(data, target)
    if not week_today or week_today != week_target:
        return None
    try:
        cache = json.loads(SCREENS_FILE.read_text(encoding="utf-8"))
        items = cache.get("items") or []
    except (OSError, ValueError):
        return None
    short = DAY_FULL_TO_SHORT.get(DAY_NAMES[target.weekday()])
    for item in items:
        if item.get("label") == short:
            path = Path(str(item.get("path") or ""))
            return path if path.is_file() else None
    return None



def build_week_answer(data: dict, today: dt.date, now: dt.datetime | None = None) -> dict:
    """Пост недели 1 в 1 как в закрепе канала: те же блоки, ОПД и скрины.

    Рендер ноль: скрины только готовые из кеша закрепа
    state/dashboard_screens.json, живые дни считаются той же логикой
    (resolve_dashboard_view), что рисует пост. Без кеша пост уйдёт без
    картинок — расписание то же.
    """
    if now is None:
        now = _dashboard_now(today)
    try:
        opd_data = opd_module.load_opd(now=now)
    except Exception:  # noqa: BLE001 - ОПД не должен ронять ответ
        opd_data = None
    late_ends = opd_module.late_ends(opd_data)
    view = resolve_dashboard_view(
        data.get("schedule"), data.get("weeks") or [], now, late_ends=late_ends)
    live_days = set(view["live_days"])
    items = [item for item in _screens_cache_items() if _item_day_live(item, live_days)]
    screenshot_media = [
        {"label": item["label"], "media": f"attach://site_screenshot_{index}"}
        for index, item in enumerate(items, 1)
    ]
    files = {f"site_screenshot_{index}": Path(str(item["path"]))
             for index, item in enumerate(items, 1)}
    sleepy = False
    try:
        state = json.loads(DASHBOARD_POST_FILE.read_text(encoding="utf-8"))
        sleepy = bool(state.get("sleepy"))
    except (OSError, ValueError):
        pass
    rich = build_dashboard_rich_message(
        data.get("schedule"),
        data.get("weeks") or [],
        config.GROUP_URL,
        view["target_date"],
        group_name="6381",
        screenshot_media=screenshot_media,
        current_date=now.date(),
        now=now,
        last_updated=_post_stamp(),
        schedule_changed=_schedule_changed_stamp(),
        opd=opd_data,
        sleep_note="😴 Сайт в спячке — сохранённое расписание" if sleepy else "",
    )
    return {"rich": rich, "files": files or None}


def _dashboard_now(today: dt.date) -> dt.datetime:
    """«Сейчас» для копии закрепа: дата плана + реальное время МСК."""
    return dt.datetime.combine(today, dt.datetime.now(MSK).timetz())


def _screens_cache_items() -> list[dict]:
    try:
        cache = json.loads(SCREENS_FILE.read_text(encoding="utf-8"))
        items = cache.get("items") or []
    except (OSError, ValueError):
        return []
    return [item for item in items
            if Path(str(item.get("path") or "")).is_file()]


def _item_day_live(item: dict, live_days: set) -> bool:
    label = str(item.get("label") or "")
    day_name = DAY_SHORT_TO_FULL.get(label)
    return day_name is None or day_name in live_days


def _post_stamp() -> str:
    """«Обновлено …» — фактическое время поста закрепа, не время ответа."""
    try:
        state = json.loads(DASHBOARD_POST_FILE.read_text(encoding="utf-8"))
        ts = str(state.get("ts") or "")
        if ts:
            stamp = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(MSK)
            return stamp.strftime("%d.%m.%Y %H:%M")
    except (OSError, ValueError):
        pass
    return dt.datetime.now(MSK).strftime("%d.%m.%Y %H:%M")


def _schedule_changed_stamp() -> str:
    try:
        state = json.loads(MONITOR_STATE_FILE.read_text(encoding="utf-8"))
        event = state.get("last_change_event") or state.get("event") or {}
        ts = str(event.get("ts") or "")
        if ts:
            stamp = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(MSK)
            return stamp.strftime("%d.%m.%Y %H:%M")
    except (OSError, ValueError):
        pass
    return ""


def build_day_answer(data: dict, target: dt.date, today: dt.date | None = None) -> dict:
    """Раздел дня ровно как в закрепе канала: rich-блок + готовый скрин.

    Возвращает {"rich": ..., "files": ...} (files может быть None) или {},
    если дня нет в расписании — тогда ответ деградирует до текстовой вырезки.
    """
    today = today or today_msk()
    week = find_week(data.get("weeks") or [], target)
    if not week:
        return {}
    view = week_view(data.get("schedule"), data.get("weeks"), week,
                     group="6381", source_url=config.GROUP_URL)
    day = next((d for d in view.get("days") or []
                if d.get("date") == target.isoformat() and d.get("lessons")), None)
    if day is None:
        return {}
    try:
        opd_data = opd_module.load_opd(now=dt.datetime.now(MSK))
    except Exception:  # noqa: BLE001 - ОПД не должен ронять ответ
        opd_data = None
    opd_view = opd_module.day_view(opd_data, target) if opd_data else None
    screen = _cached_day_screen(data, target, today)
    media = "attach://day_screenshot_1" if screen else None
    block = _dashboard_day_section(
        day,
        screenshot_media=media,
        source_url=config.GROUP_URL,
        opd_view=opd_view,
        hidden_expired=_day_hides_expired(data.get("schedule"), data.get("weeks") or [], target),
    )
    rich = {"rich_message": {"blocks": [block]}}
    files = {"day_screenshot_1": screen} if screen else None
    return {"rich": rich, "files": files}


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
    sender = message.get("from") or {}
    receiver = sender.get("id")
    data = _load_parsed()
    if data is None:
        return {
            "kind": "reply",
            "chat_id": message["chat"]["id"],
            "receiver_user_id": receiver,
            "text": "Расписание ещё не подгрузилось, попробуй через пару минут.",
        }
    if target == "week":
        plan = {
            "kind": "reply",
            "chat_id": message["chat"]["id"],
            "receiver_user_id": receiver,
            "text": build_week_text(data, today),
        }
        # Пост 1 в 1 как в закрепе канала; текст — запасной ответ.
        plan.update(build_week_answer(data, today))
        return plan
    plan = {
        "kind": "reply",
        "chat_id": message["chat"]["id"],
        "receiver_user_id": receiver,
        "text": build_day_text(data, target),
    }
    # Раздел дня как в закрепе + готовый скрин; текст остаётся запасным
    # ответом, если rich-отправка вдруг не пройдёт.
    plan.update(build_day_answer(data, target, today=today))
    return plan


def _delete_message(chat_id: int, message_id: int) -> bool:
    try:
        result = api_call(config.TG_BOT_TOKEN, "deleteMessage",
                          {"chat_id": chat_id, "message_id": message_id}, timeout=15)
    except Exception as exc:  # noqa: BLE001 - удаление лучшее из возможного
        print(f"[group_bot] delete {message_id} failed: {exc}", file=sys.stderr)
        return False
    return bool(result.get("ok"))


def _send_text(plan: dict) -> dict:
    """Отправить ответ. С receiver_user_id — как эфемерное сообщение,
    видимое только автору команды; без него (анонимный админ) — публично."""
    payload: dict[str, Any] = {
        "chat_id": plan["chat_id"],
        "text": plan["text"],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    receiver = plan.get("receiver_user_id")
    if receiver:
        payload["ephemeral_message_parameters"] = {"receiver_user_id": receiver}
    result = api_call(config.TG_BOT_TOKEN, "sendMessage", payload, timeout=30)
    if result.get("ok"):
        message = result.get("result") or {}
        if receiver and not message.get("ephemeral_message_id"):
            # Страховка: параметры проигнорированы и ответ ушёл публично
            # (message_id у эфемерных всегда 0). Мгновенно вытираем и орём.
            leaked_id = message.get("message_id") or 0
            deleted = bool(leaked_id) and _delete_message(plan["chat_id"], leaked_id)
            print(
                f"[group_bot] ephemeral ignored, public leak deleted={deleted}: {message}",
                file=sys.stderr,
            )
            _alert("group_bot: эфемерность не сработала, публичный ответ "
                   f"{'удалён' if deleted else 'НЕ удалён'} — проверь права бота")
            return {"ok": False, "error": "ephemeral_ignored", "leak_deleted": deleted}
        return result
    print(f"[group_bot] send failed: {result}", file=sys.stderr)
    _alert(f"group_bot: sendMessage failed: {result.get('description') or result}")
    return result


def _send_rich(plan: dict) -> dict:
    """Отправить rich-раздел дня. Эфемерный, если есть адресат."""
    extra: dict[str, Any] = {}
    receiver = plan.get("receiver_user_id")
    if receiver:
        extra["ephemeral_message_parameters"] = {"receiver_user_id": receiver}
    result = telegram_api.send_rich_message(
        plan["rich"],
        token=config.TG_BOT_TOKEN,
        chat_id=plan["chat_id"],
        files=plan.get("files"),
        timeout=60,
        extra_payload=extra or None,
    )
    if result.get("ok"):
        message = result.get("result") or {}
        if receiver and not message.get("ephemeral_message_id"):
            leaked_id = message.get("message_id") or 0
            deleted = bool(leaked_id) and _delete_message(plan["chat_id"], leaked_id)
            print(
                f"[group_bot] ephemeral ignored, public leak deleted={deleted}: {message}",
                file=sys.stderr,
            )
            _alert("group_bot: эфемерность не сработала, публичный rich-ответ "
                   f"{'удалён' if deleted else 'НЕ удалён'} — проверь права бота")
            return {"ok": False, "error": "ephemeral_ignored", "leak_deleted": deleted}
        return result
    print(f"[group_bot] sendRichMessage failed: {result}", file=sys.stderr)
    return result


def process_update(update: dict) -> None:
    plan = plan_response(
        update,
        allowed_chat=getattr(config, "TG_GROUP_CHAT_ID", None),
        allowed_user=config.TG_DM_TARGET,
    )
    if plan.get("kind") != "reply":
        return
    if plan.get("rich"):
        result = _send_rich(plan)
        if result.get("ok"):
            return
        # Не ушло rich'ом — деградируем до текста, ответ терять нельзя.
        print("[group_bot] falling back to plain text", file=sys.stderr)
    _send_text(plan)


def register_commands() -> dict:
    """Зарегистрировать команды группы как эфемерные (setMyCommands, scope
    chat). Идемпотентно; вызывается на старте, чтобы меню всегда было актуально."""
    payload = {
        "commands": [dict(command) | {"is_ephemeral": True} for command in EPHEMERAL_COMMANDS],
        "scope": {"type": "chat", "chat_id": config.TG_GROUP_CHAT_ID},
    }
    return api_call(config.TG_BOT_TOKEN, "setMyCommands", payload, timeout=15)


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
    try:
        registered = register_commands()
        if not registered.get("ok"):
            print(f"[group_bot] setMyCommands failed: {registered}", file=sys.stderr)
            _alert("group_bot: не смог зарегистрировать эфемерные команды — "
                   "они будут видны при отправке")
    except Exception as exc:  # noqa: BLE001 - меню не должно валить бота
        print(f"[group_bot] setMyCommands error: {exc}", file=sys.stderr)
    if "--once" in argv:
        poll_once(_read_offset())
        return 0
    run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
