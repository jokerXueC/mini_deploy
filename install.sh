#!/usr/bin/env bash
set -Eeuo pipefail
# Never expose the in-memory administrator password through shell tracing.
set +x
PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

APP_HOME_EXPLICIT="false"
DATA_HOME_EXPLICIT="false"
LOG_HOME_EXPLICIT="false"
SERVICE_NAME_EXPLICIT="false"
[[ -n "${APP_HOME:-}" ]] && APP_HOME_EXPLICIT="true"
[[ -n "${DATA_HOME:-}" ]] && DATA_HOME_EXPLICIT="true"
[[ -n "${LOG_HOME:-}" ]] && LOG_HOME_EXPLICIT="true"
[[ -n "${SERVICE_NAME:-}" ]] && SERVICE_NAME_EXPLICIT="true"

APP_HOME="${APP_HOME:-/opt/mini_deploy}"
ENV_FILE="${ENV_FILE:-/etc/mini-deploy-agent.env}"
DATA_HOME="${DATA_HOME:-/var/lib/mini-deploy-agent}"
LOG_HOME="${LOG_HOME:-/var/log/mini_deploy}"
INSTALL_BACKUP_DIR="${INSTALL_BACKUP_DIR:-/var/backups/mini-deploy-agent}"
SERVICE_NAME="${SERVICE_NAME:-mini-deploy-agent}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
NGINX_CONF_FILE="${NGINX_CONF_FILE:-/etc/nginx/conf.d/mini-deploy.conf}"
DEPLOY_DOMAIN="${DEPLOY_DOMAIN:-}"
DEPLOY_PUBLIC_IP="${DEPLOY_PUBLIC_IP:-}"
SETUP_NGINX="${SETUP_NGINX:-auto}"
SETUP_HTTPS="${SETUP_HTTPS:-ask}"
SETUP_DOCKER="${SETUP_DOCKER:-ask}"
INSTALL_LANG="${INSTALL_LANG:-}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SAME_SOURCE_AND_TARGET="false"
UPGRADE_BACKUP_PATH=""
INSTALL_MARKER_NAME=".mini-deploy-install"
MAINTENANCE_LOCK_FILE="/run/mini-deploy-agent/maintenance.lock"
MANAGED_SERVICE_MARKER="# mini_deploy-managed: agent-systemd-v1"
MANAGED_NGINX_MARKER="# mini_deploy-managed: nginx-v1"
DEPLOY_ALLOW_LEGACY_INSTALL_ADOPTION="${DEPLOY_ALLOW_LEGACY_INSTALL_ADOPTION:-false}"
EXISTING_INSTALLATION="false"
LEGACY_INSTALLATION="false"
PROJECTS_LAYOUT_STATE="unset"
UPGRADE_SERVICE_WAS_ACTIVE="false"
UPGRADE_SERVICE_FAILSAFE_ARMED="false"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "请使用 root 用户运行，或使用 sudo 执行。 / Please run as root or use sudo."
  exit 1
fi

