# -*- coding: utf-8 -*-
"""Отслеживаемые посты про конкретных людей ОПД вне группы 6381.

Пост = полное расписание учебной группы человека (тот же рендер, что у
закрепа, но без скриншотов) плюс раздел ОПД по его виртуальной группе: портал
6701 и кеш кафедры сливаются в один пост. Монитор молча держит его
актуальным: сравнивает отпечаток и редактирует сообщение только при
изменении расписания группы, данных кафедры или наступлении нового дня.
Уведомлений в канал об этих изменениях нет, пост не закрепляется.
Спеки — в knowledge/schedule_notes.json («opd_tracked_posts»), message_id и
отпечатки — в state/extra_posts.json.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import zoneinfo

import config
import fetch as fetch_module
import opd as opd_module
import parse as parse_module
import telegram_api
from format import _paragraph, _rich_paragraph, build_dashboard_rich_message

STATE_FILE = config.STATE_DIR / "extra_posts.json"

# Сколько подряд неудачных циклов терпим молча перед tech-алертом в личку —
# как у основного цикла монитора.
_FAIL_ALERT_THRESHOLD = 3

# В кеше кафедры институт хранится только у одногруппников из чужих групп,
# поэтому для членов 6381 задаём его явно.
_MEMBER_INSTITUTE = {"6381": "ИЭ"}

_MSC = zoneinfo.ZoneInfo("Europe/Moscow")


def _now_msk() -> dt.datetime:
    return dt.datetime.now(_MSC)


def _stamp(now: dt.datetime) -> str:
    return now.strftime("%d.%m.%Y %H:%M")


def _load_specs() -> list[dict]:
    try:
        notes = json.loads((config.ROOT / "knowledge" / "schedule_notes.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    specs = notes.get("opd_tracked_posts") or []
    return [spec for spec in specs if spec.get("id") and spec.get("person") and spec.get("portal_group")]


def _read_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(state: dict) -> None:
    tmp = STATE_FILE.with_name(f".{STATE_FILE.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_FILE)


def _fingerprint(rich: dict) -> str:
    blob = json.dumps(rich, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _full_name(person: dict) -> str:
    return " ".join(p for p in (person.get("last_name"), person.get("first_name"), person.get("patronymic")) if p)


def _person_opd(opd_data: dict, person: dict) -> dict:
    """Синтетический срез кафедры под одного человека: он — единственный
    «свой» член, все остальные из его ВГ — одногруппники. Тогда штатный
    рендер закрепа рисует его строку и таблицу «Вместе в ВГ»."""
    vg = str(person["vg"])
    parts = person["full_name"].split()
    group = "".join(ch for ch in str(person.get("academic_group") or "") if ch.isdigit())
    member = {"vg": vg, "last_name": parts[0],
              "first_name": parts[1] if len(parts) > 1 else "",
              "patronymic": parts[2] if len(parts) > 2 else "", "group": group}
    mates = []
    for m in opd_data.get("members") or []:
        if str(m.get("vg")) == vg and _full_name(m) != person["full_name"]:
            mate = dict(m)
            mate["institute"] = _MEMBER_INSTITUTE.get(str(m.get("group") or ""), "")
            mates.append(mate)
    for m in opd_data.get("mates") or []:
        if str(m.get("vg")) == vg and _full_name(m) != person["full_name"]:
            mates.append(m)
    return {
        "version": opd_data.get("version"),
        "group": group,
        "members": [member],
        "mates": mates,
        "sessions": opd_data.get("sessions") or [],
        "sources": opd_data.get("sources") or {},
    }


def _load_portal(spec: dict) -> dict:
    ref = spec["portal_group"]
    url = config.build_group_url(ref["group"], ref)
    html = fetch_module.fetch_html(url)
    return {"url": url, "data": parse_module.parse_all(html)}


def _fmt_date(iso: str) -> str:
    year, month, day = iso.split("-")
    return f"{day}.{month}"


def _opd_dates_blocks(person: dict, opd_data: dict, today: dt.date) -> list[dict]:
    """«Когда и где» + все будущие даты её ВГ — чтобы изменение будущего
    занятия (перенос, отмена) тоже двигало отпечаток поста."""
    vg = str(person["vg"])
    sessions = sorted((s for s in opd_data.get("sessions") or [] if str(s.get("vg")) == vg),
                      key=lambda s: s["date"])
    upcoming = [s for s in sessions if s["date"] >= today.isoformat()]
    if not upcoming:
        return [_paragraph("ОПД: дат занятий её виртуальной группы в документе кафедры больше нет.")]
    base = next((s for s in upcoming if not s.get("cancelled")), upcoming[-1])
    where = (f"по четвергам {base['block_start']}–{base['block_end']}  ·  ауд. {base['room']} "
             f"({base['place']})  ·  {base['teacher']}")
    dates = []
    for session in upcoming:
        label = _fmt_date(session["date"])
        if session.get("cancelled"):
            label += " — занятий не будет"
        dates.append(label)
    return [
        _rich_paragraph([{"type": "bold", "text": f"ОПД · ВГ {vg}: "}, where]),
        _rich_paragraph([{"type": "bold", "text": "Даты: "}, "  ·  ".join(dates)]),
    ]


def render_post(spec: dict, opd_data: dict, portal: dict, *, today: dt.date,
                last_updated: str = "", schedule_changed: str = "") -> dict:
    person = spec["person"]
    rich = build_dashboard_rich_message(
        portal["data"]["schedule"], portal["data"]["weeks"], portal["url"], today,
        group_name=str(spec["portal_group"]["group"]),
        opd=_person_opd(opd_data, person),
        current_date=today,
        last_updated=last_updated, schedule_changed=schedule_changed,
    )
    blocks = rich["rich_message"]["blocks"]
    blocks.insert(1, _paragraph(spec["intro"]))
    for offset, block in enumerate(_opd_dates_blocks(person, opd_data, today)):
        blocks.insert(2 + offset, block)
    sources = (opd_data.get("sources") or {})
    links = [("таблица ВГ", sources.get("sheet")), ("расписание ВГ", sources.get("doc")),
             ("объявление кафедры", sources.get("announcement"))]
    parts: list[object] = [{"type": "italic", "text": "ОПД: "}]
    first = True
    for text, url in links:
        if not url:
            continue
        if not first:
            parts.append(" · ")
        parts.append({"type": "url", "text": text, "url": str(url)})
        first = False
    parts.append({"type": "italic", "text": "  ·  пост обновляется сам при изменениях"})
    blocks.append(_rich_paragraph(parts))
    telegram_api.validate_rich_payload(rich)
    return rich


def _sync_one(spec: dict, opd_data: dict, entry: dict, *, today: dt.date, now: dt.datetime) -> int | None:
    """Привести один пост к текущим данным. Возвращает message_id, если пост
    реально отправили или отредактировали, иначе None. Меняет entry на месте."""
    portal = _load_portal(spec)
    portal_fp = fetch_module.content_fingerprint(portal["data"])
    stamp = _stamp(now)
    message_id = entry.get("message_id")
    last_updated = entry.get("rendered_at") or stamp
    last_change = entry.get("last_change") or ""
    rich = render_post(spec, opd_data, portal, today=today,
                       last_updated=last_updated, schedule_changed=last_change)
    fingerprint = _fingerprint(rich)
    if message_id and entry.get("fingerprint") == fingerprint:
        entry["failures"] = 0
        return None
    # Рендер изменился: либо контент портала (тогда двигаем «Расписание»),
    # либо данные кафедры/день. Штамп «Обновлено» ставим на момент правки.
    if portal_fp != entry.get("portal_fp"):
        last_change = stamp
    last_updated = stamp
    rich = render_post(spec, opd_data, portal, today=today,
                       last_updated=last_updated, schedule_changed=last_change)
    fingerprint = _fingerprint(rich)
    if message_id:
        result = telegram_api.edit_rich_message(
            rich, token=config.TG_BOT_TOKEN,
            chat_id=config.TG_CHANNEL_ID, message_id=int(message_id),
        )
        if not result.get("ok") and "not modified" not in str(result.get("description", "")):
            raise RuntimeError(f"edit extra post {spec['id']} failed: {result}")
    else:
        result = telegram_api.send_rich_message(
            rich, token=config.TG_BOT_TOKEN, chat_id=config.TG_CHANNEL_ID,
        )
        if not result.get("ok"):
            raise RuntimeError(f"send extra post {spec['id']} failed: {result}")
        message_id = result["result"]["message_id"]
    entry.update({
        "message_id": int(message_id),
        "fingerprint": fingerprint,
        "portal_fp": portal_fp,
        "rendered_at": last_updated,
        "last_change": last_change,
        "failures": 0,
    })
    return int(message_id)


def sync(opd_data: dict | None, *, today: dt.date | None = None) -> dict:
    """Привести отслеживаемые посты к текущим данным портала и кафедры.

    Без данных кафедры (None) ничего не трогаем: пост остаётся как есть.
    Сбой одного поста не мешает остальным; в личку алерт уходит только после
    трёх подряд неудачных циклов по посту. Возвращает {spec_id: message_id}
    по постам, которые реально менялись.
    """
    if not opd_data:
        return {}
    state = _read_state()
    updated: dict[str, int] = {}
    errors: list[str] = []
    changed = False
    now = _now_msk()
    today = today or now.date()
    for spec in _load_specs():
        entry = dict(state.get(spec["id"]) or {})
        try:
            message_id = _sync_one(spec, opd_data, entry, today=today, now=now)
        except Exception as exc:  # noqa: BLE001
            entry["failures"] = int(entry.get("failures") or 0) + 1
            state[spec["id"]] = entry
            changed = True
            if entry["failures"] >= _FAIL_ALERT_THRESHOLD:
                errors.append(f"{spec['id']}: {exc}")
            continue
        if message_id is not None:
            updated[spec["id"]] = message_id
        if entry != (state.get(spec["id"]) or {}):
            state[spec["id"]] = entry
            changed = True
    if changed:
        _write_state(state)
    if errors:
        raise RuntimeError("; ".join(errors))
    return updated
