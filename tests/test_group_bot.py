"""Тесты эфемерного группового бота (group_bot.py).

Эфемерность нативная (Bot API 10.3): ответ адресуется автору команды через
ephemeral_message_parameters.receiver_user_id и не удаляется вручную.
Дневной ответ — rich-раздел как в закрепе канала + готовый скрин из кеша.
"""

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
    # скрины-кеш закрепа и ОПД не трогаем: нет сети, нет реальных стейтов
    monkeypatch.setattr(group_bot, "SCREENS_FILE", tmp_path / "dashboard_screens.json")
    monkeypatch.setattr(group_bot.opd_module, "load_opd", lambda *a, **k: None)
    monkeypatch.setattr(group_bot.config, "GROUP_BOT_ANSWER_DELAY_S", 0.0)
    return tmp_path


def _screens(monkeypatch, tmp_path, *labels):
    png = tmp_path / "day_screen.png"
    png.write_bytes(b"\x89PNG fake")
    (tmp_path / "dashboard_screens.json").write_text(
        json.dumps({"items": [{"label": label, "path": str(png)} for label in labels]}),
        encoding="utf-8")
    return png


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
def test_process_update_sends_ephemeral_rich_day_section(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    png = _screens(monkeypatch, tmp_path, "Ср")
    rich_calls = []

    def fake_send(rich_message, *, token, chat_id, files=None, timeout=30,
                  disable_notification=None, extra_payload=None):
        rich_calls.append({
            "rich": rich_message, "files": files, "chat_id": chat_id,
            "extra": extra_payload,
        })
        return {"ok": True, "result": {"message_id": 0, "ephemeral_message_id": 9001}}

    monkeypatch.setattr(group_bot.telegram_api, "send_rich_message", fake_send)
    sent_text = []
    monkeypatch.setattr(group_bot, "api_call", lambda *a, **k: sent_text.append(1) or {"ok": True})
    group_bot.process_update(_update("/today"))
    assert len(rich_calls) == 1 and not sent_text, "текстовая деградация не нужна"
    call = rich_calls[0]
    assert call["chat_id"] == CHAT
    assert call["extra"]["ephemeral_message_parameters"]["receiver_user_id"] == ME
    assert call["files"] == {"day_screenshot_1": png}
    dumped = json.dumps(call["rich"], ensure_ascii=False)
    assert "attach://day_screenshot_1" in dumped
    assert "Среда" in dumped and "История России" in dumped


def test_day_answer_without_screen_has_no_files(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)  # скрины-кеш не записан
    plan = group_bot.plan_response(
        _update("/today"), allowed_chat=CHAT, allowed_user=ME, today=WED)
    assert plan["rich"] is not None
    assert plan["files"] is None
    assert "attach://" not in json.dumps(plan["rich"], ensure_ascii=False)


def test_day_outside_cached_week_is_text_only(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _screens(monkeypatch, tmp_path, "Пн", "Ср")
    plan = group_bot.plan_response(
        _update("/today пн"), allowed_chat=CHAT, allowed_user=ME, today=WED)
    # 28.09 — неделя вне расписания: rich-раздела нет, только текст
    assert "rich" not in plan
    assert plan["kind"] == "reply"


def test_rich_failure_falls_back_to_ephemeral_text(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _screens(monkeypatch, tmp_path, "Ср")
    monkeypatch.setattr(
        group_bot.telegram_api, "send_rich_message",
        lambda *a, **k: {"ok": False, "description": "Bad Request: rich message failed"})
    attempts = []

    def fake_call(token, method, payload, **kw):
        attempts.append(dict(payload))
        return {"ok": True, "result": {"message_id": 0, "ephemeral_message_id": 4242}}

    monkeypatch.setattr(group_bot, "api_call", fake_call)
    group_bot.process_update(_update("/today"))
    assert len(attempts) == 1, "rich упал — один текстовый ответ"
    payload = attempts[0]
    assert payload["parse_mode"] == "HTML"
    assert payload["ephemeral_message_parameters"]["receiver_user_id"] == ME
    assert payload["reply_to_message_id"] == 100
    assert "<b>СРЕДА" in payload["text"]


def test_week_command_is_ephemeral_rich_channel_copy(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _screens(monkeypatch, tmp_path, "Ср", "Чт")
    # фиксированное «сейчас», чтобы живые дни не зависели от реального часа
    monkeypatch.setattr(group_bot, "_dashboard_now",
                        lambda d: dt.datetime(2026, 9, 23, 12, 0, tzinfo=group_bot.MSK))
    rich_calls = []

    def fake_send(rich_message, *, token, chat_id, files=None, timeout=30,
                  disable_notification=None, extra_payload=None):
        rich_calls.append({"rich": rich_message, "files": files, "extra": extra_payload})
        return {"ok": True, "result": {"message_id": 0, "ephemeral_message_id": 8008}}

    monkeypatch.setattr(group_bot.telegram_api, "send_rich_message", fake_send)
    group_bot.process_update(_update("/timetable"))
    assert len(rich_calls) == 1
    call = rich_calls[0]
    assert call["extra"]["ephemeral_message_parameters"]["receiver_user_id"] == ME
    dumped = json.dumps(call["rich"], ensure_ascii=False)
    assert "НЕДЕЛЯ 4" in dumped and "Среда" in dumped and "Четверг" in dumped
    assert "attach://site_screenshot_1" in dumped
    assert call["files"] == {"site_screenshot_1": tmp_path / "day_screen.png",
                             "site_screenshot_2": tmp_path / "day_screen.png"}


def test_week_text_fallback_is_ephemeral(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        group_bot.telegram_api, "send_rich_message",
        lambda *a, **k: {"ok": False, "description": "Bad Request"})
    attempts = []

    def fake_call(token, method, payload, **kw):
        attempts.append(dict(payload))
        return {"ok": True, "result": {"message_id": 0, "ephemeral_message_id": 7007}}

    monkeypatch.setattr(group_bot, "api_call", fake_call)
    group_bot.process_update(_update("/timetable"))
    payload = attempts[0]
    assert payload["ephemeral_message_parameters"]["receiver_user_id"] == ME
    assert "НЕДЕЛЯ 4" in payload["text"]


def test_anonymous_admin_gets_public_text(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    attempts = []

    def fake_call(token, method, payload, **kw):
        attempts.append(dict(payload))
        return {"ok": True, "result": {"message_id": 12}}

    monkeypatch.setattr(group_bot, "api_call", fake_call)
    plan = group_bot.plan_response(
        _update("/timetable", anonymous=True), allowed_chat=CHAT, allowed_user=ME, today=WED)
    assert plan["receiver_user_id"] is None
    group_bot._send_text(plan)
    assert "ephemeral_message_parameters" not in attempts[0]
    assert "НЕДЕЛЯ 4" in attempts[0]["text"]


def test_ephemeral_params_ignored_public_leak_is_deleted(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    deleted = []

    def fake_call(token, method, payload, **kw):
        if method == "deleteMessage":
            deleted.append(dict(payload))
        return {"ok": True, "result": {"message_id": 555}}  # ушло публично

    monkeypatch.setattr(group_bot, "api_call", fake_call)
    alerts = []
    monkeypatch.setattr(group_bot, "_alert", lambda text: alerts.append(text))
    plan = group_bot.plan_response(
        _update("/timetable"), allowed_chat=CHAT, allowed_user=ME, today=WED)
    result = group_bot._send_text(plan)
    assert result.get("error") == "ephemeral_ignored"
    assert deleted == [{"chat_id": CHAT, "message_id": 555}]
    assert alerts and "НЕ удалён" not in alerts[0]


def test_send_failure_alerts_owner(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(group_bot, "api_call",
                        lambda *a, **k: {"ok": False, "description": "Too Many Requests"})
    alerts = []
    monkeypatch.setattr(group_bot, "_alert", lambda text: alerts.append(text))
    plan = group_bot.plan_response(
        _update("/timetable"), allowed_chat=CHAT, allowed_user=ME, today=WED)
    group_bot._send_text(plan)
    assert alerts and "Too Many Requests" in alerts[0]


def test_register_commands_chat_scope_ephemeral(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    attempts = []

    def fake_call(token, method, payload, **kw):
        attempts.append((method, payload))
        return {"ok": True}

    monkeypatch.setattr(group_bot, "api_call", fake_call)
    monkeypatch.setattr(group_bot.config, "TG_GROUP_CHAT_ID", CHAT)
    result = group_bot.register_commands()
    assert result["ok"]
    method, payload = attempts[0]
    assert method == "setMyCommands"
    assert payload["scope"] == {"type": "chat", "chat_id": CHAT}
    for command in payload["commands"]:
        assert command["is_ephemeral"] is True
    assert {c["command"] for c in payload["commands"]} == {"timetable", "today", "tomorrow", "opd"}


FAKE_OPD_VIEW = {
    "date": "2026-09-24",
    "group": "6381",
    "rows": [
        {
            "student": "Вересков В. Ю.", "last_name": "Вересков", "vg": "107",
            "status": "session", "next_date": "2026-10-01",
            "block_start": "14:00", "block_end": "15:00",
            "room": "106хк", "place": "рядом с ХТИ, ул. Сов. Армии, 7",
            "building": "ул. Советской Армии, 7",
            "teacher": "Мисько Э. Р.", "teacher_short": "Мисько Э. Р.", "note": "",
            "mates": [
                {"student": "Абрамов А. А.", "group": "6281", "institute": "ИГУМ"},
                {"student": "Борисов Б. Б.", "group": "6282", "institute": "ИГУМ"},
            ],
        },
        {
            "student": "Азизова А. А.", "last_name": "Азизова", "vg": "112",
            "status": "cancelled", "next_date": "2026-10-08", "mates": [],
        },
        {
            "student": "Гусев Г. Г.", "last_name": "Гусев", "vg": "103",
            "status": "free", "next_date": "2026-10-01", "mates": [],
        },
    ],
    "counts": {"session": 1, "cancelled": 1, "free": 1},
    "fetched_at": "2026-09-22T20:00:00+03:00",
    "sources": {"sheet": "https://example.com/sheet", "doc": "https://example.com/doc"},
}


def _opd_env(monkeypatch, view):
    monkeypatch.setattr(group_bot.opd_module, "load_opd", lambda *a, **k: {"stub": True})
    monkeypatch.setattr(
        group_bot.opd_module, "day_view",
        lambda opd, date: view if view and date == dt.date(2026, 9, 24) else None)


def test_opd_auto_finds_next_opd_date_and_reuses_channel_block(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _opd_env(monkeypatch, FAKE_OPD_VIEW)
    plan = group_bot.plan_response(_update("/opd"), allowed_chat=CHAT,
                                   allowed_user=ME, today=WED)
    assert plan["kind"] == "reply"
    assert plan["receiver_user_id"] == ME
    assert plan["command_message_id"] == 100
    # rich: свёрнутый раздел без источников/эмодзи/отмен + строка «Занятий
    # не будет» отдельным блоком после него
    blocks = plan["rich"]["rich_message"]["blocks"]
    assert len(blocks) == 2
    assert blocks[0] == group_bot._opd_section(
        FAKE_OPD_VIEW, sources=False, markers=False, cancelled=False,
        open_mates=False)
    # составы ВГ свёрнуты по умолчанию: сам блок «Вместе в ВГ …» без is_open
    # (институты внутри остаются раскрытыми — один клик до полной таблицы)
    import format as fmt
    mates = fmt._opd_mates_blocks(FAKE_OPD_VIEW["rows"][0], open_mates=False)
    assert "is_open" not in mates[0] and "Вместе в ВГ 107" in json.dumps(
        mates[0], ensure_ascii=False)
    assert mates[0]["blocks"] and "is_open" in mates[0]["blocks"][0]
    assert blocks[1] == group_bot._opd_cancelled_line(FAKE_OPD_VIEW, markers=False)
    section_dump = json.dumps(blocks[0], ensure_ascii=False)
    outside_dump = json.dumps(blocks[1], ensure_ascii=False)
    assert "📍" not in section_dump and "❌" not in outside_dump
    assert "Источники" not in section_dump and "example.com" not in section_dump
    assert "Занятий не будет" not in section_dump
    assert "Занятий не будет" in outside_dump
    assert "Азизова А. А. (ВГ 112)" in outside_dump
    # канал (дефолт) остаётся с маркерами, источниками и отменами внутри
    channel_dump = json.dumps(
        [group_bot._opd_section(FAKE_OPD_VIEW)], ensure_ascii=False)
    assert "📍" in channel_dump and "❌" in channel_dump
    assert "example.com" in channel_dump
    assert "Занятий не будет" in channel_dump
    # а в канале составы ВГ по-прежнему раскрыты
    assert '"is_open"' in channel_dump
    assert plan.get("files") is None
    text = plan["text"]
    assert "ОПД · ПО ВИРТУАЛЬНЫМ ГРУППАМ · 24.09 · идут 1 из 3" in text
    assert "Вересков В. Ю." in text and "ВГ 107" in text
    assert "📍" not in text and "❌" not in text
    assert "<b>Занятий не будет:</b> Азизова А. А. (ВГ 112)" in text


def test_opd_send_anchors_reply_to_command(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _opd_env(monkeypatch, FAKE_OPD_VIEW)
    rich_calls = []

    def fake_send(rich_message, *, token, chat_id, files=None, timeout=30,
                  disable_notification=None, extra_payload=None):
        rich_calls.append({"rich": rich_message, "extra": extra_payload})
        return {"ok": True, "result": {"message_id": 0, "ephemeral_message_id": 77}}

    monkeypatch.setattr(group_bot.telegram_api, "send_rich_message", fake_send)
    group_bot.process_update(_update("/opd"))
    extra = rich_calls[0]["extra"]
    assert extra["ephemeral_message_parameters"]["receiver_user_id"] == ME
    assert extra["reply_to_message_id"] == 100


def test_opd_explicit_date_without_opd_is_honest_text(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _opd_env(monkeypatch, FAKE_OPD_VIEW)
    plan = group_bot.plan_response(_update("/opd пятница"), allowed_chat=CHAT,
                                   allowed_user=ME, today=WED)
    assert plan["text"] == "На 25.09 ОПД нет."
    assert "rich" not in plan


def test_opd_no_opd_found_in_scan(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _opd_env(monkeypatch, None)
    plan = group_bot.plan_response(_update("/opd"), allowed_chat=CHAT,
                                   allowed_user=ME, today=WED)
    assert plan["text"] == "Ближайшие две недели ОПД не найдено."


def test_opd_without_cached_data_is_stub(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)  # load_opd -> None
    plan = group_bot.plan_response(_update("/опд"), allowed_chat=CHAT,
                                   allowed_user=ME, today=WED)
    assert "не подгрузилось" in plan["text"]


def test_text_reply_retry_without_anchor_when_command_gone(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _screens(monkeypatch, tmp_path, "Ср")
    monkeypatch.setattr(
        group_bot.telegram_api, "send_rich_message",
        lambda *a, **k: {"ok": False, "description": "Bad Request: rich message failed"})
    attempts = []

    def fake_call(token, method, payload, **kw):
        attempts.append(dict(payload))
        if "reply_to_message_id" in payload:
            return {"ok": False, "error_code": 400,
                    "description": "Bad Request: message to be replied not found"}
        return {"ok": True, "result": {"message_id": 0, "ephemeral_message_id": 99}}

    monkeypatch.setattr(group_bot, "api_call", fake_call)
    group_bot.process_update(_update("/today"))
    assert len(attempts) == 2, "первый send с reply падает — ретрай без привязки"
    assert attempts[0]["reply_to_message_id"] == 100
    assert "reply_to_message_id" not in attempts[1]
    assert attempts[1]["ephemeral_message_parameters"]["receiver_user_id"] == ME


def test_process_update_waits_answer_delay(monkeypatch, tmp_path):
    """Ответ из кеша обгоняет подтверждение отправки команды и рисуется над
    ней — поэтому бот выдерживает паузу перед ответом."""
    _env(monkeypatch, tmp_path)
    _screens(monkeypatch, tmp_path, "Ср")
    sleeps = []
    monkeypatch.setattr(group_bot.time, "sleep", sleeps.append)
    monkeypatch.setattr(group_bot.config, "GROUP_BOT_ANSWER_DELAY_S", 1.5)
    monkeypatch.setattr(
        group_bot.telegram_api, "send_rich_message",
        lambda *a, **k: {"ok": True, "result": {"message_id": 0, "ephemeral_message_id": 5}})
    group_bot.process_update(_update("/today"))
    assert sleeps == [1.5]
