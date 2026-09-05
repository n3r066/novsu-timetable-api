from bs4 import BeautifulSoup

from portal_parser import (
    find_schedule_table,
    parse_groups,
    parse_institutes,
    render_schedule_day_chunk_htmls,
)


def test_groups_accept_alphanumeric_names_and_any_query_order():
    html = """<table><tr><th>ИЭ</th></tr><tr><td>
    <a href="?year=2026&type=%D0%94%D0%9E&name=544P&instId=2244947&page=EditViewGroup">544P</a>
    </td></tr></table>"""
    assert parse_groups(html) == [{
        "group": "544P", "inst_id": "2244947", "type": "ДО",
        "year": "2026", "institute": "ИЭ",
    }]


def test_institutes_do_not_depend_on_original_tag_serialization():
    html = """<TABLE><TR><TH class=x>И&amp;Э</TH></TR><tr><td>
    <a href="?page=EditViewGroup&instId=2244947&name=6381&type=%D0%94%D0%9E&year=2026">6381</a>
    </td></tr></TABLE>"""
    assert parse_institutes(html) == [{"name": "И&Э", "inst_id": "2244947"}]


def test_best_complete_schedule_table_is_selected():
    partial = "<table><tr><th>дата</th><th>время</th><th>предмет</th><th>преподаватель</th><th>ауд.</th></tr></table>"
    complete = "<table id=full><tr><th>дата</th><th>время</th><th>под гр.</th><th>предмет</th><th>преподаватель</th><th>ауд.</th><th>комм.</th></tr></table>"
    table = find_schedule_table(BeautifulSoup(partial + complete, "html.parser"))
    assert table["id"] == "full"


_DIFF_HL_SOURCE = """
<div id="npe_instance_1103357_npe_content">
  <h1>Расписание занятий</h1>
  <table><tr><th>дата</th><th>время</th><th>под гр.</th><th>предмет</th><th>преподаватель</th><th>ауд.</th><th>комм.</th></tr>
  <tr><td rowspan="3">Пн</td></tr>
  <tr><td>16:00<br>17:00</td><td></td><td>(лек.) География туризма</td><td>Ефимов О. Н.</td><td>418</td><td>по верхней неделе Антоново</td></tr>
  <tr><td>17:00<br>18:00</td><td></td><td>(лек.) География туризма</td><td>Ефимов О. Н.</td><td>418</td><td>по нижней неделе Антоново</td></tr>
  <tr><td rowspan="2">Ср</td></tr>
  <tr><td>9:00<br>10:00</td><td></td><td>(пр.) Физкультура</td><td>Сидоров С. С.</td><td>1306</td><td>Антоново</td></tr>
  </table>
  <a href="/print">Распечатать</a>
</div>
"""

_DIFF_CHANGED = {
    "day": "Понедельник", "time": "16:00 17:00",
    "subject": "(лек.) География туризма\nпо верхней неделе Антоново", "subgroup": "",
    "fields": [["примечание", "Антоново", "по верхней неделе Антоново"]],
}
_DIFF_ADDED = {
    "day": "Понедельник", "time": "17:00 18:00",
    "subject": "(лек.) География туризма\nпо нижней неделе Антоново",
    "room": "418", "teacher": "Ефимов О. Н.",
}


def test_day_chunks_without_diff_stay_clean():
    chunks = render_schedule_day_chunk_htmls(_DIFF_HL_SOURCE, "https://example.test", days_per_chunk=1)
    assert [chunk["label"] for chunk in chunks] == ["Пн", "Ср"]
    assert all(chunk["marked"] == 0 for chunk in chunks)
    blob = "".join(chunk["html"] for chunk in chunks)
    assert "diff-row" not in blob
    assert "diff-legend" not in blob
    # без подсветки не тащим и её CSS
    assert "tr.diff-row > td" not in blob


