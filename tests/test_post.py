import copy
from pathlib import Path

import post


def _parsed(stub=False):
    return {
        "stub": stub,
        "weeks": [{"week": 1, "half": "top", "start": "01.09.2026", "end": "06.09.2026"}],
        "schedule": {
            "days": {
                "Понедельник": [{
                    "number": 1,
                    "subject": "Математика",
                    "time": "09:00 10:30",
                    "room": "101",
                    "teacher": "Иванов",
                    "note": "",
                    "source_row": 3,
                }],
            },
            "raw": [["volatile markup text"]],
        },
    }


def test_semantic_fingerprint_ignores_parser_diagnostics_and_row_order():
    first = _parsed()
    second = copy.deepcopy(first)
    second["schedule"]["raw"] = [["other raw text"]]
    second["schedule"]["days"]["Понедельник"][0]["source_row"] = 999
    second["schedule"]["days"]["Понедельник"].append({
        "number": 2,
        "subject": "Физика",
        "time": "11:00",
        "room": "102",
        "teacher": "Петров",
        "note": "",
    })
    reordered = copy.deepcopy(second)
    reordered["schedule"]["days"]["Понедельник"].reverse()
    assert post._semantic_fingerprint(second) == post._semantic_fingerprint(reordered)
    assert post._semantic_fingerprint(first) != post._semantic_fingerprint(second)


def test_post_full_failure_does_not_advance_last_post(monkeypatch):
    data = _parsed(stub=True)
    monkeypatch.setattr(post, "fetch_html", lambda: "volatile html")
    monkeypatch.setattr(post, "hash_html", lambda html: "raw-hash")
    monkeypatch.setattr(post, "parse_all", lambda html: data)
    monkeypatch.setattr(post, "_read_last_post", lambda: {})
    monkeypatch.setattr(post, "_tg_api", lambda *args, **kwargs: {"ok": False, "description": "down"})
    writes = []
    monkeypatch.setattr(post, "_write_last_post", writes.append)

    result = post.post_to_channel()

    assert result["posted"] is False
    assert result["complete"] is False
    assert result["partial"] is False
    assert result["reason"] == "send_failed"
    assert writes == []


def test_post_partial_fallback_failure_is_explicit_and_retryable(monkeypatch):
    data = _parsed(stub=False)
    monkeypatch.setattr(post, "fetch_html", lambda: "html")
    monkeypatch.setattr(post, "hash_html", lambda html: "raw-hash")
    monkeypatch.setattr(post, "parse_all", lambda html: data)
    monkeypatch.setattr(post, "_read_last_post", lambda: {})
    monkeypatch.setattr(post, "render_schedule_html", lambda *args, **kwargs: "<html></html>")
    monkeypatch.setattr(post, "screenshot_html", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("no chromium")))
    monkeypatch.setattr(post, "format_schedule_post", lambda *args, **kwargs: ["part 1", "part 2"])
    replies = iter([
        {"ok": True, "result": {"message_id": 10}},
        {"ok": False, "description": "down"},
    ])
    monkeypatch.setattr(post, "_tg_api", lambda *args, **kwargs: next(replies))
    writes = []
    monkeypatch.setattr(post, "_write_last_post", writes.append)

    result = post.post_to_channel()

    assert result["posted"] is True
    assert result["complete"] is False
    assert result["partial"] is True
    assert result["reason"] == "partial_failure"
    assert result["message_ids"] == [10]
    assert result["failures"][0]["index"] == 1
    assert writes == []


