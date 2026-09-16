#!/usr/bin/env python3
"""Small webhook deploy agent for low-memory servers.

The agent accepts a Gitee/GitHub/GitLab style push webhook, validates a shared
secret, filters the branch, and runs one fixed deploy script in a background
thread. It also exposes a tiny read-only dashboard protected by a local
password hash and signed cookie.
"""

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
import queue
import re
import secrets
import shlex
import shutil
import signal
import smtplib
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, replace
from email.message import EmailMessage
from functools import wraps
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote_plus, urlparse
from urllib.request import Request, urlopen

import certificates
import nginx_runtime
import nginx_install
import project_guidance

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
WEBHOOK_SECRET = os.getenv("DEPLOY_WEBHOOK_SECRET", "")
ALLOW_QUERY_WEBHOOK_TOKEN = os.getenv("DEPLOY_ALLOW_QUERY_TOKEN", "").strip().lower() in {
    "1", "true", "yes", "on", "enabled",
}
TRUST_LOOPBACK_PROXY_HEADERS = os.getenv(
    "DEPLOY_TRUST_LOOPBACK_PROXY_HEADERS",
    "",
).strip().lower() in {"1", "true", "yes", "on", "enabled"}
DEPLOY_BRANCH = os.getenv("DEPLOY_BRANCH", "main")
DEPLOY_SCRIPT = os.getenv("DEPLOY_SCRIPT", str(APP_HOME / "scripts" / "deploy.sample.sh"))
PROJECT_DIR = Path(os.getenv("PROJECT_DIR", str(APP_HOME / "workspace" / "default")))
HEALTH_URL = os.getenv("HEALTH_URL", "")
STATE_FILE = _path_from_env("DEPLOY_AGENT_STATE_FILE", "/var/lib/mini-deploy-agent/state.json")
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
DEPLOY_LOG_FILE = _path_from_env("DEPLOY_LOG_FILE", "/var/log/mini_deploy/mini_deploy.log")
AUDIT_LOG_FILE = _path_from_env("DEPLOY_AUDIT_LOG_FILE", STATE_FILE.with_name("audit.jsonl"))
PROJECT_CONFIG_BACKUP_DIR = _path_from_env(
    "DEPLOY_PROJECT_CONFIG_BACKUP_DIR",
    STATE_FILE.parent / "backups",
)
MAINTENANCE_LOCK_FILE = Path("/run/mini-deploy-agent/maintenance.lock")
MAINTENANCE_LOCK_OWNER_UID = 0
MAX_BODY_BYTES = int(os.getenv("DEPLOY_AGENT_MAX_BODY_BYTES", str(1024 * 1024)))
PROJECT_CONFIG_BACKUP_LIMIT = int(os.getenv("DEPLOY_PROJECT_CONFIG_BACKUP_LIMIT", "20"))
LOG_TAIL_LINES = int(os.getenv("DEPLOY_LOG_TAIL_LINES", "320"))
LOG_TAIL_MAX_LINES = int(os.getenv("DEPLOY_LOG_TAIL_MAX_LINES", "5000"))
LOG_DOWNLOAD_MAX_BYTES = int(os.getenv("DEPLOY_LOG_DOWNLOAD_MAX_BYTES", str(32 * 1024 * 1024)))
SYSTEM_STATUS_CACHE_SECONDS = int(os.getenv("DEPLOY_SYSTEM_STATUS_CACHE_SECONDS", "5"))
SYSTEM_METRIC_INTERVAL_SECONDS = int(os.getenv("DEPLOY_SYSTEM_METRIC_INTERVAL_SECONDS", str(30 * 60)))
SYSTEM_METRIC_MAX_POINTS = int(os.getenv("DEPLOY_SYSTEM_METRIC_MAX_POINTS", "336"))
NETWORK_MAX_MBPS = float(os.getenv("DEPLOY_NETWORK_MAX_MBPS", "100"))
DOCKER_LOG_TAIL_MAX_LINES = int(os.getenv("DEPLOY_DOCKER_LOG_TAIL_MAX_LINES", "5000"))

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
MAX_HISTORY = 60

DEPLOY_PHASE_LABELS = {
    "starting": "准备部署",
    "fetch": "拉取代码",
    "pull": "更新代码",
    "website_dependencies": "安装前端依赖",
    "website_build": "构建网站",
    "docker_build": "构建并重启容器",
    "nginx_reload": "重载 Nginx",
    "docker_ps": "检查容器",
    "health_check": "健康检查",
    "agent_restart": "重启部署 Agent",
    "canceling": "正在取消",
    "canceled": "已取消",
    "timeout": "部署超时",
    "finished": "完成",
}


@dataclass(frozen=True)
class DeployProject:
    key: str
    name: str
    template: str
    repo: str
    branch: str
    workdir: Path
    script: str
    health_url: str
    deploy_log_file: Path
    webhook_secret: str
    enabled: bool
    manual_deploy_enabled: bool
    timeout_seconds: int
    rollback_script: str
    service_name: str
    service_port: int
    start_command: str
    app_domain: str
    app_https: bool


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


_WEBHOOK_SECRET_PLACEHOLDERS = frozenset({
    "change-me",
    "changeme",
    "replace-me",
    "replace-with-a-long-random-token",
})


def _is_placeholder_webhook_secret(value: str) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized in _WEBHOOK_SECRET_PLACEHOLDERS or normalized.startswith("replace-with-")


def _is_strong_webhook_secret(value: str) -> bool:
    secret = str(value or "").strip()
    return len(secret) >= 32 and not _is_placeholder_webhook_secret(secret)


def _default_project() -> DeployProject:
    return DeployProject(
        key="default",
        name=os.getenv("DEPLOY_PROJECT_NAME", "mini_deploy"),
        template=os.getenv("DEPLOY_PROJECT_TEMPLATE", "custom"),
        repo=os.getenv("DEPLOY_REPO", ""),
        branch=DEPLOY_BRANCH,
        workdir=PROJECT_DIR,
        script=DEPLOY_SCRIPT,
        health_url=HEALTH_URL,
        deploy_log_file=DEPLOY_LOG_FILE,
        webhook_secret=WEBHOOK_SECRET,
        enabled=_bool_config(os.getenv("DEPLOY_PROJECT_ENABLED"), False),
        manual_deploy_enabled=_bool_config(os.getenv("DEPLOY_MANUAL_DEPLOY_ENABLED"), True),
        timeout_seconds=_int_config(os.getenv("DEPLOY_TIMEOUT_SECONDS"), 900),
        rollback_script=os.getenv("DEPLOY_ROLLBACK_SCRIPT", ""),
        service_name=os.getenv("DEPLOY_SERVICE_NAME", "mini-deploy-app"),
        service_port=_int_config(os.getenv("DEPLOY_SERVICE_PORT"), 8000),
        start_command=os.getenv("DEPLOY_START_COMMAND", ""),
        app_domain=os.getenv("DEPLOY_APP_DOMAIN", ""),
        app_https=_bool_config(os.getenv("DEPLOY_APP_HTTPS"), False),
    )


def _project_from_config(raw: dict[str, Any], fallback_key: str) -> DeployProject:
    key = _safe_project_key(str(raw.get("key") or fallback_key))
    branch = str(raw.get("branch") or DEPLOY_BRANCH)
    workdir = Path(str(raw.get("workdir") or raw.get("project_dir") or PROJECT_DIR))
    script = str(raw.get("script") or DEPLOY_SCRIPT)
    service_name = _safe_project_key(str(raw.get("service_name") or raw.get("service") or key))
    return DeployProject(
        key=key,
        name=str(raw.get("name") or key),
        template=str(raw.get("template") or raw.get("type") or "custom"),
        repo=str(raw.get("repo") or raw.get("repository") or ""),
        branch=branch,
        workdir=workdir,
        script=script,
        health_url=str(raw.get("health_url") or HEALTH_URL),
        deploy_log_file=Path(str(raw.get("deploy_log_file") or raw.get("log_file") or DEPLOY_LOG_FILE)),
        webhook_secret=str(raw.get("webhook_secret") or raw.get("secret") or ""),
        enabled=_bool_config(raw.get("enabled"), True),
        manual_deploy_enabled=_bool_config(raw.get("manual_deploy_enabled"), True),
        timeout_seconds=_int_config(raw.get("timeout_seconds"), _int_config(os.getenv("DEPLOY_TIMEOUT_SECONDS"), 900)),
        rollback_script=str(raw.get("rollback_script") or ""),
        service_name=service_name,
        service_port=_int_config(raw.get("service_port") or raw.get("port"), 8000),
        start_command=str(raw.get("start_command") or ""),
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


def _load_projects() -> dict[str, DeployProject]:
    config_payload = _read_selected_projects_config()
    if config_payload is not None:
        try:
            raw = json.loads(config_payload.decode("utf-8"))
            items = raw.get("projects") if isinstance(raw, dict) else raw
            if not isinstance(items, list):
                raise ValueError("projects must be a list")
            projects = {}
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    raise ValueError(f"project at index {index} must be an object")
                project = _project_from_config(item, f"project-{index + 1}")
                if project.key in projects:
                    raise ValueError(f"duplicate project key: {project.key}")
                projects[project.key] = project
            return projects
        except Exception as exc:
            raise RuntimeError(f"projects config is invalid: {PROJECTS_CONFIG_FILE}: {exc}") from exc
    project = _default_project()
    return {project.key: project}


_RUNTIME_CONFIG_ERROR: RuntimeError | None = None
try:
    PROJECTS = _load_projects()
except RuntimeError as exc:
    # Administrative CLI commands are break-glass operations. Keep the module
    # importable when projects.json is damaged, then fail closed only when the
    # HTTP service itself is started.
    PROJECTS = {}
    _RUNTIME_CONFIG_ERROR = exc
DEFAULT_PROJECT_KEY = next(iter(PROJECTS), "")

_jobs: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=20)
_jobs_admin_lock = threading.Lock()
_projects_lock = threading.Lock()
_config_transaction_lock = threading.RLock()
_running_lock = threading.Lock()
_state_lock = threading.Lock()
_state_write_lock = threading.Lock()
_audit_lock = threading.Lock()
_deploy_process_lock = threading.Lock()
_deploy_process: subprocess.Popen[str] | None = None
_deploy_worker_thread: threading.Thread | None = None
_cancel_requested: dict[str, Any] | None = None
_state: dict[str, Any] = {
    "running": False,
    "queue_size": 0,
    "last_webhook_at": None,
    "last_ignored_webhook": None,
    "current_deploy": None,
    "last_deploy": None,
    "history": [],
    "system_metrics": [],
}
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
_JOB_MAINTENANCE_LOCK_KEY = "_maintenance_lock_fd"
_JOB_DEPLOY_LIFECYCLE_OWNER_KEY = "_deploy_lifecycle_owner"
_JOB_INTERNAL_KEYS = frozenset({_JOB_MAINTENANCE_LOCK_KEY, _JOB_DEPLOY_LIFECYCLE_OWNER_KEY})
_ORPHAN_REAPER_WAIT_SECONDS = 60.0
_ORPHAN_REAPER_LOG_SECONDS = 300.0
_DEPLOY_LOCK_WAIT_SECONDS = 0.2
_DEPLOY_LOCK_WAIT_LOG_SECONDS = 30.0


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
        # Logging is diagnostic and must never change deployment lifecycle
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


