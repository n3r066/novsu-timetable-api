# NovSU Timetable Bot: Architecture and Operations

This document is the detailed handoff for humans and AI agents operating the
NovSU timetable service. It describes current code behavior. If documentation
and code disagree, inspect the named function and update this document in the
same change.

## 1. Purpose and Production Shape

The project tracks NovSU group `6381`, exposes a local JSON API, posts semantic
schedule changes to a Telegram channel, and continuously edits pinned Telegram
message `3` into a date-aware dashboard.

The deployed host currently runs:

- `novsu-timetable-monitor.service`: polling and Telegram delivery;
- `novsu-timetable-api.service`: local API on `127.0.0.1:8787`;
- `novsu-timetable-healthcheck.timer`: hourly liveness check and DM alert;
- `novsu-timetable-rollover.timer`: intentionally disabled because the main
  monitor reevaluates dashboard rollover every cycle.

The repository unit starts the monitor with `--interval 3 --jitter 0.25`, which
means a start-to-start period of about 3 minutes plus or minus 15 seconds.
Network/portal failures use exponential backoff capped at about 20 minutes.

## 2. End-to-End Data Flow

```text
portal.novsu.ru
    |
    v
fetch.py: curl + browser headers + retries + structural validation
    |
    v
parse.py / portal_parser.py: HTML -> canonical schedule + academic calendar
    |
    +--> fetch.content_fingerprint: semantic content identity
    |
    +--> api.py: SQLite snapshot + local JSON API
    |
    v
monitor.py: compare atomic baseline -> semantic diff
    |
    +--> text-first change notification -> Telegram channel
    |       |
    |       +--> optional old/new screenshot enhancement queue
    |
    +--> post.edit_dashboard_post: resolve current view -> pinned message 3
            |
            +--> format.py rich message
            +--> portal day screenshots via Chromium and cache
```

One monitor cycle fetches and parses once. The monitor persists that successful
snapshot, and the API serves the materialized snapshot for the tracked default
group rather than independently hitting the portal.

## 3. Module Responsibilities

| Module | Responsibility |
|---|---|
| `config.py` | Paths, group references, environment, portal URLs, browser headers, fetch/cache settings |
| `fetch.py` | Curl fetch, retries, structural page classification, semantic content fingerprint |
| `portal_parser.py` | Low-level recovery of malformed portal tables, grid expansion, screenshot HTML chunks |
| `parse.py` | Canonical lessons, calendar weeks, comments, locations, delivery mode, teacher IDs |
| `bells.py` | Academic-hour parsing and all interval/end-time arithmetic |
| `schedule_logic.py` | Date parsing, parity, note conditions, applicable lessons, day/week views, dashboard rollover |
| `format.py` | Telegram rich messages, fallback text, dashboard presentation, guide content |
| `screenshot.py` | Headless Chromium rendering and image trimming |
| `post.py` | Dashboard orchestration, media attachment, screenshot caches, manual posting commands |
| `monitor.py` | Poll loop, baseline, confirmation, diff, durable notification, retries, technical alerts |
| `telegram_api.py` | Rich payload validation, JSON/multipart transport, Telegram retries |
| `api.py` | HTTP routes, group resolution, validation, SQLite snapshots, response cache |
| `healthcheck.py` | Service/state/journal checks and incident/recovery DM alerts |

## 4. Canonical Data Model

`parse_all(html)` returns roughly:

```json
{
  "stub": false,
  "weeks": [
    {"week": 1, "half": "top", "start": "01.09.2026", "end": "05.09.2026"}
  ],
  "schedule": {
    "days": {
      "Среда": [
        {
          "subject": "(лек.) ...",
          "time": "17:00 18:00 19:00",
          "teacher": "...",
          "room": "418",
          "note": "Антоново",
          "location": "Антоново",
          "delivery_mode": "in_person",
          "source_row": 12
        }
      ]
    }
  },
  "physical_lesson_count": 1,
  "teacher_ids": {}
}
```

Important distinctions:

- `time` stores academic-hour start tokens from the portal, not a ready-made
  wall-clock interval. `bells.py` converts them.
