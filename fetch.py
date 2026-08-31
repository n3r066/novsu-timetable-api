"""Единая загрузка страниц портала НовГУ для API и monitor."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Mapping
from pathlib import Path

from bs4 import BeautifulSoup

import config
from portal_parser import find_schedule_table, parse_groups

STATE_DIR = config.STATE_DIR
FETCH_RETRIES = config.FETCH_RETRIES
RETRY_DELAY_S = config.FETCH_RETRY_DELAY_S


class PortalResponseError(RuntimeError):
    """HTTP был успешным, но тело не похоже на ожидаемую страницу портала."""


def _curl(url: str, ua: str, headers: dict[str, str] | None = None) -> str:
    command = ["curl", "-sSL", "--fail", "-A", ua]
    for name, value in (headers or config.chromium_headers(ua)).items():
        if name.casefold() != "user-agent":
            command.extend(["-H", f"{name}: {value}"])
    command.extend(["--compressed", url])
    out = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return out.stdout


def classify_timetable_html(html: str) -> str:
    """Вернуть ``schedule`` или ``stub``; прочий HTTP-200 HTML отклонить.

    Заглушка определяется по структурной ссылке портала на будущие семестры.
    Одного отсутствия таблицы недостаточно: так WAF/error-страница не станет
    ложной заглушкой.
    """
    if not isinstance(html, str) or not html.strip():
        raise PortalResponseError("empty response body")
    soup = BeautifulSoup(html, "html.parser")
    if find_schedule_table(soup) is not None:
        return "schedule"

    for link in soup.find_all("a", href=True):
        href = str(link.get("href") or "")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
        page = (query.get("page") or [""])[0]
        mode = (query.get("modeTime") or [""])[0]
        label = " ".join(link.stripped_strings).casefold()
        if (
            page == "EditViewGroup"
            and mode.casefold() == "futuretimetable"
            and "расписани" in label
        ):
            return "stub"

    raise PortalResponseError("HTTP 200 body is neither a timetable nor a portal stub")


def validate_portal_response(html: str, page_kind: str = "timetable") -> str:
    """Структурно проверить страницу и вернуть её тип."""
    if page_kind == "timetable":
        return classify_timetable_html(html)
    if page_kind == "index":
        if not isinstance(html, str) or not html.strip():
            raise PortalResponseError("empty response body")
        if parse_groups(html):
            return "index"
        raise PortalResponseError("HTTP 200 body is not a timetable index")
    raise ValueError(f"unknown portal page kind: {page_kind}")


def fetch_html(
    url: str = config.GROUP_URL,
    retries: int = FETCH_RETRIES,
    *,
    page_kind: str = "timetable",
) -> str:
    """Загрузить и структурно проверить страницу с ретраями и полными headers."""
    attempts = max(1, int(retries))
    last_exc: Exception | None = None
    ua, headers = config.pinned_profile()
    for attempt in range(1, attempts + 1):
        try:
            html = _curl(url, ua, headers)
            validate_portal_response(html, page_kind)
            return html
        except Exception as exc:  # noqa: BLE001 - сеть, curl и структура ретраятся одинаково
            last_exc = exc
            print(
                f"[fetch attempt {attempt}/{attempts} FAIL] ua={ua!r}: {exc}",
                file=sys.stderr,
            )
        if attempt < attempts:
            time.sleep(RETRY_DELAY_S)
    raise RuntimeError(f"fetch_html failed after {attempts} attempts: {last_exc}") from last_exc


def save_html(html: str, label: str = "6381") -> Path:
    out = STATE_DIR / f"{label}.html"
    out.write_text(html, encoding="utf-8")
    return out


def hash_html(html: str) -> str:
    # Мониторинговый hash сохраняет прежнюю нормализацию для совместимости.
    norm = " ".join(html.split())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def content_fingerprint(source: str | Mapping) -> str:
    """Семантический отпечаток бизнес-полей результата ``parse_all``.

    Можно передать исходный HTML (старый API) или уже готовый результат
    ``parse_all``. DOM-номера, raw-поля и порядок строк не влияют на hash.
    """
    if isinstance(source, str):
        from parse import parse_all

        data = parse_all(source)
    elif isinstance(source, Mapping):
        data = source
    else:
        raise TypeError("content_fingerprint expects HTML or parse_all mapping")

    lesson_fields = (
        "subject", "time", "subgroup", "teacher", "room", "note",
        "location", "delivery_mode", "link",
    )
    schedule = data.get("schedule")
    canonical_days = None
    if isinstance(schedule, Mapping):
        days = schedule.get("days")
        if isinstance(days, Mapping):
            canonical_days = []
            for day, rows in days.items():
                lessons = [
                    {field: lesson.get(field) for field in lesson_fields}
                    for lesson in (rows or [])
                    if isinstance(lesson, Mapping)
                ]
                lessons.sort(
                    key=lambda item: json.dumps(
                        item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                )
                canonical_days.append({"day": str(day), "lessons": lessons})
            canonical_days.sort(key=lambda item: item["day"])

    weeks = []
    for week in data.get("weeks") or []:
        if not isinstance(week, Mapping):
            continue
        weeks.append(
            {field: week.get(field) for field in ("week", "half", "start", "end")}
        )
    weeks.sort(
        key=lambda item: json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    )

    canon = json.dumps(
        {
            "stub": bool(data.get("stub")),
            "weeks": weeks,
            "schedule": canonical_days,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()

def is_stub(html: str) -> bool:
    """True только для настоящей структурной заглушки портала."""
    return classify_timetable_html(html) == "stub"


if __name__ == "__main__":
    html = fetch_html()
    path = save_html(html)
    print(f"saved {path} ({len(html)} bytes), hash={hash_html(html)}, stub={is_stub(html)}")
