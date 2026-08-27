# Общий конфиг фетчера, API и Telegram-бота.

from __future__ import annotations

import os
import pathlib
import random
import re
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
STATE_DIR.mkdir(exist_ok=True)

# Загружаем .env, если он есть. Fetch и API не требуют Telegram-секретов.
_env_file = ROOT / ".env"
if _env_file.exists():
    for line in _env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())

PORTAL_TIMETABLE_BASE_URL = "https://portal.novsu.ru/univer/timetable/ochn/i.1103357/"
PORTAL_TIMETABLE_INDEX_URL = "https://portal.novsu.ru/univer/timetable/ochn/"
DEFAULT_GROUP = os.environ.get("NOVSU_DEFAULT_GROUP", "6381")

# `year` в запросах портала — год набора группы, а не текущий календарный год.
GROUPS = {
    "6381": {
        "inst_id": os.environ.get("NOVSU_DEFAULT_INST_ID", "2244947"),
        "type": os.environ.get("NOVSU_DEFAULT_TYPE", "ДО"),
        "year": os.environ.get("NOVSU_DEFAULT_YEAR", "2026"),
        "institute": "ИЭ",
    },
    "5234": {"inst_id": "868344", "type": "ДО", "year": "2025", "institute": "ИГУМ"},
}


def build_group_url(
    group: str,
    group_ref: dict | None = None,
    *,
    inst_id: str | None = None,
    year: str | None = None,
    typ: str | None = None,
) -> str:
    """Собрать канонический URL группы.

    `year` всегда означает год набора. Функция намеренно не подставляет
    текущий год.
    """
    ref = dict(group_ref or {})
    group = str(ref.get("group") or group).strip()
    inst_id = str(inst_id or ref.get("inst_id") or "").strip()
    admission_year = str(year or ref.get("year") or "").strip()
    typ = str(typ or ref.get("type") or "").strip()
    if not group or not inst_id or not admission_year or not typ:
        raise ValueError("group, inst_id, type and enrollment year are required")
    query = urllib.parse.urlencode(
        [
            ("page", "EditViewGroup"),
            ("instId", inst_id),
            ("name", group),
            ("type", typ),
            ("year", admission_year),
        ]
    )
    return PORTAL_TIMETABLE_BASE_URL + "?" + query


_DEFAULT_GROUP_REF = GROUPS.get(DEFAULT_GROUP) or {
    "inst_id": os.environ.get("NOVSU_DEFAULT_INST_ID", "2244947"),
    "type": os.environ.get("NOVSU_DEFAULT_TYPE", "ДО"),
    "year": os.environ.get("NOVSU_DEFAULT_YEAR", "2026"),
    "institute": None,
}
GROUPS.setdefault(DEFAULT_GROUP, _DEFAULT_GROUP_REF)
GROUP_URL = build_group_url(DEFAULT_GROUP, {"group": DEFAULT_GROUP, **_DEFAULT_GROUP_REF})

# Значения нужны только функциям Telegram. Их отсутствие не ломает fetch/API.
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")


def _optional_int(name: str) -> int | None:
    try:
        value = os.environ.get(name)
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


TG_CHANNEL_ID = _optional_int("TG_CHANNEL_ID")
TG_DM_TARGET = _optional_int("TG_DM_TARGET")

# Только полные Chromium UA: портал отсекает короткие python-UA.
_FALLBACK_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36 Edg/127.0.0.0",
]
CHROME_UA = _FALLBACK_UA_POOL[0]  # обратная совместимость для screenshot.py

try:
    from fake_useragent import UserAgent as _UserAgent

    _ua_source = _UserAgent(
        browsers=["Chrome", "Edge"],
        os=["Windows", "Mac OS X", "Linux"],
        min_version=120.0,
    )
except Exception:  # noqa: BLE001
    _ua_source = None


def random_ua() -> str:
    """Вернуть полный Chromium UA; функция никогда не бросает исключение."""
    if _ua_source is not None:
        try:
            ua = str(_ua_source.random).strip()
            if ua.startswith("Mozilla/5.0") and ("Chrome/" in ua or "Edg/" in ua):
                return ua
        except Exception:  # noqa: BLE001
            pass
    return random.choice(_FALLBACK_UA_POOL)


def chromium_headers(ua: str | None = None) -> dict[str, str]:
    """Полный единый набор заголовков навигации Chromium."""
    ua = ua or random_ua()
    version_match = re.search(r"(?:Chrome|Edg)/(\d+)", ua)
    major = version_match.group(1) if version_match else "127"
    return {
        "User-Agent": ua,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "max-age=0",
        "Sec-CH-UA": f'"Chromium";v="{major}", "Not=A?Brand";v="99"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"' if "Windows" in ua else ('"macOS"' if "Macintosh" in ua else '"Linux"'),
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


FETCH_RETRIES = max(1, int(os.environ.get("NOVSU_FETCH_RETRIES", "3")))
FETCH_RETRY_DELAY_S = max(0.0, float(os.environ.get("NOVSU_FETCH_RETRY_DELAY", "5")))
TIMETABLE_CACHE_TTL_S = max(0.0, float(os.environ.get("NOVSU_CACHE_TTL", "60")))
SNAPSHOT_RETENTION_DAYS = max(0, int(os.environ.get("NOVSU_SNAPSHOT_RETENTION_DAYS", "90")))

# Системный chromium (snap)
CHROMIUM_BIN = "/usr/bin/chromium-browser"