- `subject` can include a newline followed by structured note text.
- `raw_*` fields preserve portal evidence; normalized fields drive behavior.
- `stub=true` is valid only for a structurally recognized portal stub.
- A malformed HTTP 200/WAF page is an error, not a stub.

The parser invariant is strict: canonical lesson count must equal the physical
lesson-row count. A mismatch must fail rather than publish partial data.

## 5. Academic Time

The portal prints starts of 45-minute academic hours. Examples:

- `09:00 10:00` -> `09:00–10:45`;
- `14:00 15:00 16:00 17:00` -> two displayed pairs,
  `14:00–15:45` and `16:00–17:45`;
- `17:00 18:00 19:00` -> `17:00–18:45` and `19:00–19:45`.

Do not reproduce this arithmetic outside `bells.py`. The UI intentionally does
not append technical labels such as `1 ак. ч.` when the interval already shows
the 45-minute duration.

## 6. Week Parity and Date Conditions

The portal calendar is authoritative:

- `half=top` means upper week and odd academic week numbers;
- `half=bottom` means lower week and even academic week numbers;
- this is not Python/ISO calendar-week parity;
- week boundaries come from each `start`/`end` entry parsed from the portal.

`lesson_applies_on()` evaluates conditions including:

- `по верхней/нижней неделе`;
- `с DD.MM`, `по DD.MM`, date ranges and date lists;
- `занятий не будет` on listed dates;
- `только DD.MM`;
- `с октября` and other month starts;
- `с N недели`, `до N недели`, `после N недели`.

Always call `lesson_applies_on()`, `lessons_for_date()`, `day_view()`, or
`week_view()`. A formatter must never guess applicability by searching strings.

`lesson_non_applicability_reason()` uses the same evaluator but returns a
user-facing contextual reason for screenshot annotations. The renderer preserves
the concrete boundary from this result, for example `С 9‑й недели`,
`После 9‑й недели`, `Только верхняя`, or `С 14.09`, and removes the redundant
generic prefix. Inactive lesson content has a thin strike-through while its
reason badge stays clear. Applicable DOT lessons remain unstruck. Screenshot
code must not derive a second, less precise reason from the note.

`lesson_has_expired()` is a conservative presentation-only deadline check. It
accepts exhausted explicit occurrence lists, ended date ranges, inclusive
`по DD.MM` limits, and exclusive `до N недели` limits. It uses the concrete day
being displayed and full academic-calendar bounds for yearless dates. Future
starts, parity, cancellation dates, and ambiguous/partially parsed conditions
cannot by themselves prove expiry. Existing applicability semantics are not
changed by this check.

### Dashboard rollover

`resolve_dashboard_view(schedule, weeks, now)` scans from Moscow "now" to the
next date with applicable lessons. It returns:

- `target_date`;
- selected week and parity;
- `live_days`, the selected week's days that are still useful.

Today's day remains visible until its last applicable lesson ends. After the
last Saturday lesson, the view may advance directly to the next teaching week.
Sunday retains that next-week view. After the calendar ends, the view contains
no selected week rather than falling back to week 1.

`dashboard_presentation_fingerprint()` hashes content fingerprint plus target
date, week, parity, live days, and `DASHBOARD_PRESENTATION_VERSION`. Therefore
the dashboard changes on calendar rollover even when portal HTML is unchanged.
The monitor must call dashboard editing on every unchanged cycle.

## 7. Dashboard Presentation Policy

Current user-facing rules:

- do not print `ОЧНО`; in-person is the ordinary case;
- the third table column is `место / время`;
- each lesson uses two stacked cells in that column: room/location (or `ДОТ`)
  above and monospace time below, separated by the table cell border;
  the number and lesson cells span both rows;
- explicitly mark only exceptional delivery such as `ДОТ`;
- show ordinary locations directly with the lesson;
- show short useful conditions such as `с 10.09` inline with the subject;
- reserve the collapsed `Важно` section for cancellations, date lists, complex
  ranges, unusual addresses, and unknown portal prose;
- never hide unknown notes merely to make the post shorter.

