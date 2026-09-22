"""ОПД virtual-group mates: раскрытый раздел «Вместе в ВГ», внутри раскрытые
разделы институтов с таблицами «ФИО · Группа» и объединением одногруппников."""
from collections import Counter
from copy import deepcopy

import pytest

from format import _opd_building_section, _opd_group_sort_key, _opd_mates_blocks, _opd_section
from telegram_api import validate_rich_payload


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_text(item) for item in value)
    if isinstance(value, dict):
        return _text(value.get("text", ""))
    return ""


def _validate(blocks):
    validate_rich_payload({"rich_message": {"blocks": blocks}})


def _person(student="Ширин А. А.", vg="116", *, mates=None, start="14:00", end="15:00"):
    return {
        "student": student, "vg": vg, "mates": mates,
        "status": "session", "block_start": start, "block_end": end,
        "building": "Антоново", "place": "ИЭ, Антоново", "room": "402",
        "teacher_short": "Трезорова О. Ю.", "note": "",
    }


def _many_mates():
    # Deliberately not in descending institute-size order. These names are synthetic.
    return [
        {"student": f"Студент{short}{index:02d} И. О.", "group": f"{base + index}", "institute": short}
        for short, count, base in [("ХТИ", 2, 6400), ("ИЭ", 8, 6000), ("ПИ", 5, 6200), ("ПТИ", 13, 6300)]
        for index in range(count)
    ]


def _sorted_expected(mates, institute):
    return sorted(
        ((mate["student"], mate["group"]) for mate in mates if mate["institute"] == institute),
        key=lambda item: (_opd_group_sort_key(item[1]), item[0]),
    )


def _assert_table(table, expected):
    assert table["type"] == "table"
    assert table["is_bordered"] is True
    assert table["is_striped"] is True
    cells = table["cells"]
    assert [_text(cell["text"]) for cell in cells[0]] == ["ФИО", "Группа"]
    assert all(cell.get("is_header") is True for cell in cells[0])
    assert len(cells) == len(expected) + 1
    for row in cells:
        assert all(cell["align"] == "left" and cell["valign"] == "middle" for cell in row)
    assert all(not cell.get("is_header") for row in cells[1:] for cell in row)
    assert _table_rows(table) == expected


def _table_rows(table):
    """Expand only the right-hand rowspans; every name must keep its own row."""
    rows = []
    remaining = 0
    group = None
    for row in table["cells"][1:]:
        assert "rowspan" not in row[0]
        if remaining:
            assert len(row) == 1
        else:
            assert len(row) == 2
            group = _text(row[1]["text"])
            remaining = row[1].get("rowspan", 1)
            assert isinstance(remaining, int) and remaining > 0
        rows.append((_text(row[0]["text"]), group))
        remaining -= 1
    assert remaining == 0, "group rowspan must end inside its own institute table"
    return rows


def _mates_sections(blocks, vg, count):
    """Единственный раскрытый раздел «Вместе в ВГ», внутри — раскрытые институты."""
    assert len(blocks) == 1
    outer = blocks[0]
    assert outer["type"] == "details" and outer.get("is_open") is True
    assert _text(outer["summary"]) == f"Вместе в ВГ {vg}  ·  {count} чел. из других групп"
    sections = outer["blocks"]
    assert sections
    for section in sections:
        assert section["type"] == "details" and section.get("is_open") is True
        assert len(section["blocks"]) == 1 and section["blocks"][0]["type"] == "table"
    return sections


def _section_table(section):
    return section["blocks"][0]


@pytest.mark.parametrize("student, vg", [("Ширин А. А.", "116"), ("Петрова Н. А.", "207")])
def test_each_student_has_all_28_mates_in_open_institute_sections(student, vg):
    mates = _many_mates()
    person = _person(student, vg, mates=mates)
    original = deepcopy(person)
    building = _opd_building_section("Антоново", [person])
    _validate([building])
    assert person == original
    details = building["blocks"][0]
    assert details["type"] == "details"
    assert not details.get("is_open", False)
    assert _text(details["summary"]) == (
        f"{student}  ·  ВГ {vg}  ·  14:00–15:00  ·  ауд. 402 · ИЭ  ·  Трезорова О. Ю."
    )
    sections = _mates_sections(details["blocks"], vg, 28)
    expected_titles = [
        ("ПТИ", "ПТИ · Политехнический институт · Б. Санкт-Петербургская, 41"),
        ("ИЭ", "ИЭ · Институт экономики · Антоново"),
        ("ПИ", "ПИ · Педагогический институт · ул. Псковская, 3"),
        ("ХТИ", "ХТИ · Химико-технологический институт · ул. Советской Армии, 7"),
    ]
    assert [_text(section["summary"]) for section in sections] == [title for _, title in expected_titles]
    all_rows = []
    for section, (institute, _) in zip(sections, expected_titles):
        expected = _sorted_expected(mates, institute)
        _assert_table(_section_table(section), expected)
        all_rows.extend(_table_rows(_section_table(section)))
    assert Counter(all_rows) == Counter((mate["student"], mate["group"]) for mate in mates)


