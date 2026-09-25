"""Каждая ситуация правки называется конкретным действием.

Сводка дня и колонка «что» говорят, что именно сделали с парой:
поменяли преподавателя, аудиторию, корпус; добавили/убрали/заменили/перенесли
пару; добавили/изменили/убрали примечание; перевели на ДОТ; отменили занятие.
Данные — в форме живого портала (место в примечании, ДОТ с «—» в аудитории).
"""
import copy

import pytest

import monitor
from format import build_changes_rich_message, changes_fallback_text
from telegram_api import validate_rich_payload

ROW = {
    "time": "9:00 10:00",
    "subject": "(пр.) Основы российской государственности\nпо верхней неделе, ИГУМ, Антоново",
    "room": "303", "teacher": "Кузина Ксения Александровна", "location": "ИГУМ, Антоново",
    "delivery_mode": "in_person", "subgroup": "", "note": "по верхней неделе, ИГУМ, Антоново",
}
OTHER = {
    "time": "14:00 15:00", "subject": "(пр.) Физическая культура и спорт", "room": "Спортзал",
    "teacher": "Карпов Евгений Евгеньевич", "location": "ИГУМ, Антоново",
    "delivery_mode": "in_person", "subgroup": "", "note": "ИГУМ, Антоново",
}


def plain(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(map(plain, value))
    if isinstance(value, dict):
        return plain(value.get("text", ""))
    return ""


def render(mutate, *, rows=None):
    old_rows = copy.deepcopy(rows or [ROW])
    new_rows = copy.deepcopy(old_rows)
    mutate(new_rows)
    diff = monitor.diff_schedules({"days": {"Среда": old_rows}}, {"days": {"Среда": new_rows}})
    payload = build_changes_rich_message(diff, [], "https://example.test")
    validate_rich_payload(payload)
    day = next(block for block in payload["rich_message"]["blocks"] if block["type"] == "details")
    table = next(block for block in day["blocks"] if block["type"] == "table")
    body = [[plain(cell["text"]) for cell in row] for row in table["cells"][1:]]
    return day["summary"], body, changes_fallback_text(diff, "https://example.test")


def edit(**changes):
    def mutate(rows):
        rows[0].update(changes)
    return mutate


@pytest.mark.parametrize(("mutate", "action"), [
    (edit(teacher="Смирнов Василий Андреевич"), "поменяли преподавателя"),
    (edit(teacher=""), "убрали преподавателя"),
    (edit(room="1331"), "поменяли корпус и аудиторию"),  # 303 — новый корпус, 1331 — старый
    (edit(room="304"), "поменяли аудиторию"),
    (edit(room="3312", location="ул.Б.Санкт.-Петербургская, д.41",
          note="по верхней неделе ПТИ,ул.Б.Санкт.-Петербургская, д.41"), "поменяли корпус и аудиторию"),
    (edit(note="по верхней неделе, ИГУМ, Антоново, с 07.10"), "добавили примечание"),
    (edit(note="по верхней неделе, ИГУМ, Антоново, 01.10 занятий не будет"), "отменили занятие"),
    (edit(note="по нижней неделе, ИГУМ, Антоново"), "поменяли неделю"),
    (edit(room="—", location=None, delivery_mode="remote_or_hybrid",
          note="по верхней неделе с использованием ДОТ"), "перевели на ДОТ"),
    (edit(subject="(лек.) Основы российской государственности\nпо верхней неделе, ИГУМ, Антоново"),
     "поменяли тип занятия"),
    (edit(subject="(пр.) Психология\nпо верхней неделе, ИГУМ, Антоново"), "переименовали пару"),
    (edit(subject="(лек.) Правоведение", teacher="Антонова Татьяна Александровна"), "заменили пару"),
    (edit(time="11:00 12:00"), "перенесли пару"),
    (lambda rows: rows.append(copy.deepcopy(OTHER)), "добавили пару"),
    (lambda rows: rows.pop(0), "убрали пару"),
])
def test_each_situation_has_its_own_action(mutate, action):
    summary, body, fallback = render(mutate)
    assert summary == f"Среда ({action})"
    assert len(body) == 1
    assert body[0][0].split("\n")[0] == action[:1].upper() + action[1:]
    assert f"<b>Среда ({action})</b>" in fallback
    assert len(fallback) <= 4096


def test_note_edits_show_old_and_new_without_place_and_week_noise():
    _summary, body, _ = render(edit(note="по верхней неделе, ИГУМ, Антоново, с 07.10"))
    assert body[0][1].endswith("примечание: с 07.10")
    _summary, body, _ = render(edit(note="по верхней неделе, ИГУМ, Антоново, с 14.10"),
                               rows=[{**ROW, "note": "по верхней неделе, ИГУМ, Антоново, с 07.10"}])
    assert body[0][0].startswith("Изменили примечание")
    assert body[0][1].endswith("примечание: с 07.10 → с 14.10")
    summary, body, _ = render(edit(note="по верхней неделе, ИГУМ, Антоново"),
                              rows=[{**ROW, "note": "по верхней неделе, ИГУМ, Антоново, с 07.10"}])
    assert summary == "Среда (убрали примечание)"
    assert body[0][1].endswith("примечание: с 07.10 → убрали")


def test_rewritten_place_in_note_is_not_announced_as_new_note():
    summary, body, _ = render(edit(note="по верхней неделе ИГУМ(Антоново)"))
    assert summary == "Среда (уточнили примечание)"
    assert "примечание:" not in body[0][1]


def test_dot_switch_shows_where_the_lesson_was_and_now_is():
    _summary, body, _ = render(edit(room="—", location=None, delivery_mode="remote_or_hybrid",
                                    note="по верхней неделе с использованием ДОТ"))
    assert body[0][2] == "ауд. 303, ИГУМ, Антоново → ДОТ"
    dot_row = {**ROW, "room": "—", "location": None, "delivery_mode": "remote_or_hybrid",
               "note": "по верхней неделе с использованием ДОТ"}
    summary, body, _ = render(edit(**{key: ROW[key] for key in ("room", "location", "delivery_mode", "note")}),
                              rows=[dot_row])
    assert summary == "Среда (убрали ДОТ)"
    assert body[0][2] == "ДОТ → ауд. 303, ИГУМ, Антоново"


def test_replaced_lesson_is_one_row_with_old_and_new():
    summary, body, _ = render(edit(subject="(лек.) Правоведение", teacher="Антонова Татьяна Александровна",
                                   room="1306"))
    assert summary == "Среда (заменили пару)"
    assert body[0][1].startswith("Основы российской государственности\nПравоведение")
    assert "Кузина Ксения Александровна → Антонова Татьяна Александровна" in body[0][1]
    assert body[0][2].startswith("303 → 1306")


def test_ambiguous_slot_is_not_guessed_as_replacement():
    """Два ушли и два пришли в один слот — не угадываем, кто кого заменил."""
    twin = {**ROW, "subgroup": "", "teacher": "Иванов И. И.", "subject": "(пр.) Химия"}

    def mutate(rows):
        rows[:] = [{**ROW, "subject": "(пр.) Физика", "teacher": "Петров П. П."},
                   {**ROW, "subject": "(пр.) Биология", "teacher": "Сидоров С. С."}]

    summary, body, _ = render(mutate, rows=[ROW, twin])
    assert "заменили" not in summary
    assert summary == "Среда · добавили пары (2 пары), убрали пары (2 пары)"


def test_real_portal_rewrites_of_the_same_place_are_not_a_building_change():
    """Живые правки портала от 22.09: место переписали, корпус тот же."""
    old = {**ROW, "location": "Антоново", "note": "по верхней неделе Антоново"}
    summary, body, _ = render(edit(location="ИГУМ, Антоново", note="по верхней неделе, ИГУМ, Антоново"),
                              rows=[old])
    assert summary == "Среда (уточнили корпус)"
    assert "примечание:" not in body[0][1]
    spb = {**ROW, "time": "19:00 20:00", "room": "3312", "location": "Б.С.-Петербургская, 41",
           "note": "по нижней неделе Б.С.-Петербургская, 41"}
    summary, body, _ = render(edit(location="ул.Б.Санкт.-Петербургская, д.41",
                                   note="англ. язык по нижней неделе ПТИ,ул.Б.Санкт.-Петербургская, д.41"),
                              rows=[spb])
    assert "поменяли корпус" not in summary
    assert summary == "Среда (уточнили корпус, добавили примечание)"
    assert body[0][1].endswith("примечание: английский язык")  # без хвоста «ПТИ»


def test_several_edits_of_one_lesson_are_listed_together():
    summary, body, _ = render(edit(teacher="Смирнов Василий Андреевич", room="304",
                                   note="по верхней неделе, ИГУМ, Антоново, с 07.10"))
    assert summary == "Среда (поменяли преподавателя и аудиторию, добавили примечание)"
    assert body[0][0].startswith("Поменяли преподавателя и аудиторию, добавили примечание")
