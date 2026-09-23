import json
from pathlib import Path
import datetime as dt

import pytest

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
            for key in ("rich_message", "text", "summary", "caption", "blocks", "items", "cells")
            if node.get(key) is not None
        )
    return ""


def _main_rows(rm) -> list[list[dict]]:
    """Строки основных таблиц дней (без шапки) как списки ячеек."""
    rows = []
    for day in rm["rich_message"]["blocks"]:
        if day.get("type") != "details":
            continue
        for inner in day["blocks"]:
            if inner.get("type") == "table":
                rows.extend(row for row in inner["cells"] if not any(cell.get("is_header") for cell in row))
    return rows


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
    rm = build_changes_rich_message(_sample_diff(), [], "https://example.test",
                                    now=dt.datetime(2026, 9, 2, 12, 0))
    validate_rich_payload(rm)
    assert "rich_message" in rm
    blocks = rm["rich_message"]["blocks"]
    # Каждый день — раскрывающийся раздел, сводка называет конкретную правку.
    days = [block for block in blocks if block.get("type") == "details"]
    assert [day["summary"] for day in days] == [
        "Вторник (добавили пару)", "Среда (сменили аудиторию)", "Четверг (убрали пару)",
    ]
    # Внутри дня — только таблица правок.
    assert all([inner["type"] for inner in day["blocks"]] == ["table"] for day in days)
    rows = _main_rows(rm)
    assert [_plain(row[0]) for row in rows] == ["+\n09:00–09:45 · 10:30–11:15", "~\n11:00–11:45 · 12:30–13:15", "−\n15:00–16:45"]
    where = rows[1][2]
    assert _plain(where) == "203 → 415"
    assert {"type": "strikethrough", "text": "203"} in where["text"]
    assert {"type": "bold", "text": "415"} in where["text"]
    # Убранная пара: зачёркнуты время и место.
    assert rows[2][0]["text"][-1] == {"type": "strikethrough", "text": "15:00–16:45"}
    assert rows[2][2]["text"] == [{"type": "strikethrough", "text": "ауд. 202"}]
    assert "В новом расписании этого занятия нет." not in _plain(rm)
    # Преподаватели — в колонке «занятие».
    assert _plain(rows[0][1]) == "Новый\nлек.\nИванов" and _plain(rows[2][1]) == "Старый\nпр.\nПетров"
    assert not any(node.get("type") in {
        "pullquote", "blockquote", "expandable_blockquote", "list",
    } for node in _rich_nodes(rm))
    for obsolete in ("Коротко", "Что именно поменяли", "Как это читать",
                     "Сводка дня", "Пояснения", "1. ", "Убрано:", "на скрине ниже", "портал НовГУ",
                     "Добавили пару", "Убрали пару", "Аудитория:"):
        assert obsolete not in _plain(rm)
    assert not any(b.get("type") == "footer" for b in blocks)
    assert "Группа" not in _plain(blocks)
    assert "3 изменения" not in _plain(blocks)


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
    assert "открыть на портале" not in text
    assert [line for line in text.splitlines() if line.startswith("<b>") and "(" in line] == [
        "<b>Вторник (добавили пару)</b>",
        "<b>Среда (сменили аудиторию)</b>",
        "<b>Четверг (убрали пару)</b>",
    ]
    assert "<s>203</s> → <b>415</b>" in text
    assert "<s>15:00–16:45</s>" in text and "<s>ауд. 202</s>" in text
    assert "В новом расписании этого занятия нет." not in text
    # Строка таблицы в фоллбеке: знак, время, занятие и место через « · ».
    assert "~ 11:00–11:45 · 12:30–13:15 · <b>Экономика</b> · <s>203</s> → <b>415</b>" in text
    big = _sample_diff()
    big["added"] = big["added"] * 300
    assert len(changes_fallback_text(big, "https://example.test")) <= 4096


def test_changes_fallback_text_stamps_detection_time_when_given():
    text = changes_fallback_text(
        _sample_diff(), "https://example.test", now=dt.datetime(2026, 9, 2, 12, 0),
    )
    assert "Поменяли расписание в 12:00" in text
    # Без now штампа времени нет — поведение обратно совместимо.
    plain = changes_fallback_text(_sample_diff(), "https://example.test")
    assert "Поменяли расписание в " not in plain


def test_dashboard_rich_message_has_week_section():
    import json
    data = _load()
    weeks = [{"week": 1, "half": "top", "start": "01.09.2026", "end": "05.09.2026"}]
    screenshots = [
        {"label": "Чт", "media": "attach://site_screenshot_1"},
    ]
    rm = build_dashboard_rich_message(data["schedule"], weeks, "https://example.test", dt.date(2026, 9, 2), screenshot_media=screenshots, current_date=dt.date(2026, 8, 24))
    blob = json.dumps(rm, ensure_ascii=False)
    assert "Расписание · группа 6381" in blob
    assert "Фокус:" not in blob
    assert "Дневник" not in blob  # removed from dashboard
    assert "На неделе:" in blob
    assert "НЕДЕЛЯ 1 · ВЕРХНЯЯ" in blob
    assert "1–5 сентября · нечётная учебная неделя" in blob
    assert "очно" not in blob.casefold()
    assert "Пн · 07.09" not in blob  # fixture contains only Thursday lessons
    assert "Как читать недели" not in blob  # guide stays out of the dashboard
    assert "Как читать время" not in blob
    assert "портал НовГУ" not in blob  # moved to separate guide post
    assert '"type": "footer"' not in blob
    assert "Среда — 02.09: 0 пар" not in blob  # diary removed
    assert "Скрины · оригинал по дням" not in blob
    assert "Скрин:" not in blob
    assert "Скрин с сайта" not in blob
    assert '"text": "день"' not in blob
    assert '"text": "скрин"' not in blob
    assert '"text": "ниже"' not in blob
    assert "attach://site_screenshot_1" in blob
    assert "Четверг" in blob
    assert "Среда" not in blob  # diary removed; 0-lesson day hidden from week view
    assert "полный оригинал" in blob
    assert "Скрин с портала · 03.09" in blob
    # В этой фикстуре на четверг нет прошедших разовых пар, значит и фразы о скрытых нет.
    assert "прошедшие разовые занятия скрыты" not in blob
    assert "полный оригинал" in blob
    assert '"text": "полный оригинал"' in blob
    assert '"type": "photo"' in blob
    assert '"header"' not in blob
    assert "details" in blob and "table" in blob
    # Дни с нулём пар скрыты из полного расписания (и их скрины тоже)
    assert "Вторник" not in blob
    assert "Скрин: Вторник" not in blob
    assert "Скрин: Среда" not in blob


