"""Тесты эфемерного группового бота (group_bot.py)."""

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import group_bot

CHAT = -1004446620031
ME = 7912629458

DATA = {
    "schedule": {"days": {
        "Среда": [
            {"number": 1, "time": "9:00 10:00", "raw_time": "9:00 10:00",
             "subject_raw": "(пр.) История России", "subject": "(пр.) История России",
             "room": "ДОТ", "teacher": "Миненко Трофим Сергеевич",
             "delivery_mode": "remote_or_hybrid", "location": "—"},
            {"number": 2, "time": "14:00 15:00", "raw_time": "14:00 15:00",
             "subject_raw": "(лек./пр.) Сервисная деятельность",
             "subject": "(лек./пр.) Сервисная деятельность",
             "room": "418", "teacher": "Ефимов Олег Николаевич",
             "delivery_mode": "in_person", "location": "ИГУМ, Антоново"},
            {"number": 3, "time": "17:00 18:00", "raw_time": "17:00 18:00 19:00",
             "subject_raw": "(пр.) Основы российской государственности",
             "subject": "(пр.) Основы российской государственности",
             "room": "1331", "teacher": "Кузина Ксения Александровна",
             "delivery_mode": "in_person", "location": "ИГУМ, Антоново"},
        ],
        "Четверг": [
            {"number": 1, "time": "9:00 10:00", "raw_time": "9:00 10:00",
             "subject_raw": "(пр.) География туризма", "subject": "(пр.) География туризма",
             "room": "418", "teacher": "—",
             "delivery_mode": "in_person", "location": "Антоново"},
        ],
    }},
    "weeks": [{"week": 4, "half": "bottom", "start": "21.09.2026", "end": "26.09.2026"}],
}

WED = dt.date(2026, 9, 23)


def _update(text, user=ME, chat=CHAT, ctype="supergroup", message_id=100, anonymous=False):
    msg = {"message_id": message_id, "text": text, "chat": {"id": chat, "type": ctype}}
    if anonymous:
        msg["from"] = None
        msg["sender_chat"] = {"id": chat, "type": ctype}
    else:
        msg["from"] = {"id": user}
    return {"update_id": 1, "message": msg}


def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(group_bot, "STATE_FILE", tmp_path / "last_parsed.json")
    (tmp_path / "last_parsed.json").write_text(json.dumps(DATA), encoding="utf-8")
    return tmp_path


def test_today_msk_uses_moscow_not_server_utc():
    utc = dt.timezone.utc
    # 23:30 UTC вторника = 02:30 МСК среды — серверный date.today() врёт.
    assert group_bot.today_msk(dt.datetime(2026, 9, 22, 23, 30, tzinfo=utc)) == dt.date(2026, 9, 23)
    assert group_bot.today_msk(dt.datetime(2026, 9, 22, 10, 0, tzinfo=utc)) == dt.date(2026, 9, 22)


def test_extract_command_variants():
    assert group_bot.extract_command("/today@novsutimetablebot") == ("today", "")
    assert group_bot.extract_command("/timetable среда") == ("timetable", "среда")
    assert group_bot.extract_command("/today@otherbot") is None
    assert group_bot.extract_command("просто текст") is None
    assert group_bot.extract_command(None) is None


def test_resolve_target_words_and_weekdays():
    today = dt.date(2026, 9, 23)  # среда
    assert group_bot.resolve_target("сегодня", today) == today
    assert group_bot.resolve_target("завтра", today) == today + dt.timedelta(days=1)
    assert group_bot.resolve_target("вся", today) == "week"
    assert group_bot.resolve_target("ср", today) == today
    assert group_bot.resolve_target("пн", today) == today + dt.timedelta(days=5)  # ближайший понедельник
    assert group_bot.resolve_target("фигня", today) is None


def test_lesson_type_extracted_from_subject():
    kind, subject = group_bot._lesson_parts({"subject_raw": "(лек./пр.) Сервисная деятельность"})
    assert kind == "ЛЕК./ПР." and subject == "Сервисная деятельность"
    kind, subject = group_bot._lesson_parts({"subject": "Без типа\nзаметка"})
    assert kind == "" and subject == "Без типа"


