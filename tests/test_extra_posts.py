"""Отслеживаемый пост про человека ОПД вне 6381: полное расписание его
учебной группы с портала плюс раздел ОПД по его виртуальной группе."""
import datetime as dt
import json
from pathlib import Path

import pytest

import extra_posts
from parse import parse_all
from telegram_api import validate_rich_payload

TODAY = dt.date(2026, 9, 3)  # четверг первой недели фикстуры

SPEC = {
    "id": "test-post",
    "intro": "Про Катю и её ВГ 105.",
    "person": {"full_name": "Яковлева Екатерина Сергеевна", "academic_group": "6701-до", "vg": "105"},
    "portal_group": {"group": "6701", "inst_id": "868342", "type": "ДО", "year": "2026",
                     "institute": "ПИ", "route": "ochn"},
}


def _portal():
    html = Path("tests/fixtures/rowspan_time.html").read_text(encoding="utf-8")
    data = parse_all(html)
    data["weeks"] = [{"week": 1, "half": "top", "start": "01.09.2026", "end": "05.09.2026"}]
    return {"url": "https://example.test/6701", "data": data}


def _opd():
    session = {"vg": "105", "block_start": "14:00", "block_end": "15:00", "room": "106хк",
               "place": "рядом с ХТИ", "building": "ул. Советской Армии, 7",
               "teacher": "Мисько Элеонора Романовна", "cancelled": False, "note": ""}
    return {
        "version": 2, "group": "6381",
        "members": [{"last_name": "Бахматова", "first_name": "Ангелина", "patronymic": "Денисовна",
                     "vg": "105", "group": "6381"}],
        "mates": [
            {"vg": "105", "last_name": "Яковлева", "first_name": "Екатерина", "patronymic": "Сергеевна",
             "group": "6701", "institute": "ПИ"},
            {"vg": "105", "last_name": "Шишмаков", "first_name": "Кирилл", "patronymic": "Александрович",
             "group": "6172", "institute": "ИГУМ"},
            {"vg": "105", "last_name": "Рогов", "first_name": "Иван", "patronymic": "Дмитриевич",
             "group": "6233", "institute": "ИГУМ"},
            {"vg": "999", "last_name": "Чужой", "first_name": "И", "patronymic": "И",
             "group": "6000", "institute": "ИЭ"},
        ],
        "sessions": [dict(session, date="2026-09-03"), dict(session, date="2026-09-17")],
        "sources": {"sheet": "https://sheet", "doc": "https://doc", "announcement": "https://ann"},
    }


def test_render_merges_full_group_schedule_with_her_opd():
    rich = extra_posts.render_post(SPEC, _opd(), _portal(), today=TODAY,
                                   last_updated="03.09.2026 10:00", schedule_changed="01.09.2026 09:00")
    validate_rich_payload(rich)
    blob = json.dumps(rich, ensure_ascii=False)
    assert "Про Катю и её ВГ 105." in blob
    assert "Расписание · группа 6701" in blob  # заголовок её учебной группы
    assert "Яковлева Е. С." in blob            # её строка в разделе ОПД
    assert "ВГ 105" in blob and "Мисько Э. Р." in blob
    assert "Шишмаков К. А." in blob and "Бахматова А. Д." in blob  # состав ВГ
    assert "Чужой" not in blob  # чужие ВГ не попадают
    assert "https://sheet" in blob and "https://ann" in blob
    assert "пост обновляется сам" in blob
    assert "Обновлено: 03.09.2026 10:00" in blob and "Расписание: 01.09.2026 09:00" in blob


def test_person_opd_makes_her_the_only_member():
    view = extra_posts._person_opd(_opd(), SPEC["person"])
    assert view["group"] == "6701"
    assert view["members"] == [{"vg": "105", "last_name": "Яковлева", "first_name": "Екатерина",
                                "patronymic": "Сергеевна", "group": "6701"}]
    names = {extra_posts._full_name(m) for m in view["mates"]}
    assert names == {"Бахматова Ангелина Денисовна", "Шишмаков Кирилл Александрович",
                     "Рогов Иван Дмитриевич"}
    assert next(m for m in view["mates"] if m["last_name"] == "Бахматова")["institute"] == "ИЭ"


