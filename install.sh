#!/usr/bin/env bash
set -Eeuo pipefail

APP_HOME="${APP_HOME:-/opt/vibepilot-deploy}"
ENV_FILE="${ENV_FILE:-/etc/vibepilot-deploy-agent.env}"
SERVICE_FILE="/etc/systemd/system/vibepilot-deploy-agent.service"
NGINX_CONF_FILE="${NGINX_CONF_FILE:-/etc/nginx/conf.d/vibepilot-deploy.conf}"
DEPLOY_DOMAIN="${DEPLOY_DOMAIN:-}"
SETUP_NGINX="${SETUP_NGINX:-auto}"
SETUP_HTTPS="${SETUP_HTTPS:-ask}"
INSTALL_LANG="${INSTALL_LANG:-}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SAME_SOURCE_AND_TARGET="false"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "请使用 root 用户运行，或使用 sudo 执行。 / Please run as root or use sudo."
  exit 1
fi

choose_language() {
  local answer="${INSTALL_LANG:-}"
  if [[ -z "$answer" && -t 0 ]]; then
    read -r -p "请选择安装向导语言 / Select installer language (zh/en，默认 zh): " answer
  fi
  answer="${answer:-zh}"
  case "${answer,,}" in
    en|eng|english) INSTALL_LANG="en" ;;
    *) INSTALL_LANG="zh" ;;
  esac
}

is_en() {
  [[ "$INSTALL_LANG" == "en" ]]
}

ask() {
  local prompt="$1"
  local default="${2:-}"
  local answer=""
  if [[ -t 0 ]]; then
    if [[ -n "$default" ]]; then
      read -r -p "$prompt [$default]: " answer
      echo "${answer:-$default}"
    else
      read -r -p "$prompt: " answer
      echo "$answer"
    fi
  else
    echo "$default"
  fi
}

ask_yes_no() {
  local prompt="$1"
  local default="${2:-n}"
  local answer=""
  if [[ -t 0 ]]; then
    read -r -p "$prompt [$default]: " answer
    answer="${answer:-$default}"
  else
    answer="$default"
  fi
  case "${answer,,}" in
    y|yes|1|true|on|是|好|确认|确定) return 0 ;;
    *) return 1 ;;
  esac
}

normalize_domain() {
  local value="$1"
  value="${value#http://}"
  value="${value#https://}"
  value="${value%%/*}"
  value="${value%%:*}"
  echo "$value"
}

