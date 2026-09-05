import json
from pathlib import Path
import datetime as dt

from format import (
    build_changes_rich_message,
    build_dashboard_rich_message,
    build_rich_message,
    changes_fallback_text,
    format_schedule_post,
    render_schedule_html,
)
from parse import parse_all
from telegram_api import validate_rich_payload


def _load():
    html = Path("tests/fixtures/rowspan_time.html").read_text(encoding="utf-8")
    return parse_all(html)


def _plain(node) -> str:
    """Текст rich-блока без разметки.

    Акценты рвут строку на несколько ранов («время:» жирным, значение обычным),
    поэтому по json-дампу фразу «время: 11:00–12:45 → 17:00–18:45» не найти —
    проверяем смысл по склеенному тексту.
    """
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_plain(part) for part in node)
    if isinstance(node, dict):
        # Только текстовые ключи: «type» и «url» в читаемый текст не входят.
        return "".join(
            _plain(node[key])
            for key in ("rich_message", "text", "summary", "caption", "blocks", "items")
            if node.get(key) is not None
        )
    return ""


def test_build_rich_message_groups_lessons_by_parity():
    import json
    data = _load()
    rm = build_rich_message(data["schedule"], data["weeks"], "https://example.test")
    assert isinstance(rm, dict) and "rich_message" in rm
    blob = json.dumps(rm, ensure_ascii=False)
    # Предмет А — по верхней неделе, Предмет Б — по нижней неделе с ДОТ.
    assert "Верхняя неделя" in blob
    assert "Нижняя неделя" in blob
    # Паритетная фраза зачищена из примечаний внутри групп.
    assert "по верхней неделе" not in blob
    assert "по нижней неделе" not in blob
    # Нечётная/чётная пометки в шапке присутствуют.
    assert "нечётные" in blob and "чётные" in blob


def test_render_schedule_html_groups_by_parity():
    data = _load()
    html = render_schedule_html(data["schedule"], data["weeks"], "https://example.test")
    assert html.startswith("<!doctype html>")
    assert "Расписание группы 6381" in html
    assert "Верхняя неделя" in html
    assert "Нижняя неделя" in html
    # parity phrase stripped from inline notes
    assert "по верхней неделе" not in html
    assert "по нижней неделе" not in html


def test_format_returns_list_of_strings_under_limit():
    data = _load()
    msgs = format_schedule_post(data["schedule"], data["weeks"], "https://example.test")
    assert isinstance(msgs, list) and msgs
    assert all(isinstance(m, str) for m in msgs)
    assert all(len(m) <= 4096 for m in msgs)
    assert len(msgs[0]) <= 1024  # photo caption limit
    joined = "\n".join(msgs)
    assert "Четверг" in joined
    assert "Предмет А" in joined
    assert "Предмет Б" in joined
    assert "открыть на портале" in msgs[0]


def test_format_splits_when_a_day_would_exceed_limit():
    data = _load()
    day = list(data["schedule"]["days"])[0]
    pair = data["schedule"]["days"][day][0]
    data["schedule"]["days"][day] = [pair for _ in range(500)]  # huge day
    msgs = format_schedule_post(data["schedule"], data["weeks"], "https://example.test")
    assert len(msgs) >= 2
    assert all(len(m) <= 4096 for m in msgs)


def test_format_escapes_html_in_subjects():
    data = _load()
    day = list(data["schedule"]["days"])[0]
    data["schedule"]["days"][day][0]["subject"] = "<script>x</script>"
    data["schedule"]["days"][day][0]["note"] = "a & b < c"
    msgs = format_schedule_post(data["schedule"], data["weeks"], "https://example.test")
    joined = "\n".join(msgs)
    assert "<script>" not in joined
    assert "&lt;script&gt;" in joined
    assert "a &amp; b &lt; c" in joined


def test_format_empty_schedule_is_safe():
    msgs = format_schedule_post(None, [], "https://example.test")
    assert isinstance(msgs, list) and msgs
    assert all(isinstance(m, str) for m in msgs)
    assert all(len(m) <= 4096 for m in msgs)


def _sample_diff():
    return {
        "added": [{"day": "Вторник", "time": "09:00 10:30", "subject": "(лек.) Новый", "room": "101", "teacher": "Иванов"}],
        "removed": [{"day": "Четверг", "time": "15:00 16:00", "subject": "(пр.) Старый", "room": "202", "teacher": "Петров"}],
        "changed": [{"day": "Среда", "time": "11:00 12:30", "subject": "Экономика",
                     "fields": [("ауд.", "203", "415")]}],
        "transition": None,
    }


