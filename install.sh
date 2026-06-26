#!/usr/bin/env bash
set -Eeuo pipefail

APP_HOME="${APP_HOME:-/opt/vibepilot-deploy}"
ENV_FILE="${ENV_FILE:-/etc/vibepilot-deploy-agent.env}"
SERVICE_FILE="/etc/systemd/system/vibepilot-deploy-agent.service"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SAME_SOURCE_AND_TARGET="false"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Please run as root."
  exit 1
fi

mkdir -p "$APP_HOME" /var/log/vibepilot /var/lib/vibepilot-deploy-agent
if [[ "$(cd "$APP_HOME" && pwd)" == "$SOURCE_DIR" ]]; then
  SAME_SOURCE_AND_TARGET="true"
fi
PROJECTS_BACKUP=""
if [[ -f "$APP_HOME/projects.json" ]]; then
  PROJECTS_BACKUP="$(mktemp)"
  cp -p "$APP_HOME/projects.json" "$PROJECTS_BACKUP"
fi

if [[ "$SAME_SOURCE_AND_TARGET" == "true" ]]; then
  echo "Source directory is already $APP_HOME; skipping file copy."
elif command -v rsync >/dev/null 2>&1; then
  rsync -a --delete \
    --exclude ".git" \
    --exclude "__pycache__" \
    --exclude "projects.json" \
    "$SOURCE_DIR/" "$APP_HOME/"
else
  shopt -s dotglob nullglob
  for item in "$APP_HOME"/*; do
    if [[ "$(basename "$item")" == "projects.json" ]]; then
      continue
    fi
    rm -rf -- "$item"
  done
  shopt -u dotglob nullglob
  cp -a "$SOURCE_DIR/." "$APP_HOME/"
  rm -rf "$APP_HOME/.git" "$APP_HOME/__pycache__"
fi
if [[ -n "$PROJECTS_BACKUP" ]]; then
  cp -p "$PROJECTS_BACKUP" "$APP_HOME/projects.json"
  rm -f "$PROJECTS_BACKUP"
fi

chmod +x "$APP_HOME/agent.py" "$APP_HOME/scripts/"*.sh

if [[ ! -f "$ENV_FILE" ]]; then
  install -m 600 "$APP_HOME/env.example" "$ENV_FILE"
  echo "Created $ENV_FILE"
else
  echo "Keep existing $ENV_FILE"
fi

if [[ ! -f "$APP_HOME/projects.json" ]]; then
  install -m 600 "$APP_HOME/examples/projects.example.json" "$APP_HOME/projects.json"
  echo "Created $APP_HOME/projects.json"
fi

install -m 644 "$APP_HOME/systemd/vibepilot-deploy-agent.service" "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable vibepilot-deploy-agent

cat <<EOF

VibePilot Deploy installed.

Next steps:
1. Start agent:
   systemctl restart vibepilot-deploy-agent
   systemctl status vibepilot-deploy-agent

2. Open the dashboard and set your admin password:
   http://YOUR_SERVER/deploy/ui

3. Optional: edit environment:
   nano $ENV_FILE

4. Optional: edit projects directly, or use the web wizard:
   nano $APP_HOME/projects.json

5. Health check:
   curl http://127.0.0.1:9010/health

EOF
