#!/usr/bin/env python3
"""Hourly health check for the NovSU timetable monitor.

Checks:
  1. systemd service is active
  2. monitor_state.json was updated in the last 20 minutes
  3. no Python tracebacks in recent journal output

Sends a DM alert on failure, a recovery message when things go green again.
State persisted to state/healthcheck_status.json so we only alert once per incident.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# --- Config (no imports needed) ---
def _env(name: str) -> str:
    """Read a secret from the environment (systemd EnvironmentFile) or .env."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    try:
        for line in (Path(__file__).resolve().parent / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                if key.strip() == name:
                    return val.strip()
    except OSError:
        pass
    return ""


BOT_TOKEN = _env("TG_BOT_TOKEN")
DM_TARGET = _env("TG_DM_TARGET")
SERVICE = "novsu-timetable-monitor.service"
API_SERVICE = "novsu-timetable-api.service"
API_HEALTH_URL = "http://127.0.0.1:8787/health"
STATE_DIR = Path(__file__).resolve().parent / "state"
STATE_FILE = STATE_DIR / "healthcheck_status.json"
MAX_SILENCE_MINUTES = 30  # includes the monitor's capped 20-minute error backoff


def _dm(text: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": DM_TARGET,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        print(f"[healthcheck] DM failed: {exc}", file=sys.stderr)


def _systemctl(*args: str) -> str:
    try:
        return subprocess.run(
            ["systemctl", *args],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception as exc:
        return f"[error: {exc}]"


def _state_file_age() -> float | None:
    """Seconds since monitor_state.json was last written. None if file missing."""
    import datetime
    try:
        path = STATE_DIR / "monitor_state.json"
        if not path.exists():
            return None
        mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime)
        return (datetime.datetime.now() - mtime).total_seconds()
    except Exception:
        return None


def _journal_has_traceback() -> str | None:
    """Return the traceback text if found in recent journal output, else None."""
    try:
        result = subprocess.run(
            ["journalctl", "-u", SERVICE, "--no-pager", "-n", "100", "-o", "cat"],
            capture_output=True, text=True, timeout=10,
        )
        output = result.stdout
        if "Traceback" in output:
            # Grab the traceback block
            idx = output.rfind("Traceback")
            return output[idx:idx + 500]
        return None
    except Exception:
        return None


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _api_health_ok() -> tuple[bool, str]:
    """Check local API health endpoint. Returns (ok, detail)."""
    try:
        req = urllib.request.Request(API_HEALTH_URL, method="GET")
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read().decode())
        if data.get("ok"):
            return True, "ok"
        return False, f"health returned ok=false: {data.get('problems', [])}"
    except Exception as exc:
        return False, f"API unreachable: {exc}"


def check() -> dict:
    """Run all checks. Returns dict with 'ok' bool and 'problems' list."""
    problems: list[str] = []

    # 1. Monitor service active?
    active = _systemctl("is-active", SERVICE)
    if active != "active":
        problems.append(f"monitor service {active!r} (not active)")

    # 2. API service active?
    api_active = _systemctl("is-active", API_SERVICE)
    if api_active != "active":
        problems.append(f"API service {api_active!r} (not active)")
    else:
        # 2b. API health endpoint responds?
        api_ok, api_detail = _api_health_ok()
        if not api_ok:
            problems.append(f"API health: {api_detail}")

    # 3. State file freshness?
    age = _state_file_age()
    if age is not None and age > MAX_SILENCE_MINUTES * 60:
        problems.append(f"state file stale for {int(age / 60)} min (monitor not running?)")
    elif age is None:
        problems.append("monitor_state.json missing")

    # 4. Tracebacks?
    tb = _journal_has_traceback()
    if tb:
        problems.append(f"traceback in logs:\n{tb[:300]}")

    return {"ok": len(problems) == 0, "problems": problems}


def _problems_fingerprint(problems: list[str]) -> str:
    """Stable hash of problem set for deduplication."""
    import hashlib
    canonical = "\n".join(sorted(problems))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def main() -> int:
    state = _load_state()
    was_ok = state.get("ok", True)
    prev_fingerprint = state.get("fingerprint", "")
    result = check()
    is_ok = result["ok"]
    current_fingerprint = _problems_fingerprint(result["problems"]) if result["problems"] else ""

    if not is_ok:
        # Alert only on OK->FAIL transition or significant problem set change.
        # Avoids sending the same alert every hour during a sustained incident.
        if was_ok or (current_fingerprint != prev_fingerprint):
            msg = "🔴 novsu monitor проблем:\n" + "\n".join(result["problems"])
            _dm(msg)
            print(msg, file=sys.stderr)
        else:
            # Sustained incident with unchanged problems — log but don't alert.
            print(f"[healthcheck] sustained incident, no alert (fingerprint={current_fingerprint})", file=sys.stderr)
    elif not was_ok:
        # Recovery: FAIL -> OK
        msg = "🟢 novsu monitor: восстановился, всё ок"
        _dm(msg)
        print(msg)

    _save_state({
        "ok": is_ok,
        "fingerprint": current_fingerprint,
        "last_check": __import__("datetime").datetime.now().isoformat(),
    })
    return 0 if is_ok else 1


if __name__ == "__main__":
    sys.exit(main())
