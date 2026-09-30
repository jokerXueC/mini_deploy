"""Bounded, privacy-conscious request records from the selected Nginx runtime."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import nginx_runtime
from certificates import CertificateError, CertificateStore, atomic_write, run, trusted_path


MARKER = "# mini-deploy-managed: request-log-v1"
CONFIG_NAME = "00-mini-deploy-request-log.conf"
LOCAL_LOG = Path("/var/log/nginx/mini-deploy-requests.log")
PREFIX = "mini_deploy_req "
MAX_RECORDS = 300


def config_path(runtime: nginx_runtime.Runtime) -> Path:
    return runtime.conf_root / CONFIG_NAME


def config_text(mode: str) -> str:
    target = "/dev/stdout" if mode == "docker" else LOCAL_LOG.as_posix()
    return "\n".join([
        MARKER,
        "log_format mini_deploy_request_v1 escape=json",
        "    'mini_deploy_req {\"at\":\"$time_iso8601\",\"host\":\"$host\",\"method\":\"$request_method\",'",
        "    '\"path\":\"$uri\",\"status\":\"$status\",\"duration\":\"$request_time\",'",
        "    '\"upstream\":\"$upstream_response_time\"}';",
        f"access_log {target} mini_deploy_request_v1;",
        "",
    ])


def enabled(runtime: nginx_runtime.Runtime) -> bool:
    path = config_path(runtime)
    trusted_path(path)
    if not path.exists():
        return False
    if not path.is_file() or path.stat().st_size > 8192:
        raise CertificateError("请求日志配置不是普通小文件，请人工检查")
    return path.read_text(encoding="utf-8") == config_text(runtime.mode)


def enable(runtime: nginx_runtime.Runtime) -> None:
    path = config_path(runtime)
    trusted_path(path)
    if path.exists():
        if enabled(runtime):
            return
        raise CertificateError("已有同名 Nginx 配置且内容不同，不会覆盖")
    if runtime.mode == "local" and not LOCAL_LOG.parent.is_dir():
        raise CertificateError("本机 Nginx 日志目录不存在，请先检查 Nginx 安装")
    CertificateStore(runtime.data_home / "certificates", runtime).commit_config(path, config_text(runtime.mode), "")


def disable(runtime: nginx_runtime.Runtime) -> None:
    if not enabled(runtime):
        if config_path(runtime).exists():
            raise CertificateError("同名 Nginx 配置不是面板托管内容，不会删除")
        return
    path = config_path(runtime)
    previous = config_text(runtime.mode)
    path.unlink()
    try:
        run(runtime.command("test"))
        run(runtime.command("reload"))
    except CertificateError as exc:
        atomic_write(path, previous)
        try:
            run(runtime.command("test"))
            run(runtime.command("reload"))
        except CertificateError as rollback_error:
            raise CertificateError("请求日志配置已恢复，但 Nginx 重新加载失败，请检查服务") from rollback_error
        raise CertificateError("Nginx 检查或加载失败，已恢复请求日志配置") from exc


def parse(line: str) -> dict[str, Any] | None:
    if not line.startswith(PREFIX) or len(line) > 8192:
        return None
    try:
        payload = json.loads(line[len(PREFIX):])
        if not isinstance(payload, dict):
            return None
        status = int(payload["status"])
        duration = float(payload["duration"])
        upstream = str(payload.get("upstream", ""))
        if not 100 <= status <= 599 or not math.isfinite(duration) or duration < 0:
            return None
        method, path, host, at = (str(payload[key]) for key in ("method", "path", "host", "at"))
        if (not re.fullmatch(r"[A-Z]{1,16}", method) or not path.startswith("/") or
                len(path) > 2048 or len(host) > 253 or len(at) > 64 or
                any(ord(char) < 32 for char in path + host + at)):
            return None
        upstream_ms = None
        if re.fullmatch(r"\d+(?:\.\d+)?", upstream):
            value = float(upstream)
            if math.isfinite(value):
                upstream_ms = round(value * 1000, 1)
        return {"at": at, "host": host, "method": method, "path": path, "status": status,
                "duration_ms": round(duration * 1000, 1), "upstream_ms": upstream_ms}
    except (KeyError, ValueError, TypeError, OverflowError):
        return None


def _local_lines(path: Path) -> list[str]:
    if path.is_symlink() or not path.is_file():
        return []
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 1024 * 1024))
            return stream.read(1024 * 1024).decode("utf-8", errors="replace").splitlines()[-1000:]
    except OSError as exc:
        raise CertificateError("无法读取本机 Nginx 请求日志，请检查文件权限") from exc


def _docker_lines(container_id: str) -> list[str]:
    try:
        result = subprocess.run(["docker", "logs", "--tail", "1000", container_id],
                                capture_output=True, timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CertificateError("无法读取 Nginx 容器日志，请检查 Docker 状态") from exc
    if result.returncode:
        raise CertificateError("Docker 日志读取失败，请检查容器日志驱动")
    output = (result.stdout + b"\n" + result.stderr)[-2 * 1024 * 1024:]
    return output.decode("utf-8", errors="replace").splitlines()[-1000:]


def recent(runtime: nginx_runtime.Runtime, limit: int = 100) -> dict[str, Any]:
    if not 1 <= limit <= MAX_RECORDS:
        raise ValueError("请求数量必须在 1-300 之间")
    active = enabled(runtime)
    lines = (_docker_lines(runtime.container_id) if runtime.mode == "docker" else _local_lines(LOCAL_LOG)) if active else []
    records = [item for line in lines if (item := parse(line)) is not None]
    return {"mode": runtime.mode, "container": runtime.profile.get("container", "") if runtime.mode == "docker" else "",
            "enabled": active, "records": records[-limit:][::-1], "limit": limit,
            "notice": ("启用后只记录经过所选 Nginx 的请求；单独配置 access_log 的站点可能不会写入这份日志。"
                       if active else "尚未开启请求记录；启用后只采集新请求，不修改已有站点配置。")}
