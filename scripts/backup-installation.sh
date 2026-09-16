#!/usr/bin/env bash
set -Eeuo pipefail
PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

APP_HOME="${APP_HOME:-/opt/mini_deploy}"
ENV_FILE="${ENV_FILE:-/etc/mini-deploy-agent.env}"
DATA_HOME="${DATA_HOME:-/var/lib/mini-deploy-agent}"
LOG_HOME="${LOG_HOME:-/var/log/mini_deploy}"
SERVICE_NAME="${SERVICE_NAME:-mini-deploy-agent}"
SERVICE_FILE="${SERVICE_FILE:-/etc/systemd/system/${SERVICE_NAME}.service}"
NGINX_CONF_FILE="${NGINX_CONF_FILE:-/etc/nginx/conf.d/mini-deploy.conf}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/mini-deploy-agent}"
BACKUP_ALLOW_LEGACY_INSTALLATION="${BACKUP_ALLOW_LEGACY_INSTALLATION:-false}"
BACKUP_MAINTENANCE_LOCK_HELD="${BACKUP_MAINTENANCE_LOCK_HELD:-false}"
BACKUP_MAINTENANCE_LOCK_FD="${BACKUP_MAINTENANCE_LOCK_FD:-}"
MAINTENANCE_LOCK_FILE="/run/mini-deploy-agent/maintenance.lock"
LEGACY_INSTALLATION="no"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Please run as root or use sudo." >&2
  exit 1
fi

for command in awk dirname flock grep install mktemp mv python3 readlink sha256sum stat sync tar; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Missing backup dependency: $command" >&2
    exit 1
  fi
done
PYTHON_BIN="$(readlink -f -- "$(command -v python3)")"
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Python 3.10 or newer is required." >&2
  exit 1