def test_dashboard_day_and_screenshot_sections_are_closed():
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
    assert open_sections == []


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
    open_sections = [block.get("summary") for block in details if block.get("is_open") or block.get("open")]
    assert open_sections == []



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
    blocks = payload["rich_message"]["blocks"]
    days = [block for block in blocks if block.get("type") == "details"]
    assert len(days) == 1 and days[0]["summary"].startswith("Среда · ")
    assert "×" not in days[0]["summary"]
    for action in ("изменили условия", "добавили пары", "убрали пары"):
        assert days[0]["summary"].count(action) == 1
    assert not any(str(b.get("summary", "")).startswith("Подробности") for b in days[0]["blocks"])
    main = len(_main_rows(payload))
    assert 3 <= main < 900
    text = _plain(payload)
    omitted = 900 - 3 * min(300, 120)  # raw_total - kind-limited entries
    assert f"ещё {omitted} не поместились" in text  # общий лимит поста режет записи
    assert text.count("старое ") == 0  # примечания больше не выводятся
    assert not any(block.get("type") in {"blockquote", "expandable_blockquote"}
                   for block in _walk_rich_blocks(blocks))
    assert "не поместились" in text


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
    assert text.count("<a ") == text.count("</a>") == 0
    assert not __import__("re").search(r"&(?:#(?:x[0-9A-Fa-f]*)?|[A-Za-z]*)$", text)
    assert "<script>" not in text


@pytest.mark.parametrize("schedule_changed", ["", "22.09.2026 10:06"])
def test_dashboard_last_updated_in_title(schedule_changed):
    data = _load()
    weeks = [{"week": 1, "half": "top", "start": "01.09.2026", "end": "05.09.2026"}]
    rm = build_dashboard_rich_message(
        data["schedule"], weeks, "https://example.test", dt.date(2026, 9, 2),
        last_updated="27.08.2026 18:45",
        schedule_changed=schedule_changed,
    )
    import json
    blob = json.dumps(rm, ensure_ascii=False)
    expected = schedule_changed or "27.08.2026 18:45"
    assert f"Расписание обновлено: {expected} МСК" in blob
    assert blob.count(" МСК") == 1
    if schedule_changed:
        assert "27.08.2026 18:45" not in blob


def test_dashboard_no_last_updated_when_empty():
    data = _load()
    weeks = [{"week": 1, "half": "top", "start": "01.09.2026", "end": "05.09.2026"}]
    rm = build_dashboard_rich_message(
        data["schedule"], weeks, "https://example.test", dt.date(2026, 9, 2),
    )
    import json
    blob = json.dumps(rm, ensure_ascii=False)
    assert "Последние изменения" not in blob
    assert "Расписание обновлено:" not in blob


def test_dashboard_week_before_diary():
    data = _load()
    weeks = [{"week": 1, "half": "top", "start": "01.09.2026", "end": "05.09.2026"}]
    rm = build_dashboard_rich_message(
        data["schedule"], weeks, "https://example.test", dt.date(2026, 9, 2),
        current_date=dt.date(2026, 8, 31),
    )
    blocks = rm["rich_message"]["blocks"]
    assert blocks[0]["type"] == "pullquote"
    assert blocks[1]["type"] == "paragraph"
    assert blocks[2]["type"] == "details"
    assert not blocks[2].get("is_open")
    assert not any(block["type"] == "expandable_blockquote" for block in blocks)
    # дневник из дашборда убран: его не должно быть ни в одном блоке
    assert all("Дневник" not in json.dumps(block, ensure_ascii=False) for block in blocks)


def test_dashboard_lower_week_has_day_tables_and_arithmetic_summary():
    data = _mini_timetable()
    lower = [{"week": 2, "half": "bottom", "start": "07.09.2026", "end": "12.09.2026"}]
    rm = build_dashboard_rich_message(
        data["schedule"], lower, "https://example.test", dt.date(2026, 9, 7),
        current_date=dt.date(2026, 9, 5),
    )
    validate_rich_payload(rm)
    blob = json.dumps(rm, ensure_ascii=False)
    assert "НЕДЕЛЯ 2 · НИЖНЯЯ" in blob
    assert "7–12 сентября · чётная учебная неделя" in blob
    assert "На неделе:" in blob
    assert "2 пары" in blob and "очно" not in blob.casefold()
    assert "ПОНЕДЕЛЬНИК · 07.09" in blob
    assert "ВТОРНИК · 08.09" in blob
    assert "Матан" in blob and "Проект" in blob
    assert len(_tables(rm)) == 2
    assert all(block.get("type") == "details" for block in rm["rich_message"]["blocks"][2:])


def test_dashboard_inlines_common_location_and_simple_condition_without_notes_section():
    data = parse_all(
        "<table>"
        "<tr><th>дата</th><th>время</th><th>под гр.</th><th>предмет</th>"
        "<th>преподаватель</th><th>ауд.</th><th>комм.</th></tr>"
        "<tr><td>Пн</td><td>9:00 10:00</td><td></td><td>(лек.) Проект с 07.09.</td>"
        "<td>Иванов И. И.</td><td>303</td><td>Антоново</td></tr>"
        "</table>"
    )
    week = [{"week": 2, "half": "bottom", "start": "07.09.2026", "end": "12.09.2026"}]
    rm = build_dashboard_rich_message(
        data["schedule"], week, "https://example.test", dt.date(2026, 9, 7),
        current_date=dt.date(2026, 9, 7),
    )
    blob = json.dumps(rm, ensure_ascii=False)
    assert "Проект" in blob and "с 07.09" in blob
    assert '"text": "303"' in blob and "Антоново" in blob
    assert "ОЧНО" not in blob
    assert "Важно" not in blob


