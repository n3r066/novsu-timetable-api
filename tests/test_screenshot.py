from pathlib import Path

import pytest

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


def test_day_crop_items_can_filter_days_and_report_marks(monkeypatch, tmp_path):
    from screenshot import screenshot_schedule_day_crop_items
    import screenshot as ss

    rows = "".join(
        f'<tr><td rowspan="2">{day}</td></tr>'
        f'<tr><td>9:00<br>10:00</td><td></td><td>{day} предмет</td><td>Препод</td><td>101</td><td></td></tr>'
        for day in ("Пн", "Вт", "Ср")
    )
    source = (
        '<div id="npe_instance_1103357_npe_content"><h1>Расписание занятий</h1>'
        '<table><tr><th>дата</th><th>время</th><th>под гр.</th><th>предмет</th>'
        f'<th>преподаватель</th><th>ауд.</th><th>комм.</th></tr>{rows}</table>'
        '<a href="/print">Распечатать</a></div>'
    )
    rendered = []

    def fake_screenshot_html(html, path, **kwargs):
        rendered.append((str(path), html))
        return path

    monkeypatch.setattr(ss, "screenshot_html", fake_screenshot_html)
    items = screenshot_schedule_day_crop_items(
        source, "https://example.test", tmp_path / "day.png",
        diff_items=[{"day": "Вторник", "time": "9:00 10:00", "subject": "Вт предмет", "room": "101"}],
        diff_legend="Жёлтым подсвечены правки",
        only_labels={"Вт"},
    )
    # отрендерили и вернули только нужный день, нумерация частей осталась сквозной
    assert [item["label"] for item in items] == ["Вт"]
    assert items[0]["marked"] == 1
    assert items[0]["path"].name == "day_part02.png"
    assert len(rendered) == 1
    assert "diff-row" in rendered[0][1]


def test_diff_screens_are_wide_enough_for_the_comment_column():
    """Скрин рендерится шире таблицы: колонка «комм.» не должна уезжать за край."""
    import inspect

    import screenshot

    src = inspect.getsource(screenshot.screenshot_schedule_day_crop_items)
    # 1280 CSS-px не хватало на портальную таблицу с колонкой примечаний
    assert "width=1280" not in src
    assert "width=1480" in src


def test_long_portal_url_fits_inside_thursday_screenshot():
    """A source URL must wrap instead of stretching the seven-column table."""
    import shutil

    import config

    playwright = pytest.importorskip("playwright.sync_api")
    executable = shutil.which(config.CHROMIUM_BIN)
    if not executable:
        pytest.skip("System Chromium is not installed")
    url = "https://portal.novsu.ru/study/newUniversity/i.1531736/?id=1651915"
    source = f"""<table><tr><th>дата</th><th>время</th><th>под гр.</th>
    <th>предмет</th><th>преподаватель</th><th>ауд.</th><th>комм.</th></tr>
    <tr><td style="width:1px">Чт</td><td style="width:1px">14:00<br>15:00</td>
    <td style="width:1px"></td><td style="width:150px">Основы проектной деятельности с 10.09.</td>
    <td style="width:100px"></td><td style="width:1px">.</td>
    <td><a href="{url}">{url}</a></td></tr></table>"""
    html = render_source_schedule_day_chunk_htmls(source, "https://example.test", days_per_chunk=1)[0]
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=executable, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            page = browser.new_page(viewport={"width": 1480, "height": 3200})
            page.route("**/*", lambda route: route.abort())
            page.set_content(html, wait_until="domcontentloaded")
            assert page.locator("table").bounding_box()["width"] <= 1480
            assert page.evaluate("document.documentElement.scrollWidth") <= 1480
            # Preserve normal headers: wrapping a URL must not split headings
            # such as "дата" and "под гр." into a tall column of letters.
            assert max(cell.bounding_box()["height"] for cell in page.locator("th").all()) < 100
            link = page.locator(f'a[href="{url}"]')
            assert link.inner_text() == url
            bounds = link.evaluate("""e => {
                const range = document.createRange(); range.selectNodeContents(e);
                return [...range.getClientRects()].map(r => ({left:r.left,right:r.right}));
            }""")
            assert len(bounds) > 1
            assert all(0 <= rect["left"] < rect["right"] <= 1480 for rect in bounds)
        finally:
            browser.close()


def test_matte_short_day_trims_tail_without_losing_last_row(tmp_path):
    from PIL import Image, ImageDraw

    from screenshot import _trim_bottom_whitespace

    path = tmp_path / "short-day.png"
    image = Image.new("RGB", (600, 1600), (253, 253, 251))
    ImageDraw.Draw(image).rectangle((12, 20, 588, 350), fill=(225, 234, 245))
    ImageDraw.Draw(image).text((18, 340), "14:00 - 15:45", fill=(32, 56, 80))
    image.save(path)
    _trim_bottom_whitespace(path, bg=(255, 255, 255))
    cropped = Image.open(path)
    assert cropped.width == image.width and 350 < cropped.height <= 375
    assert cropped.crop((0, 0, 600, 351)).tobytes() == image.crop((0, 0, 600, 351)).tobytes()
