# NovSU Timetable API Facade

Private service for reading the public server-rendered NovSU timetable and exposing a validated JSON contract. The NovSU page does not expose a timetable JSON endpoint: the source is a normal `GET` returning HTML.

## API

```text
GET /health
GET /v1/timetables/5234
```

The API fetches the official portal page, parses the timetable, validates that no lesson rows disappeared, persists the raw snapshot and normalized lessons in SQLite, and returns JSON. On upstream or parser invariant failure it returns `502` instead of publishing partial data.

## Run

```bash
python3 -m pytest -q
python3 api.py --once
python3 api.py --host 127.0.0.1 --port 8787
```

The source URL is configured in `api.py` and points to group 5234, education type `ДО`, institute `868344`, year `2025`.

## Data model

`data/annotations.json` is the versioned glossary/rule source. `db/schema.sql` stores raw snapshots, normalized lessons, and interpretation metadata. Raw room/comment values are retained; `room`, `location`, `delivery_mode`, and `link` are derived separately.

The parser treats NovSU day separator rows as authoritative because the portal currently emits stale day `rowspan` values. Lesson time `rowspan` is inherited only when the source explicitly indicates it.

## Response and failure contract

A successful timetable response has `schema_version`, `group`, `source_url`, `snapshot_id`, `fetched_at`, and `schedule.days`. Each lesson preserves `source_row`, `time`, `subject`, `teacher`, `room`, and structured location/mode/link/comment fields. `schema_version` changes when this response shape changes incompatibly.

The service returns `404` for unknown routes or groups and `502` with `{"error":"upstream_or_parse_failure"}` when fetching, parsing, validation, or persistence fails. Internal exception details are logged server-side and are not returned to clients. `/health` is a liveness check only; it does not verify upstream availability.

Each timetable request performs a live portal fetch and may write a deduplicated raw HTML snapshot and normalized lessons to `state/timetable.sqlite3`. Treat the service as unauthenticated and bind it to loopback or a protected private network; do not expose it directly to the public internet. Configure filesystem permissions and a retention policy for snapshots.
