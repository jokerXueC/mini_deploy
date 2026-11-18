"""Read-only Docker/cgroup evidence; counters are never confused with rates."""
from __future__ import annotations

import math
import os
from pathlib import Path
import re
import stat
import time


METRICS = [
    ('cpu_percent', 'CPU（100% 为单核）', '%'), ('cpu_limit_percent', 'CPU 配额利用率', '%'),
    ('cpu_throttled_percent', 'CPU 限流周期占比', '%'),
    ('memory_used_bytes', '内存工作集', 'bytes'), ('memory_limit_bytes', '内存上限', 'bytes'),
    ('memory_current_bytes', '内存总占用（含缓存）', 'bytes'),
    ('memory_headroom_bytes', '内存余量', 'bytes'), ('memory_limit_percent', '内存上限利用率', '%'),
    ('memory_anon_bytes', '匿名内存', 'bytes'), ('memory_cache_bytes', '文件缓存', 'bytes'),
    ('memory_swap_bytes', '已用 Swap', 'bytes'),
    ('network_rx_bytes_s', '网络入站', 'bytes/s'), ('network_tx_bytes_s', '网络出站', 'bytes/s'),
    ('block_read_bytes_s', '磁盘读取', 'bytes/s'), ('block_write_bytes_s', '磁盘写入', 'bytes/s'),
    ('pids_current', '进程与线程', '个'), ('pids_percent', 'PID 上限利用率', '%'),
    ('log_bytes', '当前日志段大小', 'bytes'), ('log_bytes_s', '当前日志段增长', 'bytes/s'),
    ('cpu_pressure_percent', 'CPU 等待压力（10 秒）', '%'),
    ('memory_pressure_percent', '内存等待压力（10 秒）', '%'), ('io_pressure_percent', 'I/O 等待压力（10 秒）', '%'),
    ('main_process_fds', '主进程文件描述符', '个'), ('main_process_fd_percent', '主进程 FD 上限利用率', '%'),
    ('oom_kill_total', 'cgroup OOM 终止累计', '次'),
]