def test_building_keeps_each_person_table_and_slot_room_teacher_summary():
    mate = {"student": "Соколов И. И.", "group": "6001", "institute": "ИЭ"}
    late = _person("Антонова А. А.", "207", mates=[mate], start="16:00", end="17:00")
    shifted = _person("Ширин А. А.", "116", mates=[mate], start="15:00", end="16:00")
    shifted["note"] = "с 15:00"
    early = _person("Яковлев Я. Я.", "105", mates=[mate])
    view = {
        "date": "2026-09-17", "rows": [late, shifted, early],
        "counts": {"session": 3, "cancelled": 0, "free": 0}, "sources": {},
    }
    section = _opd_section(view)
    _validate([section])
    assert "17.09  ·  идут 3 из 3" in _text(section["summary"])
    building = section["blocks"][0]
    people = building["blocks"]
    assert [_text(person["summary"]).split("  ·  ")[0] for person in people] == [
        "Яковлев Я. Я.", "Ширин А. А.", "Антонова А. А.",
    ]
    assert "ВГ 116  ·  15:00–16:00 с 15:00  ·  ауд. 402 · ИЭ  ·  Трезорова О. Ю." in _text(people[1]["summary"])
    assert "ВГ 207  ·  16:00–17:00  ·  ауд. 402 · ИЭ  ·  Трезорова О. Ю." in _text(people[2]["summary"])
    for person, source in zip(people, (early, shifted, late)):
        sections = _mates_sections(person["blocks"], source["vg"], 1)
        _assert_table(_section_table(sections[0]), [(mate["student"], mate["group"])])


def test_unknown_institutes_and_duplicate_names_keep_every_mate():
    mates = [
        {"student": "БезИнститута А. А.", "group": "7001"},
        {"student": "Одинаковый И. И.", "group": "7002", "institute": None},
        {"student": "Одинаковый И. И.", "group": "7003", "institute": ""},
        {"student": "БезАдреса Б. Б.", "group": "8001", "institute": "МИ"},
        {"student": "Новый В. В.", "group": "9001", "institute": "НОВЫЙ"},
        {"student": "Другой Г. Г.", "group": "9002", "institute": "НОВЫЙ"},
    ]
    blocks = _opd_mates_blocks(_person(mates=mates))
    _validate(blocks)
    sections = _mates_sections(blocks, "116", 6)
    # Preserve largest-first known institutes, with unresolved institutes last.
    assert [_text(section["summary"]) for section in sections] == [
        "НОВЫЙ", "МИ · Медицинский институт", "институт не определён",
    ]
    _assert_table(_section_table(sections[0]), [("Новый В. В.", "9001"), ("Другой Г. Г.", "9002")])
    _assert_table(_section_table(sections[1]), [("БезАдреса Б. Б.", "8001")])
    _assert_table(_section_table(sections[2]), [(mate["student"], mate["group"]) for mate in mates[:3]])


@pytest.mark.parametrize("vg", ["116", "207"])
def test_mates_sort_groups_and_names_and_merge_only_right_group_cells(vg):
    mates = [
        {"student": "Яковлев Я. Я.", "group": "10", "institute": "ИЭ"},
        {"student": "Сидорова С. С.", "group": "9", "institute": "ИЭ"},
        {"student": "Абрамова А. А.", "group": "10", "institute": "ИЭ"},
        {"student": "Белов Б. Б.", "group": "9", "institute": "ИЭ"},
        {"student": "Яковлев Я. Я.", "group": "10", "institute": "ИЭ"},
        {"student": "Абрамова А. А.", "group": "2", "institute": "ИЭ"},
    ]
    person = _person(vg=vg, mates=mates)
    original = deepcopy(person)
    building = _opd_building_section("Антоново", [person])
    _validate([building])
    assert person == original
    sections = _mates_sections(building["blocks"][0]["blocks"], vg, 6)
    assert len(sections) == 1
    table = _section_table(sections[0])
    expected = [
        ("Абрамова А. А.", "2"),
        ("Белов Б. Б.", "9"),
        ("Сидорова С. С.", "9"),
        ("Абрамова А. А.", "10"),
        ("Яковлев Я. Я.", "10"),
        ("Яковлев Я. Я.", "10"),
    ]
    _assert_table(table, expected)
    assert [len(row) for row in table["cells"]] == [2, 2, 2, 1, 2, 1, 1]
    assert "rowspan" not in table["cells"][1][1]
    assert table["cells"][2][1]["rowspan"] == 2
    assert table["cells"][4][1]["rowspan"] == 3
    assert Counter(_table_rows(table)) == Counter((mate["student"], mate["group"]) for mate in mates)
    # Input order cannot change presentation, and identical source records survive.
    assert _opd_mates_blocks(_person(vg=vg, mates=list(reversed(mates)))) == _opd_mates_blocks(person)