write_nginx_config() {
  local domain="$1"
  local backup=""
  if [[ -f "$NGINX_CONF_FILE" ]]; then
    backup="${NGINX_CONF_FILE}.$(date '+%Y%m%d-%H%M%S').bak"
    cp -p "$NGINX_CONF_FILE" "$backup"
    if is_en; then
      echo "Backed up existing Nginx config: $backup"
    else
      echo "已备份原有 Nginx 配置：$backup"
    fi
  fi
  mkdir -p "$(dirname "$NGINX_CONF_FILE")"
  cat >"$NGINX_CONF_FILE" <<EOF
server {
    listen 80;
    server_name $domain;

    client_max_body_size 20m;

    location = /deploy/webhook {
        proxy_pass http://127.0.0.1:9010/webhook;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location = /deploy/ui {
        return 302 /deploy/ui/;
    }

    location /deploy/ui/ {
        proxy_pass http://127.0.0.1:9010/ui/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location /deploy/ {
        proxy_pass http://127.0.0.1:9010/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
  if is_en; then
    echo "Wrote Nginx config: $NGINX_CONF_FILE"
  else
    echo "已写入 Nginx 配置：$NGINX_CONF_FILE"
  fi
}

install_nginx_if_missing() {
  if command -v nginx >/dev/null 2>&1; then
    return 0
  fi
  local prompt="检测到服务器未安装 Nginx，是否现在自动安装？"
  if is_en; then
    prompt="Nginx is not installed. Install it automatically now?"
  fi
  if ! ask_yes_no "$prompt" "y"; then
    return 1
  fi
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    apt-get install -y nginx
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y nginx
  elif command -v yum >/dev/null 2>&1; then
    yum install -y nginx
  else
    if is_en; then
      echo "No supported package manager found. Please install nginx manually first."
    else
      echo "未找到支持的包管理器，请先手动安装 nginx。"
    fi
    return 1
  fi
  systemctl enable nginx
  systemctl start nginx
}

install_certbot_if_missing() {
  if command -v certbot >/dev/null 2>&1; then
    return 0
  fi
  local prompt="未检测到 certbot，是否现在自动安装并用于申请 HTTPS 证书？"
  if is_en; then
    prompt="Certbot was not found. Install it automatically now for HTTPS certificates?"
  fi
  if ! ask_yes_no "$prompt" "n"; then
    return 1
  fi
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    apt-get install -y certbot python3-certbot-nginx
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y certbot python3-certbot-nginx
  elif command -v yum >/dev/null 2>&1; then
    yum install -y certbot python3-certbot-nginx
  else
    if is_en; then
      echo "No supported package manager found. Please install certbot manually first."
    else
      echo "未找到支持的包管理器，请先手动安装 certbot。"
    fi
    return 1
  fi
}

setup_nginx() {
  local domain="$1"
  if [[ -z "$domain" ]]; then
    if is_en; then
      echo "No domain provided. Skipped Nginx auto config."
    else
      echo "未填写域名，已跳过 Nginx 自动配置。"
    fi
    return 0
  fi
  if ! install_nginx_if_missing; then
    if is_en; then
      echo "Skipped Nginx auto config. You can rerun later with:"
    else
      echo "已跳过 Nginx 自动配置。后续可以用下面命令重新执行："
    fi
    echo "  DEPLOY_DOMAIN=$domain bash install.sh"
    return 0
  fi
  write_nginx_config "$domain"
  nginx -t
  systemctl reload nginx || systemctl restart nginx

  if install_certbot_if_missing; then
    local cert_prompt="检测到 certbot。是否现在为 $domain 申请 HTTPS 证书？请确认域名已经解析到本服务器"
    if is_en; then
      cert_prompt="Certbot found. Issue an HTTPS certificate for $domain now? Make sure DNS already points to this server"
    fi
    if [[ "$SETUP_HTTPS" == "yes" ]] || { [[ "$SETUP_HTTPS" == "ask" ]] && ask_yes_no "$cert_prompt" "n"; }; then
      certbot --nginx -d "$domain"
    fi
  else
    if is_en; then
      echo "Skipped HTTPS certificate setup. HTTP access still works. To enable HTTPS later, run:"
      echo "  apt install -y certbot python3-certbot-nginx"
      echo "  certbot --nginx -d $domain"
    else
      echo "已跳过 HTTPS 证书配置。HTTP 访问仍然可用。后续可用下面命令开启 HTTPS："
      echo "  apt install -y certbot python3-certbot-nginx"
      echo "  certbot --nginx -d $domain"
    fi
  fi
}

choose_language

if [[ -z "$DEPLOY_DOMAIN" && "$SETUP_NGINX" != "0" && "$SETUP_NGINX" != "false" ]]; then
  if is_en; then
    DEPLOY_DOMAIN="$(ask "Enter dashboard domain, for example deploy.example.com; press Enter to skip Nginx auto config" "")"
  else
    DEPLOY_DOMAIN="$(ask "请输入部署面板域名，例如 deploy.example.com；直接回车则跳过 Nginx 自动配置" "")"
  fi
fi
DEPLOY_DOMAIN="$(normalize_domain "$DEPLOY_DOMAIN")"
if [[ -n "$DEPLOY_DOMAIN" && ! "$DEPLOY_DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]]; then
  if is_en; then
    echo "Invalid domain: $DEPLOY_DOMAIN"
  else
    echo "域名格式不正确：$DEPLOY_DOMAIN"
  fi
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
  if is_en; then
    echo "Source directory is already $APP_HOME; skipped file copy."
  else
    echo "当前目录已经是 $APP_HOME，跳过文件复制。"
  fi
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
  if is_en; then
    echo "Created environment config: $ENV_FILE"
  else
    echo "已创建环境配置：$ENV_FILE"
  fi
else
  if is_en; then
    echo "Kept existing environment config: $ENV_FILE"
  else
    echo "保留已有环境配置：$ENV_FILE"
  fi
fi

if [[ ! -f "$APP_HOME/projects.json" ]]; then
  install -m 600 "$APP_HOME/examples/projects.example.json" "$APP_HOME/projects.json"
  if is_en; then
    echo "Created project config: $APP_HOME/projects.json"
  else
    echo "已创建项目配置：$APP_HOME/projects.json"
  fi
fi

install -m 644 "$APP_HOME/systemd/vibepilot-deploy-agent.service" "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable vibepilot-deploy-agent
systemctl restart vibepilot-deploy-agent

if [[ "$SETUP_NGINX" != "0" && "$SETUP_NGINX" != "false" ]]; then
  setup_nginx "$DEPLOY_DOMAIN"
fi

if curl -fsS --max-time 5 http://127.0.0.1:9010/health >/dev/null 2>&1; then
  if is_en; then
    HEALTH_RESULT="ok"
  else
    HEALTH_RESULT="正常"
  fi
else
  if is_en; then
    HEALTH_RESULT="failed; check logs"
  else
    HEALTH_RESULT="异常，请查看日志"
  fi
fi

DASHBOARD_URL="http://127.0.0.1:9010/ui"
if [[ -n "$DEPLOY_DOMAIN" ]]; then
  DASHBOARD_URL="http://$DEPLOY_DOMAIN/deploy/ui"
fi

if is_en; then
  cat <<EOF

VibePilot Deploy installed.

Agent health: $HEALTH_RESULT

Open this URL and set your admin password:
   $DASHBOARD_URL

Local fallback URL:
   http://127.0.0.1:9010/ui

Useful commands:
   systemctl status vibepilot-deploy-agent
   journalctl -u vibepilot-deploy-agent -f

Optional: edit environment config:
   nano $ENV_FILE

Optional: edit projects directly, or add projects in the web panel:
   nano $APP_HOME/projects.json

Health check:
   curl http://127.0.0.1:9010/health

EOF
else
  cat <<EOF

VibePilot Deploy 安装完成。

Agent 状态：$HEALTH_RESULT

打开下面地址，按页面提示设置管理员密码：
   $DASHBOARD_URL

本机备用访问地址：
   http://127.0.0.1:9010/ui

常用命令：
   systemctl status vibepilot-deploy-agent
   journalctl -u vibepilot-deploy-agent -f

可选：修改环境配置：
   nano $ENV_FILE

可选：直接修改项目配置，或者在网页里添加项目：
   nano $APP_HOME/projects.json

健康检查：
   curl http://127.0.0.1:9010/health

EOF
fi