def test_dashboard_keeps_date_lists_and_unknown_addresses_in_important_section():
    data = parse_all(
        "<table>"
        "<tr><th>дата</th><th>время</th><th>под гр.</th><th>предмет</th>"
        "<th>преподаватель</th><th>ауд.</th><th>комм.</th></tr>"
        "<tr><td>Сб</td><td>11:00 12:00</td><td></td><td>(пр.) Практика</td>"
        "<td>Иванов И. И.</td><td>216</td><td>ул. Псковская, 3; 12.09, 19.09</td></tr>"
        "</table>"
    )
    week = [{"week": 2, "half": "bottom", "start": "07.09.2026", "end": "12.09.2026"}]
    rm = build_dashboard_rich_message(
        data["schedule"], week, "https://example.test", dt.date(2026, 9, 12),
        current_date=dt.date(2026, 9, 7),
    )
    blob = json.dumps(rm, ensure_ascii=False)
    assert '"text": "216"' in blob and "ул. Псковская, 3" in blob
    assert "Важно · 1" in blob and "12.09, 19.09" in blob


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
    week = [{"week": 2, "half": "bottom", "start": "07.09.2026", "end": "12.09.2026"}]
    rm = build_dashboard_rich_message(
        data["schedule"], week, "https://example.test", dt.date(2026, 9, 7),
        current_date=dt.date(2026, 9, 7),
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
    assert "Как читать время" not in blob


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
    assert "18:00–18:45" in blob
    assert "ак. ч." not in blob


@pytest.mark.parametrize("portal_time,room,note,place,when", [
    ("14:00 15:00", "1331", "Антоново", "1331\nАнтоново", "14:00–15:45"),
    ("14:00 15:00", ".", "с использованием ДОТ", "ДОТ", "14:00–15:45"),
    ("14:00 15:00 16:00 17:00", "202", "Антоново", "202\nАнтоново", "14:00–15:45\n16:00–17:45"),
    ("16:00 17:00 18:00", "Спортзал", "", "Спортзал", "16:00–17:45\n18:00–18:45"),
    ("09:00 10:00", ".", "", "место уточняется", "09:00–10:45"),
])
def test_dashboard_stacks_place_above_time_in_separate_cells(portal_time, room, note, place, when):
    data = parse_all(
        "<table>"
        "<tr><th>дата</th><th>время</th><th>под гр.</th><th>предмет</th>"
        "<th>преподаватель</th><th>ауд.</th><th>комм.</th></tr>"
        f"<tr><td>Пн</td><td>{portal_time}</td><td></td><td>(пр.) Проект</td>"
        f"<td>Иванов И. И.</td><td>{room}</td><td>{note}</td></tr>"
        "</table>"
    )
    rm = build_dashboard_rich_message(
        data["schedule"], WEEK_1, "https://example.test", dt.date(2026, 8, 31),
        current_date=dt.date(2026, 8, 31),
    )
    table = next(
        block
        for day in rm["rich_message"]["blocks"]
        if day.get("type") == "details"
        for block in day.get("blocks", [])
        if block.get("type") == "table"
    )
    validate_rich_payload(rm)

    assert len(table["cells"]) == 3
    assert _plain({"text": table["cells"][0][2]["text"]}) == "место / время"
    place_row, time_row = table["cells"][1:]
    assert len(place_row) == 3
    assert place_row[0]["rowspan"] == 2
    assert place_row[1]["rowspan"] == 2
    assert _plain({"text": place_row[2]["text"]}) == place
    assert len(time_row) == 1
    assert _plain({"text": time_row[0]["text"]}) == when
    assert time_row[0]["text"] == [{"type": "code", "text": when}]
    assert table["is_bordered"] is True
    assert "ак. ч." not in when


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
    """Одиночные скрины внутри раскрывающихся разделов дней."""
    out = []
    for block in blocks:
        if block.get("type") != "details":
            continue
        day = _plain(block.get("summary")).partition(" (")[0]
        assert not block.get("is_open") and not block.get("open")
        for inner in block.get("blocks") or []:
            if inner.get("type") == "photo" and "photo" in inner:
                out.append((day, inner["photo"]["media"], inner.get("caption", {}).get("text")))
    return out


def test_changes_post_attaches_screens_only_for_changed_days():
    rm = build_changes_rich_message(
        _sample_diff(), [], "https://example.test",
        now=dt.datetime(2026, 9, 2, 12, 0),
        screenshot_media=list(reversed(_all_day_media())),
    )
    blocks = rm["rich_message"]["blocks"]
    shots = _photos_by_day(blocks)
    # В диффе Вторник, Среда и Четверг — значит и скринов ровно три, по порядку недели.
    assert [day for day, _, _ in shots] == [
        "Вторник", "Среда", "Четверг",
    ]
    assert [media for _, media, _ in shots] == [
        "attach://changes_screenshot_2", "attach://changes_screenshot_3", "attach://changes_screenshot_4",
    ]
    for day, _, caption in shots:
        assert _plain(caption) == f"Новое расписание: {day} · оригинал на портале"
        assert caption[2] == {"type": "url", "text": "портале", "url": "https://example.test"}
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
    assert [day for day, _, _ in shots] == ["Понедельник"]
    blob = json.dumps(rm, ensure_ascii=False)
    assert "Скрин этого дня общий с Пн" in blob
    assert blob.count("attach://changes_screenshot_1") == 1
    validate_rich_payload({"chat_id": -1001, **rm})


def test_changes_post_without_screens_stays_text_only():
    rm = build_changes_rich_message(_sample_diff(), [], "https://example.test",
                                    now=dt.datetime(2026, 9, 2, 12, 0))
    assert _photos_by_day(rm["rich_message"]["blocks"]) == []
    assert "photo" not in json.dumps(rm, ensure_ascii=False)


def test_changes_post_comparisons_are_closed_and_ordered_before_after():
    rm = build_changes_rich_message(
        _sample_diff(), [], "https://example.test",
        now=dt.datetime(2026, 9, 2, 12, 0),
        screenshot_media=[
            {"label": label, "source_url": "https://example.test", "after_media": f"attach://after_{index}",
             "before_media": f"attach://before_{index}"}
            for index, label in reversed(list(enumerate(["Вт", "Среда", "Чт"])))
        ],
    )
    validate_rich_payload(rm)
    # Сравнение живёт внутри раскрывающегося раздела дня, без вложенного details.
    days = [block for block in rm["rich_message"]["blocks"] if block.get("type") == "details"]
    assert [day["summary"].partition(" (")[0] for day in days] == ["Вторник", "Среда", "Четверг"]
    slideshows = [
        node for node in _rich_nodes(rm) if node.get("type") == "slideshow"
    ]
    assert len(slideshows) == 3
    for index, slideshow in enumerate(slideshows):
        assert [photo["photo"]["media"] for photo in slideshow["blocks"]] == [
            f"attach://before_{index}", f"attach://after_{index}",
        ]
        day = ["Вторник", "Среда", "Четверг"][index]
        assert [_plain(photo["caption"]) for photo in slideshow["blocks"]] == [
            f"Было · {day}", f"Стало · {day}",
        ]
        assert "Было → стало" in _plain(slideshow["caption"])
    text = _plain(rm)
    for legend in ("Жёлтым выделены", "светло-красным", "зелёным",
                   "подсветка добавлена ботом", "Таблицы из сохранённых"):
        assert legend not in text
    assert "Их не подсвечиваем на скрине" not in text


@pytest.mark.parametrize("versions", [("before", "after"), ("before",), ("after",)])
@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("highlighted", [None, False])
def test_changes_post_comparison_removals_show_old_not_new(versions, count, highlighted):
    diff = _sample_diff()
    diff["added"] = []
    diff["changed"] = []
    diff["removed"] *= count
    rm = build_changes_rich_message(
        diff, [], "https://example.test", now=dt.datetime(2026, 9, 2, 12, 0),
        screenshot_media=[{
            "label": "Чт", "source_url": "https://example.test/snapshot",
            "highlighted": highlighted,
            **{f"{version}_media": f"attach://{version}" for version in versions},
        }],
    )
    validate_rich_payload(rm)
    text = _plain(rm)
    assert "В новом расписании этого занятия нет." not in text
    assert text.count("(пр.) Старый") == 0  # префикс вида выносится в контекст
    assert len(_main_rows(rm)) == count
    # Пропавшая пара: строка со знаком «−», время и место зачёркнуты.
    assert text.count("15:00–16:45") == count
    assert text.count("ауд. 202") == count
    struck = [node for node in _rich_nodes(rm) if node.get("type") == "strikethrough"]
    assert [node["text"] for node in struck] == ["15:00–16:45", "ауд. 202"] * count
    assert "Убрано:" not in text
    assert "В новом расписании её уже нет" not in text
    assert "В новом расписании их уже нет" not in text
    assert not any(b.get("type") == "footer" for b in rm["rich_message"]["blocks"])
    assert "Группа 6381" not in _plain(rm)
    assert "на скрине ниже" not in text
    assert "Их не подсвечиваем" not in text
    days = [block for block in rm["rich_message"]["blocks"] if block.get("type") == "details"]
    assert len(days) == 1
    assert days[0]["summary"] == ("Четверг (убрали пару)" if count == 1 else "Четверг · убрали пары (2 пары)")
    assert not days[0].get("is_open") and not days[0].get("open")
    photos = [node for node in _rich_nodes(rm) if node.get("type") == "photo" and "photo" in node]
    assert [photo["photo"]["media"] for photo in photos] == [
        f"attach://{version}" for version in versions
    ]
    # Текстовой легенды у сравнения больше нет: цвета видны на самих скринах.
    assert "В «Стало» убранных строк уже нет" not in text
    if len(versions) == 1:
        assert not any(node.get("type") == "slideshow" for node in _rich_nodes(rm))
        assert "Листай" not in text


def test_changes_post_keeps_legacy_legends_separate_from_comparisons():
    rm = build_changes_rich_message(
        _sample_diff(), [], "https://example.test", now=dt.datetime(2026, 9, 2, 12, 0),
        screenshot_media=[
            {"label": "Вт", "media": "attach://legacy_added", "marked": 1, "kinds": ["added"]},
            {"label": "Ср", "before_media": "attach://before", "after_media": "attach://after",
             "source_url": "https://example.test", "marked": 1, "kinds": ["changed"]},
            {"label": "Чт", "media": "attach://legacy_removed"},
        ],
    )
    validate_rich_payload(rm)
    text = _plain(rm)
    # Сравнение «Было/Стало» больше не печатает текстовую легенду: только скрины.
    assert "Жёлтым выделены" not in text
    assert "зелёным новые пары" in text
    assert "оранжевым" not in text.casefold()
    assert "Зелёным помечены новые пары" not in text
    assert "Пояснения" not in text
    assert "на скрине ниже её уже нет" not in text
    assert "Их не подсвечиваем на скрине" not in text
    shots = _photos_by_day(rm["rich_message"]["blocks"])
    assert [(day, media) for day, media, _ in shots] == [
        ("Вторник", "attach://legacy_added"), ("Четверг", "attach://legacy_removed"),
    ]
    assert _plain(shots[0][2]).endswith(" · зелёным новые пары")
    assert _plain(shots[1][2]) == "Новое расписание: Четверг · оригинал на портале"
    assert all("жёлтым" not in _plain(caption).casefold() for _, _, caption in shots)


@pytest.mark.parametrize("metadata", [
    {"before_kinds": [], "after_kinds": []},
    {"before_kinds": ["changed"], "after_kinds": []},
    {"before_kinds": [], "after_kinds": ["changed"]},
    {"before_kinds": ["removed"], "after_kinds": []},
    {"before_kinds": [], "after_kinds": ["added"]},
    {"before_kinds": ["changed", "removed"], "after_kinds": ["added"]},
    {"before_kinds": ["changed"], "after_kinds": [], "before_media": ""},
    {"before_kinds": [], "after_kinds": ["changed"], "after_media": ""},
])
def test_changes_post_comparison_has_no_text_legend(metadata):
    """Сравнение «Было/Стало» — только скрины, без текстовой легенды подсветки."""
    diff = _sample_diff()
    for kind in ("added", "removed", "changed"):
        diff[kind][0]["day"] = "Четверг"
    rm = build_changes_rich_message(
        diff, [], "https://example.test", now=dt.datetime(2026, 9, 2, 12, 0),
        screenshot_media=[{
            "label": "Чт", "source_url": "https://example.test",
            "before_media": "attach://before", "after_media": "attach://after", **metadata,
        }],
    )
    validate_rich_payload(rm)
    text = _plain(rm).casefold()
    for word in ("жёлтым", "зелёным", "светло-красным", "оранжевым",
                 "подсветка добавлена ботом", "таблицы из сохранённых", "открыть актуальную"):
        assert word not in text


@pytest.mark.parametrize("before_kinds, after_kinds", [(["removed"], ["added"]), ([], [])])
def test_changes_post_comparison_uses_applied_kinds_for_moves(before_kinds, after_kinds):
    lesson = _sample_diff()["removed"][0]
    diff = {
        "added": [{**lesson, "time": "17:00 18:00"}],
        "removed": [lesson], "changed": [], "transition": None,
    }
    rm = build_changes_rich_message(
        diff, [], "https://example.test", now=dt.datetime(2026, 9, 2, 12, 0),
        screenshot_media=[{
            "label": "Чт", "source_url": "https://example.test",
            "before_media": "attach://before", "after_media": "attach://after",
            "before_kinds": before_kinds, "after_kinds": after_kinds,
        }],
    )
    validate_rich_payload(rm)
    days = [block for block in rm["rich_message"]["blocks"] if block.get("type") == "details"]
    assert [day["summary"] for day in days] == ["Четверг (перенесли пару)"]
    text = _plain(rm)
    assert "↔\n15:00–16:45 → 17:00–18:45" in text
    assert "1. " not in text and "1 изменение" not in text
    # Легенды у сравнения нет независимо от того, что именно подсвечено.
    for word in ("жёлтым", "зелёным", "оранжевым", "светло-красным"):
        assert word not in text.casefold()


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
    validate_rich_payload(marked)
    blob = json.dumps(marked, ensure_ascii=False)
    shots = _photos_by_day(marked["rich_message"]["blocks"])
    assert len(shots) == 1
    assert _plain(shots[0][2]) == (
        "Новое расписание: Вторник · оригинал на портале · зелёным новые пары, оранжевым правки"
    )
    assert blob.count("зелёным новые пары, оранжевым правки") == 1
    assert "Зелёным помечены новые пары" not in blob
    assert "оранжевым — поправленные" not in blob
    assert "Пояснения" not in blob

    clean = build_changes_rich_message(
        diff, [], "https://example.test", now=now,
        screenshot_media=[{"label": "Вт", "media": "attach://changes_screenshot_1", "marked": 0}],
    )
    validate_rich_payload(clean)
    clean_blob = json.dumps(clean, ensure_ascii=False)
    # чистый скрин (chromium упал) — никаких обещаний про подсветку
    assert "зелёным" not in clean_blob
    assert "оранжевым" not in clean_blob
    assert "attach://changes_screenshot_1" in clean_blob
    clean_shots = _photos_by_day(clean["rich_message"]["blocks"])
    assert len(clean_shots) == 1
    assert _plain(clean_shots[0][2]) == "Новое расписание: Вторник · оригинал на портале"

    without_media = json.dumps(
        build_changes_rich_message(diff, [], "https://example.test", now=now),
        ensure_ascii=False,
    )
    assert "зелёным" not in without_media
    assert "оранжевым" not in without_media



def test_moved_pair_is_one_entry_not_two_rows():
    """Перенос пары — одна строка «было → стало», а не «убрали» + «добавили»."""
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
    validate_rich_payload(rm)
    blocks = rm["rich_message"]["blocks"]
    days = [block for block in blocks if block.get("type") == "details"]
    assert [day["summary"] for day in days] == ["Четверг (перенесли пару)"]
    rows = _main_rows(rm)
    assert len(rows) == 1
    what, lesson_cell, where = rows[0]
    assert _plain(what) == "↔\n11:00–12:45 → 17:00–18:45"
    assert {"type": "strikethrough", "text": "11:00–12:45"} in what["text"]
    assert {"type": "bold", "text": "17:00–18:45"} in what["text"]
    assert _plain(lesson_cell) == "География туризма\nпр.\nЕфимов Олег Николаевич"
    assert _plain(where) == "ауд. 418, Антоново"  # место то же — показано без «→»
    text = _plain(rm)
    assert text.count("11:00–12:45") == 1  # старое время только в строке правки
    assert "убрали" not in text.casefold() and "добавили" not in text.casefold()
    assert "1. " not in text and "1 изменение" not in text
    assert "Пояснения" not in text and "Портал не пишет «перенос»" not in text
    assert not any(b.get("type") == "footer" for b in blocks)
    assert "Группа 6381" not in _plain(rm)
    fallback = changes_fallback_text(diff, "https://example.test")
    assert fallback.count("<b>Четверг (перенесли пару)</b>") == 1
    assert "<s>11:00–12:45</s> → <b>17:00–18:45</b>" in fallback
    assert "убрали" not in fallback.casefold() and "добавили" not in fallback.casefold()



def test_real_removal_stays_removal():
    """Если пара просто исчезла, склеивать её не с чем."""
    diff = {
        "added": [],
        "removed": [{"day": "Пятница", "time": "09:00 10:00", "subject": "(лек.) Экономика",
                     "teacher": "Иванов И. И.", "room": "1306", "location": ""}],
        "changed": [], "transition": None,
    }
    rm = build_changes_rich_message(
        diff, [], "https://example.test", now=dt.datetime(2026, 9, 3, 12, 0),
    )
    validate_rich_payload(rm)
    blocks = rm["rich_message"]["blocks"]
    days = [block for block in blocks if block.get("type") == "details"]
    assert [day["summary"] for day in days] == ["Пятница (убрали пару)"]
    assert [inner["type"] for inner in days[0]["blocks"]] == ["table"]
    what, lesson_cell, where = _main_rows(rm)[0]
    assert _plain(what) == "−\n09:00–10:45"
    assert {"type": "strikethrough", "text": "09:00–10:45"} in what["text"]
    assert _plain(lesson_cell) == "Экономика\nлек.\nИванов И. И."
    assert where["text"] == [{"type": "strikethrough", "text": "ауд. 1306"}]
    text = _plain(rm)
    assert "В новом расписании этого занятия нет." not in text
    assert "перенесли" not in text.casefold()
    assert "1. " not in text and "1 изменение" not in text
    assert "Убрано:" not in text and "Пояснения" not in text



def test_rename_on_both_weeks_is_one_entry():
    """Переименование пришло в верх и низ недели — одна строка, а не две."""
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
    validate_rich_payload(rm)
    blocks = rm["rich_message"]["blocks"]
    days = [block for block in blocks if block.get("type") == "details"]
    assert [day["summary"] for day in days] == ["Понедельник (переименовали пару)"]
    rows = _main_rows(rm)
    assert len(rows) == 1
    _what, lesson_cell, where = rows[0]
    # Старое название зачёркнуто, новое жирным; вид пары и «обе недели» — контекст.
    assert _plain(lesson_cell) == (
        "Иностранные языки в сфере профессиональной коммуникации\nИностранный язык\n"
        "пр. · обе недели\nБарышева Ангелина Алексеевна"
    )
    assert {"type": "strikethrough",
            "text": "Иностранные языки в сфере профессиональной коммуникации"} in lesson_cell["text"]
    assert {"type": "bold", "text": "Иностранный язык"} in lesson_cell["text"]
    # Разные недели — разные места: показываем построчно, без слэша.
    assert _plain(where) == "Верхняя неделя: ауд. 1318, Антоново\nНижняя неделя: ДОТ"
    text = _plain(rm)
    assert "немец. яз" not in text  # примечания больше не выводятся
    assert "Раньше:" not in text
    assert "1 изменение" not in text and "1. " not in text
    assert not any(node.get("type") in {"list", "blockquote", "pullquote"} for node in _rich_nodes(rm))
    assert not any(b.get("type") == "footer" for b in blocks)
    assert "Группа 6381" not in _plain(rm)
    fallback = changes_fallback_text(diff, "https://example.test")
    assert fallback.count("<b>Понедельник (переименовали пару)</b>") == 1
    assert "<s>Иностранные языки в сфере профессиональной коммуникации</s>" in fallback
    assert "<b>Иностранный язык</b>" in fallback
    assert "Верхняя неделя: ауд. 1318, Антоново Нижняя неделя: ДОТ" in fallback  # ячейка — одной строкой


@pytest.mark.parametrize("clean_metadata", [{}, {"kinds": ["changed"]}], ids=["unknown-kinds", "known-kinds"])
def test_changes_post_names_only_the_colors_actually_marked(clean_metadata):
    """Only the screenshot caption describes the colors actually applied."""
    diff = {
        "added": [], "removed": [], "transition": None,
        "changed": [{"day": "Понедельник", "time": "09:00 10:00", "subject": "(пр.) Иностранный язык",
                     "room": "1318", "teacher": "Барышева А. А.", "location": "Антоново",
                     "note": "", "delivery_mode": "in_person",
                     "fields": [["предмет", "старое", "новое"]]}],
    }
    now = dt.datetime(2026, 9, 4, 11, 33)
    changed_only = build_changes_rich_message(
        diff, [], "https://example.test", now=now,
        screenshot_media=[{"label": "Пн", "media": "attach://changes_screenshot_1",
                           "marked": 2, "kinds": ["changed"]}],
    )
    validate_rich_payload(changed_only)
    shots = _photos_by_day(changed_only["rich_message"]["blocks"])
    assert len(shots) == 1
    assert _plain(shots[0][2]) == "Новое расписание: Понедельник · оригинал на портале · оранжевым правки"
    text = _plain(changed_only)
    assert text.count("оранжевым правки") == 1
    assert "Оранжевым помечены поправленные пары" not in text
    assert "Пояснения" not in text
    assert "зелёным" not in text.casefold() and "жёлтым" not in text.casefold()

    clean = build_changes_rich_message(
        diff, [], "https://example.test", now=now,
        screenshot_media=[{"label": "Пн", "media": "attach://changes_screenshot_1",
                           "marked": 1, "highlighted": False, **clean_metadata}],
    )
    validate_rich_payload(clean)
    # чистый скрин из общего кеша: красок на нём нет, обещать их нельзя
    assert "зелёным" not in _plain(clean) and "оранжевым" not in _plain(clean)
    shots = _photos_by_day(clean["rich_message"]["blocks"])
    assert len(shots) == 1
    assert shots[0][1] == "attach://changes_screenshot_1"
    assert _plain(shots[0][2]) == "Новое расписание: Понедельник · оригинал на портале"


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
    return [
        _plain(block.get("summary"))
        for block in rm["rich_message"]["blocks"]
        if block.get("type") == "details"
    ]


def test_dashboard_shows_whole_week_before_it_starts():
    data = _week_tail_timetable()
    rm = build_dashboard_rich_message(
        data["schedule"], WEEK_TAIL, "https://example.test", dt.date(2026, 9, 3),
        current_date=dt.date(2026, 9, 1),
    )
    days = _dashboard_day_summaries(rm)
    assert [item for item in days if "Скрин" not in item] == [
        "ЧЕТВЕРГ · 03.09  ·  1 пара",
        "ПЯТНИЦА · 04.09  ·  1 пара",
        "СУББОТА · 05.09  ·  1 пара",
    ]
    assert "На неделе:" in json.dumps(rm, ensure_ascii=False)


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
        "ПЯТНИЦА · 04.09  ·  1 пара",
        "СУББОТА · 05.09  ·  1 пара",
    ]
    assert "Четверг" not in json.dumps(rm, ensure_ascii=False)
    blob = json.dumps(rm, ensure_ascii=False)
    # Недельный итог остаётся, а к нему добавляется, сколько ещё впереди.
    assert '"На неделе: "' in blob
    assert '"осталось: "' in blob and '"2 пары"' in blob


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
    assert "ДОТ" in blob
    assert "с использованием ДОТ" not in blob
    assert '"text": "ЛЕК."' in blob and "История России" in blob
    assert '"text": "ПР."' in blob and "Философия" in blob


