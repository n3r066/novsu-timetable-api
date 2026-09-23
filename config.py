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

# Загружаем .env, если он есть и читается. Fetch и API не требуют Telegram-секретов.
# Под systemd переменные уже приходят через EnvironmentFile=, а сам .env принадлежит
# root и недоступен сервисному пользователю — это штатно, а не ошибка.
_env_file = ROOT / ".env"
try:
    _env_lines = _env_file.read_text(encoding="utf-8").splitlines()
except OSError:
    _env_lines = []
if _env_lines:
    for line in _env_lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())

TIMETABLE_ROUTES = {
    "ochn": {
        "index_url": "https://portal.novsu.ru/univer/timetable/ochn/",
        "base_url": "https://portal.novsu.ru/univer/timetable/ochn/i.1103357/",
    },
    "zaochn": {
        "index_url": "https://portal.novsu.ru/univer/timetable/zaochn/",
        "base_url": "https://portal.novsu.ru/univer/timetable/zaochn/i.1103358/",
    },
    "session": {
        "index_url": "https://portal.novsu.ru/univer/timetable/session/",
        "base_url": "https://portal.novsu.ru/univer/timetable/ochn/i.1103357/",
    },
}

# Backward-compatible aliases for the ochn route.
PORTAL_TIMETABLE_BASE_URL = TIMETABLE_ROUTES["ochn"]["base_url"]
PORTAL_TIMETABLE_INDEX_URL = TIMETABLE_ROUTES["ochn"]["index_url"]
DEFAULT_GROUP = os.environ.get("NOVSU_DEFAULT_GROUP", "6381")


def get_route_base_url(route: str) -> str:
    """Return the base URL for a named timetable route."""
    try:
        return TIMETABLE_ROUTES[route]["base_url"]
    except KeyError:
        raise ValueError(f"unknown timetable route: {route}") from None


def get_route_index_url(route: str) -> str:
    """Return the index URL for a named timetable route."""
    try:
        return TIMETABLE_ROUTES[route]["index_url"]
    except KeyError:
        raise ValueError(f"unknown timetable route: {route}") from None

# `year` в запросах портала — год набора группы, а не текущий календарный год.
GROUPS = {
    "6381": {
        "inst_id": os.environ.get("NOVSU_DEFAULT_INST_ID", "2244947"),
        "type": os.environ.get("NOVSU_DEFAULT_TYPE", "ДО"),
        "year": os.environ.get("NOVSU_DEFAULT_YEAR", "2026"),
        "institute": "ИЭ",
        "route": "ochn",
    },
    "5234": {
        "inst_id": "868344",
        "type": "ДО",
        "year": "2025",
        "institute": "ИГУМ",
        "route": "ochn",
    },
}


def build_group_url(
    group: str,
    group_ref: dict | None = None,
    *,
    inst_id: str | None = None,
    year: str | None = None,
    typ: str | None = None,
    route: str | None = None,
) -> str:
    """Собрать канонический URL группы.

    `year` всегда означает год набора. Функция намеренно не подставляет
    текущий год. `route` выбирает base_url из TIMETABLE_ROUTES; если не
    указан, используется route из group_ref или "ochn".
    """
    ref = dict(group_ref or {})
    group = str(ref.get("group") or group).strip()
    inst_id = str(inst_id or ref.get("inst_id") or "").strip()
    admission_year = str(year or ref.get("year") or "").strip()
    typ = str(typ or ref.get("type") or "").strip()
    route = str(route or ref.get("route") or "ochn").strip()
    if not group or not inst_id or not admission_year or not typ:
        raise ValueError("group, inst_id, type and enrollment year are required")
    base_url = get_route_base_url(route)
    query = urllib.parse.urlencode(
        [
            ("page", "EditViewGroup"),
            ("instId", inst_id),
            ("name", group),
            ("type", typ),
            ("year", admission_year),
        ]
    )
    return base_url + "?" + query


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

# Чат группы для эфемерных команд group_bot (Bot API id, например -100...).
TG_GROUP_CHAT_ID = _optional_int("NOVSU_GROUP_CHAT_ID")

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


# iPhone 11 (iOS 17.6, Safari). Портал отдаёт мобильному UA то же расписание,
# а стабильная идентичность «живого устройства» выглядит естественнее, чем
# ротация десктопных Chrome каждые 10 секунд.
IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1"
)


