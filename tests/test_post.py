import copy

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
