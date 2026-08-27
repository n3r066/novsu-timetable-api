"""Stable JSON facade over the server-rendered NovSU timetable."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import config
from fetch import (
    PortalResponseError,
    classify_timetable_html,
    content_fingerprint,
    fetch_html,
)
from parse import parse_all
from portal_parser import count_schedule_lessons, parse_groups, parse_institutes
from schedule_logic import (
    day_view,
    find_week,
    find_week_by_number,
    next_lessons,
    parse_user_date,
    week_view,
)

LOGGER = logging.getLogger(__name__)
PERSIST_LOCK = threading.Lock()
INITIALIZE_LOCK = threading.RLock()
CACHE_LOCK = threading.RLock()

PARSER_VERSION = "3"
ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "state" / "timetable.sqlite3"
DEFAULT_GROUP = config.DEFAULT_GROUP
GROUPS = config.GROUPS
BASE_URL = config.PORTAL_TIMETABLE_BASE_URL
INDEX_URL = config.PORTAL_TIMETABLE_INDEX_URL
TIMETABLE_CACHE_TTL_S = config.TIMETABLE_CACHE_TTL_S
SNAPSHOT_RETENTION_DAYS = config.SNAPSHOT_RETENTION_DAYS

_TIMETABLE_CACHE: dict[str, tuple[float, dict]] = {}


class ParseValidationError(ValueError):
    """Страница портала распарсилась неполно или противоречиво."""


class AmbiguousGroupError(ValueError):
    def __init__(self, groups: list[dict]):
        super().__init__("ambiguous_group")
        self.groups = groups


def _validate_reference_fields(
    group: str | None = None,
    *,
    inst_id: str | None = None,
    year: str | None = None,
    typ: str | None = None,
    allow_partial_group: bool = True,
) -> None:
    if group is not None:
        value = str(group).strip()
        aliases = {"default", "_default", "me"}
        if value not in aliases and not re.fullmatch(r"[0-9A-Za-zА-Яа-яЁё._-]{1,64}", value):
            raise ValueError("group contains unsupported characters")
    if inst_id is not None and (not str(inst_id).strip().isdigit()):
        raise ValueError("inst_id must contain digits")
    if year is not None and not re.fullmatch(r"\d{4}", str(year).strip()):
        raise ValueError("year must be a four-digit enrollment year")
    if typ is not None and (not str(typ).strip() or len(str(typ).strip()) > 20):
        raise ValueError("type is invalid")


def source_url(group: str, cfg: dict | None = None) -> str:
    cfg = cfg or resolve_group(group)
    return config.build_group_url(group, cfg)


def _fetch_resolved(cfg: dict) -> tuple[str, str, dict]:
    url = source_url(cfg["group"], cfg)
    return fetch_html(url), url, cfg


def fetch(
    group: str | None = None,
    *,
    inst_id: str | None = None,
    year: str | None = None,
    typ: str | None = None,
):
    """API использует тот же curl/headers/retry/validator, что и monitor."""
    cfg = resolve_group(group, inst_id=inst_id, year=year, typ=typ)
    return _fetch_resolved(cfg)


def _index_html() -> str:
    return fetch_html(INDEX_URL, page_kind="index")


def list_institutes(html: str | None = None) -> list[dict]:
    return parse_institutes(html if html is not None else _index_html())


def list_groups(inst_id: str | None = None, html: str | None = None) -> list[dict]:
    _validate_reference_fields(inst_id=inst_id)
    source = html if html is not None else _index_html()
    try:
        groups = parse_groups(source, inst_id=inst_id)
    except Exception as exc:  # malformed upstream HTML is not a client error
        raise ParseValidationError(f"unexpected groups parser failure: {exc}") from exc
    return sorted(groups, key=_group_sort_key)


def find_groups(
    query: str,
    *,
    inst_id: str | None = None,
    year: str | None = None,
    typ: str | None = None,
) -> list[dict]:
    query = str(query or "").strip()
    _validate_reference_fields(query or None, inst_id=inst_id, year=year, typ=typ)
    candidates = []
    html = _index_html()
    for item in list_groups(inst_id, html):
        if year and item["year"] != str(year):
            continue
        if typ and item["type"].casefold() != str(typ).casefold():
            continue
        candidates.append(item)
    if not query:
        return sorted(candidates, key=_group_sort_key)
    exact = [item for item in candidates if item["group"] == query]
    if exact:
        return sorted(exact, key=_group_sort_key)
    return sorted((item for item in candidates if query in item["group"]), key=_group_sort_key)


def resolve_group(
    group: str | None = None,
    *,
    inst_id: str | None = None,
    year: str | None = None,
    typ: str | None = None,
) -> dict:
    group = str(group or DEFAULT_GROUP).strip()
    if group in {"default", "_default", "me"}:
        group = DEFAULT_GROUP
    _validate_reference_fields(group, inst_id=inst_id, year=year, typ=typ)

    # Для известной группы дополняем частичные overrides общим конфигом.
    if group in GROUPS:
        ref = {"group": group, **GROUPS[group]}
        if inst_id is not None:
            ref["inst_id"] = str(inst_id)
        if year is not None:
            ref["year"] = str(year)  # год набора, не текущий год
        if typ is not None:
            ref["type"] = str(typ)
        return ref

    # Полная ссылка от клиента не требует отдельного запроса индекса.
    if inst_id is not None and year is not None and typ is not None:
        return {
            "group": group,
            "inst_id": str(inst_id),
            "type": str(typ),
            "year": str(year),
            "institute": None,
        }

    matches = find_groups(group, inst_id=inst_id, year=year, typ=typ)
    if not matches:
        raise LookupError(f"group not found: {group}")
    if len(matches) > 1:
        raise AmbiguousGroupError(matches)
    return matches[0]


def _group_sort_key(item: dict) -> tuple:
    return (
        0 if item.get("group") == DEFAULT_GROUP else 1,
        item.get("institute") or "",
        item.get("year") or "",
        item.get("group") or "",
        item.get("type") or "",
    )


def physical_lesson_count(html: str) -> int:
    return count_schedule_lessons(html)


def validate(data: dict, html: str) -> None:
    """Проверить результат parse_all без превращения настоящего stub в ошибку."""
    schedule = data.get("schedule")
    if data.get("stub") is True:
        try:
            page_kind = classify_timetable_html(html)
        except PortalResponseError as exc:
            raise ParseValidationError(f"invalid portal stub: {exc}") from exc
        if page_kind != "stub" or schedule is not None:
            raise ParseValidationError("stub flag contradicts portal response")
        return

    if not isinstance(schedule, dict) or not isinstance(schedule.get("days"), dict):
        raise ParseValidationError("schedule is missing without a valid portal stub")
    lessons = [lesson for rows in schedule["days"].values() for lesson in rows]
    try:
        expected = physical_lesson_count(html)
    except (TypeError, ValueError) as exc:
        raise ParseValidationError(f"parser invariant failed: {exc}") from exc
    if len(lessons) != expected:
        raise ParseValidationError(
            f"parser invariant failed: physical={expected}, parsed={len(lessons)}"
        )
    for lesson in lessons:
        if not lesson.get("subject") or not lesson.get("time"):
            raise ParseValidationError(
                f"invalid lesson at source row {lesson.get('source_row')}"
            )


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _snapshot_schema_is_current(db: sqlite3.Connection) -> bool:
    columns = {row[1] for row in db.execute("PRAGMA table_info(snapshots)")}
    required = {"content_fingerprint", "raw_hash", "url", "parser_version"}
    if not required.issubset(columns) or "content_hash" in columns:
        return False
    identity = ("url", "content_fingerprint", "parser_version")
    for row in db.execute("PRAGMA index_list(snapshots)"):
        if not row[2]:
            continue
        indexed = tuple(item[2] for item in db.execute(f"PRAGMA index_info('{row[1]}')"))
        if indexed == identity:
            return True
    return False


def _dict_rows(cursor: sqlite3.Cursor) -> list[dict]:
    names = [item[0] for item in cursor.description or []]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


_MIGRATION_SNAPSHOT_SQL = """
CREATE TABLE snapshots_new (
 id INTEGER PRIMARY KEY,
 fetched_at TEXT NOT NULL,
 url TEXT NOT NULL,
 content_fingerprint TEXT NOT NULL,
 raw_hash TEXT NOT NULL,
 html TEXT NOT NULL,
 parser_version TEXT NOT NULL,
 UNIQUE(url, content_fingerprint, parser_version)
)
"""
_MIGRATION_LESSON_SQL = """
CREATE TABLE lessons_new (
 id INTEGER PRIMARY KEY,
 snapshot_id INTEGER NOT NULL REFERENCES snapshots_new(id) ON DELETE CASCADE,
 source_row INTEGER NOT NULL,
 day TEXT NOT NULL,
 time_text TEXT NOT NULL,
 subject_raw TEXT NOT NULL,
 subject TEXT NOT NULL,
 teacher TEXT,
 raw_room TEXT,
 room TEXT,
 raw_comment TEXT,
 location TEXT,
 delivery_mode TEXT NOT NULL,
 link TEXT,
 note TEXT,
 UNIQUE(snapshot_id, source_row)
)
"""
_LESSON_FIELDS = (
    "id", "snapshot_id", "source_row", "day", "time_text", "subject_raw",
    "subject", "teacher", "raw_room", "room", "raw_comment", "location",
    "delivery_mode", "link", "note",
)


def _migrate_legacy_schema(db: sqlite3.Connection) -> None:
    """Migrate legacy raw-hash snapshots, retaining the newest semantic copy."""
    db.commit()
    db.execute("PRAGMA foreign_keys = OFF")
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DROP TABLE IF EXISTS snapshots_new")
        db.execute("DROP TABLE IF EXISTS lessons_new")
        db.execute(_MIGRATION_SNAPSHOT_SQL)
        db.execute(_MIGRATION_LESSON_SQL)

        old_snapshots = _dict_rows(db.execute("SELECT * FROM snapshots ORDER BY id DESC"))
        snapshot_map: dict[int, int] = {}
        for old in old_snapshots:
            html = str(old.get("html") or "")
            raw_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
            semantic = old.get("content_fingerprint")
            if not semantic:
                try:
                    semantic = content_fingerprint(parse_all(html))
                except Exception:  # noqa: BLE001 - сохраняем даже очень старый мусор
                    semantic = hashlib.sha256(("legacy\0" + raw_hash).encode()).hexdigest()[:16]
            values = (
                old.get("id"),
                old.get("fetched_at") or dt.datetime.now(dt.timezone.utc).isoformat(),
                old.get("url") or "legacy://unknown",
                str(semantic),
                raw_hash,
                html,
                str(old.get("parser_version") or "legacy"),
            )
            db.execute(
                """INSERT INTO snapshots_new
                (id,fetched_at,url,content_fingerprint,raw_hash,html,parser_version)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(url,content_fingerprint,parser_version) DO NOTHING""",
                values,
            )
            new_id = db.execute(
                """SELECT id FROM snapshots_new
                WHERE url=? AND content_fingerprint=? AND parser_version=?""",
                (values[2], values[3], values[6]),
            ).fetchone()[0]
            snapshot_map[int(old["id"])] = int(new_id)

        if _table_exists(db, "lessons"):
            old_lessons = _dict_rows(db.execute("SELECT * FROM lessons ORDER BY snapshot_id DESC, id"))
            placeholders = ",".join("?" for _ in _LESSON_FIELDS)
            columns = ",".join(_LESSON_FIELDS)
            for old in old_lessons:
                old_sid = int(old.get("snapshot_id") or 0)
                if old_sid not in snapshot_map:
                    continue
                old["snapshot_id"] = snapshot_map[old_sid]
                old.setdefault("delivery_mode", "in_person")
                values = tuple(old.get(name) for name in _LESSON_FIELDS)
                db.execute(
                    f"INSERT OR IGNORE INTO lessons_new({columns}) VALUES({placeholders})",
                    values,
                )
            db.execute("DROP TABLE lessons")
        db.execute("DROP TABLE snapshots")
        db.execute("ALTER TABLE snapshots_new RENAME TO snapshots")
        db.execute("ALTER TABLE lessons_new RENAME TO lessons")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute("PRAGMA foreign_keys = ON")


def _seed_annotations_once(db: sqlite3.Connection) -> None:
    marker = "annotations_seed_v1"
    if db.execute("SELECT 1 FROM metadata WHERE key=?", (marker,)).fetchone():
        return
    annotations = json.loads(
        (ROOT / "data" / "annotations.json").read_text(encoding="utf-8")
    )
    for item in annotations["glossary"]:
        db.execute(
            """INSERT INTO glossary(term,label,explanation) VALUES(?,?,?)
            ON CONFLICT(term) DO UPDATE SET
              label=excluded.label, explanation=excluded.explanation""",
            (item["term"], item["label"], item.get("explanation")),
        )
    for item in annotations["annotation_patterns"]:
        db.execute(
            """INSERT INTO annotation_rules(id,pattern,meaning) VALUES(?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              pattern=excluded.pattern, meaning=excluded.meaning""",
            (item["id"], item["pattern"], item["meaning"]),
        )
    db.execute("INSERT INTO metadata(key,value) VALUES(?,?)", (marker, "1"))
    db.commit()


def connect(path: str | Path = DB_PATH) -> sqlite3.Connection:
    path_value = str(path)
    if path_value != ":memory:":
        Path(path_value).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path_value, timeout=30)
    db.execute("PRAGMA busy_timeout = 30000")
    db.execute("PRAGMA foreign_keys = ON")
    try:
        with INITIALIZE_LOCK:
            if _table_exists(db, "snapshots") and not _snapshot_schema_is_current(db):
                _migrate_legacy_schema(db)
            db.executescript((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
            _seed_annotations_once(db)
        return db
    except Exception:
        db.close()
        raise


def prune_snapshots(
    db: sqlite3.Connection,
    retention_days: int | None = None,
    *,
    keep_snapshot_id: int | None = None,
    now: dt.datetime | None = None,
) -> int:
    days = SNAPSHOT_RETENTION_DAYS if retention_days is None else int(retention_days)
    if days <= 0:
        return 0
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    cutoff = (current - dt.timedelta(days=days)).isoformat()
    if keep_snapshot_id is None:
        cursor = db.execute("DELETE FROM snapshots WHERE fetched_at < ?", (cutoff,))
    else:
        cursor = db.execute(
            "DELETE FROM snapshots WHERE fetched_at < ? AND id <> ?",
            (cutoff, keep_snapshot_id),
        )
    return max(0, cursor.rowcount)


def persist(db: sqlite3.Connection, html: str, url: str, data: dict) -> int:
    raw_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
    semantic = content_fingerprint(data)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    with PERSIST_LOCK:
        try:
            cursor = db.execute(
                """INSERT INTO snapshots
                (fetched_at,url,content_fingerprint,raw_hash,html,parser_version)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(url,content_fingerprint,parser_version) DO NOTHING""",
                (now, url, semantic, raw_hash, html, PARSER_VERSION),
            )
            row = db.execute(
                """SELECT id FROM snapshots
                WHERE url=? AND content_fingerprint=? AND parser_version=?""",
                (url, semantic, PARSER_VERSION),
            ).fetchone()
            if row is None:
                raise RuntimeError("snapshot insert did not produce an id")
            snapshot_id = int(row[0])

            if cursor.rowcount:
                sql = """INSERT INTO lessons(
                    snapshot_id,source_row,day,time_text,subject_raw,subject,
                    teacher,raw_room,room,raw_comment,location,delivery_mode,
                    link,note
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
                schedule = data.get("schedule") or {}
                for day, rows in (schedule.get("days") or {}).items():
                    for lesson in rows:
                        subject = str(lesson.get("subject") or "")
                        values = (
                            snapshot_id,
                            lesson.get("source_row"),
                            day,
                            lesson.get("time") or "—",
                            lesson.get("subject_raw", subject),
                            subject.split("\n", 1)[0],
                            lesson.get("teacher"),
                            lesson.get("raw_room"),
                            lesson.get("room"),
                            lesson.get("raw_comment"),
                            lesson.get("location"),
                            lesson.get("delivery_mode", "in_person"),
                            lesson.get("link"),
                            lesson.get("note"),
                        )
                        db.execute(sql, values)
            prune_snapshots(db, keep_snapshot_id=snapshot_id)
            db.commit()
            return snapshot_id
        except Exception:
            db.rollback()
            raise


def clear_timetable_cache() -> None:
    with CACHE_LOCK:
        _TIMETABLE_CACHE.clear()


def _load_timetable(cfg: dict) -> dict:
    html, url, _ = fetch(
        cfg["group"], inst_id=cfg.get("inst_id"), year=cfg.get("year"),
        typ=cfg.get("type"),
    )
    try:
        data = parse_all(html)
        validate(data, html)
    except ParseValidationError:
        raise
    except Exception as exc:  # parser bugs/upstream shape are never client 400
        raise ParseValidationError(f"unexpected parser failure: {exc}") from exc
    with connect() as db:
        snapshot_id = persist(db, html, url, data)
    return {
        "schema_version": 1,
        "group": cfg["group"],
        "group_ref": cfg,
        "source_url": url,
        "snapshot_id": snapshot_id,
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        **data,
    }


def get_timetable(
    group: str | None = None,
    *,
    inst_id: str | None = None,
    year: str | None = None,
    typ: str | None = None,
    cache_ttl: float | None = None,
) -> dict:
    cfg = resolve_group(group, inst_id=inst_id, year=year, typ=typ)
    url = source_url(cfg["group"], cfg)
    ttl = TIMETABLE_CACHE_TTL_S if cache_ttl is None else max(0.0, float(cache_ttl))
    if ttl <= 0:
        return _load_timetable(cfg)

    with CACHE_LOCK:
        current = time.monotonic()
        cached = _TIMETABLE_CACHE.get(url)
        if cached and cached[0] > current:
            return cached[1]
        # Lock stays held during refresh to prevent a thundering herd and
        # duplicate DB writes from ThreadingHTTPServer.
        result = _load_timetable(cfg)
        _TIMETABLE_CACHE[url] = (time.monotonic() + ttl, result)
        for key, (expires, _) in list(_TIMETABLE_CACHE.items()):
            if key != url and expires <= current:
                _TIMETABLE_CACHE.pop(key, None)
        return result


def _query_one(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    if not values:
        return None
    if len(values) != 1 or values[0] == "":
        raise ValueError(f"{name} must be specified once and be non-empty")
    return values[0]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if path == "/health":
            if query:
                return self.send_json(400, {"error": "bad_request"})
            return self.send_json(200, {"ok": True})
        if path == "/v1/institutes":
            if query:
                return self.send_json(400, {"error": "bad_request"})
            try:
                return self.send_json(200, {"institutes": list_institutes()})
            except Exception:
                LOGGER.exception("institutes request failed")
                return self.send_json(502, {"error": "upstream_or_parse_failure"})
        if path == "/v1/groups":
            return self._groups(query)

        prefix = "/v1/timetables/"
        if not path.startswith(prefix):
            return self.send_json(404, {"error": "not_found"})
        rest = [urllib.parse.unquote(part) for part in path[len(prefix):].split("/") if part]
        if len(rest) > 2:
            return self.send_json(404, {"error": "not_found"})
        group = rest[0] if rest else DEFAULT_GROUP
        action = rest[1] if len(rest) > 1 else "full"
        if action not in {"full", "day", "week", "next"}:
            return self.send_json(404, {"error": "not_found"})

        try:
            global_names = {"inst_id", "year", "type"}
            action_names = {
                "full": set(),
                "day": {"date"},
                "week": {"date", "week"},
                "next": {"date", "time", "limit"},
            }[action]
            unknown = set(query) - global_names - action_names
            if unknown:
                raise ValueError("unknown query parameter: " + sorted(unknown)[0])
            inst_id = _query_one(query, "inst_id")
            year = _query_one(query, "year")
            typ = _query_one(query, "type")
            _validate_reference_fields(group, inst_id=inst_id, year=year, typ=typ)

            target = None
            week_number = None
            start_time = None
            limit = 3
            if action in {"day", "week", "next"}:
                date_value = _query_one(query, "date")
                target = parse_user_date(date_value)
            if action == "week" and _query_one(query, "week") is not None:
                if _query_one(query, "date") is not None:
                    raise ValueError("use either week or date, not both")
                week_number = int(_query_one(query, "week"))
                if week_number <= 0:
                    raise ValueError("week must be positive")
            if action == "next":
                start_time = _query_one(query, "time")
                if start_time is not None:
                    try:
                        dt.datetime.strptime(start_time, "%H:%M")
                    except ValueError as exc:
                        raise ValueError("time must be HH:MM") from exc
                limit_value = _query_one(query, "limit")
                if limit_value is not None:
                    limit = int(limit_value)
                    if not 1 <= limit <= 20:
                        raise ValueError("limit must be between 1 and 20")

            data = get_timetable(group, inst_id=inst_id, year=year, typ=typ)
            if data.get("stub"):
                return self.send_json(200, data)
            if action == "full":
                return self.send_json(200, data)
            if action == "day":
                return self.send_json(
                    200,
                    day_view(
                        data.get("schedule"), data.get("weeks", []), target,
                        group=data["group"], source_url=data["source_url"],
                    ),
                )
            if action == "week":
                if week_number is not None:
                    week = find_week_by_number(data.get("weeks", []), week_number)
                else:
                    week = find_week(data.get("weeks", []), target)
                if not week:
                    return self.send_json(404, {"error": "week_not_found"})
                return self.send_json(
                    200,
                    week_view(
                        data.get("schedule"), data.get("weeks", []), week,
                        group=data["group"], source_url=data["source_url"],
                    ),
                )
            return self.send_json(
                200,
                next_lessons(
                    data.get("schedule"), data.get("weeks", []), target,
                    start_time=start_time, group=data["group"],
                    source_url=data["source_url"], limit=limit,
                ),
            )
        except ParseValidationError:
            LOGGER.exception("timetable parse validation failed for group %s", group)
            return self.send_json(502, {"error": "upstream_or_parse_failure"})
        except AmbiguousGroupError as exc:
            return self.send_json(409, {"error": "ambiguous_group", "groups": exc.groups})
        except LookupError as exc:
            return self.send_json(404, {"error": "unknown_group", "message": str(exc)})
        except (ValueError, TypeError, OverflowError) as exc:
            return self.send_json(400, {"error": "bad_request", "message": str(exc)})
        except Exception:
            LOGGER.exception("timetable request failed for group %s", group)
            return self.send_json(502, {"error": "upstream_or_parse_failure"})

    def _groups(self, query: dict[str, list[str]]):
        try:
            allowed = {"inst_id", "q", "group", "year", "type"}
            unknown = set(query) - allowed
            if unknown:
                raise ValueError("unknown query parameter: " + sorted(unknown)[0])
            if "q" in query and "group" in query:
                raise ValueError("use either q or group, not both")
            inst_id = _query_one(query, "inst_id")
            q = _query_one(query, "q") or _query_one(query, "group")
            year = _query_one(query, "year")
            typ = _query_one(query, "type")
            _validate_reference_fields(q, inst_id=inst_id, year=year, typ=typ)
            groups = (
                find_groups(q, inst_id=inst_id, year=year, typ=typ)
                if q else list_groups(inst_id)
            )
            return self.send_json(200, {"groups": groups})
        except ParseValidationError:
            LOGGER.exception("groups parse validation failed")
            return self.send_json(502, {"error": "upstream_or_parse_failure"})
        except (ValueError, TypeError) as exc:
            return self.send_json(400, {"error": "bad_request", "message": str(exc)})
        except Exception:
            LOGGER.exception("groups request failed")
            return self.send_json(502, {"error": "upstream_or_parse_failure"})

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--group", default=DEFAULT_GROUP)
    args = parser.parse_args()
    if args.once:
        print(json.dumps(get_timetable(args.group), ensure_ascii=False, indent=2))
        return
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
