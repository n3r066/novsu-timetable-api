import datetime as dt
import hashlib
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import api

SCHEDULE_HTML = Path("tests/fixtures/rowspan_time.html").read_text(encoding="utf-8")
STUB_HTML = Path("tests/fixtures/stub_with_xls_timestamps.html").read_text(encoding="utf-8")


def test_connect_seeds_annotation_database_once(tmp_path):
    path = tmp_path / "test.sqlite3"
    with api.connect(path) as db:
        assert db.execute("select label from glossary where term='ДОТ'").fetchone()[0].startswith("Дистанционные")
        assert db.execute("select count(*) from annotation_rules").fetchone()[0] >= 6
        db.execute("update glossary set label='custom' where term='ДОТ'")
    with api.connect(path) as db:
        assert db.execute("select label from glossary where term='ДОТ'").fetchone()[0] == "custom"
        assert db.execute("select value from metadata where key='annotations_seed_v1'").fetchone() == ("1",)


def test_connect_initialization_is_thread_safe(tmp_path):
    path = tmp_path / "threaded.sqlite3"

    def open_and_count(_):
        with api.connect(path) as db:
            return db.execute("select count(*) from glossary").fetchone()[0]

    with ThreadPoolExecutor(max_workers=8) as pool:
        counts = list(pool.map(open_and_count, range(16)))
    assert len(set(counts)) == 1
    assert counts[0] > 0


def test_validation_rejects_silent_row_loss():
    data = {"schedule": {"days": {"Четверг": []}}}
    with pytest.raises(api.ParseValidationError, match="physical=2, parsed=0"):
        api.validate(data, SCHEDULE_HTML)


def test_validation_accepts_genuine_portal_stub():
    data = api.parse_all(STUB_HTML)
    assert data["stub"] is True
    api.validate(data, STUB_HTML)


def test_validation_rejects_generic_page_as_stub():
    with pytest.raises(api.ParseValidationError, match="invalid portal stub"):
        api.validate({"stub": True, "schedule": None}, "<html>WAF error</html>")


def test_validation_rejects_unexpected_parser_loss():
    html = """<table><tr><th>дата</th><th>время</th><th>под гр.</th><th>предмет</th><th>преподаватель</th><th>ауд.</th><th>комм.</th></tr><tr><td>Пн</td></tr><tr><td>09:00</td><td></td><td>Алгебра</td><td>Иванов</td><td>101</td><td></td><td>unexpected</td></tr></table>"""
    with pytest.raises(api.ParseValidationError, match="parser invariant"):
        api.validate({"schedule": {"days": {"Понедельник": []}}}, html)


def test_persist_is_idempotent_by_url_semantic_fingerprint_and_parser(tmp_path):
    parsed = api.parse_all(SCHEDULE_HTML)
    html_a = SCHEDULE_HTML + '<a title="17.08.2026 03:17:30">xls</a>'
    html_b = SCHEDULE_HTML + '<a title="18.08.2026 11:45:02">xls</a>'
    with api.connect(tmp_path / "persist.sqlite3") as db:
        first = api.persist(db, html_a, "https://example.test/a", parsed)
        second = api.persist(db, html_b, "https://example.test/a", parsed)
        other_url = api.persist(db, html_b, "https://example.test/b", parsed)
        assert first == second
        assert other_url != first
        assert db.execute("select count(*) from snapshots").fetchone()[0] == 2
        assert db.execute("select count(*) from lessons").fetchone()[0] == 4
        raw, semantic = db.execute(
            "select raw_hash,content_fingerprint from snapshots where id=?", (first,)
        ).fetchone()
        assert raw == hashlib.sha256(html_a.encode()).hexdigest()
        assert semantic == api.content_fingerprint(parsed)
        head = db.execute("select snapshot_id from snapshot_heads where url=?", ("https://example.test/a",)).fetchone()
        assert head == (first,)


