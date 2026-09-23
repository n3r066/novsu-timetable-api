"""Date-scoped OPD display exclusions do not rewrite department source data."""
from __future__ import annotations
import copy
import datetime as dt
import json
import opd
import format as fmt
from telegram_api import validate_rich_payload

DATE = dt.date(2026, 9, 24)


def data():
    members = [
        {"last_name": "Абубакаров", "first_name": "Нохчо", "patronymic": "Ризванович", "vg": "104", "group": "6381"},
        {"last_name": "Астраханцева", "first_name": "Анастасия", "patronymic": "Ивановна", "vg": "103", "group": "6381"},
        {"last_name": "Ширин", "first_name": "Ярослав", "patronymic": "Алексеевич", "vg": "116", "group": "6381"},
    ]
    sessions = []
    for day in ("2026-09-24", "2026-10-08"):
        for member in members:
            shirin = member["vg"] == "116"
            sessions.append({"date": day, "vg": member["vg"], "block_start": "14:00" if shirin else "16:00",
                "block_end": "15:00" if shirin else "17:00", "room": "216хк" if shirin else "5414",
                "building": "ул. Советской Армии, 7" if shirin else "Б. Санкт-Петербургская, 41",
                "place": "ХТИ" if shirin else "ПТИ", "teacher": "Шульга Дмитрий Николаевич" if shirin else "Ионова Софья Валерьевна",
                "cancelled": False, "note": ""})
    return {"version": opd.CACHE_VERSION, "group": "6381", "members": members, "sessions": sessions, "mates": []}


def test_requested_thursday_exclusions_preserve_raw_data_and_counts():
    source = data()
    before = copy.deepcopy(source)
    view = opd.day_view(source, DATE)
    assert [r["student"] for r in view["rows"]] == ["Ширин Я. А."]
    assert view["counts"] == {"session": 1, "cancelled": 0, "free": 0}
    assert source == before
    rich = {"rich_message": {"blocks": fmt._opd_section(view)}}
    validate_rich_payload(rich)
    text = json.dumps(rich, ensure_ascii=False)
    assert "Ширин Я. А." in text
    for hidden in ("Абубакаров", "Астраханцева", "Ионова"):
        assert hidden not in text


def test_exclusions_do_not_affect_other_dates_groups_or_namesakes():
    source = data()
    assert len(opd.day_view(source, dt.date(2026, 10, 8))["rows"]) == 3
    source["group"] = "6001"
    assert len(opd.day_view(source, DATE)["rows"]) == 3
    source["group"] = "6381"
    source["members"][0]["first_name"] = "Другой"
    assert len(opd.day_view(source, DATE)["rows"]) == 2


def test_exclusions_apply_to_late_ends_and_presentation_digest(monkeypatch):
    source = data()
    assert opd.late_ends(source)[DATE] == dt.time(15, 0)
    assert opd.late_ends(source)[dt.date(2026, 10, 8)] == dt.time(17, 0)
    digest = opd.presentation_digest(source, [DATE])
    monkeypatch.setattr(opd, "_DISPLAY_EXCLUSIONS", [])
    assert opd.presentation_digest(source, [DATE]) != digest


def test_exclusions_also_hide_cancelled_or_free_rows():
    source = data()
    source["sessions"] = [s for s in source["sessions"] if s["vg"] != "103"]
    for s in source["sessions"]:
        if s["vg"] == "104": s["cancelled"] = True
    view = opd.day_view(source, DATE)
    assert [r["student"] for r in view["rows"]] == ["Ширин Я. А."]
    assert view["counts"] == {"session": 1, "cancelled": 0, "free": 0}


def test_bondarenko_hidden_everywhere_on_2026_09_24():
    source = data()
    source["members"].append({"last_name": "Бондаренко", "first_name": "Матвей",
                              "patronymic": "Кириллович", "vg": "111", "group": "6381"})
    source["sessions"].append({"date": "2026-09-24", "vg": "111", "block_start": "14:00",
        "block_end": "15:00", "room": "303", "building": "Антоново", "place": "ИЭ",
        "teacher": "Михалев Дмитрий Александрович", "cancelled": True, "note": ""})
    view = opd.day_view(source, DATE)
    blob = json.dumps(view, ensure_ascii=False)
    assert "Бондаренко" not in blob and "111" not in blob
    assert view["counts"]["cancelled"] == 0
    rich = {"rich_message": {"blocks": fmt._opd_section(view)}}
    validate_rich_payload(rich)
    assert "Бондаренко" not in json.dumps(rich, ensure_ascii=False)
    assert "Занятий не будет" not in json.dumps(rich, ensure_ascii=False)
