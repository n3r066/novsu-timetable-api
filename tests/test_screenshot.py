from pathlib import Path

from screenshot import render_source_schedule_crop_html, render_source_schedule_day_chunk_htmls


def test_render_source_schedule_crop_html_keeps_only_schedule_table():
    source = Path("tests/fixtures/rowspan_time.html").read_text(encoding="utf-8")
    html = render_source_schedule_crop_html(source, "https://example.test/source")
    assert html.startswith("<!doctype html>")
    assert "Предмет А" in html
    assert "Предмет Б" in html
    assert "width: 1280px" in html
    assert "https://example.test/source" in html


def test_render_source_schedule_crop_html_drops_print_link():
    source = """
    <div id="npe_instance_1103357_npe_content">
      <h1>Расписание занятий</h1>
      <h2>Группа 6381</h2>
      <table><tr><th>дата</th><th>время</th><th>под гр.</th><th>предмет</th><th>преподаватель</th><th>ауд.</th><th>комм.</th></tr><tr><td>Пн</td><td>9:00</td><td></td><td>Предмет</td><td>Препод</td><td>101</td><td></td></tr></table>
      <a href="/print">Распечатать</a>
    </div>
    """
    html = render_source_schedule_crop_html(source, "https://example.test/source")
    assert "Расписание занятий" in html
    assert "Предмет" in html
    assert "Распечатать" in html


def test_render_source_schedule_crop_html_drops_content_after_print_link():
    source = """
    <div id="npe_instance_1103357_npe_content">
      <h1>Расписание занятий</h1>
      <table><tr><th>дата</th><th>время</th><th>под гр.</th><th>предмет</th><th>преподаватель</th><th>ауд.</th><th>комм.</th></tr><tr><td>Пн</td><td>9:00</td><td></td><td>Предмет</td><td>Препод</td><td>101</td><td></td></tr></table>
      <a href="/print">Распечатать</a><br><br><a href="/back">Возврат к списку групп</a>
    </div>
    """
    html = render_source_schedule_crop_html(source, "https://example.test/source")
    assert "Распечатать" in html
    assert "Возврат к списку групп" not in html


def test_render_source_schedule_day_chunk_htmls_split_only_between_days():
    rows = "".join(
        f"<tr><td>{day}</td></tr><tr><td>9:00</td><td></td><td>{day} предмет</td><td>Препод</td><td>101</td><td></td></tr>"
        for day in ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб")
    )
    source = f"""
    <div id="npe_instance_1103357_npe_content">
      <h1>Расписание занятий</h1>
      <table><tr><th>дата</th><th>время</th><th>под гр.</th><th>предмет</th><th>преподаватель</th><th>ауд.</th><th>комм.</th></tr>{rows}</table>
      <a href="/print">Распечатать</a><br><br><a href="/back">Возврат к списку групп</a>
    </div>
    """
    chunks = render_source_schedule_day_chunk_htmls(source, "https://example.test/source", days_per_chunk=2)
    assert len(chunks) == 3
    assert "Пт предмет" in chunks[2]
    assert "Сб предмет" in chunks[2]
    assert "Возврат к списку групп" not in chunks[2]
    assert "Распечатать" in chunks[2]


def test_render_source_schedule_day_chunk_htmls_can_split_one_day_per_chunk():
    rows = "".join(
        f"<tr><td>{day}</td></tr><tr><td>9:00</td><td></td><td>{day} предмет</td><td>Препод</td><td>101</td><td></td></tr>"
        for day in ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб")
    )
    source = f"""
    <div id="npe_instance_1103357_npe_content">
      <h1>Расписание занятий</h1>
      <table><tr><th>дата</th><th>время</th><th>под гр.</th><th>предмет</th><th>преподаватель</th><th>ауд.</th><th>комм.</th></tr>{rows}</table>
      <a href="/print">Распечатать</a><br><br><a href="/back">Возврат к списку групп</a>
    </div>
    """
    chunks = render_source_schedule_day_chunk_htmls(source, "https://example.test/source", days_per_chunk=1)
    assert len(chunks) == 6
    assert "Пт предмет" in chunks[4]
    assert "Сб предмет" not in chunks[4]
    assert "Сб предмет" in chunks[5]