def test_latest_snapshot_returns_parsed_data_without_fetch(tmp_path):
    parsed = api.parse_all(SCHEDULE_HTML)
    path = tmp_path / "snapshot.sqlite3"
    with api.connect(path) as db:
        snapshot_id = api.persist(db, SCHEDULE_HTML, "https://example.test/a", parsed)
    loaded = api.load_latest_snapshot("https://example.test/a", db_path=path, max_age_s=60)
    assert loaded["snapshot_id"] == snapshot_id
    assert loaded["html"] == SCHEDULE_HTML
    assert loaded["data"] == parsed


def test_stub_persists_without_lessons(tmp_path):
    parsed = api.parse_all(STUB_HTML)
    with api.connect(tmp_path / "stub.sqlite3") as db:
        snapshot_id = api.persist(db, STUB_HTML, "https://example.test/stub", parsed)
        assert snapshot_id > 0
        assert db.execute("select count(*) from lessons").fetchone()[0] == 0


def test_retention_deletes_old_snapshots_and_cascades_lessons(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "SNAPSHOT_RETENTION_DAYS", 1)
    changed_html = SCHEDULE_HTML.replace("Предмет Б", "Предмет Ц")
    with api.connect(tmp_path / "retention.sqlite3") as db:
        old_id = api.persist(db, SCHEDULE_HTML, "https://example.test", api.parse_all(SCHEDULE_HTML))
        db.execute(
            "update snapshots set fetched_at=? where id=?",
            ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)).isoformat(), old_id),
        )
        db.commit()
        new_id = api.persist(db, changed_html, "https://example.test", api.parse_all(changed_html))
        assert new_id != old_id
        assert db.execute("select id from snapshots order by id").fetchall() == [(new_id,)]
        assert db.execute("select distinct snapshot_id from lessons").fetchall() == [(new_id,)]


