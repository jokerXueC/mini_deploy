"""Read-only Docker lifecycle history; incident evaluation belongs to container monitoring.

Call run() in one caller-owned worker. Only container_state['event:docker']
and coverage['docker_events'] belong to this observer; no faults are changed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time

from container_monitoring import container_identity


MAX_EVENT_IDS = 2000
MAX_LINE_BYTES = 16384
MAX_TIME_NANO = (1 << 63) - 1
MAX_FUTURE_SECONDS = 5
HEARTBEAT_SECONDS = 15
STATE_KEY = "event:docker"
ACTIONS = frozenset({"start", "die", "oom", "health_status", "restart", "stop", "pause", "unpause", "destroy"})
HEALTH_STATUSES = frozenset({"starting", "healthy", "unhealthy"})
ATTRIBUTE_FIELDS = {
    "name": "container",
    "com.docker.compose.project": "compose_project",
    "com.docker.compose.service": "compose_service",
}
ACTION_TEXT = {"start": "启动", "die": "退出", "oom": "内存不足（OOM）", "health_status": "健康状态变化",
               "restart": "重启", "stop": "停止", "pause": "暂停", "unpause": "恢复运行", "destroy": "删除"}
HEALTH_TEXT = {"starting": "启动中", "healthy": "健康", "unhealthy": "不健康"}
LIVE_DETAIL = "正在接收 Docker 容器事件；Docker 历史回放有数量限制，无法保证历史完整。"
# Never serialize .Actor.Attributes wholesale: labels can contain credentials.
EVENT_FORMAT = (
    '{"Type":{{json .Type}},"Action":{{json .Action}},'
    '"Actor":{"ID":{{json .Actor.ID}},"Attributes":{'
    '"name":{{json (index .Actor.Attributes "name")}},'
    '"com.docker.compose.project":{{json (index .Actor.Attributes "com.docker.compose.project")}},'
    '"com.docker.compose.service":{{json (index .Actor.Attributes "com.docker.compose.service")}},'
    '"com.docker.compose.container-number":{{json (index .Actor.Attributes "com.docker.compose.container-number")}},'
    '"com.docker.compose.oneoff":{{json (index .Actor.Attributes "com.docker.compose.oneoff")}},'
    '"exitCode":{{json (index .Actor.Attributes "exitCode")}}}},'
    '"time":{{json .Time}},"timeNano":{{json .TimeNano}}}'
)


def _integer(value, maximum):
    if isinstance(value, str):
        if len(value) > 20 or not re.fullmatch(r"[0-9]+", value):
            return None
        value = int(value)
    if type(value) is int and 0 <= value <= maximum:
        return value
    return None


def _normalize(payload):
    if not isinstance(payload, dict) or payload.get("Type", "container") != "container":
        return None
    action = payload.get("Action")
    if not isinstance(action, str) or len(action) > 32:
        return None
    base, separator, health = action.partition(": ")
    if base not in ACTIONS or (separator and (base != "health_status" or health not in HEALTH_STATUSES)):
        return None
    actor = payload.get("Actor")
    if not isinstance(actor, dict):
        return None
    identifier = actor.get("ID")
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-fA-F]{12,64}", identifier):
        return None
    stamp = _integer(payload.get("timeNano"), MAX_TIME_NANO)
    if not stamp:
        seconds = _integer(payload.get("time"), MAX_TIME_NANO // 1_000_000_000)
        if not seconds:
            return None
        stamp = seconds * 1_000_000_000
    metadata = {"action": base, "container_id": identifier.lower(), "time_nano": stamp}
    if separator:
        metadata["health_status"] = health
    attributes = actor.get("Attributes", {})
    if isinstance(attributes, dict):
        for field, target in ATTRIBUTE_FIELDS.items():
            value = attributes.get(field)
            if (isinstance(value, str) and len(value) <= 256
                    and re.fullmatch(r"/?[a-zA-Z0-9][a-zA-Z0-9_.-]{0,254}", value)):
                metadata[target] = value.lstrip("/")
        replica = attributes.get("com.docker.compose.container-number")
        if isinstance(replica, str) and re.fullmatch(r"[0-9]{1,32}", replica):
            metadata["compose_replica"] = replica
        oneoff = attributes.get("com.docker.compose.oneoff")
        if isinstance(oneoff, str) and oneoff.lower() in {"true", "false"}:
            metadata["compose_oneoff"] = oneoff.lower() == "true"
        exit_code = _integer(attributes.get("exitCode"), 255)
        if exit_code is not None:
            metadata["exit_code"] = exit_code
    metadata["identity"] = container_identity({**metadata, "id": metadata["container_id"]})
    return metadata


def _ledger(monitor):
    ledger = monitor.container_state.get(STATE_KEY)
    if not isinstance(ledger, dict):
        ledger = monitor.container_state[STATE_KEY] = {}
    if not isinstance(ledger.get("ids"), dict):
        ledger["ids"] = {}
    return ledger


def consume(monitor, payload):
    """Record one allowlisted lifecycle event; periodic monitor flush persists it.

    Return False for invalid/duplicate input. The 2000-ID limit excludes the
    cursor timestamp in the aggregate event:docker ledger.
    """
    metadata = _normalize(payload)
    if metadata is None:
        return False
    identity = [metadata["container_id"], metadata["action"], metadata.get("health_status"), metadata["time_nano"]]
    event_id = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    with monitor.lock:
        if metadata["time_nano"] > int((monitor.clock() + MAX_FUTURE_SECONDS) * 1_000_000_000):
            return False
        ledger = _ledger(monitor)
        ids = ledger["ids"]
        if event_id in ids:
            return False
        name = metadata.get("container", metadata["container_id"][:12])
        action = metadata["action"]
        detail = "Docker 生命周期事件：" + ACTION_TEXT[action]
        if "health_status" in metadata:
            detail += "；健康状态：" + HEALTH_TEXT[metadata["health_status"]]
        if "exit_code" in metadata:
            detail += f"；退出码 {metadata['exit_code']}"
        monitor.record_event("lifecycle", f"容器{name}：{ACTION_TEXT[action]}", detail, "docker", metadata=metadata)
        # record_event timestamps receipt; replayed history must retain Docker's time.
        event = monitor.events[-1]
        event["at_ts"] = metadata["time_nano"] / 1_000_000_000
        event["at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event["at_ts"]))
        monitor._prune_events()
        ids[event_id] = metadata["time_nano"]
        ledger["cursor"] = max(_integer(ledger.get("cursor"), MAX_TIME_NANO) or 0, metadata["time_nano"])
        if len(ids) > MAX_EVENT_IDS:
            oldest = sorted(ids, key=lambda key: _integer(ids[key], MAX_TIME_NANO) or 0)
            for key in oldest[:len(ids) - MAX_EVENT_IDS]:
                del ids[key]
    return True


def _cursor(monitor):
    with monitor.lock:
        ledger = _ledger(monitor)
        cursor = max(_integer(ledger.get("cursor"), MAX_TIME_NANO) or 0,
                     max((_integer(value, MAX_TIME_NANO) or 0 for value in ledger["ids"].values()), default=0))
        if not cursor:
            cursor = int(monitor.clock() * 1_000_000_000)
        ledger["cursor"] = cursor
        return cursor


def _since(stamp):
    seconds, nanos = divmod(stamp, 1_000_000_000)
    return f"{seconds}.{nanos:09d}"


def _coverage(monitor, status, detail):
    with monitor.lock:
        monitor.coverage["docker_events"] = {"status": status, "detail": detail, "checked_at": monitor.clock()}


def _heartbeat(monitor):
    with monitor.lock:
        coverage = monitor.coverage.get("docker_events", {})
        if coverage.get("status") != "healthy":
            return
        now = monitor.clock()
        previous = coverage.get("checked_at", 0)
        if now - previous >= HEARTBEAT_SECONDS or now < previous:
            _coverage(monitor, "healthy", LIVE_DETAIL)


def _close(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def run(monitor_getter, *, which=shutil.which, popen=subprocess.Popen, pause=time.sleep, stop_event=None):
    """Follow container events in this thread; injected dependencies never require Docker in tests.

    stdout is a binary, unbuffered, nonblocking pipe. On idle streams the worker
    checks stop_event every 250ms, without creating another reader thread.
    """
    def stopped():
        return stop_event is not None and stop_event.is_set()

    def wait(seconds):
        if stop_event is not None and pause is time.sleep:
            stop_event.wait(seconds)
        else:
            pause(seconds)

    cursor = 0
    last_monitor = None
    try:
        while not stopped():
            try:
                monitor = monitor_getter()
                if monitor is None:
                    if last_monitor is not None:
                        _coverage(last_monitor, "unknown", "监测数据暂时不可用；5 秒后重试。")
                    wait(5)
                    continue
                last_monitor = monitor
                cursor = max(cursor, _cursor(monitor))
                executable = which("docker")
                if not executable:
                    _coverage(monitor, "not_installed", "未找到 Docker 命令；15 秒后重试。")
                    wait(15)
                    continue
                command = [executable, "events", "--filter", "type=container", "--format", EVENT_FORMAT,
                           "--since", _since(cursor)]
                with popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0) as process:
                    try:
                        descriptor = process.stdout.fileno()
                        os.set_blocking(descriptor, False)
                        _coverage(monitor, "healthy", LIVE_DETAIL)
                        pending, oversized = b"", False
                        while not stopped():
                            try:
                                chunk = os.read(descriptor, MAX_LINE_BYTES + 1)
                            except BlockingIOError:
                                _heartbeat(monitor)
                                wait(0.25)
                                continue
                            if not chunk:
                                break
                            parts = chunk.split(b"\n")
                            for index, part in enumerate(parts):
                                complete = index < len(parts) - 1
                                if not oversized:
                                    if len(pending) + len(part) > MAX_LINE_BYTES:
                                        pending, oversized = b"", True
                                    else:
                                        pending += part
                                if complete:
                                    if not oversized:
                                        try:
                                            payload = json.loads(pending)
                                        except (ValueError, UnicodeError, RecursionError):
                                            payload = None
                                        if isinstance(payload, dict):
                                            current = monitor_getter()
                                            if current is not None:
                                                monitor = last_monitor = current
                                                consume(monitor, payload)
                                                cursor = max(cursor, _cursor(monitor))
                                                _coverage(monitor, "healthy", LIVE_DETAIL)
                                            else:
                                                _coverage(monitor, "unknown", "监测数据暂时不可用；事件历史可能不完整。")
                                    pending, oversized = b"", False
                                if stopped():
                                    break
                            _heartbeat(monitor)
                    finally:
                        _close(process)
                _coverage(monitor, "unknown", "Docker 事件流已结束；5 秒后重连。历史回放可能存在缺失。")
            except Exception:
                # Loading corrupt monitor state or processing an event must not
                # permanently end coverage. Never retain exception/daemon text.
                if last_monitor is not None:
                    _coverage(last_monitor, "unknown", "Docker 事件监测暂时不可用；5 秒后重试。历史可能不完整。")
            if not stopped():
                wait(5)
    finally:
        if last_monitor is not None:
            _coverage(last_monitor, "unknown", "Docker 事件监测已停止，暂时无法接收容器生命周期事件。")
