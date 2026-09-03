"""Скриншот страницы расписания 6381 через headless chromium.

Используем системный /usr/bin/chromium-browser (snap) — playwright-bundled chromium
не установлен, но это OK, headless режим работает.

Рендерим portal.novsu.ru на ширину 1440, ждём пока загрузится таблица.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import config  # noqa: E402
from portal_parser import render_schedule_crop_html, render_schedule_day_chunk_htmls  # noqa: E402


def render_source_schedule_crop_html(source_html: str, source_url: str) -> str:
    return render_schedule_crop_html(source_html, source_url)


def render_source_schedule_day_chunk_htmls(
    source_html: str,
    source_url: str,
    days_per_chunk: int = 2,
    diff_items: list[dict] | None = None,
    diff_legend: str = "",
) -> list[str]:
    return [
        chunk["html"]
        for chunk in render_schedule_day_chunk_htmls(
            source_html, source_url, days_per_chunk=days_per_chunk,
            diff_items=diff_items, diff_legend=diff_legend,
        )
    ]


def screenshot_url(url: str, out_png: Path, width: int = 1920, height: int = 3000, scale_factor: int = 2) -> Path:
    """Скриншот URL через chromium headless."""
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        config.CHROMIUM_BIN,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--hide-scrollbars",
        f"--force-device-scale-factor={scale_factor}",
        f"--window-size={width},{height}",
        "--virtual-time-budget=8000",
        f"--screenshot={out_png}",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    if result.returncode != 0:
        err = (result.stderr or b"").decode(errors="replace")[:500]
        raise RuntimeError(f"chromium screenshot failed: {err}")
    if not out_png.exists() or out_png.stat().st_size < 1000:
        raise RuntimeError(f"скриншот не получен: {out_png}")
    _trim_bottom_whitespace(out_png, bg=(255, 255, 255))
    return out_png


def screenshot_schedule_crop(source_html: str, source_url: str, out_png: Path) -> Path:
    """Screenshot only the original timetable table, enlarged and cropped."""
    html = render_source_schedule_crop_html(source_html, source_url)
    return screenshot_html(html, out_png, width=1480, height=4600, scale_factor=2, bg=(255, 255, 255))


def screenshot_schedule_day_crops(source_html: str, source_url: str, out_png: Path) -> list[Path]:
    """Screenshot timetable chunks split at day boundaries."""
    out_png = Path(out_png)
    chunks = render_schedule_day_chunk_htmls(source_html, source_url, days_per_chunk=1)
    paths: list[Path] = []
    stem = out_png.with_suffix("")
    for index, chunk in enumerate(chunks, 1):
        part = out_png if len(chunks) == 1 else out_png.with_name(f"{stem.name}_part{index:02d}{out_png.suffix}")
        screenshot_html(chunk["html"], part, width=1480, height=3200, scale_factor=2, bg=(255, 255, 255))
        paths.append(part)
    return paths


def screenshot_schedule_day_crop_items(
    source_html: str,
    source_url: str,
    out_png: Path,
    *,
    diff_items: list[dict] | None = None,
    diff_legend: str = "",
    only_labels: set[str] | None = None,
) -> list[dict]:
    """Return [{label, path, marked}] screenshots split by whole timetable days.

    *diff_items* — записи диффа (добавленные/изменённые): их строки и ячейки
    подсвечиваются прямо в портальной таблице, поэтому на скрине видно, что
    именно поменялось. *only_labels* — короткие дни портала («Ср»): рендерим
    только нужные дни, остальные части пропускаем.
    """
    out_png = Path(out_png)
    chunks = render_schedule_day_chunk_htmls(
        source_html,
        source_url,
        days_per_chunk=1,
        diff_items=diff_items,
        diff_legend=diff_legend,
    )
    items: list[dict] = []
    stem = out_png.with_suffix("")
    for index, chunk in enumerate(chunks, 1):
        label = str(chunk.get("label") or "")
        if only_labels is not None and not any(
            part.strip() in only_labels for part in label.split("+")
        ):
            continue
        path = out_png if len(chunks) == 1 else out_png.with_name(f"{stem.name}_part{index:02d}{out_png.suffix}")
        screenshot_html(chunk["html"], path, width=1280, height=3200, scale_factor=2, bg=(255, 255, 255))
        items.append({"label": label, "path": path, "marked": int(chunk.get("marked") or 0)})
    return items


def screenshot_html(
    html: str,
    out_png: Path,
    width: int = 880,
    height: int = 9000,
    scale_factor: int = 1,
    bg: tuple[int, int, int] = (238, 241, 245),
) -> Path:
    """Screenshot a standalone HTML string (the rendered schedule) to PNG.

    Writes the HTML to a temp file and renders it headless. Height is generous
    so the whole multi-day timetable fits; the page has a white background, so
    any extra space below is just whitespace.
    """
    out_png = Path(out_png).resolve()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    html_file = out_png.with_suffix(".html")
    html_file.write_text(html, encoding="utf-8")
    cmd = [
        config.CHROMIUM_BIN,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--hide-scrollbars",
        "--default-background-color=00000000",
        f"--force-device-scale-factor={scale_factor}",
        f"--window-size={width},{height}",
        "--virtual-time-budget=8000",
        f"--screenshot={out_png}",
        html_file.resolve().as_uri(),
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=90, check=False)
    if result.returncode != 0:
        err = (result.stderr or b"").decode(errors="replace")[:500]
        raise RuntimeError(f"chromium screenshot failed: {err}")
    if not out_png.exists() or out_png.stat().st_size < 1000:
        raise RuntimeError(f"скриншот не получен: {out_png}")
    _trim_bottom_whitespace(out_png, bg=bg)
    return out_png


def _trim_bottom_whitespace(out_png: Path, bg: tuple[int, int, int] = (238, 241, 245)) -> None:
    """Crop the empty grey/white tail below the rendered card."""
    try:
        from PIL import Image
        img = Image.open(out_png).convert("RGB")
        px = img.load()
        width, height = img.size
        last = height - 1
        while last >= 0:
            content = False
            for x in range(0, width, 3):
                r, g, b = px[x, last]
                if abs(r - bg[0]) > 10 or abs(g - bg[1]) > 10 or abs(b - bg[2]) > 10:
                    content = True
                    break
            if content:
                break
            last -= 1
        if 0 < last < height - 1:
            img.crop((0, 0, width, min(height, last + 20))).save(out_png)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    out = Path("/tmp/novsu-tt-monitor/state/6381_screenshot.png")
    p = screenshot_url(config.GROUP_URL, out)
    print(f"saved {p} ({p.stat().st_size} bytes)")