def test_old_sqlite_schema_is_migrated(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    old = sqlite3.connect(path)
    old.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE TABLE snapshots (
          id INTEGER PRIMARY KEY, fetched_at TEXT NOT NULL, url TEXT NOT NULL,
          content_hash TEXT NOT NULL UNIQUE, html TEXT NOT NULL, parser_version TEXT NOT NULL
        );
        CREATE TABLE lessons (
          id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
          source_row INTEGER NOT NULL, day TEXT NOT NULL, time_text TEXT NOT NULL,
          subject_raw TEXT NOT NULL, subject TEXT NOT NULL, teacher TEXT,
          raw_room TEXT, room TEXT, raw_comment TEXT, location TEXT,
          delivery_mode TEXT NOT NULL, link TEXT, note TEXT,
          UNIQUE(snapshot_id,source_row)
        );
    """)
    old.execute(
        "insert into snapshots values(1,?,?,?,?,?)",
        ("2020-01-01T00:00:00+00:00", "https://legacy", "oldhash", SCHEDULE_HTML, "2"),
    )
    old.execute(
        """insert into lessons(
        snapshot_id,source_row,day,time_text,subject_raw,subject,delivery_mode
        ) values(1,1,'Четверг','15:00','Предмет А','Предмет А','in_person')"""
    )
    old.commit()
    old.close()

    with api.connect(path) as db:
        columns = {row[1] for row in db.execute("pragma table_info(snapshots)")}
        assert "content_fingerprint" in columns
        assert "raw_hash" in columns
        assert "content_hash" not in columns
        assert db.execute("select count(*) from snapshots").fetchone() == (1,)
        assert db.execute("select count(*) from lessons").fetchone() == (1,)
        assert db.execute("pragma foreign_key_check").fetchall() == []


def test_physical_count_tolerates_non_numeric_rowspan():
    html = (
        "<table><tr><th>дата</th><th>время</th><th>под гр.</th><th>предмет</th>"
        "<th>преподаватель</th><th>ауд.</th><th>комм.</th></tr>"
        "<tr><td>Пн</td></tr>"
        "<tr><td rowspan='abc'>09:00</td><td></td><td>Алгебра</td><td>Иванов</td>"
        "<td>101</td><td></td></tr></table>"
    )
    assert api.physical_lesson_count(html) == 1
    data = {"schedule": {"days": {"Понедельник": [{"source_row": 1, "subject": "Алгебра", "time": "09:00"}]}}}
    api.validate(data, html)


def test_validate_accepts_alternative_lesson_without_inherited_time():
    html = Path("tests/fixtures/alt_lesson_no_time.html").read_text(encoding="utf-8")
    data = api.parse_all(html)
    assert api.physical_lesson_count(html) == 2
    api.validate(data, html)


def test_default_group_and_unified_url_builder_use_enrollment_year():
    assert api.DEFAULT_GROUP == "6381"
    assert api.resolve_group("default")["group"] == "6381"
    assert api.source_url("6381").endswith("name=6381&type=%D0%94%D0%9E&year=2026")
    cfg = {"group": "9999", "inst_id": "12", "type": "ДО", "year": "2021"}
    assert api.source_url("9999", cfg).endswith("name=9999&type=%D0%94%D0%9E&year=2021")


def test_find_groups_prioritises_6381(monkeypatch):
    html = """
    <table><tr><th>ИЭ</th></tr>
    <tr><td><a href="?page=EditViewGroup&instId=2244947&name=6381&type=%D0%94%D0%9E&year=2026">6381</a></td></tr>
    <tr><td><a href="?page=EditViewGroup&instId=2244947&name=6382&type=%D0%94%D0%9E&year=2026">6382</a></td></tr>
    </table><table><tr><th>ПИ</th></tr>
    <tr><td><a href="?page=EditViewGroup&instId=868342&name=16381&type=%D0%92%D0%9E&year=2021">16381</a></td></tr>
    </table>
    """
    monkeypatch.setattr(api, "_all_index_html", lambda: {"ochn": html})
    matches = api.find_groups("638")
    assert [item["group"] for item in matches] == ["6381", "6382", "16381"]
    assert matches[0]["inst_id"] == "2244947"


def test_resolve_group_finds_any_exact_group(monkeypatch):
    html = """
    <table><tr><th>ПИ</th></tr>
    <tr><td><a href="?page=EditViewGroup&instId=868342&name=5731&type=%D0%92%D0%9E&year=2026">5731</a></td></tr>
    </table>
    """
    monkeypatch.setattr(api, "_all_index_html", lambda: {"ochn": html})
    assert api.resolve_group("5731") == {
        "group": "5731", "inst_id": "868342", "type": "ВО",
        "year": "2026", "institute": "ПИ", "route": "ochn",
    }


def test_api_fetch_uses_common_fetcher(monkeypatch):
    calls = []
    monkeypatch.setattr(api, "fetch_html", lambda url, **kwargs: calls.append((url, kwargs)) or SCHEDULE_HTML)
    html, url, cfg = api.fetch("6381")
    assert html == SCHEDULE_HTML
    assert calls == [(url, {})]
    assert cfg["year"] == "2026"


def test_timetable_ttl_cache_avoids_repeated_fetch_and_persist(monkeypatch):
    api.clear_timetable_cache()
    monkeypatch.setattr(api, "load_latest_snapshot", lambda *args, **kwargs: None)
    calls = []

    def fake_load(cfg):
        calls.append(cfg)
        return {"stub": False, "group": cfg["group"], "source_url": api.source_url(cfg["group"], cfg)}

    monkeypatch.setattr(api, "_load_timetable", fake_load)
    first = api.get_timetable("6381", cache_ttl=60)
    second = api.get_timetable("6381", cache_ttl=60)
    assert first is second
    assert len(calls) == 1
    api.clear_timetable_cache()


def test_default_group_uses_materialized_snapshot_without_fetch(monkeypatch):
    cfg = api.resolve_group(api.DEFAULT_GROUP)
    materialized = {
        "snapshot_id": 7,
        "fetched_at": "2026-09-05T12:00:00+00:00",
        "data": {"stub": False, "weeks": [], "schedule": {"days": {}}},
    }
    monkeypatch.setattr(api, "load_latest_snapshot", lambda url, **kwargs: materialized)
    monkeypatch.setattr(api, "_load_timetable", lambda cfg: (_ for _ in ()).throw(AssertionError("must not fetch")))
    result = api.get_timetable(cfg["group"])
    assert result["snapshot_id"] == 7
    assert result["schedule"] == {"days": {}}


def _request(monkeypatch, payload, path, *, calls=None):
    def load(*args, **kwargs):
        if calls is not None:
            calls.append((args, kwargs))
        return payload

    monkeypatch.setattr(api, "get_timetable", load)
    server = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}{path}", timeout=3
            ) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_api_returns_stub_as_normal_200_for_derived_views(monkeypatch):
    payload = {
        "schema_version": 1, "group": "6381", "stub": True,
        "schedule": None, "weeks": [], "source_url": "https://example.test",
    }
    status, body = _request(monkeypatch, payload, "/v1/timetables/6381/day?date=2026-09-01")
    assert status == 200
    assert body["stub"] is True


def test_api_invalid_parameter_is_400_before_fetch(monkeypatch):
    calls = []
    status, body = _request(monkeypatch, {}, "/v1/timetables/6381/next?limit=nope", calls=calls)
    assert status == 400
    assert body["error"] == "bad_request"
    assert calls == []


def _auto_week_payload():
    return {"group": "6381", "stub": False, "source_url": "https://example.test",
            "schedule": api.parse_all(SCHEDULE_HTML)["schedule"],
            "weeks": [
                {"week": 1, "half": "top", "start": "01.09.2026", "end": "05.09.2026"},
                {"week": 2, "half": "bottom", "start": "07.09.2026", "end": "12.09.2026"},
                {"week": 3, "half": "top", "start": "14.09.2026", "end": "19.09.2026"},
                {"week": 18, "half": "bottom", "start": "28.12.2026", "end": "31.12.2026"},
            ]}


@pytest.mark.parametrize("anchor,number", [
    ("2026-08-31", 1), ("2026-09-05", 1), ("2026-09-06", 2),
    ("2026-09-12", 2), ("2026-09-13", 3), ("2026-12-27", 18),
    ("2026-12-31", 18), ("13.09.2026", 3),
])
def test_api_auto_week_matches_fixed_week_view(monkeypatch, anchor, number):
    payload = _auto_week_payload()
    status, body = _request(monkeypatch, payload, f"/v1/timetables/6381/week?mode=auto&date={anchor}")
    assert status == 200
    selected = next(week for week in payload["weeks"] if week["week"] == number)
    assert body == api.week_view(payload["schedule"], payload["weeks"], selected,
                                 group="6381", source_url=payload["source_url"])


def test_api_auto_week_ends_at_real_calendar_boundary(monkeypatch):
    status, body = _request(monkeypatch, _auto_week_payload(), "/v1/timetables/6381/week?mode=auto&date=2027-01-01")
    assert status == 404
    assert body == {"error": "calendar_ended", "calendar_end": "2026-12-31"}


def test_api_auto_week_distinguishes_unavailable_calendar_from_stub(monkeypatch):
    payload = {**_auto_week_payload(), "weeks": []}
    status, body = _request(monkeypatch, payload, "/v1/timetables/6381/week?mode=auto&date=2026-09-06")
    assert status == 404 and body == {"error": "calendar_unavailable"}
    payload.update(stub=True, schedule=None)
    status, body = _request(monkeypatch, payload, "/v1/timetables/6381/week?mode=auto")
    assert status == 200 and body == payload


def test_api_auto_week_uses_moscow_today_not_utc(monkeypatch):
    import datetime as dt
    import schedule_logic

    instant = dt.datetime(2026, 9, 5, 21, 30, tzinfo=dt.timezone.utc)

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz)

    monkeypatch.setattr(schedule_logic.dt, "datetime", Clock)
    status, body = _request(monkeypatch, _auto_week_payload(), "/v1/timetables/6381/week?mode=auto")
    assert status == 200 and body["week"]["week"] == 2


def test_api_auto_week_recomputes_selection_without_snapshot_changes(monkeypatch):
    import datetime as dt

    payload = _auto_week_payload()
    for today, number in ((dt.date(2026, 9, 6), 2), (dt.date(2026, 9, 13), 3)):
        monkeypatch.setattr(api, "parse_user_date", lambda value: today)
        status, body = _request(monkeypatch, payload, "/v1/timetables/6381/week?mode=auto")
        assert status == 200 and body["week"]["week"] == number
        status, body = _request(monkeypatch, payload, "/v1/timetables/6381/week?week=2")
        assert status == 200 and body["week"]["week"] == 2
    status, body = _request(monkeypatch, payload, "/v1/timetables/6381/week")
    assert status == 404 and body == {"error": "week_not_found"}


@pytest.mark.parametrize("query", [
    "mode=other", "mode=AUTO", "mode=", "mode=auto&mode=auto",
    "mode=auto&week=2", "week=2&mode=auto", "mode=auto&week=",
    "mode=auto&date=", "mode=auto&date=invalid", "mode=auto&date=today&date=tomorrow",
])
def test_api_auto_week_rejects_bad_parameters_before_loading(monkeypatch, query):
    calls = []
    status, body = _request(monkeypatch, {}, f"/v1/timetables/6381/week?{query}", calls=calls)
    assert status == 400 and body["error"] == "bad_request"
    assert calls == []


@pytest.mark.parametrize("action", ["day", "next", ""])
def test_api_mode_is_only_accepted_for_week(monkeypatch, action):
    calls = []
    status, body = _request(monkeypatch, {}, f"/v1/timetables/6381/{action}?mode=auto", calls=calls)
    assert status == 400 and body["error"] == "bad_request"
    assert calls == []


def test_api_unexpected_parse_failure_is_502(monkeypatch):
    def fail(*args, **kwargs):
        raise api.ParseValidationError("lost rows")

    monkeypatch.setattr(api, "get_timetable", fail)
    server = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/v1/timetables/6381", timeout=3
            )
        assert caught.value.code == 502
        assert json.loads(caught.value.read())["error"] == "upstream_or_parse_failure"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_resolve_group_accepts_alphanumeric_group(monkeypatch):
    html = """<table><tr><th>ПИ</th></tr><tr><td>
    <a href="?page=EditViewGroup&instId=868342&name=544P&type=%D0%92%D0%9E&year=2026">544P</a>
    </td></tr></table>"""
    monkeypatch.setattr(api, "_all_index_html", lambda: {"ochn": html})
    assert api.resolve_group("544P")["group"] == "544P"


def test_validation_counts_live_nested_first_lesson():
    html = """<table><tr><th>дата</th><th>время</th><th>под гр.</th><th>предмет</th><th>преподаватель</th><th>ауд.</th><th>комм.</th></tr>
    <tr><td rowspan="2">Пн</td><tr><td>09:00</td><td></td><td>Первая</td><td>Иванов</td><td>101</td><td></td></tr></tr>
    </table>"""
    data = api.parse_all(html)
    assert api.physical_lesson_count(html) == 1
    assert data["schedule"]["days"]["Понедельник"][0]["subject_raw"] == "Первая"
    api.validate(data, html)


def test_legacy_migration_keeps_newest_raw_copy_for_same_semantics(tmp_path):
    path = tmp_path / "legacy-duplicates.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE snapshots (
          id INTEGER PRIMARY KEY, fetched_at TEXT NOT NULL, url TEXT NOT NULL,
          content_hash TEXT NOT NULL UNIQUE, html TEXT NOT NULL, parser_version TEXT NOT NULL
        );
        CREATE TABLE lessons (
          id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
          source_row INTEGER NOT NULL, day TEXT NOT NULL, time_text TEXT NOT NULL,
          subject_raw TEXT NOT NULL, subject TEXT NOT NULL, teacher TEXT,
          raw_room TEXT, room TEXT, raw_comment TEXT, location TEXT,
          delivery_mode TEXT NOT NULL, link TEXT, note TEXT,
          UNIQUE(snapshot_id,source_row)
        );
    """)
    first = SCHEDULE_HTML + '<a title="17.08.2026 03:17:30">xls</a>'
    latest = SCHEDULE_HTML + '<a title="18.08.2026 11:45:02">xls</a>'
    for snapshot_id, fetched_at, html in ((1, "2026-01-01T00:00:00+00:00", first), (2, "2026-01-02T00:00:00+00:00", latest)):
        db.execute("insert into snapshots values(?,?,?,?,?,?)", (snapshot_id, fetched_at, "https://same", f"raw-{snapshot_id}", html, "2"))
        db.execute("insert into lessons(snapshot_id,source_row,day,time_text,subject_raw,subject,delivery_mode) values(?,?,?,?,?,?,?)", (snapshot_id, 1, "Четверг", "15:00", "Предмет", "Предмет", "in_person"))
    db.commit()
    db.close()

    with api.connect(path) as migrated:
        rows = migrated.execute("select id,html from snapshots").fetchall()
        assert rows == [(2, latest)]
        assert migrated.execute("select snapshot_id from lessons").fetchall() == [(2,)]
        assert migrated.execute("pragma foreign_key_check").fetchall() == []


