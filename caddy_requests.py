"""Read access records from a verified local Docker Caddy container."""

from __future__ import annotations

import json
import math
import re
import subprocess
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import nginx_runtime
from certificates import CertificateError, run


MAX_RECORDS = 300
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}")
CONTAINER_ID = re.compile(r"[a-f0-9]{64}")


def _verified_id(name: str) -> str:
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise CertificateError("Caddy 容器名称无效")
    nginx_runtime.require_local_docker()
    fmt = '{"id":{{json .Id}},"running":{{json .State.Running}}}'
    try:
        item = json.loads(run(["docker", "inspect", "--type", "container", "--format", fmt, name], timeout=3))
    except (ValueError, TypeError) as exc:
        raise CertificateError("无法读取 Caddy 容器信息") from exc
    if not isinstance(item, dict) or not CONTAINER_ID.fullmatch(str(item.get("id", ""))) or not item.get("running"):
        raise CertificateError("Caddy 容器未运行或信息无效")
    run(["docker", "exec", item["id"], "caddy", "version"], timeout=3)
    return item["id"]


def discover() -> list[str]:
    nginx_runtime.require_local_docker()
    names = run(["docker", "ps", "--format", "{{.Names}}"], timeout=5).splitlines()
    result = []
    for name in names[:20]:
        if "caddy" not in name.casefold() or not NAME.fullmatch(name):
            continue
        try:
            _verified_id(name)
        except CertificateError:
            continue
        result.append(name)
    return result


def parse(line: str) -> dict[str, Any] | None:
    if len(line) > 8192 or not line.startswith("{"):
        return None
    try:
        item = json.loads(line)
        if not isinstance(item, dict) or not (item.get("logger") == "http.log.access" or
                                               str(item.get("logger", "")).startswith("http.log.access.")):
            return None
        request = item["request"]
        if not isinstance(request, dict):
            return None
        raw_status, raw_duration, raw_ts = item["status"], item["duration"], item["ts"]
        if any(isinstance(value, bool) for value in (raw_status, raw_duration, raw_ts)):
            return None
        status, duration, timestamp = int(raw_status), float(raw_duration), float(raw_ts)
        if (not 100 <= status <= 599 or not math.isfinite(duration) or duration < 0 or
                not math.isfinite(timestamp) or not 0 <= timestamp < 253402300800):
            return None
        method, host, uri = (request[key] for key in ("method", "host", "uri"))
        if (not all(isinstance(value, str) for value in (method, host, uri)) or not uri.startswith("/") or
                any(ord(char) < 32 for char in uri + host)):
            return None
        path = urlsplit(uri).path
        if (not re.fullmatch(r"[A-Z]{1,16}", method) or not path.startswith("/") or
                len(path) > 2048 or len(host) > 253 or
                any(ord(char) < 32 for char in path + host)):
            return None
        at = datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        return {"at": at, "host": host, "method": method, "path": path, "status": status,
                "duration_ms": round(duration * 1000, 1), "upstream_ms": None}
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def recent(limit: int = 100, container: str = "") -> dict[str, Any]:
    if not 1 <= limit <= MAX_RECORDS:
        raise ValueError("请求数量必须在 1-300 之间")
    candidates = discover() if not container else []
    selected = container or (candidates[0] if len(candidates) == 1 else "")
    if not selected:
        notice = "未发现 Caddy 容器" if not candidates else "检测到多个 Caddy 容器，请选择一个"
        return {"mode": "caddy", "container": "", "candidates": candidates, "records": [], "limit": limit,
                "notice": notice}
    container_id = _verified_id(selected)
    try:
        result = subprocess.run(["docker", "logs", "--tail", "1000", container_id],
                                capture_output=True, timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CertificateError("无法读取 Caddy 容器日志，请检查 Docker 状态") from exc
    if result.returncode:
        raise CertificateError("Caddy 容器日志读取失败，请检查日志驱动")
    lines = (result.stdout + b"\n" + result.stderr)[-2 * 1024 * 1024:].decode("utf-8", errors="replace").splitlines()
    records = [record for line in lines[-1000:] if (record := parse(line)) is not None]
    records.sort(key=lambda record: record["at"], reverse=True)
    return {"mode": "caddy", "container": selected, "candidates": candidates or [selected],
            "records": records[:limit], "limit": limit,
            "notice": ("只显示 Caddy 已启用访问日志后经过该容器的请求；路径不含查询参数。"
                       if records else "暂未发现 Caddy 访问日志；请确认站点已启用 JSON 访问日志并产生新请求。")}