def test_dashboard_today_disappears_after_last_pair():
    """Вариант 2: после конца последней пары сегодняшний день уходит из закрепа."""
    data = _week_tail_timetable()
    friday = dt.date(2026, 9, 4)
    before = build_dashboard_rich_message(
        data["schedule"], WEEK_TAIL, "https://example.test", friday,
        current_date=friday, now=dt.datetime(2026, 9, 4, 14, 30),
    )
    assert [s for s in _dashboard_day_summaries(before) if "Скрин" not in s] == [
        "ПЯТНИЦА · 04.09  ·  1 пара",
        "СУББОТА · 05.09  ·  1 пара",
    ]
    after = build_dashboard_rich_message(
        data["schedule"], WEEK_TAIL, "https://example.test", friday,
        current_date=friday, now=dt.datetime(2026, 9, 4, 15, 46),
    )
    assert [s for s in _dashboard_day_summaries(after) if "Скрин" not in s] == [
        "СУББОТА · 05.09  ·  1 пара",
    ]


# --- ОПД по виртуальным группам в закрепе -----------------------------------

_OPD_SCHEDULE = {"days": {"Четверг": [
    {"number": 1, "subject": "(пр.) География туризма\nИГУМ, Антоново", "time": "9:00 10:00",
     "room": "418", "teacher": "Ефимов Олег Николаевич", "note": "ИГУМ, Антоново", "location": "ИГУМ, Антоново"},
    {"number": 2, "subject": "(лек/пр.) Основы проектной деятельности с 10.09.", "time": "14:00 15:00",
     "room": ".", "teacher": "—", "note": "https://portal.novsu.ru/study/newUniversity/i.1531736/?id=1651915"},
]}}
_OPD_WEEKS = [{"week": 3, "half": "top", "start": "14.09.2026", "end": "19.09.2026"}]