# ---------------------------------------------------------------------------
# Bug #1 regression tests: Route/Group Identity Model
# ---------------------------------------------------------------------------


def test_config_timetable_routes_and_backward_compat():
    """TIMETABLE_ROUTES exposes ochn/zaochn/session; legacy aliases still work."""
    import config

    assert set(config.TIMETABLE_ROUTES) >= {"ochn", "zaochn", "session"}
    assert config.PORTAL_TIMETABLE_BASE_URL == config.TIMETABLE_ROUTES["ochn"]["base_url"]
    assert config.PORTAL_TIMETABLE_INDEX_URL == config.TIMETABLE_ROUTES["ochn"]["index_url"]
    assert config.get_route_base_url("ochn") == config.PORTAL_TIMETABLE_BASE_URL
    assert config.get_route_index_url("ochn") == config.PORTAL_TIMETABLE_INDEX_URL
    assert "zaochn" in config.get_route_base_url("zaochn")
    assert "i.1103358" in config.get_route_base_url("zaochn")
    with pytest.raises(ValueError, match="unknown timetable route"):
        config.get_route_base_url("spo")


def test_build_group_url_uses_route_specific_base():
    """A zaochn group must NOT be built through the hardcoded ochn base URL."""
    import config

    url_ochn = config.build_group_url(
        "6381", {"group": "6381", "inst_id": "2244947", "type": "ДО", "year": "2026", "route": "ochn"}
    )
    url_zaochn = config.build_group_url(
        "9201", {"group": "9201", "inst_id": "55", "type": "ЗО", "year": "2025", "route": "zaochn"}
    )
    assert url_ochn.startswith(config.TIMETABLE_ROUTES["ochn"]["base_url"])
    assert url_zaochn.startswith(config.TIMETABLE_ROUTES["zaochn"]["base_url"])
    assert "zaochn/i.1103358" in url_zaochn
    assert "ochn/i.1103357" not in url_zaochn
    # route kwarg overrides group_ref
    url_override = config.build_group_url(
        "9201", {"group": "9201", "inst_id": "55", "type": "ЗО", "year": "2025"}, route="zaochn"
    )
    assert url_override.startswith(config.TIMETABLE_ROUTES["zaochn"]["base_url"])


