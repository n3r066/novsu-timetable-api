#!/bin/sh
set -eu
SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
UNIT_NAME=novsu-timetable-monitor.service
API_UNIT=novsu-timetable-api.service
HEALTHCHECK_SERVICE=novsu-timetable-healthcheck.service
HEALTHCHECK_TIMER=novsu-timetable-healthcheck.timer
install -m 0644 "$SOURCE_DIR/deploy/$UNIT_NAME" "/etc/systemd/system/$UNIT_NAME"
install -m 0644 "$SOURCE_DIR/deploy/$API_UNIT" "/etc/systemd/system/$API_UNIT"
install -m 0644 "$SOURCE_DIR/deploy/$HEALTHCHECK_SERVICE" "/etc/systemd/system/$HEALTHCHECK_SERVICE"
install -m 0644 "$SOURCE_DIR/deploy/$HEALTHCHECK_TIMER" "/etc/systemd/system/$HEALTHCHECK_TIMER"
systemctl daemon-reload
systemctl enable "$UNIT_NAME"
systemctl enable --now "$API_UNIT"
systemctl enable --now "$HEALTHCHECK_TIMER"
systemctl disable --now novsu-timetable-rollover.timer 2>/dev/null || true
printf '%s
' "Installed and enabled $UNIT_NAME. Start explicitly after checking .env:"   "  systemctl start $UNIT_NAME"   "  systemctl status $UNIT_NAME"
