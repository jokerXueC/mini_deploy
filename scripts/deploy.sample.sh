#!/usr/bin/env bash
set -Eeuo pipefail

# Generic sample deploy script.
# Copy this file into your project and replace the TODO block with your real build/restart commands.

PROJECT_DIR="${PROJECT_DIR:-$(pwd)}"
BRANCH="${DEPLOY_BRANCH:-main}"
LOG_FILE="${DEPLOY_LOG_FILE:-/var/log/vibepilot/sample-deploy.log}"
HEALTH_URL="${HEALTH_URL:-}"

mkdir -p "$(dirname "$LOG_FILE")"

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"
}

cd "$PROJECT_DIR"
log "deploy begin project_dir=$PROJECT_DIR branch=$BRANCH action=${DEPLOY_ACTION:-deploy}"
log "phase=starting prepare deploy workspace"

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  log "phase=fetch git fetch origin $BRANCH"
  git fetch origin "$BRANCH"
  log "phase=pull git checkout and fast-forward pull"
  git checkout "$BRANCH"
  git pull --ff-only origin "$BRANCH"
fi

log "phase=docker_build run project build/restart commands"
log "TODO: run your build/restart commands here"

# Examples:
# npm ci && npm run build && pm2 restart ecosystem.config.js
# mvn clean package -DskipTests && systemctl restart my-java-service
# go build -o app ./cmd/server && systemctl restart my-go-service
# docker compose up -d --build

if [[ -n "$HEALTH_URL" ]]; then
  log "phase=health_check health check: $HEALTH_URL"
  log "health check: $HEALTH_URL"
  curl -fsS --max-time 10 "$HEALTH_URL" | tee -a "$LOG_FILE"
fi

log "phase=finished deploy completed"
log "deploy done"
