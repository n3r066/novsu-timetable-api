import copy
import datetime as dt

import pytest

from format import build_changes_rich_message, changes_fallback_text
from telegram_api import validate_rich_payload


OLD_NAME = "Иностранные языки в сфере профессиональной коммуникации"
NEW_NAME = "Иностранные языки в сфере профессиональной коммуникации (второй иностранный язык)"


def rename_diff():
    return {"added": [], "removed": [], "changed": [{
        "day": "Понедельник", "subject": NEW_NAME, "time": "09:00 10:00",
        "teacher": "Барышева Ангелина Алексеевна", "room": "1318",
        "location": "Антоново", "note": "немец. яз. по верхней неделе, Антоново",
        "delivery_mode": "in_person", "fields": [["предмет", OLD_NAME, NEW_NAME]],
    }]}


def text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(text(item) for item in value)
    if isinstance(value, dict):
        return text(value.get("text", ""))
    return ""


def visible(payload):
    """Весь текст поста, включая содержимое раскрывающихся разделов."""
    def walk(node):
        if isinstance(node, str):
            return node
        if isinstance(node, list):
            return "".join(walk(item) for item in node)
        if isinstance(node, dict):
            return "".join(walk(value) for key, value in node.items()
                           if key not in {"type", "url", "media"})
        return ""
    return "\n".join(walk(block) for block in payload["rich_message"]["blocks"])


def day_sections(payload):
    return [block for block in payload["rich_message"]["blocks"] if block["type"] == "details"]


def main_rows(payload):
    """Строки основных таблиц всех дней (без шапки): список ячеек."""
    rows = []
    for day in day_sections(payload):
        for inner in day["blocks"]:
            if inner["type"] == "table":
                rows.extend(row for row in inner["cells"] if not any(cell.get("is_header") for cell in row))
    return rows


def main_cells(payload):
    return [cell for row in main_rows(payload) for cell in row]


def main_text(payload):
    """Текст поста без содержимого свёрнутых «Подробностей»."""
    parts = []
    for day in day_sections(payload):
        parts.append(text(day["summary"]))
        for inner in day["blocks"]:
            if inner["type"] == "details":
                continue
            parts.append(text(inner.get("text")) if inner["type"] != "table"
                         else "\n".join(text(cell["text"]) for row in inner["cells"] for cell in row))
    return "\n".join(parts)



def test_reported_single_rename_is_action_first_without_repeated_chrome():
    payload = build_changes_rich_message(rename_diff(), [], "https://example.test")
    validate_rich_payload(payload)
    blocks = payload["rich_message"]["blocks"]
    assert blocks[0]["type"] == "details"
    assert blocks[0]["summary"] == "Понедельник (уточнили название)"
    # День — только таблица правок, без абзацев-прозы.
    assert [inner["type"] for inner in blocks[0]["blocks"]] == ["table"]
    what, lesson, where = main_rows(payload)[0]
    # Колонка «что» называет действие словами, а не значком «~».
    assert text(what["text"]) == "Уточнили название\n09:00–10:45"
    # Название печатаем один раз: старое — префикс нового, поэтому показан
    # только подсвеченный добавленный хвост, а не два длинных названия.
    assert lesson["text"][:4] == [
        {"type": "bold", "text": OLD_NAME}, " ",
        {"type": "marked", "text": "(второй иностранный язык)"}, "\n",
    ]
    assert {"type": "italic", "text": "верхняя неделя"} in lesson["text"]
    assert {"type": "italic", "text": "Барышева Ангелина Алексеевна"} in lesson["text"]
    assert text(where["text"]) == "ауд. 1318, Антоново"
    main = main_text(payload)
    assert main.count(NEW_NAME) == 1
    assert "Раньше:" not in main
    assert "немец. яз" not in visible(payload)  # примечания больше не выводятся
    for clutter in ("Поменяли расписание", "Коротко", "Что именно поменяли", "1 изменение",
                    "кто ведёт", "где:", "Пояснения", "место не указано", "Пару «", "дополнили"):
        assert clutter not in visible(payload)
    assert not any(block["type"] in {"pullquote", "blockquote", "expandable_blockquote", "list"}
                   for block in blocks)
    assert not any(b.get("type") == "footer" for b in blocks)
    assert "Группа" not in visible(payload)


