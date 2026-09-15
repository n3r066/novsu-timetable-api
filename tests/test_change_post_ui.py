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


def sentences(payload):
    return [inner for day in day_sections(payload) for inner in day["blocks"]
            if inner["type"] == "paragraph"]


def test_reported_single_rename_is_action_first_without_repeated_chrome():
    payload = build_changes_rich_message(rename_diff(), [], "https://example.test")
    validate_rich_payload(payload)
    blocks = payload["rich_message"]["blocks"]
    assert blocks[0]["type"] == "details"
    assert blocks[0]["summary"] == "Понедельник (переименовали пару)"
    # Название печатаем один раз: старое — префикс нового, поэтому показан
    # только подсвеченный добавленный хвост, а не два длинных названия.
    assert blocks[0]["blocks"][0] == {"type": "paragraph", "text": [
        "Пару ", "«",
        {"type": "bold", "text": OLD_NAME},
        " ",
        {"type": "marked", "text": "(второй иностранный язык)"},
        "»", " (", {"type": "italic", "text": "09:00–10:45"}, " · ", "верхняя неделя", ")",
        " дополнили в названии", ", ауд. 1318 прежняя", ".",
    ]}
    main = visible(payload)
    assert main.count(NEW_NAME) == 1
    assert "Раньше:" not in main
    assert "немецкий язык" in main
    for clutter in ("Поменяли расписание", "Коротко", "Что именно поменяли", "1 изменение",
                    "кто ведёт", "где:", "Пояснения", "место не указано"):
        assert clutter not in main
    assert not any(block["type"] in {"pullquote", "blockquote", "expandable_blockquote", "list"}
                   for block in blocks)
    # Неизменённый преподаватель в пост не попадает.
    assert "Барышева" not in main
    assert blocks[-1]["type"] == "footer"


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
    assert "Обнаружено на сайте 04.09.2026 в 00:00:00 МСК" in visible(before)
    assert "Обнаружено на сайте 04.12.2026 в 00:00:00 МСК" in visible(after)


def test_upper_lower_locations_are_separate_not_a_room_remote_slash():
    diff = rename_diff()
    other = {**diff["changed"][0], "room": "—", "location": None,
             "note": "немец. яз. по нижней неделе с использованием ДОТ", "delivery_mode": "remote_or_hybrid"}
    diff["changed"].append(other)
    original = copy.deepcopy(diff)
    payload = build_changes_rich_message(diff, [], "https://example.test")
    assert diff == original
    main = visible(payload)
    assert main.count(NEW_NAME) == 1
    assert "обе недели" in main
    assert "Верхняя неделя: ауд. 1318, Антоново\nНижняя неделя: ДОТ" in main
    assert "/ дистанционно" not in main and "ауд. —" not in main


@pytest.mark.parametrize(("label", "old", "new", "brief", "sentence"), [
    ("ауд.", "1318", "1331", "сменили аудиторию", "Аудитория: 1318 → 1331"),
    ("преподаватель", "Петров", "Иванов", "сменили преподавателя", "Преподаватель: Петров → Иванов"),
    ("формат", "in_person", "remote_or_hybrid", "изменили условия", "Формат: без ДОТ → с использованием ДОТ"),
    ("ауд.", "1318", "", "сменили аудиторию", "Аудитория: 1318 → не указано"),
])
def test_change_kind_has_specific_summary_and_new_value_emphasis(label, old, new, brief, sentence):
    diff = rename_diff()
    diff["changed"][0]["fields"] = [[label, old, new]]
    payload = build_changes_rich_message(diff, [], "https://example.test")
    validate_rich_payload(payload)
    days = day_sections(payload)
    assert [day["summary"] for day in days] == [f"Понедельник ({brief})"]
    change = next(block for block in sentences(payload) if sentence in text(block))
    styled = [run for run in change["text"] if isinstance(run, dict) and run.get("type") == "bold"]
    shown_new = {"in_person": "без ДОТ", "remote": "ДОТ",
                 "remote_or_hybrid": "с использованием ДОТ"}.get(new, new) if label == "формат" else new
    assert styled[-1] == {"type": "bold", "text": shown_new or "не указано"}
    assert "remote_or_hybrid" not in visible(payload)