def test_post_complete_fallback_stores_semantic_fingerprint_and_msk(monkeypatch):
    data = _parsed(stub=False)
    monkeypatch.setattr(post, "fetch_html", lambda: "html")
    monkeypatch.setattr(post, "hash_html", lambda html: "raw-hash")
    monkeypatch.setattr(post, "parse_all", lambda html: data)
    monkeypatch.setattr(post, "_read_last_post", lambda: {})
    monkeypatch.setattr(post, "render_schedule_html", lambda *args, **kwargs: "<html></html>")
    monkeypatch.setattr(post, "screenshot_html", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("no chromium")))
    monkeypatch.setattr(post, "format_schedule_post", lambda *args, **kwargs: ["part 1", "part 2"])
    replies = iter([
        {"ok": True, "result": {"message_id": 10}},
        {"ok": True, "result": {"message_id": 11}},
    ])
    monkeypatch.setattr(post, "_tg_api", lambda *args, **kwargs: next(replies))
    writes = []
    monkeypatch.setattr(post, "_write_last_post", writes.append)

    result = post.post_to_channel()

    assert result["posted"] is True
    assert result["complete"] is True
    assert result["partial"] is False
    assert len(writes) == 1
    assert writes[0]["fingerprint"] == post._semantic_fingerprint(data)
    assert writes[0]["timezone"] == "Europe/Moscow"
    assert writes[0]["ts"].endswith("+03:00")