def test_same_academic_group_in_different_institutes_never_shares_a_span():
    mates = [
        {"student": "Яковлев Я. Я.", "group": "6001", "institute": "ИЭ"},
        {"student": "Сидоров С. С.", "group": "6001", "institute": "ПТИ"},
        {"student": "Абрамов А. А.", "group": "6001", "institute": "ИЭ"},
        {"student": "Белов Б. Б.", "group": "6001", "institute": "ПТИ"},
    ]
    blocks = _opd_mates_blocks(_person(mates=mates))
    _validate(blocks)
    sections = _mates_sections(blocks, "116", 4)
    assert [_text(section["summary"]) for section in sections] == [
        "ИЭ · Институт экономики · Антоново",
        "ПТИ · Политехнический институт · Б. Санкт-Петербургская, 41",
    ]
    _assert_table(_section_table(sections[0]), [("Абрамов А. А.", "6001"), ("Яковлев Я. Я.", "6001")])
    _assert_table(_section_table(sections[1]), [("Белов Б. Б.", "6001"), ("Сидоров С. С.", "6001")])
    assert all(_section_table(section)["cells"][1][1]["rowspan"] == 2 for section in sections)
    assert Counter(row for section in sections for row in _table_rows(_section_table(section))) == Counter(
        (mate["student"], mate["group"]) for mate in mates
    )


def test_missing_empty_and_whitespace_groups_never_merge_unknown_people():
    mates = [
        {"student": "Яковлев Я. Я.", "group": "", "institute": "ИЭ"},
        {"student": "Абрамов А. А.", "group": None, "institute": "ИЭ"},
        {"student": "Белов Б. Б.", "institute": "ИЭ"},
        {"student": "Сидоров С. С.", "group": "", "institute": "ИЭ"},
        {"student": "Титов Т. Т.", "group": " ", "institute": "ИЭ"},
        {"student": "Титов Т. Т.", "group": " ", "institute": "ИЭ"},
    ]
    original = deepcopy(mates)
    person = _person(mates=mates)
    blocks = _opd_mates_blocks(person)
    _validate(blocks)
    assert mates == original
    sections = _mates_sections(blocks, "116", 6)
    table = _section_table(sections[0])
    assert len(table["cells"]) == len(mates) + 1
    assert all(len(row) == 2 for row in table["cells"])
    assert all("rowspan" not in cell for row in table["cells"] for cell in row)
    assert Counter(_table_rows(table)) == Counter(
        (mate["student"], mate.get("group") or "—") for mate in mates
    )
    assert _opd_mates_blocks(_person(mates=list(reversed(mates)))) == blocks


def test_group_sorting_is_numeric_then_text_with_exact_distinct_group_labels():
    mates = [
        {"student": "Имя И. И.", "group": group, "institute": "ИЭ"}
        for group in ["Б-2", "10", "А-1", "2", "02", "Б-2", "2"]
    ]
    person = _person(mates=mates)
    blocks = _opd_mates_blocks(person)
    _validate(blocks)
    sections = _mates_sections(blocks, "116", 7)
    table = _section_table(sections[0])
    _assert_table(table, [("Имя И. И.", group) for group in ["02", "2", "2", "10", "А-1", "Б-2", "Б-2"]])
    assert "rowspan" not in table["cells"][1][1]  # "02" and "2" remain distinct.
    assert table["cells"][2][1]["rowspan"] == 2
    assert table["cells"][6][1]["rowspan"] == 2
    assert _opd_mates_blocks(_person(mates=list(reversed(mates)))) == blocks


@pytest.mark.parametrize("mates_state", [{}, {"mates": None}, {"mates": []}])
def test_empty_mates_keep_existing_fallback_without_empty_table(mates_state):
    person = _person()
    del person["mates"]
    person.update(mates_state)
    building = _opd_building_section("Антоново", [person])
    _validate([building])
    blocks = building["blocks"][0]["blocks"]
    assert blocks == [{
        "type": "paragraph", "text": "Состав виртуальной группы в таблице кафедры пока не найден.",
    }]
