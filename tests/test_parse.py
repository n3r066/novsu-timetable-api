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


def test_alternative_lesson_without_inherited_time_is_kept():
    html = Path("tests/fixtures/alt_lesson_no_time.html").read_text(encoding="utf-8")
    rows = parse_schedule(html)["days"]["Понедельник"]
    assert len(rows) == 2
    assert rows[0]["subject_raw"] == "(пр.) Альтернатива"
    assert rows[0]["time"] == "—"  # no explicit time cell
    assert rows[0]["delivery_mode"] == "remote_or_hybrid"
    assert rows[1]["subject_raw"] == "(лек.) Основной"
    assert rows[1]["time"] == "11:00 12:00"


def test_live_nested_first_lesson_is_not_lost_and_inherits_time():
    html = """<table>
    <tr><th>дата</th><th>время</th><th>под гр.</th><th>предмет</th><th>преподаватель</th><th>ауд.</th><th>комм.</th></tr>
    <tr><td rowspan="3">Пн</td><tr><td rowspan="2">09:00<br>10:00</td><td></td><td>Первая</td><td>Иванов</td><td>101</td><td>верхняя</td></tr></tr>
    <tr><td></td><td>Альтернатива</td><td>Петров</td><td>102</td><td>нижняя</td></tr>
    </table>"""
    rows = parse_schedule(html)["days"]["Понедельник"]
    assert [row["subject_raw"] for row in rows] == ["Первая", "Альтернатива"]
    assert [row["time"] for row in rows] == ["09:00 10:00", "09:00 10:00"]


def test_tbody_and_reordered_headers_are_supported():
    html = """<table><tbody>
    <tr><th>дата</th><th>предмет</th><th>время</th><th>комм.</th><th>ауд.</th><th>преподаватель</th><th>под&nbsp;гр.</th></tr>
    <tr><td rowspan="2">Вт</td></tr>
    <tr><td>Математика</td><td>09:00</td><td>очно</td><td>203</td><td>Иванов</td><td>1</td></tr>
    </tbody></table>"""
    row = parse_schedule(html)["days"]["Вторник"][0]
    assert row["subject_raw"] == "Математика"
    assert row["time"] == "09:00"
    assert row["teacher"] == "Иванов"
    assert row["room"] == "203"
    assert row["subgroup"] == "1"


def test_optional_subgroup_and_comment_headers_may_be_absent():
    html = """<table><tr><th>дата</th><th>время</th><th>предмет</th><th>преподаватель</th><th>ауд.</th></tr>
    <tr><td rowspan="2">Ср</td></tr><tr><td>10:00</td><td>Физика</td><td>Петров</td><td>301</td></tr></table>"""
    row = parse_schedule(html)["days"]["Среда"][0]
    assert row["subject_raw"] == "Физика"
    assert row["note"] == ""
    assert row["subgroup"] == ""


def test_rowspans_in_teacher_and_room_columns_are_expanded():
    html = """<table><tr><th>дата</th><th>время</th><th>под гр.</th><th>предмет</th><th>преподаватель</th><th>ауд.</th><th>комм.</th></tr>
    <tr><td rowspan="3">Чт</td></tr>
    <tr><td>09:00</td><td></td><td>А</td><td rowspan="2">Иванов</td><td rowspan="2">101</td><td></td></tr>
    <tr><td>10:00</td><td></td><td>Б</td><td></td></tr></table>"""
    rows = parse_schedule(html)["days"]["Четверг"]
    assert [(row["teacher"], row["room"]) for row in rows] == [("Иванов", "101"), ("Иванов", "101")]


def test_comment_metadata_handles_real_locations_and_url_punctuation():
    html = """<table><tr><th>дата</th><th>время</th><th>под гр.</th><th>предмет</th><th>преподаватель</th><th>ауд.</th><th>комм.</th></tr>
    <tr><td rowspan="3">Пт</td></tr>
    <tr><td>09:00</td><td></td><td>А</td><td>Иванов</td><td>1</td><td>Антоново</td></tr>
    <tr><td>10:00</td><td></td><td>Б</td><td>Петров</td><td>2</td><td>Б.С.-Петербургская, 41; https://x.test/a).</td></tr>
    </table>"""
    rows = parse_schedule(html)["days"]["Пятница"]
    assert rows[0]["location"] == "Антоново"
    assert rows[1]["location"] == "Б.С.-Петербургская, 41"
    assert rows[1]["link"] == "https://x.test/a"


