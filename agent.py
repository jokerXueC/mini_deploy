#!/usr/bin/env python3
"""Server monitoring, container management and website request observability."""

from __future__ import annotations

import base64
import errno
import getpass
import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import smtplib
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from email.message import EmailMessage
from functools import wraps
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote_plus, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

import certificates
import caddy_requests
import nginx_runtime
import nginx_install
import nginx_requests
import request_gateway
import gateway_connections
import monitoring
import docker_mirrors

try:
    import fcntl
except ImportError:  # pragma: no cover - Linux is the production target
    fcntl = None  # type: ignore[assignment]


def _path_from_env(name: str, default: str | Path) -> Path:
    value = os.getenv(name, "").strip()
    return Path(value or default)


def _projects_config_paths(
    explicit_value: str,
    state_file: Path,
    app_home: Path,
) -> tuple[Path, Path | None]:
    """Return the active and optional legacy projects configuration paths.

    An explicit DEPLOY_PROJECTS_FILE is authoritative and intentionally
    disables legacy-copy comparison.  Otherwise projects.json belongs beside
    the mutable agent state, while APP_HOME/projects.json is retained only as
    a read-only upgrade signal.
    """
    explicit_value = explicit_value.strip()
    if explicit_value:
        return Path(explicit_value), None

    config_file = state_file.parent / "projects.json"
    legacy_file = app_home / "projects.json"
    if legacy_file == config_file:
        legacy_file = None
    return config_file, legacy_file


APP_HOME = Path(os.getenv("MINI_DEPLOY_HOME", os.getenv("VIBEPILOT_HOME", "/opt/mini_deploy")))
# The dashboard has one fixed public entry point, including on upgrades.
HOST = "0.0.0.0"
PORT = 6868
TRUST_LOOPBACK_PROXY_HEADERS = os.getenv(
    "DEPLOY_TRUST_LOOPBACK_PROXY_HEADERS",
    "",
).strip().lower() in {"1", "true", "yes", "on", "enabled"}
STATE_FILE = _path_from_env("DEPLOY_AGENT_STATE_FILE", "/var/lib/mini-deploy-agent/state.json")
SITES_CONFIG_FILE = STATE_FILE.with_name("sites.json")
MONITORING_STATE_FILE = STATE_FILE.with_name("monitoring-state.json")
DEPLOY_PROJECTS_FILE_TEXT = os.getenv("DEPLOY_PROJECTS_FILE", "").strip()
DEPLOY_PROJECTS_FILE = Path(DEPLOY_PROJECTS_FILE_TEXT) if DEPLOY_PROJECTS_FILE_TEXT else None
PROJECTS_CONFIG_FILE, LEGACY_PROJECTS_CONFIG_FILE = _projects_config_paths(
    DEPLOY_PROJECTS_FILE_TEXT,
    STATE_FILE,
    APP_HOME,
)
AGENT_ENV_FILE = _path_from_env("DEPLOY_AGENT_ENV_FILE", "/etc/mini-deploy-agent.env")
AGENT_SERVICE_NAME = os.getenv("DEPLOY_AGENT_SERVICE_NAME", "").strip() or "mini-deploy-agent"
LOG_FILE = _path_from_env("DEPLOY_AGENT_LOG", "/var/log/mini_deploy/mini-deploy-agent.log")
AUDIT_LOG_FILE = _path_from_env("DEPLOY_AUDIT_LOG_FILE", STATE_FILE.with_name("audit.jsonl"))
PROJECT_CONFIG_BACKUP_DIR = _path_from_env(
    "DEPLOY_PROJECT_CONFIG_BACKUP_DIR",
    STATE_FILE.parent / "backups",
)
MAINTENANCE_LOCK_FILE = Path("/run/mini-deploy-agent/maintenance.lock")
MAINTENANCE_LOCK_OWNER_UID = 0
MAX_BODY_BYTES = int(os.getenv("DEPLOY_AGENT_MAX_BODY_BYTES", str(1024 * 1024)))
LOG_TAIL_LINES = int(os.getenv("DEPLOY_LOG_TAIL_LINES", "320"))
LOG_TAIL_MAX_LINES = int(os.getenv("DEPLOY_LOG_TAIL_MAX_LINES", "5000"))
LOG_DOWNLOAD_MAX_BYTES = int(os.getenv("DEPLOY_LOG_DOWNLOAD_MAX_BYTES", str(32 * 1024 * 1024)))
SYSTEM_STATUS_CACHE_SECONDS = int(os.getenv("DEPLOY_SYSTEM_STATUS_CACHE_SECONDS", "5"))
SYSTEM_METRIC_INTERVAL_SECONDS = int(os.getenv("DEPLOY_SYSTEM_METRIC_INTERVAL_SECONDS", str(30 * 60)))
DOCKER_LOG_METRIC_INTERVAL_SECONDS = max(15, min(int(os.getenv("DEPLOY_DOCKER_LOG_METRIC_INTERVAL_SECONDS", "30")), 300))
SYSTEM_METRIC_MAX_POINTS = int(os.getenv("DEPLOY_SYSTEM_METRIC_MAX_POINTS", "336"))
NETWORK_MAX_MBPS = float(os.getenv("DEPLOY_NETWORK_MAX_MBPS", "100"))
DOCKER_LOG_TAIL_MAX_LINES = int(os.getenv("DEPLOY_DOCKER_LOG_TAIL_MAX_LINES", "5000"))
HEALTH_CHECK_INTERVAL_SECONDS = max(5, min(int(os.getenv("DEPLOY_HEALTH_CHECK_INTERVAL_SECONDS", "15")), 300))
HEALTH_CHECK_TIMEOUT_SECONDS = max(1, min(float(os.getenv("DEPLOY_HEALTH_CHECK_TIMEOUT_SECONDS", "5")), 30))

UI_PASSWORD_HASH = os.getenv("DEPLOY_UI_PASSWORD_HASH", "")
UI_SESSION_SECRET = os.getenv("DEPLOY_UI_SESSION_SECRET", "").strip()
UI_SESSION_TTL_SECONDS = int(os.getenv("DEPLOY_UI_SESSION_TTL_SECONDS", str(8 * 60 * 60)))
COOKIE_SECURE_MODE = os.getenv("DEPLOY_COOKIE_SECURE", "auto").strip().lower()
COOKIE_NAME = "mini_deploy_session"
PASSWORD_HASH_ITERATIONS = 260_000
MIN_PASSWORD_HASH_ITERATIONS = 200_000
MAX_PASSWORD_HASH_ITERATIONS = 2_000_000
MIN_UI_SESSION_SECRET_LENGTH = 32
MIN_UI_SESSION_SECRET_UNIQUE_CHARS = 8
LOGIN_ATTEMPT_LIMIT = 5
LOGIN_ATTEMPT_WINDOW_SECONDS = 60.0
LOGIN_FAILURE_MAX_CLIENTS = 4096


@dataclass(frozen=True)
class Site:
    key: str
    name: str
    health_url: str = ""
    enabled: bool = True
    service_port: int = 8000
    app_domain: str = ""
    app_https: bool = False


def _safe_project_key(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "-" for ch in value.strip().lower())
    return cleaned.strip("-") or "default"


def _normalize_domain(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"^https?://", "", text)
    text = text.split("/", 1)[0].split(":", 1)[0].strip(".")
    return text


def _valid_domain(value: str) -> bool:
    if not value or len(value) > 253:
        return False
    return bool(re.fullmatch(r"(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+", value))


def _bool_config(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _int_config(value: Any, default: int) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _project_from_config(raw: dict[str, Any], fallback_key: str) -> Site:
    # Legacy deployment fields are deliberately ignored during site migration.
    key = _safe_project_key(str(raw.get("key") or fallback_key))
    return Site(
        key=key, name=str(raw.get("name") or key),
        health_url=str(raw.get("health_url") or ""),
        enabled=_bool_config(raw.get("enabled"), True),
        service_port=_int_config(raw.get("service_port") or raw.get("port"), 8000),
        app_domain=str(raw.get("app_domain") or raw.get("domain") or ""),
        app_https=_bool_config(raw.get("app_https") or raw.get("https"), False),
    )


def _read_regular_projects_config(path: Path, label: str) -> bytes | None:
    """Read one configuration candidate without following a final symlink."""
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError(f"cannot inspect {label} projects config: {path}: {exc}") from exc

    if stat.S_ISLNK(path_stat.st_mode):
        raise RuntimeError(f"{label} projects config must not be a symbolic link: {path}")
    if not stat.S_ISREG(path_stat.st_mode):
        raise RuntimeError(f"{label} projects config must be a regular file: {path}")
    if path_stat.st_nlink != 1:
        raise RuntimeError(f"{label} projects config must have exactly one hard link: {path}")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise RuntimeError(f"{label} projects config must be a regular file: {path}")
        if opened_stat.st_nlink != 1:
            raise RuntimeError(f"{label} projects config must have exactly one hard link: {path}")
        if (path_stat.st_dev, path_stat.st_ino) != (opened_stat.st_dev, opened_stat.st_ino):
            raise RuntimeError(f"{label} projects config changed while it was being opened: {path}")
        with os.fdopen(descriptor, "rb") as file_handle:
            descriptor = -1
            payload = file_handle.read()
            final_stat = os.fstat(file_handle.fileno())
        if (
            opened_stat.st_size != final_stat.st_size
            or opened_stat.st_mtime_ns != final_stat.st_mtime_ns
        ):
            raise RuntimeError(f"{label} projects config changed while it was being read: {path}")
        try:
            final_path_stat = path.lstat()
        except OSError as exc:
            raise RuntimeError(
                f"{label} projects config path changed while it was being read: {path}: {exc}"
            ) from exc
        if (
            not stat.S_ISREG(final_path_stat.st_mode)
            or final_path_stat.st_nlink != 1
            or (final_path_stat.st_dev, final_path_stat.st_ino)
            != (opened_stat.st_dev, opened_stat.st_ino)
        ):
            raise RuntimeError(f"{label} projects config path changed while it was being read: {path}")
        return payload
    except RuntimeError:
        raise
    except OSError as exc:
        raise RuntimeError(f"cannot read {label} projects config: {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_selected_projects_config() -> bytes | None:
    """Read the authoritative projects config and detect stale legacy layouts."""
    config_payload = _read_regular_projects_config(PROJECTS_CONFIG_FILE, "canonical")
    if LEGACY_PROJECTS_CONFIG_FILE is None:
        return config_payload

    legacy_payload = _read_regular_projects_config(LEGACY_PROJECTS_CONFIG_FILE, "legacy")
    if legacy_payload is None:
        return config_payload
    if config_payload is None:
        raise RuntimeError(
            "legacy projects config requires migration: "
            f"found {LEGACY_PROJECTS_CONFIG_FILE}, but the canonical data file "
            f"{PROJECTS_CONFIG_FILE} is missing; move it during installation or set "
            "DEPLOY_PROJECTS_FILE explicitly"
        )
    if config_payload != legacy_payload:
        raise RuntimeError(
            "projects config conflict: canonical file "
            f"{PROJECTS_CONFIG_FILE} and legacy file {LEGACY_PROJECTS_CONFIG_FILE} "
            "both exist with different content; reconcile them and remove the legacy copy"
        )
    return config_payload


def _read_site_config() -> bytes | None:
    payload = _read_regular_projects_config(SITES_CONFIG_FILE, "sites")
    return payload if payload is not None else _read_selected_projects_config()


def _load_projects() -> dict[str, Site]:
    config_payload = _read_site_config()
    if config_payload is None:
        return {}
    try:
        raw = json.loads(config_payload.decode("utf-8"))
        items = raw.get("sites", raw.get("projects")) if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            raise ValueError("sites must be a list")
        sites = {}
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"site at index {index} must be an object")
            site = _project_from_config(item, f"site-{index + 1}")
            if site.key in sites:
                raise ValueError(f"duplicate site key: {site.key}")
            sites[site.key] = site
        return sites
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"sites config is invalid: {exc}") from exc


_RUNTIME_CONFIG_ERROR: RuntimeError | None = None
try:
    PROJECTS = _load_projects()
except RuntimeError as exc:
    # Administrative CLI commands are break-glass operations. Keep the module
    # importable when projects.json is damaged, then fail closed only when the
    # HTTP service itself is started.
    PROJECTS = {}
    _RUNTIME_CONFIG_ERROR = exc

_projects_lock = threading.Lock()
_config_transaction_lock = threading.RLock()
_state_lock = threading.Lock()
_state_write_lock = threading.Lock()
_audit_lock = threading.Lock()
_state: dict[str, Any] = {"system_metrics": []}
_login_failures: dict[str, list[float]] = {}
_login_failures_lock = threading.Lock()
_system_status_cache: dict[str, Any] = {"at": 0.0, "payload": {}}
_realtime_metrics_lock = threading.Lock()
_realtime_metrics: deque[dict[str, Any]] = deque(maxlen=60)
_realtime_server: dict[str, Any] = {}
_system_status_lock = threading.Lock()
_notifications_lock = threading.Lock()
_last_cpu_sample: tuple[int, int] | None = None
_last_network_sample: tuple[float, int, int] | None = None
_health_status_lock = threading.Lock()
_health_status: dict[str, dict[str, Any]] = {}
_docker_log_counts_lock = threading.Lock()
_docker_log_counts: dict[str, dict[str, int]] = {}


class _MaintenanceLockError(RuntimeError):
    pass


class _MaintenanceActiveError(_MaintenanceLockError):
    pass


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _append_private_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        else:  # pragma: no cover - Windows-only development fallback
            os.chmod(path, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8", newline="\n") as file_handle:
            descriptor = -1
            file_handle.write(text)
            file_handle.flush()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _log(message: str) -> None:
    line = f"{_now_text()} {message}\n"
    try:
        print(line, end="", flush=True)
    except (OSError, UnicodeError, ValueError):
        # Logging is diagnostic and must never change operation results
        # behavior when stdout is closed or cannot encode a message.
        pass
    try:
        _append_private_text(LOG_FILE, line)
    except (OSError, UnicodeError, ValueError):
        pass


def _log_without_raising(message: str) -> None:
    try:
        _log(message)
    except Exception:  # noqa: BLE001 - lifecycle safety must not depend on diagnostics
        pass


def _validate_maintenance_lock_parent(path: Path) -> None:
    if not path.is_absolute():
        raise _MaintenanceLockError("maintenance lock path must be absolute")
    try:
        parent_status = os.lstat(path.parent)
    except OSError as exc:
        raise _MaintenanceLockError(f"maintenance lock directory is unavailable: {path.parent}") from exc
    if not stat.S_ISDIR(parent_status.st_mode):
        raise _MaintenanceLockError(f"maintenance lock directory is unsafe: {path.parent}")
    if parent_status.st_uid != MAINTENANCE_LOCK_OWNER_UID or stat.S_IMODE(parent_status.st_mode) & 0o077:
        raise _MaintenanceLockError(f"maintenance lock directory is unsafe: {path.parent}")


def _acquire_maintenance_shared_lock(*, blocking: bool) -> int | None:
    if fcntl is None:  # pragma: no cover - Windows-only development fallback
        return None

    path = MAINTENANCE_LOCK_FILE
    _validate_maintenance_lock_parent(path)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags, 0o600)
        file_status = os.fstat(descriptor)
        path_status = os.lstat(path)
        if not stat.S_ISREG(file_status.st_mode) or file_status.st_nlink != 1:
            raise _MaintenanceLockError(f"maintenance lock file is unsafe: {path}")
        if (path_status.st_dev, path_status.st_ino) != (file_status.st_dev, file_status.st_ino):
            raise _MaintenanceLockError(f"maintenance lock file changed while opening: {path}")
        if file_status.st_uid != MAINTENANCE_LOCK_OWNER_UID or stat.S_IMODE(file_status.st_mode) & 0o077:
            raise _MaintenanceLockError(f"maintenance lock file is unsafe: {path}")
        lock_flags = fcntl.LOCK_SH | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(descriptor, lock_flags)
        except OSError as exc:
            if not blocking and exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise _MaintenanceActiveError("maintenance operation is active") from exc
            raise
        acquired_descriptor = descriptor
        descriptor = -1
        return acquired_descriptor
    except _MaintenanceLockError:
        raise
    except OSError as exc:
        raise _MaintenanceLockError(f"maintenance lock file is unavailable: {path}") from exc
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _release_maintenance_lock(descriptor: int | None) -> None:
    if descriptor is None or fcntl is None:  # pragma: no cover - Windows-only fallback has no descriptor
        return
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(descriptor)
    except OSError:
        pass


