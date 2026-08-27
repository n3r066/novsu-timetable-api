#!/bin/sh
set -eu
SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
UNIT_NAME=novsu-timetable-monitor.service
install -m 0644 "$SOURCE_DIR/deploy/$UNIT_NAME" "/etc/systemd/system/$UNIT_NAME"
systemctl daemon-reload
systemctl enable "$UNIT_NAME"
printf '%s
' "Installed and enabled $UNIT_NAME. Start explicitly after checking .env:"   "  systemctl start $UNIT_NAME"   "  systemctl status $UNIT_NAME"
