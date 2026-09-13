"""Regression tests for architectural bug fixes (Bugs 5-7).

Covers:
- Bug #5: healthcheck alert deduplication, API health endpoint
- Bug #6: freshness metadata in API response
- Bug #7: holidays from knowledge data
- Snapshot pruning safety
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import tempfile
from unittest import mock

import pytest

# Ensure project root is on path
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


# ── Bug #7: Holidays from knowledge data ──────────────────────────────

class TestHolidaysFromKnowledgeData:
    """Regression: HOLIDAYS loaded from knowledge/holidays.json, not hardcoded."""

    def test_holidays_contains_2026_09_01(self):
        """01.09.2026 behavior preserved after moving to knowledge data."""
        from schedule_logic import HOLIDAYS
        assert dt.date(2026, 9, 1) in HOLIDAYS

    def test_holidays_loaded_from_file(self):
        """HOLIDAYS comes from knowledge/holidays.json."""
        from schedule_logic import _load_holidays
        holidays = _load_holidays()
        assert dt.date(2026, 9, 1) in holidays

    def test_holidays_fallback_on_missing_file(self):
        """Fallback preserves 01.09.2026 when file is missing."""
        from schedule_logic import _load_holidays
        with mock.patch("pathlib.Path.read_text", side_effect=OSError("missing")):
            holidays = _load_holidays()
            assert dt.date(2026, 9, 1) in holidays

    def test_lessons_for_date_on_holiday_returns_empty(self):
        """01.09.2026 returns empty lessons list."""
        from schedule_logic import lessons_for_date
        weeks = [{"week": 1, "half": "top", "start": "01.09.2026", "end": "05.09.2026"}]
        schedule = {"days": {"Вторник": [{"subject": "Test", "time": "09:00", "source_row": 1}]}}
        week, lessons = lessons_for_date(schedule, weeks, dt.date(2026, 9, 1))
        assert week is not None
        assert lessons == []


# ── Bug #6: Freshness metadata ────────────────────────────────────────

class TestFreshnessMetadata:
    """Regression: API response includes observed_at, age_seconds, stale."""

    def test_add_freshness_with_valid_timestamp(self):
        from api import _add_freshness
        now = dt.datetime.now(dt.timezone.utc)
        response = {"fetched_at": now.isoformat(), "data": "test"}
        result = _add_freshness(response)
        assert "observed_at" in result
        assert "age_seconds" in result
        assert "stale" in result
        assert result["age_seconds"] >= 0
        assert result["stale"] is False  # Just fetched

    def test_add_freshness_stale_when_old(self):
        from api import _add_freshness
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
        response = {"fetched_at": old.isoformat(), "data": "test"}
        result = _add_freshness(response)
        assert result["stale"] is True
        assert result["age_seconds"] > 3600

    def test_add_freshness_without_timestamp(self):
        from api import _add_freshness
        response = {"data": "test"}
        result = _add_freshness(response)
        assert result["stale"] is True


# ── Bug #5: Healthcheck alert deduplication ───────────────────────────

class TestHealthcheckAlertDedup:
    """Regression: healthcheck alerts only on OK->FAIL or problem set change."""

    def test_problems_fingerprint_stable(self):
        from healthcheck import _problems_fingerprint
        p1 = ["service inactive", "state stale"]
        p2 = ["state stale", "service inactive"]  # Same problems, different order
        assert _problems_fingerprint(p1) == _problems_fingerprint(p2)

    def test_problems_fingerprint_changes_on_new_problem(self):
        from healthcheck import _problems_fingerprint
        p1 = ["service inactive"]
        p2 = ["service inactive", "traceback"]
        assert _problems_fingerprint(p1) != _problems_fingerprint(p2)

    def test_no_alert_on_sustained_incident(self):
        """Same problems twice → second time should NOT alert."""
        from healthcheck import _load_state, _save_state, main
        import healthcheck

        state_file = healthcheck.STATE_FILE
        original_state = _load_state()

        try:
            # Simulate: first check fails → alert sent
            _save_state({"ok": True, "fingerprint": "", "last_check": "2026-01-01"})

            with mock.patch.object(healthcheck, "check", return_value={
                "ok": False, "problems": ["service inactive"]
            }):
                with mock.patch.object(healthcheck, "_dm") as mock_dm:
                    main()
                    assert mock_dm.call_count == 1  # First alert

            # Second check with same problems → NO alert
            with mock.patch.object(healthcheck, "check", return_value={
                "ok": False, "problems": ["service inactive"]
            }):
                with mock.patch.object(healthcheck, "_dm") as mock_dm:
                    main()
                    assert mock_dm.call_count == 0  # No duplicate alert

            # Different problems → alert again
            with mock.patch.object(healthcheck, "check", return_value={
                "ok": False, "problems": ["service inactive", "traceback"]
            }):
                with mock.patch.object(healthcheck, "_dm") as mock_dm:
                    main()
                    assert mock_dm.call_count == 1  # New problem set

        finally:
            _save_state(original_state)

    def test_recovery_alert_sent(self):
        """FAIL -> OK transition sends recovery alert."""
        from healthcheck import _save_state, main
        import healthcheck

        original_state = healthcheck._load_state()
        try:
            _save_state({"ok": False, "fingerprint": "abc123", "last_check": "2026-01-01"})

            with mock.patch.object(healthcheck, "check", return_value={"ok": True, "problems": []}):
                with mock.patch.object(healthcheck, "_dm") as mock_dm:
                    main()
                    assert mock_dm.call_count == 1
                    assert "восстановился" in mock_dm.call_args[0][0]
        finally:
            _save_state(original_state)


# ── Snapshot pruning safety ───────────────────────────────────────────

class TestSnapshotPruningSafety:
    """Regression: pruning never deletes current head or pending snapshot."""

    def test_prune_keeps_current_head(self):
        """prune_snapshots with keep_snapshot_id preserves that snapshot."""
        from api import connect, persist, prune_snapshots
        import api

        db = connect(":memory:")
        try:
            html = "<table><tr><th>дата</th><th>время</th><th>предмет</th><th>преподаватель</th><th>ауд.</th></tr><tr><td>Пн</td><td>09:00</td><td>Test</td><td>Prof</td><td>101</td></tr></table>"
            data = {
                "stub": False,
                "weeks": [],
                "schedule": {"days": {"Понедельник": [{"subject": "Test", "time": "09:00", "teacher": "Prof", "room": "101", "source_row": 1}]}},
                "physical_lesson_count": 1,
            }
            sid = persist(db, html, "http://test/group1", data)

            # Set retention to 0 days (would delete everything)
            deleted = prune_snapshots(db, retention_days=0, keep_snapshot_id=sid)
            assert deleted == 0  # Nothing deleted because keep_snapshot_id

            # Verify snapshot still exists
            row = db.execute("SELECT id FROM snapshots WHERE id=?", (sid,)).fetchone()
            assert row is not None
        finally:
            db.close()

    def test_prune_respects_retention_days(self):
        """Old snapshots are pruned, recent ones kept."""
        from api import connect, prune_snapshots

        db = connect(":memory:")
        try:
            now = dt.datetime.now(dt.timezone.utc)
            old = (now - dt.timedelta(days=100)).isoformat()
            recent = (now - dt.timedelta(days=1)).isoformat()

            db.execute(
                "INSERT INTO snapshots(fetched_at,url,content_fingerprint,raw_hash,html,parsed_json,parser_version) VALUES(?,?,?,?,?,?,?)",
                (old, "http://old", "fp1", "rh1", "<html>", "{}", "3"),
            )
            db.execute(
                "INSERT INTO snapshots(fetched_at,url,content_fingerprint,raw_hash,html,parsed_json,parser_version) VALUES(?,?,?,?,?,?,?)",
                (recent, "http://recent", "fp2", "rh2", "<html>", "{}", "3"),
            )
            db.commit()

            deleted = prune_snapshots(db, retention_days=90, now=now)
            assert deleted == 1  # Only old one deleted

            remaining = db.execute("SELECT url FROM snapshots").fetchall()
            assert len(remaining) == 1
            assert remaining[0][0] == "http://recent"
        finally:
            db.close()