@contextmanager
def _maintenance_shared_lock():
    descriptor = _acquire_maintenance_shared_lock(blocking=True)
    try:
        yield
    finally:
        _release_maintenance_lock(descriptor)


def _maintenance_shared_operation(function: Any) -> Any:
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with _maintenance_shared_lock():
            return function(*args, **kwargs)

    return wrapped


def _probe_maintenance_lock_for_startup() -> None:
    descriptor: int | None = None
    try:
        descriptor = _acquire_maintenance_shared_lock(blocking=False)
    except _MaintenanceActiveError:
        # install.sh intentionally restarts the Agent while it still owns the
        # exclusive lock. Read-only HTTP/health endpoints may start; every
        # mutation remains blocked or returns maintenance_in_progress.
        return
    finally:
        _release_maintenance_lock(descriptor)


_SENSITIVE_QUERY_NAMES = frozenset({
    "access_token",
    "api_key",
    "apikey",
    "key",
    "password",
    "secret",
    "session",
    "session_id",
    "signature",
    "token",
})
_QUERY_VALUE_RE = re.compile(r"([?&;])([^=&;\s\"]+)=([^&;\s\"]*)")


def _is_sensitive_query_name(value: str) -> bool:
    normalized = unquote_plus(str(value or "")).strip().lower().replace("-", "_")
    return normalized in _SENSITIVE_QUERY_NAMES or normalized.endswith((
        "_password",
        "_secret",
        "_signature",
        "_token",
    ))


def _redact_http_log_message(message: str) -> str:
    def replace(match: re.Match[str]) -> str:
        if not _is_sensitive_query_name(match.group(2)):
            return match.group(0)
        return f"{match.group(1)}{match.group(2)}=[redacted]"

    return _QUERY_VALUE_RE.sub(replace, str(message or ""))


