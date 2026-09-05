import copy
import os
import subprocess
import sys
from pathlib import Path

import pytest

import config
import fetch
from parse import parse_all

SCHEDULE_HTML = Path("tests/fixtures/rowspan_time.html").read_text(encoding="utf-8")
STUB_HTML = Path("tests/fixtures/stub_with_xls_timestamps.html").read_text(encoding="utf-8")


def test_config_import_does_not_require_telegram_secrets():
    env = dict(os.environ)
    for name in ("TG_BOT_TOKEN", "TG_CHANNEL_ID", "TG_DM_TARGET"):
        env.pop(name, None)
    result = subprocess.run(
        [sys.executable, "-c", "import config; print(config.GROUP_URL)"],
        cwd=Path.cwd(), env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "page=EditViewGroup" in result.stdout


def test_random_ua_is_full_browser_ua():
    for _ in range(20):
        ua = config.random_ua()
        assert ua.startswith("Mozilla/5.0")
        assert "Chrome/" in ua or "Edg/" in ua


def test_fetch_html_falls_back_to_desktop_ua_after_failure(monkeypatch):
    monkeypatch.setattr(fetch, "RETRY_DELAY_S", 0.0)
    used: list[str] = []

    def fake_run(cmd, **kwargs):
        used.append(cmd[cmd.index("-A") + 1])
        if len(used) == 1:
            raise subprocess.CalledProcessError(22, cmd)

        class R:
            stdout = SCHEDULE_HTML
        return R()

    monkeypatch.setattr(fetch.subprocess, "run", fake_run)
    assert fetch.fetch_html("https://example.test") == SCHEDULE_HTML
    assert len(used) == 2
    assert used[0] == config.IPHONE_UA
    assert used[1] != config.IPHONE_UA
    assert "Chrome/" in used[1] or "Edg/" in used[1]


def test_fetch_html_uses_fail_and_iphone_safari_headers(monkeypatch):
    seen_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        seen_cmd.extend(cmd)

        class R:
            stdout = SCHEDULE_HTML
        return R()

    monkeypatch.setattr(fetch.subprocess, "run", fake_run)
    fetch.fetch_html("https://example.test")
    joined = " ".join(seen_cmd)
    assert "--fail" in seen_cmd
    for header in (
        "Accept:", "Accept-Language:", "Sec-Fetch-Dest:",
        "Sec-Fetch-Mode:", "Sec-Fetch-Site:", "Upgrade-Insecure-Requests:",
    ):
        assert header in joined
    assert "Sec-CH-UA:" not in joined
    assert config.IPHONE_UA in seen_cmd


def test_fetch_html_raises_after_all_retries(monkeypatch):
    monkeypatch.setattr(fetch, "RETRY_DELAY_S", 0.0)

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(22, cmd)

    monkeypatch.setattr(fetch.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="failed after"):
        fetch.fetch_html("https://example.test")


def test_fetch_retries_http_200_waf_body(monkeypatch):
    monkeypatch.setattr(fetch, "RETRY_DELAY_S", 0.0)
    calls = 0

    def fake_run(cmd, **kwargs):
        nonlocal calls
        calls += 1

        class R:
            stdout = "<html><title>Access denied</title><body>captcha</body></html>"
        return R()

    monkeypatch.setattr(fetch.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="neither a timetable nor a portal stub"):
        fetch.fetch_html("https://example.test", retries=2)
    assert calls == 2


def test_structural_validation_accepts_real_stub_but_not_generic_html():
    assert fetch.validate_portal_response(STUB_HTML) == "stub"
    with pytest.raises(fetch.PortalResponseError):
        fetch.validate_portal_response("<html><body>maintenance</body></html>")


def test_index_has_its_own_structural_validator():
    html = '<a href="?page=EditViewGroup&instId=1&name=6381&type=DO&year=2023">6381</a>'
    assert fetch.validate_portal_response(html, "index") == "index"


def test_content_fingerprint_ignores_volatile_xls_timestamps():
    base = SCHEDULE_HTML
    a = base + '\n<a href="tt.xls" title="17.08.2026 03:17:30">xls</a>'
    b = base + '\n<a href="tt.xls" title="18.08.2026 11:45:02">xls</a>'
    assert fetch.hash_html(a) != fetch.hash_html(b)
    assert fetch.content_fingerprint(a) == fetch.content_fingerprint(b)


def test_content_fingerprint_accepts_parse_all_data():
    assert fetch.content_fingerprint(SCHEDULE_HTML) == fetch.content_fingerprint(parse_all(SCHEDULE_HTML))


def test_content_fingerprint_ignores_dom_metadata_and_neutral_order():
    data = parse_all(SCHEDULE_HTML)
    data["weeks"] = [
        {"week": 2, "half": "bottom", "start": "08.09.2026", "end": "14.09.2026"},
        {"week": 1, "half": "top", "start": "01.09.2026", "end": "07.09.2026"},
    ]
    data["schedule"]["days"]["Понедельник"] = []
    reordered = copy.deepcopy(data)
    reordered["schedule"]["days"] = dict(reversed(list(reordered["schedule"]["days"].items())))
    rows = next(rows for rows in reordered["schedule"]["days"].values() if rows)
    rows.reverse()
    for index, lesson in enumerate(rows, 900):
        lesson["source_row"] = index
        lesson["number"] = index
        lesson["raw_comment"] = f"raw-{index}"
        lesson["raw_room"] = f"raw-room-{index}"
    reordered["weeks"].reverse()
    assert fetch.content_fingerprint(data) == fetch.content_fingerprint(reordered)


def test_content_fingerprint_keeps_meaningful_lesson_fields():
    data = parse_all(SCHEDULE_HTML)
    changed = copy.deepcopy(data)
    next(iter(changed["schedule"]["days"].values()))[0]["teacher"] = "Другой"
    assert fetch.content_fingerprint(data) != fetch.content_fingerprint(changed)


def test_content_fingerprint_changes_when_lesson_changes():
    changed = SCHEDULE_HTML.replace("Предмет Б", "Предмет Ц")
    assert fetch.content_fingerprint(SCHEDULE_HTML) != fetch.content_fingerprint(changed)


def test_is_stub_true_for_stub_page_with_xls_timestamps():
    assert fetch.is_stub(STUB_HTML) is True


def test_is_stub_false_for_real_schedule():
    assert fetch.is_stub(SCHEDULE_HTML) is False


def test_is_stub_rejects_error_page_instead_of_calling_it_stub():
    with pytest.raises(fetch.PortalResponseError):
        fetch.is_stub("<html>server error</html>")


def test_content_fingerprint_sees_live_nested_first_lesson_change():
    html = """<table><tr><th>дата</th><th>время</th><th>под гр.</th><th>предмет</th><th>преподаватель</th><th>ауд.</th><th>комм.</th></tr>
    <tr><td rowspan="2">Пн</td><tr><td>09:00</td><td></td><td>Первая</td><td>Иванов</td><td>101</td><td></td></tr></tr>
    </table>"""
    assert fetch.content_fingerprint(html) != fetch.content_fingerprint(html.replace("Первая", "Изменена"))