def iphone_headers(ua: str | None = None) -> dict[str, str]:
    """Реалистичный набор заголовков навигации Safari на iPhone (без Sec-CH-*)."""
    return {
        "User-Agent": ua or IPHONE_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Cache-Control": "max-age=0",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


_PINNED_PROFILE: tuple[str, dict[str, str]] | None = None


def pinned_profile() -> tuple[str, dict[str, str]]:
    """Одна согласованная браузерная идентичность на процесс.

    Настоящий пользователь не меняет UA каждые 10 секунд, поэтому профиль
    выбирается один раз и переиспользуется во всех попытках и ретраях.
    """
    global _PINNED_PROFILE
    if _PINNED_PROFILE is None:
        _PINNED_PROFILE = (IPHONE_UA, iphone_headers(IPHONE_UA))
    return _PINNED_PROFILE


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
MONITOR_SNAPSHOT_MAX_AGE_S = max(0.0, float(os.environ.get("NOVSU_MONITOR_SNAPSHOT_MAX_AGE", "600")))
# API обслуживает только группу по умолчанию (6381): чужие группы, поиск групп и
# институтов отдают 404. Снять ограничение: NOVSU_API_DEFAULT_GROUP_ONLY=0.
API_DEFAULT_GROUP_ONLY = os.environ.get("NOVSU_API_DEFAULT_GROUP_ONLY", "1").strip() not in ("0", "false", "False", "")
# Кеш готовых ответов API (day/week/next): максимум сутки, сбрасывается новым снимком.
RESPONSE_CACHE_TTL_S = max(0.0, float(os.environ.get("NOVSU_RESPONSE_CACHE_TTL", str(24 * 3600))))

# Headless-браузер для скриншотов. Предпочитаем обычный google-chrome:
# snap-обёртка chromium-browser требует смены AppArmor-профиля и не работает
# под NoNewPrivileges= и отдельным пользователем в systemd.
_BROWSER_CANDIDATES = ("/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser")
CHROMIUM_BIN = os.environ.get("NOVSU_CHROMIUM_BIN") or next(
    (path for path in _BROWSER_CANDIDATES if os.path.exists(path)), _BROWSER_CANDIDATES[-1]
)
# Chrome требует записываемый HOME (crashpad, профиль). Держим его внутри state/,
# потому что только state/ доступен на запись сервису с ProtectSystem=strict.
BROWSER_HOME = STATE_DIR / ".chrome-home"

# --- ОПД («Основы проектной деятельности»): виртуальные группы 1 курса ---
# Два открытых Google-файла кафедры: таблица ВГ (ФИО → номер ВГ → группа) и
# расписание ВГ (ВГ → даты, блок 14:00/16:00, аудитория, преподаватель).
# Раздел в закрепе выключается NOVSU_OPD_ENABLED=0.
OPD_ENABLED = os.environ.get("NOVSU_OPD_ENABLED", "1").strip() not in ("0", "false", "False", "")
OPD_SHEET_ID = os.environ.get("NOVSU_OPD_SHEET_ID", "1j-bUhR7bM8y1zL-5emm86sD9V9aErs6Ek8oE9DSn9rA")
OPD_DOC_ID = os.environ.get("NOVSU_OPD_DOC_ID", "1SjO4xHcz-I34hYb4RHUTYsVXdiz96iUcZAas_J3fXFQ")
OPD_SHEET_CSV_URL = f"https://docs.google.com/spreadsheets/d/{OPD_SHEET_ID}/export?format=csv"
OPD_SHEET_VIEW_URL = f"https://docs.google.com/spreadsheets/d/{OPD_SHEET_ID}/"
# Только HTML-экспорт: в txt подколонки 14:00/16:00 склеиваются (см. opd.py).
OPD_DOC_HTML_URL = f"https://docs.google.com/document/d/{OPD_DOC_ID}/export?format=html"
OPD_DOC_VIEW_URL = f"https://docs.google.com/document/d/{OPD_DOC_ID}/"
OPD_ANNOUNCEMENT_URL = os.environ.get(
    "NOVSU_OPD_ANNOUNCEMENT_URL",
    "https://portal.novsu.ru/study/newUniversity/i.1531736/?id=1651915",
)
# Кэш state/opd_cache.json: обновление раз в 6 часов, после отказа Google —
# повтор не раньше чем через 30 минут, а не каждый трёхминутный цикл.
OPD_CACHE_TTL_S = max(0.0, float(os.environ.get("NOVSU_OPD_CACHE_TTL", str(6 * 3600))))
OPD_RETRY_DELAY_S = max(0.0, float(os.environ.get("NOVSU_OPD_RETRY_DELAY", str(30 * 60))))
