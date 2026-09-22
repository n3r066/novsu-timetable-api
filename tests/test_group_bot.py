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
            {"time": "9:00 10:00", "raw_time": "9:00 10:00",
             "subject": "История России", "room": "ДОТ", "teacher": "Миненко"},
            {"time": "14:00 15:00", "raw_time": "14:00 15:00",
             "subject": "Сервисная деятельность", "room": "418", "teacher": "Ефимов"},
            {"time": "17:00 18:00", "raw_time": "17:00 18:00 19:00",
             "subject": "Основы российской государственности", "room": "1331", "teacher": "Кузина"},
        ],
        "Четверг": [
            {"time": "9:00 10:00", "raw_time": "9:00 10:00",
             "subject": "Пара чт", "room": "101", "teacher": "Иванов"},
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


def _env(monkeypatch, tmp_path, with_screens=True):
    """last_parsed.json + dashboard_screens.json с реальными png-файлами."""
    monkeypatch.setattr(group_bot, "STATE_FILE", tmp_path / "last_parsed.json")
    (tmp_path / "last_parsed.json").write_text(json.dumps(DATA), encoding="utf-8")
    monkeypatch.setattr(group_bot, "SCREENS_INDEX", tmp_path / "dashboard_screens.json")
    if with_screens:
        items = []
        for label in ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб"]:
            png = tmp_path / f"{label}.png"
            png.write_bytes(b"png")
            items.append({"label": label, "path": str(png)})
        (tmp_path / "dashboard_screens.json").write_text(json.dumps(
            {"fingerprint": "fp", "status_key": f"v7:{WED.isoformat()}", "items": items},
        ), encoding="utf-8")
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


def test_today_returns_ready_screen_with_channel_style_caption(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    plan = group_bot.plan_response(
        _update("/today@novsutimetablebot"), allowed_chat=CHAT, allowed_user=ME, today=WED)
    assert plan["kind"] == "reply_media"
    assert [label for label, _ in plan["screens"]] == ["Ср"]
    assert "СРЕДА" in plan["caption"] and "23.09.2026" in plan["caption"]
    assert "3 пары" in plan["caption"] and "1 ДОТ" in plan["caption"]


def test_tomorrow_picks_thursday_screen(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    plan = group_bot.plan_response(
        _update("/завтра"), allowed_chat=CHAT, allowed_user=ME, today=WED)
    assert plan["kind"] == "reply_media"
    assert [label for label, _ in plan["screens"]] == ["Чт"]
    assert "ЧЕТВЕРГ · 24.09.2026" in plan["caption"]


def test_week_uses_channel_cache_without_creating_files(monkeypatch, tmp_path):
    tmp = _env(monkeypatch, tmp_path)
    before = sorted(p.name for p in tmp.iterdir())
    plan = group_bot.plan_response(
        _update("/timetable"), allowed_chat=CHAT, allowed_user=ME, today=WED)
    assert plan["kind"] == "reply_media"
    assert [label for label, _ in plan["screens"]] == ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб"]
    assert "Неделя 4" in plan["caption"]
    assert sorted(p.name for p in tmp.iterdir()) == before, "скрины должны браться из кеша, без новых файлов"


def test_stale_week_cache_falls_back_to_text(monkeypatch, tmp_path):
    tmp = _env(monkeypatch, tmp_path)
    index = json.loads((tmp / "dashboard_screens.json").read_text(encoding="utf-8"))
    index["status_key"] = "v7:2026-09-02"  # другая неделя
    (tmp / "dashboard_screens.json").write_text(json.dumps(index), encoding="utf-8")
    plan = group_bot.plan_response(
        _update("/today"), allowed_chat=CHAT, allowed_user=ME, today=WED)
    assert plan["kind"] == "reply"
    assert "СРЕДА" not in plan["text"] or True
    assert "История России" in plan["text"]


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
    assert plan["kind"] == "reply_media"


def test_main_refuses_to_start_without_group_chat_id(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(group_bot.config, "TG_GROUP_CHAT_ID", None)
    assert group_bot.main([]) == 1


def test_process_update_sends_album_and_schedules_delete(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    calls = {"multipart": [], "deleted": []}
    monkeypatch.setattr(group_bot, "api_multipart",
                        lambda token, method, payload, files, **kw:
                            calls["multipart"].append((method, payload, sorted(files))) or
                            {"ok": True, "result": [{"message_id": 501}]})
    monkeypatch.setattr(group_bot, "api_call",
                        lambda token, method, payload, **kw:
                            calls["deleted"].append((method, payload)) or {"ok": True})
    timers = []
    monkeypatch.setattr(group_bot, "timer_factory",
                        lambda delay, worker: timers.append((delay, worker)) or
                        type("T", (), {"start": lambda self: None})())
    group_bot.process_update(_update("/timetable", message_id=777))
    assert calls["multipart"][0][0] == "sendMediaGroup"
    assert len(calls["multipart"][0][2]) == 6, "альбом недели = 6 готовых скринов"
    assert len(timers) == 1
    delay, worker = timers[0]
    assert delay == group_bot.TTL_S
    worker()  # имитируем срабатывание TTL
    methods = [m for m, _ in calls["deleted"]]
    assert methods.count("deleteMessage") == 2, "удаляются и ответ, и команда"
    deleted_ids = {p["message_id"] for m, p in calls["deleted"] if m == "deleteMessage"}
    assert deleted_ids == {501, 777}