def test_today_is_cutout_with_channel_caption(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    plan = group_bot.plan_response(
        _update("/today@novsutimetablebot"), allowed_chat=CHAT, allowed_user=ME, today=WED)
    assert plan["kind"] == "reply"
    text = plan["text"]
    assert "<b>СРЕДА · 23.09.2026</b>" in text
    assert "3 пары" in text and "1 ДОТ" in text
    assert "<b>1.</b> <b>09:00–10:45</b> <b>ПР.</b> История России" in text
    assert "Миненко Трофим Сергеевич · ДОТ" in text
    assert "<b>17:00–18:45 + 19:00–19:45</b>" in text
    assert "Кузина Ксения Александровна · ауд. 1331 · ИГУМ, Антоново" in text
    # вырезка чистая: ни картинок, ни мусорных тире
    assert "ауд. —" not in text and "\n—\n" not in text


def test_tomorrow_picks_thursday(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    plan = group_bot.plan_response(
        _update("/завтра"), allowed_chat=CHAT, allowed_user=ME, today=WED)
    assert "<b>ЧЕТВЕРГ · 24.09.2026</b>" in plan["text"]
    assert "География туризма" in plan["text"]
    # тире-заглушка препода не должна светиться
    assert "· —" not in plan["text"] and "\n—" not in plan["text"]


def test_week_lists_days_with_header(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    plan = group_bot.plan_response(
        _update("/timetable"), allowed_chat=CHAT, allowed_user=ME, today=WED)
    text = plan["text"]
    assert "<b>НЕДЕЛЯ 4 · нижняя · 21.09.2026—26.09.2026</b>" in text
    assert "<b>ПОНЕДЕЛЬНИК · 21.09.2026</b>" not in text  # пар нет — день пропускается
    assert "<b>СРЕДА · 23.09.2026</b>" in text
    assert "<b>ЧЕТВЕРГ · 24.09.2026</b>" in text


def test_missing_parsed_data_honest_stub(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(group_bot, "STATE_FILE", tmp_path / "absent.json")
    plan = group_bot.plan_response(
        _update("/today"), allowed_chat=CHAT, allowed_user=ME, today=WED)
    assert plan["kind"] == "reply"
    assert "не подгрузилось" in plan["text"]


def test_private_chat_and_foreigners_are_ignored(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    kw = {"allowed_chat": CHAT, "allowed_user": ME, "today": WED}
    assert group_bot.plan_response(
        _update("/today", chat=ME, ctype="private"), **kw)["kind"] == "ignore"
    assert group_bot.plan_response(
        _update("/today", chat=-1009999999), **kw)["kind"] == "ignore"
    assert group_bot.plan_response(
        _update("/today", user=123456789), **kw)["kind"] == "ignore"
    assert group_bot.plan_response(
        _update("/unknowncmd"), **kw)["kind"] == "ignore"


def test_anonymous_admin_is_allowed(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    plan = group_bot.plan_response(
        _update("/сегодня", anonymous=True), allowed_chat=CHAT, allowed_user=ME, today=WED)
    assert plan["kind"] == "reply"


def test_main_refuses_to_start_without_group_chat_id(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(group_bot.config, "TG_GROUP_CHAT_ID", None)
    assert group_bot.main([]) == 1


def test_process_update_sends_text_and_schedules_delete(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    calls = {"sent": [], "deleted": []}
    monkeypatch.setattr(group_bot, "api_call", lambda token, method, payload, **kw: (
        calls["sent"].append((method, payload)) if method == "sendMessage"
        else calls["deleted"].append((method, payload))
    ) or {"ok": True, "result": {"message_id": 501}})
    timers = []
    monkeypatch.setattr(group_bot, "timer_factory",
                        lambda delay, worker: timers.append((delay, worker)) or
                        type("T", (), {"start": lambda self: None})())
    group_bot.process_update(_update("/today", message_id=777))
    method, payload = calls["sent"][0]
    assert method == "sendMessage"
    assert payload["parse_mode"] == "HTML"
    assert payload["reply_to_message_id"] == 777
    assert "<b>СРЕДА" in payload["text"]
    assert len(timers) == 1
    delay, worker = timers[0]
    assert delay == group_bot.TTL_S
    worker()
    deleted_ids = {p["message_id"] for m, p in calls["deleted"] if m == "deleteMessage"}
    assert deleted_ids == {501, 777}, "удаляются и ответ, и команда"


def test_send_retries_without_reply_when_command_gone(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    attempts = []

    def fake_call(token, method, payload, **kw):
        attempts.append(dict(payload))
        if "reply_to_message_id" in payload:
            return {"ok": False, "description": "Bad Request: message to be replied not found"}
        return {"ok": True, "result": {"message_id": 601}}

    monkeypatch.setattr(group_bot, "api_call", fake_call)
    monkeypatch.setattr(group_bot, "timer_factory",
                        lambda delay, worker: type("T", (), {"start": lambda self: None})())
    plan = group_bot.plan_response(
        _update("/today", message_id=888), allowed_chat=CHAT, allowed_user=ME, today=WED)
    sent = group_bot._send_text(plan)
    assert sent == [601]
    assert len(attempts) == 2
    assert "reply_to_message_id" not in attempts[1]