def test_missing_time_does_not_create_empty_badge_or_invent_date():
    diff = rename_diff()
    diff["changed"][0].pop("time")
    before = build_changes_rich_message(diff, [], "https://example.test", now=dt.datetime(2026, 9, 4))
    after = build_changes_rich_message(diff, [{"week": 20, "half": "bottom", "start": "01.12.2026", "end": "07.12.2026"}],
                                       "https://example.test", now=dt.datetime(2026, 12, 4))
    # У пары нет времени — пост не выдумывает ни дефис-заглушку, ни календарную
    # неделю правки. Время обнаружения при этом своё у каждого поста.
    assert "—" not in visible(before)
    assert "Неделя 20" not in visible(after)
    assert "Поменяли расписание в 00:00" in visible(before)
    assert "Поменяли расписание в 00:00" in visible(after)



def test_upper_lower_locations_are_separate_not_a_room_remote_slash():
    diff = rename_diff()
    other = {**diff["changed"][0], "room": "—", "location": None,
             "note": "немец. яз. по нижней неделе с использованием ДОТ", "delivery_mode": "remote_or_hybrid"}
    diff["changed"].append(other)
    original = copy.deepcopy(diff)
    payload = build_changes_rich_message(diff, [], "https://example.test")
    assert diff == original
    assert len(main_rows(payload)) == 1  # верх и низ одной пары — одна строка
    main = main_text(payload)
    assert main.count(NEW_NAME) == 1
    assert "обе недели" in main
    assert "Верхняя неделя: ауд. 1318, Антоново\nНижняя неделя: ДОТ" in main
    assert "/ дистанционно" not in main and "ауд. —" not in main



@pytest.mark.parametrize(("label", "old", "new", "brief", "delta"), [
    ("ауд.", "1318", "1331", "поменяли аудиторию", "1318 → 1331"),
    ("преподаватель", "Петров", "Иванов", "поменяли преподавателя", "Петров → Иванов"),
    ("формат", "in_person", "remote_or_hybrid", "перевели на ДОТ", "без ДОТ → с использованием ДОТ"),
    ("ауд.", "1318", "", "убрали аудиторию", "1318 → не указано"),
])
def test_change_kind_has_specific_summary_and_new_value_emphasis(label, old, new, brief, delta):
    diff = rename_diff()
    diff["changed"][0]["fields"] = [[label, old, new]]
    payload = build_changes_rich_message(diff, [], "https://example.test")
    validate_rich_payload(payload)
    days = day_sections(payload)
    assert [day["summary"] for day in days] == [f"Понедельник ({brief})"]
    cell = next(cell for cell in main_cells(payload) if delta in text(cell["text"]))
    styled = [run for run in cell["text"] if isinstance(run, dict) and run.get("type") == "bold"]
    shown_new = {"in_person": "без ДОТ", "remote": "ДОТ",
                 "remote_or_hybrid": "с использованием ДОТ"}.get(new, new) if label == "формат" else new
    assert styled[-1] == {"type": "bold", "text": shown_new or "не указано"}
    assert {"type": "strikethrough", "text": {"in_person": "без ДОТ"}.get(old, old)} in cell["text"]
    assert "remote_or_hybrid" not in visible(payload)


def test_room_change_to_another_building_says_so_and_same_building_stays_silent():
    diff = rename_diff()
    diff["changed"][0]["fields"] = [["ауд.", "1318", "3207"]]
    main = visible(build_changes_rich_message(diff, [], "https://example.test"))
    assert "другой корпус: корпус 3 — Б. Санкт-Петербургская, 41" in main
    diff["changed"][0]["fields"] = [["ауд.", "418", "415"]]
    main = visible(build_changes_rich_message(diff, [], "https://example.test"))
    assert "другой корпус" not in main
    assert "Антоново" not in main
    # Нечитаемый номер — молчим, корпус не выдумываем.
    diff["changed"][0]["fields"] = [["ауд.", "Спортзал", "3207"]]
    assert "другой корпус" not in visible(build_changes_rich_message(diff, [], "https://example.test"))



def test_multi_field_change_names_fields_and_flags_building_move():
    diff = rename_diff()
    diff["changed"][0]["fields"] = [
        ["преподаватель", "Барышева Ангелина Алексеевна", "Иванова Ольга Петровна"],
        ["ауд.", "1318", "415"],
    ]
    payload = build_changes_rich_message(diff, [], "https://example.test")
    days = day_sections(payload)
    assert [day["summary"] for day in days] == [
        "Понедельник (поменяли преподавателя, корпус и аудиторию)"]
    _what, lesson, where = main_rows(payload)[0]
    assert text(lesson["text"]).endswith("\nБарышева Ангелина Алексеевна → Иванова Ольга Петровна")
    # 1318 (старый корпус) → 415 (новый): здание другое, кампус тот же.
    assert text(where["text"]) == "1318 → 415\nдругой корпус: новый корпус — кампус Антоново"
    assert {"type": "strikethrough", "text": "1318"} in where["text"]
    assert {"type": "bold", "text": "415"} in where["text"]


