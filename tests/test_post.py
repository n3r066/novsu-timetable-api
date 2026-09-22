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

    monkeypatch.setattr(post, "_cached_day_screens", lambda html, **kwargs: fake_crops(html, "", tmp_path / "cached.png"))
    monkeypatch.setattr(post, "build_dashboard_rich_message", lambda *a, **k: {"rich_message": {"blocks": []}})
    monkeypatch.setattr(post, "edit_rich_message", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(post, "content_fingerprint", lambda data: "FP-1")
    data = {
        "schedule": {"days": {"Среда": [{"subject": "Пара", "time": "09:00"}]}},
        "weeks": [{"week": 1, "half": "top", "start": "31.08.2026", "end": "05.09.2026"}],
    }
    monkeypatch.setattr(post, "parse_all", lambda html: data)

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

    monkeypatch.setattr(post, "_cached_day_screens", lambda html, **kwargs: fake_crops(html, "", tmp_path / "cached.png"))
    monkeypatch.setattr(post, "build_dashboard_rich_message", lambda *a, **k: {"rich_message": {"blocks": []}})
    monkeypatch.setattr(post, "edit_rich_message", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(post, "content_fingerprint", lambda data: "FP-1")
    data = {
        "schedule": {"days": {"Среда": [{"subject": "Пара", "time": "09:00"}]}},
        "weeks": [{"week": 1, "half": "top", "start": "31.08.2026", "end": "05.09.2026"}],
    }
    monkeypatch.setattr(post, "parse_all", lambda html: data)

    state = tmp_path / "state"
    monkeypatch.setattr(post.config, "STATE_DIR", state)
    import datetime as dt
    day = dt.date(2026, 9, 2)
    post.edit_dashboard_post(1, day, html="html-A", fingerprint="FP-1")
    # Удаляем кеш-файл, имитируя утерю состояния.
    (state / "dashboard_screens.json").unlink()
    post.edit_dashboard_post(1, day, html="html-B", fingerprint="FP-1")
    assert len(calls) == 2


def test_dashboard_can_update_text_without_waiting_for_screens(monkeypatch, tmp_path):
    import datetime as dt

    state = tmp_path / "state"
    monkeypatch.setattr(post.config, "STATE_DIR", state)
    data = {"schedule": {"days": {}}, "weeks": []}
    monkeypatch.setattr(post, "_dashboard_screens", lambda *args: (_ for _ in ()).throw(AssertionError("no chromium")))
    monkeypatch.setattr(post, "build_dashboard_rich_message", lambda *args, **kwargs: {"rich_message": {"blocks": []}})
    sent = []
    monkeypatch.setattr(post, "edit_rich_message", lambda *args, **kwargs: sent.append(kwargs) or {"ok": True})
    result = post.edit_dashboard_post(
        1,
        html="html",
        fingerprint="fp",
        data=data,
        now=dt.datetime(2026, 9, 5, 22, 0),
        render_screens=False,
    )
    assert result["ok"] is True
    assert sent[0]["files"] == {}


def test_dashboard_retries_media_after_text_only_rollover(monkeypatch, tmp_path):
    """A successful text-only edit must not suppress media recovery next cycle."""
    import datetime as dt
    import json

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(post.config, "STATE_DIR", state)
    data = {
        "schedule": {"days": {"Понедельник": [
            {"subject": "Нижняя", "time": "09:00 10:00", "room": "101", "note": "по нижней неделе"},
        ]}},
        "weeks": [{"week": 2, "half": "bottom", "start": "07.09.2026", "end": "12.09.2026"}],
    }
    sent = []
    monkeypatch.setattr(post, "build_dashboard_rich_message", lambda *args, **kwargs: {"rich_message": {"blocks": []}})
    monkeypatch.setattr(post, "edit_rich_message", lambda *args, **kwargs: sent.append(kwargs) or {"ok": True})
    screen = tmp_path / "week2.png"
    screen.write_bytes(b"png" * 500)
    monkeypatch.setattr(post, "_dashboard_screens", lambda *args, **kwargs: [{"label": "Пн", "path": str(screen)}])
    now = dt.datetime(2026, 9, 6, 12, 0)

    text_only = post.edit_dashboard_post(
        3, html="same-html", fingerprint="same-fp", data=data,
        now=now, render_screens=False,
    )
    with_media = post.edit_dashboard_post(
        3, html="same-html", fingerprint="same-fp", data=data,
        now=now, render_screens=True,
    )

    assert text_only["ok"] is True and with_media["ok"] is True
    assert len(sent) == 2
    assert sent[0]["files"] == {}
    assert list(sent[1]["files"]) == ["site_screenshot_1"]
    saved = json.loads((state / "last_dashboard_post.json").read_text())
    assert saved["target_date"] == "2026-09-07"
    assert saved["screens_complete"] is True


def test_dashboard_does_not_accept_previous_week_screen_cache(monkeypatch, tmp_path):
    """Same timetable fingerprint does not make upper-week PNG valid for lower week."""
    import datetime as dt
    import json

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(post.config, "STATE_DIR", state)
    stale = tmp_path / "week1.png"
    stale.write_bytes(b"png" * 500)
    (state / "dashboard_screens.json").write_text(json.dumps({
        "fingerprint": "same-fp",
        "status_key": post._dashboard_screen_status_key(dt.date(2026, 9, 1)),
        "items": [{"label": "Пн", "path": str(stale)}],
    }))
    data = {
        "schedule": {"days": {"Понедельник": [
            {"subject": "Нижняя", "time": "09:00 10:00", "room": "101", "note": "по нижней неделе"},
        ]}},
        "weeks": [{"week": 2, "half": "bottom", "start": "07.09.2026", "end": "12.09.2026"}],
    }
    fresh = tmp_path / "week2.png"
    fresh.write_bytes(b"png" * 500)
    calls = []
    monkeypatch.setattr(post, "_dashboard_screens", lambda *args, **kwargs: calls.append(args) or [{"label": "Пн", "path": str(fresh)}])
    monkeypatch.setattr(post, "build_dashboard_rich_message", lambda *args, **kwargs: {"rich_message": {"blocks": []}})
    monkeypatch.setattr(post, "edit_rich_message", lambda *args, **kwargs: {"ok": True})

    result = post.edit_dashboard_post(
        3, html="same-html", fingerprint="same-fp", data=data,
        now=dt.datetime(2026, 9, 6, 12, 0),
    )

    assert result["ok"] is True
    assert len(calls) == 1


def test_dashboard_without_expected_screens_is_idempotent(monkeypatch, tmp_path):
    import datetime as dt

    state = tmp_path / "state"
    monkeypatch.setattr(post.config, "STATE_DIR", state)
    data = {"stub": True, "schedule": None, "weeks": []}
    monkeypatch.setattr(post, "_dashboard_screens", lambda *args: (_ for _ in ()).throw(AssertionError("no chromium")))
    monkeypatch.setattr(post, "build_dashboard_rich_message", lambda *args, **kwargs: {"rich_message": {"blocks": []}})
    sent = []
    monkeypatch.setattr(post, "edit_rich_message", lambda *args, **kwargs: sent.append(kwargs) or {"ok": True})
    now = dt.datetime(2026, 12, 1, 12, 0)
    assert post.edit_dashboard_post(1, html="stub", fingerprint="fp", data=data, now=now)["ok"] is True
    again = post.edit_dashboard_post(1, html="stub", fingerprint="fp", data=data, now=now)
    assert again == {"ok": True, "skipped": True, "reason": "same_presentation"}
    assert len(sent) == 1


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

    def fake_crop_items(html, url, out_png, *, diff_items=None, diff_legend="", only_labels=None,
                        diff_removed_note=True):
        calls.append({"diff_items": diff_items, "only_labels": only_labels, "legend": diff_legend,
                      "removed_note": diff_removed_note})
        shot_dir = Path(out_png).parent
        shot_dir.mkdir(parents=True, exist_ok=True)
        items = []
        for index, label in enumerate(sorted(only_labels or set()), 1):
            path = Path(str(Path(out_png).with_suffix("")) + f"_part{index:02d}.png")
            path.write_bytes(b"png")
            entry = {"label": label, "path": path, "marked": 1 if label != "Чт" else 0}
            if label == "Пн":
                entry["marked_kinds"] = ["changed"]
            items.append(entry)
        return items

    monkeypatch.setattr(post, "screenshot_schedule_day_crop_items", fake_crop_items)

    diff = _diff_for_screens()
    items = post.diff_day_screens("<html>", diff, dt.date(2026, 9, 3), fingerprint="fp1")

    # Скрин дня нужен и там, где пару убрали: подсвечивать нечего, но день показываем.
    assert calls[0]["only_labels"] == {"Пн", "Ср", "Чт"}
    # Помечаются только добавленные и изменённые — убранной пары в новом HTML нет.
    assert [item["subject"] for item in calls[0]["diff_items"]] == ["Та же пара", "Новая пара"]
    assert calls[0]["legend"]
    # В диффе есть убранные пары — строка про них на скрине нужна.
    assert calls[0]["removed_note"] is True
    # Типы помеченных правок проходят насквозь: подпись поста обещает только их.
    by_label = {item["label"]: item for item in items}
    assert by_label["Пн"]["marked_kinds"] == ["changed"]
    assert "marked_kinds" not in by_label["Ср"]
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


def test_comparison_day_screens_cache_by_rendered_day_html(monkeypatch, tmp_path):
    from PIL import Image
    from bs4 import BeautifulSoup

    state = tmp_path / "state"
    monkeypatch.setattr(post.config, "STATE_DIR", state)
    rendered = []
    source = Path("tests/fixtures/rowspan_time.html").read_text(encoding="utf-8")
    changed = source.replace("<td>102</td>", "<td>303</td>")

    def fake_screenshot(html, path, **kwargs):
        rendered.append(html)
        path.parent.mkdir(parents=True, exist_ok=True)
        assert kwargs == {"width": 1480, "height": 3200, "scale_factor": 2, "bg": (255, 255, 255)}
        Image.effect_noise((160, 200 if "Было" in html else 240), 30).convert("RGB").save(path)
        return path

    monkeypatch.setattr(post, "screenshot_html", fake_screenshot)
    diff = {"added": [], "removed": [], "changed": [{
        "day": "Четверг", "subject": "(пр.) Предмет Б", "time": "15:00 16:00",
        "fields": [["ауд.", "102", "303"]],
    }]}

    first = post.comparison_day_screens(source, changed, diff)
    second = post.comparison_day_screens(source, changed, diff)

    assert len(first) == 1 and first == second
    assert len(rendered) == 2
    before, after = [BeautifulSoup(html, "html.parser") for html in rendered]
    assert before.select_one(".comparison-heading").text == "Было · Чт"
    assert after.select_one(".comparison-heading").text == "Стало · Чт"
    assert before.colgroup == after.colgroup
    assert [cell.text for cell in before.select(".comparison-changed")] == ["102"]
    assert [cell.text for cell in after.select(".comparison-changed")] == ["303"]
    assert first[0]["before_kinds"] == first[0]["after_kinds"] == ["changed"]
    for side in ("before", "after"):
        with Image.open(first[0][f"{side}_path"]) as image:
            assert image.size == (160, 240)
    assert not (state / "dashboard_screens.json").exists()

    # A different annotation on identical HTML cannot reuse the old image.
    other = {"added": [], "changed": [], "removed": [{
        "day": "Четверг", "subject": "(пр.) Предмет Б", "time": "15:00 16:00",
    }]}
    assert post.comparison_day_screens(source, changed, other) != first
    assert len(rendered) == 4
    monkeypatch.setattr(post, "COMPARISON_SCREEN_STYLE_VERSION", 99)
    assert post.comparison_day_screens(source, changed, diff) != first
    assert len(rendered) == 6


def test_comparison_renderer_failure_propagates_for_media_retry(monkeypatch, tmp_path):
    import pytest

    monkeypatch.setattr(post.config, "STATE_DIR", tmp_path)
    source = Path("tests/fixtures/rowspan_time.html").read_text(encoding="utf-8")

    def fail(*args, **kwargs):
        raise RuntimeError("renderer unavailable")

    monkeypatch.setattr(post, "screenshot_html", fail)
    with pytest.raises(RuntimeError, match="renderer unavailable"):
        post.comparison_day_screens(source, source, {"removed": [{"day": "Четверг"}]})


def test_comparison_cache_recovers_from_partial_png(monkeypatch, tmp_path):
    import pytest
    from PIL import Image

    monkeypatch.setattr(post.config, "STATE_DIR", tmp_path)
    source = Path("tests/fixtures/rowspan_time.html").read_text(encoding="utf-8")
    calls = []

    def render(html, path, **kwargs):
        calls.append(path)
        if len(calls) == 1:
            path.write_bytes(b"partial PNG" * 200)
            raise RuntimeError("renderer interrupted")
        Image.effect_noise((160, 240), 30).convert("RGB").save(path)

    monkeypatch.setattr(post, "screenshot_html", render)
    diff = {"removed": [{"day": "Четверг"}]}
    with pytest.raises(RuntimeError, match="renderer interrupted"):
        post.comparison_day_screens(source, source, diff)
    pairs = post.comparison_day_screens(source, source, diff)
    assert len(calls) == 3
    # A corrupt pre-existing final cache file is also replaced on the next try.
    Path(pairs[0]["before_path"]).write_bytes(b"bad cached PNG" * 200)
    assert post.comparison_day_screens(source, source, diff) == pairs
    assert len(calls) == 4


def test_screenshot_statuses_use_selected_week_and_date():
    import datetime as dt

    data = {
        "weeks": [{"week": 2, "half": "bottom", "start": "07.09.2026", "end": "12.09.2026"}],
        "schedule": {"days": {"Понедельник": [
            {"subject": "Верхняя", "time": "9:00 10:00", "room": "101", "note": "по верхней неделе", "delivery_mode": "in_person"},
            {"subject": "Нижняя ДОТ", "time": "11:00 12:00", "room": "—", "note": "по нижней неделе с использованием ДОТ", "delivery_mode": "remote_or_hybrid"},
            {"subject": "С 14.09", "time": "14:00 15:00", "room": "303", "note": "с 14.09", "delivery_mode": "in_person"},
            {"subject": "С 9 недели", "time": "15:00 16:00", "room": "305", "note": "с 9 недели", "delivery_mode": "in_person"},
            {"subject": "С 9 недели ДОТ", "time": "17:00 18:00", "room": "—", "note": "с 9 недели, с использованием ДОТ", "delivery_mode": "remote_or_hybrid"},
            {"subject": "После 9 недели", "time": "18:00 19:00", "room": "306", "note": "после 9 недели", "delivery_mode": "in_person"},
            {"subject": "Нижняя очно", "time": "16:00 17:00", "room": "304", "note": "по нижней неделе", "delivery_mode": "in_person"},
        ]}},
    }
    items = post._schedule_screenshot_statuses(data, dt.date(2026, 9, 7))["Пн"]
    by_subject = {item["subject"]: item for item in items}
    # Badges carry the short display text; the full machine reason stays in _schedule_struct.
    assert by_subject["Верхняя"]["_schedule_tag"] == "ВЕРХНЯЯ НЕДЕЛЯ"
    assert by_subject["Верхняя"]["_schedule_struct"]["machine_reason"] == "НЕ НА ЭТОЙ НЕДЕЛЕ · ТОЛЬКО ВЕРХНЯЯ"
    assert by_subject["Верхняя"]["_schedule_struct"]["kind"] == "parity"
    assert by_subject["Нижняя ДОТ"]["_schedule_status"] == "dot"
    assert by_subject["С 14.09"]["_schedule_tag"] == "С 14.09"
    assert by_subject["С 14.09"]["_schedule_struct"]["kind"] == "starts_date"
    assert by_subject["С 9 недели"]["_schedule_tag"] == "С 9-Й НЕДЕЛИ"
    assert by_subject["С 9 недели"]["_schedule_struct"]["machine_reason"] == "НЕ НА ЭТОЙ НЕДЕЛЕ · С 9‑Й НЕДЕЛИ"
    assert by_subject["С 9 недели ДОТ"]["_schedule_status"] == "inactive"
    assert by_subject["С 9 недели ДОТ"]["_schedule_tag"] == "С 9-Й НЕДЕЛИ"
    assert by_subject["После 9 недели"]["_schedule_tag"] == "С 10-Й НЕДЕЛИ"
    assert by_subject["После 9 недели"]["_schedule_struct"]["kind"] == "after_week"
    assert "Нижняя очно" not in by_subject


def test_schedule_status_css_semantics():
    """Inactive content is struck; reason badges and applicable DOT remain clear."""
    from portal_parser import SCHEDULE_STATUS_CSS

    inactive_block = SCHEDULE_STATUS_CSS.split("tr.schedule-inactive .schedule-status-content")[1].split("}")[0]
    assert "line-through" in inactive_block
    assert "text-decoration-thickness: 2px" in inactive_block
    assert "opacity: 1" in inactive_block
    dot_block = SCHEDULE_STATUS_CSS.split("tr.schedule-dot > td,")[1].split("}")[0]
    assert "line-through" not in dot_block
    badge_block = SCHEDULE_STATUS_CSS.split("  .schedule-status-tag {")[1].split("}")[0]
    assert "text-decoration: none !important" in badge_block

    # Cancelled rows get their own class with a red accent and strike-through.
    assert "tr.schedule-cancelled" in SCHEDULE_STATUS_CSS
    cancelled_block = SCHEDULE_STATUS_CSS.split("tr.schedule-cancelled .schedule-status-content")[1].split("}")[0]
    assert "line-through" in cancelled_block
    assert "#c1440e" in SCHEDULE_STATUS_CSS  # red accent for cancelled

    # Inactive badge is muted blue, separate from DOT's green tint.
    inactive_tag = SCHEDULE_STATUS_CSS.split("tr.schedule-inactive .schedule-status-tag")[1].split("}")[0]
    assert "#d8e2ee" in inactive_tag
    assert "#5b7fa6" not in inactive_tag


def test_mark_schedule_status_uses_display_text_and_cancelled_class():
    """Badges show the short display_text; cancelled rows get the red class."""
    from bs4 import BeautifulSoup
    from portal_parser import _mark_schedule_status

    row_html = (
        '<tr><td>Пн</td><td>09:00 10:00</td><td></td>'
        '<td>(лек.) Предмет</td><td>Иванов</td><td>101</td><td>с 9 недели</td></tr>'
    )

    # from_week: grey inactive badge with short text, no strike-through wrapper class.
    soup = BeautifulSoup(row_html, "html.parser")
    row = soup.find("tr")
    _mark_schedule_status(row, {
        "subject": "(лек.) Предмет",
        "_schedule_status": "inactive",
        "_schedule_tag": "НЕ НА ЭТОЙ НЕДЕЛЕ · С 9‑Й НЕДЕЛИ",
        "_schedule_struct": {
            "kind": "from_week",
            "display_text": "С 9-Й НЕДЕЛИ",
            "machine_reason": "НЕ НА ЭТОЙ НЕДЕЛЕ · С 9‑Й НЕДЕЛИ",
            "data": {"week_number": 9},
        },
    })
    assert "schedule-inactive" in row["class"]
    assert "schedule-cancelled" not in row["class"]
    tag = row.select_one(".schedule-status-tag")
    assert tag.text == "С 9-й недели"

    # cancelled: red class + ОТМЕНЕНО badge.
    soup = BeautifulSoup(row_html, "html.parser")
    row = soup.find("tr")
    _mark_schedule_status(row, {
        "subject": "(лек.) Предмет",
        "_schedule_status": "cancelled",
        "_schedule_tag": "ЗАНЯТИЯ НЕ БУДЕТ · 19.10",
        "_schedule_struct": {
            "kind": "cancelled",
            "display_text": "ОТМЕНЕНО 19.10",
            "machine_reason": "ЗАНЯТИЯ НЕ БУДЕТ · 19.10",
            "data": {"date": "2026-10-19"},
        },
    })
    assert "schedule-cancelled" in row["class"]
    tag = row.select_one(".schedule-status-tag")
    assert tag.text == "Отменено 19.10"

    # dot: unchanged blue class and ДОТ badge.
    soup = BeautifulSoup(row_html, "html.parser")
    row = soup.find("tr")
    _mark_schedule_status(row, {
        "subject": "(лек.) Предмет",
        "_schedule_status": "dot",
        "_schedule_tag": "ДОТ",
        "_schedule_struct": None,
    })
    assert "schedule-dot" in row["class"]
    tag = row.select_one(".schedule-status-tag")
    assert tag.text == "ДОТ"


def _rollover_stubs(monkeypatch, tmp_path, weeks, today):
    """Обвязка для проверки ролловера закрепа: chromium и Telegram не дёргаем."""
    import datetime as dt

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(post.config, "STATE_DIR", state)
    monkeypatch.setattr(post, "_today_msk", lambda: today)
    monkeypatch.setattr(post, "parse_all", lambda html: {"schedule": {"days": {}}, "weeks": weeks})
    monkeypatch.setattr(post, "content_fingerprint", lambda data: "FP-ROLL")
    monkeypatch.setattr(post, "edit_rich_message", lambda *a, **k: {"ok": True})

    def bounds(week):
        fmt = "%d.%m.%Y"
        return (dt.datetime.strptime(week["start"], fmt).date(), dt.datetime.strptime(week["end"], fmt).date())

    def fake_lessons(schedule, weeks_arg, target):
        for week in weeks:
            start, end = bounds(week)
            if start <= target <= end:
                return week, [{"subject": "Пара"}]
        # Воскресенье между неделями: пар нет, фокус уедет на понедельник.
        return None, []

    monkeypatch.setattr("schedule_logic.lessons_for_date", fake_lessons)

    def fake_crops(source_html, source_url, out_png, **kwargs):
        out = Path(out_png)
        out.parent.mkdir(parents=True, exist_ok=True)
        items = []
        for index, label in enumerate(["Пн", "Вт", "Ср", "Чт", "Пт", "Сб"], 1):
            path = Path(str(out.with_suffix("")) + f"_part{index:02d}.png")
            path.write_bytes(b"png")
            items.append({"label": label, "path": str(path)})
        return items

    monkeypatch.setattr(post, "_cached_day_screens", lambda html, **kwargs: fake_crops(html, "", tmp_path / "cached.png"))

    seen = {}

    def fake_build(schedule, weeks_arg, url, target_date, **kwargs):
        seen["target"] = target_date
        seen["current_date"] = kwargs.get("current_date")
        seen["media"] = [item["label"] for item in kwargs.get("screenshot_media") or []]
        return {"rich_message": {"blocks": []}}

    monkeypatch.setattr(post, "build_dashboard_rich_message", fake_build)

    def fake_edit(rich, *, token, chat_id, message_id, files=None, timeout=30):
        seen["files"] = sorted(files or {})
        return {"ok": True}

    monkeypatch.setattr(post, "edit_rich_message", fake_edit)
    return seen


def test_dashboard_rollover_attaches_only_live_days(monkeypatch, tmp_path):
    """Пятница: в закреплённый пост идут только скрины пт и сб."""
    import datetime as dt

    weeks = [{"week": 1, "half": "top", "start": "31.08.2026", "end": "05.09.2026"}]
    seen = _rollover_stubs(monkeypatch, tmp_path, weeks, dt.date(2026, 9, 4))

    result = post.edit_dashboard_post(1, dt.date(2026, 9, 4), html="html-A", fingerprint="FP-ROLL")

    assert result["ok"] is True
    assert seen["current_date"] == dt.date(2026, 9, 4)
    assert seen["media"] == ["Пт", "Сб"]
    assert seen["files"] == ["site_screenshot_1", "site_screenshot_2"]


def test_dashboard_rollover_switches_to_next_week_on_sunday(monkeypatch, tmp_path):
    """В воскресенье фокус уезжает на понедельник, и видна вся следующая неделя."""
    import datetime as dt

    weeks = [
        {"week": 1, "half": "top", "start": "31.08.2026", "end": "05.09.2026"},
        {"week": 2, "half": "bottom", "start": "07.09.2026", "end": "12.09.2026"},
    ]
    seen = _rollover_stubs(monkeypatch, tmp_path, weeks, dt.date(2026, 9, 6))

    post.edit_dashboard_post(1, dt.date(2026, 9, 6), html="html-A", fingerprint="FP-ROLL")

    assert seen["target"] == dt.date(2026, 9, 7)
    assert seen["media"] == ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб"]
    assert seen["files"] == [f"site_screenshot_{index}" for index in range(1, 7)]


def test_edit_dashboard_post_passes_opd_data_and_extends_fingerprint(monkeypatch, tmp_path):
    import datetime as dt
    import json as _json
    import zoneinfo

    import opd

    fixtures = Path("tests/fixtures")
    opd_data = opd.build_data(
        (fixtures / "opd_members.csv").read_text(encoding="utf-8"),
        (fixtures / "opd_doc.html").read_text(encoding="utf-8"),
        group="6381", today=dt.date(2026, 9, 17), fetched_at="2026-09-17T10:00:00+03:00",
    )
    captured: list[dict] = []

    def fake_builder(*args, **kwargs):
        captured.append({"args": args, "kwargs": kwargs})
        return {"rich_message": {"blocks": []}}

    monkeypatch.setattr(post, "build_dashboard_rich_message", fake_builder)
    monkeypatch.setattr(post, "edit_rich_message", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(post, "content_fingerprint", lambda data: "FP-1")
    monkeypatch.setattr(post.opd_module, "load_opd", lambda **kwargs: opd_data)
    data = {
        "schedule": {"days": {"Четверг": [
            {"number": 2, "subject": "(лек/пр.) Основы проектной деятельности", "time": "14:00 15:00",
             "room": ".", "teacher": "—"},
        ]}},
        "weeks": [{"week": 3, "half": "top", "start": "14.09.2026", "end": "19.09.2026"}],
    }
    monkeypatch.setattr(post, "parse_all", lambda html: data)
    state = tmp_path / "state"
    monkeypatch.setattr(post.config, "STATE_DIR", state)

    # 16:30 четверга: портальная пара ОПД кончилась в 15:45, но блок 16:00 ещё идёт.
    now = dt.datetime(2026, 9, 17, 16, 30, tzinfo=zoneinfo.ZoneInfo("Europe/Moscow"))
    result = post.edit_dashboard_post(1, None, html="html", fingerprint="FP-1", now=now, render_screens=False)
    assert result.get("ok")
    assert captured[-1]["kwargs"]["opd"] is opd_data
    assert captured[-1]["args"][3] == dt.date(2026, 9, 17)  # четверг остаётся фокусом
    with_opd = _json.loads((state / "last_dashboard_post.json").read_text(encoding="utf-8"))
    assert with_opd["target_date"] == "2026-09-17"

    # Без данных ОПД отпечаток другой: появление раздела перерисовывает закреп.
    monkeypatch.setattr(post.opd_module, "load_opd", lambda **kwargs: None)
    post.edit_dashboard_post(1, None, html="html", fingerprint="FP-1", now=now, render_screens=False)
    assert captured[-1]["kwargs"]["opd"] is None
    without = _json.loads((state / "last_dashboard_post.json").read_text(encoding="utf-8"))
    assert without["presentation_fingerprint"] != with_opd["presentation_fingerprint"]


def test_dashboard_uses_cached_html_when_fetch_fails(monkeypatch, tmp_path):
    """Порталь лёг на ролловере: закреп рисуется из кеша с пометкой о спячке."""
    import json

    captured = {}

    def fake_build(*args, **kwargs):
        captured.update(kwargs)
        return {"rich_message": {"blocks": []}}

    monkeypatch.setattr(post, "build_dashboard_rich_message", fake_build)
    monkeypatch.setattr(post, "edit_rich_message", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(post, "content_fingerprint", lambda data: "FP-SLEEPY")
    monkeypatch.setattr(post, "parse_all", lambda html: {
        "schedule": {"days": {"Среда": [{"subject": "Пара", "time": "09:00"}]}},
        "weeks": [{"week": 1, "half": "top", "start": "31.08.2026", "end": "05.09.2026"}],
    })
    monkeypatch.setattr(post, "_dashboard_screens", lambda *a, **k: [])
    monkeypatch.setattr(post, "fetch_html", lambda: (_ for _ in ()).throw(RuntimeError("portal down")))

    monkeypatch.setattr(post.config, "STATE_DIR", tmp_path)
    (tmp_path / "6381.html").write_text("<cached html>", encoding="utf-8")

    import datetime as dt
    result = post.edit_dashboard_post(1, dt.date(2026, 9, 2))
    assert result["ok"] is True
    assert "спячке" in captured.get("sleep_note", "")
    state = json.loads((tmp_path / "last_dashboard_post.json").read_text(encoding="utf-8"))
    assert state.get("sleepy") is True


def test_dashboard_raises_when_fetch_fails_without_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(post, "fetch_html", lambda: (_ for _ in ()).throw(RuntimeError("portal down")))
    monkeypatch.setattr(post.config, "STATE_DIR", tmp_path)

    import datetime as dt
    try:
        post.edit_dashboard_post(1, dt.date(2026, 9, 2))
    except RuntimeError as exc:
        assert "portal down" in str(exc)
    else:
        raise AssertionError("без кеша ролловер обязан упасть, а не рисовать пустоту")


def test_dashboard_recovers_from_sleepy_when_portal_wakes_up(monkeypatch, tmp_path):
    """После восстановления портала закреп перерисовывается без пометки спячки."""
    import json

    captured = {}
    edits = []

    def fake_build(*args, **kwargs):
        captured.update(kwargs)
        return {"rich_message": {"blocks": []}}

    monkeypatch.setattr(post, "build_dashboard_rich_message", fake_build)
    monkeypatch.setattr(post, "edit_rich_message", lambda *a, **k: edits.append(1) or {"ok": True})
    monkeypatch.setattr(post, "content_fingerprint", lambda data: "FP-1")
    data = {
        "schedule": {"days": {"Среда": [{"subject": "Пара", "time": "09:00"}]}},
        "weeks": [{"week": 1, "half": "top", "start": "31.08.2026", "end": "05.09.2026"}],
    }
    monkeypatch.setattr(post, "parse_all", lambda html: data)
    monkeypatch.setattr(post, "_dashboard_screens", lambda *a, **k: [])
    monkeypatch.setattr(post.config, "STATE_DIR", tmp_path)
    (tmp_path / "6381.html").write_text("<cached html>", encoding="utf-8")
    monkeypatch.setattr(post, "fetch_html",
                        lambda: (_ for _ in ()).throw(RuntimeError("portal down")))

    import datetime as dt
    day = dt.date(2026, 9, 2)
    post.edit_dashboard_post(1, day)  # fetch упал -> sleepy
    assert "спячке" in captured.get("sleep_note", "")
    monkeypatch.setattr(post, "fetch_html", lambda: "<fresh html>")
    post.edit_dashboard_post(1, day)  # портал жив -> обычный заголовок
    assert captured.get("sleep_note") == ""
    assert len(edits) == 2, "переход спячка -> норма обязан дать реальную правку закрепа"