def test_changes_rich_message_structure():
    import json
    rm = build_changes_rich_message(_sample_diff(), [], "https://example.test",
                                    now=dt.datetime(2026, 9, 2, 12, 0))
    assert "rich_message" in rm
    blocks = rm["rich_message"]["blocks"]
    assert blocks[0]["type"] == "pullquote"
    blob = json.dumps(rm, ensure_ascii=False)
    assert "Поменяли расписание" in blob
    assert "3 изменения" in blob
    # правки сгруппированы по дням в порядке недели, а не по типу изменения
    summaries = [block.get("summary") for block in blocks if block.get("type") == "details"]
    assert summaries[:3] == ["Вторник — 1 изменение", "Среда — 1 изменение", "Четверг — 1 изменение"]
    # структура поста: «Коротко» → дни → «Как это читать»
    headings = [block.get("text") for block in blocks if block.get("type") == "heading"]
    assert headings[:2] == ["Коротко", "Что именно поменяли"]
    overview = next(block for block in blocks if block.get("type") == "list")
    # список Bot API принимает только в форме items: [{"blocks": [...]}]
    assert all("blocks" in entry for entry in overview["items"])
    overview_blob = json.dumps(overview, ensure_ascii=False)
    assert "Вторник" in overview_blob and "1 изменение" in overview_blob
    assert "(лек.) Новый" in blob
    assert "203 → 415" in _plain(rm)
    day_blocks = {
        block["summary"]: block.get("blocks") or []
        for block in blocks if block.get("type") == "details"
    }
    wednesday = day_blocks["Среда — 1 изменение"]
    # каждая правка — своя цитата, чтобы отделяться от соседних
    assert wednesday[0]["type"] == "blockquote"
    first_card = _plain(wednesday[0])
    assert "1. Изменили" in first_card
    assert "203 → 415" in first_card
    # дублирующая сводная таблица дня убрана: правка уже видна в цитате
    assert not any(inner.get("type") == "table" for inner in wednesday)
    assert not any(inner.get("type") == "details" for inner in wednesday)
    # убранная пара на новом скрине не появится — предупреждаем текстом
    thursday = day_blocks["Четверг — 1 изменение"]
    removed_note = [json.dumps(inner, ensure_ascii=False) for inner in thursday]
    assert any("1. Убрали" in note for note in removed_note)
    assert any("Убрано: 1 пара" in note and "на скрине ниже её уже нет" in note for note in removed_note)
    assert "портал НовГУ" not in blob
    # знаки +/−/~ больше не нужны: правки подписаны глаголами
    assert "Сводка дня" not in blob


def test_changes_rich_message_hides_empty_sections():
    import json
    diff = {"added": [], "removed": [], "changed": [], "transition": "published"}
    rm = build_changes_rich_message(diff, [], "https://example.test")
    blob = json.dumps(rm, ensure_ascii=False)
    assert "Добавлено" not in blob
    assert "Убрано" not in blob
    assert "Изменено —" not in blob
    assert "Расписание опубликовано" in blob


def test_changes_rich_message_survives_big_diff():
    diff = _sample_diff()
    diff["added"] = diff["added"] * 50
    rm = build_changes_rich_message(diff, [], "https://example.test")
    assert rm["rich_message"]["blocks"]


def test_changes_fallback_text_is_valid_and_bounded():
    diff = _sample_diff()
    diff["added"][0]["subject"] = "<script>x</script>"
    text = changes_fallback_text(diff, "https://example.test")
    assert len(text) <= 4096
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "открыть на портале" in text
    big = _sample_diff()
    big["added"] = big["added"] * 300
    assert len(changes_fallback_text(big, "https://example.test")) <= 4096


def test_dashboard_rich_message_has_week_section():
    import json
    data = _load()
    weeks = [{"week": 1, "half": "top", "start": "01.09.2026", "end": "05.09.2026"}]
    screenshots = [
        {"label": "Чт", "media": "attach://site_screenshot_1"},
    ]
    rm = build_dashboard_rich_message(data["schedule"], weeks, "https://example.test", dt.date(2026, 9, 2), screenshot_media=screenshots, current_date=dt.date(2026, 8, 24))
    blob = json.dumps(rm, ensure_ascii=False)
    assert "Расписание группы 6381" in blob
    assert "Фокус: 02.09.2026" in blob
    assert "Дневник" not in blob  # removed from dashboard
    assert "Полное расписание" in blob
    assert "Неделя 1 (верхняя)" in blob
    assert "Как читать" not in blob  # moved to separate guide post
    assert "портал НовГУ" not in blob  # moved to separate guide post
    assert '"type": "footer"' not in blob
    assert "Среда — 02.09: 0 пар" not in blob  # diary removed
    assert "Скрины · оригинал по дням" not in blob
    assert "Скрин: Четверг" in blob
    assert "Скрин с сайта" not in blob
    assert '"text": "день"' not in blob
    assert '"text": "скрин"' not in blob
    assert '"text": "ниже"' not in blob
    assert "attach://site_screenshot_1" in blob
    assert "Четверг" in blob
    assert "Среда" not in blob  # diary removed; 0-lesson day hidden from week view
    assert "оригинальное" in blob
    assert '"text": "расписание"' in blob
    assert '"type": "photo"' in blob
    assert '"header"' not in blob
    assert "details" in blob and "table" in blob
    # Дни с нулём пар скрыты из полного расписания (и их скрины тоже)
    assert "Вторник" not in blob
    assert "Скрин: Вторник" not in blob
    assert "Скрин: Среда" not in blob


def test_dashboard_screenshot_section_is_the_only_open_section():
    data = _load()
    weeks = [{"week": 1, "half": "top", "start": "01.09.2026", "end": "05.09.2026"}]
    screenshots = [
        {"label": "Чт", "media": "attach://site_screenshot_1"},
    ]
    rm = build_dashboard_rich_message(data["schedule"], weeks, "https://example.test", dt.date(2026, 9, 3), screenshot_media=screenshots, current_date=dt.date(2026, 9, 3))
    details = []

    def walk(blocks):
        for block in blocks:
            if block.get("type") == "details":
                details.append(block)
                walk(block.get("blocks", []))

    walk(rm["rich_message"]["blocks"])
    open_sections = [block.get("summary") for block in details if block.get("is_open") or block.get("open")]
    assert open_sections == ["Скрин: Четверг"]


