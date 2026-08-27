import json
import os
from pathlib import Path

os.environ.setdefault("TG_BOT_TOKEN", "test-token")
os.environ.setdefault("TG_CHANNEL_ID", "-1001234567890")
os.environ.setdefault("TG_DM_TARGET", "123456789")

import monitor  # noqa: E402
from parse import parse_all  # noqa: E402


def _schedule():
    html = Path("tests/fixtures/rowspan_time.html").read_text(encoding="utf-8")
    return parse_all(html)["schedule"]


def _copy(schedule):
    return json.loads(json.dumps(schedule, ensure_ascii=False))


def test_diff_identical_schedules_is_empty():
    old = _schedule()
    diff = monitor.diff_schedules(old, _copy(old))
    assert diff["added"] == []
    assert diff["removed"] == []
    assert diff["changed"] == []
    assert diff["transition"] is None


def test_diff_detects_added_and_removed():
    old = _schedule()
    new = _copy(old)
    day = list(new["days"])[0]
    removed_lesson = new["days"][day].pop(0)
    new["days"].setdefault("Пятница", []).append({
        "subject": "(лек.) Новый предмет", "time": "09:00 10:30",
        "room": "101", "teacher": "Иванов", "note": "",
    })
    diff = monitor.diff_schedules(old, new)
    assert [x["subject"] for x in diff["added"]] == ["(лек.) Новый предмет"]
    removed_subjects = [x["subject"].split("\n")[0] for x in diff["removed"]]
    assert removed_lesson["subject"].split("\n")[0] in removed_subjects
    assert diff["transition"] is None


def test_diff_detects_changed_room_and_teacher():
    old = _schedule()
    new = _copy(old)
    day = list(new["days"])[0]
    new["days"][day][0]["room"] = "999"
    new["days"][day][0]["teacher"] = "Новый Препод"
    diff = monitor.diff_schedules(old, new)
    assert diff["added"] == [] and diff["removed"] == []
    assert len(diff["changed"]) == 1
    labels = {label for label, _, _ in diff["changed"][0]["fields"]}
    assert labels == {"ауд.", "преподаватель"}


def test_diff_stub_transitions():
    schedule = _schedule()
    assert monitor.diff_schedules(None, schedule)["transition"] == "published"
    assert monitor.diff_schedules(schedule, None)["transition"] == "vanished"
    assert monitor.diff_schedules(None, None)["transition"] is None


def test_post_changes_falls_back_to_plain_html(monkeypatch):
    calls = {}
    monkeypatch.setattr(monitor, "send_rich_message",
                        lambda *a, **k: {"ok": False, "description": "rich down"})

    def fake_tg(method, **fields):
        calls["method"] = method
        calls["fields"] = fields
        return {"ok": True, "result": {"message_id": 1}}

    monkeypatch.setattr(monitor, "_tg_api", fake_tg)
    diff = monitor.diff_schedules(None, _schedule())
    result = monitor._post_changes_to_channel(diff, [])
    assert result["ok"] is True
    assert calls["method"] == "sendMessage"
    assert calls["fields"]["parse_mode"] == "HTML"
    assert "обновилось" in calls["fields"]["text"]