def test_room_change_to_another_building_says_so_and_same_building_stays_silent():
    diff = rename_diff()
    diff["changed"][0]["fields"] = [["ауд.", "1318", "3207"]]
    main = visible(build_changes_rich_message(diff, [], "https://example.test"))
    assert "Другой корпус: корпус 3 — Б. Санкт-Петербургская, 41" in main
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
        "Понедельник (сменили преподавателя и аудиторию)"]
    main = visible(payload)
    assert "Преподаватель: Барышева Ангелина Алексеевна → " \
           "Иванова Ольга Петровна\nАудитория: 1318 → 415" in main
    # 1318 (старый корпус) → 415 (новый): здание другое, кампус тот же.
    assert "Другой корпус: новый корпус — кампус Антоново" in main


def test_dot_does_not_hide_physical_location_or_unknown_conditions():
    lesson = rename_diff()["changed"][0]
    lesson = {**lesson, "delivery_mode": "remote_or_hybrid",
              "note": "по верхней неделе, Антоново, с использованием ДОТ, консультация по согласованию с кафедрой"}
    payload = build_changes_rich_message({"added": [lesson]}, [], "https://example.test")
    main = visible(payload)
    assert "ауд. 1318" in main
    assert "ДОТ" in main
    assert "консультация по согласованию с кафедрой" in main


def test_context_conditions_keep_week_specific_association():
    diff = rename_diff()
    diff["changed"][0]["note"] = "по верхней неделе с 14.09"
    diff["changed"].append({**diff["changed"][0], "note": "по нижней неделе с 21.09"})
    main = visible(build_changes_rich_message(diff, [], "https://example.test"))
    assert "Верхняя неделя: с 14.09\nНижняя неделя: с 21.09" in main


def test_comment_edit_does_not_repeat_derived_location_and_mode_edits():
    diff = rename_diff()
    diff["changed"][0]["fields"] = [
        ["примечание", "Антоново", "с использованием ДОТ с 14.09"],
        ["место", "Антоново", ""], ["формат", "in_person", "remote_or_hybrid"],
    ]
    payload = build_changes_rich_message(diff, [], "https://example.test")
    main = visible(payload)
    assert "Условия: Антоново → с использованием ДОТ с 14.09" in main
    assert "изменился формат" not in main and "сменилось место" not in main
    assert main.count("с 14.09") == 1


def test_fallback_keeps_same_day_time_move_as_one_action_and_keeps_parity():
    lesson = rename_diff()["changed"][0]
    lesson = {key: value for key, value in lesson.items() if key != "fields"}
    fallback = changes_fallback_text({"removed": [lesson], "added": [{**lesson, "time": "11:00 12:00"}]},
                                     "https://example.test?a=1&b=2")
    assert fallback.count("перенесли пару") == 1
    assert "Перенесли: <s>09:00–10:45</s> → <b>11:00–12:45</b>" in fallback
    assert "убрали" not in fallback and "добавили" not in fallback
    assert len(fallback) <= 4096


def test_new_field_values_are_used_in_sentence_and_fallback():
    diff = rename_diff()
    diff["changed"][0]["subject"] = OLD_NAME
    diff["changed"][0]["fields"].append(["ауд.", "1318", "1331"])
    payload = build_changes_rich_message(diff, [], "https://example.test")
    main = visible(payload)
    assert "поменялось сразу несколько: название " in main
    assert "аудитория 1318 → 1331" in main
    fallback = changes_fallback_text(diff, "https://example.test")
    assert f"<b>{NEW_NAME}</b>" in fallback
    assert "аудитория <s>1318</s> → <b>1331</b>" in fallback


def test_condition_on_only_one_variant_still_has_week_label():
    diff = rename_diff()
    diff["changed"][0]["note"] = "по верхней неделе с 14.09"
    diff["changed"].append({**diff["changed"][0], "note": "по нижней неделе"})
    main = visible(build_changes_rich_message(diff, [], "https://example.test"))
    assert "Верхняя неделя: с 14.09" in main
    assert "\nс 14.09\n" not in main
    assert "Верхняя неделя: с 14.09" in changes_fallback_text(diff, "https://example.test")