def valid(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def byte_value(text):
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*([kKmMgGtTpP]?i?[bB])\s*', str(text))
    if not match:
        return None
    unit = match[2].lower()
    exponent = 'bkmgtp'.index(unit[0]) if len(unit) > 1 else 0
    value = float(match[1]) * (1024 if 'i' in unit else 1000) ** exponent
    return int(value) if math.isfinite(value) else None


def pair(text):
    values = str(text or '').split('/')
    return [byte_value(v) for v in values] if len(values) == 2 else [None, None]


def inspection(item):
    host = item.get('HostConfig') or {}
    if not isinstance(host, dict):
        host = {}
    result = {}
    for key, source in [('memory_limit_bytes', 'Memory'), ('cpu_quota', 'CpuQuota'), ('cpu_period', 'CpuPeriod'),
                        ('nano_cpus', 'NanoCpus'), ('pids_limit', 'PidsLimit')]:
        value = host.get(source)
        if valid(value) and value > 0:
            result[key] = value
    cpuset = host.get('CpusetCpus')
    if isinstance(cpuset, str) and re.fullmatch(r'[0-9,-]{1,1024}', cpuset):
        cores = set()
        try:
            for part in cpuset.split(','):
                ends = list(map(int, part.split('-')))
                if len(ends) not in (1, 2) or max(ends) > 65535 or ends[-1] < ends[0]:
                    raise ValueError('cpuset')
                cores.update(range(ends[0], ends[-1] + 1))
            result['cpuset_cores'] = len(cores)
        except ValueError:
            pass
    pid = (item.get('State') or {}).get('Pid')
    if type(pid) is int and pid > 0:
        result['pid'] = pid
    driver = (host.get('LogConfig') or {}).get('Type')
    if isinstance(driver, str) and len(driver) <= 32:
        result['log_driver'] = driver
    log_path = item.get('LogPath')
    if isinstance(log_path, str) and len(log_path) < 4096 and log_path.startswith('/'):
        result['log_path'] = log_path
    return result


class Sampler:
    def __init__(self, proc=Path('/proc'), cgroup=Path('/sys/fs/cgroup'), clock=time.monotonic, budget=.25):
        self.proc, self.cgroup, self.clock, self.budget = Path(proc), Path(cgroup), clock, budget
        self.previous = {}

    @staticmethod
    def read(path):
        with path.open(encoding='ascii') as stream:
            return stream.read(16384)

    def fields(self, path):
        try:
            return {parts[0]: int(parts[1]) for line in self.read(path).splitlines()
                    if len(parts := line.split()) == 2 and parts[1].isdigit()}
        except (OSError, UnicodeError):
            return {}

    def counter(self, path):
        try:
            text = self.read(path).strip()
            return int(text) if text.isdigit() else None
        except (OSError, UnicodeError):
            return None

    def location(self, controller, group):
        relative = Path(group.lstrip('/'))
        if '..' in relative.parts or '\\' in group:
            return None
        roots = [self.cgroup] if controller == 'v2' else [self.cgroup / name for name in (
            ('cpu,cpuacct', 'cpuacct,cpu', 'cpu') if controller == 'cpu' else (controller,))]
        for root in roots:
            path = root / relative
            if path.is_dir() and path.resolve().is_relative_to(self.cgroup.resolve()):
                return path
        return None

    def cgroups(self, pid, identifier):
        if not pid:
            return {}, False
        try:
            lines = self.read(self.proc / str(pid) / 'cgroup').splitlines()
        except (OSError, UnicodeError):
            return {}, False
        paths = {}
        for line in lines:
            parts = line.split(':', 2)
            if len(parts) != 3 or not re.search(r'(?<![0-9a-f])' + identifier + r'(?![0-9a-f])', parts[2]):
                continue
            controllers = ['v2'] if not parts[1] and parts[0] == '0' else parts[1].split(',')
            for controller in controllers:
                if controller not in ('v2', 'cpu', 'memory', 'pids'):
                    continue
                path = self.location(controller, parts[2])
                if path:
                    paths[controller] = path
        return paths, bool(paths)

    def linux(self, config, identifier):
        paths, verified = self.cgroups(config.get('pid'), identifier)
        result, counters = {}, {}
        unified = paths.get('v2')
        cpu = unified or paths.get('cpu')
        memory = unified or paths.get('memory')
        pids = unified or paths.get('pids')
        if cpu:
            values = self.fields(cpu / 'cpu.stat')
            counters.update(cpu_periods=values.get('nr_periods'), cpu_throttled=values.get('nr_throttled'))
            try:
                quota, period = (self.read(cpu / 'cpu.max').split() if unified else
                                 (self.read(cpu / 'cpu.cfs_quota_us').strip(), self.read(cpu / 'cpu.cfs_period_us').strip()))
                if quota.isdigit() and period.isdigit() and int(period) > 0:
                    result['cpu_limit_cores'] = int(quota) / int(period)
            except (OSError, UnicodeError, ValueError):
                pass
        if memory:
            usage = self.counter(memory / ('memory.current' if unified else 'memory.usage_in_bytes'))
            values = self.fields(memory / 'memory.stat')
            inactive = values.get('inactive_file' if unified else 'total_inactive_file')
            result.update(memory_current_bytes=usage,
                memory_used_bytes=max(0, usage - inactive) if usage is not None and inactive is not None else None,
                memory_anon_bytes=values.get('anon' if unified else 'total_rss'),
                memory_cache_bytes=values.get('file' if unified else 'total_cache'))
            limit = self.counter(memory / ('memory.max' if unified else 'memory.limit_in_bytes'))
            if limit and limit < 2**60:
                result['memory_limit_bytes'] = limit
            if unified:
                result['memory_swap_bytes'] = self.counter(memory / 'memory.swap.current')
                counters['oom_kills'] = self.fields(memory / 'memory.events').get('oom_kill')
            else:
                combined = self.counter(memory / 'memory.memsw.usage_in_bytes')
                result['memory_swap_bytes'] = max(0, combined - usage) if combined is not None and usage is not None else None
        if pids:
            result.update(pids_current=self.counter(pids / 'pids.current'), pids_limit=self.counter(pids / 'pids.max'))
        if unified:
            for kind in ('cpu', 'memory', 'io'):
                try:
                    text = self.read(unified / f'{kind}.pressure')
                    match = re.search(r'^some avg10=(\d+(?:\.\d+)?)', text, re.M)
                    result[kind + '_pressure_percent'] = float(match[1]) if match else None
                except (OSError, UnicodeError):
                    pass
        if verified:
            path = self.proc / str(config['pid'])
            try:
                with os.scandir(path / 'fd') as entries:
                    count = 0
                    for _ in entries:
                        count += 1
                        if count > 4096:
                            break
                result['main_process_fds'] = count if count <= 4096 else None
                text = self.read(path / 'limits')
                match = re.search(r'^Max open files\s+(\d+)\s+', text, re.M)
                result['main_process_fd_limit'] = int(match[1]) if match else None
            except (OSError, UnicodeError):
                pass
        result['cgroup_version'] = 'v2' if unified else 'v1' if paths else 'unavailable'
        return result, counters

    def sample(self, snapshot, now=None, *, local=True):
        now = self.clock() if now is None else now
        started, current = self.clock(), {}
        for index, row in enumerate(snapshot.get('containers', [])):
            identifier = row.get('id', '')
            if not re.fullmatch('[a-f0-9]{64}', identifier):
                continue
            evidence = row.get('inspection') or {}
            config = evidence.get('resource_config') or {}
            used, _ = pair(row.get('memory_usage'))
            rx, tx = pair(row.get('net_io'))
            read, write = pair(row.get('block_io'))
            values = {'memory_used_bytes': used, 'memory_limit_bytes': config.get('memory_limit_bytes'),
                      'pids_current': row.get('pids'), 'pids_limit': config.get('pids_limit'),
                      'log_driver': config.get('log_driver') or '未知', 'sampled_at': time.time(),
                      'cgroup_version': 'unavailable'}
            counters = dict(network_rx_bytes=rx, network_tx_bytes=tx, block_read_bytes=read, block_write_bytes=write)
            quota = []
            if config.get('nano_cpus'):
                quota.append(config['nano_cpus'] / 1e9)
            if config.get('cpu_quota'):
                quota.append(config['cpu_quota'] / (config.get('cpu_period') or 100000))
            if config.get('cpuset_cores'):
                quota.append(config['cpuset_cores'])
            bounded = index < 256 and self.clock() - started <= self.budget
            if local and bounded and evidence.get('state') == 'running':
                linux, extra = self.linux(config, identifier)
                if linux.get('cpu_limit_cores'):
                    quota.append(linux.pop('cpu_limit_cores'))
                values.update({k: v for k, v in linux.items() if v is not None})
                counters.update(extra)
                log_path = config.get('log_path')
                if log_path and values['log_driver'] in ('json-file', 'local'):
                    try:
                        info = Path(log_path).lstat()
                        if stat.S_ISREG(info.st_mode):
                            values['log_bytes'] = info.st_size
                            counters['log_bytes'] = info.st_size
                            counters['log_inode'] = (info.st_dev, info.st_ino)
                    except OSError:
                        pass
            values['cpu_limit_cores'] = min(quota) if quota else None
            for value, limit, percent in [('memory_current_bytes', 'memory_limit_bytes', 'memory_limit_percent'),
                                           ('pids_current', 'pids_limit', 'pids_percent'),
                                           ('main_process_fds', 'main_process_fd_limit', 'main_process_fd_percent')]:
                values[percent] = values[value] / values[limit] * 100 if valid(values.get(value)) and values.get(limit) else None
            values['memory_headroom_bytes'] = max(0, values['memory_limit_bytes'] - values['memory_current_bytes']) if (
                valid(values.get('memory_current_bytes')) and values.get('memory_limit_bytes')) else None
            values['cpu_limit_percent'] = row['cpu_percent'] / values['cpu_limit_cores'] if (
                valid(row.get('cpu_percent')) and values.get('cpu_limit_cores')) else None
            previous = self.previous.get(identifier)
            token = evidence.get('started_at')
            elapsed = now - previous['at'] if previous else 0
            old = previous['counters'] if previous and token and previous['token'] == token and 0 < elapsed <= 90 else {}
            for counter in ('network_rx_bytes', 'network_tx_bytes', 'block_read_bytes', 'block_write_bytes', 'log_bytes'):
                a, b = counters.get(counter), old.get(counter)
                same_log = counter != 'log_bytes' or counters.get('log_inode') == old.get('log_inode')
                values[counter + '_s'] = (a - b) / elapsed if valid(a) and valid(b) and a >= b and same_log else None
            a, b = counters.get('cpu_periods'), old.get('cpu_periods')
            c, d = counters.get('cpu_throttled'), old.get('cpu_throttled')
            values['cpu_throttled_percent'] = (c - d) / (a - b) * 100 if (
                all(valid(v) for v in (a, b, c, d)) and a > b and 0 <= c - d <= a - b) else None
            values['oom_kill_total'] = counters.get('oom_kills')
            a, b = counters.get('oom_kills'), old.get('oom_kills')
            values['oom_kill_delta'] = a - b if valid(a) and valid(b) and a >= b else None
            values['notice'] = ('限额为当前容器配置；未设限不代表主机资源无限。内存工作集不等于应用堆；'
                '网络/磁盘为计数差值，首次、重启、计数重置及采样中断不补零。日志仅统计当前日志段，不含轮转归档；'
                'FD 仅为主进程。cgroup、日志或权限不可用的指标显示未知。')
            if not bounded:
                values['notice'] += '本轮达到轻量采集预算，部分指标暂缺。'
            row['resources'] = values
            current[identifier] = {'at': now, 'token': token, 'counters': counters}
        self.previous = current
        return snapshot


def evaluate(monitor, snapshot):
    import container_monitoring
    now = monitor.clock()
    fresh = snapshot.get('available') and not snapshot.get('stale')
    identities = {}
    for row in snapshot.get('containers', []) if fresh else []:
        evidence = row.get('inspection') or {}
        if evidence.get('id'):
            key = container_monitoring.container_identity(evidence)
            identities[key] = identities.get(key, 0) + 1
    seen = set()
    for row in snapshot.get('containers', []) if fresh else []:
        evidence = row.get('inspection') or {}
        resource = row.get('resources') or {}
        if not evidence.get('id'):
            continue
        prefix = container_monitoring.container_identity(evidence) + ':resource:'
        for reason, field, threshold, title in [
            ('memory', 'memory_limit_percent', 90, '容器内存接近上限'),
            ('cpu_throttle', 'cpu_throttled_percent', 20, '容器 CPU 持续限流'),
            ('pids', 'pids_percent', 90, '容器进程/线程接近上限'),
            ('fds', 'main_process_fd_percent', 90, '容器主进程文件描述符接近上限'),
        ]:
            key = prefix + reason
            seen.add(key)
            value = resource.get(field)
            known = (identities[prefix.removesuffix(':resource:')] == 1
                     and monitor.settings.get('resource_enabled', True) and evidence.get('state') == 'running'
                     and valid(value) and 0 <= now - resource.get('sampled_at', 0) <= 90)
            with monitor.lock:
                active = (monitor.incidents.get(key) or {}).get('active')
            bad = value >= (threshold - 5 if active else threshold) if known else None
            monitor.observe(key, bad, title + '：' + row.get('name', ''),
                f'当前 {value:.1f}% · 阈值 {threshold}%（恢复阈值 {threshold - 5}%）' if known else '指标暂不可读，不能判断已恢复',
                'docker', 'warning', metadata={'container_id': row['id'], 'container_name': row.get('name'),
                    'reason': reason, 'metric': field, 'value': value, 'threshold': threshold,
                    'resource_evidence': {k: v for k, v in resource.items() if k != 'notice'}},
                observed_at=resource.get('sampled_at') if known else None)
    with monitor.lock:
        keys = [key for key in monitor.incidents if ':resource:' in key and key.startswith('docker:container:') and key not in seen]
    for key in keys:
        monitor.observe(key, None, '', '', 'docker')
