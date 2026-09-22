import datetime as dt
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


def test_diff_enriches_short_teacher_names():
    """Bare last names in the changed/added/removed sections are enriched
    to full FIO when a matching full name exists in the schedules."""
    old = _schedule()
    new = _copy(old)
    day = list(new["days"])[0]
    # Change teacher to a bare last name that exists as a full name in old.
    new["days"][day][0]["teacher"] = "Петров"
    new["days"][day][0]["room"] = "999"
    diff = monitor.diff_schedules(old, new)
    assert len(diff["changed"]) == 1
    fields = {label: (old_v, new_v) for label, old_v, new_v in diff["changed"][0]["fields"]}
    assert fields["преподаватель"][1] == "Петров П. П."


def test_diff_enriches_with_external_lookup():
    """A teacher_lookup from a persistent cache enriches names not in either schedule."""
    old = _schedule()
    new = _copy(old)
    day = list(new["days"])[0]
    new["days"][day][0]["teacher"] = "Сидоров"
    new["days"][day][0]["room"] = "999"
    lookup = {"Сидоров": "Сидоров Сидор Сидорович"}
    diff = monitor.diff_schedules(old, new, teacher_lookup=lookup)
    fields = {label: (old_v, new_v) for label, old_v, new_v in diff["changed"][0]["fields"]}
    assert fields["преподаватель"][1] == "Сидоров Сидор Сидорович"


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
    assert "Поменяли расписание" in calls["fields"]["text"]
    assert "добавили пары (2 пары)" in calls["fields"]["text"]