def test_group_link_params_preserves_route_path():
    """_group_link_params must keep the path so resolver knows the route."""
    from portal_parser import _group_link_params

    href = "/univer/timetable/zaochn/i.1103358/?page=EditViewGroup&instId=55&name=9201&type=%D0%97%D0%9E&year=2025"
    params = _group_link_params(href)
    assert params is not None
    assert params["group"] == "9201"
    assert params["route"] == "zaochn"
    assert "zaochn" in params["route_path"]

    href_ochn = "?page=EditViewGroup&instId=2244947&name=6381&type=%D0%94%D0%9E&year=2026"
    params_ochn = _group_link_params(href_ochn)
    assert params_ochn is not None
    assert params_ochn["group"] == "6381"
    # Query-only link has no path → no route info, which is fine (legacy fixture).
    assert "route" not in params_ochn


def test_parse_groups_extracts_route_from_full_href():
    from portal_parser import parse_groups

    html = """<table><tr><th>ИЭ</th></tr><tr><td>
    <a href="/univer/timetable/zaochn/i.1103358/?page=EditViewGroup&instId=55&name=9201&type=%D0%97%D0%9E&year=2025">9201</a>
    </td></tr></table>"""
    groups = parse_groups(html)
    assert groups[0]["group"] == "9201"
    assert groups[0]["route"] == "zaochn"


