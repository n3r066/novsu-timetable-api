#!/usr/bin/env python3
"""Generate visual before/after comparison screenshots between week 3 and week 4."""

import sys, json, sqlite3
sys.path.insert(0, "/root/novsu-timetable-api")
import config
from fetch import fetch_html
from parse import parse_all, parse_schedule
from monitor import diff_schedules
from post import comparison_day_screens

DB = "state/timetable.sqlite3"
OUT_DIR = config.STATE_DIR / "_week34_diff"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# 1. Load old HTML from snapshot 85 (week 3, fetched 14.09)
conn = sqlite3.connect(DB)
row = conn.execute("SELECT html, parsed_json FROM snapshots WHERE id=85").fetchone()
conn.close()
old_html = row[0]
old_parsed = json.loads(row[1]) if row[1] else parse_all(old_html)
print(f"Old HTML: {len(old_html)} bytes, weeks: {len(old_parsed.get('weeks', []))}", file=sys.stderr)

# 2. Fetch current HTML from portal (week 4)
new_html = fetch_html()
new_parsed = parse_all(new_html)
print(f"New HTML: {len(new_html)} bytes, weeks: {len(new_parsed.get('weeks', []))}", file=sys.stderr)

# 3. Compute diff
old_schedule = old_parsed.get("schedule")
new_schedule = new_parsed.get("schedule")
diff = diff_schedules(old_schedule, new_schedule)
print(f"Diff: +{len(diff.get('added', []))} -{len(diff.get('removed', []))} ~{len(diff.get('changed', []))}", file=sys.stderr)

# 4. Generate comparison screenshots
items = comparison_day_screens(old_html, new_html, diff)
print(f"Screens: {len(items)}", file=sys.stderr)
for item in items:
    label = item.get("label", "?")
    before = item.get("before_path")
    after = item.get("after_path")
    print(f"  {label}: before={'yes' if before else 'no'} after={'yes' if after else 'no'}", file=sys.stderr)
    if before:
        print(f"    before: {before}", file=sys.stderr)
    if after:
        print(f"    after: {after}", file=sys.stderr)

# 5. Save diff JSON for reference
diff_path = OUT_DIR / "diff.json"
diff_path.write_text(json.dumps(diff, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"Diff saved: {diff_path}", file=sys.stderr)
print("DONE", file=sys.stderr)