if [[ ! "$SERVICE_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_.@-]*$ ]]; then
  echo "systemd 服务名不安全 / Invalid systemd service name: $SERVICE_NAME" >&2
  exit 1
fi

for command in awk dirname find flock grep install mktemp mv python3 readlink sha256sum stat tar; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "缺少安装依赖 / Missing installer dependency: $command" >&2
    exit 1
  fi
done
PYTHON_BIN="$(readlink -f -- "$(command -v python3)")"
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Python 版本过低，需要 Python 3.10 或更高版本 / Python 3.10 or newer is required." >&2
  exit 1
fi

for path in "$APP_HOME" "$ENV_FILE" "$DATA_HOME" "$LOG_HOME" "$INSTALL_BACKUP_DIR" "$SERVICE_FILE" "$NGINX_CONF_FILE"; do
  if [[ "$path" != /* ]]; then
    echo "安装路径必须是绝对路径 / Installation paths must be absolute: $path" >&2
    exit 1
  fi
done

for path in "$ENV_FILE" "$SERVICE_FILE" "$NGINX_CONF_FILE"; do
  if [[ -L "$path" ]]; then
    echo "控制配置路径不能是符号链接 / Control file path must not be a symbolic link: $path" >&2
    exit 1
  fi
done

for required in agent.py certificates.py nginx_runtime.py env.example systemd/mini-deploy-agent.service scripts/backup-installation.sh scripts/verify_backup.py; do
  if [[ ! -f "$SOURCE_DIR/$required" ]]; then
    echo "安装包不完整 / Incomplete installation package: $required" >&2
    exit 1
  fi
done

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

nginx_setup_enabled() {
  case "${SETUP_NGINX,,}" in
    0|false|no|off|disabled) return 1 ;;
    *) return 0 ;;
  esac
}

nginx_install_is_preapproved() {
  case "${SETUP_NGINX,,}" in
    1|true|yes|on|enabled) return 0 ;;
    *) return 1 ;;
  esac
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

write_systemd_service() {
  local service_parent=""
  local temporary=""
  local backup=""

  service_parent="$(dirname -- "$SERVICE_FILE")"
  if [[ ! -d "$service_parent" ]]; then
    install -d -m 755 "$service_parent"
  fi
  temporary="$(mktemp "$service_parent/.${SERVICE_NAME}.service.XXXXXX")"
  if [[ -f "$SERVICE_FILE" ]]; then
    backup="${SERVICE_FILE}.$(date -u '+%Y%m%dT%H%M%SZ').$$.bak"
    install -m 600 "$SERVICE_FILE" "$backup"
  fi
  cat >"$temporary" <<EOF
$MANAGED_SERVICE_MARKER
[Unit]
Description=mini_deploy lightweight deploy agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
RuntimeDirectory=mini-deploy-agent
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=yes
WorkingDirectory="$APP_HOME"
EnvironmentFile="$ENV_FILE"
ExecStart="$PYTHON_BIN" "$APP_HOME/agent.py"
Restart=always
RestartSec=3

NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
  if ! {
    chown root:root "$temporary" \
      && chmod 644 "$temporary" \
      && mv -T -- "$temporary" "$SERVICE_FILE"
  }; then
    rm -f -- "$temporary"
    return 1
  fi
}

is_persistent_app_entry() {
  case "$1" in
    "$INSTALL_MARKER_NAME"|projects.json|.backups|workspace|state.json|audit.jsonl|.env|server.env|*.log) return 0 ;;
    *) return 1 ;;
  esac
}

directory_has_entries() {
  local directory="$1"
  local -a entries=()
  shopt -s dotglob nullglob
  entries=("$directory"/*)
  shopt -u dotglob nullglob
  (( ${#entries[@]} > 0 ))
}

has_valid_install_marker() {
  local marker="$APP_HOME/$INSTALL_MARKER_NAME"
  local format=""
  local marker_app_home=""
  local mode=""
  [[ -f "$marker" && ! -L "$marker" ]] || return 1
  [[ "$(stat -c '%u' -- "$marker")" == "0" ]] || return 1
  [[ "$(stat -c '%h' -- "$marker")" == "1" ]] || return 1
  [[ "$(stat -c '%u' -- "$APP_HOME")" == "0" ]] || return 1
  mode="$(stat -c '%a' -- "$marker")"
  (( (8#$mode & 022) == 0 )) || return 1
  format="$(marker_unique_field "$marker" "format")" || return 1
  [[ "$format" == "1" || "$format" == "2" ]] || return 1
  marker_app_home="$(marker_unique_field "$marker" "app_home")" || return 1
  [[ "$marker_app_home" == "$APP_HOME" ]]
}

has_valid_data_marker() {
  local marker="$DATA_HOME/.mini-deploy-data"
  local format=""
  local marker_data_home=""
  local mode=""
  [[ -f "$marker" && ! -L "$marker" ]] || return 1
  [[ "$(stat -c '%u' -- "$marker")" == "0" ]] || return 1
  [[ "$(stat -c '%h' -- "$marker")" == "1" ]] || return 1
  mode="$(stat -c '%a' -- "$marker")"
  (( (8#$mode & 022) == 0 )) || return 1
  format="$(marker_unique_field "$marker" "format")" || return 1
  [[ "$format" == "1" ]] || return 1
  if grep -q '^data_home=' "$marker"; then
    marker_data_home="$(marker_unique_field "$marker" "data_home")" || return 1
    [[ "$marker_data_home" == "$DATA_HOME" ]]
  fi
}

marker_unique_field() {
  local marker="$1"
  local wanted="$2"
  awk -v wanted="$wanted" '
    {
      separator = index($0, "=")
      if (separator == 0) next
      key = substr($0, 1, separator - 1)
      if (key != wanted) next
      count++
      value = substr($0, separator + 1)
    }
    END {
      if (count != 1) exit 1
      print value
    }
  ' "$marker"
}

legacy_adoption_enabled() {
  case "${DEPLOY_ALLOW_LEGACY_INSTALL_ADOPTION,,}" in
    1|true|yes|on|enabled) return 0 ;;
    *) return 1 ;;
  esac
}

assert_no_symlink_components() {
  local requested="$1"
  local current="/"
  local component=""
  local -a components=()

  IFS='/' read -r -a components <<< "${requested#/}"
  for component in "${components[@]}"; do
    [[ -n "$component" && "$component" != "." ]] || continue
    if [[ "$component" == ".." ]]; then
      echo "托管路径不能包含 .. 组件 / Managed path must not contain '..': $requested" >&2
      return 1
    fi
    current="${current%/}/$component"
    if [[ -L "$current" ]]; then
      echo "托管路径不能经过符号链接 / Managed path must not traverse a symbolic link: $current" >&2
      return 1
    fi
  done
}

assert_no_nested_mounts() {
  local root="$1"
  local label="$2"

  [[ -d "$root" ]] || return 0
  "$PYTHON_BIN" - "$root" "$label" <<'PY'
import os
import re
import sys

root = os.path.normpath(os.path.abspath(sys.argv[1]))
label = sys.argv[2]


def unescape_mount_path(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


try:
    with open("/proc/self/mountinfo", "r", encoding="utf-8", errors="surrogateescape") as mountinfo:
        for line in mountinfo:
            fields = line.split()
            if len(fields) < 5:
                raise RuntimeError("malformed /proc/self/mountinfo")
            target = os.path.normpath(unescape_mount_path(fields[4]))
            if target != root and target.startswith(root.rstrip("/") + "/"):
                print(
                    f"{label} must not contain nested mount points: {target}",
                    file=sys.stderr,
                )
                raise SystemExit(1)
except OSError as exc:
    print(f"cannot inspect mount points for {label}: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
}

first_hardlinked_regular_file() {
  find "$1" -xdev -type f -links +1 -print -quit 2>/dev/null
}

looks_like_legacy_installation() {
  local required=""
  for required in agent.py install.sh env.example ui/index.html scripts/deploy.sample.sh; do
    [[ -f "$APP_HOME/$required" && ! -L "$APP_HOME/$required" ]] || return 1
  done
  return 0
}

assert_trusted_directory_chain() {
  local current="$1"
  local parent=""
  local owner=""
  local mode=""

  while [[ ! -e "$current" ]]; do
    parent="$(dirname -- "$current")"
    [[ "$parent" != "$current" ]] || break
    current="$parent"
  done
  while true; do
    if [[ -L "$current" || ! -d "$current" ]]; then
      echo "托管路径父链必须全部是普通目录 / Managed path ancestor is not a regular directory: $current" >&2
      return 1
    fi
    owner="$(stat -c '%u' -- "$current")"
    mode="$(stat -c '%a' -- "$current")"
    if [[ "$owner" != "0" ]] || (( (8#$mode & 022) != 0 )); then
      echo "托管路径父链必须归 root 所有且不可由组/其他用户写入 / Untrusted managed path ancestor: $current" >&2
      return 1
    fi
    [[ "$current" == "/" ]] && break
    parent="$(dirname -- "$current")"
    [[ "$parent" != "$current" ]] || break
    current="$parent"
  done
}

ensure_root_directory() {
  local directory="$1"
  local mode="$2"
  local owner=""
  local existed="false"

  assert_trusted_directory_chain "$(dirname -- "$directory")"
  if [[ -L "$directory" || ( -e "$directory" && ! -d "$directory" ) ]]; then
    echo "托管路径必须是普通目录且不能是符号链接 / Managed path must be a non-symlink directory: $directory" >&2
    return 1
  fi
  if [[ -d "$directory" ]]; then
    existed="true"
    owner="$(stat -c '%u' -- "$directory")"
    if [[ "$owner" != "0" ]]; then
      echo "拒绝使用非 root 所有的托管目录 / Refusing managed directory not owned by root: $directory" >&2
      return 1
    fi
  else
    install -d -m "$mode" "$directory"
  fi
  if [[ "$existed" != "true" ]]; then
    chown root:root "$directory"
  fi
  if [[ "$existed" == "true" && "$mode" == "755" ]]; then
    # LOG_HOME may deliberately be private (for example 0700). Keep any
    # existing group/other restrictions instead of widening them to 0755.
    chmod u+rwx,go-w "$directory"
  else
    chmod "$mode" "$directory"
  fi
  assert_trusted_directory_chain "$directory"
}

ensure_trusted_parent_directory() {
  local file_path="$1"
  local parent=""
  local owner=""
  local mode=""

  parent="$(dirname -- "$file_path")"
  assert_trusted_directory_chain "$parent"
  if [[ -L "$parent" || ( -e "$parent" && ! -d "$parent" ) ]]; then
    echo "控制配置父路径必须是普通目录 / Control file parent must be a directory: $parent" >&2
    return 1
  fi
  if [[ ! -d "$parent" ]]; then
    install -d -m 755 "$parent"
  fi
  owner="$(stat -c '%u' -- "$parent")"
  mode="$(stat -c '%a' -- "$parent")"
  if [[ "$owner" != "0" ]] || (( (8#$mode & 022) != 0 )); then
    echo "控制配置父目录必须归 root 所有且不可由组/其他用户写入 / Untrusted control file parent: $parent" >&2
    return 1
  fi
}

validate_existing_control_file() {
  local file_path="$1"
  local owner=""
  local mode=""

  [[ -e "$file_path" || -L "$file_path" ]] || return 0
  if [[ -L "$file_path" || ! -f "$file_path" ]]; then
    echo "控制配置必须是普通非符号链接文件 / Control path must be a regular non-symlink file: $file_path" >&2
    return 1
  fi
  owner="$(stat -c '%u' -- "$file_path")"
  mode="$(stat -c '%a' -- "$file_path")"
  if [[ "$(stat -c '%h' -- "$file_path")" != "1" ]]; then
    echo "控制配置不能是硬链接 / Control file must have exactly one hard link: $file_path" >&2
    return 1
  fi
  if [[ "$owner" != "0" ]] || (( (8#$mode & 022) != 0 )); then
    echo "控制配置必须归 root 所有且不可由组/其他用户写入 / Unsafe control file ownership or mode: $file_path" >&2
    return 1
  fi
}

validate_managed_service_file() {
  [[ -f "$SERVICE_FILE" ]] || return 0
  if grep -Fxq -- "$MANAGED_SERVICE_MARKER" "$SERVICE_FILE"; then
    return 0
  fi
  if legacy_adoption_enabled \
    && grep -Fxq -- 'Description=mini_deploy lightweight deploy agent' "$SERVICE_FILE" \
    && grep -Eq '^ExecStart=.*[/"]agent\.py"?$' "$SERVICE_FILE"; then
    echo "检测到旧版无标记 systemd unit；已按显式授权接管 / Adopting an unmarked legacy systemd unit by explicit authorization."
    return 0
  fi
  echo "已有 systemd unit 缺少 mini_deploy 托管标记，拒绝覆盖；确认是旧版配置后可临时设置 DEPLOY_ALLOW_LEGACY_INSTALL_ADOPTION=true / Refusing to overwrite an unmarked systemd unit: $SERVICE_FILE" >&2
  return 1
}

validate_managed_nginx_file() {
  [[ -f "$NGINX_CONF_FILE" ]] || return 0
  if grep -Fxq -- "$MANAGED_NGINX_MARKER" "$NGINX_CONF_FILE"; then
    return 0
  fi
  if legacy_adoption_enabled \
    && grep -Fq -- 'location = /deploy/webhook {' "$NGINX_CONF_FILE" \
    && grep -Eq -- 'proxy_pass http://127\.0\.0\.1:(9010|6868)/webhook;' "$NGINX_CONF_FILE"; then
    echo "检测到旧版无标记 Nginx 配置；已按显式授权接管 / Adopting an unmarked legacy Nginx config by explicit authorization."
    return 0
  fi
  echo "已有 Nginx 配置缺少 mini_deploy 托管标记，拒绝覆盖；确认是旧版配置后可临时设置 DEPLOY_ALLOW_LEGACY_INSTALL_ADOPTION=true / Refusing to overwrite an unmarked Nginx config: $NGINX_CONF_FILE" >&2
  return 1
}

secure_app_release_files() {
  local item=""
  local name=""

  validate_app_release_tree_safety
  chown root:root "$APP_HOME"
  chmod go-w "$APP_HOME"
  shopt -s dotglob nullglob
  for item in "$APP_HOME"/*; do
    name="$(basename -- "$item")"
    if is_persistent_app_entry "$name"; then
      continue
    fi
    find "$item" -xdev -exec chown -h -- root:root {} +
    find "$item" -xdev \( -type d -o -type f \) -exec chmod go-w -- {} +
  done
  shopt -u dotglob nullglob
}

validate_app_release_tree_safety() {
  local item=""
  local name=""
  local unsafe_hardlink=""

  [[ -d "$APP_HOME" ]] || return 0
  assert_no_nested_mounts "$APP_HOME" "APP_HOME"
  shopt -s dotglob nullglob
  for item in "$APP_HOME"/*; do
    name="$(basename -- "$item")"
    if is_persistent_app_entry "$name"; then
      continue
    fi
    if [[ -L "$item" ]] || [[ -n "$(find "$item" -xdev -type l -print -quit 2>/dev/null)" ]]; then
      echo "发布文件树不能包含符号链接 / Release tree must not contain symbolic links: $item" >&2
      shopt -u dotglob nullglob
      return 1
    fi
    unsafe_hardlink="$(first_hardlinked_regular_file "$item")"
    if [[ -n "$unsafe_hardlink" ]]; then
      echo "发布文件树不能包含硬链接 / Release tree must not contain hard-linked regular files: $unsafe_hardlink" >&2
      shopt -u dotglob nullglob
      return 1
    fi
  done
  shopt -u dotglob nullglob
}

validate_release_source_tree() {
  local item=""
  local name=""
  local unsafe_hardlink=""

  assert_no_nested_mounts "$SOURCE_DIR" "SOURCE_DIR"
  shopt -s dotglob nullglob
  for item in "$SOURCE_DIR"/*; do
    name="$(basename -- "$item")"
    case "$name" in
      .git|__pycache__) continue ;;
    esac
    if is_persistent_app_entry "$name"; then
      continue
    fi
    if [[ -L "$item" ]] || [[ -n "$(find "$item" -xdev -type l -print -quit 2>/dev/null)" ]]; then
      echo "安装包发布文件不能包含符号链接 / Release package must not contain symbolic links: $item" >&2
      shopt -u dotglob nullglob
      return 1
    fi
    unsafe_hardlink="$(first_hardlinked_regular_file "$item")"
    if [[ -n "$unsafe_hardlink" ]]; then
      echo "安装包发布文件不能包含硬链接 / Release package must not contain hard-linked regular files: $unsafe_hardlink" >&2
      shopt -u dotglob nullglob
      return 1
    fi
  done
  shopt -u dotglob nullglob
}

acquire_maintenance_lock() {
  local lock_parent=""
  local lock_root=""
  local lock_root_owner=""
  local lock_root_mode=""
  local lock_owner=""
  local lock_group=""
  local lock_mode=""
  local lock_path_identity=""
  local lock_fd_identity=""
  local lock_link_count=""
  local previous_umask=""

  lock_parent="$(dirname -- "$MAINTENANCE_LOCK_FILE")"
  lock_root="$(dirname -- "$lock_parent")"
  if [[ -L "$lock_root" || ! -d "$lock_root" ]]; then
    echo "维护锁根目录不安全 / Unsafe maintenance lock root: $lock_root" >&2
    return 1
  fi
  lock_root_owner="$(stat -c '%u' -- "$lock_root")"
  lock_root_mode="$(stat -c '%a' -- "$lock_root")"
  if [[ "$lock_root_owner" != "0" ]] || (( (8#$lock_root_mode & 022) != 0 )); then
    echo "维护锁根目录不安全 / Unsafe maintenance lock root: $lock_root" >&2
    return 1
  fi
  if [[ ! -e "$lock_parent" ]]; then
    install -d -m 700 "$lock_parent"
  elif [[ -L "$lock_parent" || ! -d "$lock_parent" ]]; then
    echo "维护锁父目录无效 / Maintenance lock directory is unavailable: $lock_parent" >&2
    return 1
  fi
  lock_owner="$(stat -c '%u' -- "$lock_parent")"
  lock_group="$(stat -c '%g' -- "$lock_parent")"
  lock_mode="$(stat -c '%a' -- "$lock_parent")"
  if [[ "$lock_owner" != "0" || "$lock_group" != "0" ]] || (( (8#$lock_mode & 077) != 0 )); then
    echo "维护锁父目录必须由 root 安全管理 / Unsafe maintenance lock directory: $lock_parent" >&2
    return 1
  fi
  if [[ -L "$MAINTENANCE_LOCK_FILE" || ( -e "$MAINTENANCE_LOCK_FILE" && ! -f "$MAINTENANCE_LOCK_FILE" ) ]]; then
    echo "维护锁路径无效 / Unsafe maintenance lock path: $MAINTENANCE_LOCK_FILE" >&2
    return 1
  fi
  previous_umask="$(umask)"
  umask 077
  exec {MAINTENANCE_LOCK_FD}>>"$MAINTENANCE_LOCK_FILE"
  umask "$previous_umask"
  if [[ ! -f "/proc/self/fd/$MAINTENANCE_LOCK_FD" ]]; then
    echo "维护锁打开后不是普通文件 / Maintenance lock is not a regular file after opening." >&2
    return 1
  fi
  lock_path_identity="$(stat -c '%d:%i' -- "$MAINTENANCE_LOCK_FILE")"
  lock_fd_identity="$(stat -Lc '%d:%i' -- "/proc/self/fd/$MAINTENANCE_LOCK_FD")"
  lock_link_count="$(stat -Lc '%h' -- "/proc/self/fd/$MAINTENANCE_LOCK_FD")"
  if [[ "$lock_path_identity" != "$lock_fd_identity" || "$lock_link_count" != "1" ]]; then
    echo "维护锁在打开时发生变化或存在硬链接 / Maintenance lock changed while opening or has multiple hard links." >&2
    return 1
  fi
  chown root:root "/proc/self/fd/$MAINTENANCE_LOCK_FD"
  chmod 600 "/proc/self/fd/$MAINTENANCE_LOCK_FD"
  if ! flock -n "$MAINTENANCE_LOCK_FD"; then
    echo "另一个安装、备份或维护操作正在运行 / Another install, backup, or maintenance operation is active." >&2
    return 1
  fi
}

copy_without_rsync() {
  local item=""
  local name=""

  validate_release_source_tree
  validate_app_release_tree_safety
  shopt -s dotglob nullglob
  for item in "$SOURCE_DIR"/*; do
    name="$(basename "$item")"
    case "$name" in
      .git|__pycache__) continue ;;
    esac
    if is_persistent_app_entry "$name"; then
      continue
    fi
    if [[ -L "$item" ]] || [[ -n "$(find "$item" -xdev -type l -print -quit 2>/dev/null)" ]]; then
      echo "安装包发布文件不能包含符号链接 / Release package must not contain symbolic links: $item" >&2
      return 1
    fi

    # Replace only entries shipped by the new release. Unknown local entries
    # are left in place when rsync is unavailable.
    rm -rf -- "$APP_HOME/$name"
    cp -a -- "$item" "$APP_HOME/$name"
    assert_no_nested_mounts "$APP_HOME/$name" "copied release entry"
    if [[ -L "$APP_HOME/$name" ]] \
      || [[ -n "$(find "$APP_HOME/$name" -xdev -type l -print -quit 2>/dev/null)" ]] \
      || [[ -n "$(first_hardlinked_regular_file "$APP_HOME/$name")" ]]; then
      echo "复制后的发布文件树不安全 / Copied release tree contains a symbolic link or hard link: $APP_HOME/$name" >&2
      return 1
    fi
    find "$APP_HOME/$name" -xdev -exec chown -h -- root:root {} +
    find "$APP_HOME/$name" -xdev \( -type d -o -type f \) -exec chmod go-w -- {} +
  done
  shopt -u dotglob nullglob
}

env_file_value() {
  local key="$1"
  awk -v wanted="$key" '
    /^[[:space:]]*#/ { next }
    {
      line = $0
      sub(/\r$/, "", line)
      separator = index(line, "=")
      if (separator == 0) next
      name = substr(line, 1, separator - 1)
      value = substr(line, separator + 1)
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

assert_managed_env_value() {
  local key="$1"
  local expected="$2"
  local current=""

  [[ -f "$ENV_FILE" ]] || return 0
  current="$(env_file_value "$key")"
  [[ -n "$current" ]] || return 0
  if [[ "$current" != /* ]] || [[ "$(readlink -m -- "$current")" != "$expected" ]]; then
    cat >&2 <<EOF
已有环境配置中的 $key 与本次安装路径不一致，安装器拒绝静默迁移数据：
  current: $current
  expected: $expected
请先备份并手动迁移，或使用与现有配置一致的 server.env 后重试。
Existing $key conflicts with the requested installer layout; refusing a silent data migration.
EOF
    return 1
  fi
}

assert_projects_env_layout() {
  local current=""
  local canonical=""
  local legacy_projects_file="$APP_HOME/projects.json"
  local data_projects_file="$DATA_HOME/projects.json"

  PROJECTS_LAYOUT_STATE="unset"
  [[ -f "$ENV_FILE" ]] || return 0
  current="$(env_file_value "DEPLOY_PROJECTS_FILE")"
  [[ -n "$current" ]] || return 0
  if [[ "$current" != /* ]]; then
    echo "DEPLOY_PROJECTS_FILE 必须是旧程序目录或新数据目录中的绝对路径 / DEPLOY_PROJECTS_FILE must be the legacy or data-directory absolute path: $current" >&2
    return 1
  fi
  canonical="$(readlink -m -- "$current")"
  if [[ "$canonical" == "$legacy_projects_file" ]]; then
    PROJECTS_LAYOUT_STATE="legacy"
  elif [[ "$canonical" == "$data_projects_file" ]]; then
    PROJECTS_LAYOUT_STATE="data"
  else
    cat >&2 <<EOF
现有 DEPLOY_PROJECTS_FILE 使用了安装器无法安全迁移的自定义路径：
  current: $current
  allowed legacy: $legacy_projects_file
  allowed data: $data_projects_file
请先备份并手动迁移该文件；安装器不会静默覆盖自定义路径。 / Existing DEPLOY_PROJECTS_FILE uses an unsupported custom path; back it up and migrate it manually before retrying.
EOF
    return 1
  fi
}

validate_private_projects_file() {
  local projects_file="$1"
  local owner=""
  local mode=""

  [[ -e "$projects_file" || -L "$projects_file" ]] || return 0
  if [[ -L "$projects_file" || ! -f "$projects_file" ]]; then
    echo "projects.json 必须是普通非符号链接文件 / projects.json must be a regular non-symlink file: $projects_file" >&2
    return 1
  fi
  if [[ "$(stat -c '%h' -- "$projects_file")" != "1" ]]; then
    echo "projects.json 不能是硬链接 / projects.json must have exactly one hard link: $projects_file" >&2
    return 1
  fi
  owner="$(stat -c '%u' -- "$projects_file")"
  mode="$(stat -c '%a' -- "$projects_file")"
  if [[ "$owner" != "0" ]] || (( (8#$mode & 077) != 0 )); then
    echo "projects.json 必须归 root 所有且不能授予组或其他用户权限 / projects.json must be root-owned and private: $projects_file" >&2
    return 1
  fi
}

validate_projects_json_structure() {
  local projects_file="$1"
  local source_kind="${2:-private}"

  "$PYTHON_BIN" - "$projects_file" "$source_kind" <<'PY'
import json
import os
import stat
import sys

path, source_kind = sys.argv[1:]
before = os.lstat(path)
if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
    raise SystemExit(f"projects config must be a single-link regular file: {path}")
if source_kind == "private" and (
    before.st_uid != 0 or stat.S_IMODE(before.st_mode) & 0o077
):
    raise SystemExit(f"projects config must be root-owned and private: {path}")

descriptor = os.open(
    path,
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0),
)
try:
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise SystemExit(f"projects config changed while opening: {path}")
    if source_kind == "private" and (
        opened.st_uid != 0 or stat.S_IMODE(opened.st_mode) & 0o077
    ):
        raise SystemExit(f"projects config became unsafe while opening: {path}")
    with os.fdopen(descriptor, "rb") as handle:
        descriptor = -1
        payload = handle.read()
        finished = os.fstat(handle.fileno())
    if opened.st_size != finished.st_size or opened.st_mtime_ns != finished.st_mtime_ns:
        raise SystemExit(f"projects config changed while reading: {path}")
finally:
    if descriptor >= 0:
        os.close(descriptor)

try:
    raw = json.loads(payload.decode("utf-8"))
except (UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise SystemExit(f"projects config is invalid JSON: {path}: {exc}") from exc
projects = raw.get("projects") if isinstance(raw, dict) else raw
if not isinstance(projects, list) or not projects:
    raise SystemExit(f"projects config must contain a non-empty projects list: {path}")
if any(not isinstance(project, dict) for project in projects):
    raise SystemExit(f"every projects config entry must be an object: {path}")
PY
}

projects_files_are_identical() {
  local left_kind="${3:-private}"

  "$PYTHON_BIN" - "$1" "$2" "$left_kind" <<'PY'
import os
import stat
import sys


def open_checked(path: str, source_kind: str):
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise SystemExit(1)
    if source_kind == "private" and (
        before.st_uid != 0 or stat.S_IMODE(before.st_mode) & 0o077
    ):
        raise SystemExit(1)
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    after = os.fstat(descriptor)
    if (
        not stat.S_ISREG(after.st_mode)
        or after.st_nlink != 1
        or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or (
            source_kind == "private"
            and (after.st_uid != 0 or stat.S_IMODE(after.st_mode) & 0o077)
        )
    ):
        os.close(descriptor)
        raise SystemExit(1)
    return os.fdopen(descriptor, "rb")


with open_checked(sys.argv[1], sys.argv[3]) as left, open_checked(
    sys.argv[2], "private"
) as right:
    while True:
        left_chunk = left.read(1024 * 1024)
        right_chunk = right.read(1024 * 1024)
        if left_chunk != right_chunk:
            raise SystemExit(1)
        if not left_chunk:
            break
PY
}

validate_projects_migration_plan() {
  local legacy_projects_file="$APP_HOME/projects.json"
  local data_projects_file="$DATA_HOME/projects.json"

  validate_private_projects_file "$legacy_projects_file"
  validate_private_projects_file "$data_projects_file"
  # Once ENV_FILE explicitly selects DATA_HOME, APP_HOME/projects.json is only
  # a stale backup candidate: require safe metadata for archiving, but do not
  # let malformed stale JSON block the authoritative data configuration.
  if [[ "$PROJECTS_LAYOUT_STATE" != "data" && -f "$legacy_projects_file" ]]; then
    validate_projects_json_structure "$legacy_projects_file" "private"
  fi
  if [[ -f "$data_projects_file" ]]; then
    validate_projects_json_structure "$data_projects_file" "private"
  fi

  if [[ "$PROJECTS_LAYOUT_STATE" != "data" \
    && -f "$legacy_projects_file" \
    && -f "$data_projects_file" ]] \
    && ! projects_files_are_identical "$legacy_projects_file" "$data_projects_file"; then
    cat >&2 <<EOF
旧、新 projects.json 内容不同，拒绝猜测哪一份包含最新 Token：
  legacy: $legacy_projects_file
  data: $data_projects_file
请人工核对并保留正确配置后重试。 / Legacy and data projects.json differ; refusing to choose one and risk losing project Tokens.
EOF
    return 1
  fi

  if [[ "$EXISTING_INSTALLATION" == "true" \
    && "$PROJECTS_LAYOUT_STATE" == "data" \
    && ! -f "$data_projects_file" ]]; then
    echo "ENV_FILE 已将数据目录版本声明为权威配置，但已安装实例的该文件缺失；拒绝创建示例或回退到可能过期的旧副本 / The installed instance's authoritative data projects.json is missing; refusing to create an example or restore a possibly stale legacy copy." >&2
    return 1
  fi

  if [[ "$EXISTING_INSTALLATION" == "true" \
    && ! -f "$legacy_projects_file" \
    && ! -f "$data_projects_file" ]]; then
    echo "已安装实例缺少所有 projects.json，拒绝用示例配置静默替代 / The installed instance has no projects.json; refusing to silently replace it with an example." >&2
    return 1
  fi

  if [[ "$EXISTING_INSTALLATION" != "true" \
    && ! -f "$legacy_projects_file" \
    && ! -f "$data_projects_file" ]]; then
    validate_projects_json_structure "$SOURCE_DIR/examples/projects.example.json" "release"
  fi
}

atomic_copy_projects_file() {
  local source_file="$1"
  local target_file="$2"
  local source_kind="$3"

  "$PYTHON_BIN" - "$source_file" "$target_file" "$source_kind" <<'PY'
import os
import shutil
import stat
import sys
import tempfile

source_path, target_path, source_kind = sys.argv[1:]
target_parent = os.path.dirname(target_path)

parent_metadata = os.lstat(target_parent)
if (
    not stat.S_ISDIR(parent_metadata.st_mode)
    or parent_metadata.st_uid != 0
    or stat.S_IMODE(parent_metadata.st_mode) & 0o077
):
    raise SystemExit(f"projects.json parent must be a private root-owned directory: {target_parent}")

source_before = os.lstat(source_path)
if not stat.S_ISREG(source_before.st_mode) or source_before.st_nlink != 1:
    raise SystemExit(f"projects.json source must be a single-link regular file: {source_path}")
if source_kind == "private" and (
    source_before.st_uid != 0 or stat.S_IMODE(source_before.st_mode) & 0o077
):
    raise SystemExit(f"projects.json source must be root-owned and private: {source_path}")

source_descriptor = os.open(
    source_path,
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0),
)
temporary_path = ""
try:
    source_after = os.fstat(source_descriptor)
    if (
        not stat.S_ISREG(source_after.st_mode)
        or source_after.st_nlink != 1
        or (source_before.st_dev, source_before.st_ino)
        != (source_after.st_dev, source_after.st_ino)
    ):
        raise SystemExit(f"projects.json source changed while opening: {source_path}")
    if source_kind == "private" and (
        source_after.st_uid != 0 or stat.S_IMODE(source_after.st_mode) & 0o077
    ):
        raise SystemExit(f"projects.json source became unsafe while opening: {source_path}")
    if os.path.lexists(target_path):
        raise SystemExit(f"refusing to replace an existing projects.json: {target_path}")

    temporary_descriptor, temporary_path = tempfile.mkstemp(
        prefix=".projects.json.", suffix=".tmp", dir=target_parent
    )
    try:
        os.fchmod(temporary_descriptor, 0o600)
        os.fchown(temporary_descriptor, 0, 0)
        source_handle = os.fdopen(source_descriptor, "rb")
        source_descriptor = -1
        try:
            target_handle = os.fdopen(temporary_descriptor, "wb")
            temporary_descriptor = -1
        except BaseException:
            source_handle.close()
            raise
        with source_handle, target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
    finally:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)

    if os.path.lexists(target_path):
        raise SystemExit(f"refusing to replace an existing projects.json: {target_path}")
    os.replace(temporary_path, target_path)
    temporary_path = ""
    directory_descriptor = os.open(
        target_parent,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
finally:
    if source_descriptor >= 0:
        os.close(source_descriptor)
    if temporary_path:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass

target_metadata = os.lstat(target_path)
if (
    not stat.S_ISREG(target_metadata.st_mode)
    or target_metadata.st_nlink != 1
    or target_metadata.st_uid != 0
    or stat.S_IMODE(target_metadata.st_mode) != 0o600
):
    raise SystemExit(f"published projects.json has unsafe metadata: {target_path}")
PY
}

migrate_projects_config() {
  local legacy_projects_file="$APP_HOME/projects.json"
  local data_projects_file="$DATA_HOME/projects.json"

  # Revalidate after the original-state backup, immediately before publishing.
  validate_projects_migration_plan
  if [[ -f "$data_projects_file" ]]; then
    if [[ -f "$legacy_projects_file" ]]; then
      if [[ "$PROJECTS_LAYOUT_STATE" == "data" ]]; then
        echo "保留 APP_HOME 中可能过期的旧 projects.json；运行时继续使用 DATA_HOME 版本 / Preserved the possibly stale legacy projects.json; DATA_HOME remains authoritative."
      else
        echo "旧、新 projects.json 完全相同；保留 root 私有旧副本并切换运行时路径 / Legacy and data projects.json are identical; preserved the private legacy copy and switched the runtime path."
      fi
    fi
    return 0
  fi

  if [[ -f "$legacy_projects_file" ]]; then
    atomic_copy_projects_file "$legacy_projects_file" "$data_projects_file" "private"
    if ! projects_files_are_identical "$legacy_projects_file" "$data_projects_file" "private"; then
      echo "迁移后的 projects.json 与旧配置字节不一致，拒绝继续 / Migrated projects.json does not match the legacy source byte-for-byte." >&2
      return 1
    fi
    echo "已将 projects.json 原子迁移到数据目录，旧副本继续保留 / Atomically migrated projects.json to DATA_HOME and preserved the legacy copy."
  else
    atomic_copy_projects_file "$SOURCE_DIR/examples/projects.example.json" "$data_projects_file" "release"
    if ! projects_files_are_identical "$SOURCE_DIR/examples/projects.example.json" "$data_projects_file" "release"; then
      echo "数据目录中的 projects.json 与示例源文件字节不一致，拒绝继续 / Published projects.json does not match its example source byte-for-byte." >&2
      return 1
    fi
    echo "已在数据目录创建 projects.json / Created projects.json in DATA_HOME."
  fi
  validate_private_projects_file "$data_projects_file"
  validate_projects_json_structure "$data_projects_file" "private"
}

restart_previous_agent_on_failure() {
  local original_status=$?

  trap - EXIT
  if [[ "$original_status" -ne 0 \
    && "$UPGRADE_SERVICE_WAS_ACTIVE" == "true" \
    && "$UPGRADE_SERVICE_FAILSAFE_ARMED" == "true" ]]; then
    if systemctl start "$SERVICE_NAME" >/dev/null 2>&1; then
      echo "安装失败；已尽力重新启动升级前运行中的 Agent 服务 / Installation failed; restarted the previously active Agent service." >&2
    else
      echo "安装失败，且无法重新启动升级前运行中的 Agent 服务，请立即检查 systemctl 状态 / Installation failed and the previously active Agent service could not be restarted." >&2
    fi
  fi
  exit "$original_status"
}

stop_existing_agent_for_upgrade() {
  local service_state=""

  [[ "$EXISTING_INSTALLATION" == "true" ]] || return 0
  service_state="$(systemctl is-active "$SERVICE_NAME" 2>/dev/null || true)"
  case "$service_state" in
    active|activating|reloading|deactivating)
      UPGRADE_SERVICE_WAS_ACTIVE="true"
      UPGRADE_SERVICE_FAILSAFE_ARMED="true"
      trap restart_previous_agent_on_failure EXIT
      systemctl stop "$SERVICE_NAME"
      service_state="$(systemctl is-active "$SERVICE_NAME" 2>/dev/null || true)"
      case "$service_state" in
        inactive|failed|unknown) ;;
        *)
          echo "无法确认旧 Agent 服务已停止，拒绝迁移 projects.json / Could not confirm the old Agent service stopped; refusing to migrate projects.json: $service_state" >&2
          return 1
          ;;
      esac
      ;;
    inactive|failed|unknown) ;;
    *)
      echo "无法确定旧 Agent 服务状态，拒绝开始文件迁移 / Could not determine the old Agent service state: $service_state" >&2
      return 1
      ;;
  esac
}

disarm_upgrade_service_failsafe() {
  UPGRADE_SERVICE_FAILSAFE_ARMED="false"
  trap - EXIT
}

upsert_env_assignment() {
  local key="$1"
  local value="$2"

  "$PYTHON_BIN" - "$ENV_FILE" "$key" "$value" <<'PY'
import os
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
output = []
found = False
for line in lines:
    candidate = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
    if candidate == key:
        if not found:
            output.append(f"{key}={value}")
            found = True
        continue
    output.append(line)
if not found:
    output.append(f"{key}={value}")

path.parent.mkdir(parents=True, exist_ok=True)
descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
temporary = Path(temporary_name)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        descriptor = -1
        handle.write("\n".join(output).rstrip() + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)
except BaseException:
    if descriptor >= 0:
        os.close(descriptor)
    temporary.unlink(missing_ok=True)
    raise
PY
}

initialize_admin_credentials() {
  local admin_password=""
  local admin_password_confirm=""
  local password_file="${DEPLOY_UI_PASSWORD_FILE:-}"
  local password_file_mode=""
  local password_file_owner=""
  local password_file_fd=""
  local password_file_path_identity=""
  local password_file_fd_identity=""
  local password_file_link_count=""
  local existing_password_hash=""
  local existing_session_secret=""

  existing_password_hash="$(env_file_value "DEPLOY_UI_PASSWORD_HASH")"
  existing_session_secret="$(env_file_value "DEPLOY_UI_SESSION_SECRET")"
  if [[ -n "$existing_password_hash" && -n "$existing_session_secret" ]]; then
    if DEPLOY_UI_PASSWORD_HASH="$existing_password_hash" \
      DEPLOY_UI_SESSION_SECRET="$existing_session_secret" \
      MINI_DEPLOY_HOME="$APP_HOME" \
      DEPLOY_AGENT_ENV_FILE="$ENV_FILE" \
      "$PYTHON_BIN" "$APP_HOME/agent.py" validate-auth >/dev/null 2>&1; then
      if is_en; then
        echo "Kept existing validated administrator credentials."
      else
        echo "已保留通过强度校验的现有管理员凭据。"
      fi
      return 0
    fi
    echo "现有管理员凭据格式或强度不再满足安全要求，将重新初始化 / Existing administrator credentials are invalid or too weak and must be reinitialized." >&2
  elif [[ -n "$existing_password_hash" || -n "$existing_session_secret" ]]; then
    echo "现有管理员凭据不完整，将重新初始化 / Existing administrator credentials are incomplete and must be reinitialized." >&2
  fi

  if [[ -n "$password_file" ]]; then
    if [[ "$password_file" != /* ]]; then
      echo "管理员密码文件必须使用绝对路径 / Administrator password file must use an absolute path: $password_file" >&2
      return 1
    fi
    assert_no_symlink_components "$password_file"
    if [[ -L "$password_file" || ! -f "$password_file" || ! -r "$password_file" ]]; then
      echo "管理员密码文件不可读 / Administrator password file is not readable: $password_file" >&2
      return 1
    fi
    exec {password_file_fd}<"$password_file"
    if [[ ! -f "/proc/self/fd/$password_file_fd" ]]; then
      echo "管理员密码文件在打开时发生变化 / Administrator password file changed while opening: $password_file" >&2
      return 1
    fi
    password_file_path_identity="$(stat -c '%d:%i' -- "$password_file")"
    password_file_fd_identity="$(stat -Lc '%d:%i' -- "/proc/self/fd/$password_file_fd")"
    password_file_link_count="$(stat -Lc '%h' -- "/proc/self/fd/$password_file_fd")"
    if [[ "$password_file_path_identity" != "$password_file_fd_identity" || "$password_file_link_count" != "1" ]]; then
      echo "管理员密码文件在打开时发生变化或存在硬链接 / Password file changed while opening or has multiple hard links." >&2
      return 1
    fi
    password_file_mode="$(stat -Lc '%a' -- "/proc/self/fd/$password_file_fd")"
    password_file_owner="$(stat -Lc '%u' -- "/proc/self/fd/$password_file_fd")"
    if [[ "$password_file_owner" != "0" ]] || (( (8#$password_file_mode & 077) != 0 )); then
      echo "管理员密码文件必须归 root 所有且权限不高于 0600 / Password file must be root-owned with mode 0600: $password_file" >&2
      return 1
    fi
    IFS= read -r admin_password <&"$password_file_fd" || [[ -n "$admin_password" ]]
    exec {password_file_fd}<&-
  elif [[ -n "${DEPLOY_UI_PASSWORD:-}" ]]; then
    admin_password="$DEPLOY_UI_PASSWORD"
    unset DEPLOY_UI_PASSWORD
  elif [[ -t 0 ]]; then
    if is_en; then
      IFS= read -r -s -p "Set administrator password (minimum 8 characters): " admin_password
      printf '\n'
      IFS= read -r -s -p "Confirm administrator password: " admin_password_confirm
      printf '\n'
    else
      IFS= read -r -s -p "设置管理员密码（至少 8 位）: " admin_password
      printf '\n'
      IFS= read -r -s -p "再次输入管理员密码: " admin_password_confirm
      printf '\n'
    fi
    if [[ "$admin_password" != "$admin_password_confirm" ]]; then
      echo "两次输入的密码不一致 / Administrator passwords do not match." >&2
      return 1
    fi
  else
    cat >&2 <<'EOF'
管理员凭据尚未初始化，非交互安装已安全终止。
请设置 DEPLOY_UI_PASSWORD_FILE（推荐）或 DEPLOY_UI_PASSWORD 后重新运行。
Administrator credentials are missing; non-interactive installation stopped safely.
Set DEPLOY_UI_PASSWORD_FILE (recommended) or DEPLOY_UI_PASSWORD and run again.
EOF
    return 1
  fi

  if [[ -z "$admin_password" ]]; then
    echo "管理员密码不能为空 / Administrator password must not be empty." >&2
    return 1
  fi

  printf '%s\n' "$admin_password" | \
    MINI_DEPLOY_HOME="$APP_HOME" \
    DEPLOY_AGENT_ENV_FILE="$ENV_FILE" \
      python3 "$APP_HOME/agent.py" set-password-stdin
  admin_password=""
  admin_password_confirm=""

  if is_en; then
    echo "Administrator credentials initialized before service startup."
  else
    echo "管理员凭据已在服务启动前完成初始化。"
  fi
}

write_nginx_config() {
  local domain="$1"
  local backup=""
  local nginx_parent=""
  local temporary=""
  if [[ -f "$NGINX_CONF_FILE" ]]; then
    backup="${NGINX_CONF_FILE}.$(date -u '+%Y%m%dT%H%M%SZ').$$.bak"
    install -m 600 "$NGINX_CONF_FILE" "$backup"
    if is_en; then
      echo "Backed up existing Nginx config: $backup"
    else
      echo "已备份原有 Nginx 配置：$backup"
    fi
  fi
  nginx_parent="$(dirname -- "$NGINX_CONF_FILE")"
  if [[ ! -d "$nginx_parent" ]]; then
    install -d -m 755 "$nginx_parent"
  fi
  temporary="$(mktemp "$nginx_parent/.mini-deploy-nginx.XXXXXX")"
  cat >"$temporary" <<EOF
$MANAGED_NGINX_MARKER
log_format mini_deploy_no_query '\$remote_addr [\$time_local] '
                                '"\$request_method \$uri \$server_protocol" \$status \$body_bytes_sent '
                                '"\$http_user_agent"';

server {
    listen 80;
    server_name $domain;

    client_max_body_size 20m;

    location = /deploy/webhook {
        # \$uri deliberately excludes the query string, so compatibility
        # tokens can never enter the Nginx access log.
        access_log /var/log/nginx/mini-deploy-webhook.access.log mini_deploy_no_query;
        proxy_pass http://127.0.0.1:6868/webhook;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location = /deploy/ui {
        return 302 /deploy/ui/;
    }

    location /deploy/ui/ {
        proxy_pass http://127.0.0.1:6868/ui/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location /deploy/ {
        proxy_pass http://127.0.0.1:6868/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
  if ! {
    chown root:root "$temporary" \
      && chmod 644 "$temporary" \
      && mv -T -- "$temporary" "$NGINX_CONF_FILE"
  }; then
    rm -f -- "$temporary"
    return 1
  fi
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
  if ! nginx_install_is_preapproved; then
    if [[ ! -t 0 ]]; then
      if is_en; then
        echo "Nginx is missing and SETUP_NGINX is not preapproved; skipped automatic installation."
      else
        echo "未检测到 Nginx，且 SETUP_NGINX 未明确设为 yes；非交互模式已安全跳过自动安装。"
      fi
      return 1
    fi
    if ! ask_yes_no "$prompt" "y"; then
      return 1
    fi
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

install_docker_if_requested() {
  if command -v docker >/dev/null 2>&1; then
    if is_en; then
      echo "Docker is already installed."
    else
      echo "已检测到 Docker，跳过 Docker 安装。"
    fi
    return 0
  fi

  if [[ "$SETUP_DOCKER" == "0" || "$SETUP_DOCKER" == "false" || "$SETUP_DOCKER" == "no" ]]; then
    return 0
  fi

  local prompt="检测到未安装 Docker。如果你的项目要用 Docker Compose 部署，是否现在自动安装 Docker？"
  if is_en; then
    prompt="Docker is not installed. Install Docker now for Docker Compose deployments?"
  fi

  if [[ "$SETUP_DOCKER" != "yes" && "$SETUP_DOCKER" != "true" && "$SETUP_DOCKER" != "1" ]]; then
    if ! ask_yes_no "$prompt" "n"; then
      if is_en; then
        echo "Skipped Docker installation."
      else
        echo "已跳过 Docker 安装。"
      fi
      return 0
    fi
  fi

  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    apt-get install -y docker.io docker-compose-plugin || apt-get install -y docker.io docker-compose
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y docker docker-compose-plugin || dnf install -y docker
  elif command -v yum >/dev/null 2>&1; then
    yum install -y docker docker-compose-plugin || yum install -y docker
  else
    if is_en; then
      echo "No supported package manager found. Please install Docker manually if your projects need it."
    else
      echo "未找到支持的包管理器。如果你的项目需要 Docker，请先手动安装 Docker。"
    fi
    return 0
  fi

  systemctl enable docker
  systemctl start docker

  if docker compose version >/dev/null 2>&1; then
    if is_en; then
      echo "Docker and Docker Compose are ready."
    else
      echo "Docker 和 Docker Compose 已可用。"
    fi
  else
    if is_en; then
      echo "Docker is installed, but 'docker compose' is not available. Install the Docker Compose plugin before using Docker projects."
    else
      echo "Docker 已安装，但 docker compose 不可用。使用 Docker 项目前，请先安装 Docker Compose 插件。"
    fi
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
  # This generated proxy connects over loopback and overwrites X-Real-IP, so the
  # Agent can safely apply login limits to the real client instead of 127.0.0.1.
  upsert_env_assignment "DEPLOY_TRUST_LOOPBACK_PROXY_HEADERS" "true"
  systemctl restart "$SERVICE_NAME"
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

dashboard_public_address() {
  local address="$DEPLOY_PUBLIC_IP"
  if [[ -z "$address" ]] && command -v curl >/dev/null 2>&1; then
    address="$(curl -4 -fsS --connect-timeout 2 --max-time 4 https://api.ipify.org 2>/dev/null || true)"
  fi
  if python3 - "$address" <<'PY'
import ipaddress
import sys
try:
    address = ipaddress.IPv4Address(sys.argv[1])
    raise SystemExit(0 if address.is_global else 1)
except ValueError:
    raise SystemExit(1)
PY
  then
    printf '%s\n' "$address"
  elif is_en; then
    printf '%s\n' '<server-public-ip>'
  else
    printf '%s\n' '<服务器公网IP>'
  fi
}

check_dashboard_port() {
  python3 - <<'PY'
import socket
import sys
try:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", 6868))
except OSError:
    sys.exit("6868 端口被占用或无法监听，请释放端口后重试 / Cannot bind TCP 6868; release the port before installing.")
PY
}

open_dashboard_firewall() {
  local failed="false"
  if command -v ufw >/dev/null 2>&1 && LC_ALL=C ufw status 2>/dev/null | grep -q '^Status: active'; then
    ufw allow 6868/tcp || failed="true"
  fi
  if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --permanent --add-port=6868/tcp || failed="true"
    firewall-cmd --add-port=6868/tcp || failed="true"
  fi
  if [[ "$failed" == "true" ]]; then
    echo "自动放行失败，请在系统防火墙中放行 TCP 6868 / Allow inbound TCP 6868 in your system firewall." >&2
  fi
}

migrate_nginx_dashboard_port() {
  [[ -f "$NGINX_CONF_FILE" ]] || return 0
  grep -Fq 'http://127.0.0.1:9010/' "$NGINX_CONF_FILE" || return 0
  local backup replacement
  backup="$(mktemp "${NGINX_CONF_FILE}.port-backup.XXXXXX")"
  replacement="$(mktemp "${NGINX_CONF_FILE}.port-new.XXXXXX")"
  cp -p -- "$NGINX_CONF_FILE" "$backup"
  sed 's@http://127\.0\.0\.1:9010/@http://127.0.0.1:6868/@g' "$NGINX_CONF_FILE" >"$replacement"
  chmod --reference="$NGINX_CONF_FILE" "$replacement"
  mv -f -- "$replacement" "$NGINX_CONF_FILE"
  if command -v nginx >/dev/null 2>&1; then
    if ! nginx -t || { systemctl is-active --quiet nginx && ! systemctl reload nginx; }; then
      mv -f -- "$backup" "$NGINX_CONF_FILE"
      nginx -t && systemctl reload nginx || true
      echo "旧 Nginx 入口端口迁移失败，已写回原配置 / Nginx port migration failed; original config restored." >&2
      return 1
    fi
  fi
  rm -f -- "$backup"
}

acquire_maintenance_lock

if [[ -f "$ENV_FILE" ]]; then
  ensure_trusted_parent_directory "$ENV_FILE"
  validate_existing_control_file "$ENV_FILE"
  if [[ "$APP_HOME_EXPLICIT" != "true" ]]; then
    existing_value="$(env_file_value "MINI_DEPLOY_HOME")"
    [[ -z "$existing_value" ]] || APP_HOME="$existing_value"
  fi
  if [[ "$DATA_HOME_EXPLICIT" != "true" ]]; then
    existing_value="$(env_file_value "DEPLOY_AGENT_STATE_FILE")"
    [[ -z "$existing_value" ]] || DATA_HOME="$(dirname -- "$existing_value")"
  fi
  if [[ "$LOG_HOME_EXPLICIT" != "true" ]]; then
    existing_value="$(env_file_value "DEPLOY_AGENT_LOG")"
    [[ -z "$existing_value" ]] || LOG_HOME="$(dirname -- "$existing_value")"
  fi
  if [[ "$SERVICE_NAME_EXPLICIT" != "true" ]]; then
    existing_value="$(env_file_value "DEPLOY_AGENT_SERVICE_NAME")"
    [[ -z "$existing_value" ]] || SERVICE_NAME="$existing_value"
  fi
fi

if [[ ! "$SERVICE_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_.@-]*$ ]]; then
  echo "systemd 服务名不安全 / Invalid systemd service name: $SERVICE_NAME" >&2
  exit 1
fi
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
if [[ -L "$SERVICE_FILE" ]]; then
  echo "systemd unit 路径不能是符号链接 / systemd unit path must not be a symbolic link: $SERVICE_FILE" >&2
  exit 1
fi
for path in "$APP_HOME" "$ENV_FILE" "$DATA_HOME" "$LOG_HOME" "$INSTALL_BACKUP_DIR" "$SERVICE_FILE" "$NGINX_CONF_FILE"; do
  if [[ "$path" != /* ]]; then
    echo "安装路径必须是绝对路径 / Installation paths must be absolute: $path" >&2
    exit 1
  fi
  assert_no_symlink_components "$path"
done

choose_language

if [[ -z "$DEPLOY_DOMAIN" ]] && nginx_install_is_preapproved; then
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

APP_HOME="$(readlink -m -- "$APP_HOME")"
ENV_FILE="$(readlink -m -- "$ENV_FILE")"
DATA_HOME="$(readlink -m -- "$DATA_HOME")"
LOG_HOME="$(readlink -m -- "$LOG_HOME")"
INSTALL_BACKUP_DIR="$(readlink -m -- "$INSTALL_BACKUP_DIR")"
NGINX_CONF_FILE="$(readlink -m -- "$NGINX_CONF_FILE")"

for path in "$APP_HOME" "$ENV_FILE" "$DATA_HOME" "$LOG_HOME" "$INSTALL_BACKUP_DIR" "$NGINX_CONF_FILE" "$PYTHON_BIN"; do
  if [[ ! "$path" =~ ^/[A-Za-z0-9_./@+-]+$ ]]; then
    echo "安装路径包含不支持的字符 / Installation path contains unsupported characters: $path" >&2
    exit 1
  fi
done

is_dangerous_managed_directory() {
  case "$1" in
    /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/media|/mnt|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/usr/local|/usr/local/bin|/usr/local/lib|/usr/local/sbin|/usr/local/share|/var|/var/backups|/var/lib|/var/log|/var/tmp|/var/www) return 0 ;;
    /bin/*|/boot/*|/dev/*|/etc/*|/home/*|/lib/*|/lib64/*|/proc/*|/root/*|/run/*|/sbin/*|/sys/*|/tmp/*|/usr/bin/*|/usr/lib/*|/usr/lib64/*|/usr/local/bin/*|/usr/local/lib/*|/usr/local/sbin/*|/usr/local/share/*|/usr/sbin/*|/var/tmp/*) return 0 ;;
    *) return 1 ;;
  esac
}

is_dangerous_app_directory() {
  case "$1" in
    /home/*|/root/*|/usr/local/share/*|/var/backups/*|/var/log/*) return 0 ;;
    *) return 1 ;;
  esac
}

paths_overlap() {
  [[ "$1" == "$2" || "$1" == "$2/"* || "$2" == "$1/"* ]]
}

path_is_within() {
  [[ "$1" == "$2" || "$1" == "$2/"* ]]
}

for managed_path in "$APP_HOME" "$DATA_HOME" "$LOG_HOME" "$INSTALL_BACKUP_DIR"; do
  if is_dangerous_managed_directory "$managed_path"; then
    echo "拒绝危险的安装目录 / Refusing dangerous managed directory: $managed_path" >&2
    exit 1
  fi
done

if is_dangerous_app_directory "$APP_HOME"; then
  echo "APP_HOME 必须是专用程序目录，不能位于用户主目录或系统数据目录 / APP_HOME must be a dedicated application directory: $APP_HOME" >&2
  exit 1
fi

for control_path in "$ENV_FILE" "$NGINX_CONF_FILE"; do
  case "$control_path" in
    /dev/*|/home/*|/proc/*|/root/*|/run/*|/sys/*|/tmp/*|/var/tmp/*)
      echo "拒绝不可信的控制配置路径 / Refusing unsafe control file path: $control_path" >&2
      exit 1
      ;;
  esac
done

if paths_overlap "$APP_HOME" "$DATA_HOME" \
  || paths_overlap "$APP_HOME" "$LOG_HOME" \
  || paths_overlap "$APP_HOME" "$INSTALL_BACKUP_DIR" \
  || paths_overlap "$DATA_HOME" "$LOG_HOME" \
  || paths_overlap "$DATA_HOME" "$INSTALL_BACKUP_DIR" \
  || paths_overlap "$LOG_HOME" "$INSTALL_BACKUP_DIR"; then
  echo "程序、数据、日志和备份目录不能互相包含 / Managed directories must not overlap." >&2
  exit 1
fi

case "$(basename -- "$ENV_FILE")" in
  *.env|*mini-deploy*|*mini_deploy*) ;;
  *)
    echo "ENV_FILE 必须使用专用 .env 或 mini-deploy 文件名 / ENV_FILE must be a dedicated .env or mini-deploy file: $ENV_FILE" >&2
    exit 1
    ;;
esac

for managed_path in "$APP_HOME" "$DATA_HOME" "$LOG_HOME" "$INSTALL_BACKUP_DIR"; do
  for control_path in "$ENV_FILE" "$NGINX_CONF_FILE"; do
    if path_is_within "$control_path" "$managed_path"; then
      echo "ENV_FILE 和 NGINX_CONF_FILE 必须位于所有托管目录之外 / Control files must be outside managed directories: $control_path" >&2
      exit 1
    fi
  done
done

if [[ "$(basename -- "$NGINX_CONF_FILE")" != *.conf ]]; then
  echo "Nginx 配置文件必须以 .conf 结尾 / Nginx config must end in .conf: $NGINX_CONF_FILE" >&2
  exit 1
fi

if [[ -f "$ENV_FILE" ]]; then
  assert_managed_env_value "MINI_DEPLOY_HOME" "$APP_HOME"
  assert_managed_env_value "DEPLOY_AGENT_ENV_FILE" "$ENV_FILE"
  assert_projects_env_layout
  assert_managed_env_value "DEPLOY_AGENT_LOG" "$LOG_HOME/mini-deploy-agent.log"
  assert_managed_env_value "DEPLOY_LOG_FILE" "$LOG_HOME/mini_deploy.log"
  assert_managed_env_value "DEPLOY_AGENT_STATE_FILE" "$DATA_HOME/state.json"
  assert_managed_env_value "DEPLOY_AUDIT_LOG_FILE" "$DATA_HOME/audit.jsonl"
  assert_managed_env_value "DEPLOY_PROJECT_CONFIG_BACKUP_DIR" "$DATA_HOME/backups"
  existing_service_name="$(env_file_value "DEPLOY_AGENT_SERVICE_NAME")"
  if [[ -n "$existing_service_name" && "$existing_service_name" != "$SERVICE_NAME" ]]; then
    echo "现有 DEPLOY_AGENT_SERVICE_NAME 与本次 SERVICE_NAME 不一致，拒绝静默迁移服务 / Existing Agent service name conflicts with SERVICE_NAME." >&2
    exit 1
  fi
fi

if [[ "$SOURCE_DIR" != "$APP_HOME" ]] && paths_overlap "$SOURCE_DIR" "$APP_HOME"; then
  echo "源码目录和 APP_HOME 不能互相包含 / SOURCE_DIR and APP_HOME must not overlap." >&2
  exit 1
fi

validate_release_source_tree

if [[ -e "$APP_HOME" && ! -d "$APP_HOME" ]]; then
  echo "APP_HOME 必须是目录 / APP_HOME must be a directory: $APP_HOME" >&2
  exit 1
fi

if [[ -d "$APP_HOME" ]] && directory_has_entries "$APP_HOME"; then
  marker="$APP_HOME/$INSTALL_MARKER_NAME"
  if [[ -e "$marker" || -L "$marker" ]]; then
    if ! has_valid_install_marker; then
      echo "安装标记无效或不安全，拒绝覆盖 / Invalid or unsafe installation marker: $marker" >&2
      exit 1
    fi
    EXISTING_INSTALLATION="true"
  elif looks_like_legacy_installation; then
    if [[ "$SOURCE_DIR" == "$APP_HOME" \
      && ! -e "$ENV_FILE" \
      && ! -e "$SERVICE_FILE" \
      && ! -e "$APP_HOME/projects.json" \
      && ! -e "$DATA_HOME/.mini-deploy-data" \
      && ! -e "$DATA_HOME/state.json" \
      && ! -e "$DATA_HOME/audit.jsonl" \
      && ! -e "$DATA_HOME/projects.json" ]]; then
      : # A source checkout being installed in place is not an upgrade yet.
    elif legacy_adoption_enabled; then
      EXISTING_INSTALLATION="true"
      LEGACY_INSTALLATION="true"
      echo "检测到未带安装标记的旧版目录，将在原样备份后接管 / Legacy installation detected; it will be adopted after an original-state backup."
    else
      echo "检测到无托管标记的旧版目录。请先核对路径；确认接管时临时设置 DEPLOY_ALLOW_LEGACY_INSTALL_ADOPTION=true / Unmarked legacy installation requires explicit adoption: $APP_HOME" >&2
      exit 1
    fi
  else
    echo "APP_HOME 非空但不是可识别的 mini_deploy 安装，拒绝使用 --delete 覆盖 / Refusing to overwrite an unrecognized non-empty APP_HOME: $APP_HOME" >&2
    exit 1
  fi
fi

# Apart from the private maintenance lock, everything through the backup call
# is read-only. A validation failure leaves the installed tree and controls intact.
assert_trusted_directory_chain "$(dirname -- "$APP_HOME")"
if [[ "$EXISTING_INSTALLATION" == "true" ]]; then
  assert_trusted_directory_chain "$APP_HOME"
fi
for managed_directory in "$DATA_HOME" "$LOG_HOME" "$INSTALL_BACKUP_DIR"; do
  if [[ -e "$managed_directory" || -L "$managed_directory" ]]; then
    if [[ -L "$managed_directory" || ! -d "$managed_directory" ]]; then
      echo "托管路径必须是普通非符号链接目录 / Managed path must be a regular non-symlink directory: $managed_directory" >&2
      exit 1
    fi
    assert_trusted_directory_chain "$managed_directory"
  else
    assert_trusted_directory_chain "$(dirname -- "$managed_directory")"
  fi
done
for control_path in "$ENV_FILE" "$SERVICE_FILE" "$NGINX_CONF_FILE"; do
  assert_trusted_directory_chain "$(dirname -- "$control_path")"
done
validate_existing_control_file "$ENV_FILE"
validate_existing_control_file "$SERVICE_FILE"
validate_existing_control_file "$NGINX_CONF_FILE"

if [[ -s "$ENV_FILE" ]] && ! grep -Eq '^(MINI_DEPLOY_HOME|DEPLOY_AGENT_[A-Z0-9_]+)=' "$ENV_FILE"; then
  echo "已有 ENV_FILE 不像 mini_deploy 配置，拒绝改写 / Existing ENV_FILE is not recognized as mini_deploy configuration: $ENV_FILE" >&2
  exit 1
fi
validate_managed_service_file
validate_managed_nginx_file

validate_projects_migration_plan

data_marker="$DATA_HOME/.mini-deploy-data"
if [[ -e "$data_marker" || -L "$data_marker" ]]; then
  if ! has_valid_data_marker; then
    echo "数据目录标记无效、不安全或绑定到了其他路径 / Invalid, unsafe, or mismatched data directory marker: $data_marker" >&2
    exit 1
  fi
elif [[ -d "$DATA_HOME" ]] && directory_has_entries "$DATA_HOME" \
  && [[ ! -f "$DATA_HOME/state.json" \
    && ! -f "$DATA_HOME/audit.jsonl" \
    && ! -f "$DATA_HOME/projects.json" ]]; then
  echo "DATA_HOME 非空但不是可识别的 mini_deploy 数据目录 / Refusing an unrecognized non-empty DATA_HOME: $DATA_HOME" >&2
  exit 1
fi

if [[ -d "$APP_HOME" && "$(cd "$APP_HOME" && pwd -P)" == "$SOURCE_DIR" ]]; then
  SAME_SOURCE_AND_TARGET="true"
fi

if [[ -d "$APP_HOME" ]]; then
  validate_app_release_tree_safety
fi

if [[ "$EXISTING_INSTALLATION" == "true" ]]; then
  UPGRADE_BACKUP_PATH="$(
    APP_HOME="$APP_HOME" \
    ENV_FILE="$ENV_FILE" \
    DATA_HOME="$DATA_HOME" \
    LOG_HOME="$LOG_HOME" \
    SERVICE_NAME="$SERVICE_NAME" \
    SERVICE_FILE="$SERVICE_FILE" \
    NGINX_CONF_FILE="$NGINX_CONF_FILE" \
    BACKUP_DIR="$INSTALL_BACKUP_DIR" \
    BACKUP_ALLOW_LEGACY_INSTALLATION="$LEGACY_INSTALLATION" \
    BACKUP_MAINTENANCE_LOCK_HELD="true" \
    BACKUP_MAINTENANCE_LOCK_FD="$MAINTENANCE_LOCK_FD" \
      bash "$SOURCE_DIR/scripts/backup-installation.sh"
  )"
  python3 "$SOURCE_DIR/scripts/verify_backup.py" "$UPGRADE_BACKUP_PATH" >/dev/null
  if is_en; then
    echo "Created and verified a non-destructive upgrade backup: $UPGRADE_BACKUP_PATH"
    echo "This installer never deletes upgrade backups automatically."
  else
    echo "已创建并校验非破坏性升级备份：$UPGRADE_BACKUP_PATH"
    echo "安装器不会自动删除升级备份。"
  fi
fi

# Legacy Agent versions do not participate in the maintenance lock. Stop an
# active old service only after its original state has been backed up and the
# bundle verified, then keep an EXIT fail-safe armed until the new service runs.
stop_existing_agent_for_upgrade

# Only now may the installer mutate the system. Persistent entries such as
# workspace and logs keep their existing owner/mode; release code is root-owned.
if [[ ! -d "$APP_HOME" ]]; then
  install -d -m 755 "$APP_HOME"
fi
assert_trusted_directory_chain "$APP_HOME"

ensure_root_directory "$DATA_HOME" 700
ensure_root_directory "$LOG_HOME" 755
ensure_root_directory "$INSTALL_BACKUP_DIR" 700
ensure_trusted_parent_directory "$ENV_FILE"
ensure_trusted_parent_directory "$SERVICE_FILE"
ensure_trusted_parent_directory "$NGINX_CONF_FILE"

install -m 600 /dev/null "$data_marker"
{
  printf 'format=1\n'
  printf 'data_home=%s\n' "$DATA_HOME"
} >"$data_marker"
chown root:root "$data_marker"
chmod 600 "$data_marker"

# Project Tokens move only after the verified original-state backup. The
# legacy APP_HOME copy remains excluded from release replacement as a private,
# read-only recovery copy; DATA_HOME/projects.json becomes authoritative.
migrate_projects_config

secure_app_release_files

if [[ "$SAME_SOURCE_AND_TARGET" == "true" ]]; then
  if is_en; then
    echo "Source directory is already $APP_HOME; skipped file copy."
  else
    echo "当前目录已经是 $APP_HOME，跳过文件复制。"
  fi
elif command -v rsync >/dev/null 2>&1; then
  validate_release_source_tree
  validate_app_release_tree_safety
  rsync -a -x --delete \
    --chown=root:root \
    --chmod=go-w \
    --exclude '/.git/' \
    --exclude '/__pycache__/' \
    --exclude '/.mini-deploy-install' \
    --exclude '/projects.json' \
    --exclude '/.backups/' \
    --exclude '/workspace/' \
    --exclude '/state.json' \
    --exclude '/audit.jsonl' \
    --exclude '/.env' \
    --exclude '/server.env' \
    --exclude '/*.log' \
    "$SOURCE_DIR/" "$APP_HOME/"
else
  copy_without_rsync
fi

secure_app_release_files
chmod +x "$APP_HOME/agent.py" "$APP_HOME/scripts/"*.sh

if [[ -L "$ENV_FILE" || ( -e "$ENV_FILE" && ! -f "$ENV_FILE" ) ]]; then
  echo "ENV_FILE 必须是普通文件且不能是符号链接 / ENV_FILE must be a regular non-symlink file: $ENV_FILE" >&2
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  install -m 600 "$APP_HOME/env.example" "$ENV_FILE"
  if is_en; then
    echo "Created environment config: $ENV_FILE"
  else
    echo "已创建环境配置：$ENV_FILE"
  fi
else
  if is_en; then
    echo "Kept existing environment config and synchronized installer-managed paths: $ENV_FILE"
  else
    echo "已保留环境配置，并同步安装器管理的路径：$ENV_FILE"
  fi
fi
chmod 600 "$ENV_FILE"
upsert_env_assignment "MINI_DEPLOY_HOME" "$APP_HOME"
upsert_env_assignment "DEPLOY_AGENT_HOST" "0.0.0.0"
upsert_env_assignment "DEPLOY_AGENT_PORT" "6868"
upsert_env_assignment "DEPLOY_COOKIE_SECURE" "auto"
upsert_env_assignment "DEPLOY_AGENT_ENV_FILE" "$ENV_FILE"
upsert_env_assignment "DEPLOY_AGENT_SERVICE_NAME" "$SERVICE_NAME"
upsert_env_assignment "DEPLOY_PROJECTS_FILE" "$DATA_HOME/projects.json"
upsert_env_assignment "DEPLOY_AGENT_LOG" "$LOG_HOME/mini-deploy-agent.log"
upsert_env_assignment "DEPLOY_LOG_FILE" "$LOG_HOME/mini_deploy.log"
upsert_env_assignment "DEPLOY_AGENT_STATE_FILE" "$DATA_HOME/state.json"
upsert_env_assignment "DEPLOY_AUDIT_LOG_FILE" "$DATA_HOME/audit.jsonl"
upsert_env_assignment "DEPLOY_PROJECT_CONFIG_BACKUP_DIR" "$DATA_HOME/backups"

validate_private_projects_file "$DATA_HOME/projects.json"
chown root:root "$ENV_FILE" "$DATA_HOME/projects.json"
chmod 600 "$ENV_FILE" "$DATA_HOME/projects.json"

marker="$APP_HOME/$INSTALL_MARKER_NAME"
if [[ -L "$marker" || ( -e "$marker" && ! -f "$marker" ) ]]; then
  echo "安装标记路径不安全 / Unsafe installation marker path: $marker" >&2
  exit 1
fi
install -m 644 /dev/null "$marker"
{
  printf 'format=2\n'
  printf 'installed_at=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  printf 'app_home=%s\n' "$APP_HOME"
  printf 'release_fingerprint=sha256:%s\n' "$(sha256sum "$APP_HOME/agent.py" | awk '{print $1}')"
} >"$marker"
chown root:root "$marker"
chmod 644 "$marker"

initialize_admin_credentials

check_dashboard_port
write_systemd_service
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
if ! systemctl is-active --quiet "$SERVICE_NAME"; then
  echo "新 Agent 服务启动后未保持 active 状态 / New Agent service did not remain active after restart." >&2
  exit 1
fi
disarm_upgrade_service_failsafe

open_dashboard_firewall
migrate_nginx_dashboard_port

if nginx_setup_enabled; then
  setup_nginx "$DEPLOY_DOMAIN"
fi

install_docker_if_requested

if curl -fsS --max-time 5 http://127.0.0.1:6868/health >/dev/null 2>&1; then
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

PUBLIC_ADDRESS="$(dashboard_public_address)"
DASHBOARD_URL="http://$PUBLIC_ADDRESS:6868"

if is_en; then
  cat <<EOF

mini_deploy installed.

Agent health: $HEALTH_RESULT
Administrator credentials: initialized

Open the dashboard and sign in:
   $DASHBOARD_URL

Local fallback URL:
   http://127.0.0.1:6868

Cloud firewall/security group: allow inbound TCP 6868.
The port is fixed; DEPLOY_AGENT_HOST/DEPLOY_AGENT_PORT no longer override it.
Optional domain: $DEPLOY_DOMAIN

Useful commands:
   systemctl status $SERVICE_NAME
   journalctl -u $SERVICE_NAME -f
   DEPLOY_AGENT_ENV_FILE=$ENV_FILE bash $APP_HOME/scripts/doctor.sh

Administrator maintenance (restart the Agent after either command):
   DEPLOY_AGENT_ENV_FILE=$ENV_FILE DEPLOY_AGENT_SERVICE_NAME=$SERVICE_NAME $PYTHON_BIN $APP_HOME/agent.py admin set-password
   DEPLOY_AGENT_ENV_FILE=$ENV_FILE DEPLOY_AGENT_SERVICE_NAME=$SERVICE_NAME $PYTHON_BIN $APP_HOME/agent.py admin reset-session
   systemctl restart $SERVICE_NAME

Manual safety backup:
   APP_HOME=$APP_HOME ENV_FILE=$ENV_FILE DATA_HOME=$DATA_HOME LOG_HOME=$LOG_HOME SERVICE_NAME=$SERVICE_NAME SERVICE_FILE=$SERVICE_FILE NGINX_CONF_FILE=$NGINX_CONF_FILE BACKUP_DIR=$INSTALL_BACKUP_DIR bash $APP_HOME/scripts/backup-installation.sh

Optional: edit environment config:
   nano $ENV_FILE

Preferred: add or edit projects in the web panel.

Advanced: after editing projects directly, restore private permissions,
validate the exact configuration, and restart the Agent:
   nano $DATA_HOME/projects.json
   chown root:root $DATA_HOME/projects.json
   chmod 600 $DATA_HOME/projects.json
   MINI_DEPLOY_HOME=$APP_HOME DEPLOY_AGENT_STATE_FILE=$DATA_HOME/state.json DEPLOY_PROJECTS_FILE=$DATA_HOME/projects.json $PYTHON_BIN $APP_HOME/agent.py validate-config
   systemctl restart $SERVICE_NAME

Health check:
   curl http://127.0.0.1:6868/health

EOF
else
  cat <<EOF

mini_deploy 安装完成。

Agent 状态：$HEALTH_RESULT
管理员凭据：已初始化

打开下面地址并登录：
   $DASHBOARD_URL

本机备用访问地址：
   http://127.0.0.1:6868

请在云厂商安全组中放行入站 TCP 6868。
端口固定，DEPLOY_AGENT_HOST / DEPLOY_AGENT_PORT 不再支持覆盖监听地址。
可选域名：$DEPLOY_DOMAIN

常用命令：
   systemctl status $SERVICE_NAME
   journalctl -u $SERVICE_NAME -f
   DEPLOY_AGENT_ENV_FILE=$ENV_FILE bash $APP_HOME/scripts/doctor.sh

管理员维护（执行任一命令后重启 Agent）：
   DEPLOY_AGENT_ENV_FILE=$ENV_FILE DEPLOY_AGENT_SERVICE_NAME=$SERVICE_NAME $PYTHON_BIN $APP_HOME/agent.py admin set-password
   DEPLOY_AGENT_ENV_FILE=$ENV_FILE DEPLOY_AGENT_SERVICE_NAME=$SERVICE_NAME $PYTHON_BIN $APP_HOME/agent.py admin reset-session
   systemctl restart $SERVICE_NAME

手动创建安全备份：
   APP_HOME=$APP_HOME ENV_FILE=$ENV_FILE DATA_HOME=$DATA_HOME LOG_HOME=$LOG_HOME SERVICE_NAME=$SERVICE_NAME SERVICE_FILE=$SERVICE_FILE NGINX_CONF_FILE=$NGINX_CONF_FILE BACKUP_DIR=$INSTALL_BACKUP_DIR bash $APP_HOME/scripts/backup-installation.sh

可选：修改环境配置：
   nano $ENV_FILE

推荐：在网页面板中添加或修改项目。

高级用法：直接修改项目配置后，请恢复私有权限、校验精确配置并重启 Agent：
   nano $DATA_HOME/projects.json
   chown root:root $DATA_HOME/projects.json
   chmod 600 $DATA_HOME/projects.json
   MINI_DEPLOY_HOME=$APP_HOME DEPLOY_AGENT_STATE_FILE=$DATA_HOME/state.json DEPLOY_PROJECTS_FILE=$DATA_HOME/projects.json $PYTHON_BIN $APP_HOME/agent.py validate-config
   systemctl restart $SERVICE_NAME

健康检查：
   curl http://127.0.0.1:6868/health

EOF
fi
