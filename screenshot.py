"""Скриншот страницы расписания 6381 через headless chrome.

Бинарник берётся из config.CHROMIUM_BIN (google-chrome, иначе chromium).
Процесс запускается с HOME внутри state/, чтобы работать от непривилегированного
пользователя под NoNewPrivileges= (snap-chromium так не умеет).

Рендер идёт через Playwright: один процесс Chrome на пачку скринов
(``browser_session()``), скрин ровно по высоте контента (``full_page``), без
запуска браузера на каждый день. Если Playwright недоступен — запасной путь
через CLI ``--screenshot`` с обрезкой пустого хвоста.

Рендерим portal.novsu.ru на ширину 1440, ждём пока загрузится таблица.
"""

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
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


class _PlaywrightSession:
    """One headless Chrome shared by a batch of screenshots."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._contexts: dict[tuple[int, int, int], object] = {}
        self._pages: dict[tuple[int, int, int], object] = {}
        self._failed = False

    def ensure_started(self) -> bool:
        """Launch Chrome on first use; False when Playwright/Chrome is unavailable.

        Ленивый старт: если все дни уже в кеше, браузер не запускается вовсе
        (старт плюс остановка Playwright стоили около 4 секунд на цикл).
        """
        if self._browser is not None:
            return True
        if self._failed:
            return False
        try:
            self.start()
        except Exception as exc:  # noqa: BLE001 - нет playwright/бинарника: уходим в CLI
            self._failed = True
            print(f"[screenshot] playwright unavailable, using CLI chrome: {exc!s:.300}", file=sys.stderr)
            return False
        return True

    def start(self) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(
                executable_path=config.CHROMIUM_BIN,
                headless=True,
                args=["--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--disable-dev-shm-usage"],
                env=_browser_env(),
            )
        except Exception:
            self._pw.stop()
            self._pw = None
            raise

    def _page(self, width: int, height: int, scale_factor: int):
        """One page per (width, height, scale): открытие новой вкладки на каждый скрин стоило ~0.3 с."""
        key = (width, height, scale_factor)
        page = self._pages.get(key)
        if page is None:
            # Низкий viewport + full_page: высота скрина равна высоте документа,
            # а не «щедрому» холсту, который потом пришлось бы обрезать.
            context = self._browser.new_context(
                viewport={"width": width, "height": min(height, 400)},
                device_scale_factor=scale_factor,
            )
            self._contexts[key] = context
            page = context.new_page()
            # Ничего не тянем из сети: шаблоны самодостаточны, а <base href> на
            # портал не должен превращать рендер в ожидание чужого сервера.
            page.route("**/*", lambda route: route.continue_() if route.request.url.startswith("file:") else route.abort())
            self._pages[key] = page
        return page

    def render(
        self,
        target: str,
        out_png: Path,
        *,
        width: int,
        height: int,
        scale_factor: int,
        transparent: bool,
        timeout_s: int,
    ) -> None:
        page = self._page(width, height, scale_factor)
        page.goto(target, wait_until="load", timeout=timeout_s * 1000)
        page.screenshot(path=str(out_png), full_page=True, omit_background=transparent, timeout=timeout_s * 1000)

    def close(self) -> None:
        for closer in (
            *(context.close for context in self._contexts.values()),
            *((self._browser.close,) if self._browser else ()),
            *((self._pw.stop,) if self._pw else ()),
        ):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass
        self._contexts.clear()
        self._pages.clear()
        self._browser = None
        self._pw = None


_ACTIVE_SESSION: _PlaywrightSession | None = None


@contextmanager
def browser_session():
    """Reuse one Chrome for every screenshot inside the block; nesting reuses the outer one."""
    global _ACTIVE_SESSION
    if _ACTIVE_SESSION is not None:
        yield
        return
    session = _PlaywrightSession()
    _ACTIVE_SESSION = session
    try:
        yield
    finally:
        _ACTIVE_SESSION = None
        session.close()


def _render(
    target: str,
    out_png: Path,
    *,
    width: int,
    height: int,
    scale_factor: int,
    transparent: bool,
    timeout_s: int,
    cli_args: list[str],
) -> None:
    """Render *target* (URL or file URI) to *out_png* via the shared session, a one-off session, or CLI."""
    session = _ACTIVE_SESSION
    one_off = session is None
    if one_off:
        session = _PlaywrightSession()
    try:
        if not session.ensure_started():
            _run_browser(cli_args, out_png, timeout=timeout_s)
            return
        try:
            session.render(
                target, out_png, width=width, height=height, scale_factor=scale_factor,
                transparent=transparent, timeout_s=timeout_s,
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"chromium screenshot failed: {exc!s:.500}") from exc
    finally:
        if one_off:
            session.close()
    if not out_png.exists() or out_png.stat().st_size < 1000:
        raise RuntimeError(f"скриншот не получен: {out_png}")


_BROWSER_FLAGS = [
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    "--hide-scrollbars",
    "--no-first-run",
    "--disable-crash-reporter",
    "--disable-breakpad",
    "--disable-background-networking",
]


def _browser_env() -> dict[str, str]:
    """Environment with a writable HOME so Chrome starts as an unprivileged user."""
    home = config.BROWSER_HOME
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_CACHE_HOME"] = str(home / ".cache")
    return env


def _run_browser(args: list[str], out_png: Path, timeout: int) -> None:
    cmd = [config.CHROMIUM_BIN, *_BROWSER_FLAGS, *args, f"--screenshot={out_png}"]
    result = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False, env=_browser_env())
    if result.returncode != 0:
        err = (result.stderr or b"").decode(errors="replace")[:500]
        raise RuntimeError(f"chromium screenshot failed: {err}")
    if not out_png.exists() or out_png.stat().st_size < 1000:
        raise RuntimeError(f"скриншот не получен: {out_png}")


def screenshot_url(url: str, out_png: Path, width: int = 1920, height: int = 3000, scale_factor: int = 2) -> Path:
    """Скриншот URL через chromium headless."""
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    _render(
        url, out_png, width=width, height=height, scale_factor=scale_factor, transparent=False, timeout_s=60,
        cli_args=[
            f"--force-device-scale-factor={scale_factor}",
            f"--window-size={width},{height}",
            "--virtual-time-budget=8000",
            url,
        ],
    )
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
    with browser_session():
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
    diff_removed_note: bool = True,
) -> list[dict]:
    """Return [{label, path, marked[, marked_kinds]}] screenshots split by whole timetable days.

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
        removed_note=diff_removed_note,
    )
    items: list[dict] = []
    stem = out_png.with_suffix("")
    with browser_session():
        for index, chunk in enumerate(chunks, 1):
            label = str(chunk.get("label") or "")
            if only_labels is not None and not any(
                part.strip() in only_labels for part in label.split("+")
            ):
                continue
            path = out_png if len(chunks) == 1 else out_png.with_name(f"{stem.name}_part{index:02d}{out_png.suffix}")
            # 1280 CSS-px не хватало: правая колонка «комм.» уезжала за край скрина.
            # Остальные скрины расписания рендерятся в 1480 — держим ту же ширину.
            screenshot_html(chunk["html"], path, width=1480, height=3200, scale_factor=2, bg=(255, 255, 255))
            items.append({
                "label": label,
                "path": path,
                "marked": int(chunk.get("marked") or 0),
                # Типы помеченных правок идут дальше в пост: подпись обещает
                # только те цвета, которые реально легли на скрин.
                **({"marked_kinds": list(chunk["marked_kinds"])} if chunk.get("marked_kinds") else {}),
            })
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
    _render(
        html_file.resolve().as_uri(), out_png, width=width, height=height, scale_factor=scale_factor,
        transparent=True, timeout_s=90,
        cli_args=[
            "--default-background-color=00000000",
            f"--force-device-scale-factor={scale_factor}",
            f"--window-size={width},{height}",
            "--virtual-time-budget=8000",
            html_file.resolve().as_uri(),
        ],
    )
    _trim_bottom_whitespace(out_png, bg=bg)
    return out_png