def test_dashboard_has_no_open_sections_on_sunday():
    data = _load()
    weeks = [{"week": 1, "half": "top", "start": "01.09.2026", "end": "06.09.2026"}]
    screenshots = [{"label": "Вс", "media": "attach://site_screenshot_1"}]
    rm = build_dashboard_rich_message(data["schedule"], weeks, "https://example.test", dt.date(2026, 9, 6), screenshot_media=screenshots, current_date=dt.date(2026, 9, 6))
    details = []

    def walk(blocks):
        for block in blocks:
            if block.get("type") == "details":
                details.append(block)
                walk(block.get("blocks", []))

    walk(rm["rich_message"]["blocks"])
    assert [block.get("summary") for block in details if block.get("is_open") or block.get("open")] == []



def _walk_rich_blocks(blocks):
    for block in blocks:
        yield block
        if block.get("type") == "details":
            yield from _walk_rich_blocks(block.get("blocks") or [])


def test_rich_tables_follow_official_cell_contract():
    data = _load()
    payload = build_rich_message(data["schedule"], data["weeks"], "https://example.test")
    validate_rich_payload(payload)
    tables = [
        block for block in _walk_rich_blocks(payload["rich_message"]["blocks"])
        if block.get("type") == "table"
    ]
    assert tables
    for table in tables:
        for row_index, row in enumerate(table["cells"]):
            for cell in row:
                assert "type" not in cell
                assert cell["align"] in {"left", "center", "right"}
                assert cell["valign"] in {"top", "middle", "bottom"}
                assert cell.get("is_header") is (True if row_index == 0 else None)


def test_local_rich_validator_rejects_old_table_cell_shape():
    payload = {
        "rich_message": {
            "blocks": [{
                "type": "table",
                "cells": [[{
                    "type": "tableCell",
                    "text": "bad",
                    "align": "left",
                    "valign": "middle",
                }]],
            }],
        },
    }
    import pytest
    with pytest.raises(ValueError, match="must not have a type"):
        validate_rich_payload(payload)


def test_large_rich_diff_is_chunked_and_within_documented_limits():
    item = {
        "day": "Среда",
        "time": "11:00 12:30",
        "subject": "Очень длинный предмет " * 80,
        "room": "101",
        "teacher": "Иванов " * 80,
    }
    changed = {
        "day": "Среда",
        "time": "11:00 12:30",
        "subject": "Экономика " * 80,
        "fields": [("примечание", "старое " * 100, "новое " * 100)],
    }
    diff = {
        "added": [item] * 300,
        "removed": [item] * 300,
        "changed": [changed] * 300,
        "transition": None,
    }
    payload = build_changes_rich_message(diff, [], "https://example.test")
    validate_rich_payload(payload)
    quotes = [
        block for block in _walk_rich_blocks(payload["rich_message"]["blocks"])
        if block.get("type") == "blockquote"
    ]
    assert len(quotes) > 1
    assert "не поместились" in __import__("json").dumps(payload, ensure_ascii=False)


def test_schedule_fallback_splits_one_huge_row_without_broken_html():
    schedule = {
        "days": {
            "Понедельник": [{
                "number": 1,
                "subject": "<&>" * 2000,
                "time": "09:00",
                "room": "101",
                "teacher": "Иванов",
                "note": "<&>" * 2000,
            }],
        },
    }
    messages = format_schedule_post(schedule, [], "https://example.test")
    assert all(len(message) <= 4096 for message in messages)
    assert all(message.count("<pre>") == message.count("</pre>") for message in messages)
    assert not any(__import__("re").search(r"&(?:#(?:x[0-9A-Fa-f]*)?|[A-Za-z]*)$", message) for message in messages)
    assert "&lt;" in "".join(messages)


def test_changes_fallback_handles_one_huge_entity_heavy_line_atomically():
    diff = _sample_diff()
    diff["added"][0]["subject"] = "<&>" * 3000
    text = changes_fallback_text(diff, "https://example.test?a=1&b=2")
    assert len(text) <= 4096
    assert text.count("<b>") == text.count("</b>")
    assert text.count("<a ") == text.count("</a>") == 1
    assert not __import__("re").search(r"&(?:#(?:x[0-9A-Fa-f]*)?|[A-Za-z]*)$", text)
    assert "<script>" not in text


def test_dashboard_last_updated_in_title():
    data = _load()
    weeks = [{"week": 1, "half": "top", "start": "01.09.2026", "end": "05.09.2026"}]
    rm = build_dashboard_rich_message(
        data["schedule"], weeks, "https://example.test", dt.date(2026, 9, 2),
        last_updated="27.08.2026 18:45",
    )
    import json
    blob = json.dumps(rm, ensure_ascii=False)
    assert "Последние изменения: 27.08.2026 18:45" in blob


def test_dashboard_no_last_updated_when_empty():
    data = _load()
    weeks = [{"week": 1, "half": "top", "start": "01.09.2026", "end": "05.09.2026"}]
    rm = build_dashboard_rich_message(
        data["schedule"], weeks, "https://example.test", dt.date(2026, 9, 2),
    )
    import json
    blob = json.dumps(rm, ensure_ascii=False)
    assert "Последние изменения" not in blob