Weekly screenshots omit definitively expired source lessons rather than leaving
them crossed out forever. Future/parity-inactive lessons keep their contextual
labels. Inactive rows use readable muted blue text and a slightly darker matte
background with a thin strike-through, so the distinction survives phone-sized
display. Do not strike reason badges or cells shared with active lessons.
Applicable DOT lessons retain their normal text and green tint. A DOT lesson
that does not apply on the selected date is inactive like any other lesson.
Only actual cancellations are red. Badges show only the concrete
condition, e.g. “После 9-й недели”, without “Не на этой неделе”. The original
portal table, columns and headings are preserved. Matte blue, ivory and muted
teal accents replace harsh grey/bright fills; no replacement page header is added.
The closed screenshot section includes its concrete date and links to
the full unfiltered portal original; filtered screenshots are not labelled as
the full original.

Versioned policy lives in `knowledge/schedule_notes.json`. It is intentionally
not a "seen once" runtime cache: identical snapshots must render identically on
all machines.

When dashboard rich presentation changes, increment
`DASHBOARD_PRESENTATION_VERSION` in `schedule_logic.py`. This forces one edit
even if the portal content did not change.

### Change notification layout

`build_changes_rich_message()` uses a separate, action-first layout:

- Each weekday is a closed `details` section whose summary names the concrete
  edits, for example `Четверг (сменили аудиторию)` or
  `Пятница (переименовали пару, убрали пару)` — not a bare change count.
- Days with multiple edits start with a short overview grouped by action.
  Additions, replacements and missing teacher names are distinct actions.
  Identical new teachers, room deltas or time moves share a row listing the
  affected subjects; repeated occurrences of one subject get a lesson count.
  Never infer a meaning from a partial free-form note. Full old/new values,
  times, week variants and unknown notes remain in a closed “Подробности” section.
  Only teacher-only days may say that times and rooms did not change.
- A single edit, and each entry inside the detailed section, is a compact
  paragraph with separate lines for the bold subject, type/time/week context
  and the field delta. No repeated “У пары …”.
  A previously missing teacher is “Указали преподавателя: …”; it is not a
  replacement. Placeholder dashes are never struck through. Renames retain
  prefix highlighting so the two full names do not repeat unnecessarily.
- Moves show old and new times once, and retain any week-specific place changes.
  Removals strike their former time and place; additions name the supplied place.
- Room changes across buildings append “Другой корпус: …”, using the existing
  room decoder. Unknown buildings are never guessed. Teacher-only edits omit
  unchanged addresses, but preserve dates and unknown condition prose.
- Time and applicable parity remain visible when supplied by the source.
  Unknown time is omitted rather than rendered as an empty badge.
- Different upper/lower locations and conditions get separate labelled lines.
  Room/location and `ДОТ` are both retained when the portal supplies both.
- Unchanged teacher and extra location context do not appear in the post at
  all: they remain available in the pinned dashboard and on the portal.
- Before/after pairs are nested in a closed “Сравнить расписание” section,
  with the short caption “Было → стало”. Thin dividers separate individual edits.
  Teacher-only batch headings name the affected lesson count. Images remain ordered before then
  after; no repeated swipe instructions. Legacy single images retain captions.
- One footer links to the source and names the group; a count is included only
  for multiple changes. The notification explicitly shows the persisted detection timestamp in Moscow
  time, including seconds, and states that the portal does not expose the exact
  edit time. HTTP Date is response time and must never be used as edit time.
  Historical posts use original observation events when available; a publication
  timestamp alone is labelled as publication, never as an observed portal edit. A render-time
  calendar week is never presented as the occurrence week of the changed class.

Field deltas override stale context before grouping, without mutating the raw
diff. Same-day time moves require valid distinct full academic-hour sequences,
matching parity and an unambiguous pair. Cross-day or ambiguous changes stay
removed/added. Merged moves retain both before and after week variants; different
durations/destinations must not collapse into one entry.

The HTML fallback mirrors the grouped overview, using an expandable blockquote
for detailed entries. It keeps whole day sections within 4096 characters and
truncates old/new field values independently.
Rich output retains its text/block budget and explicitly reports omissions.
Notification-only wording changes require no dashboard or screenshot version
bump. They do not retroactively edit old Telegram posts.

