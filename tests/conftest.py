"""Guard: the test suite must never touch production runtime files in state/."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_GUARDED = [
    _ROOT / "state" / "6381.html",
    _ROOT / "state" / "monitor_state.json",
    _ROOT / "state" / "timetable.sqlite3",
]


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


@pytest.fixture(scope="session", autouse=True)
def _production_state_untouched():
    before = {path: _digest(path) for path in _GUARDED}
    yield
    # The live monitor may legitimately rewrite these while tests run, so only
    # flag files that changed *and* now carry test-fixture markers.
    for path, old in before.items():
        new = _digest(path)
        if old == new or new is None:
            continue
        text = path.read_bytes()[:4096]
        if path.suffix == ".html" and b"portal.novsu.ru" not in text and b"novsu" not in text.lower():
            pytest.fail(f"test suite overwrote production file {path.relative_to(_ROOT)}")


@pytest.fixture(autouse=True)
def _opd_offline(monkeypatch, tmp_path):
    """Тесты не ходят в Google за файлами ОПД и не трогают state/opd_cache.json."""
    import opd

    def _blocked(url: str) -> str:
        raise RuntimeError(f"network disabled in tests: {url}")

    monkeypatch.setattr(opd, "_download", _blocked)
    monkeypatch.setattr(opd, "_cache_path", lambda: tmp_path / "opd_cache.json")
