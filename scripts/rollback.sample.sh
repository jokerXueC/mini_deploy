#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(pwd)}"
TARGET_SHA="${DEPLOY_AFTER:-}"
LOG_FILE="${DEPLOY_LOG_FILE:-/var/log/mini_deploy/sample-rollback.log}"

mkdir -p "$(dirname "$LOG_FILE")"

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"
}

cd "$PROJECT_DIR"
if [[ -z "$TARGET_SHA" ]]; then
  log "DEPLOY_AFTER is empty; rollback target is required"
  exit 1
fi

log "phase=pull rollback checkout target=$TARGET_SHA"
log "rollback to $TARGET_SHA"
git fetch --all --tags
git checkout --detach "$TARGET_SHA"

log "phase=docker_build rebuild and restart service"
log "TODO: rebuild and restart your service here"
log "phase=finished rollback completed"