def _find_details(blocks: list[dict], summary_marker: str) -> dict | None:
    for block in blocks:
        if block.get("type") == "details":
            if summary_marker in json.dumps(block.get("summary"), ensure_ascii=False):
                return block
            found = _find_details(block.get("blocks") or [], summary_marker)
            if found:
                return found
    return None


def _opd_fixture() -> dict:
    import opd

    fixtures = Path("tests/fixtures")
    return opd.build_data(
        (fixtures / "opd_members.csv").read_text(encoding="utf-8"),
        (fixtures / "opd_doc.html").read_text(encoding="utf-8"),
        group="6381",
        today=dt.date(2026, 9, 17),
        fetched_at="2026-09-17T10:00:00+03:00",
        index_html=(fixtures / "opd_index.html").read_text(encoding="utf-8"),
    )


def test_dashboard_thursday_has_opd_section_by_buildings():
    import json

    rm = build_dashboard_rich_message(
        _OPD_SCHEDULE, _OPD_WEEKS, "https://example.test", dt.date(2026, 9, 17),
        current_date=dt.date(2026, 9, 14), opd=_opd_fixture(),
    )
    validate_rich_payload(rm)
    blob = json.dumps(rm, ensure_ascii=False)
    assert "ОПД · ПО ВИРТУАЛЬНЫМ ГРУППАМ" in blob
    assert "идут 6 из 10" in blob
    # Разделы по корпусам, внутри — раздел на студента (сначала 14:00, потом 16:00).
    for building in ("📍 Антоново", "📍 ул. Псковская, 3", "📍 ул. Советской Армии, 7"):
        assert building in blob
    assert "🕑" not in blob
    section = json.dumps(_find_details(rm["rich_message"]["blocks"], "ОПД · ПО"), ensure_ascii=False)
    assert "15:45" not in section and "17:45" not in section  # слоты ОПД не приводятся к парам
    assert '"15:00–16:00"' in section and '"с 15:00"' in section  # «с 15:00» у Зайцева-Петрова
    antonovo = _find_details(rm["rich_message"]["blocks"], "📍 Антоново")
    people = [b for b in antonovo["blocks"] if b.get("type") == "details"]
    summaries = [json.dumps(b["summary"], ensure_ascii=False) for b in people]
    assert len(people) == 2 and "Елисеев А. Д." in summaries[0] and "Жукова К. А." in summaries[1]
    assert "ВГ 118" in summaries[1] and '"16:00–17:00"' in summaries[1] and '"14:00–15:00"' in summaries[0]
    assert "ауд. 402 · ИЭ" in summaries[1] and "Трезорова О. Ю." in summaries[1]
    # Состав ВГ студента: раскрытый «Вместе в ВГ», внутри раскрытые институты
    # (крупные первыми) с таблицами «ФИО · Группа» и корпусом института.
    zhukova_blocks = people[1]["blocks"]
    zhukova = json.dumps(zhukova_blocks, ensure_ascii=False)
    assert "Вместе в ВГ 118" in zhukova and "2 чел. из других групп" in zhukova
    assert "ИЭ · Институт экономики · Антоново" in zhukova
    assert "ПТИ · Политехнический институт · Б. Санкт-Петербургская, 41" in zhukova
    together = zhukova_blocks[0]
    assert together["type"] == "details" and together.get("is_open") is True
    institutes = together["blocks"]
    assert [block.get("type") for block in institutes] == ["details", "details"]
    assert all(block.get("is_open") is True for block in institutes)
    mate_tables = [block["blocks"][0] for block in institutes]
    assert all(table.get("type") == "table" for table in mate_tables)
    for table, student, group in zip(mate_tables, ("Орлова В. П.", "Морозов И. И."), ("6001", "6311")):
        assert len(table["cells"]) == 2  # header plus one mate
        assert student in json.dumps(table["cells"][1][0], ensure_ascii=False)
        assert group in json.dumps(table["cells"][1][1], ensure_ascii=False)
    assert zhukova.index("Орлова") < zhukova.index("Морозов")
    # У Григорьева (ВГ 105) одногруппников по ВГ в таблице нет.
    sovarmii = _find_details(rm["rich_message"]["blocks"], "📍 ул. Советской Армии, 7")
    grigoriev = next(b for b in sovarmii["blocks"] if "Григорьев" in json.dumps(b["summary"], ensure_ascii=False))
    assert "пока не найден" in json.dumps(grigoriev["blocks"], ensure_ascii=False)
    assert "ауд. 106хк · ХТИ" in blob and "рядом с" not in blob
    # Отмены — таблицей: метка отдельным заголовком, ФИО и ВГ — ячейками.
    assert '"text": "❌ Занятий не будет"' in blob
    assert '"text": ["Алексеева А. П."]' in blob and '"text": ["101"]' in blob
    assert '"text": ["Борисов Г. О."]' in blob and '"text": ["102"]' in blob
    # Только про эту неделю: ни чужих дат, ни списка тех, кто идёт в другой четверг.
    assert "Волкова М. И." not in blob and "Кузнецов Н. Р." not in blob
    assert "Идут 24.09" not in blob and "далее" not in blob and "остальные" not in blob
    # Без пояснений и штампов: только таблицы, отмены и источники.
    assert "Занятие идёт по виртуальным группам" not in blob
    assert "Данные из документов" not in blob
    # Ссылка на объявление у слота ОПД не рождает «Важно · 1».
    assert "Важно" not in blob
    assert '"text": "с 15:00"' in blob
    assert "см. раздел ОПД ниже" in blob  # слот ОПД в таблице дня ведёт к разделу
    assert "место уточняется" not in blob
    assert "docs.google.com/document/" in blob and "docs.google.com/spreadsheets/" in blob