fi

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
    echo "Unsafe maintenance lock root: $lock_root" >&2
    return 1
  fi
  lock_root_owner="$(stat -c '%u' -- "$lock_root")"
  lock_root_mode="$(stat -c '%a' -- "$lock_root")"
  if [[ "$lock_root_owner" != "0" ]] || (( (8#$lock_root_mode & 022) != 0 )); then
    echo "Unsafe maintenance lock root: $lock_root" >&2
    return 1
  fi
  if [[ ! -e "$lock_parent" ]]; then
    install -d -m 700 "$lock_parent"
  elif [[ -L "$lock_parent" || ! -d "$lock_parent" ]]; then
    echo "Maintenance lock directory is unavailable: $lock_parent" >&2
    return 1
  fi
  lock_owner="$(stat -c '%u' -- "$lock_parent")"
  lock_group="$(stat -c '%g' -- "$lock_parent")"
  lock_mode="$(stat -c '%a' -- "$lock_parent")"
  if [[ "$lock_owner" != "0" || "$lock_group" != "0" ]] || (( (8#$lock_mode & 077) != 0 )); then
    echo "Unsafe maintenance lock directory: $lock_parent" >&2
    return 1
  fi
  if [[ -L "$MAINTENANCE_LOCK_FILE" || ( -e "$MAINTENANCE_LOCK_FILE" && ! -f "$MAINTENANCE_LOCK_FILE" ) ]]; then
    echo "Unsafe maintenance lock path: $MAINTENANCE_LOCK_FILE" >&2
    return 1
  fi
  case "${BACKUP_MAINTENANCE_LOCK_HELD,,}" in
    1|true|yes|on|enabled)
      if [[ ! "$BACKUP_MAINTENANCE_LOCK_FD" =~ ^[0-9]+$ ]] \
        || [[ ! -f "/proc/self/fd/$BACKUP_MAINTENANCE_LOCK_FD" ]]; then
        echo "The inherited maintenance lock descriptor is unavailable." >&2
        return 1
      fi
      lock_path_identity="$(stat -c '%d:%i' -- "$MAINTENANCE_LOCK_FILE")"
      lock_fd_identity="$(stat -Lc '%d:%i' -- "/proc/self/fd/$BACKUP_MAINTENANCE_LOCK_FD")"
      lock_link_count="$(stat -Lc '%h' -- "/proc/self/fd/$BACKUP_MAINTENANCE_LOCK_FD")"
      if [[ "$lock_path_identity" != "$lock_fd_identity" || "$lock_link_count" != "1" ]]; then
        echo "The inherited maintenance lock changed while opening or has multiple hard links." >&2
        return 1
      fi
      lock_owner="$(stat -Lc '%u' -- "/proc/self/fd/$BACKUP_MAINTENANCE_LOCK_FD")"
      lock_group="$(stat -Lc '%g' -- "/proc/self/fd/$BACKUP_MAINTENANCE_LOCK_FD")"
      lock_mode="$(stat -Lc '%a' -- "/proc/self/fd/$BACKUP_MAINTENANCE_LOCK_FD")"
      if [[ "$lock_owner" != "0" || "$lock_group" != "0" ]] || (( (8#$lock_mode & 077) != 0 )); then
        echo "The inherited maintenance lock has unsafe ownership or mode." >&2
        return 1
      fi
      if ! flock -n "$BACKUP_MAINTENANCE_LOCK_FD"; then
        echo "The inherited maintenance lock is not held." >&2
        return 1
      fi
      return 0
      ;;
  esac
  previous_umask="$(umask)"
  umask 077
  exec {MAINTENANCE_LOCK_FD}>>"$MAINTENANCE_LOCK_FILE"
  umask "$previous_umask"
  if [[ ! -f "/proc/self/fd/$MAINTENANCE_LOCK_FD" ]]; then
    echo "Maintenance lock is not a regular file after opening." >&2
    return 1
  fi
  lock_path_identity="$(stat -c '%d:%i' -- "$MAINTENANCE_LOCK_FILE")"
  lock_fd_identity="$(stat -Lc '%d:%i' -- "/proc/self/fd/$MAINTENANCE_LOCK_FD")"
  lock_link_count="$(stat -Lc '%h' -- "/proc/self/fd/$MAINTENANCE_LOCK_FD")"
  if [[ "$lock_path_identity" != "$lock_fd_identity" || "$lock_link_count" != "1" ]]; then
    echo "Maintenance lock changed while opening or has multiple hard links." >&2
    return 1
  fi
  chown root:root "/proc/self/fd/$MAINTENANCE_LOCK_FD"
  chmod 600 "/proc/self/fd/$MAINTENANCE_LOCK_FD"
  if ! flock -n "$MAINTENANCE_LOCK_FD"; then
    echo "Another install, backup, or maintenance operation is active." >&2
    return 1
  fi
}

acquire_maintenance_lock

for path in "$ENV_FILE" "$SERVICE_FILE" "$NGINX_CONF_FILE" "$BACKUP_DIR"; do
  if [[ -L "$path" ]]; then
    echo "Backup control paths must not be symbolic links: $path" >&2
    exit 1
  fi
done

assert_no_symlink_components() {
  local requested="$1"
  local current="/"
  local component=""
  local -a components=()

  IFS='/' read -r -a components <<< "${requested#/}"
  for component in "${components[@]}"; do
    [[ -n "$component" && "$component" != "." ]] || continue
    if [[ "$component" == ".." ]]; then
      echo "Backup paths must not contain '..' components: $requested" >&2
      return 1
    fi
    current="${current%/}/$component"
    if [[ -L "$current" ]]; then
      echo "Backup paths must not traverse symbolic links: $current" >&2
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
                print(f"{label} must not contain nested mount points: {target}", file=sys.stderr)
                raise SystemExit(1)
except OSError as exc:
    print(f"cannot inspect mount points for {label}: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
}

for path in "$APP_HOME" "$ENV_FILE" "$DATA_HOME" "$LOG_HOME" "$SERVICE_FILE" "$NGINX_CONF_FILE" "$BACKUP_DIR"; do
  if [[ "$path" != /* ]]; then
    echo "Backup paths must be absolute: $path" >&2
    exit 1
  fi
  assert_no_symlink_components "$path"
done

APP_HOME="$(readlink -m -- "$APP_HOME")"
ENV_FILE="$(readlink -m -- "$ENV_FILE")"
DATA_HOME="$(readlink -m -- "$DATA_HOME")"
LOG_HOME="$(readlink -m -- "$LOG_HOME")"
SERVICE_FILE="$(readlink -m -- "$SERVICE_FILE")"
NGINX_CONF_FILE="$(readlink -m -- "$NGINX_CONF_FILE")"
BACKUP_DIR="$(readlink -m -- "$BACKUP_DIR")"

is_dangerous_managed_directory() {
  case "$1" in
    /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/media|/mnt|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/usr/local|/usr/local/bin|/usr/local/lib|/usr/local/sbin|/usr/local/share|/var|/var/backups|/var/lib|/var/log|/var/tmp|/var/www) return 0 ;;
    /bin/*|/boot/*|/dev/*|/etc/*|/home/*|/lib/*|/lib64/*|/proc/*|/root/*|/run/*|/sbin/*|/sys/*|/tmp/*|/usr/bin/*|/usr/lib/*|/usr/lib64/*|/usr/local/bin/*|/usr/local/lib/*|/usr/local/sbin/*|/usr/local/share/*|/usr/sbin/*|/var/tmp/*) return 0 ;;
    *) return 1 ;;
  esac
}

paths_overlap() {
  [[ "$1" == "$2" || "$1" == "$2/"* || "$2" == "$1/"* ]]
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
      echo "Backup path ancestor is not a regular directory: $current" >&2
      return 1
    fi
    owner="$(stat -c '%u' -- "$current")"
    mode="$(stat -c '%a' -- "$current")"
    if [[ "$owner" != "0" ]] || (( (8#$mode & 022) != 0 )); then
      echo "Backup path ancestor must be root-owned and not group/other writable: $current" >&2
      return 1
    fi
    [[ "$current" == "/" ]] && break
    parent="$(dirname -- "$current")"
    [[ "$parent" != "$current" ]] || break
    current="$parent"
  done
}

has_safe_marker_file() {
  local marker="$1"
  local directory=""
  local mode=""
  [[ -f "$marker" && ! -L "$marker" ]] || return 1
  [[ "$(stat -c '%u' -- "$marker")" == "0" ]] || return 1
  [[ "$(stat -c '%h' -- "$marker")" == "1" ]] || return 1
  mode="$(stat -c '%a' -- "$marker")"
  (( (8#$mode & 022) == 0 )) || return 1
  directory="$(dirname -- "$marker")"
  [[ "$(stat -c '%u' -- "$directory")" == "0" ]] || return 1
  mode="$(stat -c '%a' -- "$directory")"
  (( (8#$mode & 022) == 0 )) || return 1
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

has_valid_app_marker() {
  local marker="$APP_HOME/.mini-deploy-install"
  local format=""
  local marker_app_home=""
  has_safe_marker_file "$marker" || return 1
  format="$(marker_unique_field "$marker" "format")" || return 1
  [[ "$format" == "1" || "$format" == "2" ]] || return 1
  marker_app_home="$(marker_unique_field "$marker" "app_home")" || return 1
  [[ "$marker_app_home" == "$APP_HOME" ]]
}

has_valid_data_marker() {
  local marker="$DATA_HOME/.mini-deploy-data"
  local format=""
  local marker_data_home=""
  has_safe_marker_file "$marker" || return 1
  format="$(marker_unique_field "$marker" "format")" || return 1
  [[ "$format" == "1" ]] || return 1
  if grep -q '^data_home=' "$marker"; then
    marker_data_home="$(marker_unique_field "$marker" "data_home")" || return 1
    [[ "$marker_data_home" == "$DATA_HOME" ]]
  fi
}

legacy_backup_enabled() {
  case "${BACKUP_ALLOW_LEGACY_INSTALLATION,,}" in
    1|true|yes|on|enabled) return 0 ;;
    *) return 1 ;;
  esac
}

if [[ ! -d "$APP_HOME" ]]; then
  echo "APP_HOME does not exist or is not a directory: $APP_HOME" >&2
  exit 1
fi
app_marker="$APP_HOME/.mini-deploy-install"
if [[ -e "$app_marker" || -L "$app_marker" ]]; then
  if ! has_valid_app_marker; then
    echo "APP_HOME has an invalid or unsafe installation marker: $app_marker" >&2
    exit 1
  fi
elif legacy_backup_enabled; then
  LEGACY_INSTALLATION="yes"
  for required in agent.py install.sh env.example ui/index.html scripts/deploy.sample.sh; do
    if [[ ! -f "$APP_HOME/$required" || -L "$APP_HOME/$required" ]]; then
      echo "APP_HOME is not a recognized mini_deploy installation: $APP_HOME" >&2
      exit 1
    fi
  done
else
  echo "APP_HOME is unmarked; set BACKUP_ALLOW_LEGACY_INSTALLATION=true only after verifying a legacy installation: $APP_HOME" >&2
  exit 1
fi
if [[ -e "$DATA_HOME" ]]; then
  if [[ ! -d "$DATA_HOME" ]]; then
    echo "DATA_HOME must be a directory: $DATA_HOME" >&2
    exit 1
  fi
  data_marker="$DATA_HOME/.mini-deploy-data"
  if [[ -e "$data_marker" || -L "$data_marker" ]]; then
    if ! has_valid_data_marker; then
      echo "DATA_HOME has an invalid or unsafe marker: $data_marker" >&2
      exit 1
    fi
  elif [[ ! -f "$DATA_HOME/state.json" \
    && ! -f "$DATA_HOME/audit.jsonl" \
    && ! -f "$DATA_HOME/projects.json" ]]; then
    echo "DATA_HOME is not recognized as mini_deploy data: $DATA_HOME" >&2
    exit 1
  fi
fi

for path in "$APP_HOME" "$DATA_HOME" "$LOG_HOME" "$BACKUP_DIR"; do
  if is_dangerous_managed_directory "$path"; then
    echo "Refusing dangerous backup path: $path" >&2
    exit 1
  fi
done

if paths_overlap "$APP_HOME" "$DATA_HOME" \
  || paths_overlap "$APP_HOME" "$LOG_HOME" \
  || paths_overlap "$APP_HOME" "$BACKUP_DIR" \
  || paths_overlap "$DATA_HOME" "$LOG_HOME" \
  || paths_overlap "$DATA_HOME" "$BACKUP_DIR" \
  || paths_overlap "$LOG_HOME" "$BACKUP_DIR"; then
  echo "APP_HOME, DATA_HOME, LOG_HOME and BACKUP_DIR must not overlap." >&2
  exit 1
fi

assert_trusted_directory_chain "$APP_HOME"
if [[ -e "$DATA_HOME" ]]; then
  assert_trusted_directory_chain "$DATA_HOME"
else
  assert_trusted_directory_chain "$(dirname -- "$DATA_HOME")"
fi
if [[ -e "$LOG_HOME" ]]; then
  if [[ ! -d "$LOG_HOME" ]]; then
    echo "LOG_HOME must be a directory: $LOG_HOME" >&2
    exit 1
  fi
  assert_trusted_directory_chain "$LOG_HOME"
else
  assert_trusted_directory_chain "$(dirname -- "$LOG_HOME")"
fi
assert_trusted_directory_chain "$(dirname -- "$BACKUP_DIR")"
for path in "$ENV_FILE" "$SERVICE_FILE" "$NGINX_CONF_FILE"; do
  assert_trusted_directory_chain "$(dirname -- "$path")"
done

for path in "$APP_HOME" "$ENV_FILE" "$DATA_HOME" "$LOG_HOME" "$SERVICE_FILE" "$NGINX_CONF_FILE" "$BACKUP_DIR"; do
  if [[ ! "$path" =~ ^/[A-Za-z0-9_./@+-]+$ ]]; then
    echo "Backup path contains unsupported characters: $path" >&2
    exit 1
  fi
done

case "$(basename -- "$ENV_FILE")" in
  *.env|*mini-deploy*|*mini_deploy*) ;;
  *)
    echo "ENV_FILE must be a dedicated .env or mini-deploy file: $ENV_FILE" >&2
    exit 1
    ;;
esac

if [[ "$(basename -- "$SERVICE_FILE")" != *.service ]]; then
  echo "SERVICE_FILE must end in .service: $SERVICE_FILE" >&2
  exit 1
fi

if [[ "$(basename -- "$NGINX_CONF_FILE")" != *.conf ]]; then
  echo "NGINX_CONF_FILE must end in .conf: $NGINX_CONF_FILE" >&2
  exit 1
fi

for path in "$ENV_FILE" "$SERVICE_FILE" "$NGINX_CONF_FILE"; do
  for managed_directory in "$APP_HOME" "$DATA_HOME" "$LOG_HOME" "$BACKUP_DIR"; do
    if [[ "$path" == "$managed_directory" || "$path" == "$managed_directory/"* ]]; then
      echo "Backup control files must be outside managed directories: $path" >&2
      exit 1
    fi
  done
  if [[ -e "$path" ]]; then
    if [[ ! -f "$path" || -L "$path" ]]; then
      echo "Backup control path must be a regular non-symlink file: $path" >&2
      exit 1
    fi
    owner="$(stat -c '%u' -- "$path")"
    mode="$(stat -c '%a' -- "$path")"
    if [[ "$(stat -c '%h' -- "$path")" != "1" ]]; then
      echo "Backup control file must have exactly one hard link: $path" >&2
      exit 1
    fi
    if [[ "$owner" != "0" ]] || (( (8#$mode & 022) != 0 )); then
      echo "Backup control file must be root-owned and not group/other writable: $path" >&2
      exit 1
    fi
  fi
done

validate_private_projects_file() {
  local projects_file="$1"
  local owner=""
  local mode=""

  [[ -e "$projects_file" || -L "$projects_file" ]] || return 0
  if [[ -L "$projects_file" || ! -f "$projects_file" ]]; then
    echo "projects.json must be a regular non-symlink file: $projects_file" >&2
    return 1
  fi
  owner="$(stat -c '%u' -- "$projects_file")"
  mode="$(stat -c '%a' -- "$projects_file")"
  if [[ "$(stat -c '%h' -- "$projects_file")" != "1" ]]; then
    echo "projects.json must have exactly one hard link: $projects_file" >&2
    return 1
  fi
  if [[ "$owner" != "0" ]] || (( (8#$mode & 077) != 0 )); then
    echo "projects.json must be root-owned and private: $projects_file" >&2
    return 1
  fi
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

if [[ -f "$ENV_FILE" ]]; then
  configured_projects_file="$(env_file_value "DEPLOY_PROJECTS_FILE")"
  if [[ -n "$configured_projects_file" ]]; then
    if [[ "$configured_projects_file" != /* ]]; then
      echo "DEPLOY_PROJECTS_FILE must be an absolute managed path: $configured_projects_file" >&2
      exit 1
    fi
    configured_projects_file="$(readlink -m -- "$configured_projects_file")"
    if [[ "$configured_projects_file" != "$APP_HOME/projects.json" \
      && "$configured_projects_file" != "$DATA_HOME/projects.json" ]]; then
      echo "DEPLOY_PROJECTS_FILE uses an unmanaged custom path that this backup would omit: $configured_projects_file" >&2
      exit 1
    fi
  fi
fi

# A first layout migration can legitimately have both files. Archive and
# validate both verbatim so no project Token is lost; install.sh decides which
# file is authoritative from the managed DEPLOY_PROJECTS_FILE value.
validate_private_projects_file "$APP_HOME/projects.json"
validate_private_projects_file "$DATA_HOME/projects.json"

declare -a archive_paths=()
for path in "$APP_HOME" "$ENV_FILE" "$DATA_HOME" "$SERVICE_FILE" "$NGINX_CONF_FILE"; do
  if [[ -e "$path" || -L "$path" ]]; then
    archive_paths+=("${path#/}")
  fi
done

if (( ${#archive_paths[@]} == 0 )); then
  echo "No mini_deploy installation data was found to back up." >&2
  exit 1
fi

umask 077
if [[ -e "$BACKUP_DIR" ]]; then
  if [[ ! -d "$BACKUP_DIR" || -L "$BACKUP_DIR" ]]; then
    echo "BACKUP_DIR must be a regular non-symlink directory: $BACKUP_DIR" >&2
    exit 1
  fi
  backup_owner="$(stat -c '%u' -- "$BACKUP_DIR")"
  backup_mode="$(stat -c '%a' -- "$BACKUP_DIR")"
  if [[ "$backup_owner" != "0" ]] || (( (8#$backup_mode & 077) != 0 )); then
    echo "BACKUP_DIR must be root-owned with mode 0700 or stricter: $BACKUP_DIR" >&2
    exit 1
  fi
else
  install -d -m 700 "$BACKUP_DIR"
fi
chmod 700 "$BACKUP_DIR"

timestamp="$(date -u '+%Y%m%dT%H%M%SZ')"
staging="$(mktemp -d "$BACKUP_DIR/.mini-deploy-bundle.XXXXXX")"
bundle_suffix="${staging##*.}"
bundle="$BACKUP_DIR/mini-deploy-$timestamp-$bundle_suffix"
archive="$staging/installation.tar.gz"
manifest="$staging/manifest"
complete_marker="$staging/complete"
published_archive="$bundle/installation.tar.gz"

cleanup() {
  if [[ -n "${staging:-}" ]]; then
    assert_no_nested_mounts "$staging" "backup staging directory" || return 1
    rm -rf -- "$staging"
  fi
}
trap cleanup EXIT

assert_no_nested_mounts "$APP_HOME" "APP_HOME"
if [[ -d "$DATA_HOME" ]]; then
  assert_no_nested_mounts "$DATA_HOME" "DATA_HOME"
fi
tar -C / --one-file-system -czf "$archive" -- "${archive_paths[@]}"
chmod 600 "$archive"
tar -tzf "$archive" >/dev/null

checksum="$(sha256sum "$archive" | awk '{print $1}')"
release_fingerprint="$(sha256sum "$APP_HOME/agent.py" | awk '{print $1}')"

{
  printf 'format=2\n'
  printf 'created_at=%s\n' "$timestamp"
  printf 'archive=installation.tar.gz\n'
  printf 'sha256=%s\n' "$checksum"
  printf 'release_fingerprint=sha256:%s\n' "$release_fingerprint"
  printf 'app_home=%s\n' "$APP_HOME"
  printf 'env_file=%s\n' "$ENV_FILE"
  printf 'data_home=%s\n' "$DATA_HOME"
  printf 'log_home=%s\n' "$LOG_HOME"
  printf 'service_name=%s\n' "$SERVICE_NAME"
  printf 'service_file=%s\n' "$SERVICE_FILE"
  printf 'nginx_conf_file=%s\n' "$NGINX_CONF_FILE"
  printf 'legacy_installation=%s\n' "$LEGACY_INSTALLATION"
  printf 'included=%s\n' "${archive_paths[*]}"
  printf 'managed_log_home=%s\n' "$LOG_HOME"
  printf 'managed_log_home_included=no\n'
} >"$manifest"
chmod 600 "$manifest"
printf 'complete\n' >"$complete_marker"
chmod 600 "$complete_marker"

# Publish archive, manifest and completion marker as one atomic directory move.
sync -f "$archive" "$manifest" "$complete_marker" "$staging"
if [[ -e "$bundle" || -L "$bundle" ]]; then
  echo "Refusing to replace an existing backup bundle: $bundle" >&2
  exit 1
fi
mv -T -- "$staging" "$bundle"
staging=""
sync -f "$BACKUP_DIR"

# Keep stdout machine-readable so install.sh can capture the archive path.
printf '%s\n' "$published_archive"