def test_dashboard_week_before_diary():
    data = _load()
    weeks = [{"week": 1, "half": "top", "start": "01.09.2026", "end": "05.09.2026"}]
    rm = build_dashboard_rich_message(
        data["schedule"], weeks, "https://example.test", dt.date(2026, 9, 2),
        current_date=dt.date(2026, 8, 31),
    )
    blocks = rm["rich_message"]["blocks"]
    # block 0 = pullquote (title), block 1 = легенда времени, дальше разделы
    assert blocks[0]["type"] == "pullquote"
    week = next(block for block in blocks if block["type"] == "details")
    assert "Полное расписание" in json.dumps(week, ensure_ascii=False)
    assert "Дневник" not in json.dumps(week, ensure_ascii=False)
    # дневник из дашборда убран: его не должно быть ни в одном блоке
    assert all("Дневник" not in json.dumps(block, ensure_ascii=False) for block in blocks)


def _mini_timetable() -> dict:
    """Пн — обычная пара, Вт — четыре академических часа одной строкой."""
    html = (
        "<table>"
        "<tr><th>дата</th><th>время</th><th>под гр.</th><th>предмет</th>"
        "<th>преподаватель</th><th>ауд.</th><th>комм.</th></tr>"
        "<tr><td>Пн</td><td>9:00 10:00</td><td></td><td>(лек.) Матан</td>"
        "<td>Иванов И. И.</td><td>101</td><td></td></tr>"
        "<tr><td>Вт</td><td>14:00 15:00 16:00 17:00</td><td></td><td>(пр.) Проект</td>"
        "<td>Петров П. П.</td><td>202</td><td>Антоново</td></tr>"
        "</table>"
    )
    return parse_all(html)


WEEK_1 = [{"week": 1, "half": "top", "start": "31.08.2026", "end": "05.09.2026"}]