def test_dashboard_without_opd_data_is_unchanged():
    import json

    rm = build_dashboard_rich_message(
        _OPD_SCHEDULE, _OPD_WEEKS, "https://example.test", dt.date(2026, 9, 17),
        current_date=dt.date(2026, 9, 14),
    )
    validate_rich_payload(rm)
    blob = json.dumps(rm, ensure_ascii=False)
    assert "ОПД · ПО" not in blob and "см. раздел ОПД" not in blob
    assert "место уточняется" in blob
    assert "Важно" not in blob  # голый URL объявления не показываем и без раздела


def test_dashboard_opd_real_portal_note_survives_without_opd_data():
    import json

    schedule = {"days": {"Четверг": [
        {"number": 2, "subject": "(лек/пр.) Основы проектной деятельности", "time": "14:00 15:00",
         "room": ".", "teacher": "—", "note": "занятие переносится, см. объявление кафедры"},
    ]}}
    rm = build_dashboard_rich_message(
        schedule, _OPD_WEEKS, "https://example.test", dt.date(2026, 9, 17), current_date=dt.date(2026, 9, 14),
    )
    assert "Важно · 1" in json.dumps(rm, ensure_ascii=False)


def test_dashboard_opd_second_block_keeps_thursday_live():
    import json
    import zoneinfo

    msk = zoneinfo.ZoneInfo("Europe/Moscow")

    def _blob(now: dt.datetime, **kwargs) -> str:
        return json.dumps(build_dashboard_rich_message(
            _OPD_SCHEDULE, _OPD_WEEKS, "https://example.test", dt.date(2026, 9, 17), now=now, **kwargs,
        ), ensure_ascii=False)

    during_second_block = dt.datetime(2026, 9, 17, 16, 30, tzinfo=msk)
    assert "ЧЕТВЕРГ · 17.09" in _blob(during_second_block, opd=_opd_fixture())
    assert "ЧЕТВЕРГ · 17.09" not in _blob(during_second_block)  # портальная пара кончилась в 15:45
    after_second_block = dt.datetime(2026, 9, 17, 17, 5, tzinfo=msk)  # слот 16:00–17:00 уже прошёл
    assert "ЧЕТВЕРГ · 17.09" not in _blob(after_second_block, opd=_opd_fixture())


