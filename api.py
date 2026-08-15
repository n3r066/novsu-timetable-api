"""Stable JSON facade over the server-rendered NovSU timetable."""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import logging
import sqlite3
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from bs4 import BeautifulSoup
from parse import DAY_FULL, _find_schedule_table, parse_all

LOGGER = logging.getLogger(__name__)
PERSIST_LOCK = threading.Lock()

PARSER_VERSION = "2"
ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "state" / "timetable.sqlite3"
GROUPS = {"5234": {"inst_id": "868344", "type": "ДО", "year": "2025"}}
BASE_URL = "https://portal.novsu.ru/univer/timetable/ochn/i.1103357/"


def source_url(group):
    cfg = GROUPS[group]
    query = urllib.parse.urlencode({"page": "EditViewGroup", "instId": cfg["inst_id"], "name": group, "type": cfg["type"], "year": cfg["year"]})
    return BASE_URL + "?" + query


def fetch(group):
    url = source_url(group)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 Chrome/127", "Accept-Language": "ru-RU,ru;q=0.9"})
    with urllib.request.urlopen(req, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"NovSU returned HTTP {response.status}")
        return response.read().decode("utf-8"), url


def physical_lesson_count(html):
    table = _find_schedule_table(BeautifulSoup(html, "html.parser"))
    if table is None:
        raise ValueError("schedule table not found")
    count = 0
    current_day = False
    inherited = 0
    for tr in table.find_all("tr", recursive=False)[1:]:
        tags = tr.find_all(["td", "th"], recursive=False)
        cells = [" ".join(c.stripped_strings).strip() for c in tags]
        if len(cells) == 1 and cells[0] in DAY_FULL:
            current_day = True
            inherited = 0
            continue
        if not current_day:
            continue
        if len(cells) == 6:
            inherited = max(0, int(tags[0].get("rowspan", 1) or 1) - 1)
            subject = cells[2]
        elif len(cells) == 5 and inherited:
            inherited -= 1
            subject = cells[1]
        else:
            if any(cells):
                raise ValueError(f"unrecognized schedule row shape: {len(cells)} cells")
            continue
        count += bool(subject)
    return count


def validate(data, html):
    schedule = data.get("schedule")
    if not isinstance(schedule, dict) or not isinstance(schedule.get("days"), dict):
        raise ValueError("schedule is missing or is a stub response")
    lessons = [lesson for rows in schedule["days"].values() for lesson in rows]
    expected = physical_lesson_count(html)
    if len(lessons) != expected:
        raise ValueError(f"parser invariant failed: physical={expected}, parsed={len(lessons)}")
    for lesson in lessons:
        if not lesson.get("subject") or not lesson.get("time"):
            raise ValueError(f"invalid lesson at source row {lesson.get('source_row')}")


def connect(path=DB_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.execute("PRAGMA busy_timeout = 30000")
    db.executescript((ROOT / "db" / "schema.sql").read_text())
    annotations = json.loads((ROOT / "data" / "annotations.json").read_text())
    for item in annotations["glossary"]:
        db.execute("INSERT OR REPLACE INTO glossary(term,label,explanation) VALUES(?,?,?)", (item["term"], item["label"], item.get("explanation")))
    for item in annotations["annotation_patterns"]:
        db.execute("INSERT OR REPLACE INTO annotation_rules(id,pattern,meaning) VALUES(?,?,?)", (item["id"], item["pattern"], item["meaning"]))
    db.commit()
    return db


def persist(db, html, url, data):
    digest = hashlib.sha256(html.encode()).hexdigest()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    with PERSIST_LOCK:
        db.execute("INSERT OR IGNORE INTO snapshots(fetched_at,url,content_hash,html,parser_version) VALUES(?,?,?,?,?)", (now, url, digest, html, PARSER_VERSION))
        sid = db.execute("SELECT id FROM snapshots WHERE content_hash=?", (digest,)).fetchone()[0]
        sql = "INSERT OR REPLACE INTO lessons(snapshot_id,source_row,day,time_text,subject_raw,subject,teacher,raw_room,room,raw_comment,location,delivery_mode,link,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        for day, rows in data["schedule"]["days"].items():
            for lesson in rows:
                values = (sid, lesson["source_row"], day, lesson["time"], lesson.get("subject_raw", lesson["subject"]), lesson["subject"].split("\n", 1)[0], lesson["teacher"], lesson.get("raw_room"), lesson["room"], lesson.get("raw_comment"), lesson.get("location"), lesson.get("delivery_mode", "in_person"), lesson.get("link"), lesson.get("note"))
                db.execute(sql, values)
        db.commit()
    return sid


def get_timetable(group):
    html, url = fetch(group)
    data = parse_all(html)
    validate(data, html)
    with connect() as db:
        snapshot_id = persist(db, html, url, data)
    return {"schema_version": 1, "group": group, "source_url": url, "snapshot_id": snapshot_id, "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(), **data}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/health":
            return self.send_json(200, {"ok": True})
        prefix = "/v1/timetables/"
        if not path.startswith(prefix):
            return self.send_json(404, {"error": "not_found"})
        group = path[len(prefix):]
        if group not in GROUPS:
            return self.send_json(404, {"error": "unknown_group"})
        try:
            self.send_json(200, get_timetable(group))
        except Exception:
            LOGGER.exception("timetable request failed for group %s", group)
            self.send_json(502, {"error": "upstream_or_parse_failure"})

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
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    if args.once:
        print(json.dumps(get_timetable("5234"), ensure_ascii=False, indent=2))
        return
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
