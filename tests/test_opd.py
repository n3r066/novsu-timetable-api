"""ОПД по виртуальным группам: разбор двух Google-файлов, кэш и вид дня."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import opd

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TODAY = dt.date(2026, 9, 17)


def _csv() -> str:
    return (FIXTURES / "opd_members.csv").read_text(encoding="utf-8")


def _html() -> str:
    return (FIXTURES / "opd_doc.html").read_text(encoding="utf-8")


def _index() -> str:
    return (FIXTURES / "opd_index.html").read_text(encoding="utf-8")


def _data() -> dict:
    return opd.build_data(_csv(), _html(), group="6381", today=TODAY, fetched_at="2026-09-17T10:00:00+03:00",
                          index_html=_index())


def test_index_maps_groups_to_institute_headings():
    institutes = opd.parse_index_institutes(_index())
    assert institutes["6311"]["short"] == "ПТИ" and institutes["6311"]["inst_id"] == "2159922"
    assert institutes["6001"]["short"] == "ИЭ" and institutes["6381"]["short"] == "ИЭ"
    assert opd.parse_index_institutes("") == {}


def test_mates_come_from_other_groups_with_institutes():
    data = _data()
    assert data["institutes_known"] is True
    assert {(m["vg"], m["last_name"], m["institute"]) for m in data["mates"]} == {
        ("101", "Лебедев", "ИЭ"), ("118", "Морозов", "ПТИ"), ("118", "Орлова", "ИЭ"), ("117", "Соколов", "ПТИ"),
    }
    view = opd.day_view(data, TODAY)
    zhukova = next(row for row in view["rows"] if row["vg"] == "118")
    assert [(m["student"], m["group"], m["institute"]) for m in zhukova["mates"]] == [
        ("Орлова В. П.", "6001", "ИЭ"), ("Морозов И. И.", "6311", "ПТИ"),
    ]
    assert next(row for row in view["rows"] if row["vg"] == "105")["mates"] == []
    # Без индекса портала состав есть, но институты пустые.
    plain = opd.build_data(_csv(), _html(), group="6381", today=TODAY)
    assert plain["institutes_known"] is False
    assert {m["institute"] for m in plain["mates"]} == {""}


def test_old_cache_version_is_ignored_entirely(monkeypatch, tmp_path):
    cache = tmp_path / "opd_cache.json"
    monkeypatch.setattr(opd, "_cache_path", lambda: cache)
    monkeypatch.setattr(opd.config, "OPD_ENABLED", True)
    stale = _data()
    stale["version"] = 1
    stale["last_attempt"] = "2026-09-17T09:59:00+03:00"  # минуту назад — но это старый формат
    cache.write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(opd, "_download", _fake_download(calls, fail=True))
    assert opd.load_opd(dt.datetime(2026, 9, 17, 10, 0, tzinfo=opd.MOSCOW), group="6381") is None
    assert len(calls) == 1  # попытка обновления сделана, старый кэш не сдержал её


def test_members_only_default_group_and_title_case():
    data = _data()
    names = [member["last_name"] for member in data["members"]]
    assert len(names) == 10
    assert "Лебедев" not in names and "Морозов" not in names  # другие академические группы
    hyphenated = next(member for member in data["members"] if member["vg"] == "121")
    assert hyphenated["last_name"] == "Зайцев-Петров"
    assert hyphenated["patronymic"] == ""  # «.» в таблице — отчества нет
    assert opd.student_label(hyphenated) == "Зайцев-Петров И."


def test_sessions_follow_date_subcolumns():
    data = _data()
    thursday = {(s["vg"], s["block_start"]): s for s in data["sessions"] if s["date"] == "2026-09-17"}
    # Слоты ОПД часовые, как в строке времени документа, а не пары 14:00–15:45.
    assert thursday[("117", "14:00")]["block_end"] == "15:00"
    assert thursday[("118", "16:00")]["block_end"] == "17:00"
    assert thursday[("105", "14:00")]["block_end"] == "15:00"  # «14.00-15.00» через дефис
    assert thursday[("118", "16:00")]["room"] == "402"
    assert thursday[("118", "16:00")]["place"] == "ИЭ, Антоново"
    assert thursday[("118", "16:00")]["building"] == "Антоново"
    assert thursday[("118", "16:00")]["teacher"] == "Трезорова Ольга Юрьевна"
    assert thursday[("101", "14:00")]["cancelled"] and thursday[("102", "16:00")]["cancelled"]
    # «с 15:00» сдвигает часовой слот: 15:00–16:00, пометка остаётся видимой.
    assert thursday[("121", "15:00")]["note"] == "с 15:00"
    assert thursday[("121", "15:00")]["block_end"] == "16:00"
    assert not thursday[("121", "15:00")]["cancelled"]
    assert ("121", "14:00") not in thursday
    # Потерянная закрывающая скобка и «ауд.503» без пробела читаются.
    assert thursday[("122", "16:00")]["room"] == "503"
    assert thursday[("122", "16:00")]["place"] == "ПИ, ул. Псковская, 3"
    assert thursday[("122", "16:00")]["building"] == "ул. Псковская, 3"
    assert thursday[("105", "14:00")]["room"] == "106хк"
    assert thursday[("105", "14:00")]["building"] == "ул. Советской Армии, 7"
    # Первая шапка: ПТИ на Б. Санкт-Петербургской.
    assert thursday[("101", "14:00")]["building"] == "Б. Санкт-Петербургская, 41"


def test_second_header_typo_is_repaired_from_first_header():
    data = _data()
    dates = {s["date"] for s in data["sessions"]}
    assert "2026-10-24" not in dates
    assert dates == {"2026-09-10", "2026-09-17", "2026-09-24", "2026-10-01"}
    sep24 = {s["vg"] for s in data["sessions"] if s["date"] == "2026-09-24"}
    assert {"107", "108"} <= sep24


def test_empty_cells_produce_no_sessions():
    data = _data()
    oct1 = {s["vg"] for s in data["sessions"] if s["date"] == "2026-10-01"}
    assert "107" not in oct1 and "108" not in oct1
    assert {"101", "102", "117", "118", "121", "122"} <= oct1


def test_year_inferred_closest_to_today():
    grids = opd.parse_doc_tables(_html())
    spring = opd.parse_sessions(grids, today=dt.date(2027, 1, 20))
    assert {s["date"][:4] for s in spring} == {"2026"}
    explicit = opd.parse_sessions([[["ДАТА", "05.03.27", "05.03.27"], ["Ионова Софья Валерьевна (ауд. 1, ИЭ, Антоново)", "101 ВГ", "102 ВГ"]]], today=TODAY)
    assert {s["date"] for s in explicit} == {"2027-03-05"}
    assert {(s["block_start"], s["block_end"]) for s in explicit} == {("14:00", "15:00"), ("16:00", "17:00")}  # без строки времени — по порядку подколонок
    single = opd.parse_sessions([[["ДАТА", "05.03.27"], ["время", "15.30"], ["Ионова Софья Валерьевна (ауд. 1, ИЭ, Антоново)", "101 ВГ"]]], today=TODAY)
    assert (single[0]["block_start"], single[0]["block_end"]) == ("15:30", "16:30")  # одно время → часовой слот


def status_next(view: dict, vg: str) -> str:
    return next(row for row in view["rows"] if row["vg"] == vg)["next_date"]


def test_day_view_alphabetical_with_statuses():
    view = opd.day_view(_data(), TODAY)
    assert [row["student"] for row in view["rows"]] == [
        "Алексеева А. П.", "Борисов Г. О.", "Волкова М. И.", "Григорьев Т. С.", "Дмитриева О. В.",
        "Елисеев А. Д.", "Жукова К. А.", "Зайцев-Петров И.", "Иванова Д. Ю.", "Кузнецов Н. Р.",
    ]
    status = {row["student"]: row["status"] for row in view["rows"]}
    assert status["Алексеева А. П."] == "cancelled"
    assert status["Волкова М. И."] == "free"
    assert status["Жукова К. А."] == "session"
    assert view["counts"] == {"session": 6, "cancelled": 2, "free": 2}
    zhukova = next(row for row in view["rows"] if row["vg"] == "118")
    assert (zhukova["block_start"], zhukova["block_end"], zhukova["room"]) == ("16:00", "17:00", "402")
    assert zhukova["teacher_short"] == "Трезорова О. Ю."
    late = next(row for row in view["rows"] if row["vg"] == "121")
    assert late["note"] == "с 15:00"
    assert (late["block_start"], late["block_end"]) == ("15:00", "16:00")
    # Через неделю: у идущих сегодня следующая дата 01.10, у свободных — 24.09,
    # у отменённых — их ближайшее неотменённое занятие.
    assert zhukova["next_date"] == "2026-10-01"
    assert status_next(view, "103") == "2026-09-24"
    assert status_next(view, "101") == "2026-10-01"
    last = opd.day_view(_data(), dt.date(2026, 10, 1))
    assert all(row["next_date"] == "" for row in last["rows"])
    assert view["sources"]["doc"].startswith("https://docs.google.com/document/")


def test_day_view_none_for_dates_outside_document():
    data = _data()
    assert opd.day_view(data, dt.date(2026, 9, 18)) is None
    assert opd.day_view(None, TODAY) is None
    assert opd.day_view({"members": [], "sessions": []}, TODAY) is None


def test_late_ends_cover_second_block_of_group_members():
    ends = opd.late_ends(_data())
    assert ends[TODAY] == dt.time(17, 0)
    assert ends[dt.date(2026, 9, 10)] == dt.time(17, 0)  # ВГ 143 в слоте 16:00
    assert ends[dt.date(2026, 10, 1)] == dt.time(17, 0)
    assert opd.late_ends(None) == {}


def test_presentation_digest_tracks_day_rows():
    data = _data()
    week = [TODAY + dt.timedelta(days=offset) for offset in range(-3, 3)]
    first = opd.presentation_digest(data, week)
    assert first
    assert opd.presentation_digest(None, week) == ""
    assert opd.presentation_digest(data, [dt.date(2026, 12, 1)]) == ""
    changed = json.loads(json.dumps(data))
    for session in changed["sessions"]:
        if session["vg"] == "118" and session["date"] == "2026-09-17":
            session["room"] = "401"
    assert opd.presentation_digest(changed, week) != first


def test_place_hint_drops_building_and_house_number():
    assert opd.place_hint("ИЭ, Антоново", "Антоново") == "ИЭ"
    assert opd.place_hint("рядом с ХТИ, ул. Сов. Армии, 7", "ул. Советской Армии, 7") == "ХТИ"
    assert opd.place_hint("ПТИ, Б.Санкт-Петерб., 41", "Б. Санкт-Петербургская, 41") == "ПТИ"
    assert opd.place_hint("", "Антоново") == ""


def _fake_download(calls: list[str], *, fail: bool = False):
    def _download(url: str) -> str:
        calls.append(url)
        if fail:
            raise RuntimeError("offline")
        return _csv() if "spreadsheets" in url else _html()
    return _download


def test_load_opd_caches_by_ttl_and_keeps_stale_on_failure(monkeypatch, tmp_path):
    cache = tmp_path / "opd_cache.json"
    monkeypatch.setattr(opd, "_cache_path", lambda: cache)
    monkeypatch.setattr(opd.config, "OPD_ENABLED", True)
    monkeypatch.setattr(opd.config, "OPD_CACHE_TTL_S", 3600.0)
    monkeypatch.setattr(opd.config, "OPD_RETRY_DELAY_S", 600.0)
    calls: list[str] = []
    monkeypatch.setattr(opd, "_download", _fake_download(calls))
    t0 = dt.datetime(2026, 9, 17, 9, 0, tzinfo=opd.MOSCOW)

    first = opd.load_opd(t0, group="6381")
    assert first and len(first["members"]) == 10 and len(calls) == 2
    assert cache.exists()

    again = opd.load_opd(t0 + dt.timedelta(minutes=30), group="6381")
    assert again["fetched_at"] == first["fetched_at"] and len(calls) == 2  # свежий кэш — без сети

    # TTL истёк, Google недоступен: остаётся прошлый кэш, ошибка записана.
    monkeypatch.setattr(opd, "_download", _fake_download(calls, fail=True))
    stale = opd.load_opd(t0 + dt.timedelta(hours=2), group="6381")
    assert stale["fetched_at"] == first["fetched_at"] and len(calls) == 3
    assert "offline" in json.loads(cache.read_text(encoding="utf-8"))["last_error"]

    # Повтор раньше OPD_RETRY_DELAY_S — без обращения к сети.
    opd.load_opd(t0 + dt.timedelta(hours=2, minutes=5), group="6381")
    assert len(calls) == 3

    # После паузы — новая попытка и обновление.
    monkeypatch.setattr(opd, "_download", _fake_download(calls))
    fresh = opd.load_opd(t0 + dt.timedelta(hours=3), group="6381")
    assert len(calls) == 5 and fresh["fetched_at"] != first["fetched_at"]
    assert json.loads(cache.read_text(encoding="utf-8"))["last_error"] == ""


def test_load_opd_without_cache_and_network_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(opd, "_cache_path", lambda: tmp_path / "opd_cache.json")
    monkeypatch.setattr(opd.config, "OPD_ENABLED", True)
    monkeypatch.setattr(opd, "_download", _fake_download([], fail=True))
    assert opd.load_opd(dt.datetime(2026, 9, 17, 9, 0, tzinfo=opd.MOSCOW), group="6381") is None


def test_load_opd_disabled_by_config(monkeypatch):
    monkeypatch.setattr(opd.config, "OPD_ENABLED", False)
    assert opd.load_opd() is None


def test_build_data_rejects_empty_sources():
    import pytest

    with pytest.raises(ValueError):
        opd.build_data(_csv(), "<html><body>нет таблиц</body></html>", group="6381", today=TODAY)
    with pytest.raises(ValueError):
        opd.build_data(_csv(), _html(), group="9999", today=TODAY)
