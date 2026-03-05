#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_PLIST="$BASE_DIR/launchd/com.xuying.daily-posts.update.plist"
TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET_PLIST="$TARGET_DIR/com.xuying.daily-posts.update.plist"
LABEL="com.xuying.daily-posts.update"
LOG_DIR="$BASE_DIR/logs"

mkdir -p "$TARGET_DIR" "$LOG_DIR"

if [[ ! -f "$SOURCE_PLIST" ]]; then
  echo "[error] missing source plist: $SOURCE_PLIST"
  exit 1
fi

cp "$SOURCE_PLIST" "$TARGET_PLIST"

# Reload the job so new config takes effect.
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$TARGET_PLIST"
launchctl enable "gui/$(id -u)/$LABEL"

echo "[ok] installed: $TARGET_PLIST"
echo "[ok] loaded label: $LABEL"
echo "[info] schedule: daily at 08:00 (local time)"
echo "[info] stdout log: $BASE_DIR/logs/launchd.update_post_xml.out.log"
echo "[info] stderr log: $BASE_DIR/logs/launchd.update_post_xml.err.log"
echo "[info] run now: launchctl kickstart -k gui/$(id -u)/$LABEL"
