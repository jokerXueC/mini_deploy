#!/usr/bin/env bash
# Select an existing runtime without changing the distribution's system Python.
select_python() {
  local candidate resolved
  local candidates=(python3 python3.13 python3.12 python3.11 python3.10)
  if [[ -n "${PYTHON_BIN:-}" ]]; then candidates=("$PYTHON_BIN"); fi
  for candidate in "${candidates[@]}"; do
    resolved="$(command -v "$candidate" 2>/dev/null)" || continue
    resolved="$(readlink -f -- "$resolved")" || continue
    if "$resolved" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1 \
      && "$resolved" -c 'import sqlite3, ssl, hashlib, http.server' >/dev/null 2>&1; then
      printf '%s\n' "$resolved"
      return 0
    fi
  done
  echo "需要含 SQLite/SSL 的 Python 3.10+ / Python 3.10 or newer is required, including SQLite and SSL." >&2
  echo "Use PYTHON_BIN=/usr/bin/python3.11 to select an existing runtime; system Python is never replaced." >&2
  echo "See ui/user-guide.md and docs/PLATFORM_SUPPORT.zh-CN.md for distribution-specific preparation." >&2
  return 1
}
