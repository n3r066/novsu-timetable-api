"""Скриншот страницы расписания 6381 через headless chromium.

Используем системный /usr/bin/chromium-browser (snap) — playwright-bundled chromium
не установлен, но это OK, headless режим работает.

Рендерим portal.novsu.ru на ширину 1440, ждём пока загрузится таблица.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import config  # noqa: E402


def screenshot_url(url: str, out_png: Path, width: int = 1440, height: int = 2000) -> Path:
    """Скриншот URL через chromium headless."""
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        config.CHROMIUM_BIN,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--hide-scrollbars",
        f"--window-size={width},{height}",
        "--virtual-time-budget=8000",
        f"--screenshot={out_png}",
        url,
    ]
    subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    if not out_png.exists() or out_png.stat().st_size < 1000:
        raise RuntimeError(f"скриншот не получен: {out_png}")
    return out_png


if __name__ == "__main__":
    out = Path("/tmp/novsu-tt-monitor/state/6381_screenshot.png")
    p = screenshot_url(config.GROUP_URL, out)
    print(f"saved {p} ({p.stat().st_size} bytes)")
