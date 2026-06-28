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
import re
import secrets
import shlex
import shutil
import signal
import smtplib
import ssl
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from email.message import EmailMessage
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse
from urllib.request import Request, urlopen


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
SYSTEM_METRIC_INTERVAL_SECONDS = int(os.getenv("DEPLOY_SYSTEM_METRIC_INTERVAL_SECONDS", str(30 * 60)))
SYSTEM_METRIC_MAX_POINTS = int(os.getenv("DEPLOY_SYSTEM_METRIC_MAX_POINTS", "336"))
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
        webhook_secret=str(raw.get("webhook_secret") or raw.get("secret") or WEBHOOK_SECRET),
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
    "system_metrics": [],
}
_login_failures: dict[str, list[float]] = {}
_system_status_cache: dict[str, Any] = {"at": 0.0, "payload": {}}
_system_status_lock = threading.Lock()
_notifications_lock = threading.Lock()
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


def _load_config_file() -> dict[str, Any]:
    if not PROJECTS_CONFIG_FILE.exists():
        return {}
    try:
        raw = json.loads(PROJECTS_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - keep agent bootable on bad config
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} projects config load failed: {exc}", flush=True)
        return {}
    return raw if isinstance(raw, dict) else {"projects": raw}


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


NOTIFICATIONS = _load_notifications()


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
    return [_project_to_config(project, include_secret=include_secret) for project in PROJECTS.values()]


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
        service_name=_safe_project_key(service_name),
        service_port=_int_config(raw.get("service_port") or raw.get("port"), existing.service_port if existing else 8000),
        start_command=start_command,
        app_domain=_normalize_domain(app_domain),
        app_https=_bool_config(app_https_raw, existing.app_https if existing else False),
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


def _write_projects_config(projects: dict[str, DeployProject], notifications: dict[str, Any] | None = None) -> None:
    backup_path = _backup_projects_config()
    PROJECTS_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "projects": [_project_to_config(project, include_secret=True) for project in projects.values()],
        "notifications": _notification_config_from_raw(
            notifications if notifications is not None else _notification_config_payload(include_secret=True),
        ),
    }
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


def _replace_notifications(notifications: dict[str, Any]) -> None:
    global NOTIFICATIONS
    with _notifications_lock:
        NOTIFICATIONS = _notification_config_from_raw(notifications)


def _save_and_reload_notifications(notifications: dict[str, Any]) -> None:
    parsed = _notification_config_from_raw(notifications, existing=_notification_config_payload(include_secret=True))
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
            "if [ -x ./gradlew ]; then ./gradlew clean build -x test; else mvn clean package -DskipTests; fi",
            "mkdir -p target/deploy",
            "jar_file=$(find target -maxdepth 1 -name '*.jar' ! -name '*sources.jar' ! -name '*javadoc.jar' | head -n 1)",
            'if [ -z "${jar_file:-}" ]; then echo "未找到 target/*.jar"; exit 1; fi',
            "cp \"$jar_file\" target/deploy/app.jar",
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
    return {"step": step, "ok": ok, "detail": detail, "output": output}


def _run_bootstrap_command(command: list[str], *, step: str, timeout: float = 60.0) -> dict[str, Any]:
    code, output = _run_command(command, timeout=timeout)
    return _bootstrap_result(step, code == 0, "ok" if code == 0 else f"exit code {code}", output)


def _bootstrap_project(project: DeployProject, *, write_service: bool = True) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    service_written = False
    script_path = _project_script_path(project)

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
            "message": "服务器无法访问仓库，请先把 SSH 公钥添加到代码平台 Deploy Key / SSH Key。",
        }

    try:
        project.workdir.parent.mkdir(parents=True, exist_ok=True)
        if (project.workdir / ".git").is_dir():
            results.append(_run_bootstrap_command(["git", "-C", str(project.workdir), "fetch", "origin", project.branch], step="git_fetch", timeout=60.0))
            results.append(_run_bootstrap_command(["git", "-C", str(project.workdir), "checkout", project.branch], step="git_checkout", timeout=30.0))
            results.append(_run_bootstrap_command(["git", "-C", str(project.workdir), "pull", "--ff-only", "origin", project.branch], step="git_pull", timeout=60.0))
        else:
            results.append(_run_bootstrap_command(["git", "clone", "--branch", project.branch, project.repo, str(project.workdir)], step="git_clone", timeout=180.0))
        if not all(item["ok"] for item in results):
            return {"ok": False, "project": project.key, "results": results, "ssh_public_keys": _ssh_public_keys()}

        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(_deploy_script_text(project), encoding="utf-8")
        os.chmod(script_path, 0o755)
        results.append(_bootstrap_result("deploy_script", True, f"已写入 {script_path}"))

        project.deploy_log_file.parent.mkdir(parents=True, exist_ok=True)
        project.deploy_log_file.touch(exist_ok=True)
        results.append(_bootstrap_result("log_file", True, f"已准备 {project.deploy_log_file}"))

        if write_service and project.template in {"python", "go", "java"}:
            service_path = Path("/etc/systemd/system") / f"{project.service_name}.service"
            service_path.write_text(_systemd_service_text(project), encoding="utf-8")
            os.chmod(service_path, 0o644)
            service_written = True
            results.append(_bootstrap_result("systemd_service", True, f"已写入 {service_path}"))
            results.append(_run_bootstrap_command(["systemctl", "daemon-reload"], step="systemd_daemon_reload", timeout=30.0))
            results.append(_run_bootstrap_command(["systemctl", "enable", project.service_name], step="systemd_enable", timeout=30.0))
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