def _trim_bottom_whitespace(out_png: Path, bg: tuple[int, int, int] = (238, 241, 245)) -> None:
    """Crop the empty grey/white tail below the rendered card.

    Считаем через ImageChops (C-код), а не попиксельным циклом на Python:
    на скрине 2960×6400 цикл занимал около 5 секунд на каждый день. Если
    нижняя полоса уже содержит контент (full_page-рендер и так по высоте
    документа), файл не пересохраняем — перекодирование PNG стоит ~0.5 с.
    """
    try:
        from PIL import Image, ImageChops

        img = Image.open(out_png).convert("RGB")
        width, height = img.size

        def content_mask(region):
            diff = ImageChops.difference(region, Image.new("RGB", region.size, bg))
            # Как и раньше: пиксель считается контентом, если любой канал отличается от фона больше чем на 10.
            channels = [band.point(lambda value: 255 if value > 10 else 0) for band in diff.split()]
            return ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]), channels[2])

        tail = 32
        if height > tail and content_mask(img.crop((0, height - tail, width, height))).getbbox():
            return
        bbox = content_mask(img).getbbox()
        last = (bbox[3] - 1) if bbox else -1
        if 0 < last < height - 1:
            img.crop((0, 0, width, min(height, last + 20))).save(out_png)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    out = config.STATE_DIR / "_screenshot_probe.png"
    p = screenshot_url(config.GROUP_URL, out)
    print(f"saved {p} ({p.stat().st_size} bytes)")
