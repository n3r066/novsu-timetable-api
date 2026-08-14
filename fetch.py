"""Загрузка страницы группы 6381 + календаря недель."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import config  # noqa: E402

STATE_DIR = config.STATE_DIR


def fetch_html(url: str = config.GROUP_URL) -> str:
    """Curl с правильным UA. python-requests режется novsu.ru."""
    out = subprocess.run(
        [
            "curl", "-sSL",
            "-A", config.CHROME_UA,
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: ru-RU,ru;q=0.9,en;q=0.8",
            "--compressed",
            url,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return out.stdout


def save_html(html: str, label: str = "6381") -> Path:
    out = STATE_DIR / f"{label}.html"
    out.write_text(html, encoding="utf-8")
    return out


def hash_html(html: str) -> str:
    # Нормализуем — выкидываем нестабильные пробелы
    norm = " ".join(html.split())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def is_stub(html: str) -> bool:
    """Заглушка если на странице только ссылка 'Показать расписание занятий следующих семестров'
    и нет реальной таблицы пар. Используется монитором для отсечки."""
    if "Показать расписание занятий следующих семестров" in html:
        # ещё проверим — может таблица появилась
        # маркер реального расписания: наличие ячеек со временем (08:30 и т.п.)
        import re
        if re.search(r"\b\d{2}:\d{2}\b", html):
            return False
        return True
    return False


if __name__ == "__main__":
    html = fetch_html()
    p = save_html(html)
    print(f"saved {p} ({len(html)} bytes), hash={hash_html(html)}, stub={is_stub(html)}")