def test_dot_does_not_hide_physical_location_or_unknown_conditions():
    lesson = rename_diff()["changed"][0]
    lesson = {**lesson, "delivery_mode": "remote_or_hybrid",
              "note": "по верхней неделе, Антоново, с использованием ДОТ, консультация по согласованию с кафедрой"}
    payload = build_changes_rich_message({"added": [lesson]}, [], "https://example.test")
    main = visible(payload)
    assert "ауд. 1318" in main
    assert "ДОТ" in main
    assert "консультация по согласованию с кафедрой" not in main  # примечания не выводятся


def test_context_conditions_keep_week_specific_association():
    diff = rename_diff()
    diff["changed"][0]["note"] = "по верхней неделе с 14.09"
    diff["changed"].append({**diff["changed"][0], "note": "по нижней неделе с 21.09"})
    main = visible(build_changes_rich_message(diff, [], "https://example.test"))
    assert "ауд. 1318" in main
    assert "с 14.09" not in main  # примечания не выводятся



def test_comment_edit_does_not_repeat_derived_location_and_mode_edits():
    diff = rename_diff()
    diff["changed"][0]["fields"] = [
        ["примечание", "Антоново", "с использованием ДОТ с 14.09"],
        ["место", "Антоново", ""], ["формат", "in_person", "remote_or_hybrid"],
    ]
    payload = build_changes_rich_message(diff, [], "https://example.test")
    main = visible(payload)
    assert "ауд. 1318, ДОТ" in main  # место и формат выведены в ячейку «где»
    # Смысловая часть примечания новая («с 14.09») — её показываем, а ДОТ и
    # место внутри примечания — отдельными действиями, без повтора.
    assert day_sections(payload)[0]["summary"] == "Понедельник (перевели на ДОТ, добавили примечание)"
    assert "с 14.09" in main
    assert "изменился формат" not in main and "сменилось место" not in main
    assert "без ДОТ →" not in main



def test_fallback_keeps_same_day_time_move_as_one_action_and_keeps_parity():
    lesson = rename_diff()["changed"][0]
    lesson = {key: value for key, value in lesson.items() if key != "fields"}
    fallback = changes_fallback_text({"removed": [lesson], "added": [{**lesson, "time": "11:00 12:00"}]},
                                     "https://example.test?a=1&b=2")
    assert fallback.count("перенесли пару") == 1
    assert "<s>09:00–10:45</s> → <b>11:00–12:45</b>" in fallback
    assert "верхняя неделя" in fallback
    assert "убрали" not in fallback and "добавили" not in fallback
    assert len(fallback) <= 4096



def test_new_field_values_are_used_in_sentence_and_fallback():
    diff = rename_diff()
    diff["changed"][0]["subject"] = OLD_NAME
    diff["changed"][0]["fields"].append(["ауд.", "1318", "1331"])
    payload = build_changes_rich_message(diff, [], "https://example.test")
    _what, lesson, where = main_rows(payload)[0]
    # Новое название взято из полей правки, а не из устаревшего контекста.
    assert {"type": "marked", "text": "(второй иностранный язык)"} in lesson["text"]
    assert text(where["text"]) == "1318 → 1331"
    fallback = changes_fallback_text(diff, "https://example.test")
    assert "<b>Иностранные языки в сфере профессиональной коммуникации</b>" in fallback
    assert "(второй иностранный язык)" in fallback  # хвост переименования
    assert "<s>1318</s> → <b>1331</b>" in fallback


def test_condition_on_only_one_variant_still_has_week_label():
    diff = rename_diff()
    diff["changed"][0]["note"] = "по верхней неделе с 14.09"
    diff["changed"].append({**diff["changed"][0], "note": "по нижней неделе"})
    main = visible(build_changes_rich_message(diff, [], "https://example.test"))
    assert "ауд. 1318" in main  # место показано
    assert "с 14.09" not in main  # примечания не выводятся
    assert "с 14.09" not in changes_fallback_text(diff, "https://example.test")


