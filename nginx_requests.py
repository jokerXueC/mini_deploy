"""Bounded, privacy-conscious request records from the selected Nginx runtime."""

from __future__ import annotations

import json
import ipaddress
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


def config_text(mode: str, *, details: bool = True, diagnostics: bool = True, peer: bool = False) -> str:
    target = "/dev/stdout" if mode == "docker" else LOCAL_LOG.as_posix()
    metadata = [
        *(["    '\"peer_ip\":\"$realip_remote_addr\",'"] if peer else []),
        "    '\"client_ip\":\"$remote_addr\",\"protocol\":\"$server_protocol\",'",
        "    '\"response_bytes\":\"$body_bytes_sent\",\"upstream_address\":\"$upstream_addr\",'",
        "    '\"request_headers\":{\"Host\":\"$http_host\",\"User-Agent\":\"$http_user_agent\",'",
        "    '\"Accept\":\"$http_accept\",\"Accept-Language\":\"$http_accept_language\",'",
        "    '\"Accept-Encoding\":\"$http_accept_encoding\",\"Content-Type\":\"$http_content_type\",'",
        "    '\"Content-Length\":\"$http_content_length\",\"X-Request-ID\":\"$http_x_request_id\",'",
        "    '\"Traceparent\":\"$http_traceparent\",\"X-Forwarded-For\":\"$http_x_forwarded_for\",'",
        "    '\"X-Real-IP\":\"$http_x_real_ip\"}}';",
    ] if details else []
    diagnostic_fields = [
        "    '\"request_id\":\"$mini_deploy_request_id\",\"upstream_status\":\"$upstream_status\",'",
        "    '\"upstream_connect\":\"$upstream_connect_time\",\"upstream_header\":\"$upstream_header_time\",'",
    ] if details and diagnostics else []
    request_id_map = [
        "map $http_x_request_id $mini_deploy_request_id {",
        "    default $request_id;",
        '    "~^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$" $http_x_request_id;',
        "}",
    ] if diagnostic_fields else []
    return "\n".join([
        MARKER,
        *request_id_map,
        "log_format mini_deploy_request_v1 escape=json",
        "    'mini_deploy_req {\"at\":\"$time_iso8601\",\"host\":\"$host\",\"method\":\"$request_method\",'",
        "    '\"path\":\"$uri\",\"status\":\"$status\",\"duration\":\"$request_time\",'",
        ("    '\"upstream\":\"$upstream_response_time\",'" if details else
         "    '\"upstream\":\"$upstream_response_time\"}';"),
        *diagnostic_fields,
        *metadata,
        f"access_log {target} mini_deploy_request_v1;",
        "",
    ])


def managed_configs(mode: str) -> tuple[str, ...]:
    return (config_text(mode), config_text(mode, diagnostics=False), config_text(mode, details=False))


def enabled(runtime: nginx_runtime.Runtime) -> bool:
    path = config_path(runtime)
    trusted_path(path)
    if not path.exists():
        return False
    if not path.is_file() or path.stat().st_size > 8192:
        raise CertificateError("请求日志配置不是普通小文件，请人工检查")
    return path.read_text(encoding="utf-8") in managed_configs(runtime.mode)


def enable(runtime: nginx_runtime.Runtime) -> None:
    path = config_path(runtime)
    trusted_path(path)
    if path.exists():
        if enabled(runtime):
            previous = path.read_text(encoding="utf-8")
            if previous != config_text(runtime.mode):
                CertificateStore(runtime.data_home / "certificates", runtime).commit_config(path, config_text(runtime.mode), previous)
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
    previous = path.read_text(encoding="utf-8")
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


HEADER_NAMES = {name.lower(): name for name in (
    "Host", "User-Agent", "Accept", "Accept-Language", "Accept-Encoding", "Content-Type",
    "Content-Length", "X-Request-ID", "Traceparent", "X-Forwarded-For", "X-Real-IP")}

REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def upstream_values(raw: Any, *, status: bool = False) -> list[int | float | None]:
    if not isinstance(raw, str) or len(raw) > 256:
        return []
    parts = re.split(r"\s*(?:,|:)\s*", raw.strip())
    if len(parts) > 16:
        return []
    values = []
    for part in parts:
        if part == "-":
            values.append(None)
        elif status and re.fullmatch(r"[1-5][0-9]{2}", part):
            values.append(int(part))
        elif not status and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", part):
            value = float(part) * 1000
            values.append(round(value, 2) if math.isfinite(value) else None)
        else:
            return []
    return values


def upstream_attempts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    fields = {
        "status": upstream_values(payload.get("upstream_status"), status=True),
        "connect_ms": upstream_values(payload.get("upstream_connect")),
        "header_ms": upstream_values(payload.get("upstream_header")),
        "response_ms": upstream_values(payload.get("upstream")),
    }
    count = max(map(len, fields.values()))
    attempts = []
    for index in range(count):
        attempt = {name: values[index] if index < len(values) else None for name, values in fields.items()}
        connect, header, response = attempt["connect_ms"], attempt["header_ms"], attempt["response_ms"]
        attempt["wait_ms"] = round(header - connect, 2) if connect is not None and header is not None and header >= connect else None
        attempt["transfer_ms"] = round(response - header, 2) if header is not None and response is not None and response >= header else None
        attempts.append(attempt)
    return attempts


def request_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Bound metadata before persistence; credentials and arbitrary headers are never retained."""
    result: dict[str, Any] = {}
    for key in ("client_ip", "peer_ip"):
        value = payload.get(key)
        try:
            if isinstance(value, str):
                result[key] = str(ipaddress.ip_address(value))
        except ValueError:
            pass
    protocol = payload.get("protocol")
    if isinstance(protocol, str) and protocol in {"HTTP/1.0", "HTTP/1.1", "HTTP/2", "HTTP/2.0", "HTTP/3", "HTTP/3.0"}:
        result["protocol"] = protocol
    size = payload.get("response_bytes")
    if not isinstance(size, bool) and isinstance(size, (str, int)) and re.fullmatch(r"[0-9]{1,16}", str(size)):
        result["response_bytes"] = int(size)
    address = payload.get("upstream_address")
    if isinstance(address, str) and address != "-" and not any(ord(char) < 32 for char in address):
        result["upstream_address"] = address[:256]
    headers = payload.get("request_headers")
    if isinstance(headers, dict):
        safe, remaining = {}, 2048
        for raw_name, value in headers.items():
            name = HEADER_NAMES.get(str(raw_name).lower())
            if not name or remaining <= 0:
                continue
            if isinstance(value, list):
                value = ", ".join(part[:256] for part in value[:4] if isinstance(part, str))
            if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
                continue
            value = value[:min(256, remaining)]
            safe[name] = value
            remaining -= len(value)
        result["request_headers"] = safe
    incoming_id = result.get("request_headers", {}).get("X-Request-ID", "")
    identifier = payload.get("request_id") or incoming_id
    if isinstance(identifier, str) and REQUEST_ID.fullmatch(identifier):
        result["request_id"] = identifier
        result["request_id_source"] = "incoming" if identifier == incoming_id else "generated"
    if any(key in payload for key in ("upstream_status", "upstream_connect", "upstream_header")):
        result["upstream_attempts"] = upstream_attempts(payload)
    return result


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
                "duration_ms": round(duration * 1000, 1), "upstream_ms": upstream_ms,
                **request_metadata(payload)}
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
            "enabled": active,
            "details_enabled": active and config_path(runtime).read_text(encoding="utf-8") == config_text(runtime.mode),
            "records": records[-limit:][::-1], "limit": limit,
            "notice": ("启用后只记录经过所选 Nginx 的请求；单独配置 access_log 的站点可能不会写入这份日志。"
                       if active else "尚未开启请求记录；启用后只采集新请求，不修改已有站点配置。")}
