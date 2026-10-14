"""Prometheus text exposition and a dedicated, read-only scrape credential."""
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading
import time

from certificates import trusted_path
from server_performance import values
LOCK = threading.RLock()


class Access:
    def __init__(self, home):
        self.path = Path(home) / 'prometheus-access.json'
        self.lock = LOCK

    def read(self):
        trusted_path(self.path)
        if not self.path.exists():
            return {}
        info = self.path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096:
            raise ValueError('监控密钥配置不安全，原文件已保留')
        data = json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('hash') and not re.fullmatch('[a-f0-9]{64}', str(data['hash'])):
            raise ValueError('监控密钥配置损坏，原文件已保留')
        return data

    def snapshot(self):
        with self.lock:
            data = self.read()
            return {'enabled': bool(data.get('hash')), 'changed_at': data.get('changed_at'), 'path': '/metrics'}

    def verify(self, token):
        if not isinstance(token, str) or not 20 <= len(token) <= 128:
            return False
        with self.lock:
            expected = self.read().get('hash', '')
        return bool(expected) and hmac.compare_digest(expected, hashlib.sha256(token.encode()).hexdigest())

    def change(self, action):
        if action not in {'enable', 'rotate', 'disable'}:
            raise ValueError('不支持的监控接入操作')
        with self.lock:
            existing = self.read()  # Never silently overwrite a corrupt configuration.
            if action == 'enable' and existing.get('hash'):
                raise ValueError('已开启采集；需要替换密钥时请选择重新生成。')
            token = secrets.token_urlsafe(32) if action != 'disable' else ''
            data = {'hash': hashlib.sha256(token.encode()).hexdigest() if token else '', 'changed_at': time.time()}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix='.metrics-access-', dir=self.path.parent)
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
                    json.dump(data, target)
                    target.flush()
                    os.fsync(target.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return {**self.snapshot(), 'token': token}


def number(value):
    return type(value) in (float, int) and math.isfinite(value) and value >= 0


def label(value):
    text = str(value)
    if len(text) > 160:
        text = text[:140] + '_' + hashlib.sha256(text.encode()).hexdigest()[:16]
    return text.replace('\\', '\\\\').replace('\n', '\\n').replace('"', '\\"').replace('\r', '')


def render(server, docker, monitor, *, now=None, started=None, docker_at=None):
    now = time.time() if now is None else now
    output = []
    def metric(name, help, samples, kind='gauge'):
        good = [(labels, value) for labels, value in samples if number(value)]
        if not good:
            return
        name = 'mini_deploy_' + name
        output.extend([f'# HELP {name} {help}', f'# TYPE {name} {kind}'])
        for labels, value in good:
            tags = '{' + ','.join(f'{key}="{label(content)}"' for key, content in labels.items()) + '}' if labels else ''
            output.append(f'{name}{tags} {value:.12g}')
    def single(name, help, value):
        metric(name, help, [({}, value)])
    at = server.get('sampled_ts')
    fresh = number(at) and 0 <= now - at <= 10
    single('server_sample_available', 'Whether the cached server sample is fresh.', int(fresh))
    single('server_sample_timestamp_seconds', 'Unix time of the cached server sample.', at)
    single('uptime_seconds', 'Agent uptime in seconds.', max(0, now - started) if started is not None else None)
    if fresh:
        metrics = values(server)
        for key, name, scale, help in [
            ('cpu_percent', 'cpu_usage_ratio', .01, 'Overall CPU usage, all logical cores combined.'),
            ('memory_percent', 'memory_usage_ratio', .01, 'Host memory usage ratio.'),
            ('memory_used_mb', 'memory_used_bytes', 1048576, 'Used host memory in bytes.'),
            ('swap_percent', 'swap_usage_ratio', .01, 'Swap usage ratio; omitted when swap is unavailable.'),
            ('swap_used_mb', 'swap_used_bytes', 1048576, 'Used swap in bytes.'),
            ('disk_percent', 'root_disk_usage_ratio', .01, 'Root filesystem usage ratio.'),
            ('disk_used_gb', 'root_disk_used_bytes', 1073741824, 'Used root filesystem bytes.'),
            ('inode_percent', 'root_inode_usage_ratio', .01, 'Root inode usage ratio.'),
            ('iowait_percent', 'cpu_iowait_ratio', .01, 'CPU I/O wait ratio.'),
            ('network_rx_kib_s', 'network_receive_bytes_per_second', 1024, 'Cached aggregate non-loopback receive rate, including virtual interfaces.'),
            ('network_tx_kib_s', 'network_transmit_bytes_per_second', 1024, 'Cached aggregate non-loopback transmit rate, including virtual interfaces.'),
            ('disk_read_kib_s', 'disk_read_bytes_per_second', 1024, 'Aggregate identified device read rate.'),
            ('disk_write_kib_s', 'disk_write_bytes_per_second', 1024, 'Aggregate identified device write rate.'),
            ('disk_iops', 'disk_operations_per_second', 1, 'Aggregate identified device operation rate.'),
            ('disk_await_ms', 'disk_await_seconds', .001, 'IOPS weighted disk request latency.'),
            ('disk_busy_percent', 'disk_busy_ratio', .01, 'Busiest identified device busy ratio.')]:
            value = metrics.get(key)
            single(name, help, value * scale if number(value) else None)
        metric('load', 'Host load average.', [({'window': period}, metrics.get(key)) for period, key in [('1m', 'load_one'), ('5m', 'load_five'), ('15m', 'load_fifteen')]])
        single('cpu_logical_cores', 'Logical CPU count.', (server.get('cpu') or {}).get('logical_count'))
        single('memory_total_bytes', 'Total host memory in bytes.', (server.get('memory') or {}).get('total_mb', 0) * 1048576 if number((server.get('memory') or {}).get('total_mb')) else None)
    docker_fresh = number(docker_at) and 0 <= now - docker_at <= 65 and docker.get('available')
    single('docker_sample_available', 'Whether Docker cached state is fresh and available.', int(bool(docker_fresh)))
    single('docker_sample_timestamp_seconds', 'Unix time of cached Docker state.', docker_at)
    if docker_fresh:
        containers = (docker.get('containers') or [])[:500]
        # Container names, not IDs/PIDs/IPs/URLs, bound the label cardinality.
        for name, help, convert in [
            ('container_running', 'Container is running.', lambda c: int(c.get('state') == 'running')),
            ('container_cpu_cores', 'Container CPU usage in cores, 1 means one full core.', lambda c: c['cpu_percent'] / 100 if number(c.get('cpu_percent')) else None),
            ('container_memory_usage_ratio', 'Container memory usage ratio relative to its limit.', lambda c: c['memory_percent'] / 100 if number(c.get('memory_percent')) else None),
            ('container_unhealthy', 'Container healthcheck reports unhealthy.', lambda c: int(c.get('health') == 'unhealthy') if c.get('health') in {'healthy', 'unhealthy'} else None)]:
            unique = {str(c.get('name')): c for c in containers if c.get('name')}
            metric(name, help, [({'container': key}, convert(c)) for key, c in unique.items()])
    if monitor is not None:
        with monitor.lock:
            metric('active_alerts', 'Active incidents, including acknowledged incidents.', [({'severity': severity}, sum(i.get('active', False) and i.get('level') == severity for i in monitor.incidents.values())) for severity in ('critical', 'warning', 'info')])
            single('notifications_muted', 'Notifications are currently muted.', int(monitor.muted_until > now))
            single('resource_alerts_enabled', 'Resource alert evaluation is enabled.', int(monitor.settings.get('resource_enabled', True)))
            metric('website_sample_available', 'Whether a website probe result is fresh.', [({'target': key}, int(0 <= now - monitor.results.get(key, {}).get('checked_at', 0) <= 120)) for key, t in monitor.targets.items() if t.get('enabled')])
            for name, help, field in [('website_available', 'Website HTTP probe succeeded.', 'status'), ('website_duration_seconds', 'Website probe duration in seconds.', 'duration_ms')]:
                rows = []
                for key, target in monitor.targets.items():
                    result = monitor.results.get(key, {})
                    if not target.get('enabled') or not 0 <= now - result.get('checked_at', 0) <= 120:
                        continue
                    value = int(result.get('status') == 'healthy') if field == 'status' else result.get(field) / 1000 if number(result.get(field)) else None
                    rows.append(({'target': key}, value))
                metric(name, help, rows)
    return ('\n'.join(output) + '\n').encode('utf-8')