def test_resolver_preserves_route_for_zaochn_group(monkeypatch):
    """A ЗО group resolved from the zaochn index keeps route=zaochn and
    builds its URL against the zaochn base, not the ochn one."""
    import config

    zaochn_html = """<table><tr><th>ИЭ</th></tr><tr><td>
    <a href="/univer/timetable/zaochn/i.1103358/?page=EditViewGroup&instId=55&name=9201&type=%D0%97%D0%9E&year=2025">9201</a>
    </td></tr></table>"""
    monkeypatch.setattr(api, "_all_index_html", lambda: {"ochn": "<html></html>", "zaochn": zaochn_html})
    ref = api.resolve_group("9201")
    assert ref["route"] == "zaochn"
    url = api.source_url("9201", ref)
    assert url.startswith(config.TIMETABLE_ROUTES["zaochn"]["base_url"])
    assert "ochn/i.1103357" not in url


def test_find_groups_returns_ambiguity_across_routes(monkeypatch):
    """Same group number present in ochn and zaochn must surface both matches
    and resolve_group must raise AmbiguousGroupError instead of picking one."""
    ochn_html = """<table><tr><th>ИЭ</th></tr><tr><td>
    <a href="/univer/timetable/ochn/i.1103357/?page=EditViewGroup&instId=2244947&name=9999&type=%D0%94%D0%9E&year=2026">9999</a>
    </td></tr></table>"""
    zaochn_html = """<table><tr><th>ИЭ</th></tr><tr><td>
    <a href="/univer/timetable/zaochn/i.1103358/?page=EditViewGroup&instId=55&name=9999&type=%D0%97%D0%9E&year=2025">9999</a>
    </td></tr></table>"""
    monkeypatch.setattr(api, "_all_index_html", lambda: {"ochn": ochn_html, "zaochn": zaochn_html})

    matches = api.find_groups("9999")
    assert len(matches) == 2
    assert {m["route"] for m in matches} == {"ochn", "zaochn"}

    with pytest.raises(api.AmbiguousGroupError) as caught:
        api.resolve_group("9999")
    assert len(caught.value.groups) == 2


