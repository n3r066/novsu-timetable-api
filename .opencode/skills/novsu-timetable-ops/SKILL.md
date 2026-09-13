---
name: novsu-timetable-ops
description: Use ONLY when operating or modifying the NovSU timetable repository: monitor.py, schedule_logic.py, Telegram dashboard post 3, upper/lower week rollover, parser, screenshots, state recovery, healthcheck, API, or systemd deployment.
---

# NovSU Timetable Operations

Use this workflow for every task in this repository that can affect parsed
schedule data, week/date selection, Telegram output, runtime state, or services.

## Load Context First

1. Read `AGENTS.md`.
2. Read the relevant sections of `docs/OPERATIONS.md`.
3. Inspect current code and tests; do not infer behavior from the conversation
   or README alone.
4. Check `git status --short`. Preserve unrelated user changes.
5. For service work, compare `deploy/` units with `/etc/systemd/system/`.

## Classify the Task

Use the matching source of truth:

- Fetch/WAF/portal response: `fetch.py`.
- Malformed HTML/table structure: `portal_parser.py`, then `parse.py`.
- Academic time and interval display: `bells.py`.
- Upper/lower week, dates, cancellation, applicability, rollover:
  `schedule_logic.py`.
- Telegram wording and rich structure: `format.py`.
- Dashboard media and caches: `post.py`.
- Polling, diffs, baseline, retries, alerts: `monitor.py`.
- Rich payload validation/transport: `telegram_api.py`.
- HTTP groups, snapshots, SQLite: `api.py` and `db/schema.sql`.
- Runtime health: `healthcheck.py` and systemd.

Do not implement the same business rule in multiple layers.

## Protect Core Invariants

- Portal `year` means enrollment year.
- `top` is upper/odd academic week; `bottom` is lower/even academic week.
- Use portal calendar bounds, never ISO week parity.
- Use `lesson_applies_on()` or higher-level views for applicability.
- Use `lesson_non_applicability_reason()` for contextual screenshot labels;
  never collapse week boundaries into a generic date reason.
- Reject partial parses and unrecognized HTTP 200 pages.
- Keep change delivery text-first.
- Do not advance a changed baseline before text delivery succeeds.
- Evaluate dashboard view even when portal content is unchanged.
- Match screenshot cache by content fingerprint and week/style status key.
- Keep unknown note prose visible under `Важно`.
- Do not label normal lessons `ОЧНО`; call out only `ДОТ`.
- Keep room/location above and time below as separate stacked cells in the
  single `место / время` dashboard column.
- Do not expose `.env`, token values, chat IDs, or private state.

## Versioning Triggers

- Increment `DASHBOARD_PRESENTATION_VERSION` in `schedule_logic.py` when
  dashboard wording, structure, or visible semantics change.
- Increment `SCHEDULE_SCREEN_STYLE_VERSION` in `post.py` when generated
  screenshot markup/status/style changes.
- Review `PARSER_VERSION` in `api.py` for incompatible persisted parse changes.

## Test the Failure, Not Just the Happy Path

Add a regression test for the exact issue. For date logic, test before/on/after
the boundary and both parity halves. For autonomous rollover, keep the content
fingerprint unchanged while advancing time. For delivery, fail Telegram or
Chromium once and verify recovery without duplicate text or baseline loss.

Always run:

```bash
PYTHONPATH=. python3 -m pytest -q
PYTHONPATH=. python3 -m py_compile *.py
git diff --check
```

For runtime work also run:

```bash
systemctl is-active novsu-timetable-monitor.service
systemctl is-enabled novsu-timetable-monitor.service
python3 -c 'import healthcheck,json; print(json.dumps(healthcheck.check(), ensure_ascii=False))'
journalctl -u novsu-timetable-monitor.service --since '10 minutes ago' --no-pager -o cat
```

## Production Side Effects

The following commands touch production and require user intent:

```bash
PYTHONPATH=. python3 post.py --roll 3
PYTHONPATH=. python3 post.py --edit-rich 3 --date YYYY-MM-DD
PYTHONPATH=. python3 monitor.py --update-post 3
systemctl restart novsu-timetable-monitor.service
systemctl restart novsu-timetable-api.service
```

Before a Telegram edit, validate the local rich payload. After an edit, inspect
Telegram's returned `rich_message`; do not rely only on local output. After a
service restart, wait for a complete cycle and verify active state, recent
journal, fresh state, and healthcheck.

Set `NOVSU_DM_SILENT=1` for manual development runs that could otherwise send
technical DMs. It does not suppress channel/dashboard side effects.

## State Recovery

Never delete `state/` as a generic fix. `pending_notification.json` can contain
an undelivered durable diff, and `pending_media.json` can contain work for an
already delivered post. Before manual state edits, stop the monitor, back up
the relevant state and SQLite database, understand queue semantics, then make
the smallest possible repair.

## Completion Report

State exactly:

- what behavior changed;
- which versions were bumped;
- tests and runtime checks actually run;
- whether Telegram or systemd was touched;
- anything not verified;
- that OpenCode must be restarted/new session opened to discover skill edits.
