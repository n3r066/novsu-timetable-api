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
    monkeypatch.setattr(api, "_index_html", lambda: html)
    matches = api.find_groups("638")
    assert [item["group"] for item in matches] == ["6381", "6382", "16381"]
    assert matches[0]["inst_id"] == "2244947"


def test_resolve_group_finds_any_exact_group(monkeypatch):
    html = """
    <table><tr><th>ПИ</th></tr>
    <tr><td><a href="?page=EditViewGroup&instId=868342&name=5731&type=%D0%92%D0%9E&year=2026">5731</a></td></tr>
    </table>
    """
    monkeypatch.setattr(api, "_index_html", lambda: html)
    assert api.resolve_group("5731") == {
        "group": "5731", "inst_id": "868342", "type": "ВО",
        "year": "2026", "institute": "ПИ",
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


def _request(monkeypatch, payload, path):
    monkeypatch.setattr(api, "get_timetable", lambda *args, **kwargs: payload)
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
    status, body = _request(monkeypatch, {}, "/v1/timetables/6381/next?limit=nope")
    assert status == 400
    assert body["error"] == "bad_request"


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
    monkeypatch.setattr(api, "_index_html", lambda: html)
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
