import copy
import datetime as dt
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

import post
from parse import parse_all, parse_schedule
from portal_parser import render_schedule_day_chunk_htmls


WEEKS = [{"week": 1, "half": "top", "start": "01.09.2026", "end": "05.09.2026"}]
for number in range(2, 19):
    start = dt.date(2026, 9, 7) + dt.timedelta(weeks=number - 2)
    end = min(start + dt.timedelta(days=5), dt.date(2026, 12, 31))
    WEEKS.append({"week": number, "half": "bottom" if number % 2 == 0 else "top",
                  "start": start.strftime("%d.%m.%Y"), "end": end.strftime("%d.%m.%Y")})

HEADER = "<tr><th>дата</th><th>время</th><th>под гр.</th><th>предмет</th><th>преподаватель</th><th>ауд.</th><th>комм.</th></tr>"
THURSDAY = "<table>" + HEADER + """
<tr><td rowspan="3">Чт</td></tr>
<tr><td>14:00 15:00</td><td></td><td>Основы проектной деятельности с 10.09.</td><td></td><td>.</td><td>Антоново</td></tr>
<tr><td>17:00 18:00</td><td></td><td>География туризма</td><td>Ефимов</td><td>418</td><td>только 03.09. Антоново</td></tr>
</table>"""


def render(html, date="2026-09-10"):
    data = {**parse_all(html), "weeks": WEEKS}
    original = copy.deepcopy(data)
    statuses = post._schedule_screenshot_statuses(data, dt.date.fromisoformat(date))
    chunks = render_schedule_day_chunk_htmls(html, "https://example.test", schedule_statuses=statuses)
    assert data == original
    return chunks


def lessons_in(chunk):
    soup = BeautifulSoup(chunk["html"], "html.parser")
    for tag in soup.select(".schedule-status-tag"):
        tag.decompose()
    parsed = parse_schedule(str(soup))
    return [lesson for lessons in parsed["days"].values() for lesson in lessons]


def test_expired_geography_disappears_but_source_and_comparisons_remain_complete():
    before = render(THURSDAY, "2026-09-03")[0]
    assert "География туризма" in before["html"]
    current = render(THURSDAY)[0]
    assert [row["subject_raw"] for row in lessons_in(current)] == ["Основы проектной деятельности с 10.09."]
    assert "География туризма" not in current["html"]
    assert "03.09" not in current["html"]
    data = {**parse_all(THURSDAY), "weeks": WEEKS}
    assert data["physical_lesson_count"] == 2
    statuses = post._schedule_screenshot_statuses(data, dt.date(2026, 9, 10))
    assert statuses["Чт"][0]["_schedule_expired"] is True
    clean = render_schedule_day_chunk_htmls(THURSDAY, "https://example.test")[0]
    assert "География туризма" in clean["html"]
    for title in ("Было", "Стало"):
        comparison = render_schedule_day_chunk_htmls(
            THURSDAY, "https://example.test", comparison_title=title, schedule_statuses=statuses,
        )[0]
        assert "География туризма" in comparison["html"]
        assert not BeautifulSoup(comparison["html"], "html.parser").select(".schedule-status-tag")


@pytest.mark.parametrize("removed", [0, 1, 2])
def test_removing_shared_span_owner_or_middle_or_tail_preserves_remaining_fields(removed):
    source = "<table>" + HEADER + '<tr><td rowspan="4">Чт</td></tr>'
    for index, name in enumerate(("А", "Б", "В")):
        time = '<td rowspan="3">15:00<br>16:00</td>' if index == 0 else ""
        shared = '<td rowspan="3"><a href="/teacher">Иванов</a></td><td rowspan="3">101</td>' if index == 0 else ""
        source += f'<tr>{time}<td></td><td>{name}</td>{shared}<td>{"только 03.09" if index == removed else ""}</td></tr>'
    source += "</table>"
    chunk = render(source)[0]
    lessons = lessons_in(chunk)
    assert [row["subject_raw"] for row in lessons] == [name for index, name in enumerate(("А", "Б", "В")) if index != removed]
    assert all(row["time"] == "15:00 16:00" and row["teacher"] == "Иванов" and row["room"] == "101" for row in lessons)
    soup = BeautifulSoup(chunk["html"], "html.parser")
    time_cell = next(cell for cell in soup.select("td") if "15:00" in cell.text)
    assert time_cell["rowspan"] == "2"
    assert soup.select_one('a[href="/teacher"]').parent["rowspan"] == "2"
    assert len(soup.select("tr[data-source-row]")) == 2


def test_shared_time_between_active_and_inactive_lessons_is_not_struck_through():
    source = Path("tests/fixtures/rowspan_time.html").read_text(encoding="utf-8")
    chunk = render(source)[0]
    soup = BeautifulSoup(chunk["html"], "html.parser")
    time_cell = next(cell for cell in soup.select("td") if "15:00" in cell.text)
    assert not time_cell.select(".schedule-status-content")
    assert "schedule-inactive" not in time_cell.get("class", [])
    rows = soup.select("tr[data-source-row]")
    assert rows[0]["data-schedule-status"] == "inactive"
    assert rows[1]["data-schedule-status"] == "dot"
    assert rows[0].select_one(".schedule-status-tag")
    assert rows[1].select_one(".schedule-status-tag").text == "ДОТ"


def test_future_explicit_dates_and_other_week_parity_stay_visible():
    source = Path("tests/fixtures/rowspan_time.html").read_text(encoding="utf-8")
    source = source.replace("по нижней неделе, с использованием ДОТ", "только 03.09, 17.09; с использованием ДОТ")
    chunk = render(source)[0]
    soup = BeautifulSoup(chunk["html"], "html.parser")
    assert len(lessons_in(chunk)) == 2
    assert len(soup.select("tr[data-schedule-status=inactive]")) == 2
    assert "17.09" in soup.get_text()
    expired = render(source, "2026-09-24")[0]
    assert [row["subject_raw"] for row in lessons_in(expired)] == ["(пр.) Предмет А"]