def test_run_once_no_change_does_not_post(monkeypatch, tmp_path):
    html = Path("tests/fixtures/rowspan_time.html").read_text(encoding="utf-8")
    monkeypatch.setattr(monitor.config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(monitor, "fetch_html", lambda *a, **k: html)
    posts: list = []
    monkeypatch.setattr(monitor, "_post_changes_to_channel",
                        lambda diff, weeks: posts.append(diff) or {"ok": True})
    dms: list = []
    monkeypatch.setattr(monitor, "_dm", lambda text: dms.append(text))

    first = monitor.run_once()
    assert first["changed"] is False  # первый прогон — нет прошлого отпечатка
    second = monitor.run_once()
    assert second["changed"] is False  # контент не менялся
    assert posts == []
    assert dms == []


def test_run_once_posts_diff_to_channel_on_change(monkeypatch, tmp_path):
    html1 = Path("tests/fixtures/rowspan_time.html").read_text(encoding="utf-8")
    html2 = html1.replace("Предмет Б", "Предмет Ц")
    pages = iter([html1, html2])
    monkeypatch.setattr(monitor.config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(monitor, "fetch_html", lambda *a, **k: next(pages))
    posts: list = []
    monkeypatch.setattr(monitor, "_post_changes_to_channel",
                        lambda diff, weeks: posts.append(diff) or {"ok": True})
    dms: list = []
    monkeypatch.setattr(monitor, "_dm", lambda text: dms.append(text))

    monitor.run_once()
    result = monitor.run_once()
    assert result["changed"] is True
    assert len(posts) == 1
    diff = posts[0]
    added = [x["subject"].split("\n")[0] for x in diff["added"]]
    removed = [x["subject"].split("\n")[0] for x in diff["removed"]]
    assert any("Предмет Ц" in s for s in added)
    assert any("Предмет Б" in s for s in removed)
    assert dms == []  # личка молчит про содержимое


def test_run_once_volatile_timestamp_change_is_silent(monkeypatch, tmp_path):
    base = Path("tests/fixtures/rowspan_time.html").read_text(encoding="utf-8")
    pages = iter([
        base + '\n<a href="tt.xls" title="17.08.2026 03:17:30">xls</a>',
        base + '\n<a href="tt.xls" title="18.08.2026 11:45:02">xls</a>',
    ])
    monkeypatch.setattr(monitor.config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(monitor, "fetch_html", lambda *a, **k: next(pages))
    posts: list = []
    monkeypatch.setattr(monitor, "_post_changes_to_channel",
                        lambda diff, weeks: posts.append(diff) or {"ok": True})

    monitor.run_once()
    result = monitor.run_once()
    assert result["changed"] is False
    assert posts == []


def test_fetch_error_does_not_count_as_change(monkeypatch, tmp_path):
    monkeypatch.setattr(monitor.config, "STATE_DIR", tmp_path)

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(monitor, "fetch_html", boom)
    posts: list = []
    monkeypatch.setattr(monitor, "_post_changes_to_channel",
                        lambda diff, weeks: posts.append(diff) or {"ok": True})
    try:
        monitor.run_once()
    except RuntimeError:
        pass
    else:
        raise AssertionError("run_once must propagate fetch errors")
    assert posts == []
    assert not (tmp_path / "last.json").exists()  # состояние не перезаписано


def test_diff_preserves_duplicate_simultaneous_subgroups():
    old = {"days": {"Понедельник": [
        {"subject": "Матан", "time": "09:00", "room": "101", "teacher": "Иванов", "note": "", "subgroup": "1"},
        {"subject": "Матан", "time": "09:00", "room": "102", "teacher": "Петров", "note": "", "subgroup": "2"},
    ]}}
    new = _copy(old)
    new["days"]["Понедельник"][0]["room"] = "201"
    diff = monitor.diff_schedules(old, new)
    assert diff["added"] == [] and diff["removed"] == []
    assert len(diff["changed"]) == 1
    assert diff["changed"][0]["subgroup"] == "1"
    assert diff["changed"][0]["fields"] == [("ауд.", "101", "201")]


def test_explicit_stub_transition_does_not_confuse_empty_published_table():
    empty = {"days": {}}
    diff = monitor.diff_schedules(empty, empty, old_stub=False, new_stub=False)
    assert diff["transition"] is None
    assert monitor.diff_schedules(empty, None, old_stub=False, new_stub=True)["transition"] == "vanished"


def test_failed_delivery_is_retried_and_baseline_is_not_advanced(monkeypatch, tmp_path):
    html1 = Path("tests/fixtures/rowspan_time.html").read_text(encoding="utf-8")
    html2 = html1.replace("Предмет Б", "Предмет Ц")
    pages = iter([html1, html2, html2])
    monkeypatch.setattr(monitor.config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(monitor, "fetch_html", lambda *a, **k: next(pages))
    results = iter([{"ok": False, "error": "down"}, {"ok": True}])
    posts = []
    monkeypatch.setattr(monitor, "_post_changes_to_channel", lambda diff, weeks: posts.append(diff) or next(results))
    monkeypatch.setattr(monitor, "_dm", lambda text: None)

    monitor.run_once()
    failed = monitor.run_once()
    retried = monitor.run_once()
    assert failed["delivery_pending"] is True
    assert retried["posted"] is True
    assert len(posts) == 2
    assert not (tmp_path / "pending_notification.json").exists()


def test_stub_transition_requires_two_matching_samples(monkeypatch, tmp_path):
    schedule = Path("tests/fixtures/rowspan_time.html").read_text(encoding="utf-8")
    stub = Path("tests/fixtures/stub_with_xls_timestamps.html").read_text(encoding="utf-8")
    pages = iter([schedule, stub, stub])
    monkeypatch.setattr(monitor.config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(monitor, "fetch_html", lambda *a, **k: next(pages))
    posts = []
    monkeypatch.setattr(monitor, "_post_changes_to_channel", lambda diff, weeks: posts.append(diff) or {"ok": True})

    monitor.run_once()
    candidate = monitor.run_once()
    confirmed = monitor.run_once()
    assert candidate["pending_confirmation"] is True
    assert candidate["changed"] is False
    assert confirmed["changed"] is True
    assert posts[0]["transition"] == "vanished"