def test_dashboard_opd_section_absent_on_days_outside_document():
    import json

    weeks = [{"week": 12, "half": "top", "start": "16.11.2026", "end": "21.11.2026"}]
    rm = build_dashboard_rich_message(
        _OPD_SCHEDULE, weeks, "https://example.test", dt.date(2026, 11, 19),
        current_date=dt.date(2026, 11, 16), opd=_opd_fixture(),
    )
    blob = json.dumps(rm, ensure_ascii=False)
    assert "ОПД · ПО" not in blob and "место уточняется" in blob


def test_screenshot_caption_mentions_hidden_rows_only_when_day_has_expired_lesson():
    import json

    weeks = [
        {"week": 1, "half": "top", "start": "01.09.2026", "end": "05.09.2026"},
        {"week": 2, "half": "bottom", "start": "07.09.2026", "end": "12.09.2026"},
        {"week": 3, "half": "top", "start": "14.09.2026", "end": "19.09.2026"},
    ]
    schedule = {"days": {
        "Четверг": [
            {"number": 1, "subject": "(пр.) География туризма", "time": "9:00 10:00", "room": "418",
             "teacher": "Ефимов Олег Николаевич", "note": "ИГУМ, Антоново", "location": "ИГУМ, Антоново"},
            {"number": 3, "subject": "(пр.) География туризма", "time": "17:00 18:00", "room": "418",
             "teacher": "Ефимов Олег Николаевич", "note": "только 03.09. Антоново"},
        ],
        "Пятница": [
            {"number": 3, "subject": "(лек./пр.) Культурно-исторические центры", "time": "11:00 12:00",
             "room": "1310", "teacher": "Иванов Иван Иванович", "note": ""},
        ],
    }}
    screenshots = [
        {"label": "Чт", "media": "attach://site_screenshot_1"},
        {"label": "Пт", "media": "attach://site_screenshot_2"},
    ]
    rm = build_dashboard_rich_message(
        schedule, weeks, "https://example.test", dt.date(2026, 9, 17),
        screenshot_media=screenshots, current_date=dt.date(2026, 9, 14),
    )
    validate_rich_payload(rm)
    captions = {}
    def walk(blocks):
        for block in blocks:
            if block.get("type") == "photo":
                text = block["caption"]["text"]
                captions[text[0]["text"]] = "".join(t if isinstance(t, str) else t["text"] for t in text)
            walk(block.get("blocks") or [])
    walk(rm["rich_message"]["blocks"])
    # Четверг: разовая пара «только 03.09» уже прошла и на скрине скрыта.
    assert "прошедшие разовые занятия скрыты" in captions["Четверг"]
    # Пятница: скрывать нечего — подпись без этой фразы.
    assert "прошедшие разовые занятия скрыты" not in captions["Пятница"]
    assert captions["Пятница"] == "Пятница · полный оригинал"


def test_opd_building_table_orders_by_time_before_alphabet():
    import json

    from format import _opd_section

    row = {"status": "session", "room": "1", "place": "ИЭ, Антоново", "building": "Антоново",
           "teacher": "Иванов Иван Иванович", "teacher_short": "Иванов И. И.", "note": ""}
    view = {
        "date": "2026-09-17", "group": "6381", "fetched_at": "", "sources": {},
        "counts": {"session": 2, "cancelled": 0, "free": 0},
        "rows": [
            {**row, "student": "Антонов А. А.", "vg": "106", "block_start": "16:00", "block_end": "17:00"},
            {**row, "student": "Яковлев Я. Я.", "vg": "105", "block_start": "14:00", "block_end": "15:00"},
        ],
    }
    blob = json.dumps(_opd_section(view), ensure_ascii=False)
    assert blob.index("Яковлев Я. Я.") < blob.index("Антонов А. А.")
    assert "пока не найден" in blob  # строки без состава ВГ рендерятся