def test_post_deduplicates_by_semantic_fingerprint(monkeypatch):
    data = _parsed(stub=False)
    fingerprint = post._semantic_fingerprint(data)
    monkeypatch.setattr(post, "fetch_html", lambda: "different volatile html")
    monkeypatch.setattr(post, "hash_html", lambda html: "different-raw-hash")
    monkeypatch.setattr(post, "parse_all", lambda html: data)
    monkeypatch.setattr(post, "_read_last_post", lambda: {"fingerprint": fingerprint, "hash": "old-raw-hash"})
    monkeypatch.setattr(post, "_tg_api", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not send")))

    result = post.post_to_channel()

    assert result["posted"] is False
    assert result["reason"] == "same_fingerprint"


def test_photo_caption_is_complete_html_and_bounded():
    caption = post._photo_caption("https://example.test?a=1&b=2")
    assert len(caption) <= 1024
    assert caption.endswith("</blockquote>")
    assert "&amp;" in caption


def test_dashboard_screens_cached_by_fingerprint(monkeypatch, tmp_path):
    """Скрины пересоздаются только при смене fingerprint расписания."""
    calls = []

    def fake_crops(source_html, source_url, out_png):
        calls.append(out_png)
        out_png = Path(out_png)
        out_png.parent.mkdir(parents=True, exist_ok=True)
        out_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 50)
        return [{"label": "Пн", "path": str(out_png)}]

    monkeypatch.setattr(post, "screenshot_schedule_day_crop_items", fake_crops)
    monkeypatch.setattr(post, "build_dashboard_rich_message", lambda *a, **k: {"rich_message": {"blocks": []}})
    monkeypatch.setattr(post, "edit_rich_message", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(post, "content_fingerprint", lambda data: "FP-1")
    monkeypatch.setattr(post, "parse_all", lambda html: {"schedule": {"days": {}}, "weeks": []})

    state = tmp_path / "state"
    monkeypatch.setattr(post.config, "STATE_DIR", state)

    import datetime as dt
    day = dt.date(2026, 9, 2)
    post.edit_dashboard_post(1, day, html="html-A", fingerprint="FP-1")
    post.edit_dashboard_post(1, day, html="html-B", fingerprint="FP-1")
    assert len(calls) == 1, f"chromium должен был гоняться 1 раз, а не {len(calls)}"

    post.edit_dashboard_post(1, day, html="html-C", fingerprint="FP-2")
    assert len(calls) == 2, f"при смене расписания скрины должны пересоздаться (calls={len(calls)})"


def test_dashboard_screens_recreated_when_cache_file_missing(monkeypatch, tmp_path):
    """Если кеш-файл утерян, скрины рендерятся заново даже при том же fingerprint."""
    calls = []

    def fake_crops(source_html, source_url, out_png):
        calls.append(out_png)
        out_png = Path(out_png)
        out_png.parent.mkdir(parents=True, exist_ok=True)
        out_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 50)
        return [{"label": "Пн", "path": str(out_png)}]

    monkeypatch.setattr(post, "screenshot_schedule_day_crop_items", fake_crops)
    monkeypatch.setattr(post, "build_dashboard_rich_message", lambda *a, **k: {"rich_message": {"blocks": []}})
    monkeypatch.setattr(post, "edit_rich_message", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(post, "content_fingerprint", lambda data: "FP-1")
    monkeypatch.setattr(post, "parse_all", lambda html: {"schedule": {"days": {}}, "weeks": []})

    state = tmp_path / "state"
    monkeypatch.setattr(post.config, "STATE_DIR", state)
    import datetime as dt
    day = dt.date(2026, 9, 2)
    post.edit_dashboard_post(1, day, html="html-A", fingerprint="FP-1")
    # Удаляем кеш-файл, имитируя утерю состояния.
    (state / "dashboard_screens.json").unlink()
    post.edit_dashboard_post(1, day, html="html-B", fingerprint="FP-1")
    assert len(calls) == 2


def _diff_for_screens():
    return {
        "added": [{"day": "Среда", "time": "9:00 10:00", "subject": "Новая пара", "room": "101", "teacher": "Иванов"}],
        "removed": [{"day": "Четверг", "time": "9:00 10:00", "subject": "Старая пара", "room": "102", "teacher": "Петров"}],
        "changed": [{"day": "Понедельник", "time": "11:00 12:00", "subject": "Та же пара",
                     "fields": [["ауд.", "101", "202"]]}],
        "transition": None,
    }


def test_diff_day_screens_marks_only_visible_changes_and_caches(monkeypatch, tmp_path):
    import datetime as dt

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(post.config, "STATE_DIR", state)
    calls = []

    def fake_crop_items(html, url, out_png, *, diff_items=None, diff_legend="", only_labels=None):
        calls.append({"diff_items": diff_items, "only_labels": only_labels, "legend": diff_legend})
        shot_dir = Path(out_png).parent
        shot_dir.mkdir(parents=True, exist_ok=True)
        items = []
        for index, label in enumerate(sorted(only_labels or set()), 1):
            path = Path(str(Path(out_png).with_suffix("")) + f"_part{index:02d}.png")
            path.write_bytes(b"png")
            items.append({"label": label, "path": path, "marked": 1 if label != "Чт" else 0})
        return items

    monkeypatch.setattr(post, "screenshot_schedule_day_crop_items", fake_crop_items)

    diff = _diff_for_screens()
    items = post.diff_day_screens("<html>", diff, dt.date(2026, 9, 3), fingerprint="fp1")

    # Скрин дня нужен и там, где пару убрали: подсвечивать нечего, но день показываем.
    assert calls[0]["only_labels"] == {"Пн", "Ср", "Чт"}
    # Помечаются только добавленные и изменённые — убранной пары в новом HTML нет.
    assert [item["subject"] for item in calls[0]["diff_items"]] == ["Та же пара", "Новая пара"]
    assert calls[0]["legend"]
    assert sorted(item["label"] for item in items) == ["Пн", "Ср", "Чт"]
    assert (state / "diff_screens.json").exists()

    # Тот же отпечаток и тот же дифф — кеш, chromium повторно не дёргаем.
    again = post.diff_day_screens("<html>", diff, dt.date(2026, 9, 3), fingerprint="fp1")
    assert len(calls) == 1
    assert again == items

    # Другой дифф — другая подпись: рендерим заново и подчищаем старые файлы.
    other = copy.deepcopy(diff)
    other["added"][0]["subject"] = "Совсем другая пара"
    fresh = post.diff_day_screens("<html>", other, dt.date(2026, 9, 3), fingerprint="fp1")
    assert len(calls) == 2
    left = sorted(p.name for p in (state / "dashboard_screens").glob("6381_diff_*.png"))
    assert left == sorted(Path(item["path"]).name for item in fresh)