def _valid_signature(headers: Any, body: bytes, query: dict[str, list[str]]) -> bool:
    if not _is_strong_webhook_secret(WEBHOOK_SECRET):
        return False

    token_candidates = [
        headers.get("X-Gitee-Token", ""),
        headers.get("X-Gitlab-Token", ""),
        headers.get("X-Webhook-Token", ""),
        headers.get("X-Hook-Token", ""),
    ]
    if ALLOW_QUERY_WEBHOOK_TOKEN:
        token_candidates.extend([
            query.get("token", [""])[0],
            query.get("secret", [""])[0],
        ])
    if any(token and _constant_time_equal(token, WEBHOOK_SECRET) for token in token_candidates):
        return True

    signature = headers.get("X-Hub-Signature-256", "")
    if signature.startswith("sha256="):
        digest = hmac.new(WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return _constant_time_equal(signature, f"sha256={digest}")

    return False


def _extract_ref(payload: dict[str, Any]) -> str:
    ref = payload.get("ref")
    if isinstance(ref, str):
        return ref
    return ""


def _extract_commit(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if isinstance(value, str):
        return value
    if key == "after":
        head = payload.get("head_commit")
        if isinstance(head, dict) and isinstance(head.get("id"), str):
            return head["id"]
    return ""


def _extract_changed_files(payload: dict[str, Any], limit: int = 80) -> tuple[list[str], int]:
    files: list[str] = []
    seen: set[str] = set()
    commits = payload.get("commits")
    commit_items = commits if isinstance(commits, list) else []
    head = payload.get("head_commit")
    if isinstance(head, dict):
        commit_items = [head, *commit_items]
    for commit in commit_items:
        if not isinstance(commit, dict):
            continue
        for key in ("added", "modified", "removed"):
            values = commit.get(key)
            if not isinstance(values, list):
                continue
            for value in values:
                text = str(value or "").strip()
                if text and text not in seen:
                    seen.add(text)
                    files.append(text)
    return files[:limit], len(files)


def _extract_commit_details(payload: dict[str, Any]) -> dict[str, Any]:
    head = payload.get("head_commit")
    changed_files, changed_file_count = _extract_changed_files(payload)
    if not isinstance(head, dict):
        return {
            "commit_message": "",
            "commit_author": "",
            "changed_files": changed_files,
            "changed_file_count": changed_file_count,
        }
    message = head.get("message") if isinstance(head.get("message"), str) else ""
    author = head.get("author")
    author_name = ""
    if isinstance(author, dict):
        for key in ("name", "username", "email"):
            value = author.get(key)
            if isinstance(value, str) and value:
                author_name = value
                break
    return {
        "commit_message": message,
        "commit_author": author_name,
        "changed_files": changed_files,
        "changed_file_count": changed_file_count,
    }


def _repo_candidates(payload: dict[str, Any]) -> set[str]:
    candidates: set[str] = set()
    repository = payload.get("repository")
    if isinstance(repository, dict):
        for key in ("full_name", "path_with_namespace", "name", "url", "html_url", "ssh_url", "git_ssh_url", "clone_url", "git_http_url"):
            value = repository.get(key)
            if isinstance(value, str) and value:
                candidates.add(value)
    project = payload.get("project")
    if isinstance(project, dict):
        for key in ("path_with_namespace", "web_url", "ssh_url_to_repo", "http_url_to_repo"):
            value = project.get(key)
            if isinstance(value, str) and value:
                candidates.add(value)
    return candidates


def _normalize_repo(value: str) -> str:
    text = value.strip().lower()
    for prefix in ("git@", "https://", "http://", "ssh://"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    text = text.replace(":", "/")
    if text.endswith(".git"):
        text = text[:-4]
    return text.strip("/")


def _project_matches_repo(project: DeployProject, candidates: set[str]) -> bool:
    if not project.repo:
        return len(PROJECTS) == 1
    expected = _normalize_repo(project.repo)
    for candidate in candidates:
        normalized = _normalize_repo(candidate)
        if normalized == expected or expected.endswith(f"/{normalized}") or normalized.endswith(f"/{expected}"):
            return True
    return False


def _project_for_webhook(payload: dict[str, Any], query: dict[str, list[str]]) -> DeployProject | None:
    requested = query.get("project", [""])[0] or query.get("project_key", [""])[0]
    if requested:
        return PROJECTS.get(_safe_project_key(requested))

    candidates = _repo_candidates(payload)
    for project in PROJECTS.values():
        if _project_matches_repo(project, candidates):
            return project
    if len(PROJECTS) == 1:
        return PROJECTS.get(DEFAULT_PROJECT_KEY)
    return None


def _project_for_manual(query: dict[str, list[str]]) -> DeployProject | None:
    requested = query.get("project", [""])[0] or query.get("project_key", [""])[0]
    if requested:
        return PROJECTS.get(_safe_project_key(requested))
    return PROJECTS.get(DEFAULT_PROJECT_KEY)


def _valid_signature_for_project(project: DeployProject, headers: Any, body: bytes, query: dict[str, list[str]]) -> bool:
    secret = project.webhook_secret
    if not _is_strong_webhook_secret(secret):
        return False

    token_candidates = [
        headers.get("X-Gitee-Token", ""),
        headers.get("X-Gitlab-Token", ""),
        headers.get("X-Webhook-Token", ""),
        headers.get("X-Hook-Token", ""),
    ]
    if ALLOW_QUERY_WEBHOOK_TOKEN:
        token_candidates.extend([
            query.get("token", [""])[0],
            query.get("secret", [""])[0],
        ])
    if any(token and _constant_time_equal(token, secret) for token in token_candidates):
        return True

    signature = headers.get("X-Hub-Signature-256", "")
    if signature.startswith("sha256="):
        digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return _constant_time_equal(signature, f"sha256={digest}")

    return False


def _clean_config_text(value: Any, default: str = "", max_length: int = 1024) -> str:
    text = str(value if value is not None else default).strip()
    return text[:max_length]


def _load_config_file() -> dict[str, Any]:
    config_payload = _read_selected_projects_config()
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


def _project_to_config(project: DeployProject, include_secret: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {
        "key": project.key,
        "name": project.name,
        "template": project.template,
        "repo": project.repo,
        "branch": project.branch,
        "workdir": str(project.workdir),
        "script": project.script,
        "rollback_script": project.rollback_script,
        "health_url": project.health_url,
        "deploy_log_file": str(project.deploy_log_file),
        "enabled": project.enabled,
        "manual_deploy_enabled": project.manual_deploy_enabled,
        "timeout_seconds": project.timeout_seconds,
        "service_name": project.service_name,
        "service_port": project.service_port,
        "start_command": project.start_command,
        "app_domain": project.app_domain,
        "app_https": project.app_https,
    }
    if include_secret:
        data["webhook_secret"] = project.webhook_secret
    return data


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


def _projects_config_payload(include_secret: bool = True) -> dict[str, Any]:
    with _config_transaction_lock:
        return {
            "config_file": str(PROJECTS_CONFIG_FILE),
            "config_exists": PROJECTS_CONFIG_FILE.exists(),
            "projects": _projects_config_items(include_secret=include_secret),
            "notifications": _notification_config_payload(include_secret=include_secret),
        }


def _project_from_form(raw: dict[str, Any], existing: DeployProject | None = None) -> DeployProject:
    fallback_key = existing.key if existing else raw.get("name") or "project"
    key = _safe_project_key(_clean_config_text(raw.get("key") or fallback_key, max_length=80))
    name = _clean_config_text(raw.get("name"), existing.name if existing else key, max_length=120) or key
    template = _clean_config_text(raw.get("template") or raw.get("type"), existing.template if existing else "custom", max_length=40) or "custom"
    repo = _clean_config_text(raw.get("repo") or raw.get("repository"), existing.repo if existing else "", max_length=512)
    branch = _clean_config_text(raw.get("branch"), existing.branch if existing else DEPLOY_BRANCH, max_length=120) or DEPLOY_BRANCH
    workdir_text = _clean_config_text(raw.get("workdir") or raw.get("project_dir"), str(existing.workdir) if existing else str(PROJECT_DIR), max_length=512)
    script = _clean_config_text(raw.get("script"), existing.script if existing else DEPLOY_SCRIPT, max_length=512) or DEPLOY_SCRIPT
    health_url = _clean_config_text(raw.get("health_url"), existing.health_url if existing else HEALTH_URL, max_length=512)
    log_file = _clean_config_text(
        raw.get("deploy_log_file") or raw.get("log_file"),
        str(existing.deploy_log_file) if existing else str(DEPLOY_LOG_FILE),
        max_length=512,
    )
    service_name = _clean_config_text(
        raw.get("service_name") or raw.get("service"),
        existing.service_name if existing else key,
        max_length=80,
    ) or key
    start_command = _clean_config_text(
        raw.get("start_command"),
        existing.start_command if existing else "",
        max_length=1024,
    )
    app_https_raw = raw["app_https"] if "app_https" in raw else raw.get("https")
    app_domain = _clean_config_text(
        raw.get("app_domain") or raw.get("domain"),
        existing.app_domain if existing else "",
        max_length=253,
    )
    webhook_secret = _clean_config_text(raw.get("webhook_secret") or raw.get("secret"), existing.webhook_secret if existing else "", max_length=256)
    if not webhook_secret or _is_placeholder_webhook_secret(webhook_secret):
        webhook_secret = secrets.token_hex(32)
    elif not _is_strong_webhook_secret(webhook_secret):
        raise ValueError("WebHook Token/Secret 至少需要 32 位")
    return DeployProject(
        key=key,
        name=name,
        template=template,
        repo=repo,
        branch=branch,
        workdir=Path(workdir_text or str(PROJECT_DIR)),
        script=script,
        health_url=health_url,
        deploy_log_file=Path(log_file or str(DEPLOY_LOG_FILE)),
        webhook_secret=webhook_secret,
        enabled=_bool_config(raw.get("enabled"), existing.enabled if existing else True),
        manual_deploy_enabled=_bool_config(
            raw.get("manual_deploy_enabled"),
            existing.manual_deploy_enabled if existing else True,
        ),
        timeout_seconds=_int_config(raw.get("timeout_seconds"), existing.timeout_seconds if existing else 900),
        rollback_script=_clean_config_text(raw.get("rollback_script"), existing.rollback_script if existing else "", max_length=512),
        service_name=_safe_project_key(service_name),
        service_port=_int_config(raw.get("service_port") or raw.get("port"), existing.service_port if existing else 8000),
        start_command=start_command,
        app_domain=_normalize_domain(app_domain),
        app_https=_bool_config(app_https_raw, existing.app_https if existing else False),
    )


def _backup_projects_config() -> str:
    if not PROJECTS_CONFIG_FILE.exists():
        return ""
    backup_dir = PROJECT_CONFIG_BACKUP_DIR
    backup_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    descriptor, raw_backup_path = tempfile.mkstemp(
        prefix=f"{PROJECTS_CONFIG_FILE.name}.{stamp}.",
        suffix=".bak",
        dir=backup_dir,
    )
    os.close(descriptor)
    backup_path = Path(raw_backup_path)
    try:
        shutil.copyfile(PROJECTS_CONFIG_FILE, backup_path)
        os.chmod(backup_path, 0o600)
    except Exception:
        backup_path.unlink(missing_ok=True)
        raise
    backups = sorted(
        backup_dir.glob(f"{PROJECTS_CONFIG_FILE.name}.*.bak"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for old in backups[max(PROJECT_CONFIG_BACKUP_LIMIT, 1):]:
        try:
            old.unlink()
        except OSError:
            pass
    return str(backup_path)


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
def _write_projects_config(projects: dict[str, DeployProject], notifications: dict[str, Any] | None = None) -> None:
    backup_path = _backup_projects_config()
    PROJECTS_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "projects": [_project_to_config(project, include_secret=True) for project in projects.values()],
        "notifications": _notification_config_from_raw(
            notifications if notifications is not None else _notification_config_payload(include_secret=True),
        ),
    }
    _atomic_write_private_text(
        PROJECTS_CONFIG_FILE,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )
    if backup_path:
        _log(f"projects config backup created file={backup_path}")


def _replace_projects(projects: dict[str, DeployProject]) -> None:
    global PROJECTS, DEFAULT_PROJECT_KEY
    with _projects_lock:
        PROJECTS = dict(projects)
        DEFAULT_PROJECT_KEY = next(iter(PROJECTS), "")


def _save_and_reload_projects(projects: dict[str, DeployProject]) -> None:
    with _config_transaction_lock:
        _write_projects_config(projects)
        _replace_projects(projects)


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


class _ProjectKeyExistsError(ValueError):
    pass


class _ProjectNotFoundError(ValueError):
    pass


class _ProjectRunningError(ValueError):
    pass


def _save_project_transaction(raw_project: dict[str, Any], original_key: str = "") -> DeployProject:
    with _config_transaction_lock:
        projects = dict(PROJECTS)
        existing = projects.get(original_key) if original_key else None
        project = _project_from_form(raw_project, existing=existing)
        if existing and (STATE_FILE.parent / "certificates" / existing.key).exists():
            if (project.key, project.app_domain, project.service_port) != (existing.key, existing.app_domain, existing.service_port):
                raise ValueError("请先在证书管理中停用并删除证书，再修改项目标识、域名或端口")
        if existing and (project.key, project.app_domain, project.service_port) != (existing.key, existing.app_domain, existing.service_port):
            if _nginx_project_has_site(existing):
                raise ValueError("请先在 Nginx 接入中移除域名入口，再修改项目标识、域名或端口")
        if original_key and original_key != project.key:
            projects.pop(original_key, None)
        if project.key in projects and original_key != project.key:
            raise _ProjectKeyExistsError(project.key)
        projects[project.key] = project
        _save_and_reload_projects(projects)
        return project


def _delete_project_transaction(key: str) -> None:
    with _config_transaction_lock:
        projects = dict(PROJECTS)
        if key not in projects:
            raise _ProjectNotFoundError(key)
        if (STATE_FILE.parent / "certificates" / key).exists():
            raise certificates.CertificateError("请先在证书管理中停用并删除该项目的证书")
        if _nginx_project_has_site(projects[key]):
            raise certificates.CertificateError("请先在 Nginx 接入中移除该项目的域名入口")
        with _state_lock:
            current = _state.get("current_deploy")
        if (
            isinstance(current, dict)
            and (current.get("project_key") or "default") == key
            and current.get("status") == "running"
        ):
            raise _ProjectRunningError(key)
        if any(job.get("project_key") == key for job in _queued_jobs_snapshot()):
            raise _ProjectRunningError(key)
        projects.pop(key)
        _save_and_reload_projects(projects)


def _reset_project_secret_transaction(key: str) -> DeployProject:
    with _config_transaction_lock:
        projects = dict(PROJECTS)
        project = projects.get(key)
        if not project:
            raise _ProjectNotFoundError(key)
        projects[key] = DeployProject(
            key=project.key,
            name=project.name,
            template=project.template,
            repo=project.repo,
            branch=project.branch,
            workdir=project.workdir,
            script=project.script,
            health_url=project.health_url,
            deploy_log_file=project.deploy_log_file,
            webhook_secret=secrets.token_hex(32),
            enabled=project.enabled,
            manual_deploy_enabled=project.manual_deploy_enabled,
            timeout_seconds=project.timeout_seconds,
            rollback_script=project.rollback_script,
            service_name=project.service_name,
            service_port=project.service_port,
            start_command=project.start_command,
            app_domain=project.app_domain,
            app_https=project.app_https,
        )
        _save_and_reload_projects(projects)
        return projects[key]


def _initialize_projects_config_transaction() -> None:
    with _config_transaction_lock:
        _write_projects_config(PROJECTS, notifications=_notification_config_payload(include_secret=True))


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
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=8) as smtp:
            if _bool_config(config.get("use_starttls"), False):
                smtp.starttls(context=context)
            if username:
                smtp.login(username, password)
            smtp.send_message(message)
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


def _send_notifications_async(title: str, body: str) -> None:
    config = _notification_config_payload(include_secret=True)
    if not _any_notification_enabled(config):
        return

    def worker() -> None:
        for result in _send_notifications(config, title, body):
            if result.get("enabled") and not result.get("ok"):
                _log(f"notification failed channel={result.get('channel')} detail={result.get('detail')}")

    threading.Thread(target=worker, name="deploy-notification", daemon=True).start()


def _notify_deploy_finished(entry: dict[str, Any]) -> None:
    status = str(entry.get("status") or "")
    project_name = str(entry.get("project_name") or entry.get("project_key") or "project")
    title = f"mini_deploy deployment {'succeeded' if status == 'success' else 'failed'}: {project_name}"
    lines = [
        f"- Status: {status}",
        f"- Project: {project_name}",
        f"- Commit: {_short_sha(entry.get('after'))}",
        f"- Duration: {entry.get('duration_seconds', '-')}s",
        f"- Actor: {entry.get('actor') or entry.get('source') or '-'}",
    ]
    message = str(entry.get("commit_message") or "").strip()
    if message:
        lines.append(f"- Message: {message}")
    _send_notifications_async(title, "\n".join(lines))


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
    global _state
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    if isinstance(data, dict):
        with _state_lock:
            _state.update(data)
            _state["running"] = False
            _state["current_deploy"] = None
            _state["queue_size"] = 0


@_maintenance_shared_operation
def _write_state() -> None:
    try:
        with _state_write_lock:
            with _state_lock:
                data = dict(_state)
                data["queue_size"] = _jobs.qsize()
            _atomic_write_private_text(
                STATE_FILE,
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            )
    except OSError as exc:
        _log(f"state write failed: {exc}")


def _update_state(**changes: Any) -> None:
    with _state_lock:
        _state.update(changes)
        _state["queue_size"] = _jobs.qsize()
    _write_state()


def _phase_label(phase: str | None) -> str:
    return DEPLOY_PHASE_LABELS.get(str(phase or "").strip(), str(phase or "") or "-")


def _update_current_deploy(**changes: Any) -> None:
    with _state_lock:
        current = _state.get("current_deploy")
        if not isinstance(current, dict) or current.get("status") != "running":
            return
        now = time.time()
        old_phase = current.get("phase")
        new_phase = changes.get("phase", old_phase)
        if new_phase and old_phase and new_phase != old_phase:
            phase_started_ts = current.get("phase_started_ts")
            if isinstance(phase_started_ts, (int, float)):
                durations = list(current.get("phase_durations") or [])
                durations.append({
                    "phase": old_phase,
                    "label": _phase_label(str(old_phase)),
                    "duration_seconds": round(max(now - float(phase_started_ts), 0), 1),
                })
                current["phase_durations"] = durations
            current["phase_started_ts"] = now
        current.update(changes)
        if current.get("phase"):
            current["phase_label"] = _phase_label(current.get("phase"))
        started_ts = current.get("started_ts")
        if isinstance(started_ts, (int, float)):
            current["duration_seconds"] = round(max(time.time() - float(started_ts), 0), 1)
        _state["queue_size"] = _jobs.qsize()
    _write_state()


def _close_current_phase(current: dict[str, Any], finished_ts: float) -> None:
    phase = current.get("phase")
    phase_started_ts = current.get("phase_started_ts")
    if not phase or not isinstance(phase_started_ts, (int, float)):
        return
    durations = list(current.get("phase_durations") or [])
    if durations and durations[-1].get("phase") == phase:
        return
    durations.append({
        "phase": phase,
        "label": _phase_label(str(phase)),
        "duration_seconds": round(max(finished_ts - float(phase_started_ts), 0), 1),
    })
    current["phase_durations"] = durations


def _phase_from_deploy_line(line: str) -> tuple[str, str] | None:
    text = str(line or "").strip()
    marker = "phase="
    if marker not in text:
        return None
    phase = text.split(marker, 1)[1].split(None, 1)[0].strip()
    if not phase:
        return None
    return phase, text


def _build_progress_detail(line: str) -> str | None:
    """Map stable BuildKit/Compose output to a concise dashboard detail."""
    text = str(line or "").strip().lower()
    if not text:
        return None
    if "apt-get install" in text:
        return "安装系统依赖"
    if "pip install" in text or "-r requirements.txt" in text:
        return "安装 Python 依赖"
    if "exporting layers" in text:
        return "导出镜像层"
    if "exporting manifest" in text or "exporting config" in text:
        return "写入镜像元数据"
    if "unpacking to docker.io" in text or "unpacking to " in text:
        return "写入 Docker 镜像"
    if "container " in text and (" recreat" in text or " restart" in text):
        return "重建容器"
    if "container " in text and (" starting" in text or " started" in text):
        return "启动容器"
    if "container " in text and (" running" in text or " healthy" in text):
        return "容器已启动"
    return None


def _append_history(entry: dict[str, Any]) -> None:
    with _state_lock:
        history = list(_state.get("history") or [])
        history.insert(0, entry)
        _state["history"] = history[:MAX_HISTORY]
        _state["last_deploy"] = entry
        _state["running"] = False
        _state["current_deploy"] = None
        _state["queue_size"] = _jobs.qsize()
    _write_state()


def _run_git(args: list[str], project: DeployProject | None = None) -> str:
    target = project or PROJECTS.get(DEFAULT_PROJECT_KEY)
    if target is None:
        return ""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(target.workdir),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
    except Exception:
        return ""
    return (proc.stdout or "").strip()


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
    return cleaned.strip("-") or "deploy-log"


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


def _doctor_item(key: str, title: str, ok: bool, message: str, level: str | None = None) -> dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "ok": ok,
        "level": level or ("ok" if ok else "fail"),
        "message": message,
    }


def _project_doctor(project: DeployProject) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    checks.append(_doctor_item(
        "workdir",
        "项目目录",
        project.workdir.is_dir(),
        f"{project.workdir} {'存在' if project.workdir.is_dir() else '不存在，请先 clone 项目或执行面板生成的服务器指令'}",
    ))

    git_dir = project.workdir / ".git"
    checks.append(_doctor_item(
        "git",
        "Git 仓库",
        git_dir.is_dir(),
        f"{git_dir} {'存在' if git_dir.is_dir() else '不存在，WebHook 部署前需要先 clone 仓库'}",
    ))

    script_path = _project_script_path(project)
    script_exists = script_path.is_file()
    script_executable = os.access(script_path, os.X_OK) if script_exists else False
    checks.append(_doctor_item(
        "script",
        "部署脚本",
        script_exists,
        f"{script_path} {'存在' if script_exists else '不存在，请先创建 deploy.sh'}",
    ))
    checks.append(_doctor_item(
        "script_executable",
        "脚本权限",
        script_executable,
        "脚本可执行" if script_executable else f"需要执行 chmod +x {script_path}",
        level="warn" if script_exists and not script_executable else None,
    ))

    command_map = {
        "docker": ["docker"],
        "node": ["node", "npm"],
        "python": ["python3"],
        "java": ["java"],
        "go": ["go"],
        "static": ["node", "npm"],
        "custom": [],
    }
    for command in command_map.get(project.template, []):
        found = shutil.which(command)
        checks.append(_doctor_item(
            f"cmd_{command}",
            f"运行环境：{command}",
            bool(found),
            found or f"未找到 {command} 命令，请先安装或改用自定义脚本",
        ))

    if project.template in {"python", "go", "java"}:
        service_path = Path("/etc/systemd/system") / f"{project.service_name}.service"
        checks.append(_doctor_item(
            "systemd_service",
            "systemd 服务",
            service_path.is_file(),
            f"{service_path} {'存在' if service_path.is_file() else '不存在，可点击自动初始化生成初版 service'}",
            level="warn" if not service_path.is_file() else None,
        ))

    if project.health_url:
        code, output = _run_command(["curl", "-fsS", "--max-time", "5", project.health_url], timeout=6.0)
        checks.append(_doctor_item(
            "health_url",
            "健康检查",
            code == 0,
            "健康检查可访问" if code == 0 else (output or "健康检查失败，项目未启动时这是正常的"),
            level="warn" if code != 0 else None,
        ))
    else:
        checks.append(_doctor_item(
            "health_url",
            "健康检查",
            False,
            "未填写 health_url，可以稍后补充",
            level="warn",
        ))

    ok = all(item["ok"] or item.get("level") == "warn" for item in checks)
    return {
        "project": project.key,
        "ok": ok,
        "checks": checks,
    }


def _template_deploy_steps(project: DeployProject) -> list[str]:
    service = project.service_name or project.key
    health_line = [f"curl -fsS --max-time 10 {shlex.quote(project.health_url)}"] if project.health_url else [
        "# 可选：在项目配置里填写 health_url 后，这里会自动检查",
    ]
    template = project.template
    if template == "docker":
        return ["docker compose up -d --build", "docker compose ps", *health_line]
    if template == "node":
        return [
            "if command -v pnpm >/dev/null 2>&1; then pnpm install --frozen-lockfile; else npm ci; fi",
            "if [ -f package.json ]; then npm run build --if-present; fi",
            f"pm2 restart {shlex.quote(service)} || pm2 start npm --name {shlex.quote(service)} -- start",
            *health_line,
        ]
    if template == "python":
        return [
            "python3 -m venv .venv",
            ". .venv/bin/activate",
            "pip install --upgrade pip",
            "pip install -r requirements.txt",
            f"systemctl restart {shlex.quote(service)}",
            *health_line,
        ]
    if template == "java":
        return [
            "if [ -f ./gradlew ]; then bash ./gradlew clean build -x test; jar_dir=build/libs; elif [ -f ./mvnw ]; then bash ./mvnw clean package -DskipTests; jar_dir=target; else mvn clean package -DskipTests; jar_dir=target; fi",
            "mkdir -p target/deploy",
            "mapfile -t jars < <(find \"$jar_dir\" -maxdepth 1 -type f -name '*.jar' ! -name '*sources.jar' ! -name '*javadoc.jar' ! -name '*-plain.jar')",
            'if [ "${#jars[@]}" -ne 1 ]; then echo "需要唯一的可执行 JAR，请检查构建产物"; exit 1; fi',
            'cp "${jars[0]}" target/deploy/app.jar',
            f"systemctl restart {shlex.quote(service)}",
            *health_line,
        ]
    if template == "go":
        return [
            "mkdir -p bin",
            "if [ -d cmd/server ]; then go build -o bin/app ./cmd/server; else go build -o bin/app .; fi",
            f"systemctl restart {shlex.quote(service)}",
            *health_line,
        ]
    if template == "static":
        return [
            "if command -v pnpm >/dev/null 2>&1; then pnpm install --frozen-lockfile; else npm ci; fi",
            "npm run build",
            "# TODO: 把 dist/ 同步到你的 Nginx 静态目录，例如：",
            f"# rsync -a --delete dist/ /var/www/{service}/",
            *health_line,
        ]
    return ["# TODO: 在这里填写项目自己的部署步骤", "# 示例：docker compose up -d --build", *health_line]


def _deploy_script_text(project: DeployProject) -> str:
    lines = [
        "#!/usr/bin/env bash",
        "set -Eeuo pipefail",
        "",
        f"cd {shlex.quote(str(project.workdir))}",
        f"BRANCH=${{DEPLOY_BRANCH:-{shlex.quote(project.branch)}}}",
        f"LOG_FILE=${{DEPLOY_LOG_FILE:-{shlex.quote(str(project.deploy_log_file))}}}",
        "",
        'mkdir -p "$(dirname "$LOG_FILE")"',
        "log() {",
        "  printf '%s %s\\n' \"$(date '+%Y-%m-%d %H:%M:%S')\" \"$*\" | tee -a \"$LOG_FILE\"",
        "}",
        "",
        'log "phase=starting prepare deploy workspace"',
        'log "phase=fetch git fetch origin $BRANCH"',
        'git fetch origin "$BRANCH"',
        'log "phase=pull git checkout and fast-forward pull"',
        'git checkout "$BRANCH"',
        'git pull --ff-only origin "$BRANCH"',
        "",
        'log "phase=docker_build build or restart service"',
        *_template_deploy_steps(project),
        "",
        'log "phase=finished deploy completed"',
    ]
    return "\n".join(lines) + "\n"


def _default_start_command(project: DeployProject) -> str:
    if project.start_command:
        return project.start_command
    port = project.service_port or 8000
    if project.template == "python":
        return f"{project.workdir}/.venv/bin/uvicorn main:app --host 127.0.0.1 --port {port}"
    if project.template == "go":
        return f"{project.workdir}/bin/app"
    if project.template == "java":
        return f"/usr/bin/java -jar {project.workdir}/target/deploy/app.jar --server.port={port}"
    return project.start_command


def _systemd_service_text(project: DeployProject) -> str:
    command = _default_start_command(project)
    if not command:
        raise ValueError("start_command_required")
    return "\n".join([
        "[Unit]",
        f"Description={project.name} service",
        "After=network.target",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={project.workdir}",
        f"ExecStart={command}",
        "Restart=always",
        "RestartSec=3",
        "KillSignal=SIGINT",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ])


def _ssh_public_keys() -> list[str]:
    keys: list[str] = []
    for name in ("id_ed25519.pub", "id_rsa.pub"):
        path = Path.home() / ".ssh" / name
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            keys.append(text)
    return keys


def _bootstrap_result(step: str, ok: bool, detail: str, output: str = "") -> dict[str, Any]:
    return {"step": step, "ok": ok, "detail": detail, "output": output,
            "diagnosis": [] if ok else project_guidance.diagnose(output or detail)}


def _project_preview(project: DeployProject) -> dict[str, Any]:
    if project.template not in {"docker", "python", "go", "java", "node", "static", "custom"}:
        raise ValueError("请选择支持的项目类型")
    if not 1 <= project.service_port <= 65535:
        raise ValueError("业务端口必须在 1 到 65535 之间")
    if not project.workdir.is_absolute():
        raise ValueError("服务器目录必须是绝对路径")
    files = []
    paths = [("deploy.sh", _project_script_path(project), _deploy_script_text(project))]
    if project.template in {"python", "go", "java"}:
        paths.append(("systemd", Path("/etc/systemd/system") / f"{project.service_name}.service",
                      _systemd_service_text(project)))
    for kind, path, generated in paths:
        if path.is_symlink():
            raise ValueError(f"文件是符号链接，请人工核对：{path}")
        exists = path.exists()
        if exists:
            if not path.is_file() or path.stat().st_size > 128 * 1024:
                raise ValueError(f"文件不是普通小型配置文件，请人工核对：{path}")
            content = path.read_text(encoding="utf-8", errors="replace")
        else:
            content = generated
        files.append({"kind": kind, "path": str(path), "exists": exists, "content": content})
    return {"files": files}


def _run_bootstrap_command(command: list[str], *, step: str, timeout: float = 60.0) -> dict[str, Any]:
    code, output = _run_command(command, timeout=timeout)
    return _bootstrap_result(step, code == 0, "ok" if code == 0 else f"exit code {code}", output)


@_maintenance_shared_operation
def _bootstrap_project(project: DeployProject, *, write_service: bool = True) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    service_written = False
    script_path = _project_script_path(project)
    try:
        project_guidance.validate_repository(project.repo, project.branch)
        _project_preview(project)
    except (ValueError, OSError) as exc:
        return {"ok": False, "project": project.key,
                "results": [_bootstrap_result("configuration", False, str(exc))]}

    if not project.repo:
        return {
            "ok": False,
            "project": project.key,
            "results": [_bootstrap_result("repo", False, "仓库地址为空，请先填写 repo")],
            "ssh_public_keys": _ssh_public_keys(),
        }

    if not shutil.which("git"):
        return {
            "ok": False,
            "project": project.key,
            "results": [_bootstrap_result("git", False, "服务器未安装 git，请先安装 git")],
            "ssh_public_keys": _ssh_public_keys(),
        }

    ls_remote = _run_bootstrap_command(["git", "ls-remote", project.repo, "HEAD"], step="repo_access", timeout=30.0)
    results.append(ls_remote)
    if not ls_remote["ok"]:
        return {
            "ok": False,
            "project": project.key,
            "results": results,
            "ssh_public_keys": _ssh_public_keys(),
            "message": "服务器无法访问仓库，请查看下方原因和建议。",
        }

    try:
        project.workdir.parent.mkdir(parents=True, exist_ok=True)
        if (project.workdir / ".git").is_dir():
            for step, args in (("git_fetch", ["fetch", "origin", project.branch]),
                               ("git_checkout", ["checkout", project.branch]),
                               ("git_pull", ["pull", "--ff-only", "origin", project.branch])):
                result = _run_bootstrap_command(["git", "-C", str(project.workdir), *args], step=step, timeout=60.0)
                results.append(result)
                if not result["ok"]:
                    break
        else:
            results.append(_run_bootstrap_command(["git", "clone", "--branch", project.branch, project.repo, str(project.workdir)], step="git_clone", timeout=180.0))
        if not all(item["ok"] for item in results):
            return {"ok": False, "project": project.key, "results": results, "ssh_public_keys": _ssh_public_keys()}

        script_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with script_path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(_deploy_script_text(project))
            os.chmod(script_path, 0o755)
            results.append(_bootstrap_result("deploy_script", True, f"已写入 {script_path}"))
        except FileExistsError:
            results.append(_bootstrap_result("deploy_script", True, f"保留已有文件：{script_path}"))

        project.deploy_log_file.parent.mkdir(parents=True, exist_ok=True)
        project.deploy_log_file.touch(exist_ok=True)
        results.append(_bootstrap_result("log_file", True, f"已准备 {project.deploy_log_file}"))

        if write_service and project.template in {"python", "go", "java"}:
            service_path = Path("/etc/systemd/system") / f"{project.service_name}.service"
            try:
                with service_path.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(_systemd_service_text(project))
                os.chmod(service_path, 0o644)
                service_written = True
                results.append(_bootstrap_result("systemd_service", True, f"已写入 {service_path}"))
                reload_result = _run_bootstrap_command(["systemctl", "daemon-reload"], step="systemd_daemon_reload", timeout=30.0)
                results.append(reload_result)
                if reload_result["ok"]:
                    results.append(_run_bootstrap_command(["systemctl", "enable", project.service_name], step="systemd_enable", timeout=30.0))
            except FileExistsError:
                results.append(_bootstrap_result("systemd_service", True, f"保留已有服务：{service_path}"))
    except Exception as exc:  # noqa: BLE001 - return actionable bootstrap failure to UI
        results.append(_bootstrap_result("bootstrap", False, str(exc)))

    ok = all(item["ok"] for item in results)
    return {
        "ok": ok,
        "project": project.key,
        "results": results,
        "service_written": service_written,
        "service_name": project.service_name,
        "script": str(script_path),
        "doctor": _project_doctor(project),
        "preflight": _preflight_payload(project),
    }


_nginx_lock = threading.RLock()


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


def _nginx_project_has_site(project: DeployProject) -> bool:
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
                             "steps": steps, "notice": "无需项目、域名或证书。请在云安全组放行 HTTP 端口。"}}
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
                         "notice": "独立准备本机 Nginx，业务项目与域名可稍后配置。"}}
    if not _constant_time_equal(str(data.get("token") or ""), token):
        raise certificates.CertificateError("安装环境已变化，请重新检查后确认")
    nginx_runtime.prepare_local(local)
    status = nginx_install.check_http(80)
    return {"ok": True, "settings": _nginx_payload(), "message": "本机 Nginx 已安装并运行，HTTP 检查通过，可随后添加项目和域名入口。",
            "access_port": 80, "http_status": status}