## 8. Fingerprints and Caches

These identities solve different problems:

### Content fingerprint

`fetch.content_fingerprint(data)` hashes canonical business fields and calendar
weeks. It ignores raw DOM noise, source-row numbers, and ordering noise. It is
used for change detection and snapshot identity.

### Presentation fingerprint

`schedule_logic.dashboard_presentation_fingerprint(content_fp, view)` adds the
current dashboard view and presentation version. It is used to decide whether
pinned message `3` needs editing.

### Screenshot status key

`post._dashboard_screen_status_key(target_date)` combines
`SCHEDULE_SCREEN_STYLE_VERSION` with target date. Dashboard screenshots are
valid only when both content fingerprint and status key match. This prevents an
upper-week PNG from being reused for a lower week when source HTML is unchanged.

### Per-day image cache

`state/day_screens/<sha256>.png` is keyed by the exact rendered day HTML. It can
be reused across snapshots. Images are pruned by age and total size.

If screenshot status markup or CSS changes, increment
`SCHEDULE_SCREEN_STYLE_VERSION` in `post.py`.

Dashboard filtering uses snapshot-local `source_row` and physical cell
provenance, not fuzzy subject matching. Only the temporary rendering DOM is
projected: cells shared with surviving lessons are re-anchored and their spans
recomputed. Shared active/inactive cells remain neutral. A provenance mismatch
fails rendering through the text-only fallback rather than guessing a deletion.
Empty filtered days are omitted, including intentional empty cache results;
never restore the unfiltered source as their fallback. Style/presentation
versions invalidate old-policy images without manual state deletion.

### Change comparison screenshots

`post.comparison_day_screens()` renders the exact saved before/after HTML, with
`Было` / `Стало` headings. Only changed cells are yellow; added lesson cells are
green with `Добавили`, removed lesson cells are light red with `Убрали` in the
before image. No dashboard week/DOT statuses are applied to these comparisons.

Diff records are matched to canonical lessons in each snapshot, restoring old
field values on the before side. `source_row` plus optional cell provenance from
`expand_table()` identifies the physical cells, including inherited rowspans.
Ambiguous matches are left unmarked. Shared cells remain neutral for additions
and removals when an unchanged alternative still uses them. Captions describe
only annotation kinds actually rendered, not guessed from the semantic diff.

Both images use fixed column proportions and identical viewport/font settings.
After trimming, the shorter image is padded to the same canvas size without
rescaling. Rows can still change height when their text changes. The cache key
includes both annotated HTML documents, render dimensions and
`COMPARISON_SCREEN_STYLE_VERSION`. Pruning protects both images together.
Renderer errors propagate to the existing pending-media retry; a genuinely
missing snapshot/day produces a one-sided comparison rather than a fake image.

Increment only `COMPARISON_SCREEN_STYLE_VERSION` for notification-only layout
changes. This leaves the dashboard screenshot cache and pinned post untouched.

## 9. Monitor and Delivery Semantics

`monitor.run_once()` is serialized with `state/.monitor.lock`.

Normal cycle:

1. Fetch and structurally validate HTML.
2. Save last successful raw HTML.
3. Parse and verify lesson count.
4. Persist a SQLite snapshot.
5. Compare semantic fingerprint with atomic baseline.
6. If unchanged, refresh baseline observation time, evaluate dashboard rollover,
   and process queued media.
7. If changed, compute semantic diff and send durable text first.
8. Only after successful text delivery advance the baseline.
9. Queue optional old/new screenshot enhancement and update dashboard text.

Important failure behavior:

- Network/portal error does not overwrite the last good baseline.
- A pending text notification is retained in `pending_notification.json`.
- Rich delivery falls back to ordinary HTML `sendMessage`.
- Media enhancement is independent and retried from `pending_media.json`.
- Dashboard failures are retained in `pending_dashboard.json` and alerted once
  per distinct error.
- A suspicious large disappearance requires confirmation before becoming a
  real change.
