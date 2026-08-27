from bs4 import BeautifulSoup

from portal_parser import find_schedule_table, parse_groups, parse_institutes


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