def _nginx_site_plan(data: dict[str, Any]) -> tuple[DeployProject, dict[str, Any]]:
    existing = PROJECTS.get(str(data.get("project") or ""))
    if not existing:
        raise certificates.CertificateError("请先添加并保存业务项目")
    domain = str(data.get("domain") or "").strip().lower()
    port = data.get("port")
    if not _valid_domain(domain) or not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise certificates.CertificateError("请填写纯域名和 1-65535 之间的业务端口")
    if any(p.key != existing.key and p.app_domain == domain for p in PROJECTS.values()):
        raise certificates.CertificateError("此域名已分配给其他项目")
    settings = _nginx_settings()
    saved = settings.read()
    if (STATE_FILE.parent / "certificates" / existing.key).exists():
        raise certificates.CertificateError("此项目已有上传证书，请在证书页管理；修改入口前请先停用并删除证书")
    if _nginx_project_has_site(existing) and (domain != existing.app_domain or port != existing.service_port):
        raise certificates.CertificateError("该项目已有访问入口，请先移除旧入口，再修改域名或端口")
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
            raise certificates.CertificateError("请选择已保存的业务项目")
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
            raise certificates.CertificateError("请选择已保存的项目")
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
            raise certificates.CertificateError("请选择已登记的项目")
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