def test_new_day_clears_stale_rowspans_in_all_columns():
    html = """<table><tr><th>дата</th><th>время</th><th>под гр.</th><th>предмет</th><th>преподаватель</th><th>ауд.</th><th>комм.</th></tr>
    <tr><td rowspan="5">Пн</td></tr>
    <tr><td rowspan="5">09:00</td><td></td><td>Понедельник</td><td rowspan="5">Старый</td><td rowspan="5">101</td><td></td></tr>
    <tr><td rowspan="2">Вт</td></tr>
    <tr><td>10:00</td><td></td><td>Вторник</td><td>Новый</td><td>202</td><td></td></tr></table>"""
    days = parse_schedule(html)["days"]
    assert days["Вторник"][0]["subject_raw"] == "Вторник"
    assert days["Вторник"][0]["time"] == "10:00"
    assert days["Вторник"][0]["teacher"] == "Новый"


def test_colspan_day_separator_does_not_create_phantom_lesson():
    html = """<table><tr><th>дата</th><th>время</th><th>под гр.</th><th>предмет</th><th>преподаватель</th><th>ауд.</th><th>комм.</th></tr>
    <tr><td colspan="7">Пн</td></tr>
    <tr><td>09:00</td><td></td><td>Алгебра</td><td>Иванов</td><td>101</td><td></td></tr></table>"""
    rows = parse_schedule(html)["days"]["Понедельник"]
    assert [row["subject_raw"] for row in rows] == ["Алгебра"]


def test_subgroup_header_without_period_is_normalized():
    html = """<table><tr><th>дата</th><th>время</th><th>под гр</th><th>предмет</th><th>преподаватель</th><th>ауд.</th><th>комм.</th></tr>
    <tr><td rowspan="2">Пн</td></tr><tr><td>09:00</td><td>2</td><td>Алгебра</td><td>Иванов</td><td>101</td><td></td></tr></table>"""
    assert parse_schedule(html)["days"]["Понедельник"][0]["subgroup"] == "2"


def test_real_antovo_comment_does_not_treat_cancelled_date_as_address():
    html = """<table><tr><th>дата</th><th>время</th><th>под гр.</th><th>предмет</th><th>преподаватель</th><th>ауд.</th><th>комм.</th></tr>
    <tr><td rowspan="2">Пн</td></tr><tr><td>09:00</td><td></td><td>Физкультура</td><td>Иванов</td><td>зал</td><td>ИГУМ, Антоново, 24.09.; 22.10.; 17.12. занятий не будет</td></tr></table>"""
    assert parse_schedule(html)["days"]["Понедельник"][0]["location"] == "ИГУМ, Антоново"


SCHEDULE_HEAD = "<tr><th>дата</th><th>время</th><th>предмет</th><th>преподаватель</th><th>ауд.</th><th>комм.</th></tr>"


def _day_variant_html(day_cell: str) -> str:
    return (
        f"<table>{SCHEDULE_HEAD}"
        f"<tr><td>{day_cell}</td></tr>"
        "<tr><td>9:00</td><td>А</td><td>Иванов</td><td>203</td><td></td></tr>"
        "<tr><td>10:00</td><td>Б</td><td>Петров</td><td>204</td><td>ДОТ</td></tr>"
        "</table>"
    )


def test_day_separator_case_dot_and_space_variants_keep_lessons():
    for day_cell in ("Вт", "вт", "Вт.", "ВТ", " Вт "):
        days = parse_schedule(_day_variant_html(day_cell))["days"]
        assert "Вторник" in days, day_cell
        assert [x["subject"] for x in days["Вторник"]] == ["А", "Б\nДОТ"], day_cell


def test_day_separator_variants_do_not_change_fingerprint():
    from fetch import content_fingerprint
    base = content_fingerprint(_day_variant_html("Вт"))
    for day_cell in ("вт", "Вт.", "ВТ"):
        assert content_fingerprint(_day_variant_html(day_cell)) == base, day_cell


def test_calendar_accepts_dash_and_em_dash_separators():
    from parse import parse_calendar
    for sep in ("-", "–", "—"):
        html = (
            "<table><tr><td>1</td><td>01.09.2026 " + sep + " 05.09.2026</td>"
            "<td>2</td><td>07.09.2026 " + sep + " 12.09.2026</td></tr></table>"
        )
        weeks = parse_calendar(html)
        assert len(weeks) == 2, sep
        assert weeks[0]["half"] == "top" and weeks[1]["half"] == "bottom"


def test_plural_header_aliases_keep_columns_mapped():
    html = (
        "<table>"
        "<tr><th>Дата</th><th>Время занятий</th><th>Предметы</th>"
        "<th>Преподаватели</th><th>Аудитории</th><th>Комментарии</th></tr>"
        "<tr><td>Ср.</td></tr>"
        "<tr><td>11:00</td><td>Экономика</td><td>Сидорова</td><td>415</td><td>Антоново</td></tr>"
        "</table>"
    )
    rows = parse_schedule(html)["days"]["Среда"]
    assert rows[0]["teacher"] == "Сидорова"
    assert rows[0]["room"] == "415"
    assert rows[0]["note"] == "Антоново"
    assert rows[0]["time"] == "11:00"
