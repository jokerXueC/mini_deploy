#!/usr/bin/env bash
set -Eeuo pipefail
set +x
PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH
# server.env may contain administrator credentials; never echo sourced values.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -e "$ROOT_DIR/server.env" || -L "$ROOT_DIR/server.env" ]]; then
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "Please run the server bootstrap as root or with sudo." >&2
    exit 1
  fi
  if [[ -L "$ROOT_DIR/server.env" || ! -f "$ROOT_DIR/server.env" ]]; then
    echo "server.env must be a regular non-symlink file." >&2
    exit 1
  fi
  exec {server_env_fd}<"$ROOT_DIR/server.env"
  if [[ ! -f "/proc/self/fd/$server_env_fd" ]]; then
    echo "server.env changed while it was being opened; refusing to source it." >&2
    exit 1
  fi
  server_env_path_identity="$(stat -c '%d:%i' -- "$ROOT_DIR/server.env")"
  server_env_fd_identity="$(stat -Lc '%d:%i' -- "/proc/self/fd/$server_env_fd")"
  server_env_link_count="$(stat -Lc '%h' -- "/proc/self/fd/$server_env_fd")"
  if [[ "$server_env_path_identity" != "$server_env_fd_identity" || "$server_env_link_count" != "1" ]]; then
    echo "server.env changed while it was being opened or has multiple hard links." >&2
    exit 1
  fi
  server_env_owner="$(stat -Lc '%u' -- "/proc/self/fd/$server_env_fd")"
  server_env_mode="$(stat -Lc '%a' -- "/proc/self/fd/$server_env_fd")"
  if [[ "$server_env_owner" != "0" ]] || (( (8#$server_env_mode & 077) != 0 )); then
    echo "server.env may contain credentials and must be root-owned with mode 0600 or stricter." >&2
    exit 1
  fi
  restore_allexport="false"
  case "$-" in
    *a*) restore_allexport="true" ;;
    *) set -a ;;
  esac
  # shellcheck disable=SC1090
  source "/proc/self/fd/$server_env_fd"
  exec {server_env_fd}<&-
  if [[ "$restore_allexport" != "true" ]]; then
    set +a
  fi
fi

# Keep the existing installer as the single implementation. This entry point
# mirrors the source project's one-command bootstrap while remaining generic.
PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH
exec /bin/bash "$ROOT_DIR/install.sh" "$@"