def test_known_groups_6381_and_5234_keep_ochn_route():
    """Known groups keep working and carry route=ochn; URLs stay on ochn base."""
    import config

    ref_6381 = api.resolve_group("6381")
    assert ref_6381["route"] == "ochn"
    assert api.source_url("6381", ref_6381).startswith(config.TIMETABLE_ROUTES["ochn"]["base_url"])

    ref_5234 = api.resolve_group("5234")
    assert ref_5234["route"] == "ochn"
    assert ref_5234["inst_id"] == "868344"
    assert api.source_url("5234", ref_5234).startswith(config.TIMETABLE_ROUTES["ochn"]["base_url"])

    # default alias still resolves to 6381 with ochn route
    assert api.resolve_group("default")["route"] == "ochn"



def test_response_cache_serves_views_until_snapshot_changes(monkeypatch):
    """Кеш day/week живёт до суток, но любой новый snapshot делает его недействительным."""
    api.clear_response_cache()
    snapshot = {"id": 1}
    data = api.parse_all(SCHEDULE_HTML)

    def fake_get_timetable(group, **kwargs):
        return {"group": "6381", "source_url": "https://example.test/6381", "snapshot_id": snapshot["id"],
                "weeks": data.get("weeks", []), "schedule": data.get("schedule")}

    monkeypatch.setattr(api, "get_timetable", fake_get_timetable)
    view_calls = {"n": 0}
    real_day_view = api.day_view

    def counting_day_view(*args, **kwargs):
        view_calls["n"] += 1
        return real_day_view(*args, **kwargs)

    monkeypatch.setattr(api, "day_view", counting_day_view)
    server = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def get(path):
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}{path}", timeout=3) as response:
            return response.status, json.loads(response.read())

    try:
        assert get("/v1/timetables/6381/day?date=2026-09-02")[0] == 200
        assert get("/v1/timetables/6381/day?date=2026-09-02")[0] == 200
        assert view_calls["n"] == 1  # второй запрос — из кеша
        assert get("/v1/timetables/6381/day?date=2026-09-03")[0] == 200
        assert view_calls["n"] == 2  # другая дата — другой ключ
        snapshot["id"] = 2
        assert get("/v1/timetables/6381/day?date=2026-09-02")[0] == 200
        assert view_calls["n"] == 3  # новый snapshot сбросил запись
        # /next без явного времени зависит от текущей минуты и не кешируется
        next_calls = {"n": 0}
        real_next = api.next_lessons

        def counting_next(*args, **kwargs):
            next_calls["n"] += 1
            return real_next(*args, **kwargs)

        monkeypatch.setattr(api, "next_lessons", counting_next)
        get("/v1/timetables/6381/next?limit=1")
        get("/v1/timetables/6381/next?limit=1")
        assert next_calls["n"] == 2
        # а с явными датой и временем — кешируется
        get("/v1/timetables/6381/next?date=2026-09-02&time=09:00&limit=1")
        get("/v1/timetables/6381/next?date=2026-09-02&time=09:00&limit=1")
        assert next_calls["n"] == 3
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        api.clear_response_cache()



