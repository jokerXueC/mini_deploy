#!/usr/bin/env python3
"""Small webhook deploy agent for low-memory servers.

The agent accepts a Gitee/GitHub/GitLab style push webhook, validates a shared
secret, filters the branch, and runs one fixed deploy script in a background
thread. It also exposes a tiny read-only dashboard protected by a local
password hash and signed cookie.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import html
import json
import os
import queue
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


APP_HOME = Path(os.getenv("MINI_DEPLOY_HOME", os.getenv("VIBEPILOT_HOME", "/opt/mini_deploy")))
HOST = os.getenv("DEPLOY_AGENT_HOST", "127.0.0.1")
PORT = int(os.getenv("DEPLOY_AGENT_PORT", "9010"))
WEBHOOK_SECRET = os.getenv("DEPLOY_WEBHOOK_SECRET", "")
DEPLOY_BRANCH = os.getenv("DEPLOY_BRANCH", "main")
DEPLOY_SCRIPT = os.getenv("DEPLOY_SCRIPT", str(APP_HOME / "scripts" / "deploy.sample.sh"))
PROJECT_DIR = Path(os.getenv("PROJECT_DIR", str(APP_HOME / "workspace" / "default")))
HEALTH_URL = os.getenv("HEALTH_URL", "")
DEPLOY_PROJECTS_FILE_TEXT = os.getenv("DEPLOY_PROJECTS_FILE", "").strip()
DEPLOY_PROJECTS_FILE = Path(DEPLOY_PROJECTS_FILE_TEXT) if DEPLOY_PROJECTS_FILE_TEXT else None
PROJECTS_CONFIG_FILE = DEPLOY_PROJECTS_FILE or APP_HOME / "projects.json"
AGENT_ENV_FILE = Path(os.getenv("DEPLOY_AGENT_ENV_FILE", "/etc/mini-deploy-agent.env"))
LOG_FILE = Path(os.getenv("DEPLOY_AGENT_LOG", "/var/log/mini_deploy/mini-deploy-agent.log"))
DEPLOY_LOG_FILE = Path(os.getenv("DEPLOY_LOG_FILE", "/var/log/mini_deploy/mini_deploy.log"))
STATE_FILE = Path(os.getenv("DEPLOY_AGENT_STATE_FILE", "/var/lib/mini-deploy-agent/state.json"))
AUDIT_LOG_FILE = Path(os.getenv("DEPLOY_AUDIT_LOG_FILE", str(STATE_FILE.with_name("audit.jsonl"))))
MAX_BODY_BYTES = int(os.getenv("DEPLOY_AGENT_MAX_BODY_BYTES", str(1024 * 1024)))
PROJECT_CONFIG_BACKUP_LIMIT = int(os.getenv("DEPLOY_PROJECT_CONFIG_BACKUP_LIMIT", "20"))
LOG_TAIL_LINES = int(os.getenv("DEPLOY_LOG_TAIL_LINES", "320"))
LOG_TAIL_MAX_LINES = int(os.getenv("DEPLOY_LOG_TAIL_MAX_LINES", "5000"))
LOG_DOWNLOAD_MAX_BYTES = int(os.getenv("DEPLOY_LOG_DOWNLOAD_MAX_BYTES", str(32 * 1024 * 1024)))
SYSTEM_STATUS_CACHE_SECONDS = int(os.getenv("DEPLOY_SYSTEM_STATUS_CACHE_SECONDS", "5"))
NETWORK_MAX_MBPS = float(os.getenv("DEPLOY_NETWORK_MAX_MBPS", "100"))
DOCKER_LOG_TAIL_MAX_LINES = int(os.getenv("DEPLOY_DOCKER_LOG_TAIL_MAX_LINES", "5000"))

UI_PASSWORD_HASH = os.getenv("DEPLOY_UI_PASSWORD_HASH", "")
UI_SESSION_SECRET = os.getenv("DEPLOY_UI_SESSION_SECRET", "").strip() or WEBHOOK_SECRET
UI_SESSION_TTL_SECONDS = int(os.getenv("DEPLOY_UI_SESSION_TTL_SECONDS", str(8 * 60 * 60)))
COOKIE_SECURE_MODE = os.getenv("DEPLOY_COOKIE_SECURE", "auto").strip().lower()
COOKIE_NAME = "mini_deploy_session"
PASSWORD_HASH_ITERATIONS = 260_000
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


def _safe_project_key(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "-" for ch in value.strip().lower())
    return cleaned.strip("-") or "default"


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
        enabled=_bool_config(os.getenv("DEPLOY_PROJECT_ENABLED"), True),
        manual_deploy_enabled=_bool_config(os.getenv("DEPLOY_MANUAL_DEPLOY_ENABLED"), True),
        timeout_seconds=_int_config(os.getenv("DEPLOY_TIMEOUT_SECONDS"), 900),
        rollback_script=os.getenv("DEPLOY_ROLLBACK_SCRIPT", ""),
    )


def _project_from_config(raw: dict[str, Any], fallback_key: str) -> DeployProject:
    key = _safe_project_key(str(raw.get("key") or fallback_key))
    branch = str(raw.get("branch") or DEPLOY_BRANCH)
    workdir = Path(str(raw.get("workdir") or raw.get("project_dir") or PROJECT_DIR))
    script = str(raw.get("script") or DEPLOY_SCRIPT)
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
        webhook_secret=str(raw.get("webhook_secret") or raw.get("secret") or WEBHOOK_SECRET),
        enabled=_bool_config(raw.get("enabled"), True),
        manual_deploy_enabled=_bool_config(raw.get("manual_deploy_enabled"), True),
        timeout_seconds=_int_config(raw.get("timeout_seconds"), _int_config(os.getenv("DEPLOY_TIMEOUT_SECONDS"), 900)),
        rollback_script=str(raw.get("rollback_script") or ""),
    )


def _load_projects() -> dict[str, DeployProject]:
    if PROJECTS_CONFIG_FILE.exists():
        try:
            raw = json.loads(PROJECTS_CONFIG_FILE.read_text(encoding="utf-8"))
            items = raw.get("projects") if isinstance(raw, dict) else raw
            if isinstance(items, list):
                projects = {}
                for index, item in enumerate(items):
                    if not isinstance(item, dict):
                        continue
                    project = _project_from_config(item, f"project-{index + 1}")
                    projects[project.key] = project
                if projects:
                    return projects
        except Exception as exc:  # noqa: BLE001 - keep agent bootable on bad config
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} projects config load failed: {exc}", flush=True)
    project = _default_project()
    return {project.key: project}


PROJECTS = _load_projects()
DEFAULT_PROJECT_KEY = next(iter(PROJECTS))

_jobs: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=20)
_jobs_admin_lock = threading.Lock()
_projects_lock = threading.Lock()
_running_lock = threading.Lock()
_state_lock = threading.Lock()
_audit_lock = threading.Lock()
_deploy_process_lock = threading.Lock()
_deploy_process: subprocess.Popen[str] | None = None
_cancel_requested: dict[str, Any] | None = None
_state: dict[str, Any] = {
    "running": False,
    "queue_size": 0,
    "last_webhook_at": None,
    "last_ignored_webhook": None,
    "current_deploy": None,
    "last_deploy": None,
    "history": [],
}
_login_failures: dict[str, list[float]] = {}
_system_status_cache: dict[str, Any] = {"at": 0.0, "payload": {}}
_system_status_lock = threading.Lock()
_last_cpu_sample: tuple[int, int] | None = None
_last_network_sample: tuple[float, int, int] | None = None


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _log(message: str) -> None:
    line = f"{_now_text()} {message}\n"
    print(line, end="", flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


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
        "actor": actor,
        "target": target,
        "success": bool(success),
        "detail": detail or {},
    }
    try:
        AUDIT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _audit_lock:
            with AUDIT_LOG_FILE.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError as exc:
        _log(f"audit write failed: {exc}")


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _valid_signature(headers: Any, body: bytes, query: dict[str, list[str]]) -> bool:
    if not WEBHOOK_SECRET:
        return False

    token_candidates = [
        headers.get("X-Gitee-Token", ""),
        headers.get("X-Gitlab-Token", ""),
        headers.get("X-Webhook-Token", ""),
        headers.get("X-Hook-Token", ""),
        query.get("token", [""])[0],
        query.get("secret", [""])[0],
    ]
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


def _extract_commit_details(payload: dict[str, Any]) -> dict[str, str]:
    head = payload.get("head_commit")
    if not isinstance(head, dict):
        return {"commit_message": "", "commit_author": ""}
    message = head.get("message") if isinstance(head.get("message"), str) else ""
    author = head.get("author")
    author_name = ""
    if isinstance(author, dict):
        for key in ("name", "username", "email"):
            value = author.get(key)
            if isinstance(value, str) and value:
                author_name = value
                break
    return {"commit_message": message, "commit_author": author_name}


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
        return PROJECTS[DEFAULT_PROJECT_KEY]
    return None


def _project_for_manual(query: dict[str, list[str]]) -> DeployProject | None:
    requested = query.get("project", [""])[0] or query.get("project_key", [""])[0]
    if requested:
        return PROJECTS.get(_safe_project_key(requested))
    return PROJECTS.get(DEFAULT_PROJECT_KEY)


def _valid_signature_for_project(project: DeployProject, headers: Any, body: bytes, query: dict[str, list[str]]) -> bool:
    secret = project.webhook_secret
    if not secret:
        return False

    token_candidates = [
        headers.get("X-Gitee-Token", ""),
        headers.get("X-Gitlab-Token", ""),
        headers.get("X-Webhook-Token", ""),
        headers.get("X-Hook-Token", ""),
        query.get("token", [""])[0],
        query.get("secret", [""])[0],
    ]
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
    }
    if include_secret:
        data["webhook_secret"] = project.webhook_secret
    return data


def _projects_config_items(include_secret: bool = True) -> list[dict[str, Any]]:
    return [_project_to_config(project, include_secret=include_secret) for project in PROJECTS.values()]


def _projects_config_payload(include_secret: bool = True) -> dict[str, Any]:
    return {
        "config_file": str(PROJECTS_CONFIG_FILE),
        "config_exists": PROJECTS_CONFIG_FILE.exists(),
        "projects": _projects_config_items(include_secret=include_secret),
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
    webhook_secret = _clean_config_text(raw.get("webhook_secret") or raw.get("secret"), existing.webhook_secret if existing else "", max_length=256)
    if not webhook_secret:
        webhook_secret = secrets.token_hex(32)
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
    )


def _backup_projects_config() -> str:
    if not PROJECTS_CONFIG_FILE.exists():
        return ""
    backup_dir = PROJECTS_CONFIG_FILE.parent / ".backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"{PROJECTS_CONFIG_FILE.name}.{stamp}.bak"
    shutil.copy2(PROJECTS_CONFIG_FILE, backup_path)
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


def _write_projects_config(projects: dict[str, DeployProject]) -> None:
    backup_path = _backup_projects_config()
    PROJECTS_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"projects": [_project_to_config(project, include_secret=True) for project in projects.values()]}
    tmp = PROJECTS_CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PROJECTS_CONFIG_FILE)
    try:
        os.chmod(PROJECTS_CONFIG_FILE, 0o600)
    except OSError:
        pass
    if backup_path:
        _log(f"projects config backup created file={backup_path}")


def _replace_projects(projects: dict[str, DeployProject]) -> None:
    global PROJECTS, DEFAULT_PROJECT_KEY
    if not projects:
        raise ValueError("at_least_one_project_required")
    with _projects_lock:
        PROJECTS = dict(projects)
        DEFAULT_PROJECT_KEY = next(iter(PROJECTS))


def _save_and_reload_projects(projects: dict[str, DeployProject]) -> None:
    _write_projects_config(projects)
    _replace_projects(projects)


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


def _write_state() -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        with _state_lock:
            data = dict(_state)
            data["queue_size"] = _jobs.qsize()
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)
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
        current.update(changes)
        if current.get("phase"):
            current["phase_label"] = _phase_label(current.get("phase"))
        started_ts = current.get("started_ts")
        if isinstance(started_ts, (int, float)):
            current["duration_seconds"] = round(max(time.time() - float(started_ts), 0), 1)
        _state["queue_size"] = _jobs.qsize()
    _write_state()


def _phase_from_deploy_line(line: str) -> tuple[str, str] | None:
    text = str(line or "").strip()
    marker = "phase="
    if marker not in text:
        return None
    phase = text.split(marker, 1)[1].split(None, 1)[0].strip()
    if not phase:
        return None
    return phase, text


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
    target = project or PROJECTS[DEFAULT_PROJECT_KEY]
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


def _queued_jobs_snapshot() -> list[dict[str, Any]]:
    with _jobs_admin_lock:
        return list(_jobs.queue)


def _enqueue_job(job: dict[str, Any]) -> None:
    with _jobs_admin_lock:
        _jobs.put_nowait(job)


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
            if clean_line:
                _log(f"deploy: {clean_line}")
                phase = _phase_from_deploy_line(clean_line)
                if phase:
                    _update_current_deploy(phase=phase[0], phase_detail=phase[1])
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
        payload = {
            "server": {
                "cpu_percent": _cpu_percent(),
                "load": _load_average(),
                "memory": _memory_status(),
                "disk": _disk_status(Path("/")),
                "network": _network_status(),
                "sampled_at": _now_text(),
                "cache_seconds": SYSTEM_STATUS_CACHE_SECONDS,
            },
            "docker": _docker_status(),
        }
        _system_status_cache["at"] = now
        _system_status_cache["payload"] = payload
        return json.loads(json.dumps(payload, ensure_ascii=False))


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


def _status_payload() -> dict[str, Any]:
    with _state_lock:
        state = json.loads(json.dumps(_state, ensure_ascii=False))
    state["running"] = _running_lock.locked()
    state["queue_size"] = _jobs.qsize()
    default_project = PROJECTS[DEFAULT_PROJECT_KEY]
    queued_jobs = _queued_jobs_snapshot()
    project_payloads = []
    for project in PROJECTS.values():
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
    return {
        "agent": {
            "status": "ok",
            "branch": default_project.branch,
            "host": HOST,
            "port": PORT,
            "project_dir": str(default_project.workdir),
            "deploy_script": default_project.script,
            "project_count": len(PROJECTS),
            "projects_config_file": str(PROJECTS_CONFIG_FILE),
            "projects_config_exists": PROJECTS_CONFIG_FILE.exists(),
            "ui_auth_configured": bool(UI_PASSWORD_HASH and UI_SESSION_SECRET),
        },
        "git": {
            "head": _run_git(["rev-parse", "HEAD"], default_project),
            "short_head": _run_git(["rev-parse", "--short", "HEAD"], default_project),
            "branch": _run_git(["branch", "--show-current"], default_project),
        },
        "projects": project_payloads,
        "default_project": DEFAULT_PROJECT_KEY,
        "system": _system_status_payload(),
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


def _verify_password(password: str) -> bool:
    try:
        scheme, iterations, salt_b64, expected_b64 = UI_PASSWORD_HASH.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected = base64.urlsafe_b64decode(expected_b64.encode("ascii"))
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            int(iterations),
        )
        return hmac.compare_digest(digest, expected)
    except Exception:
        return False


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


def _is_rate_limited(ip: str) -> bool:
    now = time.time()
    failures = [ts for ts in _login_failures.get(ip, []) if now - ts < 60]
    _login_failures[ip] = failures
    return len(failures) >= 5


def _record_login_failure(ip: str) -> None:
    failures = _login_failures.setdefault(ip, [])
    failures.append(time.time())
    _login_failures[ip] = failures[-10:]


def _upsert_env_file(path: Path, values: dict[str, str]) -> None:
    existing_lines: list[str] = []
    if path.exists():
        existing_lines = path.read_text(encoding="utf-8").splitlines()
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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _run_deploy(job: dict[str, Any]) -> None:
    if not _running_lock.acquire(blocking=False):
        _log(f"deploy skipped because another job is running after={job.get('after')}")
        return

    project_key = str(job.get("project_key") or DEFAULT_PROJECT_KEY)
    project = PROJECTS.get(project_key)
    if not project:
        _log(f"deploy skipped because project no longer exists project={project_key} after={job.get('after')}")
        _running_lock.release()
        return
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
    }
    _update_state(running=True, current_deploy=current)
    canceled = False
    exit_code = 1
    proc: subprocess.Popen[str] | None = None
    output_stop = threading.Event()
    output_reader: threading.Thread | None = None

    try:
        env = os.environ.copy()
        env["DEPLOY_PROJECT_KEY"] = project.key
        env["DEPLOY_PROJECT_NAME"] = project.name
        env["PROJECT_DIR"] = str(project.workdir)
        env["DEPLOY_BRANCH"] = project.branch
        env["DEPLOY_SCRIPT"] = str(script_path)
        env["DEPLOY_ACTION"] = action
        env["HEALTH_URL"] = project.health_url
        env["DEPLOY_LOG_FILE"] = str(project.deploy_log_file)
        env["DEPLOY_REF"] = str(job.get("ref") or "")
        env["DEPLOY_BEFORE"] = str(job.get("before") or "")
        env["DEPLOY_AFTER"] = str(job.get("after") or "")
        env["DEPLOY_SOURCE"] = str(job.get("source") or "webhook")
        env["DEPLOY_TRIGGERED_AT"] = str(int(time.time()))

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
        global _deploy_process, _cancel_requested
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
        if proc is not None and proc.poll() is None:
            _terminate_process(proc)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _kill_process(proc)
                proc.wait(timeout=10)
        _log(f"deploy failed: {exc}")
    finally:
        output_stop.set()
        if output_reader is not None:
            output_reader.join(timeout=2)
        finished_ts = time.time()
        entry = {
            **current,
            "status": "canceled" if canceled else ("success" if exit_code == 0 else "failed"),
            "phase": "canceled" if canceled else current.get("phase"),
            "phase_label": _phase_label("canceled") if canceled else current.get("phase_label"),
            "finished_at": _now_text(),
            "duration_seconds": round(finished_ts - started_ts, 1),
            "exit_code": exit_code,
        }
        _append_history(entry)
        with _deploy_process_lock:
            if _deploy_process is not None and _deploy_process.poll() is not None:
                _deploy_process = None
            _cancel_requested = None
        _running_lock.release()


def _worker() -> None:
    while True:
        job = _dequeue_job()
        try:
            _run_deploy(job)
        finally:
            _jobs.task_done()


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
        setup_note = """
        <div class="setup">
          <strong>首次使用：请设置管理密码。</strong>
          密码会加密写入环境文件，设置完成后重启 Agent 即可登录。
        </div>
        """
        form_html = """
        <form method="post" action="setup-password" class="login-form">
          <input type="password" name="password" placeholder="设置管理密码" autofocus autocomplete="new-password">
          <input type="password" name="password_confirm" placeholder="再次输入密码" autocomplete="new-password">
          <button type="submit">初始化密码</button>
        </form>
        """
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
        if path == "/projects-config/delete":
            if self._require_auth_json():
                self._handle_projects_config_delete(parse_qs(parsed.query))
            return
        if path == "/projects-config/secret":
            if self._require_auth_json():
                self._handle_projects_config_secret(parse_qs(parsed.query))
            return
        if path != "/webhook":
            self._write_json(404, {"error": "not_found"})
            return
        self._handle_webhook(parsed)

    def _handle_login(self) -> None:
        if not UI_PASSWORD_HASH or not UI_SESSION_SECRET:
            self._write_html(500, _render_login("部署面板密码尚未初始化。"))
            return
        ip = self.client_address[0]
        if _is_rate_limited(ip):
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
            _record_login_failure(ip)
            _audit_event("login", actor=ip, success=False, detail={"reason": "bad_password"})
            self._write_html(401, _render_login("密码错误。"))
            return

        _audit_event("login", actor=ip, success=True)
        self.send_response(303)
        self.send_header("Location", "ui")
        self._set_session_cookie()
        self.end_headers()

    def _handle_setup_password(self) -> None:
        global UI_PASSWORD_HASH, UI_SESSION_SECRET
        if UI_PASSWORD_HASH:
            self._write_html(403, _render_login("管理密码已经初始化。"))
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0 or length > 4096:
            self._write_html(400, _render_login("请求内容无效。"))
            return
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        fields = parse_qs(body)
        password = fields.get("password", [""])[0]
        password_confirm = fields.get("password_confirm", [""])[0]
        if len(password) < 8:
            self._write_html(400, _render_login("密码至少需要 8 位。"))
            return
        if password != password_confirm:
            self._write_html(400, _render_login("两次输入的密码不一致。"))
            return
        password_hash = _hash_password(password)
        session_secret = secrets.token_hex(32)
        try:
            _upsert_env_file(AGENT_ENV_FILE, {
                "DEPLOY_UI_PASSWORD_HASH": password_hash,
                "DEPLOY_UI_SESSION_SECRET": session_secret,
            })
        except OSError as exc:
            self._write_html(500, _render_login(f"写入环境文件失败：{exc}"))
            return
        UI_PASSWORD_HASH = password_hash
        UI_SESSION_SECRET = session_secret
        _audit_event("setup_password", actor=self.client_address[0], target=str(AGENT_ENV_FILE), success=True)
        self._write_html(200, _render_login(f"密码已写入 {AGENT_ENV_FILE}，现在可以直接登录。后续重启服务也会继续生效。"))

    def _handle_projects_config_init(self) -> None:
        try:
            _write_projects_config(PROJECTS)
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
        projects = dict(PROJECTS)
        existing = projects.get(original_key) if original_key else None
        try:
            project = _project_from_form(raw_project, existing=existing)
        except Exception as exc:  # noqa: BLE001 - normalize validation errors for UI
            self._write_json(400, {"error": "invalid_project", "detail": str(exc)})
            return
        if original_key and original_key != project.key:
            projects.pop(original_key, None)
        if project.key in projects and original_key != project.key:
            self._write_json(409, {"error": "project_key_exists"})
            return
        projects[project.key] = project
        try:
            _save_and_reload_projects(projects)
        except OSError as exc:
            _log(f"projects config save failed: {exc}")
            _audit_event("projects_config_save", actor=self.client_address[0], target=project.key, success=False, detail={"error": str(exc)})
            self._write_json(500, {"error": "config_write_failed", "detail": str(exc)})
            return
        _log(f"projects config saved project={project.key} file={PROJECTS_CONFIG_FILE}")
        _audit_event("projects_config_save", actor=self.client_address[0], target=project.key, success=True, detail={"original_key": original_key})
        self._write_json(200, _projects_config_payload(include_secret=True))

    def _handle_projects_config_doctor(self, query: dict[str, list[str]]) -> None:
        key = _safe_project_key(query.get("project", [""])[0] or query.get("key", [""])[0])
        project = PROJECTS.get(key)
        if not project:
            self._write_json(404, {"error": "project_not_found"})
            return
        self._write_json(200, _project_doctor(project))

    def _handle_projects_config_delete(self, query: dict[str, list[str]]) -> None:
        key = _safe_project_key(query.get("project", [""])[0] or query.get("key", [""])[0])
        projects = dict(PROJECTS)
        if key not in projects:
            self._write_json(404, {"error": "project_not_found"})
            return
        if len(projects) <= 1:
            self._write_json(400, {"error": "at_least_one_project_required"})
            return
        with _state_lock:
            current = _state.get("current_deploy")
        if isinstance(current, dict) and (current.get("project_key") or "default") == key and current.get("status") == "running":
            self._write_json(409, {"error": "project_is_running"})
            return
        projects.pop(key)
        try:
            _save_and_reload_projects(projects)
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
        projects = dict(PROJECTS)
        project = projects.get(key)
        if not project:
            self._write_json(404, {"error": "project_not_found"})
            return
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
        )
        try:
            _save_and_reload_projects(projects)
        except OSError as exc:
            _log(f"projects config secret reset failed: {exc}")
            _audit_event("projects_config_secret_reset", actor=self.client_address[0], target=key, success=False, detail={"error": str(exc)})
            self._write_json(500, {"error": "config_write_failed", "detail": str(exc)})
            return
        _log(f"projects config secret reset project={key}")
        _audit_event("projects_config_secret_reset", actor=self.client_address[0], target=key, success=True)
        self._write_json(200, _projects_config_payload(include_secret=True))

    def _handle_docker_logs(self, query: dict[str, list[str]]) -> None:
        try:
            container = _validate_docker_container(query.get("container", [""])[0])
            raw_tail = query.get("tail", ["200"])[0]
            tail = _docker_log_line_limit(raw_tail)
            lines = _docker_logs(container, tail=tail)
        except ValueError as exc:
            self._write_json(400, {"error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - return Docker/runtime errors to the dashboard
            self._write_json(500, {"error": "docker_logs_failed", "detail": str(exc)})
            return
        self._write_json(200, {"container": container, "lines": lines})

    def _handle_docker_logs_download(self, query: dict[str, list[str]]) -> None:
        try:
            container = _validate_docker_container(query.get("container", [""])[0])
            raw_lines = (query.get("lines", [""])[0] or "").strip().lower()
            all_lines = raw_lines in {"all", "0", "-1"} or (query.get("all", [""])[0] or "").strip() == "1"
            lines = None if all_lines else _docker_log_line_limit(raw_lines)
            text, truncated = _docker_logs_text(container, lines=lines, all_lines=all_lines)
        except ValueError as exc:
            self._write_json(400, {"error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - return Docker/runtime errors to the dashboard
            self._write_json(500, {"error": "docker_logs_failed", "detail": str(exc)})
            return
        suffix = "all" if all_lines else f"{lines}-lines"
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

        project = _project_for_webhook(payload, query)
        if not project:
            _log(f"webhook rejected from {self.client_address[0]}: project not found")
            self._write_json(404, {"error": "project_not_found"})
            return
        if not project.enabled:
            ignored = {"project_key": project.key, "reason": "project_disabled", "at": _now_text()}
            _update_state(last_ignored_webhook=ignored)
            _log(f"webhook ignored project={project.key}: project disabled")
            self._write_json(202, {"status": "ignored", "project": project.key, "reason": "project_disabled"})
            return
        if not _valid_signature_for_project(project, self.headers, body, query):
            _log(f"webhook rejected from {self.client_address[0]}: invalid secret project={project.key}")
            self._write_json(403, {"error": "invalid_secret"})
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
        except queue.Full:
            self._write_json(429, {"error": "deploy_queue_full"})
            return

        _update_state(last_webhook_at=_now_text(), queue_size=_jobs.qsize())
        _log(f"webhook accepted project={project.key} ref={ref} after={job['after']}")
        self._write_json(202, {"status": "queued", "project": project.key, "queue_size": _jobs.qsize()})

    def log_message(self, fmt: str, *args: Any) -> None:
        message = fmt % args
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


def _project_script_path(project: DeployProject, action: str = "deploy") -> Path:
    script_text = project.rollback_script if action == "rollback" and project.rollback_script else project.script
    script = Path(script_text)
    return script if script.is_absolute() else project.workdir / script


def _script_is_executable(path: Path) -> bool:
    if os.name == "nt":
        return path.is_file()
    return path.is_file() and os.access(path, os.X_OK)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "hash-password":
        _print_password_hash()
        return

    enabled_projects = [project for project in PROJECTS.values() if project.enabled]
    missing_secrets = [project.key for project in enabled_projects if not project.webhook_secret]
    if missing_secrets:
        raise SystemExit(f"webhook secret missing for projects: {', '.join(missing_secrets)}")
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
        raise SystemExit(f"rollback script is not executable, run chmod +x: {', '.join(non_executable_rollback_scripts)}")

    _read_state()
    threading.Thread(target=_worker, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    _log(f"deploy agent listening on {HOST}:{PORT}, projects={len(PROJECTS)} default={DEFAULT_PROJECT_KEY}")
    server.serve_forever()


if __name__ == "__main__":
    main()