def _fake_telegram(monkeypatch, send_results):
    calls = {"send": [], "edit": []}

    def fake_send(rich_message, **kwargs):
        validate_rich_payload({"chat_id": 1, **rich_message})
        calls["send"].append(kwargs)
        return send_results.pop(0)

    def fake_edit(rich_message, **kwargs):
        validate_rich_payload({"chat_id": 1, **rich_message})
        calls["edit"].append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(extra_posts.telegram_api, "send_rich_message", fake_send)
    monkeypatch.setattr(extra_posts.telegram_api, "edit_rich_message", fake_edit)
    return calls


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(extra_posts, "STATE_FILE", tmp_path / "extra_posts.json")
    monkeypatch.setattr(extra_posts, "_load_specs", lambda: [SPEC])
    monkeypatch.setattr(extra_posts, "_load_portal", lambda spec: _portal())
    clock = {"now": dt.datetime(2026, 9, 3, 10, 0, tzinfo=extra_posts._MSC)}
    monkeypatch.setattr(extra_posts, "_now_msk", lambda: clock["now"])
    return clock


def test_sync_sends_once_then_stays_silent(sandbox, monkeypatch):
    calls = _fake_telegram(monkeypatch, [{"ok": True, "result": {"message_id": 555}}])
    assert extra_posts.sync(_opd(), today=TODAY) == {"test-post": 555}
    assert len(calls["send"]) == 1 and not calls["edit"]
    assert extra_posts.sync(_opd(), today=TODAY) == {}
    assert len(calls["send"]) == 1 and not calls["edit"]


def test_sync_edits_on_opd_change_without_touching_schedule_stamp(sandbox, monkeypatch):
    calls = _fake_telegram(monkeypatch, [{"ok": True, "result": {"message_id": 555}}])
    extra_posts.sync(_opd(), today=TODAY)
    entry = json.loads(extra_posts.STATE_FILE.read_text())["test-post"]
    changed = _opd()
    changed["sessions"].append(dict(changed["sessions"][0], date="2026-10-01"))
    sandbox["now"] = dt.datetime(2026, 9, 3, 11, 0, tzinfo=extra_posts._MSC)
    assert extra_posts.sync(changed, today=TODAY) == {"test-post": 555}
    assert len(calls["edit"]) == 1 and calls["edit"][0]["message_id"] == 555
    entry2 = json.loads(extra_posts.STATE_FILE.read_text())["test-post"]
    assert entry2["last_change"] == entry["last_change"]      # портал не менялся
    assert entry2["rendered_at"] != entry["rendered_at"]      # а «Обновлено» подвинулось


def test_sync_moves_schedule_stamp_only_on_portal_change(sandbox, monkeypatch):
    calls = _fake_telegram(monkeypatch, [{"ok": True, "result": {"message_id": 555}}])
    extra_posts.sync(_opd(), today=TODAY)
    entry = json.loads(extra_posts.STATE_FILE.read_text())["test-post"]
    portal2 = _portal()
    portal2["data"]["schedule"]["days"]["Суббота"] = list(portal2["data"]["schedule"]["days"]["Четверг"])
    monkeypatch.setattr(extra_posts, "_load_portal", lambda spec: portal2)
    sandbox["now"] = dt.datetime(2026, 9, 3, 12, 0, tzinfo=extra_posts._MSC)
    assert extra_posts.sync(_opd(), today=TODAY) == {"test-post": 555}
    assert len(calls["edit"]) == 1
    entry2 = json.loads(extra_posts.STATE_FILE.read_text())["test-post"]
    assert entry2["last_change"] != entry["last_change"]


def test_sync_alerts_only_after_three_consecutive_failures(sandbox, monkeypatch):
    _fake_telegram(monkeypatch, [])

    def boom(spec):
        raise RuntimeError("portal down")

    monkeypatch.setattr(extra_posts, "_load_portal", boom)
    assert extra_posts.sync(_opd(), today=TODAY) == {}
    assert extra_posts.sync(_opd(), today=TODAY) == {}
    with pytest.raises(RuntimeError, match="portal down"):
        extra_posts.sync(_opd(), today=TODAY)
    entry = json.loads(extra_posts.STATE_FILE.read_text())["test-post"]
    assert entry["failures"] == 3


def test_sync_without_opd_data_is_silent_noop(sandbox, monkeypatch):
    calls = _fake_telegram(monkeypatch, [])
    assert extra_posts.sync(None, today=TODAY) == {}
    assert calls == {"send": [], "edit": []}
