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
