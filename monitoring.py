"""Read-only website probes and persistent, bounded alert evaluation."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import secrets
import socket
import ssl
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import resource_alerts


DEFAULTS = {"sustain_seconds": 60, "repeat_seconds": 3600, "recovery": True,
            "slow_ms": 3000, "certificate_days": 14, **resource_alerts.DEFAULTS}
MAX_TARGETS = 50
MAX_EVENTS = 1000
MAX_DIAGNOSTIC_EVENTS = 1000
INCIDENT_EVENT_KINDS = frozenset({'alert', 'recovery', 'recovered', 'resolved', 'disappeared', 'container_missing', 'container_disappeared'})
EVENT_RETENTION_SECONDS = 30 * 86400


def normalize_url(value):
    value = str(value or "").strip()
    if not value or len(value) > 2048 or any(ord(c) <= 32 for c in value):
        raise ValueError("请填写有效的网站地址，例如 https://example.com/health")
    if "://" not in value:
        value = "https://" + value
    try:
        parts = urlsplit(value)
        port = parts.port
        if port == 0:
            raise ValueError("port is zero")
        host = (parts.hostname or "").encode("idna").decode("ascii").lower()
    except (ValueError, UnicodeError) as exc:
        raise ValueError("网址或端口无效") from exc
    if (parts.scheme not in {"http", "https"} or not host or parts.username is not None
            or parts.password is not None or parts.fragment or parts.query or "\\" in value):
        raise ValueError("仅支持 HTTP/HTTPS 地址；请移除账号、密码、查询参数和 # 片段")
    authority = f"[{host}]" if ":" in host else host
    if port is not None and port != (443 if parts.scheme == "https" else 80):
        authority += f":{port}"
    return urlunsplit((parts.scheme, authority, quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~"), "", ""))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Probe the supplied address directly. Redirects are reported, never silently followed.
OPENER = build_opener(ProxyHandler({}), NoRedirect())


def probe_options(data, previous=None):
    previous = previous or {}
    options = copy.deepcopy(previous.get('probe', {}))
    if previous.get('url'):
        old, new = urlsplit(previous['url']), urlsplit(normalize_url(data.get('url')))
        if (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
            options.pop('headers', None)
            options.pop('body', None)
    value = data.get('probe')
    if value is None:
        return options
    if not isinstance(value, dict):
        raise ValueError('接口检查参数无效')
    method = value.get('method', 'GET')
    if method not in {'GET', 'HEAD', 'POST'}:
        raise ValueError('检查方法仅支持 GET、HEAD、POST')
    headers = value.get('headers')
    if headers is None:
        headers = options.get('headers', {})
    if not isinstance(headers, dict) or len(headers) > 20:
        raise ValueError('请求头最多 20 项')
    for name, content in headers.items():
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,64}', name)
                or name.lower() in {'host', 'content-length', 'transfer-encoding', 'connection', 'proxy-authorization'}
                or not isinstance(content, str) or len(content) > 2048
                or any(ord(c) < 32 or ord(c) > 255 for c in content)):
            raise ValueError('请求头名称或内容无效')
    body = value.get('body')
    body = options.get('body', '') if body is None else body
    if not isinstance(body, str) or len(body.encode()) > 4096 or method != 'POST' and body:
        raise ValueError('请求体仅支持 POST，最多 4 KiB')
    expected = value.get('expected_status', [])
    if not isinstance(expected, list) or len(expected) > 20 or any(type(code) is not int or not 100 <= code <= 599 for code in expected):
        raise ValueError('预期状态码必须是 100 到 599 的数字列表')
    assertion = value.get('assertion', {})
    if not isinstance(assertion, dict) or assertion and assertion.get('kind') not in {'json', 'contains'}:
        raise ValueError('响应校验类型无效')
    if assertion:
        if method == 'HEAD':
            raise ValueError('HEAD 请求没有响应体，无法校验内容')
        if assertion['kind'] == 'json':
            path = assertion.get('path', '')
            if not isinstance(path, str) or not re.fullmatch(r'[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){0,15}', path):
                raise ValueError('JSON 字段示例：data.ready')
            expected_value = assertion.get('value')
            if type(expected_value) not in {str, int, float, bool, type(None)}:
                raise ValueError('JSON 预期值必须为字符串、数字、布尔值或 null')
            if isinstance(expected_value, float) and not math.isfinite(expected_value):
                raise ValueError('JSON 数字必须有限')
        elif not isinstance(assertion.get('value'), str) or not assertion['value']:
            raise ValueError('请填写需要包含的文本')
        if len(json.dumps(assertion, ensure_ascii=False)) > 2048:
            raise ValueError('响应校验内容过长')
    return {'method': method, 'headers': dict(headers), 'body': body,
            'expected_status': expected, 'assertion': assertion}


def public_target(target):
    result = copy.deepcopy(target)
    if 'probe' in result:
        options = result['probe']
        options['has_headers'] = bool(options.pop('headers', {}))
        options['has_body'] = bool(options.pop('body', ''))
    return result


def validate_response(response, assertion):
    if not assertion:
        return True
    # Fixed-length bounded reads with an absolute deadline; never store response bodies.
    deadline, body = time.monotonic() + 5, bytearray()
    while len(body) <= 65536:
        if time.monotonic() >= deadline:
            raise TimeoutError('response deadline')
        block = response.read1(min(4096, 65537 - len(body)))
        if not block:
            break
        body.extend(block)
    if len(body) > 65536:
        return False
    if assertion['kind'] == 'contains':
        return assertion['value'] in body.decode('utf-8', errors='replace')
    try:
        value = json.loads(body)
        for part in assertion['path'].split('.'):
            value = value[int(part)] if isinstance(value, list) and part.isdigit() else value[part]
        return type(value) is type(assertion['value']) and value == assertion['value']
    except (ValueError, KeyError, IndexError, TypeError, UnicodeError):
        return False


def certificate_probe(url, timeout=5):
    parts = urlsplit(url)
    if parts.scheme != "https":
        return {"status": "not_applicable", "detail": "HTTP 地址没有 TLS 证书", "checked_at": time.time()}
    checked = time.time()
    try:
        context = ssl.create_default_context()
        with socket.create_connection((parts.hostname, parts.port or 443), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=parts.hostname) as conn:
                cert = conn.getpeercert()
                der = conn.getpeercert(binary_form=True)
        expires = ssl.cert_time_to_seconds(cert["notAfter"])
        issuer = ", ".join(value for group in cert.get("issuer", ()) for key, value in group
                           if key in {"organizationName", "commonName"})
        return {"status": "valid", "verified": True, "expires_at": expires,
                "issuer": issuer, "sha256": hashlib.sha256(der).hexdigest(),
                "detail": "证书信任链及域名校验通过", "checked_at": checked}
    except ssl.SSLCertVerificationError as exc:
        return {"status": "invalid", "verified": False, "checked_at": checked,
                "detail": f"证书校验失败：{exc.verify_message}"}
    except (OSError, ValueError, KeyError) as exc:
        return {"status": "unknown", "checked_at": checked,
                "detail": f"暂时无法读取线上证书：{type(exc).__name__}"}


def probe(target, check_certificate=True):
    started = time.monotonic()
    result = {"checked_at": time.time(), "status": "failed", "code": None}
    options = target.get('probe') or {}
    assertion_ok = not options.get('assertion')
    try:
        request = Request(target["url"], headers={"User-Agent": "mini_deploy-monitor", **options.get('headers', {})},
                          method=options.get('method', 'GET'),
                          data=options.get('body', '').encode() if options.get('method') == 'POST' else None)
        with OPENER.open(request, timeout=5) as response:
            result["code"] = response.status
            assertion_ok = validate_response(response, options.get('assertion'))
    except HTTPError as exc:
        result["code"] = exc.code
        try:
            assertion_ok = validate_response(exc, options.get('assertion'))
        except (OSError, ValueError):
            assertion_ok = False
        finally:
            exc.close()
    except (OSError, URLError, ValueError) as exc:
        result["detail"] = f"连接失败（{type(exc).__name__}），请检查网站、网络或证书"
    code = result["code"]
    if code is not None:
        codes = options.get('expected_status')
        accepted = code in codes if codes else 200 <= code < 400
        result["status"] = "healthy" if accepted and assertion_ok else "failed"
        result["detail"] = f"HTTP {code}" + ("（重定向，未跟随）" if 300 <= code < 400 else "")
        if not assertion_ok:
            result['detail'] += ' · 响应内容未通过校验（或超过 64 KiB 校验范围）'
    result["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
    if check_certificate:
        result["certificate"] = certificate_probe(target["url"])
    return result


class Monitor:
    def __init__(self, path: Path, clock=time.time):
        self.path, self.clock = path, clock
        self.lock = threading.RLock()
        self.targets, self.results, self.incidents, self.events = {}, {}, {}, []
        self.settings = dict(DEFAULTS)
        self.muted_until = 0
        self.delivery = {}
        self.pending = {}
        self.evaluated = {}
        self.container_state = {}
        self.coverage = {}
        self.due = {}
        self.last_flush = 0
        if path.exists():
            if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
                raise ValueError("监测数据必须为普通文件")
            if path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("监测数据文件过大")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.targets = data.get("targets", {})
            self.results = data.get("results", {})
            self.incidents = data.get("incidents", {})
            self.events = data.get("events", [])
            self.settings.update(data.get("settings", {}))
            self.muted_until = data.get("muted_until", 0)
            self.delivery = data.get("delivery", {})
            self.pending = data.get("pending", {})
            self.container_state = data.get("container_state", {})
            self.evaluated = {k: v.get("checked_at") for k, v in self.results.items()}
            # Persisted active incidents survive restart; pending conditions need fresh samples.
            self.incidents = {k: v for k, v in self.incidents.items() if v.get("active")}
            for item in self.incidents.values():
                item.setdefault("incident_id", secrets.token_hex(12))
            self._prune_events()

    def _prune_events(self):
        cutoff = self.clock() - EVENT_RETENTION_SECONDS
        # Routine container/audit traffic must not evict incident history.
        retained, counts = [], {True: 0, False: 0}
        for item in reversed(self.events):
            incident = item.get('kind') in INCIDENT_EVENT_KINDS
            limit = MAX_EVENTS if incident else MAX_DIAGNOSTIC_EVENTS
            if item.get('at_ts', self.clock()) >= cutoff and counts[incident] < limit:
                retained.append(item)
                counts[incident] += 1
        self.events = list(reversed(retained))

    def flush(self):
        with self.lock:
            self._prune_events()
            data = {key: getattr(self, key) for key in (
                "targets", "results", "incidents", "events", "settings", "muted_until", "delivery", "pending", "container_state")}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(prefix=".monitor-", dir=self.path.parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(name, self.path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            self.last_flush = self.clock()

    def summary(self):
        with self.lock:
            return {"active_count": sum(1 for item in self.incidents.values() if item.get("active")),
                    "unacknowledged_count": sum(1 for item in self.incidents.values()
                                                if item.get("active") and not item.get("acknowledged_at")),
                    "generated_at": self.clock()}

    def snapshot(self):
        with self.lock:
            now = self.clock()
            targets = []
            for target in self.targets.values():
                result = copy.deepcopy(self.results.get(target["key"], {}))
                result["stale"] = now - result.get("checked_at", 0) > 120
                cert = result.get("certificate", {})
                cert["stale"] = now - cert.get("checked_at", 0) > 7200
                if cert.get("expires_at"):
                    cert["days_remaining"] = math.floor((cert["expires_at"] - now) / 86400)
                targets.append({**public_target(target), "result": result})
            self._prune_events()
            alerts = [{**v, "id": k, "muted": self.muted_until > now,
                       "stale": bool(v.get("sample_unknown")) or now - v.get("last_sample", 0) > 120,
                       "duration_seconds": max(0, int(now - v.get("first_seen", now)))}
                      for k, v in self.incidents.items() if v.get("active")]
            alerts.sort(key=lambda item: (bool(item.get("acknowledged_at")), item.get("level") != "critical", item.get("first_seen", now)))
            return copy.deepcopy({"targets": targets, "alerts": alerts, "events": list(reversed(self.events)),
                                  "settings": self.settings, "muted_until": self.muted_until,
                                  "delivery": self.delivery, "generated_at": now, "coverage": self.coverage,
                                  "history_retention_days": 30, "history_limit": MAX_EVENTS})

    def operation(self, data):
        with self.lock:
            fields = ("targets", "results", "incidents", "events", "settings", "muted_until",
                      "pending", "due", "evaluated", "container_state")
            before = {key: copy.deepcopy(getattr(self, key)) for key in fields}
            try:
                return self._operation(data)
            except Exception:
                for key, value in before.items():
                    setattr(self, key, value)
                raise

    def _operation(self, data):
        with self.lock:
            action = data.get("action")
            key = str(data.get("key") or "")
            if action in {"acknowledge", "acknowledge_all"}:
                if action == "acknowledge":
                    item = self.incidents.get(key)
                    if not item or not item.get("active") or data.get("incident_id") != item.get("incident_id"):
                        raise ValueError("故障已变化或恢复，请刷新后重试")
                    keys = [key]
                else:
                    keys = [k for k, item in self.incidents.items() if item.get("active")]
                for incident_key in keys:
                    item = self.incidents[incident_key]
                    if item.get("acknowledged_at"):
                        continue
                    item["acknowledged_at"] = self.clock()
                    self.pending.pop(incident_key, None)
                    self.record_event("acknowledged", "已知晓：" + item["title"],
                                      "故障仍在持续；暂停该次故障的重复通知", item["source"],
                                      level="info", incident_id=item["incident_id"], metadata=item.get("metadata"))
            elif action == "container_watch":
                if not key.startswith("docker:") or key not in self.container_state:
                    raise ValueError("容器监测对象不存在，请刷新页面")
                expected = data.get("expected_running")
                if not isinstance(expected, bool):
                    raise ValueError("持续运行必须是开关值")
                self.container_state[key]["expected_running"] = expected
                self.container_state[key]["watch_explicit"] = True
            elif action == "save":
                url = normalize_url(data.get("url"))
                if key and key not in self.targets:
                    raise ValueError("监测项不存在，请刷新页面")
                if any(t["url"] == url and t["key"] != key for t in self.targets.values()):
                    raise ValueError("这个地址已在监测列表中")
                if not key and len(self.targets) >= MAX_TARGETS:
                    raise ValueError(f"最多监测 {MAX_TARGETS} 个地址")
                key = key or secrets.token_hex(8)
                options = probe_options(data, self.targets.get(key))
                if key in self.targets:
                    self._forget(key)
                self.targets[key] = {"key": key, "url": url, "enabled": True,
                                     "name": str(data.get("name") or urlsplit(url).netloc)[:80],
                                     "revision": secrets.token_hex(8)}
                if options:
                    self.targets[key]['probe'] = options
                self.due[key] = 0
            elif action in {"remove", "toggle", "check"}:
                if key not in self.targets:
                    raise ValueError("监测项不存在，请刷新页面")
                if action == "remove":
                    self._forget(key)
                    del self.targets[key]
                elif action == "toggle":
                    self._forget(key)
                    self.targets[key]["enabled"] = not self.targets[key]["enabled"]
                    self.targets[key]["revision"] = secrets.token_hex(8)
                    self.due[key] = 0
                else:
                    self.due[key] = 0
            elif action == "mute":
                seconds = data.get("seconds")
                if seconds not in (0, 3600, 14400, 86400):
                    raise ValueError("请选择静音 1 小时、4 小时或 24 小时")
                self.muted_until = self.clock() + seconds if seconds else 0
            elif action == "settings":
                candidate = dict(self.settings)
                for field, low, high in (("sustain_seconds", 15, 600), ("repeat_seconds", 300, 86400),
                                         ("slow_ms", 100, 60000), ("certificate_days", 1, 90)):
                    value = data.get(field, candidate[field])
                    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                        raise ValueError(f"{field} 必须在 {low} 到 {high} 之间")
                    candidate[field] = value
                if not isinstance(data.get("recovery", candidate['recovery']), bool):
                    raise ValueError("恢复通知必须是开关值")
                candidate["recovery"] = data.get("recovery", candidate['recovery'])
                for field, (low, high) in resource_alerts.RANGES.items():
                    value = data.get(field, candidate[field])
                    if type(value) is not int or not low <= value <= high:
                        raise ValueError(f'{field} 必须在 {low} 到 {high} 之间')
                    candidate[field] = value
                enabled = data.get('resource_enabled', candidate['resource_enabled'])
                if not isinstance(enabled, bool):
                    raise ValueError('资源告警必须是开关值')
                candidate['resource_enabled'] = enabled
                if any(candidate.get(k) != self.settings.get(k) for k in resource_alerts.DEFAULTS):
                    for key, item in list(self.incidents.items()):
                        if key.startswith('server:') and not item.get('active'):
                            self.incidents.pop(key, None)
                            self.pending.pop(key, None)
                    if hasattr(self, 'resource_state'):
                        self.resource_state.clear()
                        self.resource_memory.clear()
                self.settings = candidate
            else:
                raise ValueError("不支持的监测操作")
            self.flush()
            return self.snapshot()

    def _forget(self, key):
        self.results.pop(key, None)
        self.due.pop(key, None)
        self.evaluated.pop(key, None)
        for incident in set(self.incidents) | set(self.pending):
            if incident.startswith(f"website:{key}:"):
                self.incidents.pop(incident, None)
                self.pending.pop(incident, None)

    def record_event(self, kind, title, detail, source, *, level="info", incident_id=None, metadata=None,
                     duration_seconds=None):
        with self.lock:
            now = self.clock()
            event = {"id": secrets.token_hex(12), "kind": kind, "level": level, "title": title,
                     "detail": detail, "source": source, "at_ts": now,
                     "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))}
            if incident_id:
                event["incident_id"] = incident_id
            if metadata:
                event["metadata"] = copy.deepcopy(metadata)
            if duration_seconds is not None:
                event["duration_seconds"] = duration_seconds
            self.events.append(event)
            self._prune_events()

    def _event(self, item, recovered=False):
        self.record_event("recovery" if recovered else "alert",
                          ("已恢复：" if recovered else "") + item["title"], item["detail"], item["source"],
                          level="success" if recovered else item["level"], incident_id=item.get("incident_id"),
                          metadata=item.get("metadata"),
                          duration_seconds=max(0, int(self.clock() - item["first_seen"])) if recovered else None)

    def observe(self, key, bad, title, detail, source, level="critical", *, metadata=None, immediate=False, observed_at=None):
        """None means no usable sample; it must never resolve an incident."""
        with self.lock:
            now = self.clock() if observed_at is None else observed_at
            item = self.incidents.get(key)
            if bad is None:
                if item:
                    item["sample_unknown"] = True
                if item and not item.get("active"):
                    self.incidents.pop(key, None)
                return
            if not bad:
                if item and item.get("active"):
                    self._event(item, recovered=True)
                    if (item.get("notified") or self.pending.get(key, {}).get("inflight")) and self.settings["recovery"] and self.muted_until <= now:
                        self.pending[key] = {"title": "已恢复：" + item["title"], "body": detail,
                                             "recovery": True, "retry_at": now, "token": secrets.token_hex(8)}
                    else:
                        self.pending.pop(key, None)
                self.incidents.pop(key, None)
                return
            if not item:
                self.pending.pop(key, None)
                item = {"first_seen": now, "active": False, "notified": False, "last_notice": 0,
                        "incident_id": secrets.token_hex(12)}
                self.incidents[key] = item
            elif not item["active"] and now - item.get("last_sample", now) > 120:
                item["first_seen"] = now
            item.update(title=title, detail=detail, source=source, level=level, last_sample=now, sample_unknown=False)
            if metadata is not None:
                item["metadata"] = copy.deepcopy(metadata)
            if not item["active"] and (immediate or now - item["first_seen"] >= self.settings["sustain_seconds"]):
                item["active"] = True
                item["at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
                self._event(item)
            if item["active"] and not item.get("acknowledged_at") and (not item["notified"] or now - item["last_notice"] >= self.settings["repeat_seconds"]):
                self.pending.setdefault(key, {"title": title, "body": detail, "recovery": False,
                                              "retry_at": now, "token": secrets.token_hex(8)})

    def accept(self, target, result):
        with self.lock:
            current = self.targets.get(target["key"])
            if not current or current != target or not current["enabled"]:
                return
            previous = self.results.get(target["key"], {})
            self.results[target["key"]] = {**previous, **result}

    def evaluate_websites(self):
        with self.lock:
            now = self.clock()
            for key, target in self.targets.items():
                if not target["enabled"]:
                    continue
                result = self.results.get(key, {})
                fresh = now - result.get("checked_at", 0) < 120
                name, url = target["name"], target["url"]
                failed = result.get("status") == "failed"
                if not fresh or self.evaluated.get(key) != result.get("checked_at"):
                    self.evaluated[key] = result.get("checked_at")
                    self.observe(f"website:{key}:availability", failed if fresh else None,
                                 f"网站无法访问：{name}", f"{url} · {result.get('detail', '等待检查')}", "website")
                    self.observe(f"website:{key}:slow", (result.get("duration_ms", 0) >= self.settings["slow_ms"])
                                 if fresh and not failed else None, f"网站响应缓慢：{name}",
                                 f"{url} · 响应 {result.get('duration_ms', 0):.2f} ms", "website", "warning")
                cert = result.get("certificate", {})
                valid = cert.get("status") == "valid"
                cert_fresh = now - cert.get("checked_at", 0) < 7200
                remaining = (cert.get("expires_at", now) - now) / 86400
                bad_cert = cert.get("status") == "invalid" or (valid and remaining <= self.settings["certificate_days"])
                self.observe(f"website:{key}:certificate", bad_cert if cert_fresh and cert.get("status") in {"valid", "invalid"} else None,
                             f"证书需要关注：{name}", f"{url} · " + (f"剩余 {math.floor(remaining)} 天" if valid else cert.get("detail", "等待检查")),
                             "certificate", "warning" if valid and remaining > 7 else "critical")

    def next_notification(self, enabled):
        with self.lock:
            now = self.clock()
            if not enabled or self.muted_until > now:
                return None
            for key, message in list(self.pending.items()):
                if key.startswith('server:') and not self.settings.get('resource_enabled', True):
                    continue
                if message["recovery"] and not self.settings["recovery"]:
                    self.pending.pop(key, None)
                    continue
                if message["retry_at"] > now:
                    continue
                item = self.incidents.get(key)
                if not message["recovery"] and (not item or item.get("acknowledged_at") or item.get("sample_unknown") or (item.get('metadata') or {}).get('recovering')
                                                or now - item.get("last_sample", 0) > 120):
                    continue
                message["retry_at"] = now + 300
                message["inflight"] = True
                return key, dict(message)
            return None

    def delivered(self, key, message, results):
        with self.lock:
            enabled = [r for r in results if r.get("enabled")]
            ok = bool(enabled) and all(r.get("ok") for r in enabled)
            self.delivery = {"at": self.clock(), "ok": ok,
                             "detail": "通知发送成功" if ok else "通知未全部送达，5 分钟后重试；请检查通知配置"}
            current = self.pending.get(key)
            if current and current.get("token") == message.get("token"):
                current["inflight"] = False
            if ok and current and current.get("token") == message.get("token"):
                self.pending.pop(key, None)
                item = self.incidents.get(key)
                if item and not message["recovery"]:
                    item.update(notified=True, last_notice=self.clock())
            self.flush()