def _project_nginx_conf_path(project: DeployProject) -> Path:
    return Path("/etc/nginx/conf.d") / f"mini-deploy-{project.key}.conf"


def _project_nginx_config_text(project: DeployProject) -> str:
    domain = _normalize_domain(project.app_domain)
    port = project.service_port or 8000
    return "\n".join([
        "server {",
        "    listen 80;",
        f"    server_name {domain};",
        "",
        "    client_max_body_size 50m;",
        "",
        "    location / {",
        f"        proxy_pass http://127.0.0.1:{port};",
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


def _configure_project_nginx(project: DeployProject, *, issue_https: bool | None = None) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    domain = _normalize_domain(project.app_domain)
    https_requested = project.app_https if issue_https is None else bool(issue_https)
    conf_path = _project_nginx_conf_path(project)
    backup_path: Path | None = None

    if not domain:
        return {"ok": False, "project": project.key, "results": [_bootstrap_result("domain", False, "请先填写业务域名")]}
    if not _valid_domain(domain):
        return {"ok": False, "project": project.key, "results": [_bootstrap_result("domain", False, f"域名格式不正确: {domain}")]}
    if not 1 <= int(project.service_port or 0) <= 65535:
        return {"ok": False, "project": project.key, "results": [_bootstrap_result("service_port", False, "服务端口必须在 1-65535 之间")]}
    if not shutil.which("nginx"):
        return {
            "ok": False,
            "project": project.key,
            "results": [_bootstrap_result("nginx", False, "服务器未安装 Nginx", "Ubuntu/Debian: apt install -y nginx\nCentOS/Rocky: dnf install -y nginx")],
        }

    try:
        conf_path.parent.mkdir(parents=True, exist_ok=True)
        if conf_path.exists():
            backup_path = conf_path.with_suffix(f".conf.{time.strftime('%Y%m%d-%H%M%S')}.bak")
            shutil.copy2(conf_path, backup_path)
        conf_path.write_text(_project_nginx_config_text(project), encoding="utf-8")
        os.chmod(conf_path, 0o644)
        results.append(_bootstrap_result("nginx_config", True, f"已写入 {conf_path}"))

        test = _run_bootstrap_command(["nginx", "-t"], step="nginx_test", timeout=20.0)
        results.append(test)
        if not test["ok"]:
            if backup_path and backup_path.exists():
                shutil.copy2(backup_path, conf_path)
            else:
                try:
                    conf_path.unlink()
                except OSError:
                    pass
            results.append(_bootstrap_result("nginx_restore", True, "Nginx 测试失败，已恢复旧配置"))
            return {"ok": False, "project": project.key, "domain": domain, "conf": str(conf_path), "results": results}

        reload_result = _run_bootstrap_command(["systemctl", "reload", "nginx"], step="nginx_reload", timeout=20.0)
        if not reload_result["ok"]:
            reload_result = _run_bootstrap_command(["systemctl", "restart", "nginx"], step="nginx_restart", timeout=30.0)
        results.append(reload_result)

        if https_requested:
            if not shutil.which("certbot"):
                results.append(_bootstrap_result("certbot", False, "未安装 certbot，HTTP 已可用；如需 HTTPS 请先安装 certbot"))
            else:
                results.append(_run_bootstrap_command([
                    "certbot",
                    "--nginx",
                    "-d",
                    domain,
                    "--non-interactive",
                    "--agree-tos",
                    "--register-unsafely-without-email",
                ], step="certbot_https", timeout=180.0))
    except Exception as exc:  # noqa: BLE001 - return actionable Nginx failure to UI
        results.append(_bootstrap_result("nginx", False, str(exc)))

    http_ready = any(item["step"] in {"nginx_reload", "nginx_restart"} and item["ok"] for item in results)
    certbot_items = [item for item in results if item["step"] == "certbot_https"]
    https_ok = bool(certbot_items) and all(item["ok"] for item in certbot_items)
    ok = http_ready and (not https_requested or https_ok)
    return {
        "ok": ok,
        "http_ready": http_ready,
        "https_ok": https_ok,
        "project": project.key,
        "domain": domain,
        "url": f"{'https' if https_ok else 'http'}://{domain}",
        "conf": str(conf_path),
        "results": results,
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
        server = {
            "cpu_percent": _cpu_percent(),
            "load": _load_average(),
            "memory": _memory_status(),
            "disk": _disk_status(Path("/")),
            "network": _network_status(),
            "sampled_at": _now_text(),
            "cache_seconds": SYSTEM_STATUS_CACHE_SECONDS,
        }
        history = _record_system_metric(server, now)
        payload = {
            "server": server,
            "docker": _docker_status(),
            "history": history,
            "history_interval_seconds": SYSTEM_METRIC_INTERVAL_SECONDS,
            "history_max_points": SYSTEM_METRIC_MAX_POINTS,
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


def _deploy_lock_status(state: dict[str, Any]) -> dict[str, Any]:
    with _deploy_process_lock:
        proc = _deploy_process
        active_pid = proc.pid if proc is not None and proc.poll() is None else None
    current = state.get("current_deploy") if isinstance(state.get("current_deploy"), dict) else {}
    locked = _running_lock.locked()
    duration = current.get("duration_seconds")
    return {
        "locked": locked,
        "active_pid": active_pid,
        "active_process": active_pid is not None,
        "duration_seconds": duration,
        "project_key": current.get("project_key") or "",
        "phase": current.get("phase") or "",
        "phase_label": current.get("phase_label") or "",
        "can_force_unlock": bool(locked and active_pid is None),
    }


def _force_unlock_deploy() -> tuple[bool, dict[str, Any]]:
    with _deploy_process_lock:
        proc = _deploy_process
        active_pid = proc.pid if proc is not None and proc.poll() is None else None
    if active_pid is not None:
        return False, {
            "error": "deploy_process_running",
            "active_pid": active_pid,
            "suggested_commands": [
                f"ps -fp {active_pid}",
                f"kill {active_pid}",
                "systemctl restart mini-deploy-agent",
            ],
        }
    if not _running_lock.locked():
        return True, {"ok": True, "message": "deploy lock is already clear"}
    try:
        _running_lock.release()
    except RuntimeError:
        pass
    _update_state(running=False, current_deploy=None, queue_size=_jobs.qsize())
    return True, {"ok": True, "message": "stale deploy lock cleared"}


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
        alerts.append(_alert("critical", "Deployment lock may be stale", "No active deploy process was found, but the lock is still held.", "deploy", "systemctl restart mini-deploy-agent"))
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
    system_payload = _system_status_payload()
    lock = _deploy_lock_status(state)
    alerts = _alerts_payload(system_payload, state, lock)
    events = _events_payload(state, system_payload, alerts)
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
        "phase_started_ts": started_ts,
        "phase_durations": [],
        "changed_files": job.get("changed_files") or [],
        "changed_file_count": job.get("changed_file_count") or 0,
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
        env["DEPLOY_SERVICE_NAME"] = project.service_name
        env["DEPLOY_SERVICE_PORT"] = str(project.service_port)
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
        _append_history(entry)
        _notify_deploy_finished(entry)
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
            _log(f"projects bootstrap config save failed: {exc}")
            _audit_event("projects_config_bootstrap", actor=self.client_address[0], target=project.key, success=False, detail={"error": str(exc)})
            self._write_json(500, {"error": "config_write_failed", "detail": str(exc)})
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
            _log(f"projects nginx config save failed: {exc}")
            _audit_event("projects_config_nginx", actor=self.client_address[0], target=project.key, success=False, detail={"error": str(exc)})
            self._write_json(500, {"error": "config_write_failed", "detail": str(exc)})
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
            service_name=project.service_name,
            service_port=project.service_port,
            start_command=project.start_command,
            app_domain=project.app_domain,
            app_https=project.app_https,
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
            config = _notification_config_from_raw(raw, existing=_notification_config_payload(include_secret=True))
            _save_and_reload_notifications(config)
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
