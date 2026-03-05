#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="$BASE_DIR/.venv/bin/python"
LOG_DIR="$BASE_DIR/logs"
POST_XML_PATH="$BASE_DIR/post.xml"
POST_XML_RELATIVE_PATH="post.xml"
ICLOUD_DIR="$HOME/Library/Mobile Documents/com~apple~CloudDocs/daily-posts"
ICLOUD_POST_XML_PATH="$ICLOUD_DIR/post.xml"

# Allow overrides via environment variables when needed.
FEED_LINK="${FEED_LINK:-https://xyz5020.github.io/daily-posts/}"
FEED_SELF_LINK="${FEED_SELF_LINK:-https://xyz5020.github.io/daily-posts/post.xml}"

mkdir -p "$LOG_DIR"
RUN_LOG="$LOG_DIR/update_post_xml_$(date '+%Y-%m-%d_%H-%M-%S').log"
exec > >(tee -a "$RUN_LOG") 2>&1

echo "[info] $(date '+%Y-%m-%d %H:%M:%S') start update_post_xml"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "[error] Python executable not found: $PYTHON_BIN"
  exit 1
fi

cd "$BASE_DIR"

"$PYTHON_BIN" scripts/run_full_pipeline.py \
  --allow-fallback \
  --skip-ai-if-no-key \
  --pretty \
  --feed-title "Daily Tech 摘要" \
  --feed-link "$FEED_LINK" \
  --feed-description "每日技术文章摘要" \
  --feed-self-link "$FEED_SELF_LINK" \
  --feed-output "$POST_XML_PATH" \
  --publish-path "$POST_XML_PATH" \
  --skip-opml

mkdir -p "$ICLOUD_DIR"
cp "$POST_XML_PATH" "$ICLOUD_POST_XML_PATH"
echo "[info] synced to iCloud: $ICLOUD_POST_XML_PATH"

# Commit and push only post.xml, ignoring unrelated staged/unstaged files.
git add -- "$POST_XML_RELATIVE_PATH"
if ! git diff --cached --quiet -- "$POST_XML_RELATIVE_PATH"; then
  COMMIT_MSG="chore: update post.xml ($(date -u '+%Y-%m-%dT%H:%M:%SZ'))"
  git commit --only -m "$COMMIT_MSG" -- "$POST_XML_RELATIVE_PATH"
  git push origin main
  echo "[info] pushed update to origin/main"
else
  echo "[info] post.xml unchanged, skip commit/push"
fi

echo "[info] $(date '+%Y-%m-%d %H:%M:%S') finished update_post_xml"