def test_run_once_no_change_does_not_post(monkeypatch, tmp_path):
    html = Path("tests/fixtures/rowspan_time.html").read_text(encoding="utf-8")
    monkeypatch.setattr(monitor.config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(monitor, "fetch_html", lambda *a, **k: html)
    posts: list = []
    monkeypatch.setattr(monitor, "_post_changes_to_channel",
                        lambda diff, weeks, **kwargs: posts.append(diff) or {"ok": True})
    dms: list = []
    monkeypatch.setattr(monitor, "_dm", lambda text: dms.append(text))

    first = monitor.run_once()
    assert first["changed"] is False  # первый прогон — нет прошлого отпечатка
    second = monitor.run_once()
    assert second["changed"] is False  # контент не менялся
    assert posts == []
    assert dms == []


def test_run_once_updates_dashboard_on_baseline_and_unchanged_cycles(monkeypatch, tmp_path):
    """Calendar rollover is checked even when the portal HTML never changes."""
    html = Path("tests/fixtures/rowspan_time.html").read_text(encoding="utf-8")
    monkeypatch.setattr(monitor.config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(monitor, "fetch_html", lambda *args, **kwargs: html)
    dashboards = []
    monkeypatch.setattr(
        monitor,
        "_try_dashboard",
        lambda message_id, post_date, source, fingerprint, data, **kwargs:
            dashboards.append((message_id, fingerprint)) or True,
    )
    posts = []
    monkeypatch.setattr(
        monitor, "_post_changes_to_channel",
        lambda *args, **kwargs: posts.append(args) or {"ok": True},
    )

    first = monitor.run_once(update_post_id=3)
    second = monitor.run_once(update_post_id=3)

    assert first["dashboard_updated"] is True
    assert second["dashboard_updated"] is True
    assert len(dashboards) == 2
    assert dashboards[0] == dashboards[1]
    assert posts == []


def test_run_once_posts_diff_to_channel_on_change(monkeypatch, tmp_path):
    html1 = Path("tests/fixtures/rowspan_time.html").read_text(encoding="utf-8")
    html2 = html1.replace("Предмет Б", "Предмет Ц")
    pages = iter([html1, html2])
    monkeypatch.setattr(monitor.config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(monitor, "fetch_html", lambda *a, **k: next(pages))
    posts: list = []
    monkeypatch.setattr(monitor, "_post_changes_to_channel",
                        lambda diff, weeks, **kwargs: posts.append(diff) or {"ok": True})
    dms: list = []
    monkeypatch.setattr(monitor, "_dm", lambda text: dms.append(text))

    monitor.run_once()
    result = monitor.run_once()
    assert result["changed"] is True
    assert len(posts) == 1
    diff = posts[0]
    # Переименование пары в том же слоте — одна правка поля «предмет», а не
    # «убрали» + «добавили»: пара никуда не делась, у неё сменилось название.
    assert diff["added"] == [] and diff["removed"] == []
    changed = diff["changed"]
    assert len(changed) == 1
    assert [field[0] for field in changed[0]["fields"]] == ["предмет"]
    _, was, now_subject = changed[0]["fields"][0]
    assert "Предмет Б" in was and "Предмет Ц" in now_subject
    # контекст пары остаётся в записи: пост печатает где и кто ведёт
    assert changed[0]["room"] == "102"
    assert changed[0]["teacher"] == "Петров П. П."
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
                        lambda diff, weeks, **kwargs: posts.append(diff) or {"ok": True})

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
                        lambda diff, weeks, **kwargs: posts.append(diff) or {"ok": True})
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
    monkeypatch.setattr(monitor, "_post_changes_to_channel", lambda diff, weeks, **kwargs: posts.append(diff) or next(results))
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
    monkeypatch.setattr(monitor, "_post_changes_to_channel", lambda diff, weeks, **kwargs: posts.append(diff) or {"ok": True})

    monitor.run_once()
    candidate = monitor.run_once()
    confirmed = monitor.run_once()
    assert candidate["pending_confirmation"] is True
    assert candidate["changed"] is False
    assert confirmed["changed"] is True
    assert posts[0]["transition"] == "vanished"


def _day_shots(tmp_path, labels):
    shots = []
    for label in labels:
        path = tmp_path / f"{label}.png"
        path.write_bytes(b"png")
        shots.append({"label": label, "path": str(path)})
    return shots


def _tuesday_diff():
    return {
        "added": [{"day": "Вторник", "time": "09:00 10:00", "subject": "(лек.) Физика",
                   "room": "101", "teacher": "Иванов"}],
        "removed": [],
        "changed": [],
        "transition": None,
    }


def test_post_changes_attaches_screens_of_changed_days(monkeypatch, tmp_path):
    before = tmp_path / "before.png"
    after = tmp_path / "after.png"
    before.write_bytes(b"png")
    after.write_bytes(b"png")
    monkeypatch.setattr(monitor, "comparison_day_screens", lambda old, new, diff: [
        {"label": "Вт", "before_path": str(before), "after_path": str(after)},
    ])
    sent = []

    def fake_send(rich, **kwargs):
        sent.append((rich, kwargs))
        return {"ok": True, "result": {"message_id": 7}}

    monkeypatch.setattr(monitor, "send_rich_message", fake_send)
    result = monitor._post_changes_to_channel(
        _tuesday_diff(), [], html="<new>", old_html="<old>", fingerprint="fp",
    )
    assert result["ok"] is True
    assert len(sent) == 1
    rich, kwargs = sent[0]
    assert kwargs.get("files") is None
    assert "photo" not in json.dumps(rich, ensure_ascii=False)


def test_post_changes_retries_without_screens_when_media_rejected(monkeypatch, tmp_path):
    shot = tmp_path / "new.png"
    shot.write_bytes(b"png")
    monkeypatch.setattr(monitor, "comparison_day_screens", lambda *args: [
        {"label": "Вт", "before_path": None, "after_path": str(shot)},
    ])
    sent = []

    def fake_send(rich, **kwargs):
        sent.append((rich, kwargs))
        return {"ok": True, "result": {"message_id": 9}}

    monkeypatch.setattr(monitor, "send_rich_message", fake_send)
    result = monitor._post_changes_to_channel(
        _tuesday_diff(), [], html="<new>", old_html="<old>", fingerprint="fp",
    )
    assert result["ok"] is True
    assert len(sent) == 1
    assert sent[0][1].get("files") is None


def test_pending_media_edits_delivered_message_and_clears_queue(monkeypatch, tmp_path):
    before = tmp_path / "before.png"
    after = tmp_path / "after.png"
    before.write_bytes(b"png")
    after.write_bytes(b"png")
    monkeypatch.setattr(monitor.config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(monitor.timetable_api, "load_snapshot_html", lambda snapshot_id, **kwargs: f"html-{snapshot_id}")
    monkeypatch.setattr(monitor, "comparison_day_screens", lambda old, new, diff: [
        {"label": "Вт", "before_path": str(before), "after_path": str(after)},
    ])
    edits = []
    monkeypatch.setattr(monitor, "edit_rich_message", lambda rich, **kwargs: edits.append((rich, kwargs)) or {"ok": True})
    monitor._queue_media_enhancement(
        {"ok": True, "_rich": True, "result": {"message_id": 9}}, _tuesday_diff(), [], 1, 2,
    )
    assert (tmp_path / "pending_media.json").exists()
    assert monitor._try_pending_media() is True
    assert not (tmp_path / "pending_media.json").exists()
    assert edits[0][1]["files"] == {"changes_before_1": before, "changes_after_1": after}
    blob = json.dumps(edits[0][0], ensure_ascii=False)
    assert "Было" in blob and "Стало" in blob
    assert '"type": "slideshow"' in blob


def test_post_changes_survives_screenshot_render_failure(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise RuntimeError("chromium screenshot failed")

    monkeypatch.setattr(monitor, "comparison_day_screens", boom)
    sent = []

    def fake_send(rich, **kwargs):
        sent.append((rich, kwargs))
        return {"ok": True, "result": {"message_id": 11}}

    monkeypatch.setattr(monitor, "send_rich_message", fake_send)
    result = monitor._post_changes_to_channel(
        _tuesday_diff(), [], html="<new>", old_html="<old>", fingerprint="fp",
    )
    assert result["ok"] is True
    assert sent[0][1].get("files") is None
    assert "photo" not in json.dumps(sent[0][0], ensure_ascii=False)


def test_post_changes_without_html_skips_screens(monkeypatch):
    called = []
    monkeypatch.setattr(monitor, "comparison_day_screens", lambda *a, **k: called.append(a) or [])
    sent = []
    monkeypatch.setattr(monitor, "send_rich_message",
                        lambda rich, **kwargs: sent.append(kwargs) or {"ok": True, "result": {"message_id": 12}})
    result = monitor._post_changes_to_channel(_tuesday_diff(), [])
    assert result["ok"] is True
    assert called == []
    assert sent[0].get("files") is None


def test_comparison_screen_media_maps_old_and_new_files(monkeypatch, tmp_path):
    diff = {
        "added": [{"day": "Среда", "time": "9:00 10:00", "subject": "Новая пара", "room": "101"}],
        "removed": [], "changed": [], "transition": None,
    }
    before = tmp_path / "before.png"
    after = tmp_path / "after.png"
    monkeypatch.setattr(monitor, "comparison_day_screens", lambda old, new, diff_arg: [
        {"label": "Ср", "before_path": str(before), "after_path": str(after),
         "before_kinds": [], "after_kinds": ["added"]},
    ])
    media, files = monitor._comparison_screen_media(diff, "<old>", "<new>")
    assert media[0]["before_media"] == "attach://changes_before_1"
    assert media[0]["after_media"] == "attach://changes_after_1"
    assert media[0]["before_kinds"] == []
    assert media[0]["after_kinds"] == ["added"]
    assert files == {"changes_before_1": before, "changes_after_1": after}


def test_pending_media_keeps_task_after_renderer_failure_then_retries(monkeypatch, tmp_path):
    monkeypatch.setattr(monitor.config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(monitor.timetable_api, "load_snapshot_html", lambda *args, **kwargs: "<snapshot>")
    task = {"message_id": 19, "diff": _tuesday_diff(), "weeks": [],
            "old_snapshot_id": 1, "new_snapshot_id": 2}
    pending = tmp_path / "pending_media.json"
    pending.write_text(json.dumps({"items": [task]}), encoding="utf-8")
    attempts = []

    def render(*args):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("renderer unavailable")
        return [{"label": "Вт", "before_media": "attach://before", "before_kinds": ["removed"]}], {"before": tmp_path / "before.png"}

    monkeypatch.setattr(monitor, "_comparison_screen_media", render)
    edits = []
    monkeypatch.setattr(monitor, "edit_rich_message", lambda *args, **kwargs: edits.append(kwargs) or {"ok": True})
    assert monitor._try_pending_media() is False
    assert json.loads(pending.read_text())["items"] == [task]
    assert edits == []
    assert monitor._try_pending_media() is True
    assert not pending.exists()
    assert len(edits) == 1 and edits[0]["message_id"] == 19


def test_fingerprint_change_without_diff_is_reported_not_swallowed(monkeypatch, tmp_path):
    """Отпечаток уехал, а дифф пустой — монитор обязан пожаловаться, а не молчать.

    Так выглядит правка в поле, которого нет в _DIFF_FIELDS (например link),
    или потеря правки парсером: раньше монитор молча сдвигал базу.
    """
    html = Path("tests/fixtures/rowspan_time.html").read_text(encoding="utf-8")
    monkeypatch.setattr(monitor.config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(monitor, "fetch_html", lambda *a, **k: html)
    fingerprints = iter(["fp-old", "fp-new"])
    monkeypatch.setattr(monitor, "content_fingerprint", lambda *a, **k: next(fingerprints))
    posts = []
    monkeypatch.setattr(monitor, "_post_changes_to_channel",
                        lambda diff, weeks, **kwargs: posts.append(diff) or {"ok": True})
    dms = []
    monkeypatch.setattr(monitor, "_dm", lambda text: dms.append(text))

    monitor.run_once()
    result = monitor.run_once()

    assert result.get("unexplained") is True
    # в канал пустой дифф не летит
    assert posts == []
    # зато в личку летит предупреждение с уликой
    assert len(dms) == 1
    assert "дифф пустой" in dms[0]
    evidence = json.loads((tmp_path / "unexplained_change.json").read_text(encoding="utf-8"))
    assert evidence["previous_fingerprint"] == "fp-old"
    assert evidence["event"]["fingerprint"] == "fp-new"
    assert evidence["new_schedule"]
    # база сдвинулась, иначе монитор жаловался бы на одно и то же вечно
    state = json.loads((tmp_path / "monitor_state.json").read_text(encoding="utf-8"))
    assert state["event"]["fingerprint"] == "fp-new"


def test_stale_pending_is_not_posted_twice(monkeypatch, tmp_path):
    """Если портал уехал дальше, старый недоставленный дифф не дублируем."""
    html1 = Path("tests/fixtures/rowspan_time.html").read_text(encoding="utf-8")
    html2 = html1.replace("Предмет Б", "Предмет Ц")
    html3 = html2.replace("Предмет А", "Предмет Э")
    pages = iter([html1, html2, html3])
    monkeypatch.setattr(monitor.config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(monitor, "fetch_html", lambda *a, **k: next(pages))
    results = iter([{"ok": False, "error": "down"}, {"ok": True}])
    posts = []
    monkeypatch.setattr(monitor, "_post_changes_to_channel",
                        lambda diff, weeks, **kwargs: posts.append(diff) or next(results))
    monkeypatch.setattr(monitor, "_dm", lambda text: None)

    monitor.run_once()
    failed = monitor.run_once()
    recovered = monitor.run_once()

    assert failed["delivery_pending"] is True
    assert recovered["posted"] is True
    # ровно два поста: неудачный и один свежий, включающий обе правки
    assert len(posts) == 2
    subjects = {
        str(item.get("subject") or "")
        for key in ("added", "removed", "changed")
        for item in posts[1].get(key) or []
    }
    assert any("Предмет Ц" in name for name in subjects)
    assert any("Предмет Э" in name for name in subjects)
    assert not (tmp_path / "pending_notification.json").exists()


def test_dm_is_silenced_for_manual_runs_but_loud_in_production(monkeypatch, capsys):
    """Ручной прогон при кодинге не пишет в личку; боевой цикл — пишет."""
    sent = []

    import urllib.request as urlreq

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(req, timeout=0):
        sent.append(req.data)
        return _Resp()

    monkeypatch.setattr(urlreq, "urlopen", fake_urlopen)

    monkeypatch.setenv("NOVSU_DM_SILENT", "1")
    monitor._dm("аварийный алерт")
    assert sent == []
    assert "[dm silent]" in capsys.readouterr().err

    monkeypatch.delenv("NOVSU_DM_SILENT", raising=False)
    monitor._dm("аварийный алерт")
    assert len(sent) == 1


def test_pending_media_drops_terminal_error_and_processes_next(monkeypatch, tmp_path):
    """Мёртвый пост не должен душить очередь: задача снимается одним алертом,
    следующая в очереди обрабатывается в том же цикле."""
    before = tmp_path / "before.png"
    after = tmp_path / "after.png"
    before.write_bytes(b"png")
    after.write_bytes(b"png")
    monkeypatch.setattr(monitor.config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(monitor.timetable_api, "load_snapshot_html", lambda snapshot_id, **kwargs: f"html-{snapshot_id}")
    monkeypatch.setattr(monitor, "comparison_day_screens", lambda old, new, diff: [
        {"label": "Вт", "before_path": str(before), "after_path": str(after)},
    ])

    def fake_edit(rich, **kwargs):
        if kwargs.get("message_id") == 19:
            return {"ok": False, "description": "Bad Request: message to edit not found"}
        return {"ok": True}

    monkeypatch.setattr(monitor, "edit_rich_message", fake_edit)
    dms = []
    monkeypatch.setattr(monitor, "_dm", lambda text: dms.append(text))
    monitor._queue_media_enhancement({"ok": True, "_rich": True, "result": {"message_id": 19}}, _tuesday_diff(), [], 1, 2)
    monitor._queue_media_enhancement({"ok": True, "_rich": True, "result": {"message_id": 20}}, _tuesday_diff(), [], 1, 2)
    assert monitor._try_pending_media() is True
    assert not (tmp_path / "pending_media.json").exists(), "обе задачи должны уйти из очереди"
    assert len(dms) == 1, f"алерт ровно один: {dms}"
    assert "19" in dms[0] and "снята" in dms[0]


def test_pending_media_transient_error_still_blocks_head(monkeypatch, tmp_path):
    """Transient-ошибка (сеть) по-прежнему оставляет задачу в голове очереди."""
    before = tmp_path / "before.png"
    after = tmp_path / "after.png"
    before.write_bytes(b"png")
    after.write_bytes(b"png")
    monkeypatch.setattr(monitor.config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(monitor.timetable_api, "load_snapshot_html", lambda snapshot_id, **kwargs: f"html-{snapshot_id}")
    monkeypatch.setattr(monitor, "comparison_day_screens", lambda old, new, diff: [
        {"label": "Вт", "before_path": str(before), "after_path": str(after)},
    ])
    monkeypatch.setattr(monitor, "edit_rich_message",
                        lambda rich, **kwargs: {"ok": False, "description": "connection timeout"})
    dms = []
    monkeypatch.setattr(monitor, "_dm", lambda text: dms.append(text))
    monitor._queue_media_enhancement({"ok": True, "_rich": True, "result": {"message_id": 19}}, _tuesday_diff(), [], 1, 2)
    assert monitor._try_pending_media() is False
    state = json.loads((tmp_path / "pending_media.json").read_text(encoding="utf-8"))
    assert [item["message_id"] for item in state["items"]] == [19]
    assert dms == [], "transient-ошибка алертов не требует"
