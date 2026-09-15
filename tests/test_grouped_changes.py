import copy

from format import build_changes_rich_message
from telegram_api import validate_rich_payload


def plain(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(map(plain, value))
    if isinstance(value, dict):
        return "".join(plain(value[k]) for k in ("text", "blocks", "summary", "rich_message") if k in value)
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


def details(section):
    return next(b for b in section["blocks"] if str(b.get("summary", "")).startswith("Подробности"))


def test_same_teacher_and_subject_are_combined_and_occurrences_remain_in_details():
    diff = {"changed": [
        lesson("География туризма", "16:00 17:00", note="по верхней неделе", fields=[["преподаватель", "", "Ефимов Олег Николаевич"]]),
        lesson("География туризма", "17:00 18:00", note="по нижней неделе", fields=[["преподаватель", "—", "Ефимов Олег Николаевич"]]),
        lesson("История России", fields=[["преподаватель", "", "Миненко Трофим Сергеевич"]]),
    ]}
    section = day(diff)
    text = overview(section)
    assert section["summary"] == "Понедельник · указали преподавателей (3 пары)"
    assert text.count("Указали преподавателей") == 1
    assert text.count("География туризма") == 1 and "География туризма (2 пары)" in text
    assert text.count("Ефимов Олег Николаевич") == 1
    assert "Время и аудитории не менялись." in text
    assert "16:00" not in text and "17:00" not in text
    full = plain(details(section))
    assert "16:00–17:45" in full and "17:00–18:45" in full
    assert "верхняя неделя" in full and "нижняя неделя" in full
    assert not details(section).get("is_open")


def test_add_replace_and_remove_teacher_are_not_misrepresented_as_one_action():
    section = day({"changed": [
        lesson("История", fields=[["преподаватель", "", "Петров"]]),
        lesson("Физика", "11:00", fields=[["преподаватель", "Иванов", "Петров"]]),
        lesson("Химия", "13:00", fields=[["преподаватель", "Сидоров", ""]]),
    ]})
    text = overview(section)
    assert "Указали преподавателя:" in text
    assert "Сменили преподавателя:" in text
    assert "Преподаватель больше не указан:" in text
    assert "Иванов → Петров" in plain(details(section))
    assert "Сидоров → не указано" in plain(details(section))


def test_different_subjects_with_identical_room_delta_share_one_row():
    section = day({"changed": [
        lesson("История", fields=[["ауд.", "301", "302"]]),
        lesson("Физика", "11:00", fields=[["ауд.", "301", "302"]]),
        lesson("Химия", "13:00", fields=[["ауд.", "303", "302"]]),
    ]})
    text = overview(section)
    assert "История, Физика — 301 → 302" in text
    assert "Химия — 303 → 302" in text
    assert text.count("301 → 302") == 1
    assert "Время и аудитории не менялись" not in text
    assert "09:00–10:45" in plain(details(section))


def test_unknown_notes_are_preserved_in_closed_details_without_dominating_overview():
    section = day({"changed": [
        lesson("История", fields=[["примечание", "ранняя запись", "только 25.09; консультация по согласованию"]]),
        lesson("Физика", "11:00", fields=[["примечание", "старый адрес", "уточнить место у деканата"]]),
    ]})
    assert "Изменили условия:" in overview(section)
    assert "История, Физика" in overview(section)
    assert "консультация" not in overview(section)
    assert "ранняя запись → только 25.09; консультация по согласованию" in plain(details(section))
    assert "старый адрес → уточнить место у деканата" in plain(details(section))


def test_mixed_teacher_and_room_changes_do_not_claim_unchanged_rooms():
    section = day({"changed": [
        lesson("История", fields=[["преподаватель", "", "Петров"]]),
        lesson("Физика", "11:00", fields=[["ауд.", "301", "302"]]),
    ]})
    assert "Время и аудитории не менялись" not in overview(section)
    assert "Указали преподавателя" in overview(section) and "Сменили аудиторию" in overview(section)


def test_html_fallback_has_the_same_grouped_overview_and_expandable_details():
    from format import changes_fallback_text
    diff = {"changed": [
        lesson("История <&>", note="только 25.09", fields=[["преподаватель", "", "Петров"]]),
        lesson("История <&>", "11:00", note="только 26.09", fields=[["преподаватель", "", "Петров"]]),
    ]}
    html = changes_fallback_text(diff, "https://example.test")
    visible, hidden = html.split("<blockquote expandable>", 1)
    assert "Указали преподавателей:" in visible
    assert visible.count("История &lt;&amp;&gt;") == 1 and visible.count("Петров") == 1
    assert "25.09" not in visible and "26.09" not in visible
    assert "только 25.09" in hidden and "только 26.09" in hidden
    assert "09:00–10:45" in hidden and "11:00–11:45" in hidden
    assert "</blockquote>" in hidden and len(html) <= 4096