def test_duplicate_index_rows_do_not_make_a_group_ambiguous(monkeypatch):
    """Индекс портала печатает одну группу дважды — это одна ссылка, а не две группы."""
    row = {"inst_id": "868344", "group": "5231", "type": "ДО", "year": "2025", "route_path": "x", "route": "ochn", "institute": None}
    monkeypatch.setattr(api, "_all_index_html", lambda: {"ochn": "<html/>"})
    monkeypatch.setattr(api, "parse_groups", lambda html, inst_id=None: [dict(row), dict(row), {**row, "type": "ЗО"}])
    found = api.find_groups("5231")
    assert [(g["group"], g["type"]) for g in found] == [("5231", "ДО"), ("5231", "ЗО")]
    assert [(g["group"], g["type"]) for g in api.find_groups("5231", typ="ДО")] == [("5231", "ДО")]
    assert len(api.list_groups()) == 2



def test_api_serves_only_the_default_group(monkeypatch):
    """Сервис ведёт одну группу: чужие группы, индекс групп и институтов недоступны."""
    monkeypatch.setattr(api.config, "API_DEFAULT_GROUP_ONLY", True)
    payload = {"schema_version": 1, "group": "6381", "stub": False, "schedule": {"days": {}}, "weeks": [], "source_url": "https://example.test", "snapshot_id": 1}
    calls = []
    assert _request(monkeypatch, payload, "/v1/timetables/5234/day?date=2026-09-01", calls=calls)[0] == 404
    assert _request(monkeypatch, payload, "/v1/timetables/6381?year=2025", calls=calls)[0] == 404
    assert calls == []  # до фетча дело не доходит
    status, body = _request(monkeypatch, payload, "/v1/groups?q=52")
    assert (status, body["error"]) == (404, "not_found")
    assert _request(monkeypatch, payload, "/v1/institutes")[0] == 404
    assert _request(monkeypatch, payload, "/v1/timetables/6381")[0] == 200
    assert _request(monkeypatch, payload, "/v1/timetables/me/day?date=2026-09-01")[0] == 200
    monkeypatch.setattr(api.config, "API_DEFAULT_GROUP_ONLY", False)
    assert _request(monkeypatch, payload, "/v1/timetables/5234/day?date=2026-09-01", calls=calls)[0] == 200