- A new fingerprint with an empty semantic diff creates
  `unexplained_change.json` and a technical DM instead of silently disappearing.
- On the first successful cycle after state loss, a baseline is established and
  the dashboard is immediately reconciled; no fake "all lessons added" alert is
  posted.

DMs are operational only. Schedule content belongs in the channel.

## 10. Runtime State Reference

All runtime files are under ignored `state/` and should normally be treated as
opaque production state.

| Path | Meaning | Safe to delete? |
|---|---|---|
| `monitor_state.json` | Authoritative atomic baseline and last true change event | No; loss resets baseline history |
| `last.json`, `last_parsed.json` | Compatibility copies of baseline | Not independently; inspect authoritative state first |
| `6381.html` | Last successfully fetched HTML | Re-creatable, useful evidence |
| `timetable.sqlite3` | Deduplicated snapshots, current heads, normalized lessons | No without explicit recovery plan |
| `changes.jsonl` | Poll observation log, rotated around 1 MB | Diagnostic only |
| `candidate_change.json` | First observation of suspicious destructive transition | Let monitor manage it |
| `pending_notification.json` | Undelivered durable text diff | Never discard casually |
| `pending_media.json` | FIFO of delivered rich posts awaiting media enhancement | Let monitor retry |
| `pending_dashboard.json` | Last failed dashboard edit | Let monitor retry |
| `last_dashboard_post.json` | Last successful message/presentation state | Re-creatable, affects idempotency |
| `dashboard_screens.json` | Dashboard screenshot index and status key | Re-creatable |
| `day_screens/` | Content-addressed dashboard and paired comparison PNGs | Re-creatable; cache only |
| `dashboard_screens/`, `diff_screens.json` | Diff screenshot files/index | Re-creatable; pending media may reference snapshots |
| `teacher_names.json`, `teacher_ids.json` | Name enrichment caches | Re-creatable but useful |
| `unexplained_change.json` | Evidence for a fingerprint/diff inconsistency | Preserve until diagnosed |
| `healthcheck_status.json` | Incident/recovery deduplication state | Re-creatable |

Before any manual state surgery:

1. Stop the monitor to avoid concurrent writes.
2. Copy the relevant files and SQLite database outside `state/`.
3. Understand pending notification/media semantics.
4. Make the smallest change.
5. Start the monitor and inspect a complete cycle plus healthcheck.

Never use `rm -rf state` as routine troubleshooting.

## 11. SQLite Behavior

`api.persist()` deduplicates snapshots by `(url, content_fingerprint,
parser_version)`. Raw HTML and parsed JSON are retained with normalized lessons.
`snapshot_heads` records the latest observation for each URL even if semantic
content is unchanged.

The default tracked group is served from the monitor's materialized snapshot,
including during a portal outage. Other groups use request-path fetches and a
short in-process TTL cache.

If parsed persistence semantics become incompatible, review `PARSER_VERSION`
in `api.py`, migration helpers, `db/schema.sql`, and old snapshot behavior.

## 12. Environment and Secrets

`.env` is loaded by `config.py` and systemd units. Never print or commit it.

Required for Telegram operation:

```text
TG_BOT_TOKEN
TG_CHANNEL_ID
TG_DM_TARGET
```

Relevant optional variables:

```text
NOVSU_DASHBOARD_POST_ID
NOVSU_DEFAULT_GROUP
NOVSU_DEFAULT_INST_ID
NOVSU_DEFAULT_TYPE
NOVSU_DEFAULT_YEAR
NOVSU_FETCH_RETRIES
NOVSU_FETCH_RETRY_DELAY
NOVSU_CACHE_TTL
NOVSU_SNAPSHOT_RETENTION_DAYS
NOVSU_MONITOR_SNAPSHOT_MAX_AGE
NOVSU_DM_SILENT
```

`NOVSU_DEFAULT_YEAR` is the enrollment year. For manual development cycles set
`NOVSU_DM_SILENT=1` when a failing path could otherwise send a technical DM.

## 13. API

The local service listens on `127.0.0.1:8787` by default.