def test_fallback_long_old_value_does_not_hide_new_value_or_clearing():
    diff = rename_diff()
    diff["changed"][0]["fields"] = [["примечание", "<&>" * 800, ""]]
    fallback = changes_fallback_text(diff, "https://example.test")
    assert "→ <b>не указано</b>" in fallback
    assert "<s>&lt;&amp;&gt;" in fallback
    assert "Условия:" in fallback
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
    assert "с 10.09" in main and "17:00–18:45" in main
    assert "место не указано" not in changes_fallback_text(diff, "https://example.test")


def test_day_summary_lists_every_change_and_details_hold_the_sentences():
    """Сводка дня называет обе правки, предложения ждут внутри раздела."""
    diff = {"added": [_lesson(room="301")],
            "removed": [_lesson(subject="Психология", time="09:00 10:00", room="1306", note="")],
            "changed": []}
    payload = build_changes_rich_message(diff, [], "https://example.test")
    validate_rich_payload(payload)
    days = day_sections(payload)
    assert [day["summary"] for day in days] == ["Среда · добавили пару, убрали пару"]
    summary = [text(block) for block in days[0]["blocks"] if block["type"] == "paragraph"]
    assert summary == ["Добавили пару:\n• Проектная деятельность", "Убрали пару:\n• Психология"]
    details = next(block for block in days[0]["blocks"] if str(block.get("summary", "")).startswith("Подробности"))
    assert not details.get("is_open")
    sentences_text = [text(block) for block in details["blocks"] if block["type"] == "paragraph"]
    assert sentences_text == [
        "Проектная деятельность\n17:00–18:45\nДобавили пару\nауд. 301\nс 10.09",
        "Психология\n09:00–10:45\nУбрали пару\nауд. 1306",
    ]


def test_time_comes_before_the_edit_and_old_value_is_cancelled():
    diff = rename_diff()
    diff["changed"][0]["fields"] = [["ауд.", "1318", "1331"]]
    payload = build_changes_rich_message(diff, [], "https://example.test")
    sentence = next(block for block in sentences(payload)
                    if "Аудитория:" in text(block))
    line = text(sentence)
    assert line.index("09:00–10:45") < line.index("1318 → 1331")
    assert {"type": "strikethrough", "text": "1318"} in sentence["text"]
    assert "У пары" not in line and "\nАудитория:" in line
    assert {"type": "bold", "text": "1331"} in sentence["text"]


def test_placeholder_old_value_is_not_struck_through():
    diff = rename_diff()
    diff["changed"][0]["fields"] = [["ауд.", "", "1331"]]
    payload = build_changes_rich_message(diff, [], "https://example.test")
    sentence = next(block for block in sentences(payload)
                    if "Аудитория:" in text(block))
    assert "Аудитория: 1331" in text(sentence)
    assert {"type": "strikethrough", "text": "не указано"} not in sentence["text"]
    assert {"type": "bold", "text": "1331"} in sentence["text"]


def test_highlighted_rename_tail_does_not_add_a_placeholder_line():
    """Хвост подсвечен в названии — отдельной «обновили запись» быть не должно."""
    diff = rename_diff()
    main = visible(build_changes_rich_message(diff, [], "https://example.test"))
    assert "обновили на портале" not in main
    assert "обновили на портале" not in changes_fallback_text(diff, "https://example.test")
    assert main.count("(второй иностранный язык)") == 1


def test_tail_highlight_normalizes_portal_whitespace_before_the_tail():
    """Пробельные узлы портала между названием и хвостом не уезжают в пост."""
    diff = rename_diff()
    noisy = f"{OLD_NAME}\t\t  (второй иностранный язык)"
    diff["changed"][0]["subject"] = noisy
    diff["changed"][0]["fields"] = [["предмет", OLD_NAME, noisy]]
    payload = build_changes_rich_message(diff, [], "https://example.test")
    sentence = next(block for block in sentences(payload) if "дополнили в названии" in text(block))
    joined = "".join(part if isinstance(part, str) else part["text"] for part in sentence["text"])
    assert "\t" not in joined
    assert NEW_NAME in joined
    assert {"type": "marked", "text": "(второй иностранный язык)"} in sentence["text"]