def _redact_audit_value(value: Any, *, field_name: str = "") -> Any:
    if field_name and _is_sensitive_query_name(field_name):
        return "[redacted]"
    if isinstance(value, dict):
        return {
            str(key): _redact_audit_value(item, field_name=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_audit_value(item) for item in value]
    if isinstance(value, str):
        return _redact_http_log_message(value)
    return value


@_maintenance_shared_operation
def _audit_event(
    action: str,
    *,
    actor: str = "",
    target: str = "",
    success: bool = True,
    detail: dict[str, Any] | None = None,
) -> None:
    event = {
        "at": _now_text(),
        "action": action,
        "actor": _redact_audit_value(actor),
        "target": _redact_audit_value(target),
        "success": bool(success),
        "detail": _redact_audit_value(detail or {}),
    }
    try:
        with _audit_lock:
            _append_private_text(
                AUDIT_LOG_FILE,
                json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n",
            )
    except OSError as exc:
        _log(f"audit write failed: {exc}")


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _clean_config_text(value: Any, default: str = "", max_length: int = 1024) -> str:
    text = str(value if value is not None else default).strip()
    return text[:max_length]


def _load_config_file() -> dict[str, Any]:
    config_payload = _read_site_config()
    if config_payload is None:
        return {}
    try:
        raw = json.loads(config_payload.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"projects config is invalid: {PROJECTS_CONFIG_FILE}: {exc}") from exc
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return {"projects": raw}
    raise RuntimeError(f"projects config must contain an object or list: {PROJECTS_CONFIG_FILE}")


def _default_notification_config() -> dict[str, Any]:
    return {
        "wecom": {"enabled": False, "webhook_url": ""},
        "dingtalk": {"enabled": False, "webhook_url": "", "secret": ""},
        "email": {
            "enabled": False,
            "smtp_host": "",
            "smtp_port": 465,
            "username": "",
            "password": "",
            "from_addr": "",
            "to_addrs": "",
            "use_ssl": True,
            "use_starttls": False,
        },
    }


def _notification_config_from_raw(raw: Any, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    base = _default_notification_config()
    if isinstance(existing, dict):
        for channel, values in existing.items():
            if channel in base and isinstance(values, dict):
                base[channel].update(values)
    src = raw if isinstance(raw, dict) else {}

    wecom = src.get("wecom") if isinstance(src.get("wecom"), dict) else {}
    base["wecom"].update({
        "enabled": _bool_config(wecom.get("enabled"), bool(base["wecom"].get("enabled"))),
        "webhook_url": _clean_config_text(wecom.get("webhook_url"), str(base["wecom"].get("webhook_url") or ""), max_length=2048),
    })

    dingtalk = src.get("dingtalk") if isinstance(src.get("dingtalk"), dict) else {}
    base["dingtalk"].update({
        "enabled": _bool_config(dingtalk.get("enabled"), bool(base["dingtalk"].get("enabled"))),
        "webhook_url": _clean_config_text(dingtalk.get("webhook_url"), str(base["dingtalk"].get("webhook_url") or ""), max_length=2048),
        "secret": _clean_config_text(dingtalk.get("secret"), str(base["dingtalk"].get("secret") or ""), max_length=512),
    })

    email = src.get("email") if isinstance(src.get("email"), dict) else {}
    to_addrs_value = email.get("to_addrs", base["email"].get("to_addrs") or "")
    if isinstance(to_addrs_value, list):
        to_addrs_value = ", ".join(str(item) for item in to_addrs_value)
    password = _clean_config_text(email.get("password"), "", max_length=2048)
    if not password:
        password = str(base["email"].get("password") or "")
    base["email"].update({
        "enabled": _bool_config(email.get("enabled"), bool(base["email"].get("enabled"))),
        "smtp_host": _clean_config_text(email.get("smtp_host"), str(base["email"].get("smtp_host") or ""), max_length=512),
        "smtp_port": _int_config(email.get("smtp_port"), int(base["email"].get("smtp_port") or 465)),
        "username": _clean_config_text(email.get("username"), str(base["email"].get("username") or ""), max_length=512),
        "password": password,
        "from_addr": _clean_config_text(email.get("from_addr"), str(base["email"].get("from_addr") or ""), max_length=512),
        "to_addrs": _clean_config_text(to_addrs_value, "", max_length=2048),
        "use_ssl": _bool_config(email.get("use_ssl"), bool(base["email"].get("use_ssl", True))),
        "use_starttls": _bool_config(email.get("use_starttls"), bool(base["email"].get("use_starttls"))),
    })
    if base["email"]["use_ssl"]:
        base["email"]["use_starttls"] = False
    return base


def _load_notifications() -> dict[str, Any]:
    raw = _load_config_file()
    return _notification_config_from_raw(raw.get("notifications") if isinstance(raw, dict) else {})


if _RUNTIME_CONFIG_ERROR is None:
    try:
        NOTIFICATIONS = _load_notifications()
    except RuntimeError as exc:
        NOTIFICATIONS = _default_notification_config()
        _RUNTIME_CONFIG_ERROR = exc
else:
    NOTIFICATIONS = _default_notification_config()


def _project_to_config(project: Site, include_secret: bool = False) -> dict[str, Any]:
    return {"key": project.key, "name": project.name, "health_url": project.health_url,
            "enabled": project.enabled, "service_port": project.service_port,
            "app_domain": project.app_domain, "app_https": project.app_https}


def _projects_config_items(include_secret: bool = True) -> list[dict[str, Any]]:
    with _projects_lock:
        projects = list(PROJECTS.values())
    return [_project_to_config(project, include_secret=include_secret) for project in projects]


def _notification_config_payload(include_secret: bool = True) -> dict[str, Any]:
    with _notifications_lock:
        payload = json.loads(json.dumps(NOTIFICATIONS, ensure_ascii=False))
    if not include_secret:
        if isinstance(payload.get("dingtalk"), dict):
            payload["dingtalk"]["secret"] = ""
        if isinstance(payload.get("email"), dict):
            payload["email"]["password"] = ""
    return payload


def _sites_config_payload() -> dict[str, Any]:
    with _config_transaction_lock:
        return {"sites": _projects_config_items(),
                "notifications": _notification_config_payload(include_secret=True)}


def _backup_projects_config() -> None:
    payload = _read_regular_projects_config(SITES_CONFIG_FILE, "sites")
    if payload is None:
        return
    PROJECT_CONFIG_BACKUP_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(PROJECT_CONFIG_BACKUP_DIR, 0o700)
    descriptor, backup = tempfile.mkstemp(prefix="sites.json.", suffix=".bak", dir=PROJECT_CONFIG_BACKUP_DIR)
    with os.fdopen(descriptor, "wb") as handle:
        os.chmod(backup, 0o600)
        handle.write(payload)


def _atomic_write_private_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = -1
    temporary_path: Path | None = None
    try:
        descriptor, raw_temporary_path = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(raw_temporary_path)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file_handle:
            descriptor = -1
            file_handle.write(text)
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@_maintenance_shared_operation
def _write_projects_config(projects: dict[str, Site], notifications: dict[str, Any] | None = None) -> None:
    # Keep the original deployment configuration untouched, including its secrets.
    _backup_projects_config()
    payload = {
        "sites": [_project_to_config(project) for project in projects.values()],
        "notifications": _notification_config_from_raw(
            notifications if notifications is not None else _notification_config_payload(include_secret=True)),
    }
    _atomic_write_private_text(SITES_CONFIG_FILE, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _replace_projects(projects: dict[str, Site]) -> None:
    global PROJECTS
    with _projects_lock:
        PROJECTS = dict(projects)


def _save_and_reload_projects(projects: dict[str, Site]) -> None:
    with _config_transaction_lock:
        _write_projects_config(projects)
        _replace_projects(projects)


@_maintenance_shared_operation
def _save_site(data: dict[str, Any]) -> None:
    with _config_transaction_lock, _nginx_lock:
        raw = data.get("site")
        if not isinstance(raw, dict):
            raise ValueError("请填写站点信息")
        original_key = str(data.get("original_key") or "")
        key = str(raw.get("key") or original_key or f"site-{secrets.token_hex(6)}")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", key):
            raise ValueError("站点标识只能使用小写字母、数字、短横线和下划线，最多 80 位")
        if original_key and (original_key != key or key not in PROJECTS):
            raise ValueError("站点不存在或标识已变化，请刷新后重试")
        existing = PROJECTS.get(key)
        if existing and not original_key:
            raise ValueError("站点标识已存在")
        name = _clean_config_text(raw.get("name"), max_length=120)
        domain = str(raw.get("app_domain") or "").strip().lower()
        port = raw.get("service_port", 8000)
        health_url = str(raw.get("health_url") or "").strip()
        if not name:
            raise ValueError("请填写站点名称")
        if domain and not _valid_domain(domain):
            raise ValueError("请填写纯域名，例如 api.example.com")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError("后端端口必须在 1-65535 之间")
        if domain and any(site.key != key and site.app_domain == domain for site in PROJECTS.values()):
            raise ValueError("域名已用于其他站点")
        if health_url:
            parsed = urlparse(health_url)
            if (len(health_url) > 2048 or parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.fragment
                    or any(ord(ch) < 32 for ch in health_url)):
                raise ValueError("健康检查地址需为不含账号密码的 HTTP 或 HTTPS 地址")
        candidate = Site(key=key, name=name, health_url=health_url,
                         enabled=_bool_config(raw.get("enabled"), True),
                         service_port=port, app_domain=domain,
                         app_https=existing.app_https if existing else False)
        protected = _nginx_project_has_site(candidate) or (STATE_FILE.parent / "certificates" / key).exists()
        if protected and (existing is None or (domain, port) != (existing.app_domain, existing.service_port)):
            raise ValueError("此标识已有站点配置或证书；请先处理原入口，不能覆盖其域名和后端端口")
        sites = {**PROJECTS, key: candidate}
        _save_and_reload_projects(sites)


@_maintenance_shared_operation
def _delete_site(data: dict[str, Any]) -> None:
    with _config_transaction_lock, _nginx_lock:
        key = str(data.get("key") or "")
        existing = PROJECTS.get(key)
        if existing is None:
            raise ValueError("站点不存在，请刷新后重试")
        if _nginx_project_has_site(existing) or (STATE_FILE.parent / "certificates" / key).exists():
            raise ValueError("此站点仍有关联的 Nginx 配置或证书，请先处理关联；删除登记不会删除服务器文件")
        _save_and_reload_projects({name: site for name, site in PROJECTS.items() if name != key})


def _replace_notifications(notifications: dict[str, Any]) -> None:
    global NOTIFICATIONS
    with _notifications_lock:
        NOTIFICATIONS = _notification_config_from_raw(notifications)


def _save_and_reload_notifications(notifications: dict[str, Any]) -> None:
    with _config_transaction_lock:
        parsed = _notification_config_from_raw(
            notifications,
            existing=_notification_config_payload(include_secret=True),
        )
        _write_projects_config(PROJECTS, notifications=parsed)
        _replace_notifications(parsed)


def _notification_result(channel: str, enabled: bool, ok: bool, detail: str) -> dict[str, Any]:
    return {"channel": channel, "enabled": enabled, "ok": ok, "detail": detail}


def _http_post_json(url: str, payload: dict[str, Any], timeout: float = 8.0) -> str:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "mini_deploy-agent"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - user-configured webhook target
        text = response.read(1024).decode("utf-8", errors="replace")
        if response.status >= 400:
            raise RuntimeError(f"http {response.status}: {text}")
        try:
            result = json.loads(text)
        except ValueError as exc:
            raise RuntimeError("通知平台未返回有效 JSON，无法确认送达") from exc
        if not isinstance(result, dict) or str(result.get("errcode")) != "0":
            raise RuntimeError("通知平台拒绝了请求，请检查 Webhook、加签和机器人设置")
        return text


def _send_wecom_notification(config: dict[str, Any], title: str, body: str) -> str:
    webhook_url = str(config.get("webhook_url") or "").strip()
    if not webhook_url:
        raise ValueError("WeCom webhook is not configured")
    payload = {"msgtype": "markdown", "markdown": {"content": f"**{title}**\n\n{body}"}}
    return _http_post_json(webhook_url, payload)


def _dingtalk_signed_url(webhook_url: str, secret: str) -> str:
    if not secret:
        return webhook_url
    timestamp = str(int(time.time() * 1000))
    sign_text = f"{timestamp}\n{secret}"
    sign = quote_plus(base64.b64encode(hmac.new(secret.encode("utf-8"), sign_text.encode("utf-8"), hashlib.sha256).digest()).decode("utf-8"))
    separator = "&" if "?" in webhook_url else "?"
    return f"{webhook_url}{separator}timestamp={timestamp}&sign={sign}"


def _send_dingtalk_notification(config: dict[str, Any], title: str, body: str) -> str:
    webhook_url = str(config.get("webhook_url") or "").strip()
    if not webhook_url:
        raise ValueError("DingTalk webhook is not configured")
    url = _dingtalk_signed_url(webhook_url, str(config.get("secret") or "").strip())
    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": f"### {title}\n\n{body}"}}
    return _http_post_json(url, payload)


def _split_email_recipients(value: Any) -> list[str]:
    text = str(value or "")
    return [item.strip() for item in re.split(r"[,;\n\r]+", text) if item.strip()][:50]


def _send_email_notification(config: dict[str, Any], title: str, body: str) -> str:
    host = str(config.get("smtp_host") or "").strip()
    port = _int_config(config.get("smtp_port"), 465)
    username = str(config.get("username") or "").strip()
    password = str(config.get("password") or "")
    from_addr = str(config.get("from_addr") or username).strip()
    recipients = _split_email_recipients(config.get("to_addrs"))
    if not host:
        raise ValueError("SMTP host is not configured")
    if not from_addr:
        raise ValueError("email sender is not configured")
    if not recipients:
        raise ValueError("email recipients are not configured")

    message = EmailMessage()
    message["Subject"] = title
    message["From"] = from_addr
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    context = ssl.create_default_context()
    if _bool_config(config.get("use_ssl"), True):
        with smtplib.SMTP_SSL(host, port, timeout=8, context=context) as smtp:
            if username:
                smtp.login(username, password)
            if smtp.send_message(message):
                raise RuntimeError("部分收件人被 SMTP 服务器拒绝")
    else:
        with smtplib.SMTP(host, port, timeout=8) as smtp:
            if _bool_config(config.get("use_starttls"), False):
                smtp.starttls(context=context)
            if username:
                smtp.login(username, password)
            if smtp.send_message(message):
                raise RuntimeError("部分收件人被 SMTP 服务器拒绝")
    return f"sent to {len(recipients)} recipient(s)"


def _send_notifications(config: dict[str, Any], title: str, body: str) -> list[dict[str, Any]]:
    parsed = _notification_config_from_raw(config)
    channels = [
        ("wecom", "WeCom", _send_wecom_notification),
        ("dingtalk", "DingTalk", _send_dingtalk_notification),
        ("email", "Email", _send_email_notification),
    ]
    results: list[dict[str, Any]] = []
    for key, label, sender in channels:
        channel_config = parsed.get(key) if isinstance(parsed.get(key), dict) else {}
        enabled = _bool_config(channel_config.get("enabled"), False)
        if not enabled:
            results.append(_notification_result(key, False, True, "disabled"))
            continue
        try:
            detail = sender(channel_config, title, body)
            results.append(_notification_result(key, True, True, detail or "ok"))
        except Exception as exc:  # noqa: BLE001 - report channel-level failure
            results.append(_notification_result(key, True, False, f"{label} send failed: {exc}"))
    return results


def _any_notification_enabled(config: dict[str, Any]) -> bool:
    return any(_bool_config((config.get(key) if isinstance(config.get(key), dict) else {}).get("enabled"), False) for key in ("wecom", "dingtalk", "email"))




def _read_json_body(handler: BaseHTTPRequestHandler, max_bytes: int = 64 * 1024) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0 or length > max_bytes:
        raise ValueError("invalid_body_size")
    body = handler.rfile.read(length)
    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("invalid_json_object")
    return data


def _read_state() -> None:
    path = MONITORING_STATE_FILE if MONITORING_STATE_FILE.exists() else STATE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(data, dict) and isinstance(data.get("system_metrics"), list):
        with _state_lock:
            _state.clear()
            _state["system_metrics"] = data["system_metrics"][-SYSTEM_METRIC_MAX_POINTS:]


@_maintenance_shared_operation
def _write_state() -> None:
    try:
        with _state_write_lock:
            with _state_lock:
                data = {"system_metrics": list(_state.get("system_metrics", []))}
            _atomic_write_private_text(
                MONITORING_STATE_FILE, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    except OSError as exc:
        _log(f"monitoring state write failed: {exc}")


def _update_state(**changes: Any) -> None:
    with _state_lock:
        _state.update(changes)
    _write_state()


def _system_metric_history() -> list[dict[str, Any]]:
    with _state_lock:
        history = _state.get("system_metrics") if isinstance(_state.get("system_metrics"), list) else []
        return json.loads(json.dumps(history, ensure_ascii=False))


def _record_system_metric(server: dict[str, Any], now: float) -> list[dict[str, Any]]:
    memory = server.get("memory") if isinstance(server.get("memory"), dict) else {}
    network = server.get("network") if isinstance(server.get("network"), dict) else {}
    cpu_percent = server.get("cpu_percent")
    if cpu_percent is None:
        return _system_metric_history()

    with _state_lock:
        history = list(_state.get("system_metrics") or [])
        last_ts = 0.0
        if history and isinstance(history[0], dict):
            try:
                last_ts = float(history[0].get("ts") or 0)
            except (TypeError, ValueError):
                last_ts = 0.0
        if last_ts and now - last_ts < SYSTEM_METRIC_INTERVAL_SECONDS:
            return json.loads(json.dumps(history, ensure_ascii=False))

        entry = {
            "ts": int(now),
            "at": _now_text(),
            "cpu_percent": _percent(cpu_percent),
            "memory_percent": _percent(memory.get("percent")),
            "network_percent": _percent(network.get("percent")),
            "network_total_kbps": network.get("total_kbps"),
            "network_rx_kbps": network.get("rx_kbps"),
            "network_tx_kbps": network.get("tx_kbps"),
        }
        history.insert(0, entry)
        _state["system_metrics"] = history[:max(1, SYSTEM_METRIC_MAX_POINTS)]
    _write_state()
    return _system_metric_history()


def _log_line_limit(value: Any, default: int = LOG_TAIL_LINES) -> int:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        parsed = default
    return max(20, min(parsed, LOG_TAIL_MAX_LINES))


def _docker_log_line_limit(value: Any, default: int = 200) -> int:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        parsed = default
    return max(20, min(parsed, DOCKER_LOG_TAIL_MAX_LINES))


DOCKER_LOG_LEVEL_PATTERNS = {
    "error": (
        "traceback",
        "exception",
        " error",
        "[error]",
        "error:",
        "failed",
        "failure",
        "fatal",
        "panic",
        "timeout",
        "timed out",
        "refused",
        "unavailable",
        "429",
        "500",
        "502",
        "503",
        "504",
    ),
    "warn": (
        "warn",
        "warning",
        "retry",
        "retrying",
        "deprecated",
        "ignored",
        "slow",
        "rate limit",
    ),
}


def _docker_log_filter_options(query: dict[str, list[str]]) -> dict[str, Any]:
    level = str(query.get("level", ["all"])[0] or "all").strip().lower()
    if level not in {"all", "error", "warn", "keyword"}:
        level = "all"
    keyword = str(query.get("keyword", [""])[0] or "").strip()
    if len(keyword) > 200:
        keyword = keyword[:200]
    regex = str(query.get("regex", [""])[0] or "").strip().lower() in {"1", "true", "yes", "on"}
    try:
        context = int(str(query.get("context", ["0"])[0] or "0").strip())
    except (TypeError, ValueError):
        context = 0
    return {
        "level": level,
        "keyword": keyword,
        "regex": regex,
        "context": max(0, min(context, 20)),
    }


def _docker_log_matcher(options: dict[str, Any]) -> tuple[Any | None, str]:
    level = options.get("level") or "all"
    keyword = str(options.get("keyword") or "")
    if level == "all" and not keyword:
        return None, "全部"

    if keyword:
        if options.get("regex"):
            try:
                pattern = re.compile(keyword, re.IGNORECASE)
                return lambda line: bool(pattern.search(line)), f"正则 {keyword}"
            except re.error:
                pass
        lower_keyword = keyword.lower()
        return lambda line: lower_keyword in line.lower(), f"关键词 {keyword}"

    patterns = DOCKER_LOG_LEVEL_PATTERNS.get(level)
    if patterns:
        return lambda line: any(pattern in line.lower() for pattern in patterns), "异常" if level == "error" else "警告"
    return None, "全部"


def _filter_docker_log_lines(lines: list[str], options: dict[str, Any]) -> dict[str, Any]:
    matcher, label = _docker_log_matcher(options)
    if matcher is None:
        return {
            "lines": lines,
            "matched_count": len(lines),
            "filtered": False,
            "filter_label": label,
        }

    context = int(options.get("context") or 0)
    matched_indexes = [index for index, line in enumerate(lines) if matcher(line)]
    include: set[int] = set()
    for index in matched_indexes:
        start = max(0, index - context)
        end = min(len(lines), index + context + 1)
        include.update(range(start, end))
    filtered_lines = [lines[index] for index in sorted(include)]
    return {
        "lines": filtered_lines,
        "matched_count": len(matched_indexes),
        "filtered": True,
        "filter_label": label,
    }


def _tail(path: Path, lines: int = 160, max_bytes: int | None = None) -> list[str]:
    safe_lines = _log_line_limit(lines, default=LOG_TAIL_LINES)
    read_bytes = max_bytes if max_bytes is not None else max(128 * 1024, safe_lines * 2048)
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - read_bytes), os.SEEK_SET)
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return data.splitlines()[-safe_lines:]


def _read_log_text(path: Path, lines: int | None = None, all_lines: bool = False) -> tuple[str, bool]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            truncated = size > LOG_DOWNLOAD_MAX_BYTES
            if all_lines:
                fh.seek(max(0, size - LOG_DOWNLOAD_MAX_BYTES), os.SEEK_SET)
                data = fh.read()
            else:
                safe_lines = _log_line_limit(lines, default=LOG_TAIL_LINES)
                read_bytes = min(LOG_DOWNLOAD_MAX_BYTES, max(128 * 1024, safe_lines * 2048))
                fh.seek(max(0, size - read_bytes), os.SEEK_SET)
                data = fh.read()
    except OSError:
        return "", False
    text = data.decode("utf-8", errors="replace")
    if not all_lines:
        text = "\n".join(text.splitlines()[-_log_line_limit(lines, default=LOG_TAIL_LINES):])
    return text, truncated and all_lines


def _safe_download_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value.strip())
    return cleaned.strip("-") or "agent-log"


def _audit_tail(lines: int = 200) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in _tail(AUDIT_LOG_FILE, lines=lines):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def _nginx_step_result(step: str, ok: bool, detail: str, output: str = "") -> dict[str, Any]:
    return {"step": step, "ok": ok, "detail": detail, "output": output,
            "diagnosis": []}


def _run_nginx_command(command: list[str], *, step: str, timeout: float = 60.0) -> dict[str, Any]:
    code, output = _run_command(command, timeout=timeout)
    return _nginx_step_result(step, code == 0, "ok" if code == 0 else f"exit code {code}", output)


_nginx_lock = threading.RLock()


@_maintenance_shared_operation
def _request_gateway_operation(data: dict[str, Any]) -> dict[str, Any]:
    return request_gateway.Store(STATE_FILE.parent).operate(data)


@_maintenance_shared_operation
def _gateway_connection_operation(data: dict[str, Any]) -> dict[str, Any]:
    with _config_transaction_lock, _nginx_lock:
        return gateway_connections.Connections(STATE_FILE.parent).operate(data)


def _nginx_serialized(function: Any) -> Any:
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with _config_transaction_lock, _nginx_lock:
            return function(*args, **kwargs)
    return wrapped


def _certificate_store() -> certificates.CertificateStore:
    return certificates.CertificateStore(STATE_FILE.parent / "certificates", _nginx_settings().runtime(live=False))


def _nginx_settings() -> nginx_runtime.Settings:
    return nginx_runtime.Settings(STATE_FILE.parent)


def _nginx_project_has_site(project: Site) -> bool:
    settings = _nginx_settings()
    if settings.read()["profile"].get("mode") == "none":
        return False
    return (settings.runtime(live=False).conf_root / f"mini-deploy-{project.key}.conf").exists()


def _nginx_payload(*, discover: bool = False) -> dict[str, Any]:
    with _config_transaction_lock, _nginx_lock:
        settings = _nginx_settings()
        data = settings.read()
        runtime = settings.runtime(live=False) if data["profile"].get("mode") != "none" else None
        data["projects"] = [{"key": p.key, "name": p.name, "port": p.service_port, "domain": p.app_domain,
                             "site_configured": bool(runtime and (runtime.conf_root / f"mini-deploy-{p.key}.conf").exists())}
                            for p in PROJECTS.values()]
        if discover:
            data["detected"] = settings.discover()
        return data


def _nginx_requests_payload(limit: int = 100) -> dict[str, Any]:
    if not 1 <= limit <= nginx_requests.MAX_RECORDS:
        raise ValueError("请求数量必须在 1-300 之间")
    with _config_transaction_lock, _nginx_lock:
        settings = _nginx_settings()
        data = settings.read()
        if not data["configured"] or data["profile"]["mode"] == "none":
            return {"mode": "none", "enabled": False, "records": [], "limit": limit,
                    "notice": "请先在域名与证书页接入本机或 Docker Nginx。"}
        return nginx_requests.recent(settings.runtime(), limit)


@_nginx_serialized
def _enable_nginx_requests() -> dict[str, Any]:
    settings = _nginx_settings()
    data = settings.read()
    if not data["configured"] or data["profile"]["mode"] == "none":
        raise ValueError("请先接入本机或 Docker Nginx")
    runtime = settings.runtime()
    nginx_requests.enable(runtime)
    return nginx_requests.recent(runtime)


@_nginx_serialized
def _disable_nginx_requests() -> dict[str, Any]:
    settings = _nginx_settings()
    runtime = settings.runtime()
    nginx_requests.disable(runtime)
    return nginx_requests.recent(runtime)


def _nginx_http_url(domain: str, profile: dict[str, Any]) -> str:
    port = profile.get("http_port", 80)
    return f"http://{domain}" + (f":{port}" if port != 80 else "")


@_maintenance_shared_operation
@_nginx_serialized
def _nginx_install_operation(data: dict[str, Any]) -> dict[str, Any]:
    mode = data.get("mode", "local")
    port = data.get("port", 80)
    if mode == "docker":
        settings = _nginx_settings().read()
        docker = nginx_install.docker_plan(STATE_FILE.parent, data.get("container", "mini-deploy-nginx"),
                                           port, data.get("reserve_https", False))
        token = nginx_install.plan_token(docker)
        if data.get("action") == "plan-install":
            steps = [f"下载镜像 {docker['image']}", f"创建容器 {docker['container']}，HTTP {port} → 80",
                     "自动准备配置和独立证书目录挂载", "启动容器并检查 HTTP 测试页面"]
            if docker["reserve_https"]:
                steps.insert(2, "预留 HTTPS 443 端口，暂不启用 TLS")
            return {"plan": {"token": token, "mode": mode, "port": port, "installed": False,
                             "steps": steps, "notice": "无需站点、域名或证书。请在云安全组放行 HTTP 端口。"}}
        if not _constant_time_equal(str(data.get("token") or ""), token):
            raise certificates.CertificateError("安装参数或环境已变化，请重新检查后确认")
        result = nginx_install.install_docker(STATE_FILE.parent, docker)
        message = "Docker Nginx 已启动，HTTP 测试通过。"
        if not settings["configured"] or settings["profile"].get("mode") == "none":
            try:
                _save_nginx_settings({"mode": "docker", "container": result["container"]})
                message += "已接入面板，可随后配置业务入口。"
            except (ValueError, OSError) as exc:
                message += f"自动接入未完成：{exc}。可在高级接入中选择该容器。"
        else:
            message += "已保留原有接入实例，新容器可在高级接入中选择。"
        return {"ok": True, "settings": _nginx_payload(), "message": message,
                "access_port": port, "http_status": result["http_status"]}
    if mode != "local" or not isinstance(port, int) or isinstance(port, bool) or port != 80:
        raise certificates.CertificateError("本机安装使用默认 HTTP 80 端口；需要自选映射端口请使用 Docker 安装")
    local = nginx_runtime.local_setup_plan()
    steps = []
    if not local["installed"]:
        steps.append(f"使用 {local['package_manager']} 安装本机 Nginx")
    if not local["active"]:
        steps.append("启动 Nginx 并设置开机启动")
    steps.append("检查本机 Nginx 配置")
    token = hashlib.sha256(json.dumps(local, sort_keys=True).encode()).hexdigest()
    if data.get("action") == "plan-install":
        return {"plan": {"token": token, "steps": steps, "installed": local["installed"],
                         "notice": "独立准备本机 Nginx，站点与域名可稍后配置。"}}
    if not _constant_time_equal(str(data.get("token") or ""), token):
        raise certificates.CertificateError("安装环境已变化，请重新检查后确认")
    nginx_runtime.prepare_local(local)
    status = nginx_install.check_http(80)
    return {"ok": True, "settings": _nginx_payload(), "message": "本机 Nginx 已安装并运行，HTTP 检查通过，可随后添加站点和域名入口。",
            "access_port": 80, "http_status": status}


def _nginx_site_plan(data: dict[str, Any]) -> tuple[Site, dict[str, Any]]:
    existing = PROJECTS.get(str(data.get("project") or ""))
    if not existing:
        raise certificates.CertificateError("请先添加并保存站点")
    domain = str(data.get("domain") or "").strip().lower()
    port = data.get("port")
    if not _valid_domain(domain) or not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise certificates.CertificateError("请填写纯域名和 1-65535 之间的业务端口")
    if any(p.key != existing.key and p.app_domain == domain for p in PROJECTS.values()):
        raise certificates.CertificateError("此域名已分配给其他站点")
    settings = _nginx_settings()
    saved = settings.read()
    if (STATE_FILE.parent / "certificates" / existing.key).exists():
        raise certificates.CertificateError("此站点已有上传证书，请在证书页管理；修改入口前请先停用并删除证书")
    if _nginx_project_has_site(existing) and (domain != existing.app_domain or port != existing.service_port):
        raise certificates.CertificateError("该站点已有访问入口，请先移除旧入口，再修改域名或端口")
    project = replace(existing, app_domain=domain, service_port=port)
    configured = saved["configured"] and saved["profile"].get("mode") != "none"
    profile = saved["profile"] if configured else {"mode": "local"}
    local = nginx_runtime.local_setup_plan() if profile["mode"] == "local" else None
    if configured or (local and local["installed"]):
        candidate = settings.candidate(profile["mode"], profile.get("container", ""))
        runtime = nginx_runtime.Runtime(candidate, STATE_FILE.parent)
    else:
        runtime = nginx_runtime.Runtime(profile, STATE_FILE.parent, live=False)
    host = runtime.upstream(data.get("host") or saved["upstreams"].get(project.key))
    runtime.probe(host, port)
    path = runtime.conf_root / f"mini-deploy-{project.key}.conf"
    store = certificates.CertificateStore(STATE_FILE.parent / "certificates", runtime)
    previous = store.config(path)
    if previous and not previous.startswith("# mini-deploy-managed: project-http-v1\n"):
        raise certificates.CertificateError("此入口不是向导托管的 HTTP 配置，请使用原管理方式修改")
    if configured or (local and local["installed"]):
        nginx_runtime.check_domain_conflict(runtime, domain, path)
    steps = []
    if local and not local["installed"]:
        steps.append(f"使用 {local['package_manager']} 安装本机 Nginx")
    if local and not local["active"]:
        steps.append("启动 Nginx 并设置开机启动")
    steps.extend([f"保存 {domain} → {host}:{port}", "检查 Nginx 配置并重新加载"])
    plan = {"project": project.key, "domain": domain, "port": port, "host": host, "mode": profile["mode"],
            "local_setup": local, "steps": steps, "url": _nginx_http_url(domain, profile),
            "config": _nginx_http_config_text(domain, host, port),
            "notice": f"域名需解析到本服务器，并在云安全组放行 {profile.get('http_port', 80)} 端口。当前入口使用 HTTP，证书可稍后配置。"}
    fingerprint = [plan, saved, _project_to_config(existing), previous]
    plan["token"] = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return project, plan


@_maintenance_shared_operation
@_nginx_serialized
def _nginx_site_operation(data: dict[str, Any]) -> dict[str, Any]:
    project, plan = _nginx_site_plan(data)
    if data.get("action") == "plan-site":
        return {"plan": plan}
    if not _constant_time_equal(str(data.get("token") or ""), plan["token"]):
        raise certificates.CertificateError("配置或服务器环境已变化，请重新检查并确认")
    settings = _nginx_settings()
    old_settings = settings.read()
    old_settings_text = settings.path.read_text(encoding="utf-8") if settings.path.exists() else None
    old_projects = dict(PROJECTS)
    if plan["local_setup"]:
        nginx_runtime.prepare_local(plan["local_setup"])
    try:
        if not old_settings["configured"] or old_settings["profile"].get("mode") == "none":
            _save_nginx_settings({"mode": "local"})
        saved = settings.read()
        saved["upstreams"][project.key] = plan["host"]
        settings.save(saved["profile"], saved["upstreams"])
        projects = dict(PROJECTS)
        projects[project.key] = project
        _save_and_reload_projects(projects)
        runtime = settings.runtime()
        nginx_runtime.check_domain_conflict(runtime, project.app_domain, _project_nginx_conf_path(project))
        result = _configure_project_nginx(project, issue_https=False)
        if not result["ok"]:
            raise certificates.CertificateError(result["results"][-1]["detail"])
    except (ValueError, OSError):
        # Nginx config application rolls itself back; restore its metadata as well.
        if old_settings_text is None:
            settings.path.unlink(missing_ok=True)
        else:
            certificates.atomic_write(settings.path, old_settings_text)
        if PROJECTS != old_projects:
            _save_and_reload_projects(old_projects)
        raise
    return {"ok": True, "url": result["url"], "settings": _nginx_payload(), "notice": plan["notice"]}


@_maintenance_shared_operation
def _save_nginx_settings(data: dict[str, Any]) -> dict[str, Any]:
    with _config_transaction_lock, _nginx_lock:
        settings = _nginx_settings()
        old = settings.read()
        profile = settings.candidate(data.get("mode"), data.get("container", ""))
        if old["profile"] != profile:
            cert_dir = STATE_FILE.parent / "certificates"
            if cert_dir.exists() and any(cert_dir.iterdir()):
                raise certificates.CertificateError("请先停用并删除已有证书，再切换 Nginx 实例")
            if old["profile"].get("mode") != "none":
                old_runtime = settings.runtime(live=False)
                if nginx_requests.enabled(old_runtime):
                    raise certificates.CertificateError("请先在请求记录页关闭记录，再切换 Nginx 实例")
                if any(old_runtime.conf_root.glob("mini-deploy-*.conf")):
                    raise certificates.CertificateError("旧实例仍有 mini-deploy 站点配置，请先移除这些站点再切换实例")
        settings.save(profile, old["upstreams"] if old["profile"] == profile else {})
        return _nginx_payload()


@_maintenance_shared_operation
def _nginx_upstream_operation(data: dict[str, Any]) -> dict[str, Any]:
    with _config_transaction_lock, _nginx_lock:
        settings = _nginx_settings()
        saved = settings.read()
        key = data.get("project")
        if not isinstance(key, str) or key not in PROJECTS:
            raise certificates.CertificateError("请选择已保存的站点")
        project = PROJECTS[key]
        runtime = settings.runtime()
        host = runtime.upstream(data.get("host"))
        runtime.probe(host, project.service_port)
        if data.get("action") == "save-upstream":
            old_host = saved["upstreams"].get(key, "127.0.0.1")
            if old_host != host and _project_nginx_conf_path(project).exists():
                raise certificates.CertificateError("现有站点配置仍在使用旧地址，请先在接入设置中移除站点再修改后端地址")
            saved["upstreams"][key] = host
            settings.save(saved["profile"], saved["upstreams"])
        return {"ok": True, "host": host, "port": project.service_port, "settings": _nginx_payload()}


@_maintenance_shared_operation
def _remove_nginx_site(data: dict[str, Any]) -> dict[str, Any]:
    with _config_transaction_lock, _nginx_lock:
        key = data.get("project")
        if not isinstance(key, str) or key not in PROJECTS:
            raise certificates.CertificateError("请选择已保存的站点")
        project = PROJECTS[key]
        runtime = _nginx_settings().runtime()
        store = certificates.CertificateStore(STATE_FILE.parent / "certificates", runtime)
        path = _project_nginx_conf_path(project)
        previous = store.config(path)
        record = store.read(project)
        if record and store.active(project, record, previous):
            raise certificates.CertificateError("请先停用 HTTPS，再移除站点")
        if previous and previous != _project_nginx_config_text(project) and not previous.startswith("# mini-deploy-managed: project-http-v1\n"):
            raise certificates.CertificateError("仅能移除本面板托管的 HTTP 站点")
        if previous:
            # Keep an empty managed placeholder until Nginx accepts the removal.
            store.commit_config(path, "# mini-deploy-managed: project-http-v1\n", previous)
            path.unlink()
        return {"ok": True}


def _certificates_payload() -> dict[str, Any]:
    with _config_transaction_lock, _nginx_lock:
        items = []
        for project in PROJECTS.values():
            try:
                store = _certificate_store()
                items.append(store.describe(project, _project_nginx_conf_path(project)))
            except (ValueError, OSError):
                items.append({"project": project.key, "name": project.name, "domain": project.app_domain,
                              "certificate": None, "error": "证书记录或文件权限异常，请检查服务器"})
        return {"projects": items, "nginx_available": bool(shutil.which("nginx")),
                "openssl_available": bool(shutil.which("openssl"))}


@_maintenance_shared_operation
def _certificate_operation(data: dict[str, Any]) -> None:
    with _config_transaction_lock, _nginx_lock:
        key = data.get("project")
        if not isinstance(key, str) or key not in PROJECTS:
            raise certificates.CertificateError("请选择已登记的站点")
        project = PROJECTS[key]
        settings = _nginx_settings()
        if not settings.read()["configured"]:
            raise certificates.CertificateError("请先检测并保存 Nginx 运行环境")
        runtime = settings.runtime()
        store = certificates.CertificateStore(STATE_FILE.parent / "certificates", runtime)
        if data.get("action") == "enable":
            if runtime.mode == "docker" and runtime.profile.get("network") != "host":
                item = nginx_runtime.inspect_container(runtime.profile.get("container", ""))
                bindings = item.get("ports", {}).get("443/tcp") or []
                if not any(binding.get("HostPort") == "443" for binding in bindings):
                    raise certificates.CertificateError("此容器尚未发布 HTTPS 443 端口。可先使用 HTTP；启用 HTTPS 前需维护容器端口映射，面板不会自动重建容器")
            runtime.probe(settings.read()["upstreams"].get(key), project.service_port)
        store.operate(project, data.get("action"), data,
                                     _project_nginx_conf_path(project), _project_nginx_config_text(project))


def _project_nginx_conf_path(project: Site) -> Path:
    return _nginx_settings().runtime(live=False).conf_root / f"mini-deploy-{project.key}.conf"


def _project_nginx_config_text(project: Site) -> str:
    domain = _normalize_domain(project.app_domain)
    port = project.service_port or 8000
    settings = _nginx_settings()
    upstream = settings.runtime(live=False).upstream(settings.read()["upstreams"].get(project.key))
    return _nginx_http_config_text(domain, upstream, port)


def _nginx_http_config_text(domain: str, upstream: str, port: int) -> str:
    return "\n".join([
        "server {",
        "    listen 80;",
        f"    server_name {domain};",
        "",
        "    client_max_body_size 50m;",
        "",
        "    location / {",
        f"        proxy_pass http://{upstream}:{port};",
        "        proxy_http_version 1.1;",
        "        proxy_set_header Host $host;",
        "        proxy_set_header X-Real-IP $remote_addr;",
        "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        "        proxy_set_header X-Forwarded-Proto $scheme;",
        "        proxy_set_header Upgrade $http_upgrade;",
        "        proxy_set_header Connection \"upgrade\";",
        "    }",
        "}",
        "",
    ])


@_maintenance_shared_operation
@_nginx_serialized
def _configure_project_nginx(project: Site, *, issue_https: bool | None = None) -> dict[str, Any]:
    domain = _normalize_domain(project.app_domain)
    https_requested = project.app_https if issue_https is None else bool(issue_https)
    results: list[dict[str, Any]] = []
    profile: dict[str, Any] = {}
    try:
        settings = _nginx_settings()
        saved = settings.read()
        profile = saved["profile"]
        if not saved["configured"]:
            raise certificates.CertificateError("请先在 Nginx 证书页检测并保存运行环境")
        if not _valid_domain(domain) or not 1 <= int(project.service_port or 0) <= 65535:
            raise certificates.CertificateError("请先填写有效的业务域名和服务端口")
        runtime = settings.runtime()
        store = certificates.CertificateStore(STATE_FILE.parent / "certificates", runtime)
        conf_path = _project_nginx_conf_path(project)
        previous = store.config(conf_path)
        record = store.read(project)
        if record and store.active(project, record, previous):
            raise certificates.CertificateError("此站点已使用上传证书，请到证书管理中操作 HTTPS")
        config = _project_nginx_config_text(project)
        marker = "# mini-deploy-managed: project-http-v1\n"
        if previous and previous != config and not previous.startswith(marker):
            raise certificates.CertificateError("已有站点不是本面板托管的 HTTP 配置，拒绝覆盖")
        runtime.probe(settings.read()["upstreams"].get(project.key), project.service_port)
        store.commit_config(conf_path, marker + config, previous)
        results.append(_nginx_step_result("nginx_reload", True, "Nginx 配置已校验并重载"))
        if https_requested:
            if runtime.mode == "docker":
                results.append(_nginx_step_result("certbot_https", False, "Docker Nginx 请在证书页上传证书并启用 HTTPS；不会在宿主机运行 certbot --nginx"))
            elif not shutil.which("certbot"):
                results.append(_nginx_step_result("certbot_https", False, "未安装 Certbot，HTTP 已可用；也可上传证书"))
            else:
                results.append(_run_nginx_command([
                    "certbot", "--nginx", "-d", domain, "--non-interactive",
                    "--agree-tos", "--register-unsafely-without-email",
                ], step="certbot_https", timeout=180.0))
    except (ValueError, OSError) as exc:
        results.append(_nginx_step_result("nginx", False, str(exc)))
    http_ready = any(item["step"] == "nginx_reload" and item["ok"] for item in results)
    https_ok = any(item["step"] == "certbot_https" and item["ok"] for item in results)
    return {
        "ok": http_ready and (not https_requested or https_ok), "http_ready": http_ready,
        "https_ok": https_ok, "project": project.key, "domain": domain,
        "url": f"https://{domain}" if https_ok else _nginx_http_url(domain, profile), "results": results,
    }


def _percent(value: float | int | str | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return round(max(0.0, min(100.0, parsed)), 1)


def _read_cpu_total_idle() -> tuple[int, int] | None:
    try:
        first = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
        parts = [int(part) for part in first.split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        return sum(parts), idle
    except Exception:
        return None


def _cpu_percent() -> float | None:
    global _last_cpu_sample
    sample = _read_cpu_total_idle()
    if sample is None:
        return None
    if _last_cpu_sample is None:
        _last_cpu_sample = sample
        return None
    prev_total, prev_idle = _last_cpu_sample
    total, idle = sample
    _last_cpu_sample = sample
    total_delta = total - prev_total
    idle_delta = idle - prev_idle
    if total_delta <= 0:
        return None
    return _percent((1 - idle_delta / total_delta) * 100)


def _load_average() -> dict[str, float | None]:
    try:
        one, five, fifteen, *_ = Path("/proc/loadavg").read_text(encoding="utf-8").split()
        return {"one": float(one), "five": float(five), "fifteen": float(fifteen)}
    except Exception:
        return {"one": None, "five": None, "fifteen": None}


def _memory_status() -> dict[str, Any]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            values[key] = int(raw.strip().split()[0]) * 1024
    except Exception:
        return {"used_mb": None, "total_mb": None, "percent": None}
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    used = max(0, total - available)
    return {
        "used_mb": round(used / 1024 / 1024, 1) if total else None,
        "total_mb": round(total / 1024 / 1024, 1) if total else None,
        "percent": _percent(used / total * 100) if total else None,
    }


def _disk_status(path: Path = Path("/")) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(path)
        used = usage.total - usage.free
        return {
            "path": str(path),
            "used_gb": round(used / 1024 / 1024 / 1024, 1),
            "total_gb": round(usage.total / 1024 / 1024 / 1024, 1),
            "percent": _percent(used / usage.total * 100) if usage.total else None,
        }
    except Exception:
        return {"path": str(path), "used_gb": None, "total_gb": None, "percent": None}


def _network_totals() -> tuple[int, int]:
    rx_total = 0
    tx_total = 0
    try:
        lines = Path("/proc/net/dev").read_text(encoding="utf-8").splitlines()[2:]
        for line in lines:
            iface, raw = line.split(":", 1)
            if iface.strip() == "lo":
                continue
            parts = raw.split()
            if len(parts) >= 16:
                rx_total += int(parts[0])
                tx_total += int(parts[8])
    except Exception:
        pass
    return rx_total, tx_total


def _network_status() -> dict[str, Any]:
    global _last_network_sample
    now = time.time()
    rx_total, tx_total = _network_totals()
    if _last_network_sample is None:
        _last_network_sample = (now, rx_total, tx_total)
        return {
            "rx_kbps": None,
            "tx_kbps": None,
            "total_kbps": None,
            "percent": None,
            "max_mbps": NETWORK_MAX_MBPS,
        }
    prev_at, prev_rx, prev_tx = _last_network_sample
    _last_network_sample = (now, rx_total, tx_total)
    elapsed = max(now - prev_at, 0.001)
    rx_kbps = max(0.0, (rx_total - prev_rx) / elapsed / 1024)
    tx_kbps = max(0.0, (tx_total - prev_tx) / elapsed / 1024)
    total_kbps = rx_kbps + tx_kbps
    return {
        "rx_kbps": round(rx_kbps, 1),
        "tx_kbps": round(tx_kbps, 1),
        "total_kbps": round(total_kbps, 1),
        "percent": _percent(total_kbps / max(NETWORK_MAX_MBPS * 1024, 1) * 100),
        "max_mbps": NETWORK_MAX_MBPS,
    }


def _run_command(command: list[str], timeout: float = 3.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip()
    except Exception as exc:
        return 1, str(exc)


def _docker_log_counts_snapshot() -> dict[str, dict[str, int]]:
    with _docker_log_counts_lock:
        return {key: dict(value) for key, value in _docker_log_counts.items()}


def _docker_status() -> dict[str, Any]:
    if not shutil.which("docker"):
        return {"available": False, "error": "docker command not found", "containers": []}
    code, ps_output = _run_command(["docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}"], timeout=4.0)
    if code != 0:
        return {"available": False, "error": ps_output or "docker ps failed", "containers": []}

    containers = []
    by_id: dict[str, dict[str, Any]] = {}
    for line in ps_output.splitlines():
        try:
            item = json.loads(line)
        except Exception:
            continue
        container_id = str(item.get("ID") or "")
        labels = str(item.get("Labels") or "")
        service = ""
        for label in labels.split(","):
            if label.startswith("com.docker.compose.service="):
                service = label.split("=", 1)[1]
                break
        status_text = str(item.get("Status") or "")
        health = ""
        if "(healthy)" in status_text:
            health = "healthy"
        elif "(unhealthy)" in status_text:
            health = "unhealthy"
        elif "(health: starting)" in status_text:
            health = "starting"
        container = {
            "id": container_id,
            "name": str(item.get("Names") or ""),
            "image": str(item.get("Image") or ""),
            "service": service,
            "state": str(item.get("State") or ""),
            "status": status_text,
            "health": health,
            "ports": str(item.get("Ports") or ""),
            "cpu_percent": None,
            "memory_percent": None,
            "memory_usage": "",
            "net_io": "",
            "recent_error_count": 0,
            "recent_warn_count": 0,
        }
        containers.append(container)
        if container_id:
            by_id[container_id] = container

    stats_code, stats_output = _run_command(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
        timeout=6.0,
    )
    if stats_code == 0:
        for line in stats_output.splitlines():
            try:
                item = json.loads(line)
            except Exception:
                continue
            container_id = str(item.get("ID") or "")
            target = by_id.get(container_id)
            if not target:
                name = str(item.get("Name") or item.get("Container") or "")
                target = next((c for c in containers if c.get("name") == name), None)
            if not target:
                continue
            target["cpu_percent"] = _percent(str(item.get("CPUPerc") or "").replace("%", ""))
            target["memory_percent"] = _percent(str(item.get("MemPerc") or "").replace("%", ""))
            target["memory_usage"] = str(item.get("MemUsage") or "")
            target["net_io"] = str(item.get("NetIO") or "")

    log_counts = _docker_log_counts_snapshot()
    for container in containers:
        counts = log_counts.get(str(container.get("id") or ""), {})
        container["recent_error_count"] = int(counts.get("error", 0) or 0)
        container["recent_warn_count"] = int(counts.get("warn", 0) or 0)

    return {"available": True, "error": "" if stats_code == 0 else stats_output, "containers": containers}


def _sample_docker_log_counts() -> None:
    global _docker_log_counts
    if not shutil.which("docker"):
        with _docker_log_counts_lock:
            _docker_log_counts = {}
        return
    code, output = _run_command(["docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}"], timeout=4.0)
    if code != 0:
        return
    error_matcher, _ = _docker_log_matcher({"level": "error", "keyword": "", "regex": False, "context": 0})
    warn_matcher, _ = _docker_log_matcher({"level": "warn", "keyword": "", "regex": False, "context": 0})
    counts: dict[str, dict[str, int]] = {}
    for line in output.splitlines():
        try:
            container_id = str(json.loads(line).get("ID") or "")
        except (TypeError, ValueError):
            continue
        if not re.fullmatch(r"[a-f0-9]{64}", container_id):
            continue
        try:
            lines = _docker_logs(container_id, tail=200)
        except Exception:
            continue
        counts[container_id] = {
            "error": sum(1 for item in lines if error_matcher and error_matcher(item)),
            "warn": sum(1 for item in lines if warn_matcher and warn_matcher(item)),
        }
    with _docker_log_counts_lock:
        _docker_log_counts = counts


def _docker_log_metric_sampler() -> None:
    time.sleep(2)
    while True:
        try:
            _sample_docker_log_counts()
        except Exception as exc:  # noqa: BLE001 - sampling must survive transient Docker errors
            _log(f"docker log metric sample failed: {exc}")
        time.sleep(DOCKER_LOG_METRIC_INTERVAL_SECONDS)


def _docker_container_names() -> set[str]:
    code, output = _run_command(["docker", "ps", "-a", "--format", "{{.ID}}\t{{.Names}}"], timeout=4.0)
    if code != 0:
        raise RuntimeError(output or "docker ps failed")
    names: set[str] = set()
    for line in output.splitlines():
        parts = line.split("\t", 1)
        if parts and parts[0]:
            names.add(parts[0])
        if len(parts) > 1 and parts[1]:
            names.add(parts[1])
    return names


def _validate_docker_container(container: Any) -> str:
    name = str(container or "").strip()
    if not name or len(name) > 128:
        raise ValueError("invalid_container")
    if name not in _docker_container_names():
        raise ValueError("container_not_found")
    return name


def _docker_logs(container: str, tail: int = 200) -> list[str]:
    if not shutil.which("docker"):
        raise RuntimeError("docker command not found")
    safe_tail = _docker_log_line_limit(tail)
    code, output = _run_command(["docker", "logs", "--tail", str(safe_tail), container], timeout=10.0)
    if code != 0:
        raise RuntimeError(output or "docker logs failed")
    return output.splitlines()


def _docker_logs_text(container: str, lines: int | None = None, all_lines: bool = False) -> tuple[str, bool]:
    if not shutil.which("docker"):
        raise RuntimeError("docker command not found")
    command = ["docker", "logs"]
    if not all_lines:
        command.extend(["--tail", str(_docker_log_line_limit(lines))])
    command.append(container)
    code, output = _run_command(command, timeout=20.0 if all_lines else 10.0)
    if code != 0:
        raise RuntimeError(output or "docker logs failed")
    if len(output.encode("utf-8", errors="replace")) > LOG_DOWNLOAD_MAX_BYTES:
        encoded = output.encode("utf-8", errors="replace")[-LOG_DOWNLOAD_MAX_BYTES:]
        return encoded.decode("utf-8", errors="replace"), True
    return output, False


@_maintenance_shared_operation
def _docker_action(container: str, action: str, expected_id: str = "") -> str:
    if not shutil.which("docker"):
        raise RuntimeError("docker command not found")
    allowed = {"restart", "stop", "start", "pause", "unpause", "remove"}
    if action not in allowed:
        raise ValueError("invalid_action")
    if action == "remove":
        if not re.fullmatch(r"[a-f0-9]{64}", expected_id):
            raise ValueError("请刷新列表并确认要删除的容器")
        code, output = _run_command(["docker", "inspect", "--type", "container", "--format",
            '{{json .Id}} {{json .State.Status}}', container], timeout=5.0)
        if code != 0:
            raise ValueError("容器已不存在，请刷新列表")
        parts = output.strip().split()
        if len(parts) != 2 or json.loads(parts[0]) != expected_id:
            raise ValueError("容器已发生变化，请刷新后重新确认")
        if json.loads(parts[1]) not in {"exited", "created", "dead"}:
            raise ValueError("请先停止容器，再执行删除")
        command = ["docker", "rm", expected_id]
    else:
        command = ["docker", action, container]
    code, output = _run_command(command, timeout=30.0)
    if code != 0:
        raise RuntimeError(output or f"docker {action} failed")
    return output


def _docker_images() -> list[dict[str, Any]]:
    code, output = _run_command(["docker", "image", "ls", "--no-trunc", "--digests", "--format", "{{json .}}"], timeout=10)
    if code != 0:
        raise RuntimeError(output or "无法读取 Docker 镜像")
    images: dict[str, dict[str, Any]] = {}
    for line in output.splitlines():
        item = json.loads(line)
        image_id = str(item.get("ID", ""))
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
            raise RuntimeError("Docker 返回了无法识别的镜像 ID")
        image = images.setdefault(image_id, {"id": image_id, "tags": [], "size": item.get("Size", ""),
                                            "created": item.get("CreatedAt", ""), "digests": []})
        if item.get("Repository") not in (None, "<none>") and item.get("Tag") not in (None, "<none>"):
            tag = f'{item["Repository"]}:{item["Tag"]}'
            if tag not in image["tags"]:
                image["tags"].append(tag)
        digest = item.get("Digest")
        if digest and digest != "<none>" and digest not in image["digests"]:
            image["digests"].append(digest)
    return list(images.values())


_docker_mirror_manager: docker_mirrors.Manager | None = None
_docker_mirror_lock = threading.Lock()


def _docker_mirrors_manager() -> docker_mirrors.Manager:
    global _docker_mirror_manager
    with _docker_mirror_lock:
        if _docker_mirror_manager is None or _docker_mirror_manager.home != STATE_FILE.parent:
            _docker_mirror_manager = docker_mirrors.Manager(STATE_FILE.parent)
        return _docker_mirror_manager


@_maintenance_shared_operation
def _apply_docker_mirrors(operation: Any) -> None:
    operation()


@_maintenance_shared_operation
def _docker_image_action(action: str, reference: str) -> str:
    if action == "pull":
        if (len(reference) > 255 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", reference)
                or "://" in reference):
            raise ValueError("请输入镜像名称，例如 nginx:stable-alpine 或 registry.example.com/team/app:v1")
        command = ["docker", "pull", reference]
    elif action == "remove":
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", reference):
            raise ValueError("请刷新列表并选择完整镜像 ID")
        command = ["docker", "image", "rm", reference]
    else:
        raise ValueError("不支持的镜像操作")
    code, output = _run_command(command, timeout=180 if action == "pull" else 30)
    if code != 0:
        hint = "拉取失败，请检查镜像名称、仓库权限、镜像源和服务器网络。" if action == "pull" else "删除失败：镜像可能被运行中或已停止的容器引用，或有多个标签；不会强制删除。"
        raise RuntimeError(f"{hint}\n{output[-4000:]}")
    return output[-4000:]


def _system_status_payload() -> dict[str, Any]:
    now = time.time()
    with _system_status_lock:
        if now - float(_system_status_cache.get("at") or 0) < SYSTEM_STATUS_CACHE_SECONDS:
            return json.loads(json.dumps(_system_status_cache.get("payload") or {}, ensure_ascii=False))
        realtime = _realtime_metrics_payload()
        server = realtime["server"]
        history = _record_system_metric(server, now)
        payload = {
            **realtime,
            "server": server,
            "docker": _docker_status(),
            "history": history,
            "history_interval_seconds": SYSTEM_METRIC_INTERVAL_SECONDS,
            "history_max_points": SYSTEM_METRIC_MAX_POINTS,
        }
        _system_status_cache["at"] = now
        _system_status_cache["payload"] = payload
        return json.loads(json.dumps(payload, ensure_ascii=False))


def _sample_realtime_metrics() -> None:
    global _realtime_server
    # Only this sampler reads the cumulative CPU/network counters.
    now = time.time()
    server = {
        "cpu_percent": _cpu_percent(), "load": _load_average(), "memory": _memory_status(),
        "disk": _disk_status(Path("/")), "network": _network_status(),
        "sampled_at": _now_text(), "sampled_ts": now, "cache_seconds": 1,
    }
    memory, network = server["memory"], server["network"]
    entry = {
        "ts": now, "cpu_percent": server["cpu_percent"], "memory_percent": memory.get("percent"),
        "network_total_kbps": network.get("total_kbps"), "network_rx_kbps": network.get("rx_kbps"),
        "network_tx_kbps": network.get("tx_kbps"),
    }
    with _realtime_metrics_lock:
        _realtime_server = server
        _realtime_metrics.appendleft(entry)


def _realtime_metrics_payload() -> dict[str, Any]:
    with _realtime_metrics_lock:
        return json.loads(json.dumps({
            "server": _realtime_server, "realtime_history": list(_realtime_metrics),
            "realtime_interval_seconds": 1,
        }, ensure_ascii=False))


def _realtime_metric_sampler() -> None:
    deadline = time.monotonic()
    while True:
        try:
            _sample_realtime_metrics()
        except Exception as exc:  # noqa: BLE001 - sampling must survive transient OS errors
            _log(f"realtime metric sample failed: {exc}")
        deadline += 1
        now = time.monotonic()
        if deadline <= now:
            deadline = now + 1
        time.sleep(deadline - now)


def _system_metric_sampler() -> None:
    """Keep long-range system trends populated independently of UI traffic."""
    time.sleep(2)
    while True:
        try:
            _system_status_payload()
        except Exception as exc:  # noqa: BLE001 - sampler must never stop the agent
            _log(f"system metric sample failed: {exc}")
        time.sleep(max(60, SYSTEM_METRIC_INTERVAL_SECONDS))




class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


_HEALTH_OPENER = build_opener(_NoRedirectHandler)


def _probe_health_url(project: Site) -> dict[str, Any]:
    url = project.health_url.strip()
    checked_at = _now_text()
    started = time.monotonic()
    if not url:
        return {"status": "not_configured", "code": None, "duration_ms": None,
                "checked_at": checked_at, "detail": "未配置健康检查地址"}
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("健康检查地址必须是没有账号密码和片段的 HTTP/HTTPS 地址")
        request = Request(url, headers={"User-Agent": "mini_deploy-health-check"}, method="GET")
        with _HEALTH_OPENER.open(request, timeout=HEALTH_CHECK_TIMEOUT_SECONDS) as response:
            code = int(response.status)
            response.read(512)
        ok = 200 <= code < 400
        return {"status": "healthy" if ok else "failed", "code": code,
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
                "checked_at": checked_at, "detail": "HTTP 响应正常" if ok else f"HTTP 状态码 {code}"}
    except HTTPError as exc:
        return {"status": "failed", "code": int(exc.code),
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
                "checked_at": checked_at, "detail": f"HTTP 状态码 {exc.code}"}
    except (OSError, URLError, ValueError) as exc:
        return {"status": "failed", "code": None,
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
                "checked_at": checked_at, "detail": str(exc)[:240]}


def _health_status_for(project: Site) -> dict[str, Any]:
    if not project.health_url:
        return {"status": "not_configured", "code": None, "duration_ms": None,
                "checked_at": None, "detail": "未配置健康检查地址"}
    with _health_status_lock:
        cached = _health_status.get(project.key)
        return dict(cached) if cached else {
            "status": "pending", "code": None, "duration_ms": None,
            "checked_at": None, "detail": "等待首次检查",
        }




def _health_check_sampler() -> None:
    probes = ThreadPoolExecutor(max_workers=4, thread_name_prefix="website-probe")
    sender = ThreadPoolExecutor(max_workers=1, thread_name_prefix="alert-delivery")
    running: dict[Any, dict[str, Any]] = {}
    sending = None
    next_system = 0.0
    while True:
        try:
            monitor = _monitor()
            now = time.time()
            for future, target in list(running.items()):
                if future.done():
                    del running[future]
                    try:
                        result = future.result()
                        if target.get("legacy"):
                            site = PROJECTS.get(target["site"].key)
                            if site == target["site"]:
                                with _health_status_lock:
                                    _health_status[site.key] = {**result, "sampled_at": now}
                                monitor.observe(f"health:{site.key}", result["status"] == "failed",
                                                f"健康检查失败：{site.name}", result["detail"], "health")
                        else:
                            monitor.accept(target, result)
                    except Exception as exc:
                        _log(f"website probe failed: {type(exc).__name__}")
            with monitor.lock:
                busy = {t["key"] for t in running.values()}
                with _projects_lock:
                    legacy = [{"key": f"health:{s.key}", "site": s, "legacy": True, "enabled": True}
                              for s in PROJECTS.values() if s.enabled and s.health_url]
                candidates = [*monitor.targets.values(), *legacy]
                for target in sorted(candidates, key=lambda t: monitor.due.get(t["key"], 0)):
                    key = target["key"]
                    if len(running) >= 4:
                        break
                    if not target["enabled"] or key in busy or monitor.due.get(key, 0) > now:
                        continue
                    cert = monitor.results.get(key, {}).get("certificate", {})
                    check_cert = (monitor.due.get(key, 0) == 0 or cert.get("status") not in {"valid", "not_applicable"}
                                  or now - cert.get("checked_at", 0) >= 3600)
                    future = (probes.submit(_probe_health_url, target["site"]) if target.get("legacy")
                              else probes.submit(monitoring.probe, dict(target), check_cert))
                    running[future] = dict(target)
                    monitor.due[key] = now + (HEALTH_CHECK_INTERVAL_SECONDS if target.get("legacy") else 30)
            monitor.evaluate_websites()
            if now >= next_system:
                _evaluate_resource_alerts(monitor, _system_status_payload())
                next_system = now + 15
            if sending and sending[0].done():
                future, key, message = sending
                sending = None
                try:
                    results = future.result()
                except Exception:
                    results = [{"enabled": True, "ok": False}]
                monitor.delivered(key, message, results)
            if sending is None:
                config = _notification_config_payload(include_secret=True)
                notice = monitor.next_notification(_any_notification_enabled(config))
                if notice:
                    key, message = notice
                    # Save retry timing before submitting, including across agent restarts.
                    monitor.flush()
                    sending = (sender.submit(_send_notifications, config, "mini_deploy · " + message["title"], message["body"]), key, message)
            if now - monitor.last_flush >= 30:
                monitor.flush()
        except Exception as exc:
            _log(f"monitoring iteration failed: {type(exc).__name__}: {exc}")
        time.sleep(1)


_monitor_instance: monitoring.Monitor | None = None
_monitor_lock = threading.Lock()


def _monitor() -> monitoring.Monitor:
    global _monitor_instance
    with _monitor_lock:
        path = STATE_FILE.with_name("website-monitoring.json")
        if _monitor_instance is None or _monitor_instance.path != path:
            _monitor_instance = monitoring.Monitor(path)
        return _monitor_instance


@_maintenance_shared_operation
def _monitoring_operation(data: dict[str, Any]) -> dict[str, Any]:
    return _monitor().operation(data)


def _evaluate_resource_alerts(monitor: monitoring.Monitor, system: dict[str, Any]) -> None:
    with _projects_lock:
        health_keys = {f"health:{s.key}" for s in PROJECTS.values() if s.enabled and s.health_url}
    with monitor.lock:
        for key in set(monitor.incidents) | set(monitor.pending):
            if key.startswith("health:") and key not in health_keys:
                monitor.incidents.pop(key, None)
                monitor.pending.pop(key, None)
    server = system.get("server") or {}
    values = [("cpu", "CPU 使用率", server.get("cpu_percent"), 90),
              ("memory", "内存使用率", (server.get("memory") or {}).get("percent"), 90),
              ("disk", "磁盘使用率", (server.get("disk") or {}).get("percent"), 90)]
    for key, title, value, threshold in values:
        value = _percent(value)
        monitor.observe(f"server:{key}", None if value is None else value >= threshold,
                        title + "过高", f"当前 {value}% · 阈值 {threshold}%", "server")
    docker = system.get("docker") or {}
    installed = docker.get("error") != "docker command not found"
    monitor.observe("docker:daemon", not docker["available"] if "available" in docker and installed else None,
                    "Docker 无法连接", "请检查 Docker 服务是否正常运行", "docker")
    if not docker.get("available"):
        return
    observed = set()
    for container in docker.get("containers") or []:
        key = "docker:container:" + str(container.get("id") or container.get("name"))
        observed.add(key)
        state = str(container.get("state") or "").lower()
        abnormal_exit = state == "exited" and bool(re.search(r"Exited \([1-9][0-9]*\)", str(container.get("status")), re.I))
        bad = container.get("health") == "unhealthy" or state in {"restarting", "dead"} or abnormal_exit
        monitor.observe(key, bad if state else None, "容器异常：" + str(container.get("name") or key),
                        str(container.get("status") or state), "docker")
    # Removed containers stop being watched; disappearance is not a recovery event.
    with monitor.lock:
        for key in list(monitor.incidents):
            if key.startswith("docker:container:") and key not in observed:
                monitor.incidents.pop(key, None)
                monitor.pending.pop(key, None)


def _alerts_payload(system: dict[str, Any]) -> list[dict[str, Any]]:
    return _monitor().snapshot()["alerts"]


def _status_payload() -> dict[str, Any]:
    system = _system_status_payload()
    try:
        monitored = _monitor().snapshot()
    except (OSError, ValueError, TypeError, AttributeError):
        monitored = {"alerts": [{"level": "critical", "source": "monitoring", "title": "无法读取监测数据",
                                  "detail": "请检查服务日志及 website-monitoring.json，原文件已保留", "at": _now_text()}],
                     "events": []}
    return {
        "agent": {"status": "ok", "host": HOST, "port": PORT,
                  "site_count": len(PROJECTS),
                  "ui_auth_configured": bool(UI_PASSWORD_HASH and UI_SESSION_SECRET)},
        "system": system, "alerts": monitored["alerts"], "events": monitored["events"],
        "csrf_token": "", "generated_at": int(time.time()),
    }


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_HASH_ITERATIONS,
    )
    return (
        "pbkdf2_sha256$"
        f"{PASSWORD_HASH_ITERATIONS}$"
        f"{base64.urlsafe_b64encode(salt).decode('ascii')}$"
        f"{base64.urlsafe_b64encode(digest).decode('ascii')}"
    )


def _parse_password_hash(value: str) -> tuple[int, bytes, bytes] | None:
    try:
        scheme, iterations_text, salt_b64, expected_b64 = str(value or "").split("$", 3)
        if scheme != "pbkdf2_sha256":
            return None
        iterations = int(iterations_text)
        if not MIN_PASSWORD_HASH_ITERATIONS <= iterations <= MAX_PASSWORD_HASH_ITERATIONS:
            return None
        salt = base64.b64decode(salt_b64.encode("ascii"), altchars=b"-_", validate=True)
        expected = base64.b64decode(expected_b64.encode("ascii"), altchars=b"-_", validate=True)
        if len(salt) != 16 or len(expected) != hashlib.sha256().digest_size:
            return None
        return iterations, salt, expected
    except (UnicodeEncodeError, ValueError):
        return None


def _verify_password(password: str) -> bool:
    parsed = _parse_password_hash(UI_PASSWORD_HASH)
    if parsed is None:
        return False
    iterations, salt, expected = parsed
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(digest, expected)


def _session_signature(expires: int) -> str:
    msg = f"deploy-ui:{expires}".encode("utf-8")
    return hmac.new(UI_SESSION_SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _csrf_token(session_value: str) -> str:
    msg = f"deploy-csrf:{session_value}".encode("utf-8")
    return hmac.new(UI_SESSION_SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _make_session_cookie() -> str:
    expires = int(time.time()) + UI_SESSION_TTL_SECONDS
    payload = f"{expires}:{_session_signature(expires)}"
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _verify_session_cookie(value: str) -> bool:
    if not UI_SESSION_SECRET:
        return False
    try:
        payload = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
        expires_text, signature = payload.split(":", 1)
        expires = int(expires_text)
    except Exception:
        return False
    if expires < int(time.time()):
        return False
    return _constant_time_equal(signature, _session_signature(expires))


def _request_client_ip(handler: Any) -> str:
    peer_ip = str(handler.client_address[0])
    try:
        peer_address = ipaddress.ip_address(peer_ip)
    except ValueError:
        return peer_ip
    if not TRUST_LOOPBACK_PROXY_HEADERS or not peer_address.is_loopback:
        return peer_ip
    forwarded = str(handler.headers.get("X-Real-IP", "")).strip()
    try:
        return str(ipaddress.ip_address(forwarded)) if forwarded else peer_ip
    except ValueError:
        return peer_ip


def _reserve_login_attempt(ip: str) -> bool:
    now = time.time()
    with _login_failures_lock:
        for client_ip, timestamps in list(_login_failures.items()):
            active = [
                timestamp
                for timestamp in timestamps
                if 0 <= now - timestamp < LOGIN_ATTEMPT_WINDOW_SECONDS
            ]
            if active:
                _login_failures[client_ip] = active
            else:
                _login_failures.pop(client_ip, None)

        attempts = list(_login_failures.get(ip, []))
        if len(attempts) >= LOGIN_ATTEMPT_LIMIT:
            _login_failures[ip] = attempts
            return False
        if ip not in _login_failures and len(_login_failures) >= LOGIN_FAILURE_MAX_CLIENTS:
            oldest_ip = min(
                _login_failures,
                key=lambda client_ip: _login_failures[client_ip][-1],
            )
            _login_failures.pop(oldest_ip, None)
        attempts.append(now)
        _login_failures[ip] = attempts
        return True


def _clear_login_attempts(ip: str) -> None:
    with _login_failures_lock:
        _login_failures.pop(ip, None)


def _upsert_env_file(path: Path, values: dict[str, str]) -> None:
    existing_lines: list[str] = []
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        metadata = None
    if metadata is not None:
        if stat.S_ISLNK(metadata.st_mode):
            raise OSError(f"refusing to update symbolic-link environment file: {path}")
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"environment file is not a regular file: {path}")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened_metadata = os.fstat(descriptor)
            if not stat.S_ISREG(opened_metadata.st_mode):
                raise OSError(f"environment file is not a regular file: {path}")
            if (
                metadata.st_dev,
                metadata.st_ino,
            ) != (
                opened_metadata.st_dev,
                opened_metadata.st_ino,
            ):
                raise OSError(f"environment file changed while opening: {path}")
            with os.fdopen(descriptor, "r", encoding="utf-8") as file_handle:
                descriptor = -1
                existing_lines = file_handle.read().splitlines()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    seen: set[str] = set()
    output: list[str] = []
    for line in existing_lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in values:
            output.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            output.append(line)
    for key, value in values.items():
        if key not in seen:
            output.append(f"{key}={value}")
    _atomic_write_private_text(path, "\n".join(output).rstrip() + "\n")


_DEPLOY_CONTROL_SECRET_ENV_NAMES = frozenset({
    "DEPLOY_UI_PASSWORD",
    "DEPLOY_UI_PASSWORD_FILE",
    "DEPLOY_UI_PASSWORD_HASH",
    "DEPLOY_UI_SESSION_SECRET",
    "DEPLOY_WEBHOOK_SECRET",
})


def _normal_path(path: str) -> str:
    if path == "/deploy":
        return "/ui"
    if path.startswith("/deploy/"):
        return path[len("/deploy") :]
    return path


UI_DIR = Path(__file__).resolve().parent / "ui"
UI_ASSET_TYPES = {
    "sites.js": "application/javascript; charset=utf-8",
    "monitoring.js": "application/javascript; charset=utf-8",
    "request-gateway.js": "application/javascript; charset=utf-8",
    "gateway-connect.js": "application/javascript; charset=utf-8",
    "selects.js": "application/javascript; charset=utf-8",
    "motion.js": "application/javascript; charset=utf-8",
    "nginx.js": "application/javascript; charset=utf-8",
    "docker-images.js": "application/javascript; charset=utf-8",
    "docker-mirrors.js": "application/javascript; charset=utf-8",
    "nginx-requests.js": "application/javascript; charset=utf-8",
    "certificates.js": "application/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
}


def _read_ui_file(name: str) -> str:
    return (UI_DIR / name).read_text(encoding="utf-8")


def _render_template(name: str, **values: str) -> str:
    rendered = _read_ui_file(name)
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def _render_login(error: str = "") -> str:
    error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
    setup_note = ""
    if not UI_PASSWORD_HASH:
        setup_command = (
            f"DEPLOY_AGENT_ENV_FILE={shlex.quote(str(AGENT_ENV_FILE))} "
            f"python3 {shlex.quote(str(Path(__file__).resolve()))} admin set-password"
        )
        setup_note = f"""
        <div class="setup">
          <strong>管理员密码尚未初始化。</strong>
          为避免公网抢注，网页不提供初始化功能。请在服务器执行
          <code>{html.escape(setup_command)}</code>。
        </div>
        """
        form_html = ""
    else:
        form_html = """
        <form method="post" action="login" class="login-form">
          <input type="password" name="password" placeholder="访问密码" autofocus autocomplete="current-password">
          <button type="submit">进入面板</button>
        </form>
        """
    return _render_template("login.html", ERROR_HTML=error_html, SETUP_NOTE=setup_note, FORM_HTML=form_html)


def _render_ui() -> str:
    return _read_ui_file("index.html")


class Handler(BaseHTTPRequestHandler):
    def _retired_deployment_route(self, path: str) -> bool:
        if path not in {"/webhook", "/redeploy", "/rollback", "/cancel", "/force-unlock", "/preflight", "/projects-config"} and not path.startswith("/projects-config/"):
            return False
        self.close_connection = True
        self._write_json(410, {"error": "deployment_removed", "detail": "部署功能已移除，请在原部署工具中管理代码更新；服务器文件和服务保持不变。"})
        return True

    server_version = "mini_deploy_agent/1.1"

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_html(self, status: int, html_text: str) -> None:
        body = html_text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_ui_asset(self, asset_name: str) -> None:
        content_type = UI_ASSET_TYPES.get(asset_name)
        if not content_type:
            self._write_json(404, {"error": "not_found"})
            return
        try:
            body = (UI_DIR / asset_name).read_bytes()
        except OSError:
            self._write_json(404, {"error": "not_found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_text_download(self, filename: str, text: str, truncated: bool = False) -> None:
        if truncated:
            text = (
                f"# Log was larger than {LOG_DOWNLOAD_MAX_BYTES} bytes; "
                "download contains the latest readable segment.\n"
                + text
            )
        body = text.encode("utf-8")
        safe_name = _safe_download_name(filename)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Disposition", f'attachment; filename="{safe_name}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_command_download(self, filename: str, command: list[str]) -> None:
        safe_name = _safe_download_name(filename)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Disposition", f'attachment; filename="{safe_name}"')
        self.end_headers()
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            if proc.stdout is not None:
                while True:
                    chunk = proc.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)

    def _redirect(self, location: str, clear_cookie: bool = False) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        if clear_cookie:
            self.send_header(
                "Set-Cookie",
                f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict",
            )
        self.end_headers()

    def _cookie_secure(self) -> bool:
        if COOKIE_SECURE_MODE in {"1", "true", "yes", "on", "always"}:
            return True
        if COOKIE_SECURE_MODE in {"0", "false", "no", "off", "never"}:
            return False
        forwarded_proto = self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
        forwarded_ssl = self.headers.get("X-Forwarded-SSL", "").strip().lower()
        return forwarded_proto == "https" or forwarded_ssl in {"1", "true", "on"}

    def _set_session_cookie(self) -> None:
        secure = "; Secure" if self._cookie_secure() else ""
        self.send_header(
            "Set-Cookie",
            (
                f"{COOKIE_NAME}={_make_session_cookie()}; Path=/; "
                f"Max-Age={UI_SESSION_TTL_SECONDS}; HttpOnly; SameSite=Strict{secure}"
            ),
        )

    def _authenticated(self) -> bool:
        return bool(self._session_cookie_value())

    def _session_cookie_value(self) -> str:
        cookie_header = self.headers.get("Cookie", "")
        jar = cookies.SimpleCookie()
        try:
            jar.load(cookie_header)
        except cookies.CookieError:
            return ""
        morsel = jar.get(COOKIE_NAME)
        if not morsel or not _verify_session_cookie(morsel.value):
            return ""
        return morsel.value

    def _require_auth_json(self) -> bool:
        if self._authenticated():
            if self.command in {"POST", "PUT", "PATCH", "DELETE"}:
                return self._require_csrf_json()
            return True
        self._write_json(401, {"error": "unauthorized"})
        return False

    def _csrf_token_for_request(self) -> str:
        session_value = self._session_cookie_value()
        return _csrf_token(session_value) if session_value else ""

    def _require_csrf_json(self) -> bool:
        expected = self._csrf_token_for_request()
        supplied = self.headers.get("X-CSRF-Token", "")
        if expected and supplied and _constant_time_equal(supplied, expected):
            return True
        _audit_event("csrf_rejected", actor=self.client_address[0], target=self.path, success=False)
        self._write_json(403, {"error": "invalid_csrf"})
        return False

    def _resolve_log_target(self, query: dict[str, list[str]]) -> tuple[Path, str]:
        return LOG_FILE, "agent"

    def _handle_logs(self, query: dict[str, list[str]]) -> None:
        lines = _log_line_limit(query.get("lines", [LOG_TAIL_LINES])[0], default=LOG_TAIL_LINES)
        self._write_json(200, {
            "line_limit": lines, "max_line_limit": LOG_TAIL_MAX_LINES,
            "agent_log": _tail(LOG_FILE, lines=lines),
        })

    def _handle_logs_download(self, query: dict[str, list[str]]) -> None:
        path, label = self._resolve_log_target(query)
        raw_lines = (query.get("lines", [""])[0] or "").strip().lower()
        all_lines = raw_lines in {"all", "0", "-1"} or (query.get("all", [""])[0] or "").strip() == "1"
        lines = None if all_lines else _log_line_limit(raw_lines, default=LOG_TAIL_LINES)
        text, truncated = _read_log_text(path, lines=lines, all_lines=all_lines)
        suffix = "all" if all_lines else f"{lines}-lines"
        self._write_text_download(f"{label}-{suffix}.log", text, truncated=truncated)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        path = _normal_path(parsed.path)
        if self._retired_deployment_route(path):
            return
        if path == "/sites":
            if self._require_auth_json():
                self._write_json(200, _sites_config_payload())
            return
        if path == "/monitoring":
            if self._require_auth_json():
                try:
                    payload = _monitor().snapshot()
                except (OSError, ValueError, TypeError, AttributeError):
                    self._write_json(503, {"error": "monitoring_unavailable", "detail": "无法读取监测数据，请检查服务日志；原文件已保留"})
                    return
                payload["notifications_enabled"] = _any_notification_enabled(_notification_config_payload())
                self._write_json(200, payload)
            return
        if path == "/notifications":
            if self._require_auth_json():
                self._write_json(200, {"notifications": _notification_config_payload(include_secret=True)})
            return
        if path == "/health":
            self._write_json(200, {"status": "ok"})
            return
        if path in {"/", "/ui", "/ui/"}:
            if self._authenticated():
                self._write_html(200, _render_ui())
            else:
                self._write_html(200, _render_login())
            return
        if path.startswith("/ui/"):
            self._write_ui_asset(path.removeprefix("/ui/"))
            return
        if path == "/status":
            if self._require_auth_json():
                payload = _status_payload()
                payload["csrf_token"] = self._csrf_token_for_request()
                self._write_json(200, payload)
            return
        if path == "/system-metrics":
            if self._require_auth_json():
                self._write_json(200, _realtime_metrics_payload())
            return
        if path == "/logs":
            if self._require_auth_json():
                self._handle_logs(parse_qs(parsed.query))
            return
        if path == "/logs/download":
            if self._authenticated():
                self._handle_logs_download(parse_qs(parsed.query))
            else:
                self._write_json(401, {"error": "unauthorized"})
            return
        if path == "/docker/logs":
            if self._require_auth_json():
                self._handle_docker_logs(parse_qs(parsed.query))
            return
        if path == "/docker/images":
            if self._require_auth_json():
                try:
                    self._write_json(200, {"images": _docker_images()})
                except (OSError, ValueError, RuntimeError) as exc:
                    self._write_json(500, {"error": "docker_images_failed", "detail": str(exc)})
            return
        if path == "/docker/mirrors":
            if self._require_auth_json():
                try:
                    result = _docker_mirrors_manager().status(job_only=parse_qs(parsed.query).get("job") == ["1"])
                    self._write_json(200, result)
                except (OSError, ValueError, RuntimeError) as exc:
                    self._write_json(503, {"error": "docker_mirrors_unavailable", "detail": str(exc)})
            return
        if path == "/docker/logs/download":
            if self._authenticated():
                self._handle_docker_logs_download(parse_qs(parsed.query))
            else:
                self._write_json(401, {"error": "unauthorized"})
            return
        if path == "/certificates":
            if self._require_auth_json():
                self._write_json(200, _certificates_payload())
            return
        if path == "/nginx-settings":
            if self._require_auth_json():
                try:
                    self._write_json(200, _nginx_payload(discover=parse_qs(parsed.query).get("discover") == ["1"]))
                except (ValueError, OSError) as exc:
                    self._write_json(400, {"error": "nginx_settings_invalid", "detail": str(exc)})
            return
        if path == "/nginx-requests":
            if self._require_auth_json():
                try:
                    count = int(parse_qs(parsed.query).get("limit", ["100"])[0])
                    self._write_json(200, _nginx_requests_payload(count))
                except (ValueError, OSError) as exc:
                    self._write_json(400, {"error": "nginx_requests_failed", "detail": str(exc)})
            return
        if path == "/caddy-requests":
            if self._require_auth_json():
                try:
                    query = parse_qs(parsed.query)
                    count = int(query.get("limit", ["100"])[0])
                    container = query.get("container", [""])[0]
                    self._write_json(200, caddy_requests.recent(count, container))
                except (ValueError, OSError) as exc:
                    self._write_json(400, {"error": "caddy_requests_failed", "detail": str(exc)})
            return
        if path in {"/request-gateways", "/request-gateways/networks", "/gateway-requests", "/gateway-connections/discover"}:
            if self._require_auth_json():
                try:
                    store = request_gateway.Store(STATE_FILE.parent)
                    query = parse_qs(parsed.query)
                    if path == "/gateway-connections/discover":
                        result = gateway_connections.discover()
                    elif path == "/gateway-requests":
                        result = store.records(query.get("key", [""])[0], int(query.get("limit", ["100"])[0]))
                    elif path.endswith("/networks"):
                        result = {"networks": request_gateway.networks()}
                    else:
                        with request_gateway.LOCK:
                            result = {"entries": [store.describe(entry) for entry in store.entries()]}
                    self._write_json(200, result)
                except (ValueError, OSError) as exc:
                    self._write_json(400, {"error": "gateway_read_failed", "detail": str(exc)})
            return
        if path == "/audit":
            if self._require_auth_json():
                query = parse_qs(parsed.query)
                lines = _log_line_limit(query.get("lines", ["200"])[0], default=200)
                self._write_json(200, {"events": _audit_tail(lines=lines), "line_limit": lines})
            return
        if path == "/logout":
            self._redirect("ui", clear_cookie=True)
            return
        self._write_json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        path = _normal_path(parsed.path)
        if self._retired_deployment_route(path):
            return
        if path == "/monitoring":
            if self._require_auth_json():
                try:
                    data = _read_json_body(self, max_bytes=8192)
                    result = _monitoring_operation(data)
                except (ValueError, OSError, _MaintenanceLockError) as exc:
                    self._write_json(503 if isinstance(exc, _MaintenanceLockError) else 400,
                                     {"error": "monitoring_failed", "detail": str(exc)})
                    return
                _audit_event("monitoring_" + str(data.get("action", "")), actor=self.client_address[0], success=True)
                self._write_json(200, result)
            return
        if path in {"/sites/save", "/sites/delete"}:
            if self._require_auth_json():
                try:
                    data = _read_json_body(self, max_bytes=8192)
                    if path == "/sites/save":
                        _save_site(data)
                    else:
                        _delete_site(data)
                except (ValueError, OSError, _MaintenanceLockError) as exc:
                    self._write_json(503 if isinstance(exc, _MaintenanceLockError) else 400,
                                     {"error": "site_operation_failed", "detail": str(exc)})
                    return
                _audit_event("site_save" if path.endswith("save") else "site_delete", actor=self.client_address[0], success=True)
                self._write_json(200, _sites_config_payload())
            return
        if path == "/gateway-connections":
            if self._require_auth_json():
                action = 'invalid'
                try:
                    data = _read_json_body(self, max_bytes=8192)
                    action = str(data.get('action', 'invalid'))[:32]
                    result = _gateway_connection_operation(data)
                except (ValueError, OSError, _MaintenanceLockError) as exc:
                    _audit_event('gateway_connection', actor=self.client_address[0], success=False, detail={'action': action})
                    self._write_json(503 if isinstance(exc, _MaintenanceLockError) else 400,
                                     {'error': 'gateway_connection_failed', 'detail': str(exc)})
                    return
                _audit_event('gateway_connection', actor=self.client_address[0], success=True, detail={'action': action})
                self._write_json(200, result)
            return
        if path == "/request-gateways":
            if self._require_auth_json():
                self._handle_request_gateway()
            return
        if path == "/nginx-settings":
            if self._require_auth_json():
                self._handle_nginx_settings()
            return
        if path == "/certificates":
            if self._require_auth_json():
                self._handle_certificates()
            return
        if path == "/setup-password":
            self._handle_setup_password()
            return
        if path == "/login":
            self._handle_login()
            return
        if path == "/docker/action":
            if self._require_auth_json():
                self._handle_docker_action()
            return
        if path == "/docker/images/action":
            if self._require_auth_json():
                self._handle_docker_image_action()
            return
        if path == "/docker/mirrors":
            if self._require_auth_json():
                try:
                    data = _read_json_body(self, max_bytes=48 * 1024)
                    result = _docker_mirrors_manager().start(data.get("mirrors"), data.get("revision"),
                        operation=_apply_docker_mirrors,
                        audit=lambda ok: _audit_event("docker_mirrors_apply", actor=self.client_address[0], success=ok))
                    self._write_json(202, result)
                except (OSError, ValueError, RuntimeError) as exc:
                    self._write_json(400, {"error": "docker_mirrors_failed", "detail": str(exc)})
            return
        if path == "/notifications":
            if self._require_auth_json():
                self._handle_notifications_save()
            return
        if path == "/notifications/test":
            if self._require_auth_json():
                self._handle_notifications_test()
            return
        self._write_json(404, {"error": "not_found"})

    def _handle_request_gateway(self) -> None:
        action = "invalid"
        try:
            data = _read_json_body(self, max_bytes=8192)
            action = str(data.get("action", "invalid"))[:32]
            result = _request_gateway_operation(data)
        except (ValueError, OSError, _MaintenanceLockError) as exc:
            _audit_event("request_gateway", actor=self.client_address[0], success=False, detail={"action": action})
            self._write_json(503 if isinstance(exc, _MaintenanceLockError) else 400,
                             {"error": "gateway_operation_failed", "detail": str(exc)})
            return
        _audit_event("request_gateway", actor=self.client_address[0], success=True, detail={"action": action})
        self._write_json(200, result)

    def _handle_nginx_settings(self) -> None:
        try:
            data = _read_json_body(self, max_bytes=8192)
            action = data.get("action", "")
            if action == "save":
                result = _save_nginx_settings(data)
            elif action in ("plan-site", "apply-site"):
                result = _nginx_site_operation(data)
            elif action in ("plan-install", "install-local", "install-nginx"):
                result = _nginx_install_operation(data)
            elif action in ("probe", "save-upstream"):
                result = _nginx_upstream_operation(data)
            elif action == "remove-site":
                result = _remove_nginx_site(data)
            elif action == "enable-request-logging":
                result = _enable_nginx_requests()
            elif action == "disable-request-logging":
                result = _disable_nginx_requests()
            else:
                raise ValueError("未知的 Nginx 操作")
        except (ValueError, OSError, _MaintenanceLockError) as exc:
            self._write_json(503 if isinstance(exc, _MaintenanceLockError) else 400,
                             {"error": "nginx_operation_failed", "detail": str(exc)})
            return
        _audit_event("nginx_settings", actor=self.client_address[0], success=True, detail={"action": action})
        self._write_json(200, result)

    def _handle_certificates(self) -> None:
        action, project = "invalid", ""
        try:
            data = _read_json_body(self, max_bytes=300 * 1024)
            if not isinstance(data, dict):
                raise ValueError("请求必须为 JSON 对象")
            action = data.get("action", "invalid")
            project = data.get("project", "")
            _certificate_operation(data)
        except (ValueError, OSError, _MaintenanceLockError) as exc:
            _audit_event("certificate_operation", actor=self.client_address[0], success=False,
                         detail={"action": action if isinstance(action, str) and action in {"upload", "enable", "disable", "rename", "delete"} else "invalid"})
            status = 503 if isinstance(exc, _MaintenanceLockError) else 400
            detail = str(exc) if isinstance(exc, certificates.CertificateError) else "证书操作失败，请检查请求、文件权限或维护状态"
            self._write_json(status, {"error": "certificate_operation_failed", "detail": detail})
            return
        _audit_event("certificate_operation", actor=self.client_address[0], target=project, success=True,
                     detail={"action": action})
        self._write_json(200, _certificates_payload())

    def _handle_login(self) -> None:
        if not UI_PASSWORD_HASH or not UI_SESSION_SECRET:
            self._write_html(500, _render_login("面板密码尚未初始化。"))
            return
        ip = _request_client_ip(self)
        if not _reserve_login_attempt(ip):
            self._write_html(429, _render_login("登录失败次数过多，请稍后再试。"))
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0 or length > 4096:
            self._write_html(400, _render_login("请求内容无效。"))
            return
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        fields = parse_qs(body)
        password = fields.get("password", [""])[0]
        if not _verify_password(password):
            _audit_event("login", actor=ip, success=False, detail={"reason": "bad_password"})
            self._write_html(401, _render_login("密码错误。"))
            return

        _clear_login_attempts(ip)
        _audit_event("login", actor=ip, success=True)
        self.send_response(303)
        self.send_header("Location", "ui")
        self._set_session_cookie()
        self.end_headers()

    def _handle_setup_password(self) -> None:
        self._write_html(403, _render_login("网页不允许初始化管理员密码，请在服务器终端执行 set-password。"))


    def _handle_notifications_save(self) -> None:
        try:
            data = _read_json_body(self, max_bytes=96 * 1024)
        except (ValueError, json.JSONDecodeError) as exc:
            self._write_json(400, {"error": "invalid_json", "detail": str(exc)})
            return
        raw = data.get("notifications", data)
        if not isinstance(raw, dict):
            self._write_json(400, {"error": "invalid_notifications"})
            return
        try:
            _save_and_reload_notifications(raw)
        except OSError as exc:
            _log(f"notifications config save failed: {exc}")
            self._write_json(500, {"error": "config_write_failed", "detail": str(exc)})
            return
        _log(f"notifications config saved file={PROJECTS_CONFIG_FILE}")
        _audit_event("notifications_save", actor=self.client_address[0], target=str(PROJECTS_CONFIG_FILE), success=True)
        self._write_json(200, {"notifications": _notification_config_payload(include_secret=True)})

    def _handle_notifications_test(self) -> None:
        try:
            data = _read_json_body(self, max_bytes=96 * 1024)
        except (ValueError, json.JSONDecodeError) as exc:
            self._write_json(400, {"error": "invalid_json", "detail": str(exc)})
            return
        raw = data.get("notifications", data)
        if not isinstance(raw, dict):
            self._write_json(400, {"error": "invalid_notifications"})
            return
        config = _notification_config_from_raw(raw, existing=_notification_config_payload(include_secret=True))
        results = _send_notifications(
            config,
            "mini_deploy test notification",
            f"This is a test notification.\n\n- Time: {_now_text()}\n- Agent: {HOST}:{PORT}",
        )
        enabled_results = [item for item in results if item.get("enabled")]
        ok = bool(enabled_results) and all(item.get("ok") for item in enabled_results)
        _audit_event("notifications_test", actor=self.client_address[0], success=ok, detail={"results": results})
        self._write_json(200, {"ok": ok, "results": results})


    def _handle_docker_logs(self, query: dict[str, list[str]]) -> None:
        try:
            container = _validate_docker_container(query.get("container", [""])[0])
            raw_tail = query.get("tail", ["200"])[0]
            tail = _docker_log_line_limit(raw_tail)
            lines = _docker_logs(container, tail=tail)
            options = _docker_log_filter_options(query)
            filtered = _filter_docker_log_lines(lines, options)
        except ValueError as exc:
            self._write_json(400, {"error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - return Docker/runtime errors to the dashboard
            self._write_json(500, {"error": "docker_logs_failed", "detail": str(exc)})
            return
        self._write_json(200, {
            "container": container,
            "lines": filtered["lines"],
            "source_lines": len(lines),
            "matched_count": filtered["matched_count"],
            "filtered": filtered["filtered"],
            "filter_label": filtered["filter_label"],
            "context": options["context"],
        })

    def _handle_docker_logs_download(self, query: dict[str, list[str]]) -> None:
        try:
            container = _validate_docker_container(query.get("container", [""])[0])
            raw_lines = (query.get("lines", [""])[0] or "").strip().lower()
            all_lines = raw_lines in {"all", "0", "-1"} or (query.get("all", [""])[0] or "").strip() == "1"
            options = _docker_log_filter_options(query)
            has_filter = bool(options.get("keyword")) or options.get("level") in {"error", "warn"}
            if all_lines and not has_filter:
                self._write_command_download(f"docker-{container}-all.log", ["docker", "logs", container])
                return
            lines = None if all_lines else _docker_log_line_limit(raw_lines)
            text, truncated = _docker_logs_text(container, lines=lines, all_lines=all_lines)
            if has_filter:
                filtered = _filter_docker_log_lines(text.splitlines(), options)
                text = "\n".join(filtered["lines"])
        except ValueError as exc:
            self._write_json(400, {"error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - return Docker/runtime errors to the dashboard
            self._write_json(500, {"error": "docker_logs_failed", "detail": str(exc)})
            return
        filter_suffix = ""
        if has_filter:
            safe_filter = _safe_download_name(str(options.get("level") or "filter"))
            filter_suffix = f"-{safe_filter}"
        suffix = "all" if all_lines else f"{lines}-lines"
        suffix = f"{suffix}{filter_suffix}"
        self._write_text_download(f"docker-{container}-{suffix}.log", text, truncated=truncated)

    def _handle_docker_action(self) -> None:
        try:
            data = _read_json_body(self, max_bytes=4096)
            action = str(data.get("action") or "").strip()
            container = _validate_docker_container(data.get("container"))
            output = _docker_action(container, action, str(data.get("container_id") or ""))
        except (ValueError, json.JSONDecodeError) as exc:
            _audit_event("docker_action", actor=self.client_address[0], success=False, detail={"error": str(exc)})
            self._write_json(400, {"error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - normalize Docker command failures for UI
            _audit_event("docker_action", actor=self.client_address[0], success=False, detail={"error": str(exc)})
            self._write_json(500, {"error": "docker_action_failed", "detail": str(exc)})
            return
        with _system_status_lock:
            _system_status_cache.clear()
        _log(f"docker action {action} container={container} actor={self.client_address[0]}")
        _audit_event("docker_action", actor=self.client_address[0], target=container, success=True, detail={"action": action})
        self._write_json(200, {
            "ok": True,
            "action": action,
            "container": container,
            "output": output,
        })

    def _handle_docker_image_action(self) -> None:
        try:
            data = _read_json_body(self, max_bytes=4096)
            action = str(data.get("action") or "")
            reference = str(data.get("reference") or "").strip()
            if action == "remove" and data.get("confirmed") is not True:
                raise ValueError("请先确认删除镜像")
            output = _docker_image_action(action, reference)
        except ValueError as exc:
            self._write_json(400, {"error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - report bounded Docker failures
            _audit_event("docker_image_action", actor=self.client_address[0], success=False)
            self._write_json(500, {"error": "docker_image_action_failed", "detail": str(exc)})
            return
        _audit_event("docker_image_action", actor=self.client_address[0], target=reference,
                     success=True, detail={"action": action})
        self._write_json(200, {"ok": True, "output": output})


    def log_message(self, fmt: str, *args: Any) -> None:
        message = _redact_http_log_message(fmt % args)
        if (
            '"GET /status ' in message
            or '"GET /logs ' in message
            or '"GET /deploy/status ' in message
            or '"GET /deploy/logs ' in message
        ):
            return
        _log(f"http {self.client_address[0]} {message}")


def _print_password_hash() -> None:
    first = getpass.getpass("新的面板密码: ")
    second = getpass.getpass("再次输入面板密码: ")
    if not first:
        raise SystemExit("密码不能为空")
    if first != second:
        raise SystemExit("两次输入的密码不一致")
    print(f"DEPLOY_UI_PASSWORD_HASH={_hash_password(first)}")
    print(f"DEPLOY_UI_SESSION_SECRET={secrets.token_hex(32)}")


def _write_admin_password(password: str) -> None:
    if len(password) < 8:
        raise SystemExit("管理员密码至少需要 8 位")
    _upsert_env_file(AGENT_ENV_FILE, {
        "DEPLOY_UI_PASSWORD_HASH": _hash_password(password),
        "DEPLOY_UI_SESSION_SECRET": secrets.token_hex(32),
    })
    print(f"管理员密码已写入 {AGENT_ENV_FILE}；重启 {AGENT_SERVICE_NAME} 后生效。")


def _set_admin_password() -> None:
    first = getpass.getpass("新的面板密码: ")
    second = getpass.getpass("再次输入面板密码: ")
    if first != second:
        raise SystemExit("两次输入的密码不一致")
    _write_admin_password(first)


def _set_admin_password_from_stdin() -> None:
    password = sys.stdin.readline().rstrip("\r\n")
    if not password:
        raise SystemExit("标准输入中没有管理员密码")
    _write_admin_password(password)


def _reset_admin_sessions() -> None:
    _upsert_env_file(AGENT_ENV_FILE, {
        "DEPLOY_UI_SESSION_SECRET": secrets.token_hex(32),
    })
    print(f"Session Secret 已写入 {AGENT_ENV_FILE}；重启 {AGENT_SERVICE_NAME} 后所有旧 Session 将失效。")


def _print_cli_help() -> None:
    print(
        "mini_deploy agent commands:\n"
        "  set-password              set the administrator password and revoke sessions\n"
        "  set-password-stdin        read the new password from stdin (installer use)\n"
        "  reset-session             revoke all administrator sessions\n"
        "  validate-config           validate site configuration without starting HTTP\n"
        "  validate-auth             validate credentials from the process environment\n"
        "  hash-password             print credentials without writing the env file\n"
        "  admin set-password        alias for set-password\n"
        "  admin reset-session       alias for reset-session"
    )


def _is_strong_ui_session_secret(value: str) -> bool:
    secret = str(value or "").strip()
    normalized = secret.lower()
    if len(secret) < MIN_UI_SESSION_SECRET_LENGTH:
        return False
    if len(set(secret)) < MIN_UI_SESSION_SECRET_UNIQUE_CHARS:
        return False
    return not normalized.startswith((
        "change-me",
        "changeme",
        "replace-me",
        "replace-with-",
        "session-secret",
        "your-secret",
    ))


def _validate_ui_auth_config() -> None:
    if bool(UI_PASSWORD_HASH) != bool(UI_SESSION_SECRET):
        raise SystemExit(
            "管理员认证配置不完整：DEPLOY_UI_PASSWORD_HASH 和 "
            "DEPLOY_UI_SESSION_SECRET 必须同时设置；请执行 agent.py set-password。"
        )
    if not UI_PASSWORD_HASH:
        return
    if _parse_password_hash(UI_PASSWORD_HASH) is None:
        raise SystemExit(
            "DEPLOY_UI_PASSWORD_HASH 格式或强度无效；请执行 agent.py set-password 重新生成。"
        )
    if not _is_strong_ui_session_secret(UI_SESSION_SECRET):
        raise SystemExit(
            "DEPLOY_UI_SESSION_SECRET 强度不足；请执行 agent.py reset-session 重新生成。"
        )


def _validate_projects_runtime_config() -> list[Site]:
    if _RUNTIME_CONFIG_ERROR is not None:
        raise SystemExit(str(_RUNTIME_CONFIG_ERROR))
    return [site for site in PROJECTS.values() if site.enabled]


def main() -> None:
    arguments = sys.argv[1:]
    if arguments in (["help"], ["--help"], ["-h"]):
        _print_cli_help()
        return
    if arguments == ["hash-password"]:
        _print_password_hash()
        return
    if arguments in (["set-password"], ["admin", "set-password"]):
        _set_admin_password()
        return
    if arguments == ["set-password-stdin"]:
        _set_admin_password_from_stdin()
        return
    if arguments in (["reset-session"], ["admin", "reset-session"]):
        _reset_admin_sessions()
        return
    if arguments == ["validate-auth"]:
        _validate_ui_auth_config()
        return
    if arguments == ["validate-config"]:
        enabled_projects = _validate_projects_runtime_config()
        print(
            "sites config OK: "
            f"file={SITES_CONFIG_FILE} sites={len(PROJECTS)} enabled={len(enabled_projects)}"
        )
        return
    if arguments:
        raise SystemExit(f"未知命令：{' '.join(arguments)}；使用 --help 查看可用命令。")

    _validate_projects_runtime_config()

    _validate_ui_auth_config()

    try:
        _probe_maintenance_lock_for_startup()
    except _MaintenanceLockError as exc:
        raise SystemExit(f"maintenance lock is unavailable: {exc}") from exc

    _read_state()
    threading.Thread(target=_realtime_metric_sampler, name="realtime-metric-sampler", daemon=True).start()
    threading.Thread(target=_system_metric_sampler, name="system-metric-sampler", daemon=True).start()
    threading.Thread(target=_docker_log_metric_sampler, name="docker-log-metric-sampler", daemon=True).start()
    threading.Thread(target=_health_check_sampler, name="health-check-sampler", daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    _log(f"monitoring agent listening on {HOST}:{PORT}, sites={len(PROJECTS)}")
    server.serve_forever()


if __name__ == "__main__":
    main()
