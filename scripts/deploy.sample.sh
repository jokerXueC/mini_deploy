#!/usr/bin/env bash
set -Eeuo pipefail

# Generic sample deploy script.
# Copy this file into your project and replace the TODO block with your real build/restart commands.

PROJECT_DIR="${PROJECT_DIR:-$(pwd)}"
BRANCH="${DEPLOY_BRANCH:-main}"
LOG_FILE="${DEPLOY_LOG_FILE:-/var/log/mini_deploy/sample-deploy.log}"
HEALTH_URL="${HEALTH_URL:-}"
DOCKER_REGISTRY_MIRRORS="${DOCKER_REGISTRY_MIRRORS:-disabled}"
DOCKER_DAEMON_CONFIG="${DOCKER_DAEMON_CONFIG:-/etc/docker/daemon.json}"

mkdir -p "$(dirname "$LOG_FILE")"

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"
}

ensure_docker_registry_mirror() {
  [[ "$DOCKER_REGISTRY_MIRRORS" == "disabled" || -z "$DOCKER_REGISTRY_MIRRORS" ]] && return 0
  command -v docker >/dev/null 2>&1 || { log "docker is not installed"; return 1; }
  [[ "$(id -u)" == "0" ]] || { log "root is required to update $DOCKER_DAEMON_CONFIG"; return 1; }
  local result
  result="$(DOCKER_DAEMON_CONFIG="$DOCKER_DAEMON_CONFIG" DOCKER_REGISTRY_MIRRORS="$DOCKER_REGISTRY_MIRRORS" python3 - <<'PY'
import json
import os
import stat
import tempfile
from pathlib import Path

path = Path(os.environ["DOCKER_DAEMON_CONFIG"])
mirrors = [item.strip().rstrip("/") for item in os.environ["DOCKER_REGISTRY_MIRRORS"].split(",") if item.strip()]
if not mirrors:
    raise SystemExit("DOCKER_REGISTRY_MIRRORS must contain a URL or disabled")
if path.exists():
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise SystemExit("Docker daemon configuration must be a JSON object")
    mode = stat.S_IMODE(path.stat().st_mode)
else:
    config = {}
    mode = 0o644
if config.get("registry-mirrors") == mirrors:
    print("unchanged")
    raise SystemExit(0)
config["registry-mirrors"] = mirrors
path.parent.mkdir(parents=True, exist_ok=True)
fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")
    os.chmod(temporary, mode)
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
print("changed")
PY
  )" || { log "failed to update Docker registry mirror configuration"; return 1; }
  if [[ "$result" == "changed" ]]; then
    log "phase=docker_registry updating Docker registry mirrors: $DOCKER_REGISTRY_MIRRORS"
    systemctl restart docker
    systemctl is-active --quiet docker || { log "Docker did not become active after mirror update"; return 1; }
  else
    log "Docker registry mirror configuration already current"
  fi
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

ensure_docker_registry_mirror
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
