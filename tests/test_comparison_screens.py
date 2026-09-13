from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from monitor import diff_schedules
from parse import parse_schedule
from portal_parser import expand_table, find_schedule_table, render_schedule_day_chunk_htmls
from post import _comparison_marks


SOURCE = Path("tests/fixtures/rowspan_time.html").read_text(encoding="utf-8")


def comparison(old, new):
    schedules = [parse_schedule(html) for html in (old, new)]
    diff = diff_schedules(*schedules)
    return [
        render_schedule_day_chunk_htmls(
            html, "https://example.test", comparison_title=title,
            comparison_marks=_comparison_marks(schedule, diff, before=before),
        )
        for html, schedule, before, title in zip(
            (old, new), schedules, (True, False), ("Было", "Стало"),
        )
    ]


@pytest.mark.parametrize(("old_value", "new_value", "expected_old", "expected_new"), [
    ("<td>102</td>", "<td>303</td>", "102", "303"),
    ("Предмет Б", "Новое название", "(пр.) Предмет Б", "(пр.) Новое название"),
    ("<td>102</td>", "<td></td>", "102", ""),
    ("по нижней неделе, с использованием ДОТ", "", "по нижней неделе, с использованием ДОТ", ""),
])
def test_changed_cells_on_both_sides_preserve_every_other_cell(old_value, new_value, expected_old, expected_new):
    old, new = comparison(SOURCE, SOURCE.replace(old_value, new_value))
    for chunks, expected in ((old, expected_old), (new, expected_new)):
        soup = BeautifulSoup(chunks[0]["html"], "html.parser")
        marked = soup.select("td.comparison-changed")
        assert [cell.get_text(" ", strip=True) for cell in marked] == [expected]
        assert not soup.select("tr.comparison-changed, .comparison-tag, .schedule-status-tag")
        assert chunks[0]["comparison_kinds"] == ["changed"]
        assert not soup.select(".comparison-added, .comparison-removed")


def test_added_removed_alternative_leaves_shared_time_and_day_neutral():
    removed = SOURCE.replace(
        '<tr><td></td><td>(пр.) Предмет Б</td><td>Петров П. П.</td><td>102</td>'
        '<td>по нижней неделе, с использованием ДОТ</td></tr>', "",
    )
    for old_html, new_html, side, kind, tag in (
        (SOURCE, removed, 0, "removed", "Убрали"),
        (removed, SOURCE, 1, "added", "Добавили"),
    ):
        chunks = comparison(old_html, new_html)[side]
        soup = BeautifulSoup(chunks[0]["html"], "html.parser")
        marked = soup.select(f".comparison-{kind}")
        assert len(marked) == 5
        assert soup.select_one(".comparison-tag").text == tag
        assert "Предмет Б" in soup.select_one(".comparison-tag").parent.text
        assert all("15:00" not in cell.text and cell.text != "Чт" for cell in marked)
        assert chunks[0]["comparison_kinds"] == [kind]


def test_cell_provenance_keeps_rowspan_and_parser_grid_identical():
    table = find_schedule_table(BeautifulSoup(SOURCE, "html.parser"))
    owners = []
    assert expand_table(table, cell_rows=owners) == expand_table(table)
    assert owners[2][1] is owners[3][1]
    assert owners[2][5] is not owners[3][5]


def test_duplicate_subject_time_matches_parity_and_subgroup_not_neighbour():
    source = SOURCE.replace("Предмет Б", "Предмет А").replace("Петров П. П.", "Иванов И. И.")
    new = source.replace("<td>102</td>", "<td>555</td>")
    old_chunks, new_chunks = comparison(source, new)
    for chunks, room in ((old_chunks, "102"), (new_chunks, "555")):
        soup = BeautifulSoup(chunks[0]["html"], "html.parser")
        assert [cell.text for cell in soup.select(".comparison-changed")] == [room]
        assert "по нижней неделе" in soup.select_one(".comparison-changed").parent.text


def test_ambiguous_partial_diff_is_not_guessed():
    source = SOURCE.replace("Предмет Б", "Предмет А")
    marks = _comparison_marks(parse_schedule(source), {"changed": [{
        "day": "Четверг", "subject": "(пр.) Предмет А", "time": "15:00 16:00",
        "fields": [],
    }]}, before=False)
    assert marks == []


