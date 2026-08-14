# Конфиг для бота расписания группы 6381 НовГУ
# Не хранить токены в MEMORY — только ссылки на .env

import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
STATE_DIR.mkdir(exist_ok=True)

# Загружаем .env если есть
_env_file = ROOT / ".env"
if _env_file.exists():
    for line in _env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

# URL группы 6381 (Институт экономики, Туризм)
GROUP_URL = (
    "https://portal.novsu.ru/univer/timetable/ochn/i.1103357/"
    "?page=EditViewGroup&name=6381&type=%D0%92%D0%9E&year=2026&instId=1786977"
)

# Telegram
TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_CHANNEL_ID = int(os.environ["TG_CHANNEL_ID"])  # -1004256784811
TG_DM_TARGET = int(os.environ["TG_DM_TARGET"])    # Георгий: 7912629458

# Chrome UA — обязательно реальный Chrome, иначе novsu.ru режет (python-UA режутся)
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

# Системный chromium (snap)
CHROMIUM_BIN = "/usr/bin/chromium-browser"
