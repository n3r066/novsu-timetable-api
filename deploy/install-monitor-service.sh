#!/bin/sh
set -eu
SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
UNIT_NAME=novsu-timetable-monitor.service
ROLLOVER_UNIT=novsu-timetable-rollover.service
ROLLOVER_TIMER=novsu-timetable-rollover.timer
install -m 0644 "$SOURCE_DIR/deploy/$UNIT_NAME" "/etc/systemd/system/$UNIT_NAME"
# Полночный ролловер закрепа: таймер включает oneshot-сервис в 00:00 Europe/Moscow.
install -m 0644 "$SOURCE_DIR/deploy/$ROLLOVER_UNIT" "/etc/systemd/system/$ROLLOVER_UNIT"
install -m 0644 "$SOURCE_DIR/deploy/$ROLLOVER_TIMER" "/etc/systemd/system/$ROLLOVER_TIMER"
systemctl daemon-reload
systemctl enable "$UNIT_NAME"
systemctl enable --now "$ROLLOVER_TIMER"
printf '%s
' "Installed and enabled $UNIT_NAME. Start explicitly after checking .env:"   "  systemctl start $UNIT_NAME"   "  systemctl status $UNIT_NAME"
