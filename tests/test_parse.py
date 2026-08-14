import json
from pathlib import Path

from parse import parse_schedule


def test_rowspan_time_is_inherited_by_alternative_lesson():
    html = Path("tests/fixtures/rowspan_time.html").read_text(encoding="utf-8")
    rows = parse_schedule(html)["days"]["Четверг"]
    assert [row["time"] for row in rows] == ["15:00 16:00", "15:00 16:00"]
    assert "по слоту выше" not in json.dumps(rows, ensure_ascii=False)


def test_annotation_database_is_valid():
    db = json.loads(Path("data/annotations.json").read_text(encoding="utf-8"))
    assert db["schema_version"] == 1
    assert {item["term"] for item in db["glossary"]} >= {"ДОТ", "лек.", "пр.", "лаб.", "ауд.", "подгр."}
    assert db["week_parity"]["верхняя"]["weeks"][:3] == [1, 3, 5]
    assert db["week_parity"]["нижняя"]["weeks"][:3] == [2, 4, 6]


def test_live_style_stale_day_rowspan_does_not_shift_columns():
    html = """<table><tr><th>дата</th><th>время</th><th>под гр.</th><th>предмет</th><th>преподаватель</th><th>ауд.</th><th>комм.</th></tr><tr><td rowspan='5'>Вт</td></tr><tr><td>9:00</td><td></td><td>А</td><td>Иванов</td><td>203</td><td></td></tr><tr><td>10:00</td><td></td><td>Б</td><td>Петров</td><td>.</td><td>ДОТ</td></tr><tr><td rowspan='2'>Чт</td></tr><tr><td>15:00</td><td></td><td>В</td><td></td><td>Спортзал</td><td>ИГУМ, Антоново</td></tr></table>"""
    days = parse_schedule(html)["days"]
    assert [x["subject"] for x in days["Вторник"]] == ["А", "Б\nДОТ"]
    assert days["Четверг"][0]["room"] == "Спортзал"
    assert days["Четверг"][0]["location"] == "ИГУМ, Антоново"


def test_subject_artifact_room_and_delivery_are_structured():
    html = Path("tests/fixtures/rowspan_time.html").read_text(encoding="utf-8").replace("(пр.) Предмет Б", "(пр.) .Предмет Б")
    row = parse_schedule(html)["days"]["Четверг"][1]
    assert row["subject"].startswith("(пр.) Предмет Б")
    assert row["raw_room"] == "102"
    assert row["delivery_mode"] == "remote_or_hybrid"
