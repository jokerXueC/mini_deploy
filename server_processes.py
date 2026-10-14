"""Bounded Linux process counters; no commands, arguments or environment reads."""
from __future__ import annotations

import os
from pathlib import Path
import re
import time


class Sampler:
    def __init__(self, proc=Path('/proc'), clock=time.monotonic, limit=4096, budget=.15):
        self.proc, self.clock, self.limit, self.budget = Path(proc), clock, limit, budget
        self.previous = {}
        self.ticks = os.sysconf('SC_CLK_TCK') if hasattr(os, 'sysconf') else 100
        self.page = os.sysconf('SC_PAGE_SIZE') if hasattr(os, 'sysconf') else 4096
        self.users = {}
        try:
            for line in Path('/etc/passwd').read_text().splitlines():
                fields = line.split(':')
                if len(fields) >= 4:
                    self.users[int(fields[2])] = fields[0][:80]
        except (OSError, ValueError):
            pass

    @staticmethod
    def read(path):
        with path.open(encoding='utf-8', errors='replace') as handle:
            return handle.read(16384)

    def sample(self, server, docker=None, now=None):
        now = time.time() if now is None else now
        started = self.clock()
        if not (self.proc / 'stat').exists():
            return {'at': now, 'available': False, 'warnings': ['当前系统没有 Linux /proc，无法记录进程现场。'], 'processes': []}
        rows, current, denied, gone, scanned, limited = [], {}, 0, 0, 0, False
        total = (server.get('memory') or {}).get('total_mb') or 0
        cores = (server.get('cpu') or {}).get('logical_count') or os.cpu_count() or 1
        with os.scandir(self.proc) as entries:
            for entry in entries:
                if not entry.name.isdecimal():
                    continue
                if scanned >= self.limit or self.clock() - started >= self.budget:
                    limited = True
                    break
                scanned += 1
                path = self.proc / entry.name
                try:
                    text = self.read(path / 'stat')
                    end = text.rfind(')')
                    fields = text[end + 2:].split()
                    if end < 0 or len(fields) < 22:
                        raise ValueError('invalid proc stat')
                    pid, identity = int(entry.name), int(fields[19])
                    ticks = int(fields[11]) + int(fields[12])
                    rss = max(0, int(fields[21])) * self.page / 1048576
                    observed = self.clock()
                    uid = None
                    try:
                        status = self.read(path / 'status')
                        match = re.search(r'^Uid:\s+(\d+)', status, re.M)
                        uid = int(match[1]) if match else None
                    except PermissionError:
                        denied += 1
                    io = None
                    try:
                        counters = dict(line.split(':', 1) for line in self.read(path / 'io').splitlines() if ':' in line)
                        io = (int(counters['read_bytes']), int(counters['write_bytes']))
                    except PermissionError:
                        denied += 1
                    except (KeyError, ValueError, FileNotFoundError, ProcessLookupError):
                        pass
                    previous = self.previous.get(pid)
                    elapsed = observed - previous['at'] if previous and previous['identity'] == identity else 0
                    cpu, read, write = None, None, None
                    if elapsed > 0:
                        cpu = max(0, ticks - previous['ticks']) / self.ticks / elapsed * 100
                        if io is not None and previous['io'] is not None:
                            read = max(0, io[0] - previous['io'][0]) / elapsed / 1024
                            write = max(0, io[1] - previous['io'][1]) / elapsed / 1024
                    cpu_change = cpu - previous['cpu'] if elapsed > 0 and previous['cpu'] is not None and cpu is not None else None
                    rss_change = rss - previous['rss'] if elapsed > 0 else None
                    current[pid] = dict(identity=identity, ticks=ticks, io=io, at=observed, cpu=cpu, rss=rss)
                    rows.append({'pid': pid, 'start_ticks': identity, 'ppid': int(fields[1]),
                        'name': text[text.find('(') + 1:end][:160], 'uid': uid,
                        'user': self.users.get(uid, str(uid) if uid is not None else '未知'),
                        'state': fields[0], 'threads': int(fields[17]), 'rss_mib': rss,
                        'memory_percent': rss / total * 100 if total > 0 else None,
                        'cpu_percent': cpu, 'host_cpu_percent': cpu / cores if cpu is not None else None,
                        'cpu_change': cpu_change, 'rss_change_mib': rss_change,
                        'read_kib_s': read, 'write_kib_s': write, 'interval_seconds': elapsed or None})
                except PermissionError:
                    denied += 1
                except (OSError, ValueError, IndexError):
                    gone += 1
        self.previous = current
        selected = {}
        for field in ('cpu_percent', 'rss_mib', 'read_kib_s', 'write_kib_s', 'cpu_change', 'rss_change_mib'):
            for row in sorted((r for r in rows if r[field] is not None and r[field] > 0), key=lambda r: r[field], reverse=True)[:5]:
                selected[row['pid']] = row
        containers = {c.get('id'): c for c in (docker or {}).get('containers', [])}
        for row in selected.values():
            path = self.proc / str(row['pid'])
            row.update(service='', container_id='', container='', executable='')
            try:
                # Recheck identity before adding metadata to avoid PID reuse.
                check = self.read(path / 'stat')
                if int(check[check.rfind(')') + 2:].split()[19]) != row['start_ticks']:
                    row['metadata_unavailable'] = True
                    continue
                cgroup = self.read(path / 'cgroup')
                unit = re.search(r'/([^/\n]+\.service)(?:/|$)', cgroup)
                container = re.search(r'(?:docker[-/]|cri-containerd-)([a-f0-9]{64})(?:\.scope)?(?:/|$)', cgroup)
                row['service'] = unit[1][:160] if unit else ''
                row['container_id'] = container[1] if container else ''
                item = containers.get(row['container_id'], {})
                row['container'] = str(item.get('name') or '')[:160]
                row['container_state'] = str(item.get('state') or '')[:80]
                row['container_health'] = str(item.get('health') or '')[:80]
                row['executable'] = os.readlink(path / 'exe')[:512]
            except (OSError, ValueError, IndexError):
                row['metadata_unavailable'] = True
        warnings = []
        if limited:
            warnings.append('进程扫描达到数量或时间预算，排名仅覆盖已读取的进程。')
        if denied:
            warnings.append(f'{denied} 项进程计数读取权限不足，缺失值未补零。')
        if gone:
            warnings.append(f'{gone} 个进程在读取时退出或计数无法解析。')
        return {'at': now, 'available': True, 'scanned': scanned, 'readable': len(rows),
            'blocked_count': sum(r['state'] == 'D' for r in rows), 'zombie_count': sum(r['state'] == 'Z' for r in rows),
            'new_baselines': sum(r['cpu_percent'] is None for r in rows), 'limited': limited,
            'scan_ms': (self.clock() - started) * 1000, 'warnings': warnings,
            'processes': list(selected.values()), 'logical_cores': cores}