def _project_nginx_conf_path(project: DeployProject) -> Path:
    return _nginx_settings().runtime(live=False).conf_root / f"mini-deploy-{project.key}.conf"


def _project_nginx_config_text(project: DeployProject) -> str:
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
def _configure_project_nginx(project: DeployProject, *, issue_https: bool | None = None) -> dict[str, Any]:
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
            raise certificates.CertificateError("此项目已使用上传证书，请到证书管理中操作 HTTPS")
        config = _project_nginx_config_text(project)
        marker = "# mini-deploy-managed: project-http-v1\n"
        if previous and previous != config and not previous.startswith(marker):
            raise certificates.CertificateError("已有站点不是本面板托管的 HTTP 配置，拒绝覆盖")
        runtime.probe(settings.read()["upstreams"].get(project.key), project.service_port)
        store.commit_config(conf_path, marker + config, previous)
        results.append(_bootstrap_result("nginx_reload", True, "Nginx 配置已校验并重载"))
        if https_requested:
            if runtime.mode == "docker":
                results.append(_bootstrap_result("certbot_https", False, "Docker Nginx 请在证书页上传证书并启用 HTTPS；不会在宿主机运行 certbot --nginx"))
            elif not shutil.which("certbot"):
                results.append(_bootstrap_result("certbot_https", False, "未安装 Certbot，HTTP 已可用；也可上传证书"))
            else:
                results.append(_run_bootstrap_command([
                    "certbot", "--nginx", "-d", domain, "--non-interactive",
                    "--agree-tos", "--register-unsafely-without-email",
                ], step="certbot_https", timeout=180.0))
    except (ValueError, OSError) as exc:
        results.append(_bootstrap_result("nginx", False, str(exc)))
    http_ready = any(item["step"] == "nginx_reload" and item["ok"] for item in results)
    https_ok = any(item["step"] == "certbot_https" and item["ok"] for item in results)
    return {
        "ok": http_ready and (not https_requested or https_ok), "http_ready": http_ready,
        "https_ok": https_ok, "project": project.key, "domain": domain,
        "url": f"https://{domain}" if https_ok else _nginx_http_url(domain, profile), "results": results,
    }


def _queued_jobs_snapshot() -> list[dict[str, Any]]:
    with _jobs_admin_lock:
        return [
            {key: value for key, value in job.items() if key not in _JOB_INTERNAL_KEYS}
            for job in _jobs.queue
        ]


def _release_job_maintenance_lock(job: dict[str, Any]) -> None:
    descriptor = job.pop(_JOB_MAINTENANCE_LOCK_KEY, None)
    _release_maintenance_lock(descriptor)


def _enqueue_job(job: dict[str, Any]) -> None:
    descriptor = _acquire_maintenance_shared_lock(blocking=False)
    try:
        with _jobs_admin_lock:
            if _JOB_MAINTENANCE_LOCK_KEY in job:
                raise RuntimeError("deploy job already owns a maintenance lock")
            job[_JOB_MAINTENANCE_LOCK_KEY] = descriptor
            try:
                _jobs.put_nowait(job)
            except Exception:
                job.pop(_JOB_MAINTENANCE_LOCK_KEY, None)
                raise
        descriptor = None
    finally:
        _release_maintenance_lock(descriptor)


def _dequeue_job() -> dict[str, Any]:
    while True:
        with _jobs_admin_lock:
            try:
                return _jobs.get_nowait()
            except queue.Empty:
                pass
        time.sleep(0.2)


def _cancel_queued_jobs(project: DeployProject | None = None) -> list[dict[str, Any]]:
    canceled: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    with _jobs_admin_lock:
        while True:
            try:
                job = _jobs.get_nowait()
            except queue.Empty:
                break
            _jobs.task_done()
            if project is None or (job.get("project_key") or DEFAULT_PROJECT_KEY) == project.key:
                canceled.append(job)
            else:
                kept.append(job)
        for job in kept:
            try:
                _jobs.put_nowait(job)
            except queue.Full:
                canceled.append(job)
    for job in canceled:
        _release_job_maintenance_lock(job)
    _update_state(queue_size=_jobs.qsize())
    return canceled


def _terminate_process(proc: subprocess.Popen[str]) -> None:
    try:
        if os.name == "nt":
            proc.terminate()
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass


def _kill_process(proc: subprocess.Popen[str]) -> None:
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _read_process_output(proc: subprocess.Popen[str], stop_event: threading.Event) -> None:
    if proc.stdout is None:
        return
    try:
        for line in proc.stdout:
            clean_line = line.rstrip()
            diagnostic_tail = getattr(proc, "_mini_deploy_diagnostic_tail", None)
            if diagnostic_tail is not None:
                diagnostic_tail.append(clean_line[-1024:])
            if clean_line:
                _log(f"deploy: {clean_line}")
                phase = _phase_from_deploy_line(clean_line)
                if phase:
                    _update_current_deploy(phase=phase[0], phase_detail=phase[1])
                else:
                    build_detail = _build_progress_detail(clean_line)
                    if build_detail:
                        _update_current_deploy(phase="docker_build", phase_detail=build_detail)
            if stop_event.is_set() and proc.poll() is not None:
                break
    except Exception as exc:  # noqa: BLE001 - output reading must not orphan the deploy process
        _log(f"deploy output read failed: {exc}")


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


def _docker_status() -> dict[str, Any]:
    if not shutil.which("docker"):
        return {"available": False, "error": "docker command not found", "containers": []}
    code, ps_output = _run_command(["docker", "ps", "-a", "--format", "{{json .}}"], timeout=4.0)
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

    error_matcher, _ = _docker_log_matcher({"level": "error", "keyword": "", "regex": False, "context": 0})
    warn_matcher, _ = _docker_log_matcher({"level": "warn", "keyword": "", "regex": False, "context": 0})
    for container in containers:
        name = container.get("name") or container.get("id")
        if not name:
            continue
        try:
            lines = _docker_logs(str(name), tail=200)
        except Exception:
            continue
        if error_matcher is not None:
            container["recent_error_count"] = sum(1 for line in lines if error_matcher(line))
        if warn_matcher is not None:
            container["recent_warn_count"] = sum(1 for line in lines if warn_matcher(line))

    return {"available": True, "error": "" if stats_code == 0 else stats_output, "containers": containers}


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
def _docker_action(container: str, action: str) -> str:
    if not shutil.which("docker"):
        raise RuntimeError("docker command not found")
    allowed = {"restart", "stop", "start", "pause", "unpause"}
    if action not in allowed:
        raise ValueError("invalid_action")
    code, output = _run_command(["docker", action, container], timeout=30.0)
    if code != 0:
        raise RuntimeError(output or f"docker {action} failed")
    return output


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


def _project_history(state: dict[str, Any], project: DeployProject) -> list[dict[str, Any]]:
    history = []
    current = state.get("current_deploy")
    if isinstance(current, dict):
        history.append(current)
    raw_history = state.get("history") if isinstance(state.get("history"), list) else []
    history.extend(item for item in raw_history if isinstance(item, dict))
    return [item for item in history if (item.get("project_key") or "default") == project.key]


def _usable_commit(value: Any) -> str:
    text = str(value or "").strip()
    if not text or set(text) == {"0"}:
        return ""
    return text


