#!/usr/bin/env python3
"""Refresh teacher_names.json cache from the NovSU portal teacher directory.

Scrapes the full teacher list (allTeachersTimetable page), keeps only
unambiguous last-name → full-name mappings, and merges with the existing cache.
Run monthly or when new teachers appear.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent
CACHE_PATH = BASE / "state" / "teacher_names.json"
PORTAL_URL = (
    "https://portal.novsu.ru/univer/timetable/ochn/i.1103357/"
    "?page=allTeachersTimetable"
)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,*/*",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


def fetch_teachers() -> tuple[list[str], dict[str, str]]:
    """Returns (names, teacher_id_map) where teacher_id_map is {teacherId: name}."""
    import re
    req = urllib.request.Request(PORTAL_URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    names: list[str] = []
    id_map: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"teacherId=((?:ora|ext)_\d+)", href)
        if not m:
            continue
        text = a.get_text(strip=True)
        parts = text.split()
        if len(parts) >= 2:
            names.append(text)
            id_map[m.group(1)] = text
    return names, id_map


def build_unique_lookup(names: list[str]) -> dict[str, str]:
    by_last: dict[str, set[str]] = defaultdict(set)
    for full_name in names:
        last = full_name.split()[0]
        by_last[last].add(full_name)
    return {last: fulls.pop() for last, fulls in by_last.items() if len(fulls) == 1}


def main() -> int:
    print("Fetching teacher directory from portal...")
    names, id_map = fetch_teachers()
    print(f"  Found {len(names)} teachers, {len(id_map)} with IDs")

    portal_lookup = build_unique_lookup(names)
    print(f"  Unique last names: {len(portal_lookup)}")

    # --- Update teacher_names.json (last_name -> full_name) ---
    try:
        existing = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        existing = {}
    print(f"  Existing name cache: {len(existing)} entries")

    merged = dict(existing)
    added = 0
    for last, full in portal_lookup.items():
        if last not in merged:
            merged[last] = full
            added += 1

    print(f"  Added from portal: {added}")
    print(f"  Total name cache: {len(merged)} entries")
    CACHE_PATH.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # --- Update teacher_ids.json (teacherId -> full_name) ---
    id_cache_path = CACHE_PATH.parent / "teacher_ids.json"
    try:
        existing_ids = json.loads(id_cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        existing_ids = {}

    merged_ids = {**existing_ids, **id_map}
    id_cache_path.write_text(
        json.dumps(merged_ids, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"  Total ID cache: {len(merged_ids)} entries")
    print(f"✅ Saved to {CACHE_PATH} and {id_cache_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