def test_nested_expired_first_lesson_cannot_swallow_next_day():
    source = "<table><tbody>" + HEADER + """
    <tr><td rowspan="2">Чт</td>
      <tr><td>9:00</td><td></td><td>Старая</td><td>А</td><td>101</td><td>только 03.09</td></tr>
      <tr><td rowspan="2">Пт</td>
        <tr><td>11:00</td><td></td><td>Оставшаяся</td><td>Б</td><td>202</td><td></td></tr>
      </tr>
    </tr></tbody></table>"""
    chunks = render(source)
    assert [chunk["label"] for chunk in chunks] == ["Пт"]
    assert [row["subject_raw"] for row in lessons_in(chunks[0])] == ["Оставшаяся"]
    assert "Старая" not in chunks[0]["html"]


def test_all_expired_rows_return_empty_not_unfiltered_fallback():
    source = "<table>" + HEADER + """
    <tr><td>Чт</td><td>9:00</td><td></td><td>Старая</td><td>А</td><td>101</td><td>только 03.09</td></tr></table>"""
    assert render(source) == []


def test_shared_colspan_is_transferred_without_losing_cell_content():
    source = "<table>" + HEADER + """
    <tr><td rowspan="3">Чт</td></tr>
    <tr><td rowspan="2">9:00</td><td></td><td>Старая</td><td colspan="2" rowspan="2">Общая</td><td>только 03.09</td></tr>
    <tr><td></td><td>Новая</td><td></td></tr></table>"""
    chunk = render(source)[0]
    lesson = lessons_in(chunk)[0]
    assert lesson["subject_raw"] == "Новая" and lesson["time"] == "9:00"
    assert lesson["teacher"] == lesson["room"] == "Общая"
    soup = BeautifulSoup(chunk["html"], "html.parser")
    assert soup.select_one('td[colspan="2"]').text == "Общая"


def test_missing_time_is_an_empty_logical_cell_not_a_shifted_subject():
    source = Path("tests/fixtures/alt_lesson_no_time.html").read_text(encoding="utf-8")
    chunk = render(source, "2026-09-07")[0]
    lessons = lessons_in(chunk)
    assert lessons[0]["time"] == "—"
    assert lessons[0]["subject_raw"] == "(пр.) Альтернатива"
    assert lessons[1]["time"] == "11:00 12:00"


def test_wrapped_source_table_is_not_copied_unfiltered_into_heading():
    source = '<div id="npe_instance_1103357_npe_content"><h1>Группа 6381</h1><div><h2>Осенний семестр</h2>' + THURSDAY + '</div></div>'
    chunk = render(source)[0]
    soup = BeautifulSoup(chunk["html"], "html.parser")
    assert len(soup.find_all("table")) == 1
    assert "География туризма" not in soup.get_text()
    assert soup.h1.text == "Группа 6381" and soup.h2.text == "Осенний семестр"
    assert len(lessons_in(chunk)) == 1


def test_changed_provenance_fails_instead_of_deleting_a_different_lesson():
    data = {**parse_all(THURSDAY), "weeks": WEEKS}
    statuses = post._schedule_screenshot_statuses(data, dt.date(2026, 9, 10))
    statuses["Чт"][0]["source_row"] = 2
    with pytest.raises(ValueError, match="provenance mismatch"):
        render_schedule_day_chunk_htmls(THURSDAY, "https://example.test", schedule_statuses=statuses)


def test_old_style_cache_is_regenerated_and_empty_cache_is_reused(monkeypatch, tmp_path):
    monkeypatch.setattr(post.config, "STATE_DIR", tmp_path)
    old = tmp_path / "old.png"
    old.write_bytes(b"old image")
    post._write_json_atomic(tmp_path / "dashboard_screens.json", {
        "fingerprint": "same", "status_key": "v3:2026-09-07", "items": [{"label": "Чт", "path": str(old)}],
    })
    calls = []
    monkeypatch.setattr(post, "_cached_day_screens", lambda *args, **kwargs: calls.append(kwargs) or [])
    data = {**parse_all(THURSDAY), "weeks": WEEKS}
    assert post._dashboard_screens(THURSDAY, "same", dt.date(2026, 9, 7), data=data) == []
    assert post._dashboard_screens(THURSDAY, "same", dt.date(2026, 9, 7), data=data) == []
    assert len(calls) == 1
    assert calls[0]["schedule_statuses"]["Чт"][0]["_schedule_expired"] is True
    assert post._read_json(tmp_path / "dashboard_screens.json")["status_key"] == "v4:2026-09-07"


def test_failed_new_render_never_reuses_old_policy_images(monkeypatch, tmp_path):
    monkeypatch.setattr(post.config, "STATE_DIR", tmp_path)
    old = tmp_path / "old.png"
    old.write_bytes(b"old image")
    post._write_json_atomic(tmp_path / "dashboard_screens.json", {
        "fingerprint": "same", "status_key": "v3:2026-09-07", "items": [{"label": "Чт", "path": str(old)}],
    })

    def fail(*args, **kwargs):
        raise RuntimeError("renderer unavailable")

    monkeypatch.setattr(post, "_cached_day_screens", fail)
    data = {**parse_all(THURSDAY), "weeks": WEEKS}
    assert post._dashboard_screens(THURSDAY, "same", dt.date(2026, 9, 7), data=data) == []
    assert post._read_json(tmp_path / "dashboard_screens.json")["fingerprint"] == ""
