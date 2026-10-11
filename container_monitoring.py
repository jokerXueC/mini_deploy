"""Bounded Docker inspect evidence and polling-only container fault evaluation.

Integration: attach_inspection(snapshot, stdout) after a batched
``docker inspect --type container <full IDs...>``; then evaluate_containers(monitor,
snapshot). Failed or partial inspection is unknown, never a recovery. Call
mark_intentional_stop after successful stop/pause/remove (stopped=True) or
start/unpause/restart (stopped=False). The caller owns polling, locking of its
snapshot, and flushing Monitor; this module performs no Docker I/O or events.

Monitor must provide clock(), lock (reentrant), container_state, coverage,
incidents, and observe(..., metadata=..., immediate=...). State keys returned by
container_identity also support expected_running plus watch_explicit overrides.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from fractions import Fraction


RESTART_WINDOW_SECONDS = 300
RESTART_THRESHOLD = 3
MAX_CONTAINER_STATE = 256
FAULT_REASONS = ("exit", "oom", "health", "restarts")
_TITLES = {"exit": "容器运行异常", "oom": "容器内存耗尽", "health": "容器健康检查失败", "restarts": "容器频繁重启"}
_STATES = {"created", "running", "paused", "restarting", "removing", "exited", "dead"}
_STATE_NAMES = {"created": "已创建", "running": "运行中", "paused": "已暂停", "restarting": "正在重启",
                "removing": "正在移除", "exited": "已退出", "dead": "无法运行"}
_HEALTH_NAMES = {"healthy": "正常", "unhealthy": "失败", "starting": "启动中", "none": "未配置", None: "未知"}


def _text(value, limit=256):
    return value[:limit] if isinstance(value, str) else ""


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def parse_inspect(payload):
    """Return full-ID -> normalized evidence from Docker's JSON array or a list.

    Invalid records are omitted independently. Only the last health log's exit
    code and End are retained, never Output or environment/configuration values.
    Missing individual fault fields stay None rather than becoming good samples.
    """
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except (ValueError, UnicodeError):
            return {}
    if not isinstance(payload, list):
        return {}
    result = {}
    duplicates = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        container_id = _text(item.get("Id"), 128)
        state = item.get("State")
        if (not container_id or not isinstance(state, dict) or not isinstance(state.get("Status"), str)
                or state["Status"] not in _STATES):
            continue
        config = item.get("Config") or {}
        host = item.get("HostConfig") or {}
        labels = config.get("Labels") if isinstance(config, dict) else None
        labels = labels if isinstance(labels, dict) else {}
        policy = host.get("RestartPolicy") if isinstance(host, dict) else None
        policy = policy if isinstance(policy, dict) else {}
        health = state.get("Health")
        health_status = "none" if "Health" not in state else None
        last_check = {}
        if isinstance(health, dict):
            health_status = health.get("Status")
            if not isinstance(health_status, str) or health_status not in {"healthy", "unhealthy", "starting"}:
                health_status = None
            log = health.get("Log")
            if isinstance(log, list) and log and isinstance(log[-1], dict):
                last_check = {"exit_code": _count(log[-1].get("ExitCode")),
                              "end": _text(log[-1].get("End"), 64)}
        evidence = {
            "id": container_id, "name": _text(item.get("Name")).lstrip("/"),
            "state": state["Status"], "exit_code": _count(state.get("ExitCode")),
            "oom_killed": state.get("OOMKilled") if isinstance(state.get("OOMKilled"), bool) else None,
            "engine_error": bool(state["Error"]) if isinstance(state.get("Error"), str) else None,
            "started_at": _text(state.get("StartedAt"), 64),
            "finished_at": _text(state.get("FinishedAt"), 64),
            "health": health_status, "health_last_check": last_check,
            "restart_count": _count(item.get("RestartCount")),
            "restart_policy": _text(policy.get("Name"), 32),
            "restart_maximum_retry_count": _count(policy.get("MaximumRetryCount")),
            "compose_project": _text(labels.get("com.docker.compose.project")),
            "compose_service": _text(labels.get("com.docker.compose.service")),
            "compose_replica": _text(labels.get("com.docker.compose.container-number"), 32),
            "compose_oneoff": str(labels.get("com.docker.compose.oneoff", "")).lower() == "true",
        }
        if container_id in result:
            duplicates.add(container_id)
        result[container_id] = evidence
    for container_id in duplicates:
        result.pop(container_id, None)
    return result


def container_identity(evidence):
    """Stable state/incident prefix shared with lifecycle collectors.

    SHA256 of ASCII JSON with separators=(',', ':') and ensure_ascii=True.
    Compose identity is ['compose', project, service, replica]; replica defaults
    to ''. Standalone and one-off identity is ['container', full_id]. Multiple
    simultaneous containers with the same Compose identity are unknown samples.
    """
    if evidence.get("compose_project") and evidence.get("compose_service") and not evidence.get("compose_oneoff"):
        parts = ["compose", evidence["compose_project"], evidence["compose_service"],
                 evidence.get("compose_replica") or ""]
    else:
        parts = ["container", evidence["id"]]
    digest = hashlib.sha256(json.dumps(parts, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()
    return "docker:container:" + digest


def attach_inspection(docker_snapshot, payload):
    """Copy a docker-ps snapshot and attach evidence under each row's inspection.

    Full IDs must match exactly. Missing records replace previous evidence with
    None, including when a batch exits nonzero but stdout contains partial JSON.
    """
    evidence = parse_inspect(payload)
    snapshot = dict(docker_snapshot)
    snapshot["containers"] = [dict(row, inspection=evidence.get(row.get("id")))
                              for row in docker_snapshot.get("containers", []) if isinstance(row, dict)]
    return snapshot


def _bound_state(monitor):
    state = monitor.container_state
    owned = [key for key in state if key.startswith(("docker:container:", "docker:intent:"))]
    if len(owned) > MAX_CONTAINER_STATE:
        oldest = sorted(owned, key=lambda key: state[key].get("last_seen", 0))
        for key in oldest[:len(owned) - MAX_CONTAINER_STATE]:
            del state[key]


def mark_intentional_stop(monitor, container, stopped=True):
    """Mark a full ID or exact name after a successful Docker action.

    No fault is resolved here. Intent applies to that container, not subsequent
    replacements; watch_explicit/expected_running overrides are left intact.
    Unknown selectors are bounded pending markers for the next valid inspection.
    Prefer a full ID for removal/name reuse. A running sample clears stop intent
    only if StartedAt is strictly later than the persisted intentional_at.
    """
    if not isinstance(container, str) or not container or not isinstance(stopped, bool):
        raise ValueError("请提供容器 ID 或名称，以及布尔类型的停止标记")
    selector = container.lstrip("/")
    with monitor.lock:
        now = monitor.clock()
        found = False
        for key, entry in monitor.container_state.items():
            if not key.startswith(("docker:container:", "docker:intent:")):
                continue
            if selector in {entry.get("container_id"), entry.get("name"), entry.get("selector")}:
                entry.update(intentional_stop=stopped, intentional_at=now if stopped else None, last_seen=now)
                found = True
        if not found:
            key = "docker:intent:" + hashlib.sha256(selector.encode("utf-8")).hexdigest()
            monitor.container_state[key] = {"selector": selector, "intentional_stop": stopped,
                                            "intentional_at": now if stopped else None, "last_seen": now}
        _bound_state(monitor)


def _started_after(started_at, intentional_at):
    """Compare Docker RFC3339 timestamps without losing nanosecond precision."""
    if intentional_at is None:
        return False
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})", started_at)
    if not match:
        return False
    try:
        whole = datetime.fromisoformat(match[1] + match[3].replace("Z", "+00:00"))
        fraction = match[2] or "0"
        stamp = int(whole.timestamp()) + Fraction(int(fraction), 10 ** len(fraction))
        return stamp > Fraction(str(intentional_at))
    except (ValueError, OverflowError, OSError):
        return False


def _restart_delta(entry, evidence, now):
    # Timestamped counter increases are observed at poll time, not event time.
    buckets = [[stamp, count] for stamp, count in entry.get("restart_deltas", [])
               if now - RESTART_WINDOW_SECONDS < stamp <= now]
    current = evidence["restart_count"]
    previous = entry.get("restart_count")
    sampled_at = entry.get("restart_sample_at")
    same_container = entry.get("container_id") == evidence["id"]
    if current is not None:
        if (same_container and previous is not None and sampled_at is not None
                and now - sampled_at > RESTART_WINDOW_SECONDS and current > previous):
            # A long gap cannot locate these increases inside the rolling window.
            entry["restart_incomplete_until"] = max(entry.get("restart_incomplete_until", 0), now + RESTART_WINDOW_SECONDS)
        if (same_container and previous is not None and sampled_at is not None
                and 0 <= now - sampled_at <= RESTART_WINDOW_SECONDS and current > previous):
            stamp, delta = now, current - previous
            if buckets and buckets[-1][0] == stamp:
                buckets[-1][1] += delta
            else:
                buckets.append([stamp, delta])
        entry.update(restart_count=current, restart_sample_at=now)
    elif not same_container:
        entry.pop("restart_count", None)
        entry.pop("restart_sample_at", None)
    limit = RESTART_WINDOW_SECONDS + 1
    if len(buckets) > limit:
        entry["restart_incomplete_until"] = max(entry.get("restart_incomplete_until", 0),
                                                buckets[-limit - 1][0] + RESTART_WINDOW_SECONDS)
    entry["restart_deltas"] = buckets[-limit:]
    total = sum(count for _, count in entry["restart_deltas"])
    complete = now >= entry.get("restart_incomplete_until", 0)
    return total, current is not None and complete


def _conditions(evidence, entry, restart_total, restart_known, threshold):
    state = evidence["state"]
    running = state == "running"
    if entry.get("intentional_stop") or state in {"created", "paused", "removing"}:
        return dict.fromkeys(FAULT_REASONS)
    restarts = True if restart_total >= threshold else (False if restart_known else None)
    if running:
        health = evidence["health"]
        return {"exit": False, "oom": False,
                "health": health == "unhealthy" if health in {"healthy", "unhealthy", "none"} else None,
                "restarts": restarts}
    exit_code, oom, error = evidence["exit_code"], evidence["oom_killed"], evidence["engine_error"]
    abnormal = exit_code is not None and exit_code not in {0, 137, 143}
    clean_expected = (entry.get("expected_running") and
                      (entry.get("watch_explicit") or evidence["restart_policy"] == "always"))
    bad_exit = state == "dead" or bool(error) or abnormal or (exit_code == 0 and clean_expected)
    if not bad_exit and oom is not True and state != "restarting":
        # Clean / signal-style exits could be external stops or successful jobs.
        return dict.fromkeys(FAULT_REASONS)
    return {"exit": True if bad_exit or state == "restarting" else None,
            "oom": True if oom is True else None, "health": None,
            "restarts": restarts}


def _migrate_legacy(monitor, evidence, identity, conditions, metadata, detail):
    """Move old ID/name incidents only when inspect supplies a fault or recovery."""
    legacy_keys = {"docker:container:" + evidence["id"]}
    if evidence["name"]:
        legacy_keys.add("docker:container:" + evidence["name"])
    reason = next((reason for reason in ("oom", "restarts", "health", "exit")
                   if conditions[reason] is True), None)
    healthy = (evidence["state"] == "running" and evidence["health"] in {"healthy", "none"}
               and not metadata["intentional_stop"] and reason is None)
    for key in sorted(legacy_keys):
        legacy = monitor.incidents.get(key)
        if not legacy:
            continue
        if reason:
            target = identity + ":" + reason
            current = monitor.incidents.get(target, {})
            # Prefer the legacy incident so its ID and acknowledgement survive.
            if not legacy.get("acknowledged_at") and current.get("acknowledged_at"):
                legacy["acknowledged_at"] = current["acknowledged_at"]
            monitor.incidents[target] = legacy
            del monitor.incidents[key]
            if key in monitor.pending:
                monitor.pending[target] = monitor.pending.pop(key)
        elif healthy:
            monitor.observe(key, False, legacy["title"], detail, "docker", legacy.get("level", "critical"),
                            metadata=metadata, immediate=False)


def evaluate_containers(monitor, docker_snapshot, *, restart_threshold=RESTART_THRESHOLD):
    """Evaluate attached inspect samples; mutate bounded Monitor state only.

    Reasons: exit (includes dead/restarting/engine errors), oom, health, restarts.
    OOM and restart storms activate immediately; other faults use Monitor sustain.
    Clean exits require explicit expectation or restart policy 'always'. Prior
    running and 'unless-stopped' inform expected_running but cannot prove a clean
    exit was unintentional. Nonzero exits remain faults even for standalone jobs,
    except ambiguous stop signals (137/143) without OOM/engine-error evidence.
    """
    if _count(restart_threshold) is None or restart_threshold < 1:
        raise ValueError("重启次数阈值必须为正整数")
    with monitor.lock:
        now = monitor.clock()
        rows = docker_snapshot.get("containers") or []
        available = docker_snapshot.get("available") is True
        groups = {}
        if available:
            for row in rows:
                evidence = row.get("inspection") if isinstance(row, dict) else None
                if not isinstance(evidence, dict) or evidence.get("id") != row.get("id"):
                    continue
                groups.setdefault(container_identity(evidence), []).append(evidence)
        observed = set()
        usable = 0
        for key, samples in list(groups.items())[:MAX_CONTAINER_STATE]:
            if len(samples) != 1:
                continue
            evidence = samples[0]
            entry = monitor.container_state.setdefault(key, {})
            if now < entry.get("last_seen", now):
                continue
            same_container = entry.get("container_id") == evidence["id"]
            if not same_container:
                entry["intentional_stop"] = False
                entry.pop("intentional_at", None)
            for marker_key, marker in list(monitor.container_state.items()):
                if (marker_key.startswith("docker:intent:")
                        and marker.get("selector") in {evidence["id"], evidence["name"]}):
                    entry["intentional_stop"] = marker["intentional_stop"]
                    entry["intentional_at"] = marker.get("intentional_at")
                    del monitor.container_state[marker_key]
            total, known = _restart_delta(entry, evidence, now)
            if evidence["state"] == "running":
                entry["seen_running"] = True
                if entry.get("intentional_stop") and _started_after(evidence["started_at"], entry.get("intentional_at")):
                    entry["intentional_stop"] = False
                    entry.pop("intentional_at", None)
            if not entry.get("watch_explicit"):
                entry["expected_running"] = (not evidence["compose_oneoff"] and
                                             (evidence["restart_policy"] in {"always", "unless-stopped"}
                                              or bool(entry.get("seen_running"))))
            entry.update(container_id=evidence["id"], name=evidence["name"], last_seen=now)
            metadata = {**evidence, "identity": key,
                        "container": evidence["name"], "container_id": evidence["id"],
                        "project": evidence["compose_project"], "service": evidence["compose_service"],
                        "expected_running": bool(entry.get("expected_running")),
                        "intentional_stop": bool(entry.get("intentional_stop")), "restart_delta_5m": total,
                        "restart_delta_complete": known,
                        "restart_window_seconds": RESTART_WINDOW_SECONDS, "sampling": "polling"}
            conditions = _conditions(evidence, entry, total, known, restart_threshold)
            name = evidence["name"] or evidence["id"][:12]
            exit_display = "未知" if evidence["exit_code"] is None else str(evidence["exit_code"])
            oom_display = {True: "是", False: "否", None: "未知"}[evidence["oom_killed"]]
            detail = (f"{name} · 状态 {_STATE_NAMES[evidence['state']]} · 退出码 {exit_display} · "
                      f"内存耗尽标记 {oom_display} · 健康状态 {_HEALTH_NAMES[evidence['health']]} · "
                      f"近 5 分钟轮询观测到重启增加 {total} 次")
            if evidence["engine_error"]:
                detail += " · Docker 报告运行错误（未保存原始错误文本）"
            _migrate_legacy(monitor, evidence, key, conditions, metadata, detail)
            for reason, bad in conditions.items():
                fault_key = key + ":" + reason
                observed.add(fault_key)
                monitor.observe(fault_key, bad, f"{_TITLES[reason]}：{name}", detail, "docker", "critical",
                                metadata={**metadata, "reason": reason}, immediate=reason in {"oom", "restarts"})
            usable += 1
        # Vanished, uninspected and ambiguous identities stay stale, including
        # incidents whose bounded tracking state has already been evicted.
        for key, item in list(monitor.incidents.items()):
            if key.startswith("docker:container:") and key not in observed:
                monitor.observe(key, None, item["title"], "容器检查不可用或已不再观测到，等待有效样本",
                                "docker", item.get("level", "critical"), metadata=item.get("metadata"), immediate=False)
        monitor.coverage["docker_containers"] = {
            "sampling": "polling", "available": available, "checked_at": now,
            "listed": len(rows), "inspected": usable,
            "status": ("not_installed" if docker_snapshot.get("error") == "docker command not found" else
                       "healthy" if available and usable == len(rows) else "unknown"),
            "detail": "使用轮询检查；重启次数仅统计观测到的增加，可能遗漏采样之间的事件；未保存容器日志或原始错误文本。",
        }
        _bound_state(monitor)
