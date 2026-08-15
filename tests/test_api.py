import sqlite3
from pathlib import Path
import api


def test_connect_seeds_annotation_database(tmp_path):
    db = api.connect(tmp_path / "test.sqlite3")
    assert db.execute("select label from glossary where term='ДОТ'").fetchone()[0].startswith("Дистанционные")
    assert db.execute("select count(*) from annotation_rules").fetchone()[0] >= 6
    db.close()


def test_validation_rejects_silent_row_loss():
    html = Path("tests/fixtures/rowspan_time.html").read_text(encoding="utf-8")
    data = {"schedule": {"days": {"Четверг": []}}}
    try:
        api.validate(data, html)
    except ValueError as exc:
        assert "physical=2, parsed=0" in str(exc)
    else:
        raise AssertionError("validation accepted lost lessons")


def test_validation_rejects_stub_schedule():
    try:
        api.validate({"stub": True, "schedule": None}, "<html></html>")
    except ValueError as exc:
        assert "stub" in str(exc)
    else:
        raise AssertionError("stub schedule was accepted")


def test_validation_rejects_unrecognized_nonempty_row():
    html = """<table><tr><th>дата</th><th>время</th><th>под гр.</th><th>предмет</th><th>преподаватель</th><th>ауд.</th><th>комм.</th></tr><tr><td>Пн</td></tr><tr><td>09:00</td><td></td><td>Алгебра</td><td>Иванов</td><td>101</td><td></td><td>unexpected</td></tr></table>"""
    try:
        api.validate({"schedule": {"days": {"Понедельник": []}}}, html)
    except ValueError as exc:
        assert "row shape" in str(exc)
    else:
        raise AssertionError("unknown row shape was accepted")


def test_persist_is_idempotent(tmp_path):
    html = Path("tests/fixtures/rowspan_time.html").read_text(encoding="utf-8")
    parsed = api.parse_all(html)
    with api.connect(tmp_path / "persist.sqlite3") as db:
        first = api.persist(db, html, "https://example.test", parsed)
        second = api.persist(db, html, "https://example.test", parsed)
        assert first == second
        assert db.execute("select count(*) from snapshots").fetchone()[0] == 1
        assert db.execute("select count(*) from lessons").fetchone()[0] == 2
