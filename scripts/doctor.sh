#!/usr/bin/env bash
set -Eeuo pipefail
PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

APP_HOME="${MINI_DEPLOY_HOME:-}"
ENV_FILE="${DEPLOY_AGENT_ENV_FILE:-/etc/mini-deploy-agent.env}"
SERVICE_NAME="${DEPLOY_AGENT_SERVICE_NAME:-}"
STATE_FILE="${DEPLOY_AGENT_STATE_FILE:-}"
PORT=6868
PROJECTS_FILE="${DEPLOY_PROJECTS_FILE:-}"

env_file_value() {
  local key="$1"
  awk -v wanted="$key" '
    /^[[:space:]]*#/ { next }
    {
      separator = index($0, "=")
      if (separator == 0) next
      name = substr($0, 1, separator - 1)
      value = substr($0, separator + 1)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", name)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
      if (name != wanted) next
      if (value ~ /^".*"$/ || value ~ /^\047.*\047$/) {
        value = substr(value, 2, length(value) - 2)
      }
      result = value
    }
    END { print result }
  ' "$ENV_FILE"
}

if [[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]]; then
  [[ -n "$APP_HOME" ]] || APP_HOME="$(env_file_value "MINI_DEPLOY_HOME")"
  [[ -n "$SERVICE_NAME" ]] || SERVICE_NAME="$(env_file_value "DEPLOY_AGENT_SERVICE_NAME")"
  [[ -n "$STATE_FILE" ]] || STATE_FILE="$(env_file_value "DEPLOY_AGENT_STATE_FILE")"
  [[ -n "$PROJECTS_FILE" ]] || PROJECTS_FILE="$(env_file_value "DEPLOY_PROJECTS_FILE")"
fi
APP_HOME="${APP_HOME:-/opt/mini_deploy}"
SERVICE_NAME="${SERVICE_NAME:-mini-deploy-agent}"
STATE_FILE="${STATE_FILE:-/var/lib/mini-deploy-agent/state.json}"
DATA_HOME="${DATA_HOME:-$(dirname -- "$STATE_FILE")}"
PROJECTS_FILE="${PROJECTS_FILE:-$DATA_HOME/projects.json}"
PYTHON_BIN="$(command -v python3 2>/dev/null || true)"
if [[ -n "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(readlink -f -- "$PYTHON_BIN")"
fi

ok=0
warn=0
fail=0

pass() { ok=$((ok + 1)); printf '[OK]   %s\n' "$*"; }
note() { warn=$((warn + 1)); printf '[WARN] %s\n' "$*"; }
bad() { fail=$((fail + 1)); printf '[FAIL] %s\n' "$*"; }

check_file() {
  if [[ -e "$1" ]]; then pass "$2: $1"; else bad "$2 missing: $1"; fi
}

printf 'mini_deploy diagnostics\n\n'
check_file "$APP_HOME/agent.py" "agent"
check_file "$APP_HOME/ui/index.html" "UI"
check_file "$PROJECTS_FILE" "projects config"
check_file "$ENV_FILE" "environment file"
check_file "$APP_HOME/systemd/mini-deploy-agent.service" "systemd unit source"

if [[ -f "$PROJECTS_FILE" ]]; then
  if [[ -z "$PYTHON_BIN" ]]; then
    bad "Python 3 is unavailable; cannot run Agent config validation"
  elif MINI_DEPLOY_HOME="$APP_HOME" \
    DEPLOY_AGENT_STATE_FILE="$STATE_FILE" \
    DEPLOY_PROJECTS_FILE="$PROJECTS_FILE" \
    "$PYTHON_BIN" "$APP_HOME/agent.py" validate-config >/dev/null; then
    pass "Agent validates projects config: $PROJECTS_FILE"
  else
    bad "Agent rejected projects config: $PROJECTS_FILE"
  fi
fi

if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
  pass "systemd service active: $SERVICE_NAME"
else
  note "systemd service is not active: $SERVICE_NAME"
fi

if curl -fsS --max-time 5 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  pass "agent health responds on 127.0.0.1:$PORT"
else
  bad "agent health check failed on 127.0.0.1:$PORT"
fi

if command -v docker >/dev/null 2>&1; then
  pass "docker is installed"
  if docker compose version >/dev/null 2>&1; then pass "docker compose is available"; else note "docker compose plugin is unavailable"; fi
else
  note "docker is not installed (only required for Docker projects)"
fi

printf '\nSummary: OK=%s WARN=%s FAIL=%s\n' "$ok" "$warn" "$fail"
(( fail == 0 ))