def _commit_exists(project: DeployProject, commit: str) -> bool:
    if not commit:
        return False
    try:
        proc = subprocess.run(
            ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
            cwd=str(project.workdir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _project_rollback_target(state: dict[str, Any], project: DeployProject) -> str:
    try:
        head = _usable_commit(_run_git(["rev-parse", "HEAD"], project))
    except Exception:
        head = ""
    successful = [
        item for item in _project_history(state, project)
        if item.get("status") == "success" and _usable_commit(item.get("after"))
    ]
    if not successful:
        return ""
    latest = successful[0]
    before = _usable_commit(latest.get("before"))
    if before and before != head and _commit_exists(project, before):
        return before
    for item in successful[1:]:
        after = _usable_commit(item.get("after"))
        if after and after != head and _commit_exists(project, after):
            return after
    return ""


def _project_runtime_status(state: dict[str, Any], project: DeployProject, queued_jobs: list[dict[str, Any]]) -> dict[str, Any]:
    history = _project_history(state, project)
    current = state.get("current_deploy")
    running = bool(
        isinstance(current, dict)
        and (current.get("project_key") or "default") == project.key
        and current.get("status") == "running"
    )
    queued = sum(1 for job in queued_jobs if (job.get("project_key") or "default") == project.key)
    last_deploy = next((item for item in history if item.get("status") != "running"), None)
    rollback_target = _project_rollback_target(state, project)
    return {
        "enabled": project.enabled,
        "manual_deploy_enabled": project.manual_deploy_enabled,
        "running": running,
        "queue_size": queued,
        "last_deploy": last_deploy,
        "rollback_available": bool(project.rollback_script or rollback_target),
        "rollback_target": rollback_target,
        "timeout_seconds": project.timeout_seconds,
    }


def _deploy_lock_status(state: dict[str, Any]) -> dict[str, Any]:
    with _deploy_process_lock:
        proc = _deploy_process
        active_pid = proc.pid if proc is not None and proc.poll() is None else None
        worker_active = bool(_deploy_worker_thread and _deploy_worker_thread.is_alive())
        locked = _running_lock.locked()
    current = state.get("current_deploy") if isinstance(state.get("current_deploy"), dict) else {}
    duration = current.get("duration_seconds")
    return {
        "locked": locked,
        "active_pid": active_pid,
        "active_process": active_pid is not None,
        "active_worker": worker_active,
        "duration_seconds": duration,
        "project_key": current.get("project_key") or "",
        "phase": current.get("phase") or "",
        "phase_label": current.get("phase_label") or "",
        "can_force_unlock": bool(locked and active_pid is None and not worker_active),
    }


def _force_unlock_deploy() -> tuple[bool, dict[str, Any]]:
    global _cancel_requested, _deploy_process, _deploy_worker_thread
    with _deploy_process_lock:
        proc = _deploy_process
        active_pid = proc.pid if proc is not None and proc.poll() is None else None
        worker_active = bool(_deploy_worker_thread and _deploy_worker_thread.is_alive())
        if active_pid is not None:
            return False, {
                "error": "deploy_process_running",
                "active_pid": active_pid,
                "suggested_commands": [
                    f"ps -fp {active_pid}",
                    f"kill {active_pid}",
                    f"systemctl restart {shlex.quote(AGENT_SERVICE_NAME)}",
                ],
            }
        if worker_active:
            return False, {
                "error": "deploy_worker_active",
                "active_pid": None,
                "message": "deployment worker is still starting or cleaning up",
            }
        if not _running_lock.locked():
            return True, {"ok": True, "message": "deploy lock is already clear"}
        try:
            # Clear persisted/in-memory state before making the deploy lock
            # available. Otherwise a newly acquired job can publish running=True
            # and then be overwritten by this stale unlock request.
            _update_state(running=False, current_deploy=None, queue_size=_jobs.qsize())
        finally:
            try:
                _running_lock.release()
            except RuntimeError:
                pass
            _deploy_process = None
            _cancel_requested = None
            _deploy_worker_thread = None
    return True, {"ok": True, "message": "stale deploy lock cleared"}


def _release_deploy_worker_lifecycle() -> None:
    global _cancel_requested, _deploy_process, _deploy_worker_thread
    with _deploy_process_lock:
        if _deploy_worker_thread is not threading.current_thread():
            return
        # The queue worker is long-lived, but its ownership of this job ends
        # here even if an unresponsive child has not exited yet. Keep the lock
        # and process while it is active; force-unlock may clear them only after
        # poll() confirms that the orphaned process is gone.
        _deploy_worker_thread = None
        if _deploy_process is not None and _deploy_process.poll() is None:
            return
        _deploy_process = None
        _cancel_requested = None
        if _running_lock.locked():
            _running_lock.release()


def _clear_completed_orphan_lifecycle(proc: subprocess.Popen[str]) -> None:
    global _cancel_requested, _deploy_process, _deploy_worker_thread
    with _deploy_process_lock:
        if _deploy_process is not proc or _deploy_worker_thread is not None:
            return
        try:
            if proc.poll() is None:
                return
        except Exception:  # noqa: BLE001 - unreadable process state must remain fail closed
            return
        try:
            _update_state(running=False, current_deploy=None, queue_size=_jobs.qsize())
        finally:
            if _running_lock.locked():
                _running_lock.release()
            _deploy_process = None
            _cancel_requested = None
            _deploy_worker_thread = None


def _reap_orphan_process_maintenance_lock(proc: subprocess.Popen[str], descriptor: int) -> None:
    try:
        pid = getattr(proc, "pid", "unknown")
        _log_without_raising(
            f"deploy process pid={pid} outlived its worker; maintenance remains blocked until it exits"
        )
        last_log_at = time.monotonic()
        while True:
            try:
                if proc.poll() is not None:
                    return
                proc.wait(timeout=_ORPHAN_REAPER_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                pass
            except Exception as exc:  # noqa: BLE001 - fail closed while an orphan may still be mutating files
                now = time.monotonic()
                if now - last_log_at >= _ORPHAN_REAPER_LOG_SECONDS:
                    _log_without_raising(f"still waiting for orphan deploy process pid={pid}: {exc}")
                    last_log_at = now
                time.sleep(min(_ORPHAN_REAPER_WAIT_SECONDS, 5.0))
                continue
            now = time.monotonic()
            if now - last_log_at >= _ORPHAN_REAPER_LOG_SECONDS:
                _log_without_raising(
                    f"still waiting for orphan deploy process pid={pid}; maintenance remains blocked"
                )
                last_log_at = now
    finally:
        try:
            _clear_completed_orphan_lifecycle(proc)
        finally:
            _release_maintenance_lock(descriptor)


def _retain_job_lock_for_orphan_process(job: dict[str, Any]) -> None:
    if not job.pop(_JOB_DEPLOY_LIFECYCLE_OWNER_KEY, False):
        return
    descriptor = job.get(_JOB_MAINTENANCE_LOCK_KEY)
    if not isinstance(descriptor, int) or fcntl is None:
        return

    with _deploy_process_lock:
        proc = _deploy_process
        if proc is None:
            return
        try:
            active = proc.poll() is None
        except Exception:  # noqa: BLE001 - an unreadable process state must fail closed
            active = True
        if not active:
            return
        # flock(2) locks are shared by dup() descriptors, so unlocking the
        # worker's original descriptor would also unlock a duplicate. Transfer
        # the original descriptor to the reaper and remove it from the job.
        retained_descriptor = job.pop(_JOB_MAINTENANCE_LOCK_KEY)

    try:
        reaper = threading.Thread(
            target=_reap_orphan_process_maintenance_lock,
            args=(proc, retained_descriptor),
            name=f"deploy-orphan-reaper-{getattr(proc, 'pid', 'unknown')}",
            daemon=True,
        )
        reaper.start()
    except Exception as exc:  # noqa: BLE001 - synchronous fallback must keep the shared lock held
        _log_without_raising(f"failed to start orphan deploy reaper: {exc}; waiting synchronously")
        _reap_orphan_process_maintenance_lock(proc, retained_descriptor)


def _acquire_deploy_worker_lifecycle(job: dict[str, Any]) -> None:
    global _deploy_worker_thread
    last_log_at = time.monotonic()
    while True:
        with _deploy_process_lock:
            if _running_lock.acquire(blocking=False):
                _deploy_worker_thread = threading.current_thread()
                job[_JOB_DEPLOY_LIFECYCLE_OWNER_KEY] = True
                return
        now = time.monotonic()
        if now - last_log_at >= _DEPLOY_LOCK_WAIT_LOG_SECONDS:
            _log_without_raising("deploy worker is waiting for the previous process lifecycle to clear")
            last_log_at = now
        time.sleep(_DEPLOY_LOCK_WAIT_SECONDS)


def _alert(level: str, title: str, detail: str, source: str, command: str = "") -> dict[str, Any]:
    return {"level": level, "title": title, "detail": detail, "source": source, "command": command, "at": _now_text()}


def _alerts_payload(system: dict[str, Any], state: dict[str, Any], lock: dict[str, Any]) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    server = system.get("server") if isinstance(system.get("server"), dict) else {}
    memory = server.get("memory") if isinstance(server.get("memory"), dict) else {}
    disk = server.get("disk") if isinstance(server.get("disk"), dict) else {}
    network = server.get("network") if isinstance(server.get("network"), dict) else {}
    docker = system.get("docker") if isinstance(system.get("docker"), dict) else {}

    cpu = _percent(server.get("cpu_percent"))
    if cpu is not None and cpu >= 90:
        alerts.append(_alert("critical", "CPU usage is critical", f"CPU {cpu}%", "server", "top -o %CPU"))
    elif cpu is not None and cpu >= 75:
        alerts.append(_alert("warning", "CPU usage is high", f"CPU {cpu}%", "server", "top -o %CPU"))

    memory_percent = _percent(memory.get("percent"))
    if memory_percent is not None and memory_percent >= 90:
        alerts.append(_alert("critical", "Memory usage is critical", f"Memory {memory_percent}%", "server", "free -h"))
    elif memory_percent is not None and memory_percent >= 80:
        alerts.append(_alert("warning", "Memory usage is high", f"Memory {memory_percent}%", "server", "free -h"))

    disk_percent = _percent(disk.get("percent"))
    if disk_percent is not None and disk_percent >= 90:
        alerts.append(_alert("critical", "Disk space is critical", f"{disk.get('path') or '/'} used {disk_percent}%", "server", "df -h"))
    elif disk_percent is not None and disk_percent >= 80:
        alerts.append(_alert("warning", "Disk space is high", f"{disk.get('path') or '/'} used {disk_percent}%", "server", "df -h"))

    network_percent = _percent(network.get("percent"))
    if network_percent is not None and network_percent >= 90:
        alerts.append(_alert("warning", "Network throughput is near limit", f"Network {network_percent}%", "server", "iftop"))

    if not docker.get("available", False):
        alerts.append(_alert("critical", "Docker is unavailable", str(docker.get("error") or "cannot read Docker status"), "docker", "systemctl status docker --no-pager"))
    for container in docker.get("containers") or []:
        if not isinstance(container, dict):
            continue
        name = str(container.get("name") or container.get("id") or "container")
        state_text = str(container.get("state") or "")
        health = str(container.get("health") or "")
        if state_text and state_text.lower() != "running":
            alerts.append(_alert("critical", f"Container is not running: {name}", str(container.get("status") or state_text), "docker", f"docker start {name}"))
        elif health == "unhealthy":
            alerts.append(_alert("critical", f"Container health check failed: {name}", str(container.get("status") or ""), "docker", f"docker logs --tail=200 {name}"))
        error_count = int(container.get("recent_error_count") or 0)
        if error_count > 0:
            alerts.append(_alert("warning", f"Container has recent error logs: {name}", f"{error_count} error lines in latest 200 lines", "docker", f"docker logs --tail=200 {name}"))

    current = state.get("current_deploy") if isinstance(state.get("current_deploy"), dict) else {}
    duration = current.get("duration_seconds")
    if lock.get("locked") and isinstance(duration, (int, float)) and duration > 600:
        alerts.append(_alert("warning", "Deployment has been running for a long time", f"{duration}s at {current.get('phase_label') or '-'}", "deploy", "journalctl -u mini-deploy-agent -f"))
    if lock.get("locked") and lock.get("can_force_unlock"):
        alerts.append(_alert(
            "critical",
            "Deployment lock may be stale",
            "No active deploy process was found, but the lock is still held.",
            "deploy",
            f"systemctl restart {shlex.quote(AGENT_SERVICE_NAME)}",
        ))
    return alerts[:40]


def _event_item(kind: str, level: str, title: str, detail: str, at: str, source: str = "") -> dict[str, Any]:
    return {"kind": kind, "level": level, "title": title, "detail": detail, "at": at, "source": source}


def _events_payload(state: dict[str, Any], system: dict[str, Any], alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    current = state.get("current_deploy")
    if isinstance(current, dict) and current.get("status") == "running":
        events.append(_event_item(
            "deploy",
            "running",
            f"{current.get('project_name') or current.get('project_key') or 'Project'} deploying",
            f"{current.get('phase_label') or '-'} · {current.get('duration_seconds') or 0}s",
            str(current.get("started_at") or _now_text()),
            "deploy",
        ))
    for item in list(state.get("history") or [])[:20]:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "")
        events.append(_event_item(
            "deploy",
            "critical" if status == "failed" else ("warning" if status == "canceled" else "success"),
            f"{item.get('project_name') or item.get('project_key') or 'Project'} deployment {status or '-'}",
            f"{_short_sha(item.get('after'))} · {item.get('duration_seconds') or '-'}s · {item.get('commit_message') or '-'}",
            str(item.get("finished_at") or item.get("started_at") or ""),
            "deploy",
        ))
    ignored = state.get("last_ignored_webhook")
    if isinstance(ignored, dict):
        events.append(_event_item("webhook", "warning", "Webhook ignored", str(ignored.get("reason") or ignored.get("ref") or "-"), str(ignored.get("at") or ""), "webhook"))
    docker = system.get("docker") if isinstance(system.get("docker"), dict) else {}
    for container in docker.get("containers") or []:
        if not isinstance(container, dict):
            continue
        name = str(container.get("name") or container.get("id") or "container")
        error_count = int(container.get("recent_error_count") or 0)
        if error_count:
            events.append(_event_item("docker", "warning", f"{name} has error logs", f"{error_count} error lines in latest 200 lines", _now_text(), "docker"))
        state_text = str(container.get("state") or "")
        if state_text and state_text.lower() != "running":
            events.append(_event_item("docker", "critical", f"{name} is not running", str(container.get("status") or state_text), _now_text(), "docker"))
    for alert in alerts[:12]:
        events.append(_event_item("alert", str(alert.get("level") or "warning"), str(alert.get("title") or "Alert"), str(alert.get("detail") or ""), str(alert.get("at") or ""), str(alert.get("source") or "alert")))
    return events[:60]


def _manual_deploy_job(actor: str, project: DeployProject) -> dict[str, str]:
    head = _run_git(["rev-parse", "HEAD"], project)
    subject = _run_git(["log", "-1", "--pretty=%s"], project)
    author = _run_git(["log", "-1", "--pretty=%an"], project)
    return {
        "project_key": project.key,
        "project_name": project.name,
        "ref": f"refs/heads/{project.branch}",
        "before": head,
        "after": head,
        "source": "manual",
        "actor": actor,
        "commit_message": subject,
        "commit_author": author,
    }


def _rollback_job(actor: str, project: DeployProject, state: dict[str, Any]) -> dict[str, str]:
    job = _manual_deploy_job(actor, project)
    target = _project_rollback_target(state, project)
    job["source"] = "rollback"
    job["action"] = "rollback"
    if target:
        job["after"] = target
        job["commit_message"] = f"Rollback to {target[:8]}"
    return job


def _preflight_item(level: str, title: str, detail: str, command: str = "") -> dict[str, Any]:
    return {"level": level, "title": title, "detail": detail, "command": command}


def _preflight_payload(project: DeployProject) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    if not project.enabled:
        items.append(_preflight_item("critical", "Project is disabled", "This project will not respond to webhook or manual deploy.", "Enable the project in the panel"))
    if not project.workdir.exists():
        items.append(_preflight_item("critical", "Project directory does not exist", str(project.workdir), f"mkdir -p {shlex.quote(str(project.workdir))}"))
    elif not (project.workdir / ".git").exists():
        items.append(_preflight_item("warning", "Project directory is not a Git repository", str(project.workdir), f"cd {shlex.quote(str(project.workdir))} && git status"))
    else:
        status = _run_git(["status", "--porcelain"], project)
        if status:
            items.append(_preflight_item("warning", "Git working tree has local changes", status.splitlines()[0], f"cd {shlex.quote(str(project.workdir))} && git status --short"))
        remote = _run_git(["remote", "-v"], project)
        if not remote:
            items.append(_preflight_item("warning", "Git remote is not configured", "Deploy may be unable to pull remote code.", f"cd {shlex.quote(str(project.workdir))} && git remote -v"))

    script_path = _project_script_path(project)
    if not script_path.is_file():
        items.append(_preflight_item("critical", "Deploy script does not exist", str(script_path), f"chmod +x {shlex.quote(str(script_path))}"))
    elif not _script_is_executable(script_path):
        items.append(_preflight_item("critical", "Deploy script is not executable", str(script_path), f"chmod +x {shlex.quote(str(script_path))}"))

    if project.template == "docker":
        if not shutil.which("docker"):
            items.append(_preflight_item("critical", "Docker command was not found", "This Docker project cannot deploy without Docker.", "SETUP_DOCKER=yes bash install.sh"))
        else:
            code, output = _run_command(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=4.0)
            if code != 0:
                items.append(_preflight_item("critical", "Docker daemon is unavailable", output or "docker info failed", "systemctl status docker --no-pager"))
            compose_file = project.workdir / "docker-compose.yml"
            compose_yaml = project.workdir / "docker-compose.yaml"
            if not compose_file.exists() and not compose_yaml.exists():
                items.append(_preflight_item("warning", "docker-compose file was not found", str(project.workdir), f"ls -lh {shlex.quote(str(project.workdir))}"))

    try:
        usage = shutil.disk_usage(project.workdir if project.workdir.exists() else Path("/"))
        free_gb = usage.free / 1024 / 1024 / 1024
        free_percent = usage.free / usage.total * 100 if usage.total else 0
        if free_gb < 1 or free_percent < 5:
            items.append(_preflight_item("critical", "Disk free space is low", f"Free {free_gb:.1f}GB / {free_percent:.1f}%", "df -h"))
    except OSError as exc:
        items.append(_preflight_item("warning", "Failed to read disk space", str(exc), "df -h"))

    for path, label in ((STATE_FILE.parent, "State directory"), (LOG_FILE.parent, "Agent log directory"), (project.deploy_log_file.parent, "Deploy log directory")):
        if not path.exists():
            items.append(_preflight_item("critical", f"{label} does not exist", str(path), f"mkdir -p {shlex.quote(str(path))} && chmod 700 {shlex.quote(str(path))}"))
        elif not os.access(path, os.W_OK):
            items.append(_preflight_item("critical", f"{label} is not writable", str(path), f"chmod 700 {shlex.quote(str(path))}"))

    if not project.health_url:
        items.append(_preflight_item("warning", "Health URL is not configured", "Deploy cannot automatically verify service health.", "Fill health_url in project config"))

    level_order = {"critical": 2, "warning": 1, "ok": 0}
    worst = "ok"
    for item in items:
        if level_order.get(str(item.get("level")), 0) > level_order[worst]:
            worst = str(item.get("level"))
    if not items:
        items.append(_preflight_item("ok", "Preflight passed", "No blocking deployment issues were found."))
    return {
        "project": project.key,
        "project_name": project.name,
        "ok": worst == "ok",
        "level": worst,
        "items": items,
        "checked_at": _now_text(),
    }


def _status_payload() -> dict[str, Any]:
    with _state_lock:
        state = json.loads(json.dumps(_state, ensure_ascii=False))
    state["running"] = _running_lock.locked()
    state["queue_size"] = _jobs.qsize()
    current = state.get("current_deploy")
    if isinstance(current, dict) and current.get("status") == "running":
        started_ts = current.get("started_ts")
        if isinstance(started_ts, (int, float)):
            current["duration_seconds"] = round(max(time.time() - float(started_ts), 0), 1)
        if current.get("phase"):
            current["phase_label"] = _phase_label(current.get("phase"))
    with _projects_lock:
        projects = dict(PROJECTS)
        default_key = DEFAULT_PROJECT_KEY
    default_project = projects.get(default_key)
    queued_jobs = _queued_jobs_snapshot()
    project_payloads = []
    for project in projects.values():
        runtime = _project_runtime_status(state, project, queued_jobs)
        project_payloads.append({
            "key": project.key,
            "name": project.name,
            "repo": project.repo,
            "branch": project.branch,
            "project_dir": str(project.workdir),
            "deploy_script": project.script,
            "health_url": project.health_url,
            **runtime,
            "head": _run_git(["rev-parse", "HEAD"], project),
            "short_head": _run_git(["rev-parse", "--short", "HEAD"], project),
            "git_branch": _run_git(["branch", "--show-current"], project),
        })
    system_payload = _system_status_payload()
    lock = _deploy_lock_status(state)
    alerts = _alerts_payload(system_payload, state, lock)
    events = _events_payload(state, system_payload, alerts)
    return {
        "agent": {
            "status": "ok",
            "branch": default_project.branch if default_project else "",
            "host": HOST,
            "port": PORT,
            "project_dir": str(default_project.workdir) if default_project else "",
            "deploy_script": default_project.script if default_project else "",
            "project_count": len(projects),
            "projects_config_file": str(PROJECTS_CONFIG_FILE),
            "projects_config_exists": PROJECTS_CONFIG_FILE.exists(),
            "ui_auth_configured": bool(UI_PASSWORD_HASH and UI_SESSION_SECRET),
        },
        "git": {
            "head": _run_git(["rev-parse", "HEAD"], default_project) if default_project else "",
            "short_head": _run_git(["rev-parse", "--short", "HEAD"], default_project) if default_project else "",
            "branch": _run_git(["branch", "--show-current"], default_project) if default_project else "",
        },
        "projects": project_payloads,
        "default_project": default_key,
        "system": system_payload,
        "alerts": alerts,
        "events": events,
        "lock": lock,
        "state": state,
        "csrf_token": "",
        "generated_at": int(time.time()),
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


def _deploy_subprocess_environment(
    project: DeployProject,
    action: str,
    script_path: Path,
    job: dict[str, Any],
) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _DEPLOY_CONTROL_SECRET_ENV_NAMES
    }
    env.update({
        "DEPLOY_PROJECT_KEY": project.key,
        "DEPLOY_PROJECT_NAME": project.name,
        "PROJECT_DIR": str(project.workdir),
        "DEPLOY_BRANCH": project.branch,
        "DEPLOY_SCRIPT": str(script_path),
        "DEPLOY_ACTION": action,
        "HEALTH_URL": project.health_url,
        "DEPLOY_LOG_FILE": str(project.deploy_log_file),
        "DEPLOY_SERVICE_NAME": project.service_name,
        "DEPLOY_SERVICE_PORT": str(project.service_port),
        "DEPLOY_REF": str(job.get("ref") or ""),
        "DEPLOY_BEFORE": str(job.get("before") or ""),
        "DEPLOY_AFTER": str(job.get("after") or ""),
        "DEPLOY_SOURCE": str(job.get("source") or "webhook"),
        "DEPLOY_TRIGGERED_AT": str(int(time.time())),
    })
    return env


def _run_deploy(job: dict[str, Any]) -> None:
    owns_maintenance_lock = _JOB_MAINTENANCE_LOCK_KEY not in job
    if owns_maintenance_lock:
        job[_JOB_MAINTENANCE_LOCK_KEY] = _acquire_maintenance_shared_lock(blocking=True)
    try:
        _run_deploy_locked(job)
    finally:
        if owns_maintenance_lock:
            try:
                _retain_job_lock_for_orphan_process(job)
            finally:
                _release_job_maintenance_lock(job)


def _run_deploy_locked(job: dict[str, Any]) -> None:
    global _cancel_requested, _deploy_process, _deploy_worker_thread
    _acquire_deploy_worker_lifecycle(job)

    project_key = str(job.get("project_key") or DEFAULT_PROJECT_KEY)
    project = PROJECTS.get(project_key)
    if not project:
        _log(f"deploy skipped because project no longer exists project={project_key} after={job.get('after')}")
        _release_deploy_worker_lifecycle()
        return
    try:
        action = str(job.get("action") or "deploy")
        script_path = _project_script_path(project, action=action)
        started_at = _now_text()
        started_ts = time.time()
        current = {
            "status": "running",
            "project_key": project.key,
            "project_name": project.name,
            "ref": job.get("ref") or "",
            "before": job.get("before") or "",
            "after": job.get("after") or "",
            "source": job.get("source") or "webhook",
            "action": action,
            "actor": job.get("actor") or "",
            "commit_message": job.get("commit_message") or "",
            "commit_author": job.get("commit_author") or "",
            "started_at": started_at,
            "started_ts": started_ts,
            "finished_at": None,
            "duration_seconds": None,
            "exit_code": None,
            "phase": "starting",
            "phase_label": _phase_label("starting"),
            "phase_detail": "",
            "phase_started_ts": started_ts,
            "phase_durations": [],
            "changed_files": job.get("changed_files") or [],
            "changed_file_count": job.get("changed_file_count") or 0,
        }
        _update_state(running=True, current_deploy=current)
    except Exception:
        _release_deploy_worker_lifecycle()
        raise
    canceled = False
    exit_code = 1
    proc: subprocess.Popen[str] | None = None
    output_stop = threading.Event()
    output_reader: threading.Thread | None = None
    diagnostic_tail: deque[str] = deque(maxlen=64)
    diagnostic_error = ""

    try:
        env = _deploy_subprocess_environment(project, action, script_path, job)

        _log(
            "deploy start "
            f"project={project.key} action={action} ref={env['DEPLOY_REF']} "
            f"before={env['DEPLOY_BEFORE']} after={env['DEPLOY_AFTER']}"
        )
        popen_kwargs: dict[str, Any] = {}
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            [str(script_path)],
            cwd=str(project.workdir),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **popen_kwargs,
        )
        proc._mini_deploy_diagnostic_tail = diagnostic_tail
        with _deploy_process_lock:
            _deploy_process = proc
            _cancel_requested = None
        output_reader = threading.Thread(target=_read_process_output, args=(proc, output_stop), daemon=True)
        output_reader.start()
        timed_out = False
        deadline = time.time() + project.timeout_seconds
        while True:
            with _deploy_process_lock:
                cancel_request = _cancel_requested
            if cancel_request:
                canceled = True
                _log(f"deploy canceled project={project.key} action={action} actor={cancel_request.get('actor', '')}")
                _update_current_deploy(
                    phase="canceled",
                    phase_label=_phase_label("canceled"),
                    phase_detail=f"取消人 {cancel_request.get('actor', '')}",
                )
                if proc.poll() is None:
                    _terminate_process(proc)
                break
            if proc.poll() is not None:
                break
            if time.time() > deadline:
                timed_out = True
                _log(f"deploy timeout project={project.key} action={action} after {project.timeout_seconds}s")
                _update_current_deploy(
                    phase="timeout",
                    phase_label=_phase_label("timeout"),
                    phase_detail=f"超过 {project.timeout_seconds}s",
                )
                _terminate_process(proc)
                break
            time.sleep(0.2)
        if timed_out:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _kill_process(proc)
                proc.wait(timeout=10)
            exit_code = 124
        elif canceled:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _kill_process(proc)
                proc.wait(timeout=10)
            exit_code = 130
        else:
            exit_code = proc.returncode if proc.returncode is not None else proc.wait(timeout=10)
        _log(f"deploy finished project={project.key} action={action} code={exit_code} after={env['DEPLOY_AFTER']}")
    except Exception as exc:  # noqa: BLE001 - top-level worker guard
        exit_code = 1
        diagnostic_error = str(exc)
        if proc is not None and proc.poll() is None:
            _terminate_process(proc)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _kill_process(proc)
                proc.wait(timeout=10)
        _log(f"deploy failed: {exc}")
    finally:
        if output_reader is not None:
            output_reader.join(timeout=2)
        output_stop.set()
        finished_ts = time.time()
        current_snapshot = dict(current)
        with _state_lock:
            live_current = _state.get("current_deploy")
            if isinstance(live_current, dict):
                current_snapshot.update(live_current)
        _close_current_phase(current_snapshot, finished_ts)
        entry = {
            **current_snapshot,
            "status": "canceled" if canceled else ("success" if exit_code == 0 else "failed"),
            "phase": "canceled" if canceled else current_snapshot.get("phase"),
            "phase_label": _phase_label("canceled") if canceled else current_snapshot.get("phase_label"),
            "finished_at": _now_text(),
            "duration_seconds": round(finished_ts - started_ts, 1),
            "exit_code": exit_code,
        }
        if entry["status"] == "failed":
            entry["diagnosis"] = project_guidance.diagnose(
                "\n".join([*list(diagnostic_tail), diagnostic_error]), exit_code=exit_code,
            )
        _append_history(entry)
        _notify_deploy_finished(entry)
        _release_deploy_worker_lifecycle()


def _worker() -> None:
    while True:
        job = _dequeue_job()
        try:
            _run_deploy(job)
        except Exception as exc:  # noqa: BLE001 - one failed job must not stop the worker
            _log(f"deploy worker recovered from unexpected error: {exc}")
        finally:
            try:
                _release_deploy_worker_lifecycle()
            finally:
                try:
                    _retain_job_lock_for_orphan_process(job)
                finally:
                    try:
                        _jobs.task_done()
                    finally:
                        _release_job_maintenance_lock(job)


def _normal_path(path: str) -> str:
    if path == "/deploy":
        return "/ui"
    if path.startswith("/deploy/"):
        return path[len("/deploy") :]
    return path


def _short_sha(value: Any) -> str:
    text = str(value or "")
    return text[:8] if text else "-"


UI_DIR = Path(__file__).resolve().parent / "ui"
UI_ASSET_TYPES = {
    "selects.js": "application/javascript; charset=utf-8",
    "motion.js": "application/javascript; charset=utf-8",
    "nginx.js": "application/javascript; charset=utf-8",
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
        kind = (query.get("kind", ["deploy"])[0] or "deploy").strip()
        project_raw = (query.get("project", [""])[0] or "").strip()
        project_key = _safe_project_key(project_raw) if project_raw else ""
        if kind == "agent":
            return LOG_FILE, "agent"
        if project_key and project_key in PROJECTS:
            return PROJECTS[project_key].deploy_log_file, f"{project_key}-deploy"
        return DEPLOY_LOG_FILE, "deploy"

    def _handle_logs(self, query: dict[str, list[str]]) -> None:
        lines = _log_line_limit(query.get("lines", [LOG_TAIL_LINES])[0], default=LOG_TAIL_LINES)
        project_logs = {
            project.key: _tail(project.deploy_log_file, lines=lines)
            for project in PROJECTS.values()
        }
        self._write_json(200, {
            "line_limit": lines,
            "max_line_limit": LOG_TAIL_MAX_LINES,
            "deploy_log": _tail(DEPLOY_LOG_FILE, lines=lines),
            "agent_log": _tail(LOG_FILE, lines=lines),
            "project_logs": project_logs,
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
        if path == "/health":
            self._write_json(200, {"status": "ok", "queue_size": _jobs.qsize()})
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
        if path == "/preflight":
            if self._require_auth_json():
                self._handle_preflight(parse_qs(parsed.query))
            return
        if path == "/docker/logs":
            if self._require_auth_json():
                self._handle_docker_logs(parse_qs(parsed.query))
            return
        if path == "/docker/logs/download":
            if self._authenticated():
                self._handle_docker_logs_download(parse_qs(parsed.query))
            else:
                self._write_json(401, {"error": "unauthorized"})
            return
        if path == "/projects-config":
            if self._require_auth_json():
                self._write_json(200, _projects_config_payload(include_secret=True))
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
        if path == "/projects-config/doctor":
            if self._require_auth_json():
                self._handle_projects_config_doctor(parse_qs(parsed.query))
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
        if path == "/redeploy":
            if self._require_auth_json():
                self._handle_redeploy(parse_qs(parsed.query))
            return
        if path == "/rollback":
            if self._require_auth_json():
                self._handle_rollback(parse_qs(parsed.query))
            return
        if path == "/cancel":
            if self._require_auth_json():
                self._handle_cancel(parse_qs(parsed.query))
            return
        if path == "/force-unlock":
            if self._require_auth_json():
                self._handle_force_unlock()
            return
        if path == "/docker/action":
            if self._require_auth_json():
                self._handle_docker_action()
            return
        if path == "/projects-config/init":
            if self._require_auth_json():
                self._handle_projects_config_init()
            return
        if path == "/projects-config/save":
            if self._require_auth_json():
                self._handle_projects_config_save()
            return
        if path == "/projects-config/bootstrap":
            if self._require_auth_json():
                self._handle_projects_config_bootstrap()
            return
        if path in {"/projects-config/inspect", "/projects-config/preview"}:
            if self._require_auth_json():
                self._handle_project_guidance(path.rsplit("/", 1)[-1])
            return
        if path == "/projects-config/nginx":
            if self._require_auth_json():
                self._handle_projects_config_nginx()
            return
        if path == "/projects-config/delete":
            if self._require_auth_json():
                self._handle_projects_config_delete(parse_qs(parsed.query))
            return
        if path == "/projects-config/secret":
            if self._require_auth_json():
                self._handle_projects_config_secret(parse_qs(parsed.query))
            return
        if path == "/projects-config/notifications":
            if self._require_auth_json():
                self._handle_notifications_save()
            return
        if path == "/notifications/test":
            if self._require_auth_json():
                self._handle_notifications_test()
            return
        if path != "/webhook":
            self._write_json(404, {"error": "not_found"})
            return
        self._handle_webhook(parsed)

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
            self._write_html(500, _render_login("部署面板密码尚未初始化。"))
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

    def _handle_projects_config_init(self) -> None:
        try:
            _initialize_projects_config_transaction()
        except OSError as exc:
            _log(f"projects config init failed: {exc}")
            _audit_event("projects_config_init", actor=self.client_address[0], target=str(PROJECTS_CONFIG_FILE), success=False, detail={"error": str(exc)})
            self._write_json(500, {"error": "config_write_failed", "detail": str(exc)})
            return
        _log(f"projects config initialized file={PROJECTS_CONFIG_FILE}")
        _audit_event("projects_config_init", actor=self.client_address[0], target=str(PROJECTS_CONFIG_FILE), success=True)
        self._write_json(200, _projects_config_payload(include_secret=True))

    def _handle_projects_config_save(self) -> None:
        try:
            data = _read_json_body(self)
        except (ValueError, json.JSONDecodeError) as exc:
            self._write_json(400, {"error": "invalid_json", "detail": str(exc)})
            return

        raw_project = data.get("project", data)
        if not isinstance(raw_project, dict):
            self._write_json(400, {"error": "invalid_project"})
            return
        original_key_raw = data.get("original_key") or raw_project.get("original_key") or ""
        original_key = _safe_project_key(str(original_key_raw)) if original_key_raw else ""
        try:
            project = _save_project_transaction(raw_project, original_key)
        except _ProjectKeyExistsError:
            self._write_json(409, {"error": "project_key_exists"})
            return
        except OSError as exc:
            target = original_key or _safe_project_key(str(raw_project.get("key") or "project"))
            _log(f"projects config save failed: {exc}")
            _audit_event("projects_config_save", actor=self.client_address[0], target=target, success=False, detail={"error": str(exc)})
            self._write_json(500, {"error": "config_write_failed", "detail": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - normalize validation errors for UI
            self._write_json(400, {"error": "invalid_project", "detail": str(exc)})
            return
        _log(f"projects config saved project={project.key} file={PROJECTS_CONFIG_FILE}")
        _audit_event("projects_config_save", actor=self.client_address[0], target=project.key, success=True, detail={"original_key": original_key})
        self._write_json(200, _projects_config_payload(include_secret=True))

    def _handle_project_guidance(self, action: str) -> None:
        try:
            data = _read_json_body(self, max_bytes=128 * 1024)
            raw = data.get("project")
            if not isinstance(raw, dict):
                raise ValueError("请填写项目配置")
            if action == "inspect":
                environment = {key: value for key, value in os.environ.items()
                               if key not in _DEPLOY_CONTROL_SECRET_ENV_NAMES}
                result = project_guidance.inspect_repository(
                    str(raw.get("repo") or "").strip(), str(raw.get("branch") or "main").strip(), env=environment,
                )
            else:
                result = _project_preview(_project_from_form(raw))
        except (ValueError, OSError) as exc:
            self._write_json(400, {"error": "project_guidance_failed", "detail": str(exc)})
            return
        _audit_event(f"project_{action}", actor=self.client_address[0], success=result.get("ok", True))
        self._write_json(200, result)

    def _handle_projects_config_bootstrap(self) -> None:
        try:
            data = _read_json_body(self, max_bytes=128 * 1024)
        except (ValueError, json.JSONDecodeError) as exc:
            self._write_json(400, {"error": "invalid_json", "detail": str(exc)})
            return

        raw_project = data.get("project", data)
        if not isinstance(raw_project, dict):
            self._write_json(400, {"error": "invalid_project"})
            return
        try:
            project_guidance.validate_repository(str(raw_project.get("repo") or ""), str(raw_project.get("branch") or "main"))
            _project_preview(_project_from_form(raw_project))
        except (ValueError, OSError) as exc:
            self._write_json(400, {"error": "invalid_project", "detail": str(exc)})
            return
        original_key_raw = data.get("original_key") or raw_project.get("original_key") or ""
        original_key = _safe_project_key(str(original_key_raw)) if original_key_raw else ""
        try:
            project = _save_project_transaction(raw_project, original_key)
        except _ProjectKeyExistsError:
            self._write_json(409, {"error": "project_key_exists"})
            return
        except OSError as exc:
            target = original_key or _safe_project_key(str(raw_project.get("key") or "project"))
            _log(f"projects bootstrap config save failed: {exc}")
            _audit_event("projects_config_bootstrap", actor=self.client_address[0], target=target, success=False, detail={"error": str(exc)})
            self._write_json(500, {"error": "config_write_failed", "detail": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - normalize validation errors for UI
            self._write_json(400, {"error": "invalid_project", "detail": str(exc)})
            return

        options = data.get("options") if isinstance(data.get("options"), dict) else {}
        write_service = _bool_config(options.get("write_service"), True)
        result = _bootstrap_project(project, write_service=write_service)
        _log(f"project bootstrap project={project.key} ok={result.get('ok')}")
        _audit_event(
            "projects_config_bootstrap",
            actor=self.client_address[0],
            target=project.key,
            success=bool(result.get("ok")),
            detail={"service_written": result.get("service_written"), "results": result.get("results")},
        )
        payload = _projects_config_payload(include_secret=True)
        payload["bootstrap"] = result
        self._write_json(200, payload)

    def _handle_projects_config_nginx(self) -> None:
        try:
            data = _read_json_body(self, max_bytes=128 * 1024)
        except (ValueError, json.JSONDecodeError) as exc:
            self._write_json(400, {"error": "invalid_json", "detail": str(exc)})
            return

        raw_project = data.get("project", data)
        if not isinstance(raw_project, dict):
            self._write_json(400, {"error": "invalid_project"})
            return
        original_key_raw = data.get("original_key") or raw_project.get("original_key") or ""
        original_key = _safe_project_key(str(original_key_raw)) if original_key_raw else ""
        try:
            project = _save_project_transaction(raw_project, original_key)
        except _ProjectKeyExistsError:
            self._write_json(409, {"error": "project_key_exists"})
            return
        except OSError as exc:
            target = original_key or _safe_project_key(str(raw_project.get("key") or "project"))
            _log(f"projects nginx config save failed: {exc}")
            _audit_event("projects_config_nginx", actor=self.client_address[0], target=target, success=False, detail={"error": str(exc)})
            self._write_json(500, {"error": "config_write_failed", "detail": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - normalize validation errors for UI
            self._write_json(400, {"error": "invalid_project", "detail": str(exc)})
            return

        options = data.get("options") if isinstance(data.get("options"), dict) else {}
        issue_https = _bool_config(options.get("issue_https"), project.app_https)
        result = _configure_project_nginx(project, issue_https=issue_https)
        _log(f"project nginx config project={project.key} domain={result.get('domain', '')} ok={result.get('ok')}")
        _audit_event(
            "projects_config_nginx",
            actor=self.client_address[0],
            target=project.key,
            success=bool(result.get("ok")),
            detail={"domain": result.get("domain"), "results": result.get("results")},
        )
        payload = _projects_config_payload(include_secret=True)
        payload["nginx"] = result
        self._write_json(200, payload)

    def _handle_projects_config_doctor(self, query: dict[str, list[str]]) -> None:
        key = _safe_project_key(query.get("project", [""])[0] or query.get("key", [""])[0])
        project = PROJECTS.get(key)
        if not project:
            self._write_json(404, {"error": "project_not_found"})
            return
        self._write_json(200, _project_doctor(project))

    def _handle_projects_config_delete(self, query: dict[str, list[str]]) -> None:
        key = _safe_project_key(query.get("project", [""])[0] or query.get("key", [""])[0])
        try:
            _delete_project_transaction(key)
        except _ProjectNotFoundError:
            self._write_json(404, {"error": "project_not_found"})
            return
        except _ProjectRunningError:
            self._write_json(409, {"error": "project_is_running"})
            return
        except certificates.CertificateError as exc:
            self._write_json(409, {"error": "certificate_in_use", "detail": str(exc)})
            return
        except OSError as exc:
            _log(f"projects config delete failed: {exc}")
            _audit_event("projects_config_delete", actor=self.client_address[0], target=key, success=False, detail={"error": str(exc)})
            self._write_json(500, {"error": "config_write_failed", "detail": str(exc)})
            return
        _log(f"projects config deleted project={key} file={PROJECTS_CONFIG_FILE}")
        _audit_event("projects_config_delete", actor=self.client_address[0], target=key, success=True)
        self._write_json(200, _projects_config_payload(include_secret=True))

    def _handle_projects_config_secret(self, query: dict[str, list[str]]) -> None:
        key = _safe_project_key(query.get("project", [""])[0] or query.get("key", [""])[0])
        try:
            _reset_project_secret_transaction(key)
        except _ProjectNotFoundError:
            self._write_json(404, {"error": "project_not_found"})
            return
        except OSError as exc:
            _log(f"projects config secret reset failed: {exc}")
            _audit_event("projects_config_secret_reset", actor=self.client_address[0], target=key, success=False, detail={"error": str(exc)})
            self._write_json(500, {"error": "config_write_failed", "detail": str(exc)})
            return
        _log(f"projects config secret reset project={key}")
        _audit_event("projects_config_secret_reset", actor=self.client_address[0], target=key, success=True)
        self._write_json(200, _projects_config_payload(include_secret=True))

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
        self._write_json(200, _projects_config_payload(include_secret=True))

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

    def _handle_preflight(self, query: dict[str, list[str]]) -> None:
        project = _project_for_manual(query)
        if not project:
            self._write_json(404, {"error": "project_not_found"})
            return
        self._write_json(200, _preflight_payload(project))

    def _handle_force_unlock(self) -> None:
        ok, payload = _force_unlock_deploy()
        _audit_event("force_unlock", actor=self.client_address[0], success=ok, detail=payload)
        if ok:
            _log(f"force unlock requested actor={self.client_address[0]} result={payload.get('message')}")
            self._write_json(200, payload)
        else:
            self._write_json(409, payload)

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
            output = _docker_action(container, action)
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

    def _handle_redeploy(self, query: dict[str, list[str]]) -> None:
        if _jobs.full():
            self._write_json(429, {"error": "deploy_queue_full"})
            return
        project = _project_for_manual(query)
        if not project:
            self._write_json(404, {"error": "project_not_found"})
            return
        if not project.enabled:
            self._write_json(403, {"error": "project_disabled"})
            return
        if not project.manual_deploy_enabled:
            self._write_json(403, {"error": "manual_deploy_disabled"})
            return
        job = _manual_deploy_job(self.client_address[0], project)
        try:
            _enqueue_job(job)
        except _MaintenanceActiveError:
            self._write_json(503, {"error": "maintenance_in_progress"})
            return
        except _MaintenanceLockError as exc:
            _log(f"manual deploy rejected because maintenance lock is unavailable: {exc}")
            self._write_json(503, {"error": "maintenance_lock_unavailable"})
            return
        except queue.Full:
            self._write_json(429, {"error": "deploy_queue_full"})
            return
        _update_state(last_manual_trigger_at=_now_text(), queue_size=_jobs.qsize())
        _log(f"manual deploy queued project={project.key} after={job['after']} actor={job['actor']}")
        _audit_event("manual_deploy", actor=self.client_address[0], target=project.key, success=True, detail={"after": job["after"]})
        self._write_json(202, {
            "status": "queued",
            "project": project.key,
            "queue_size": _jobs.qsize(),
            "after": job["after"],
        })

    def _handle_rollback(self, query: dict[str, list[str]]) -> None:
        if _jobs.full():
            self._write_json(429, {"error": "deploy_queue_full"})
            return
        project = _project_for_manual(query)
        if not project:
            self._write_json(404, {"error": "project_not_found"})
            return
        if not project.enabled:
            self._write_json(403, {"error": "project_disabled"})
            return
        if not project.manual_deploy_enabled:
            self._write_json(403, {"error": "manual_deploy_disabled"})
            return
        with _state_lock:
            state = json.loads(json.dumps(_state, ensure_ascii=False))
        rollback_target = _project_rollback_target(state, project)
        if not project.rollback_script and not rollback_target:
            self._write_json(400, {"error": "rollback_not_available"})
            return
        job = _rollback_job(self.client_address[0], project, state)
        try:
            _enqueue_job(job)
        except _MaintenanceActiveError:
            self._write_json(503, {"error": "maintenance_in_progress"})
            return
        except _MaintenanceLockError as exc:
            _log(f"rollback rejected because maintenance lock is unavailable: {exc}")
            self._write_json(503, {"error": "maintenance_lock_unavailable"})
            return
        except queue.Full:
            self._write_json(429, {"error": "deploy_queue_full"})
            return
        _update_state(last_manual_trigger_at=_now_text(), queue_size=_jobs.qsize())
        _log(f"rollback queued project={project.key} after={job['after']} actor={job['actor']}")
        _audit_event("rollback", actor=self.client_address[0], target=project.key, success=True, detail={"after": job["after"]})
        self._write_json(202, {
            "status": "queued",
            "action": "rollback",
            "project": project.key,
            "queue_size": _jobs.qsize(),
            "after": job["after"],
        })

    def _handle_cancel(self, query: dict[str, list[str]]) -> None:
        requested = query.get("project", [""])[0] or query.get("project_key", [""])[0]
        all_requested = (query.get("all", [""])[0] or "").strip().lower() in {"1", "true", "yes", "all"}
        if not requested and not all_requested:
            self._write_json(400, {"error": "project_required"})
            return
        project: DeployProject | None = None
        if requested:
            project = PROJECTS.get(_safe_project_key(requested))
            if not project:
                self._write_json(404, {"error": "project_not_found"})
                return

        canceled_jobs = _cancel_queued_jobs(project)
        running_canceled = False
        running_project_key = ""
        with _state_lock:
            current = _state.get("current_deploy")
        if isinstance(current, dict) and current.get("status") == "running":
            running_project_key = str(current.get("project_key") or DEFAULT_PROJECT_KEY)
            if project is None or running_project_key == project.key:
                with _deploy_process_lock:
                    proc = _deploy_process
                    if proc is not None and proc.poll() is None:
                        global _cancel_requested
                        _cancel_requested = {
                            "actor": self.client_address[0],
                            "project_key": running_project_key,
                            "at": _now_text(),
                        }
                        running_canceled = True
                if running_canceled and proc is not None:
                    _update_current_deploy(
                        phase="canceling",
                        phase_label=_phase_label("canceling"),
                        phase_detail=f"取消人 {self.client_address[0]}",
                    )
                    _terminate_process(proc)

        target = project.key if project else "all"
        _audit_event(
            "deploy_cancel",
            actor=self.client_address[0],
            target=target,
            success=bool(running_canceled or canceled_jobs),
            detail={
                "running_canceled": running_canceled,
                "running_project": running_project_key,
                "queued_canceled": len(canceled_jobs),
            },
        )
        self._write_json(202, {
            "status": "cancel_requested" if running_canceled else "queued_canceled",
            "project": target,
            "running_canceled": running_canceled,
            "queued_canceled": len(canceled_jobs),
            "queue_size": _jobs.qsize(),
        })

    def _handle_webhook(self, parsed: Any) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0 or length > MAX_BODY_BYTES:
            self._write_json(413, {"error": "invalid_body_size"})
            return

        body = self.rfile.read(length)
        query = parse_qs(parsed.query)
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self._write_json(400, {"error": "invalid_json"})
            return

        client_ip = _request_client_ip(self)
        project = _project_for_webhook(payload, query)
        if not project:
            _log(f"webhook rejected from {client_ip}: project not found")
            self._write_json(403, {"error": "forbidden"})
            return
        if not _valid_signature_for_project(project, self.headers, body, query):
            _log(f"webhook rejected from {client_ip}: invalid secret project={project.key}")
            self._write_json(403, {"error": "forbidden"})
            return
        if not project.enabled:
            ignored = {"project_key": project.key, "reason": "project_disabled", "at": _now_text()}
            _update_state(last_ignored_webhook=ignored)
            _log(f"webhook ignored project={project.key}: project disabled")
            self._write_json(202, {"status": "ignored", "project": project.key, "reason": "project_disabled"})
            return

        ref = _extract_ref(payload)
        expected_ref = f"refs/heads/{project.branch}"
        if ref != expected_ref:
            ignored = {"project_key": project.key, "ref": ref, "expected": expected_ref, "at": _now_text()}
            _update_state(last_ignored_webhook=ignored)
            _log(f"webhook ignored project={project.key} ref={ref}, expected={expected_ref}")
            self._write_json(202, {"status": "ignored", "project": project.key, "reason": "branch_mismatch"})
            return

        job = {
            "project_key": project.key,
            "project_name": project.name,
            "ref": ref,
            "before": _extract_commit(payload, "before"),
            "after": _extract_commit(payload, "after"),
            "source": "webhook",
            "actor": self.headers.get("X-Gitee-Event", "") or self.client_address[0],
            **_extract_commit_details(payload),
        }
        try:
            _enqueue_job(job)
        except _MaintenanceActiveError:
            self._write_json(503, {"error": "maintenance_in_progress"})
            return
        except _MaintenanceLockError as exc:
            _log(f"webhook rejected because maintenance lock is unavailable: {exc}")
            self._write_json(503, {"error": "maintenance_lock_unavailable"})
            return
        except queue.Full:
            self._write_json(429, {"error": "deploy_queue_full"})
            return

        _update_state(last_webhook_at=_now_text(), queue_size=_jobs.qsize())
        _log(f"webhook accepted project={project.key} ref={ref} after={job['after']}")
        self._write_json(202, {"status": "queued", "project": project.key, "queue_size": _jobs.qsize()})

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
    first = getpass.getpass("新的部署面板密码: ")
    second = getpass.getpass("再次输入部署面板密码: ")
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
    first = getpass.getpass("新的部署面板密码: ")
    second = getpass.getpass("再次输入部署面板密码: ")
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
        "  validate-config           validate projects configuration without starting HTTP\n"
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


def _project_script_path(project: DeployProject, action: str = "deploy") -> Path:
    script_text = project.rollback_script if action == "rollback" and project.rollback_script else project.script
    script = Path(script_text)
    return script if script.is_absolute() else project.workdir / script


def _script_is_executable(path: Path) -> bool:
    if os.name == "nt":
        return path.is_file()
    return path.is_file() and os.access(path, os.X_OK)


def _validate_projects_runtime_config() -> list[DeployProject]:
    if _RUNTIME_CONFIG_ERROR is not None:
        raise SystemExit(str(_RUNTIME_CONFIG_ERROR))
    if PROJECTS and (not DEFAULT_PROJECT_KEY or DEFAULT_PROJECT_KEY not in PROJECTS):
        raise SystemExit("projects config did not load a valid default project")

    enabled_projects = [project for project in PROJECTS.values() if project.enabled]
    weak_secrets = [
        project.key
        for project in enabled_projects
        if not _is_strong_webhook_secret(project.webhook_secret)
    ]
    if weak_secrets:
        raise SystemExit(
            "webhook secret missing, too short, or still a placeholder for projects: "
            f"{', '.join(weak_secrets)}"
        )
    missing_scripts = [
        f"{project.key}:{_project_script_path(project)}"
        for project in enabled_projects
        if not _project_script_path(project).is_file()
    ]
    if missing_scripts:
        raise SystemExit(f"deploy script not found: {', '.join(missing_scripts)}")
    non_executable_scripts = [
        f"{project.key}:{_project_script_path(project)}"
        for project in enabled_projects
        if not _script_is_executable(_project_script_path(project))
    ]
    if non_executable_scripts:
        raise SystemExit(f"deploy script is not executable, run chmod +x: {', '.join(non_executable_scripts)}")
    missing_rollback_scripts = [
        f"{project.key}:{_project_script_path(project, action='rollback')}"
        for project in enabled_projects
        if project.rollback_script and not _project_script_path(project, action="rollback").is_file()
    ]
    if missing_rollback_scripts:
        raise SystemExit(f"rollback script not found: {', '.join(missing_rollback_scripts)}")
    non_executable_rollback_scripts = [
        f"{project.key}:{_project_script_path(project, action='rollback')}"
        for project in enabled_projects
        if project.rollback_script and not _script_is_executable(_project_script_path(project, action="rollback"))
    ]
    if non_executable_rollback_scripts:
        raise SystemExit(
            "rollback script is not executable, run chmod +x: "
            f"{', '.join(non_executable_rollback_scripts)}"
        )
    return enabled_projects


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
            "projects config OK: "
            f"file={PROJECTS_CONFIG_FILE} projects={len(PROJECTS)} enabled={len(enabled_projects)}"
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
    if ALLOW_QUERY_WEBHOOK_TOKEN:
        _log("security warning: query-string webhook tokens are enabled and may leak through access logs")
    threading.Thread(target=_worker, daemon=True).start()
    threading.Thread(target=_realtime_metric_sampler, name="realtime-metric-sampler", daemon=True).start()
    threading.Thread(target=_system_metric_sampler, name="system-metric-sampler", daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    _log(f"deploy agent listening on {HOST}:{PORT}, projects={len(PROJECTS)} default={DEFAULT_PROJECT_KEY}")
    server.serve_forever()


if __name__ == "__main__":
    main()