def _tables(node) -> list:
    """Все table-блоки rich-сообщения, в любом порядке вложенности."""
    found: list = []
    if isinstance(node, dict):
        if node.get("type") == "table":
            found.append(node)
        for value in node.values():
            found.extend(_tables(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_tables(value))
    return found


def test_dashboard_shows_real_pair_intervals_instead_of_portal_hour_starts():
    data = _mini_timetable()
    rm = build_dashboard_rich_message(
        data["schedule"], WEEK_1, "https://example.test", dt.date(2026, 8, 31),
    )
    blob = json.dumps(rm, ensure_ascii=False)
    validate_rich_payload(rm)
    tables = json.dumps(_tables(rm), ensure_ascii=False)
    # Портальная запись «начала часов» в таблицы не просачивается
    # (в легенде она остаётся как наглядный пример).
    assert "9:00 10:00" not in tables
    assert "14:00 15:00 16:00 17:00" not in tables
    # Обычная пара — интервал с 15-минутным перерывом внутри.
    assert "09:00–10:45" in blob
    # Четыре академических часа — две пары, а не одно занятие до 17:45.
    assert "14:00–15:45" in blob and "16:00–17:45" in blob
    # Легенда объясняет сетку и вариант без перерыва.
    assert "45 + 15 перерыв + 45" in blob
    assert "09:00–10:30" in blob


def test_dashboard_keeps_odd_hour_visible():
    data = parse_all(
        "<table>"
        "<tr><th>дата</th><th>время</th><th>под гр.</th><th>предмет</th>"
        "<th>преподаватель</th><th>ауд.</th><th>комм.</th></tr>"
        "<tr><td>Пн</td><td>16:00 17:00 18:00</td><td></td><td>(лек.) Физра</td>"
        "<td>—</td><td>Спортзал</td><td></td></tr>"
        "</table>"
    )
    rm = build_dashboard_rich_message(
        data["schedule"], WEEK_1, "https://example.test", dt.date(2026, 8, 31),
        current_date=dt.date(2026, 8, 31),
    )
    blob = json.dumps(rm, ensure_ascii=False)
    assert "16:00–17:45" in blob
    assert "18:00–18:45 (1 ак. ч.)" in blob


def test_semester_post_and_html_screenshot_use_the_same_intervals():
    data = _mini_timetable()
    rm = build_rich_message(data["schedule"], WEEK_1, "https://example.test", dt.date(2026, 8, 31))
    blob = json.dumps(rm, ensure_ascii=False)
    assert "09:00–10:45" in blob and "16:00–17:45" in blob
    html = render_schedule_html(data["schedule"], WEEK_1, "https://example.test", dt.date(2026, 8, 31))
    assert "09:00–10:45" in html
    assert "<br>" in html  # вторая пара того же дня — отдельной строкой
    assert "9:00 10:00" not in html


def _all_day_media():
    return [
        {"label": label, "media": f"attach://changes_screenshot_{index}"}
        for index, label in enumerate(["Пн", "Вт", "Ср", "Чт", "Пт", "Сб"], 1)
    ]


def _photos_by_day(blocks):
    """Фото внутри дневных секций: [(summary секции, media, caption)]."""
    out = []
    for block in blocks:
        if block.get("type") != "details" or not block.get("summary"):
            continue
        for inner in block.get("blocks") or []:
            if inner.get("type") == "photo":
                out.append((block["summary"], inner["photo"]["media"], inner.get("caption", {}).get("text")))
    return out


def test_changes_post_attaches_screens_only_for_changed_days():
    rm = build_changes_rich_message(
        _sample_diff(), [], "https://example.test",
        now=dt.datetime(2026, 9, 2, 12, 0),
        screenshot_media=_all_day_media(),
    )
    blocks = rm["rich_message"]["blocks"]
    shots = _photos_by_day(blocks)
    # В диффе Вторник, Среда и Четверг — значит и скринов ровно три, по порядку недели.
    assert [summary for summary, _, _ in shots] == [
        "Вторник — 1 изменение", "Среда — 1 изменение", "Четверг — 1 изменение",
    ]
    assert [media for _, media, _ in shots] == [
        "attach://changes_screenshot_2", "attach://changes_screenshot_3", "attach://changes_screenshot_4",
    ]
    assert all("Новое расписание" in json.dumps(caption, ensure_ascii=False) for _, _, caption in shots)
    blob = json.dumps(rm, ensure_ascii=False)
    # Понедельник, Пятница и Суббота не менялись — их скрины в пост не попали.
    for unused in ("changes_screenshot_1", "changes_screenshot_5", "changes_screenshot_6"):
        assert unused not in blob
    validate_rich_payload({"chat_id": -1001, **rm})


def test_changes_post_reuses_one_screen_for_combined_day_label():
    diff = {
        "added": [{"day": "Понедельник", "time": "09:00 10:00", "subject": "Новая пара",
                   "room": "101", "teacher": "Иванов"}],
        "removed": [{"day": "Вторник", "time": "11:00 12:00", "subject": "Старая пара",
                     "room": "202", "teacher": "Петров"}],
        "changed": [],
        "transition": None,
    }
    rm = build_changes_rich_message(
        diff, [], "https://example.test", now=dt.datetime(2026, 9, 2, 12, 0),
        screenshot_media=[{"label": "Пн + Вт", "media": "attach://changes_screenshot_1"}],
    )
    shots = _photos_by_day(rm["rich_message"]["blocks"])
    # одна картинка на два дня: вставляется в первый день, второму — пометка
    assert [summary for summary, _, _ in shots] == ["Понедельник — 1 изменение"]
    blob = json.dumps(rm, ensure_ascii=False)
    assert "Скрин этого дня общий с Пн" in blob
    assert blob.count("attach://changes_screenshot_1") == 1
    validate_rich_payload({"chat_id": -1001, **rm})


def test_changes_post_without_screens_stays_text_only():
    rm = build_changes_rich_message(_sample_diff(), [], "https://example.test",
                                    now=dt.datetime(2026, 9, 2, 12, 0))
    assert _photos_by_day(rm["rich_message"]["blocks"]) == []
    assert "photo" not in json.dumps(rm, ensure_ascii=False)


def test_pick_day_screens_understands_combined_labels():
    from format import pick_day_screens
    items = [
        {"label": "Пн + Вт", "path": "/tmp/a.png"},
        {"label": "Ср", "path": "/tmp/b.png"},
        {"label": "Пт", "path": "/tmp/c.png"},
    ]
    picked = pick_day_screens(_sample_diff(), items)
    # «Пн + Вт» берём из-за Вторника, Пятница не менялась — мимо.
    assert [item["label"] for item in picked] == ["Пн + Вт", "Ср"]
    assert pick_day_screens({"added": [], "removed": [], "changed": []}, items) == []
    assert pick_day_screens(_sample_diff(), []) == []


def _rich_nodes(node):
    """Обход всех rich-элементов (dict) в любую глубину."""
    found: list = []
    if isinstance(node, dict):
        found.append(node)
        for value in node.values():
            found.extend(_rich_nodes(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_rich_nodes(value))
    return found


def test_guide_decodes_rooms_and_igum_suffix():
    from format import _guide_decode_room

    assert _guide_decode_room("402ИГУМ") == {
        "floor": "4",
        "num": "02",
        "place": "корпус Гуманитарного института (ИГУМ), новый корпус — кампус Антоново",
    }
    assert _guide_decode_room("1318")["place"] == "старый корпус — кампус Антоново"
    assert _guide_decode_room("214")["place"] == "новый корпус — кампус Антоново"
    assert _guide_decode_room("1313ИГУМ")["place"] == (
        "корпус Гуманитарного института (ИГУМ), старый корпус — кампус Антоново"
    )
    assert _guide_decode_room("310 ИГУМ")["place"].endswith("новый корпус — кампус Антоново")
    # портал пишет неряшливо: опечатка «1321ИГ» и склейка «1313ИГУМ313» тоже читаются
    assert _guide_decode_room("1321ИГ")["place"].startswith("корпус Гуманитарного института")
    assert _guide_decode_room("1313ИГУМ313") == {
        "floor": "3",
        "num": "13",
        "place": "корпус Гуманитарного института (ИГУМ), старый корпус — кампус Антоново",
    }
    # чужая метка не выдаётся за корпус Гуманитарного института
    assert "пометка ИЭ" in _guide_decode_room("218ИЭ")["place"]
    # у этих подразделений адрес официально другой — цифры номера тут не решают
    assert _guide_decode_room("310 ИНПО")["place"] == (
        "пометка ИНПО: ул. Чудинцева, 6 — не Антоново, сверься с примечаниями"
    )
    assert "Псковская, 3" in _guide_decode_room("103ПИ")["place"]
    # литера, вариант в скобках и поточная аудитория тоже читаются
    assert _guide_decode_room("323а") == {
        "floor": "3", "num": "23а", "place": "новый корпус — кампус Антоново",
    }
    assert _guide_decode_room("1100(1)") == {
        "floor": "1", "num": "00", "place": "старый корпус — кампус Антоново",
    }
    assert _guide_decode_room("3поточная") == {
        "floor": "—", "num": "3 поточная", "place": "корпус 3 — Б. Санкт-Петербургская, 41",
    }
    assert _guide_decode_room("3207")["place"] == "корпус 3 — Б. Санкт-Петербургская, 41"
    assert _guide_decode_room("Спортзал") is None
    assert _guide_decode_room("209/226") is None


def test_freshman_guide_serves_economics_institute_with_real_links():
    from format import _freshman_guide

    data = _mini_timetable()
    guide = _freshman_guide(data["schedule"], "https://example.test")
    blob = json.dumps(guide, ensure_ascii=False)
    validate_rich_payload({"chat_id": -1001, "rich_message": {"blocks": [guide]}})

    # психологическую помощь из гайда убрали, вместо неё — транспорт
    assert "Психологическая помощь" not in blob
    assert "Автобусы: Антоново → западный район" in blob

    # ООД и студсовет — свои, Института экономики
    assert "ООД Института экономики" in blob
    assert "Tatyana.Odinokova@novsu.ru" in blob
    assert "Студсовет Института экономики" in blob

    # ИГУМ объяснён: институт + метка здания, без выдуманных «д. 1 / д. 2»
    assert "ИГУМ = Институт гуманитарный" in blob
    assert "218ИГУМ" in blob
    # «218ИГУМ и 218ИЭ — разные кабинеты» реестром не подтверждено, вместо этого
    # честный пример того, что суффикс меняет адрес: 310 ИГУМ и 310 ИНПО.
    assert "310 ИНПО" in blob and "Чудинцева" in blob
    assert "Антоново, д. 1" not in blob and "Антоново, д. 2" not in blob
    assert "территория Антоново, 1" not in blob and "Псковская, 3" in blob

    nodes = _rich_nodes(guide)
    urls = {n.get("url") for n in nodes if n.get("type") == "url"}
    for expected in (
        "https://vk.com/studsovet.ie_novsu",
        "https://vk.com/studsovet.novsu",
        "https://vk.com/novsu.event",
        "https://vk.com/sport_novsu",
        "https://vk.com/profkomnovgu",
        "https://vk.com/wall-34755757_44442",
        "https://vk.com/wall-34755757_44440",
        "https://www.novsu.ru/study/campus_navigation/navigation_in_buildings/",
        "https://www.novsu.ru/study/ood/",
        "https://www.novsu.ru/study/freshman/adapters/",
    ):
        assert expected in urls
    # адаптеры «Туризма» — в посте 44442 (ИЭ), а не в 44440 (ИГУМ/ИЮР/ПИ)
    assert "Морковин" in blob and "Мехоношина" in blob
    # мусорных ссылок и «ссылок кодом» быть не должно
    assert not any(str(u).rstrip("/").endswith("/js") for u in urls)
    assert "https://vk.com/wall-34755757_34267" not in urls
    codes = [n.get("text") for n in nodes if n.get("type") == "code"]
    assert not any("vk." in str(text) for text in codes)


def test_changes_post_promises_highlight_only_when_screens_are_marked():
    diff = {
        "added": [{"day": "Вторник", "time": "09:00 10:30", "subject": "(лек.) Новый",
                   "room": "101", "teacher": "Иванов"}],
        "removed": [], "changed": [], "transition": None,
    }
    now = dt.datetime(2026, 9, 2, 12, 0)
    marked = build_changes_rich_message(
        diff, [], "https://example.test", now=now,
        screenshot_media=[{"label": "Вт", "media": "attach://changes_screenshot_1", "marked": 1}],
    )
    blob = json.dumps(marked, ensure_ascii=False)
    # пояснение про цвета теперь один раз внизу, а не в каждом дне
    assert "Зелёным помечены новые пары" in blob
    assert "оранжевым — поправленные" in blob
    assert "зелёным новые пары, оранжевым правки" in blob
    assert "Как это читать" in blob

    clean = build_changes_rich_message(
        diff, [], "https://example.test", now=now,
        screenshot_media=[{"label": "Вт", "media": "attach://changes_screenshot_1", "marked": 0}],
    )
    clean_blob = json.dumps(clean, ensure_ascii=False)
    # чистый скрин (chromium упал) — никаких обещаний про подсветку
    assert "зелёным" not in clean_blob
    assert "оранжевым" not in clean_blob
    assert "attach://changes_screenshot_1" in clean_blob

    without_media = json.dumps(
        build_changes_rich_message(diff, [], "https://example.test", now=now),
        ensure_ascii=False,
    )
    assert "зелёным" not in without_media
    assert "оранжевым" not in without_media


def test_moved_pair_is_one_card_not_two_rows():
    """Перенос пары — одна правка «было → стало», а не «убрали» + «добавили»."""
    lesson = {
        "day": "Четверг", "subject": "(пр.) География туризма", "subgroup": "",
        "teacher": "Ефимов Олег Николаевич", "room": "418", "location": "Антоново",
    }
    diff = {
        "added": [{**lesson, "time": "17:00 18:00"}],
        "removed": [{**lesson, "time": "11:00 12:00"}],
        "changed": [], "transition": None,
    }
    rm = build_changes_rich_message(
        diff, [], "https://example.test", now=dt.datetime(2026, 9, 3, 12, 0),
    )
    blocks = rm["rich_message"]["blocks"]
    day = next(b for b in blocks if b.get("type") == "details")
    # обе записи склеены в один перенос, поэтому и заголовок про одно изменение
    assert day["summary"] == "Четверг — 1 изменение"
    card = _plain(day["blocks"][0])
    assert "1. Перенесли" in card
    assert "(пр.) География туризма" in card
    assert "время: 11:00–12:45 → 17:00–18:45" in card
    blob = json.dumps(rm, ensure_ascii=False)
    # старую пару не выдаём за отдельное «убрали»
    assert "Убрали" not in blob
    # пояснение про перенос — один раз внизу, не в каждом дне
    assert "Почему «перенесли», а не «убрали и добавили»" in blob
    assert blob.count("Портал не пишет «перенос»") == 1


def test_real_removal_stays_removal():
    """Если пара просто исчезла, склеивать её не с чем."""
    diff = {
        "added": [],
        "removed": [{"day": "Пятница", "time": "09:00 10:00", "subject": "(лек.) Экономика",
                     "teacher": "Иванов И. И.", "room": "1306", "location": ""}],
        "changed": [], "transition": None,
    }
    blob = json.dumps(
        build_changes_rich_message(diff, [], "https://example.test",
                                   now=dt.datetime(2026, 9, 3, 12, 0)),
        ensure_ascii=False,
    )
    assert "1. Убрали" in blob
    assert "(лек.) Экономика" in blob
    assert "этой пары в новом расписании нет" in blob
    assert "Перенесли" not in blob


def test_rename_on_both_weeks_is_one_card():
    """Переименование пришло в верх и низ недели — одна карточка, а не две."""
    base = {
        "day": "Понедельник", "time": "09:00 10:00", "subgroup": "",
        "teacher": "Барышева Ангелина Алексеевна",
        "fields": [["предмет",
                    "(пр.) Иностранные языки в сфере профессиональной коммуникации",
                    "(пр.) Иностранный язык"]],
    }
    diff = {
        "added": [], "removed": [], "transition": None,
        "changed": [
            {**base,
             "subject": "(пр.) Иностранный язык\nнемец. яз. по верхней неделе, Антоново",
             "note": "немец. яз. по верхней неделе, Антоново",
             "room": "1318", "location": "Антоново", "delivery_mode": "in_person"},
            {**base,
             "subject": "(пр.) Иностранный язык\nнемец. яз. по нижней неделе с использованием ДОТ",
             "note": "немец. яз. по нижней неделе с использованием ДОТ",
             "room": "—", "location": None, "delivery_mode": "remote_or_hybrid"},
        ],
    }
    rm = build_changes_rich_message(diff, [], "https://example.test",
                                    now=dt.datetime(2026, 9, 4, 11, 33))
    blocks = rm["rich_message"]["blocks"]
    day = next(b for b in blocks if b.get("type") == "details")
    assert day["summary"] == "Понедельник — 1 изменение"
    cards = [b for b in day["blocks"] if b.get("type") == "blockquote"]
    assert len(cards) == 1
    card = _plain(cards[0])
    # глагол по смыслу правки: название сменилось, остальное на месте
    assert "1. Переименовали: (пр.) Иностранный язык" in card
    assert "обе недели" in card
    assert "было: (пр.) Иностранные языки в сфере профессиональной коммуникации" in card
    assert "верхняя неделя — ауд. 1318, Антоново" in card
    # пустая аудитория портала — прочерк, поэтому место названо форматом
    assert "нижняя неделя — дистанционно" in card
    assert "немецкий язык" in card
    assert "немец. яз" not in card
    assert "кто ведёт: Барышева Ангелина Алексеевна" in card
    # шапка и «Коротко» считают одну правку, а не четыре записи портала
    assert "1 изменение" in _plain(blocks[0])
    overview = _plain(next(b for b in blocks if b.get("type") == "list"))
    assert "переименовали" in overview
    assert "изменили" not in overview


def test_changes_post_names_only_the_colors_actually_marked():
    """Подпись и пояснение обещают только те цвета, что легли на скрин."""
    diff = {
        "added": [], "removed": [], "transition": None,
        "changed": [{"day": "Понедельник", "time": "09:00 10:00", "subject": "(пр.) Иностранный язык",
                     "room": "1318", "teacher": "Барышева А. А.", "location": "Антоново",
                     "note": "", "delivery_mode": "in_person",
                     "fields": [["предмет", "старое", "новое"]]}],
    }
    now = dt.datetime(2026, 9, 4, 11, 33)
    changed_only = json.dumps(build_changes_rich_message(
        diff, [], "https://example.test", now=now,
        screenshot_media=[{"label": "Пн", "media": "attach://changes_screenshot_1",
                           "marked": 2, "kinds": ["changed"]}],
    ), ensure_ascii=False)
    assert "оранжевым правки" in changed_only
    assert "Оранжевым помечены поправленные пары" in changed_only
    assert "зелёным" not in changed_only and "Зелёным" not in changed_only

    clean = json.dumps(build_changes_rich_message(
        diff, [], "https://example.test", now=now,
        screenshot_media=[{"label": "Пн", "media": "attach://changes_screenshot_1",
                           "marked": 1, "highlighted": False}],
    ), ensure_ascii=False)
    # чистый скрин из общего кеша: красок на нём нет, обещать их нельзя
    assert "зелёным" not in clean and "оранжевым" not in clean
    assert "attach://changes_screenshot_1" in clean


def _week_tail_timetable() -> dict:
    """Пары на Чт, Пт и Сб учебной недели 01.09—05.09.2026.

    Понедельник 01.09.2026 в HOLIDAYS (День знаний), поэтому для проверки
    исчезающих прошедших дней берём хвост недели.
    """
    html = (
        "<table>"
        "<tr><th>дата</th><th>время</th><th>под гр.</th><th>предмет</th>"
        "<th>преподаватель</th><th>ауд.</th><th>комм.</th></tr>"
        "<tr><td>Чт</td><td>11:00 12:00</td><td></td><td>(пр.) Философия</td>"
        "<td>Петров П. П.</td><td>202</td><td></td></tr>"
        "<tr><td>Пт</td><td>14:00 15:00</td><td></td><td>(лек.) История</td>"
        "<td>Сидоров С. С.</td><td>303</td><td></td></tr>"
        "<tr><td>Сб</td><td>9:00 10:00</td><td></td><td>(пр.) Английский</td>"
        "<td>Кузнецова К. К.</td><td>404</td><td></td></tr>"
        "</table>"
    )
    return parse_all(html)


WEEK_TAIL = [{"week": 1, "half": "top", "start": "01.09.2026", "end": "05.09.2026"}]


def _dashboard_day_summaries(rm) -> list[str]:
    def walk(blocks):
        for block in blocks:
            yield block
            if block.get("type") == "details":
                yield from walk(block.get("blocks") or [])

    return [
        str(block.get("summary"))
        for block in walk(rm["rich_message"]["blocks"])
        if block.get("type") == "details" and isinstance(block.get("summary"), str)
    ]


def test_dashboard_shows_whole_week_before_it_starts():
    data = _week_tail_timetable()
    rm = build_dashboard_rich_message(
        data["schedule"], WEEK_TAIL, "https://example.test", dt.date(2026, 9, 3),
        current_date=dt.date(2026, 9, 1),
    )
    days = _dashboard_day_summaries(rm)
    assert [item for item in days if "Скрин" not in item] == [
        "Четверг — 03.09: 1 пара", "Пятница — 04.09: 1 пара", "Суббота — 05.09: 1 пара",
    ]
    assert "Полное расписание" in json.dumps(rm, ensure_ascii=False)


def test_dashboard_drops_past_days_after_midnight_msk():
    """В пятницу в закрепе остаются только пт и сб — прошедшие дни уезжают в 00:00 Мск."""
    data = _week_tail_timetable()
    rm = build_dashboard_rich_message(
        data["schedule"], WEEK_TAIL, "https://example.test", dt.date(2026, 9, 4),
        screenshot_media=[{"label": "Пт", "media": "attach://site_screenshot_1"}],
        current_date=dt.date(2026, 9, 4),
    )
    days = _dashboard_day_summaries(rm)
    assert [item for item in days if "Скрин" not in item] == [
        "Пятница — 04.09: 1 пара", "Суббота — 05.09: 1 пара",
    ]
    assert "Четверг" not in json.dumps(rm, ensure_ascii=False)
    blob = json.dumps(rm, ensure_ascii=False)
    # Подпись раздела не обещает полное расписание, если часть дней уже прошла.
    assert "Расписание до конца недели" in blob
    assert "Полное расписание" not in blob


def test_dashboard_says_week_is_over_when_every_day_is_past():
    data = _week_tail_timetable()
    rm = build_dashboard_rich_message(
        data["schedule"], WEEK_TAIL, "https://example.test", dt.date(2026, 9, 5),
        current_date=dt.date(2026, 9, 6),
    )
    blob = json.dumps(rm, ensure_ascii=False)
    assert "уже прошли" in blob
    assert _dashboard_day_summaries(rm) == []


def _timetable_with_dot_and_room() -> dict:
    """Дистанционная (ДОТ) пара и обычная с аудиторией на Ср/Чт недели 01.09—05.09.2026."""
    html = (
        "<table>"
        "<tr><th>дата</th><th>время</th><th>под гр.</th><th>предмет</th>"
        "<th>преподаватель</th><th>ауд.</th><th>комм.</th></tr>"
        "<tr><td>Ср</td><td>19:00 20:00</td><td></td><td>(лек.) История России</td>"
        "<td>Сидоров С. С.</td><td>—</td><td>с использованием ДОТ</td></tr>"
        "<tr><td>Чт</td><td>11:00 12:00</td><td></td><td>(пр.) Философия</td>"
        "<td>Петров П. П.</td><td>202</td><td></td></tr>"
        "</table>"
    )
    return parse_all(html)


def test_dashboard_dot_pair_shows_place_and_drops_dot_note():
    data = _timetable_with_dot_and_room()
    rm = build_dashboard_rich_message(
        data["schedule"], WEEK_TAIL, "https://example.test", dt.date(2026, 9, 2),
        current_date=dt.date(2026, 9, 1),
    )
    blob = json.dumps(rm, ensure_ascii=False)
    # Предмет без [ДОТ], формат передан местом, примечание про ДОТ не дублируется.
    assert "[ДОТ]" not in blob
    assert "дистанционно" in blob
    assert "с использованием ДОТ" not in blob
    assert "(лек.) История России" in blob
    assert "(пр.) Философия" in blob


def test_dashboard_today_disappears_after_last_pair():
    """Вариант 2: после конца последней пары сегодняшний день уходит из закрепа."""
    data = _week_tail_timetable()
    friday = dt.date(2026, 9, 4)
    before = build_dashboard_rich_message(
        data["schedule"], WEEK_TAIL, "https://example.test", friday,
        current_date=friday, now=dt.datetime(2026, 9, 4, 14, 30),
    )
    assert [s for s in _dashboard_day_summaries(before) if "Скрин" not in s] == [
        "Пятница — 04.09: 1 пара", "Суббота — 05.09: 1 пара",
    ]
    after = build_dashboard_rich_message(
        data["schedule"], WEEK_TAIL, "https://example.test", friday,
        current_date=friday, now=dt.datetime(2026, 9, 4, 15, 46),
    )
    assert [s for s in _dashboard_day_summaries(after) if "Скрин" not in s] == [
        "Суббота — 05.09: 1 пара",
    ]
