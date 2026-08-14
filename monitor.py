"""Ежедневный мониторинг изменений страницы группы 6381.

Логика:
1. fetch_html → sha256
2. сравнить с предыдущим из state/last.json
3. если изменилось:
   - дописать событие в state/changes.jsonl (utc_ts, hash, size, stub)
   - отправить Георгию алерт в личку через бота
4. обновить state/last.json

Запуск:
  python3 monitor.py                # один прогон
  python3 monitor.py --schedule     # раз в день в 09:00 Europe/Moscow (для cron)

Этот скрипт НЕ публикует в канал — только следит и алертит.
Публикация — post.py.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

import config  # noqa: E402
from fetch import fetch_html, hash_html, is_stub  # noqa: E402


def _dm(text: str) -> None:
    """Короткое сообщение Георгию в личку."""
    import urllib.request
    import urllib.parse

    url = f"https://api.telegram.org/bot{config.TG_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": str(config.TG_DM_TARGET),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read())
            if not body.get("ok"):
                print(f"[dm FAIL] {body}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[dm ERR] {e}", file=sys.stderr)


def run_once() -> dict:
    """Один прогон. Возвращает dict {changed: bool, hash: str, stub: bool}."""
    last_file = config.STATE_DIR / "last.json"
    changes_log = config.STATE_DIR / "changes.jsonl"

    last = {}
    if last_file.exists():
        try:
            last = json.loads(last_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            last = {}

    html = fetch_html()
    new_hash = hash_html(html)
    stub = is_stub(html)
    size = len(html)

    event = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "hash": new_hash,
        "size": size,
        "stub": stub,
    }

    prev_hash = last.get("hash")
    changed = prev_hash is not None and prev_hash != new_hash

    # Всегда дописываем событие (полная история мониторинга = "тихие" записи тоже)
    with changes_log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

    last_file.write_text(
        json.dumps(
            {"hash": new_hash, "size": size, "stub": stub, "ts": event["ts"]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if changed:
        prev_stub = last.get("stub", True)
        if prev_stub and not stub:
            kind = "появилось расписание (заглушка → реальная таблица)"
        elif not prev_stub and stub:
            kind = "⚠️ расписание пропало (была таблица → заглушка)"
        else:
            kind = "обновилось содержимое расписания"
        msg = (
            f"🔔 <b>Расписание 6381: {kind}</b>\n"
            f"ts: {event['ts']}\n"
            f"hash: <code>{prev_hash}</code> → <code>{new_hash}</code>\n"
            f"size: {last.get('size', '?')} → {size}\n"
            f"<a href=\"{config.GROUP_URL}\">открыть портал</a>"
        )
        _dm(msg)
        print(f"[changed] {kind}")
    else:
        print(f"[no change] hash={new_hash} stub={stub}")

    return {"changed": changed, "hash": new_hash, "stub": stub, **event}


def schedule_daily(hour: int = 9, minute: int = 0):
    """Простой scheduler: спим до 09:00 Europe/Moscow каждый день."""
    import zoneinfo
    tz = zoneinfo.ZoneInfo("Europe/Moscow")
    while True:
        now = dt.datetime.now(tz)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += dt.timedelta(days=1)
        sleep_s = (target - now).total_seconds()
        print(f"[sleep {sleep_s:.0f}s until {target.isoformat()}]")
        time.sleep(sleep_s)
        try:
            run_once()
        except Exception as e:  # noqa: BLE001
            print(f"[run ERR] {e}", file=sys.stderr)
            _dm(f"⚠️ monitor.run_once failed: <code>{e}</code>")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--schedule", action="store_true", help="вечный scheduler раз в день 09:00 MSK")
    args = ap.parse_args()

    if args.schedule:
        schedule_daily()
    else:
        result = run_once()
        print(json.dumps(result, ensure_ascii=False))