def test_enriched_teacher_fields_still_match_raw_source_names():
    source = SOURCE.replace("Иванов И. И.", "Иванов")
    updated = source.replace("Иванов", "Сидоров")
    old, new = parse_schedule(source), parse_schedule(updated)
    diff = diff_schedules(old, new, teacher_lookup={
        "Иванов": "Иванов Иван Иванович", "Сидоров": "Сидоров Петр Петрович",
    })
    for schedule, before in ((old, True), (new, False)):
        assert _comparison_marks(schedule, diff, before=before) == [
            {"source_row": 2, "kind": "changed", "columns": ["преподаватель"]},
        ]
    added = diff_schedules(None, new, teacher_lookup={"Сидоров": "Сидоров Петр Петрович"})
    assert len(_comparison_marks(new, added, before=False)) == 2


def test_unclosed_day_row_does_not_swallow_next_day():
    source = '''<table><tbody>
    <tr><th>дата</th><th>время</th><th>предмет</th><th>преподаватель</th><th>ауд.</th></tr>
    <tr><td rowspan="2">Пн</td>
      <tr><td>9:00</td><td>Первая</td><td>А</td><td>101</td></tr>
      <tr><td rowspan="2">Вт</td>
        <tr><td>11:00</td><td>Вторая</td><td>Б</td><td>102</td></tr>
      </tr>
    </tr></tbody></table>'''
    old, new = comparison(source, source.replace("102", "888"))
    for chunks, room in ((old, "102"), (new, "888")):
        assert [chunk["label"] for chunk in chunks] == ["Пн", "Вт"]
        monday, tuesday = [BeautifulSoup(chunk["html"], "html.parser") for chunk in chunks]
        assert "Вторая" not in monday.get_text()
        assert tuesday.select_one(".comparison-changed").text == room
        assert tuesday.get_text().count("Вторая") == 1


def test_nested_first_lesson_not_duplicated_by_comparison():
    source = SOURCE.replace('<td rowspan="3">Чт</td></tr><tr>', '<td rowspan="3">Чт</td><tr>', 1)
    source = source.replace("по верхней неделе</td></tr>", "по верхней неделе</td></tr></tr>", 1)
    old, new = comparison(source, source.replace("<td>101</td>", "<td>777</td>"))
    for chunks in (old, new):
        soup = BeautifulSoup(chunks[0]["html"], "html.parser")
        assert soup.get_text().count("Предмет А") == 1
        assert soup.get_text().count("Предмет Б") == 1
        assert len(soup.select("td.comparison-changed")) == 1


def test_comparison_does_not_change_clean_or_dashboard_chunks():
    clean = render_schedule_day_chunk_htmls(SOURCE, "https://example.test")
    comparison(SOURCE, SOURCE.replace("<td>101</td>", "<td>777</td>"))
    assert clean == render_schedule_day_chunk_htmls(SOURCE, "https://example.test")
    assert "comparison-" not in clean[0]["html"]


def test_shared_room_change_marks_the_owning_cell_once():
    source = SOURCE.replace("<td>101</td>", '<td rowspan="2">101</td>').replace("<td>102</td>", "")
    old, new = comparison(source, source.replace(
        '<td rowspan="2">101</td>', '<td rowspan="2">555</td>',
    ))
    for chunks, expected in ((old, "101"), (new, "555")):
        soup = BeautifulSoup(chunks[0]["html"], "html.parser")
        cells = soup.select("td.comparison-changed")
        assert len(cells) == 1 and cells[0].text == expected
        assert cells[0]["rowspan"] == "2"


def test_cross_day_move_keeps_source_days_and_before_after_evidence():
    old, new = comparison(SOURCE, SOURCE.replace(">Чт<", ">Пт<"))
    assert [chunk["label"] for chunk in old] == ["Чт"]
    assert [chunk["label"] for chunk in new] == ["Пт"]
    assert old[0]["comparison_kinds"] == ["removed"]
    assert new[0]["comparison_kinds"] == ["added"]


def test_inline_day_rows_split_without_marking_date_column():
    source = '''<table><tr><th>дата</th><th>время</th><th>предмет</th><th>преподаватель</th><th>ауд.</th></tr>
    <tr><td>Пн</td><td>9:00</td><td>Математика</td><td>А</td><td>101</td></tr>
    <tr><td>Вт</td><td>9:00</td><td>Физика</td><td>Б</td><td>102</td></tr></table>'''
    old, new = comparison(source, source.replace("101", "777").replace("102", "888"))
    for chunks in (old, new):
        assert [chunk["label"] for chunk in chunks] == ["Пн", "Вт"]
        for chunk in chunks:
            soup = BeautifulSoup(chunk["html"], "html.parser")
            assert len(soup.select("td.comparison-changed")) == 1
            assert soup.select_one("td.comparison-changed").text not in {"Пн", "Вт"}