```text
GET /health
GET /v1/institutes
GET /v1/groups?q=6381
GET /v1/timetables/6381
GET /v1/timetables/6381/day?date=tomorrow
GET /v1/timetables/6381/week?week=2
GET /v1/timetables/6381/week?mode=auto
GET /v1/timetables/6381/week?mode=auto&date=2026-09-13
GET /v1/timetables/6381/next?date=2026-09-07&time=09:30&limit=3
```

Keep this service on loopback or a private network; it has no authentication.

### Automatic calendar week

`/week?mode=auto` uses `find_current_or_next_week()` in `schedule_logic.py`.
The anchor defaults to today's Moscow date; optional `date` accepts the same
formats as the existing day/week endpoints. Selection is recomputed per request,
even when the parsed snapshot has not changed.

Containment takes priority: if the anchor is inside a published week, return
that week, including any Sunday explicitly included by the portal. Otherwise
return the chronologically nearest future week, even before the calendar starts
or across a long gap. Never infer a week number or extend the published dates.
Malformed/reversed date intervals are ignored. Selection does not depend on
lesson availability or the current time, unlike dashboard rollover.

Successful responses retain the existing week-view schema and applicability
filtering. After the last valid calendar date, return HTTP 404 with
`{"error":"calendar_ended","calendar_end":"YYYY-MM-DD"}`. With no valid calendar,
return HTTP 404 with `{"error":"calendar_unavailable"}`. A validated portal stub
still returns HTTP 200 with `stub: true`, as on other derived endpoints.

Omitted `mode` preserves the existing behavior, including calendar-gap 404s.
Explicit `week=N` remains a fixed selection from the latest loaded calendar, not
an archive. `mode=auto` plus `week`, unknown modes, empty/duplicate parameters,
and `mode` on other endpoints are rejected as HTTP 400 before loading data.

This is request-time selection only: no DB migration or presentation/parser
version bump is needed. It does not change snapshot freshness or add another
upstream polling owner. Restart the API after deploying the shared helper;
the monitor and pinned message require no change for this API-only feature.

## 14. Systemd and Deployment

Repository units live under `deploy/`. Installed units live under
`/etc/systemd/system/`; inspect both when diagnosing configuration drift.

Install repository units:

```bash
sudo ./deploy/install-monitor-service.sh
sudo systemctl start novsu-timetable-monitor.service
```

The installer intentionally does not start the monitor until `.env` is checked.
The API is enabled and started. The standalone rollover timer is disabled
because monitor polling already performs rollover.

After editing Python used by a long-running service:

```bash
sudo systemctl restart novsu-timetable-monitor.service
sudo systemctl restart novsu-timetable-api.service  # only when API code/config changed
```

After editing a unit:

```bash
sudo install -m 0644 deploy/<unit> /etc/systemd/system/<unit>
sudo systemctl daemon-reload
sudo systemctl restart <unit>
```

Do not assume an edited repository unit is active until the installed unit is
compared and reinstalled.

## 15. Routine Operational Commands

Status:

```bash
systemctl is-active novsu-timetable-monitor.service
systemctl is-enabled novsu-timetable-monitor.service
systemctl status novsu-timetable-monitor.service --no-pager
systemctl list-timers 'novsu*' --all --no-pager
```

Recent logs:

```bash
journalctl -u novsu-timetable-monitor.service --since '30 minutes ago' --no-pager -o cat
journalctl -u novsu-timetable-healthcheck.service --since '24 hours ago' --no-pager -o cat
```

Health:

```bash
python3 healthcheck.py
curl --fail http://127.0.0.1:8787/health
```

One development cycle without production DMs:

```bash
NOVSU_DM_SILENT=1 PYTHONPATH=. python3 monitor.py --update-post 3
```

This still has production channel/dashboard side effects. Omit `--update-post`
and mock Telegram in tests when no production side effect is intended.

Manual dashboard reconciliation:

```bash
PYTHONPATH=. python3 post.py --roll 3
```

Explicit historical/future preview and edit:

```bash
PYTHONPATH=. python3 post.py --edit-rich 3 --date 2026-09-07
```

These commands edit production Telegram content. Use them only when requested.

