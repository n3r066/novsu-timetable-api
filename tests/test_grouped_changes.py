import copy

from format import build_changes_rich_message
from telegram_api import validate_rich_payload


def plain(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(map(plain, value))
    if isinstance(value, dict):
        return "".join(plain(value[k]) for k in ("text", "blocks", "summary", "rich_message", "cells") if k in value)
    return ""


def lesson(name, time="09:00 10:00", **extra):
    return {"day": "Понедельник", "subject": name, "time": time, **extra}


def day(diff):
    original = copy.deepcopy(diff)
    payload = build_changes_rich_message(diff, [], "https://example.test")
    validate_rich_payload(payload)
    assert diff == original
    return next(b for b in payload["rich_message"]["blocks"] if b["type"] == "details")


def overview(section):
    return plain([b for b in section["blocks"] if b["type"] != "details"])


def rows(section):
    """Строки основной таблицы дня как списки текстов ячеек."""
    table = next(b for b in section["blocks"] if b["type"] == "table")
    return [[plain(cell["text"]) for cell in row] for row in table["cells"][1:]]


def has_details(section):
    return any(str(b.get("summary", "")).startswith("Подробности") for b in section["blocks"])



def test_each_occurrence_is_a_row_and_details_keep_week_and_time():
    diff = {"changed": [
        lesson("География туризма", "16:00 17:00", note="по верхней неделе", fields=[["преподаватель", "", "Ефимов Олег Николаевич"]]),
        lesson("География туризма", "17:00 18:00", note="по нижней неделе", fields=[["преподаватель", "—", "Ефимов Олег Николаевич"]]),
        lesson("История России", fields=[["преподаватель", "", "Миненко Трофим Сергеевич"]]),
    ]}
    section = day(diff)
    assert section["summary"] == "Понедельник · указали преподавателей (3 пары)"
    table = rows(section)
    assert [row[0] for row in table] == ["~\n09:00–10:45", "~\n16:00–17:45", "~\n17:00–18:45"]
    assert [row[1].split("\n")[0] for row in table] == ["История России", "География туризма", "География туризма"]
    # Новый преподаватель жирным, без «→»: раньше его просто не было.
    text = overview(section)
    assert text.count("Ефимов Олег Николаевич") == 2 and "→" not in text
    assert "верхняя неделя" in table[1][1] and "нижняя неделя" in table[2][1]
    assert not has_details(section)  # подробности убраны



def test_add_replace_and_remove_teacher_are_not_misrepresented_as_one_action():
    section = day({"changed": [
        lesson("История", fields=[["преподаватель", "", "Петров"]]),
        lesson("Физика", "11:00", fields=[["преподаватель", "Иванов", "Петров"]]),
        lesson("Химия", "13:00", fields=[["преподаватель", "Сидоров", ""]]),
    ]})
    assert section["summary"] == (
        "Понедельник · указали преподавателя, сменили преподавателя, преподаватель больше не указан")
    table = rows(section)
    assert table[0][1] == "История\nПетров"
    assert table[1][1] == "Физика\nИванов → Петров"
    assert table[2][1] == "Химия\nСидоров → не указано"



def test_room_deltas_are_shown_per_row_with_time():
    section = day({"changed": [
        lesson("История", fields=[["ауд.", "301", "302"]]),
        lesson("Физика", "11:00", fields=[["ауд.", "301", "302"]]),
        lesson("Химия", "13:00", fields=[["ауд.", "303", "302"]]),
    ]})
    table = rows(section)
    assert [row[2] for row in table] == ["301 → 302", "301 → 302", "303 → 302"]
    assert table[0][0] == "~\n09:00–10:45"



def test_unknown_notes_are_preserved_in_closed_details_without_dominating_overview():
    section = day({"changed": [
        lesson("История", fields=[["примечание", "ранняя запись", "только 25.09; консультация по согласованию"]]),
        lesson("Физика", "11:00", fields=[["примечание", "старый адрес", "уточнить место у деканата"]]),
    ]})
    assert section["summary"] == "Понедельник · изменили условия (2 пары)"
    assert "История" in overview(section) and "Физика" in overview(section)
    assert "консультация" not in overview(section)  # примечания не выводятся
    assert "ранняя запись" not in plain(section)  # примечания не выводятся
    assert "уточнить место" not in plain(section)  # примечания не выводятся



def test_mixed_teacher_and_room_changes_do_not_claim_unchanged_rooms():
    section = day({"changed": [
        lesson("История", fields=[["преподаватель", "", "Петров"]]),
        lesson("Физика", "11:00", fields=[["ауд.", "301", "302"]]),
    ]})
    table = rows(section)
    assert table[0][1] == "История\nПетров" and table[0][2] == "—"  # место не выдумываем
    assert table[1][2] == "301 → 302"
    assert "Время и аудитории не менялись" not in overview(section)



def test_html_fallback_has_the_same_rows_and_expandable_details():
    from format import changes_fallback_text
    diff = {"changed": [
        lesson("История <&>", note="только 25.09", fields=[["преподаватель", "", "Петров"]]),
        lesson("История <&>", "11:00", note="только 26.09", fields=[["преподаватель", "", "Петров"]]),
    ]}
    html = changes_fallback_text(diff, "https://example.test")
    assert "указали преподавателей (2 пары)" in html
    assert html.count("История &lt;&amp;&gt;") == 2 and html.count("<b>Петров</b>") == 2
    assert "25.09" not in html and "26.09" not in html  # примечания не выводятся
    assert "<blockquote expandable>" not in html  # подробности убраны
    assert len(html) <= 4096
