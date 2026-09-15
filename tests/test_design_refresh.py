import copy

from bs4 import BeautifulSoup

from format import build_changes_rich_message, changes_fallback_text, _note_brief
from portal_parser import _status_label, render_schedule_day_chunk_htmls
from telegram_api import validate_rich_payload


def flatten(node):
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(flatten(x) for x in node)
    if isinstance(node, dict):
        return "".join(flatten(v) for k, v in node.items() if k in {"text", "blocks", "summary", "rich_message"})
    return ""


def test_missing_teacher_is_information_added_without_fake_replacement():
    for placeholder in ("", "—", "-", "не указано"):
        diff = {"changed": [{"day": "Понедельник", "subject": "(пр.) Иностранный язык",
                            "time": "09:00 10:00", "note": "немец. язык по верхней неделе",
                            "teacher": "Иванова Анна Игоревна",
                            "fields": [["преподаватель", placeholder, "Иванова Анна Игоревна"]]}]}
        original = copy.deepcopy(diff)
        payload = build_changes_rich_message(diff, [], "https://example.test")
        validate_rich_payload(payload)
        text = flatten(payload)
        assert "Указали преподавателя: Иванова Анна Игоревна" in text
        assert "→" not in text and "сменили преподавателя" not in text
        assert "09:00–10:45" in text and "верхняя неделя" in text
        assert text.count("Иностранный язык") == 1
        assert diff == original
        assert "Указали преподавателя: <b>Иванова Анна Игоревна</b>" in changes_fallback_text(diff, "https://example.test")


def test_language_expansion_consumes_full_word_and_is_idempotent():
    for note in ("англ. яз.", "англ. язык", "английский язык"):
        result = _note_brief({"note": note})
        assert result == "английский язык"
        assert _note_brief({"note": result}) == result
    assert _note_brief({"note": "немец. язык, консультация по согласованию"}) == "немецкий язык, консультация по согласованию"


def test_teacher_only_card_keeps_conditions_without_repeating_unchanged_addresses():
    common = {"day": "Вторник", "subject": "История", "time": "11:00 12:00",
              "teacher": "Смирнов", "fields": [["преподаватель", "", "Смирнов"]]}
    diff = {"changed": [{**common, "room": "1318", "location": "Антоново", "note": "по верхней неделе с 14.09"},
                        {**common, "room": "3207", "location": "Другой адрес", "note": "по нижней неделе с 21.09"}]}
    text = flatten(build_changes_rich_message(diff, [], "https://example.test"))
    assert text.count("Указали преподавателя") == 1
    assert "обе недели" in text
    assert "Верхняя неделя: с 14.09" in text and "Нижняя неделя: с 21.09" in text
    assert "1318" not in text and "Другой адрес" not in text


def test_compact_week_badges_preserve_inclusive_exclusive_wording():
    assert _status_label({"_schedule_struct": {"kind": "after_week", "data": {"week_number": 9}}}) == "После 9‑й недели"
    assert _status_label({"_schedule_tag": "НЕ НА ЭТОЙ НЕДЕЛЕ · С 9‑Й НЕДЕЛИ"}) == "С 9‑й недели"
    assert _status_label({"_schedule_tag": "ДОТ"}) == "ДОТ"


def test_original_layout_and_headings_remain_with_muted_blue_status():
    from portal_parser import SCHEDULE_STATUS_CSS
    source = '<h1>Расписание занятий</h1><h2>Группа 6381</h2>'
    source += '<table><tr><th>дата</th><th>время</th><th>предмет</th><th>преподаватель</th><th>ауд.</th></tr>'
    source += '<tr><td>Пт</td><td>09:00</td><td>История</td><td>Петров</td><td>301</td></tr></table>'
    source = '<div id="npe_instance_1103357_npe_content">' + source + '</div>'
    chunk = render_schedule_day_chunk_htmls(source, "https://example.test", schedule_statuses={})[0]
    soup = BeautifulSoup(chunk["html"], "html.parser")
    assert soup.h1.text == "Расписание занятий" and soup.h2.text == "Группа 6381"
    assert soup.select_one(".screen-header") is None
    assert "color: #4d627a" in SCHEDULE_STATUS_CSS and "opacity: 1" in SCHEDULE_STATUS_CSS
    assert "История" in soup.get_text() and "Петров" in soup.get_text()