def test_diff_highlight_marks_changed_row_cell_and_added_row():
    chunks = render_schedule_day_chunk_htmls(
        _DIFF_HL_SOURCE, "https://example.test", days_per_chunk=1,
        diff_items=[{**_DIFF_CHANGED, "_diff_kind": "changed"},
                    {**_DIFF_ADDED, "_diff_kind": "added"}],
        diff_legend="Жёлтым подсвечены правки",
    )
    monday, wednesday = chunks
    # две правки Пн: изменённая 16:00 и добавленная 17:00; Ср не тронута
    assert monday["marked"] == 2
    assert wednesday["marked"] == 0
    assert "diff-row" not in wednesday["html"]

    soup = BeautifulSoup(monday["html"], "html.parser")
    rows = soup.select("tr.diff-row")
    assert len(rows) == 2
    def row_time(row):
        # В первую ячейку теперь вставляется текстовая метка правки.
        text = row.select_one("td").get_text(" ", strip=True)
        for tag in ("НОВАЯ ПАРА", "ИЗМЕНИЛИ"):
            text = text.replace(tag, "").strip()
        return text

    by_time = {row_time(row): row for row in rows}
    changed_row = by_time["16:00 17:00"]
    added_row = by_time["17:00 18:00"]
    # тип правки видно словами и классом, а не только цветом
    assert "diff-changed" in changed_row.get("class", [])
    assert "diff-added" in added_row.get("class", [])
    assert changed_row.select_one(".diff-tag").get_text(strip=True) == "ИЗМЕНИЛИ"
    assert added_row.select_one(".diff-tag").get_text(strip=True) == "НОВАЯ ПАРА"
    # изменённая пара: подсвечена конкретная ячейка с новым значением
    cells = changed_row.select("td.diff-cell")
    assert [cell.get_text(" ", strip=True) for cell in cells] == ["по верхней неделе Антоново"]
    # добавленная пара — новая целиком, точечной ячейки у неё нет
    assert not added_row.select("td.diff-cell")
    # день портала не закрашен: правки внутри дня, а не весь Пн
    assert "diff-row" not in str(soup.select_one("tr"))
    # CSS и легенда приезжают вместе с подсветкой
    assert "tr.diff-row > td" in monday["html"]
    legend = monday["html"][monday["html"].find('class="diff-legend"'):]
    assert "НОВАЯ ПАРА" in legend and "ИЗМЕНИЛИ" in legend
    assert "swatch added" in legend
    # типы помеченных правок уходят в пост: подпись обещает только свои цвета
    assert monday["marked_kinds"] == ["added", "changed"]
    assert "marked_kinds" not in wednesday


def test_diff_highlight_prefers_exact_time_over_neighbour_pair():
    """Соседняя пара с общим часом не должна перехватывать строку изменённой."""
    only_changed = render_schedule_day_chunk_htmls(
        _DIFF_HL_SOURCE, "https://example.test", days_per_chunk=1,
        diff_items=[_DIFF_CHANGED], diff_legend="",
    )[0]
    only_changed_soup = BeautifulSoup(only_changed["html"], "html.parser")
    rows = only_changed_soup.select("tr.diff-row")
    assert len(rows) == 1
    assert rows[0].select_one("td").get_text(" ", strip=True) == "ИЗМЕНИЛИ 16:00 17:00"
    assert rows[0].select_one("td.diff-cell").get_text(" ", strip=True) == "по верхней неделе Антоново"
    # без легенды подписи под таблицей нет (сам CSS при диффе остаётся)
    assert only_changed_soup.select_one(".diff-legend") is None
    assert only_changed["marked_kinds"] == ["changed"]


def test_diff_legend_drops_removal_note_when_nothing_was_removed():
    """Строка про убранные пары нужна только когда их правда убирали."""
    chunk = render_schedule_day_chunk_htmls(
        _DIFF_HL_SOURCE, "https://example.test", days_per_chunk=1,
        diff_items=[_DIFF_CHANGED], diff_legend="Жёлтым подсвечены правки",
        removed_note=False,
    )[0]
    legend = chunk["html"][chunk["html"].find('class="diff-legend"'):]
    assert "ИЗМЕНИЛИ" in legend
    assert "Убранных пар на скрине уже нет" not in legend
    # зелёного образца тоже нет: новых пар на этом скрине не помечали
    assert "swatch added" not in legend
    assert chunk["marked_kinds"] == ["changed"]
