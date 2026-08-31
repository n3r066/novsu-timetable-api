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
    assert "Добавлено — 1" in blob
    assert "Убрано — 1" in blob
    assert "Изменено — 1" in blob
    assert "(лек.) Новый" in blob
    assert "203 → 415" in blob
    assert "портал НовГУ" in blob
    assert '"type": "table"' in blob


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


def test_dashboard_rich_message_has_diary_and_week_sections():
    import json
    data = _load()
    weeks = [{"week": 1, "half": "top", "start": "01.09.2026", "end": "05.09.2026"}]
    screenshots = [
        {"label": "Вт", "media": "attach://site_screenshot_1"},
        {"label": "Ср", "media": "attach://site_screenshot_2"},
    ]
    rm = build_dashboard_rich_message(data["schedule"], weeks, "https://example.test", dt.date(2026, 9, 2), screenshot_media=screenshots, current_date=dt.date(2026, 8, 24))
    blob = json.dumps(rm, ensure_ascii=False)
    assert "Расписание группы 6381" in blob
    assert "Фокус: 02.09.2026" in blob
    assert "Дневник" in blob
    assert "Полное расписание" in blob
    assert "Неделя 1 (верхняя)" in blob
    assert "Как читать" in blob
    assert "портал НовГУ" in blob
    assert '"type": "footer"' not in blob
    assert "Среда — 02.09: 0 пар" in blob
    assert "Скрины · оригинал по дням" not in blob
    assert "Скрин: Вторник" in blob
    assert "Скрин: Среда" in blob
    assert "Скрин с сайта" not in blob
    assert '"text": "день"' not in blob
    assert '"text": "скрин"' not in blob
    assert '"text": "ниже"' not in blob
    assert "attach://site_screenshot_1" in blob
    assert "attach://site_screenshot_2" in blob
    assert "Вторник" in blob
    assert "Среда" in blob
    assert "оригинальное" in blob
    assert '"text": "расписание"' in blob
    assert '"type": "photo"' in blob
    assert '"header"' not in blob
    assert "details" in blob and "table" in blob


def test_dashboard_screenshot_section_is_the_only_open_section():
    data = _load()
    weeks = [{"week": 1, "half": "top", "start": "01.09.2026", "end": "05.09.2026"}]
    screenshots = [
        {"label": "Вт", "media": "attach://site_screenshot_1"},
        {"label": "Ср", "media": "attach://site_screenshot_2"},
    ]
    rm = build_dashboard_rich_message(data["schedule"], weeks, "https://example.test", dt.date(2026, 9, 2), screenshot_media=screenshots, current_date=dt.date(2026, 9, 2))
    details = []

    def walk(blocks):
        for block in blocks:
            if block.get("type") == "details":
                details.append(block)
                walk(block.get("blocks", []))

    walk(rm["rich_message"]["blocks"])
    open_sections = [block.get("summary") for block in details if block.get("is_open") or block.get("open")]
    assert open_sections == ["Скрин: Среда"]


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
    tables = [
        block for block in _walk_rich_blocks(payload["rich_message"]["blocks"])
        if block.get("type") == "table"
    ]
    assert len(tables) > 1
    assert all(len(table["cells"]) <= 26 for table in tables)  # header + 25 rows
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
    )
    blocks = rm["rich_message"]["blocks"]
    # block 0 = pullquote (title)
    # block 1 = details (week) — must come BEFORE diary
    # block 3 = details (diary)
    assert blocks[0]["type"] == "pullquote"
    assert blocks[1]["type"] == "details"
    assert "Дневник" not in json.dumps(blocks[1], ensure_ascii=False)
    assert "Полное расписание" in json.dumps(blocks[1], ensure_ascii=False)
    diary_found = False
    for b in blocks[2:]:
        if b.get("type") == "details":
            blob = json.dumps(b, ensure_ascii=False)
            if "Дневник" in blob:
                diary_found = True
                break
    assert diary_found