def test_fallback_long_old_value_does_not_hide_new_value_or_clearing():
    diff = rename_diff()
    diff["changed"][0]["fields"] = [["примечание", "<&>" * 800, ""]]
    fallback = changes_fallback_text(diff, "https://example.test")
    assert "(убрали примечание)" in fallback
    # Старое примечание зачёркнуто и обрезано, экранировано, дальше — «убрали».
    assert "<s>&lt;&amp;&gt;" in fallback and "…</s> → убрали" in fallback
    assert "<blockquote expandable><b>Подробности</b>" not in fallback
    assert len(fallback) <= 4096


def _lesson(**overrides):
    base = {"day": "Среда", "subject": "Проектная деятельность", "time": "17:00 18:00",
            "room": "", "teacher": "", "location": "", "note": "с 10.09",
            "delivery_mode": "in_person"}
    return {**base, **overrides}


def test_unknown_place_is_not_announced():
    """Пустое место — не новость: пост молчит, как уже молчит про пустое время."""
    diff = {"added": [_lesson()], "removed": [], "changed": []}
    main = visible(build_changes_rich_message(diff, [], "https://example.test"))
    assert "место не указано" not in main
    assert "17:00–18:45" in main  # время показано
    assert "с 10.09" not in main  # примечания не выводятся
    assert "место не указано" not in changes_fallback_text(diff, "https://example.test")



def test_day_summary_lists_every_change_and_details_hold_the_sentences():
    """Сводка дня называет обе правки, таблица — строку на каждую."""
    diff = {"added": [_lesson(room="301")],
            "removed": [_lesson(subject="Психология", time="09:00 10:00", room="1306", note="")],
            "changed": []}
    payload = build_changes_rich_message(diff, [], "https://example.test")
    validate_rich_payload(payload)
    days = day_sections(payload)
    assert [day["summary"] for day in days] == ["Среда · добавили пару, убрали пару"]
    assert [inner["type"] for inner in days[0]["blocks"]] == ["table"]
    rows = [[text(cell["text"]) for cell in row] for row in main_rows(payload)]
    assert rows == [
        ["Добавили пару\n17:00–18:45", "Проектная деятельность", "ауд. 301"],
        ["Убрали пару\n09:00–10:45", "Психология", "ауд. 1306"],
    ]
    _what, _lesson_cell, where = main_rows(payload)[1]
    assert where["text"] == [{"type": "strikethrough", "text": "ауд. 1306"}]
    assert not any(str(block.get("summary", "")).startswith("Подробности")
                   for block in days[0]["blocks"])



def test_time_comes_before_the_edit_and_old_value_is_cancelled():
    diff = rename_diff()
    diff["changed"][0]["fields"] = [["ауд.", "1318", "1331"]]
    payload = build_changes_rich_message(diff, [], "https://example.test")
    what, _lesson_cell, where = main_rows(payload)[0]
    assert "09:00–10:45" in text(what["text"])  # время — в первой колонке, правка — в последней
    assert {"type": "strikethrough", "text": "1318"} in where["text"]
    assert {"type": "bold", "text": "1331"} in where["text"]
    assert "У пары" not in visible(payload) and "Аудитория:" not in visible(payload)



def test_placeholder_old_value_is_not_struck_through():
    diff = rename_diff()
    diff["changed"][0]["fields"] = [["ауд.", "", "1331"]]
    payload = build_changes_rich_message(diff, [], "https://example.test")
    _what, _lesson_cell, where = main_rows(payload)[0]
    assert where["text"] == [{"type": "bold", "text": "1331"}]
    assert "не указано" not in visible(payload)



def test_highlighted_rename_tail_does_not_add_a_placeholder_line():
    """Хвост подсвечен в названии — отдельной «обновили запись» быть не должно."""
    diff = rename_diff()
    payload = build_changes_rich_message(diff, [], "https://example.test")
    assert "обновили на портале" not in visible(payload)
    assert "обновили на портале" not in changes_fallback_text(diff, "https://example.test")
    assert main_text(payload).count("(второй иностранный язык)") == 1



def test_tail_highlight_normalizes_portal_whitespace_before_the_tail():
    """Пробельные узлы портала между названием и хвостом не уезжают в пост."""
    diff = rename_diff()
    noisy = f"{OLD_NAME}\t\t  (второй иностранный язык)"
    diff["changed"][0]["subject"] = noisy
    diff["changed"][0]["fields"] = [["предмет", OLD_NAME, noisy]]
    payload = build_changes_rich_message(diff, [], "https://example.test")
    cell = next(cell for cell in main_cells(payload)
                if {"type": "marked", "text": "(второй иностранный язык)"} in cell["text"])
    joined = text(cell["text"])
    assert "\t" not in joined
    assert joined.startswith(NEW_NAME)