## 16. Verification Matrix

Minimum for any code change:

```bash
PYTHONPATH=. python3 -m pytest -q
PYTHONPATH=. python3 -m py_compile *.py
git diff --check
```

Then run focused evidence appropriate to the change:

| Change | Additional verification |
|---|---|
| Parser/portal shape | Fixture for the exact HTML shape; assert physical count invariant |
| Time arithmetic | `tests/test_bells.py`, schedule view tests, exact displayed interval |
| Parity/date rules | Test upper and lower weeks plus the boundary date |
| Dashboard rollover | Unchanged content fingerprint across day/week boundary |
| Rich formatting | `validate_rich_payload()` on realistic data and Telegram response if production edit requested |
| Screenshot/cache | Same content across different target weeks; text-only failure then media recovery |
| Monitor delivery | Failure then retry; assert baseline does not advance before text delivery |
| API/schema | Route tests, persistence round-trip, existing DB migration behavior |
| systemd/runtime | Restart, wait for one complete cycle, inspect journal and healthcheck |

Never claim production success based solely on unit tests. Verify the actual
service and, when a Telegram edit was requested, inspect Telegram's returned
rich message rather than only local JSON.

## 17. Safe Change Recipes

### Change dashboard wording/layout

1. Edit `format.py` and presentation tests.
2. Preserve compact policy from section 7.
3. Increment `DASHBOARD_PRESENTATION_VERSION`.
4. Validate rich payload against real parsed data.
5. Run the full suite.
6. If requested, edit post `3`, inspect Telegram response, restart monitor.

### Change a date/parity rule

1. Add the portal phrase as a regression fixture/test.
2. Implement only in `schedule_logic.py`.
3. Test before, on, and after the boundary for both parity halves.
4. Simulate at least `top -> bottom -> top` with unchanged portal content.
5. Run the full suite.

### Change parser behavior

1. Preserve raw evidence and create a minimal fixture.
2. Keep malformed nested-row support.
3. Assert parsed count equals physical count.
4. Check semantic fingerprint impact and diff behavior.
5. Review persistence/parser version if normalized output changes incompatibly.

### Change screenshot rendering

1. Update `portal_parser.py`/`screenshot.py` or status markup in `post.py`.
2. Increment `SCHEDULE_SCREEN_STYLE_VERSION` for dashboard output changes, or
   `COMPARISON_SCREEN_STYLE_VERSION` for notification-only comparisons.
3. Test cache invalidation across target weeks.
4. Verify text-only delivery still succeeds if Chromium fails.

### Change monitor delivery

1. Preserve the text-first ordering.
2. Test Telegram failure and next-cycle retry.
3. Assert changed baseline advances only after successful text delivery.
4. Assert no duplicate post after recovery.
5. Ensure DM text is technical and escaped.

## 18. Common Mistakes

- Using current year instead of enrollment year in the portal URL.
- Computing parity from ISO week number.
- Filtering only on `по верхней/нижней` and ignoring date conditions.
- Treating missing table as a valid empty schedule.
- Updating baseline before notification delivery.
- Running Chromium before sending durable text.
- Keying screenshots only by content fingerprint.
- Forgetting presentation/style version bumps.
- Reintroducing noisy `ОЧНО`, `все очно`, or technical `ак. ч.` labels.
- Hiding unknown notes instead of retaining them under `Важно`.
- Editing a unit in `deploy/` but not reinstalling it.
- Deleting state to fix idempotency and thereby losing pending delivery.
- Running manual monitor/post commands without understanding Telegram side effects.
- Exposing tokens or private chat/channel identifiers in diagnostic output.

## 19. Current Known Operational Notes

- The project repository is private, but secrets still do not belong in Git.
- Production pinned dashboard message ID is currently `3`; prefer
  `NOVSU_DASHBOARD_POST_ID` over adding more hard-coded IDs.
- Healthcheck unit templates are versioned in `deploy/`. Older checkouts may
  still differ from `/etc/systemd/system`; compare both before deployment.
- The README is an overview. This runbook and named code functions carry the
  detailed operational contract.
