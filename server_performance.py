"""Continuous server samples, durable 30-second statistics and offline reports."""
from __future__ import annotations

import base64
import csv
import gzip
from collections import deque
from contextlib import closing, contextmanager, nullcontext
import hashlib
import io
import json
import math
from pathlib import Path
import socket
import sqlite3
import stat
import tempfile
import threading
import time
import uuid
import zipfile
import zlib

from certificates import trusted_path
from request_export import stamp, numeric, write_csv, safe_cell

INTERVAL = 30
EVIDENCE_BUDGET = 32 * 1024 * 1024
RETENTION = 7 * 86400
PERIODS = {'1h': 3600, '24h': 86400, '7d': RETENTION}
METRICS = [
    ('cpu_percent', 'CPU', '%'), ('memory_percent', '内存', '%'), ('memory_used_mb', '已用内存', 'MiB'),
    ('swap_percent', 'Swap', '%'), ('swap_used_mb', '已用 Swap', 'MiB'),
    ('load_one', '1 分钟负载', ''), ('load_five', '5 分钟负载', ''), ('load_fifteen', '15 分钟负载', ''),
    ('disk_percent', '根分区占用', '%'), ('disk_used_gb', '根分区已用', 'GiB'), ('inode_percent', '根分区 inode', '%'),
    ('network_rx_kib_s', '网络接收', 'KiB/s'), ('network_tx_kib_s', '网络发送', 'KiB/s'),
    ('disk_read_kib_s', '磁盘读取', 'KiB/s'), ('disk_write_kib_s', '磁盘写入', 'KiB/s'),
    ('disk_iops', '磁盘 IOPS', '次/s'), ('disk_await_ms', '磁盘平均延迟', 'ms'),
    ('disk_busy_percent', '最忙磁盘忙碌率', '%'), ('iowait_percent', 'CPU I/O wait', '%')]


def values(server):
    memory, disk, network, load, io = (server.get(key) or {} for key in ('memory', 'disk', 'network', 'load', 'disk_io'))
    swap, inode = memory.get('swap') or {}, disk.get('inode') or {}
    result = {'cpu_percent': server.get('cpu_percent'), 'memory_percent': memory.get('percent'),
        'memory_used_mb': memory.get('used_mb'), 'swap_percent': swap.get('percent'), 'swap_used_mb': swap.get('used_mb'),
        'load_one': load.get('one'), 'load_five': load.get('five'), 'load_fifteen': load.get('fifteen'),
        'disk_percent': disk.get('percent'), 'disk_used_gb': disk.get('used_gb'), 'inode_percent': inode.get('percent'),
        'network_rx_kib_s': network.get('rx_kbps'), 'network_tx_kib_s': network.get('tx_kbps'),
        'iowait_percent': io.get('iowait_percent')}
    devices = io.get('devices') or []
    if io.get('available') and devices:
        for target, source in [('disk_read_kib_s', 'read_kib_s'), ('disk_write_kib_s', 'write_kib_s'), ('disk_iops', 'iops')]:
            valid = [numeric(device.get(source)) for device in devices]
            result[target] = math.fsum(v for v in valid if v is not None) if any(v is not None for v in valid) else None
        busy = [numeric(d.get('busy_percent')) for d in devices]
        result['disk_busy_percent'] = max((v for v in busy if v is not None), default=None)
        delays = [(numeric(d.get('await_ms')), numeric(d.get('iops'))) for d in devices]
        delays = [(delay, ops) for delay, ops in delays if delay is not None and ops is not None and ops > 0]
        result['disk_await_ms'] = math.fsum(delay * ops for delay, ops in delays) / math.fsum(ops for _, ops in delays) if delays else None
    return {key: numeric(result.get(key)) for key, _, _ in METRICS}


def capacity(server):
    cpu, memory, disk = (server.get(key) or {} for key in ('cpu', 'memory', 'disk'))
    return {'hostname': socket.gethostname(), 'cpu_model': str(cpu.get('model', ''))[:256],
        'logical_cores': numeric(cpu.get('logical_count')), 'physical_cores': numeric(cpu.get('physical_cores')),
        'memory_total_mb': numeric(memory.get('total_mb')), 'swap_total_mb': numeric((memory.get('swap') or {}).get('total_mb')),
        'disk_total_gb': numeric(disk.get('total_gb')), 'disk_path': str(disk.get('path', '/'))[:256]}


def add(stats, value, weight=1):
    if value is None:
        return
    if not stats:
        stats.update(count=0, sum=0, weight=0, minimum=value, peak=value, latest=value, high=0)
    stats['count'] += 1
    stats['sum'] += value * weight
    stats['weight'] += weight
    stats['minimum'] = min(stats['minimum'], value)
    stats['peak'] = max(stats['peak'], value)
    stats['latest'] = value
    stats['high'] += value >= 85


class History:
    def __init__(self, home, maintenance=nullcontext):
        self.path = Path(home) / 'performance' / 'server.sqlite3'
        self.maintenance = maintenance
        self.lock, self.db_lock, self.flush_lock = threading.RLock(), threading.RLock(), threading.Lock()
        self.run = uuid.uuid4().hex
        self.pending, self.persisted = {}, {}
        self.last_second, self.last_flush, self.error = None, None, ''
        self.contexts, self.previous_context = {}, {}
        self.recent, self.latest_scene = deque(maxlen=120), None

    @contextmanager
    def connect(self):
        with self.maintenance(), self.db_lock:
            for suffix in ('', '-journal', '-wal', '-shm'):
                path = Path(str(self.path) + suffix)
                trusted_path(path)
                if path.exists() and (not stat.S_ISREG(path.stat().st_mode) or path.stat().st_nlink != 1):
                    raise ValueError('服务器性能数据库路径不安全，原文件已保留')
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.path.parent.chmod(0o700)
            with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
                self.path.chmod(0o600)
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA max_page_count=32768')
                db.executescript('''CREATE TABLE IF NOT EXISTS samples (
                    run TEXT NOT NULL, ts INTEGER NOT NULL, first REAL NOT NULL, last REAL NOT NULL,
                    observations INTEGER NOT NULL, data TEXT NOT NULL, metadata TEXT NOT NULL,
                    PRIMARY KEY(run,ts)); CREATE INDEX IF NOT EXISTS server_age ON samples(ts);
                    CREATE TABLE IF NOT EXISTS process_context (
                    run TEXT NOT NULL, ts INTEGER NOT NULL, at REAL NOT NULL, score REAL NOT NULL,
                    summary TEXT NOT NULL, detail BLOB NOT NULL, PRIMARY KEY(run,ts));
                    CREATE INDEX IF NOT EXISTS process_age ON process_context(at);''')
                yield db

    def record_context(self, snapshot, server):
        """Retain the strongest observed scene per interval, independently of 1s sampling."""
        at = snapshot['at']
        metrics, reasons = values(server), []
        cores = (server.get('cpu') or {}).get('logical_count') or 1
        for key, threshold, label in [('cpu_percent', 85, 'CPU 高占用'), ('memory_percent', 90, '内存高占用'),
                                     ('iowait_percent', 20, 'I/O 等待偏高'), ('disk_busy_percent', 85, '磁盘忙碌')]:
            if metrics.get(key) is not None and metrics[key] >= threshold:
                reasons.append(label)
        if metrics.get('load_one') is not None and metrics['load_one'] > cores:
            reasons.append('系统负载超过逻辑核数')
        with self.lock:
            previous = self.previous_context
            if previous and 0 < at - previous['at'] <= 15:
                for key, delta, label in [('cpu_percent', 20, 'CPU 突升'), ('memory_used_mb', 256, '已用内存增加 ≥256 MiB')]:
                    if metrics.get(key) is not None and previous['metrics'].get(key) is not None and metrics[key] - previous['metrics'][key] >= delta:
                        reasons.append(label)
            self.previous_context = {'at': at, 'metrics': metrics}
            score = max((metrics.get('cpu_percent') or 0) / 85,
                        (metrics.get('memory_percent') or 0) / 90,
                        (metrics.get('disk_busy_percent') or 0) / 85,
                        (metrics.get('iowait_percent') or 0) / 20,
                        (metrics.get('load_one') or 0) / cores) + len(reasons) * .25
            ts = int(at // INTERVAL) * INTERVAL
            key = (self.run, ts)
            scene = {**snapshot, 'metrics': metrics, 'reasons': reasons or ['定时现场'],
                     'run': self.run, 'ts': ts, 'score': score, 'server_sampled_at': server.get('sampled_ts')}
            self.latest_scene = scene
            if key not in self.contexts or score >= self.contexts[key]['score']:
                self.contexts[key] = scene
            for expired in [k for k in self.contexts if k[1] < at - RETENTION]:
                self.contexts.pop(expired)

    def observe(self, server, now=None):
        """Only accumulate in memory; never block the 1-second sampler on disk."""
        now = time.time() if now is None else now
        metrics = values(server)
        if all(value is None for value in metrics.values()):
            return
        second, ts = int(now), int(now // INTERVAL) * INTERVAL
        with self.lock:
            if second == self.last_second:
                return
            if self.last_second is not None and second < self.last_second:
                self.run = uuid.uuid4().hex
                self.last_flush = None
            self.last_second = second
            key = (self.run, ts)
            bucket = self.pending.setdefault(key, {'run': self.run, 'ts': ts, 'first': now, 'last': now,
                'observations': 0, 'seconds': 0, 'stats': {name: {} for name, _, _ in METRICS}, 'metadata': {}})
            bucket['last'] = now
            bucket['observations'] += 1
            bucket['seconds'] |= 1 << (second - ts)
            bucket['metadata'] = capacity(server)
            self.recent.append({'sampled_ts': now, 'metrics': metrics, 'logical_cores': bucket['metadata']['logical_cores']})
            for field, value in metrics.items():
                weight = metrics.get('disk_iops') if field == 'disk_await_ms' else 1
                if weight is not None and weight > 0:
                    add(bucket['stats'][field], value, weight)
            # Bound only pending, unpersisted monitoring samples, not server files.
            expired = [key for key in self.pending if key[1] < now - RETENTION]
            for key in expired:
                self.pending.pop(key)
                self.persisted.pop(key, None)

    def flush(self, now=None, force=False):
        now = time.time() if now is None else now
        with self.flush_lock:
            if not force and self.last_flush is not None and now - self.last_flush < INTERVAL:
                return
            with self.lock:
                batch = [json.loads(json.dumps(bucket)) for key, bucket in self.pending.items()
                         if self.persisted.get(key) != bucket['observations']]
                contexts = list(self.contexts.values())
            if not batch and not contexts:
                return
            try:
                with self.connect() as db:
                    db.execute('DELETE FROM process_context WHERE at < ?', (now - RETENTION,))
                    encoded = [(c['run'], c['ts'], c['at'], c['score'], json.dumps(context_summary(c), ensure_ascii=False),
                                zlib.compress(json.dumps(c, ensure_ascii=False, allow_nan=False).encode())) for c in contexts]
                    encoded.sort(key=lambda row: row[2], reverse=True)
                    size, keep = 0, []
                    for row in encoded:
                        if size + len(row[-1]) <= EVIDENCE_BUDGET * .875:
                            keep.append(row)
                            size += len(row[-1])
                    encoded = keep
                    used = db.execute('SELECT COALESCE(SUM(length(detail)),0) FROM process_context').fetchone()[0]
                    required = sum(len(row[-1]) for row in encoded)
                    if used + required > EVIDENCE_BUDGET:
                        victims = []
                        for row in db.execute('SELECT rowid,length(detail) FROM process_context ORDER BY at'):
                            victims.append((row[0],))
                            used -= row[1]
                            if used + required <= EVIDENCE_BUDGET * .875:
                                break
                        db.executemany('DELETE FROM process_context WHERE rowid=?', victims)
                    db.executemany('''INSERT INTO process_context VALUES(?,?,?,?,?,?) ON CONFLICT(run,ts) DO UPDATE SET
                        at=excluded.at,score=excluded.score,summary=excluded.summary,detail=excluded.detail
                        WHERE excluded.score >= process_context.score''', encoded)
                    db.executemany('''INSERT INTO samples VALUES(?,?,?,?,?,?,?) ON CONFLICT(run,ts) DO UPDATE SET
                        first=excluded.first,last=excluded.last,observations=excluded.observations,
                        data=excluded.data,metadata=excluded.metadata''',
                        [(b['run'], b['ts'], b['first'], b['last'], b['observations'], json.dumps({**b['stats'], '_seconds': b['seconds']}, allow_nan=False),
                          json.dumps(b['metadata'], ensure_ascii=False)) for b in batch])
                    db.execute('DELETE FROM samples WHERE ts < ?', (now - RETENTION,))
                    # Multiple restarts in one interval must not erase earlier samples.
                    count = db.execute('SELECT COUNT(*) FROM samples').fetchone()[0]
                    if count > 22000:
                        db.execute('DELETE FROM samples WHERE rowid IN (SELECT rowid FROM samples ORDER BY ts LIMIT ?)', (count - 22000,))
            except (ValueError, OSError, sqlite3.Error, RuntimeError):
                self.error = '性能历史写入失败，请检查数据目录、维护状态或磁盘空间；待写采样暂存于内存。'
                raise
            with self.lock:
                for scene in contexts:
                    key = (scene['run'], scene['ts'])
                    if self.contexts.get(key) is scene:
                        self.contexts.pop(key)
                for bucket in batch:
                    key = (bucket['run'], bucket['ts'])
                    self.persisted[key] = bucket['observations']
                    if bucket['ts'] + INTERVAL <= now and self.pending.get(key, {}).get('observations') == bucket['observations']:
                        self.pending.pop(key, None)
                        self.persisted.pop(key, None)
                self.last_flush, self.error = now, ''

    def version(self, period='24h', now=None):
        if period not in PERIODS:
            raise ValueError('请选择 1 小时、24 小时或 7 天')
        now = time.time() if now is None else now
        with self.connect() as db:
            samples = tuple(db.execute('SELECT MAX(last),SUM(observations),COUNT(*) FROM samples').fetchone())
            scenes = tuple(db.execute('SELECT MAX(at),SUM(score),COUNT(*) FROM process_context').fetchone())
            info = self.path.stat()
            modified = (info.st_mtime_ns, info.st_size, info.st_ino)
        stale = bool(samples[0] is not None and now - samples[0] > 65)
        signature = (period, int(now // INTERVAL), samples, scenes, modified, self.error, stale)
        return '"' + hashlib.sha256(repr(signature).encode()).hexdigest() + '"'

    def query(self, period='24h', now=None, include_details=True):
        if period not in PERIODS:
            raise ValueError('请选择 1 小时、24 小时或 7 天')
        now = time.time() if now is None else now
        since = now - PERIODS[period]
        with self.connect() as db:
            raw = [dict(row) for row in db.execute('SELECT * FROM samples WHERE ts>=? AND first<=? ORDER BY first,run', (since, now))]
            contexts = [json.loads(row[0]) for row in db.execute(
                'SELECT summary FROM process_context WHERE at>=? AND at<=? ORDER BY at DESC', (since, now))]
            # Bound embedded details. The ZIP includes every retained process scene as CSV.
            chosen = {(c['run'], c['ts']) for c in contexts[:120] + sorted(contexts, key=lambda c: c['score'], reverse=True)[:120]} if include_details else set()
            details = []
            for run, ts in chosen:
                row = db.execute('SELECT detail FROM process_context WHERE run=? AND ts=?', (run, ts)).fetchone()
                details.append(json.loads(zlib.decompress(row[0])))
        summary = {key: {} for key, _, _ in METRICS}
        points, samples, sessions, covered = [], 0, set(), {}
        for row in raw:
            stats = json.loads(row['data'])
            covered[row['ts']] = covered.get(row['ts'], 0) | stats.get('_seconds', 0)
            sessions.add(row['run'])
            samples += row['observations']
            point = {key: row[key] for key in ('ts', 'first', 'last', 'observations', 'run')}
            point['weights'] = {}
            for field, _, _ in METRICS:
                values = stats.get(field) or {}
                point[field] = values.get('sum', 0) / values['weight'] if values.get('weight') else None
                point[field + '_peak'] = values.get('peak')
                point[field + '_samples'] = values.get('count', 0)
                point['weights'][field] = values.get('weight', 0)
                if values.get('count'):
                    target = summary[field]
                    if not target:
                        target.update(count=0, sum=0, weight=0, minimum=values['minimum'], peak=values['peak'], latest=values['latest'], high=0)
                    for key in ('count', 'sum', 'weight', 'high'):
                        target[key] += values[key]
                    target['minimum'] = min(target['minimum'], values['minimum'])
                    target['peak'] = max(target['peak'], values['peak'])
                    target['latest'] = values['latest']
            points.append(point)
        for stats in summary.values():
            if stats:
                stats['average'] = stats['sum'] / stats['weight'] if stats['weight'] else None
        metadata = json.loads(raw[-1]['metadata']) if raw else {}
        latest = max((row['last'] for row in raw), default=None)
        coverage_seconds = sum(1 for ts, mask in covered.items() for second in range(INTERVAL)
                               if mask & (1 << second) and since <= ts + second <= now)
        return {'period': period, 'since': stamp(since), 'until': stamp(now), 'generated_at': stamp(now),
            'interval_seconds': INTERVAL, 'retention_days': 7, 'sample_seconds': samples,
            'coverage_seconds': coverage_seconds,
            'coverage_percent': min(100, coverage_seconds / PERIODS[period] * 100), 'session_count': len(sessions),
            'first_at': stamp(min(row['first'] for row in raw)) if raw else '', 'latest_at': stamp(latest) if latest is not None else '',
            'stale': latest is not None and now - latest > 65, 'error': self.error,
            'metadata': metadata, 'points': points, 'summary': summary, 'metrics': METRICS,
            'contexts': contexts, 'context_details': details,
            'context_notice': '进程每约 5 秒读取计数差值，每 30 秒保留压力最明显的一次现场；进程速率为计数窗口平均，整机指标为最近的 1 秒采样，窗口不同可能产生差异。CPU 以单逻辑核为 100%，整机占比另列；常驻内存 RSS 可能包含共享页。磁盘 I/O 为内核记账的存储读写字节，不是网络流量；写入记账与设备落盘时刻可能不同。排名只覆盖采样时仍存在且有读取权限的进程，短暂进程和两次采样之间的尖峰可能遗漏。同一时刻占用高是排查线索，不等于已确认因果；不采集命令参数、环境变量或文件内容。现场最多保留 7 天，压缩明细达到 32 MiB 时较早现场会先淘汰；总体性能统计独立保留。',
            'notice': '每秒采集、每 30 秒持久保存平均值和峰值；无需打开网页。仅分析已保存采样，缺失不补零；峰值为实际 1 秒采样峰值。网络统计沿用所有非回环接口之和，可能包含虚拟接口流量。'}

    def nearby(self, at, radius=300):
        with self.connect() as db:
            rows = list(db.execute('SELECT * FROM samples WHERE ts>=? AND first<=? ORDER BY first LIMIT 100', (at - radius - INTERVAL, at + radius)))
            scenes = list(db.execute('SELECT detail FROM process_context WHERE at>=? AND at<=? ORDER BY at LIMIT 30', (at - radius, at + radius)))
        points = []
        for row in rows:
            stats = json.loads(row['data'])
            point = {'ts': row['first'], 'interval_seconds': INTERVAL}
            for field, _, _ in METRICS:
                s = stats.get(field) or {}
                point[field] = s['sum'] / s['weight'] if s.get('weight') else None
                point[field + '_peak'] = s.get('peak')
            points.append(point)
        return {'points': points, 'scenes': [json.loads(zlib.decompress(row[0])) for row in scenes]}

    def context(self, run, ts):
        if not isinstance(run, str) or len(run) != 32 or any(c not in '0123456789abcdef' for c in run):
            raise ValueError('现场批次无效')
        ts = int(ts)
        with self.connect() as db:
            row = db.execute('SELECT detail FROM process_context WHERE run=? AND ts=?', (run, ts)).fetchone()
        return json.loads(zlib.decompress(row[0])) if row else None

    def export_contexts(self, data, scenes, processes):
        since, until = data['since'], data['until']
        # Release the database lock between bounded pages; files use the existing private spool.
        from datetime import datetime
        start, end = (datetime.fromisoformat(t.replace('Z', '+00:00')).timestamp() for t in (since, until))
        marker = None
        while True:
            with self.connect() as db:
                rows = list(db.execute('''SELECT at,run,ts,detail FROM process_context WHERE at>=? AND at<=?
                    AND (? IS NULL OR (at,run,ts) > (?,?,?)) ORDER BY at,run,ts LIMIT 128''',
                    (start, end, None if marker is None else 1, *(marker or (0, '', 0)))))
            if not rows:
                break
            for row in rows:
                scene = json.loads(zlib.decompress(row['detail']))
                append_csv(scenes, CONTEXT_FIELDS, [context_summary(scene)])
                append_csv(processes, PROCESS_FIELDS, [{**p, 'at': stamp(scene['at']), 'run': scene['run'],
                    'ts': scene['ts'], 'reasons': '；'.join(scene['reasons'])} for p in scene['processes']])
            marker = (rows[-1]['at'], rows[-1]['run'], rows[-1]['ts'])


def context_summary(scene):
    rows = scene.get('processes') or []
    result = {key: scene.get(key) for key in ('run', 'ts', 'at', 'score', 'available', 'scanned', 'readable',
        'blocked_count', 'zombie_count', 'new_baselines', 'scan_ms', 'warnings', 'reasons')}
    result['at_text'] = stamp(scene['at'])
    result['reasons_text'] = '；'.join(scene.get('reasons', []))
    result['metrics'] = scene.get('metrics', {})
    for label, field in [('cpu', 'cpu_percent'), ('memory', 'rss_mib'), ('write', 'write_kib_s')]:
        valid = [p for p in rows if p.get(field) is not None]
        top = max(valid, key=lambda p: p[field], default={})
        result[label + '_name'] = top.get('name', '')
        result[label + '_pid'] = top.get('pid')
        result[label + '_value'] = top.get(field)
    return result


CONTEXT_FIELDS = [('at_text', '现场时间 (UTC)'), ('run', '批次'), ('ts', '区间'), ('reasons_text', '现场提示'),
    ('scanned', '读取进程数'), ('blocked_count', '不可中断等待进程数'), ('zombie_count', '僵尸进程数'),
    ('cpu_name', 'CPU最高进程'), ('cpu_pid', 'CPU最高PID'), ('cpu_value', 'CPU单核占用 (%)'),
    ('memory_name', '内存最高进程'), ('memory_pid', '内存最高PID'), ('memory_value', '常驻内存 (MiB)'),
    ('write_name', '写入最高进程'), ('write_pid', '写入最高PID'), ('write_value', '磁盘写入 (KiB/s)')]
PROCESS_FIELDS = [('at', '现场时间 (UTC)'), ('run', '批次'), ('ts', '区间'), ('reasons', '现场提示'),
    ('pid', 'PID'), ('start_ticks', '进程启动标识'), ('ppid', '父PID'), ('name', '进程'), ('user', '用户'),
    ('uid', 'UID'), ('state', '状态'), ('threads', '线程数'), ('interval_seconds', '计数间隔 (s)'),
    ('cpu_percent', 'CPU单核占用 (%)'), ('host_cpu_percent', 'CPU整机占比 (%)'),
    ('cpu_change', 'CPU较上次变化 (百分点)'), ('rss_mib', '常驻内存 (MiB)'),
    ('rss_change_mib', '常驻内存较上次变化 (MiB)'), ('memory_percent', '整机内存占比 (%)'),
    ('read_kib_s', '磁盘读取 (KiB/s)'), ('write_kib_s', '磁盘写入 (KiB/s)'),
    ('service', '服务'), ('container', '容器'), ('container_id', '容器ID'),
    ('container_state', '容器状态'), ('container_health', '容器健康'), ('executable', '可执行文件')]


def append_csv(target, fields, rows):
    text = io.StringIO(newline='')
    writer = csv.writer(text)
    for row in rows:
        writer.writerow([safe_cell(row.get(key)) for key, _ in fields])
    target.write(text.getvalue().encode('utf-8'))

FIELDS = [('at', '区间起点 (UTC)'), ('first_at', '首次采样 (UTC)'), ('last_at', '最后采样 (UTC)'),
          ('observations', '采样秒数'), ('run', '采样批次')]
FIELDS += [(key + suffix, label + title + (f' ({unit})' if unit else '')) for key, label, unit in METRICS
           for suffix, title in [('', '平均'), ('_peak', '峰值')]]
FIELDS += [(key + '_samples', label + '有效采样数') for key, label, _ in METRICS]
FIELDS += [('disk_delay_weight', '磁盘延迟 IOPS 权重（用于加权平均）')]


def csv_points(data):
    return [{**p, 'at': stamp(p['ts']), 'first_at': stamp(p['first']), 'last_at': stamp(p['last']),
             'disk_delay_weight': p['weights'].get('disk_await_ms', 0)} for p in data['points']]


def report(data, theme='light'):
    directory = Path(__file__).resolve().parent / 'ui'
    javascript = (directory / 'server-performance-report.js').read_text(encoding='utf-8')
    digest = base64.b64encode(hashlib.sha256(javascript.encode()).digest()).decode()
    encoded = base64.b64encode(json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()).decode()
    css = (directory / 'request-report.css').read_text(encoding='utf-8') + (directory / 'server-performance.css').read_text(encoding='utf-8')
    return ((directory / 'server-performance-report.html').read_text(encoding='utf-8')
        .replace('__REPORT_CSS__', css).replace('__REPORT_DATA__', encoded).replace('__SCRIPT_HASH__', digest)
        .replace('__THEME__', 'dark' if theme == 'dark' else 'light').replace('__REPORT_JS__', javascript)).encode()


def build(data, format='zip', theme='light', history=None, *, compress=False):
    if format not in {'csv', 'html', 'zip'} or theme not in {'light', 'dark'}:
        raise ValueError('性能导出格式无效')
    output = tempfile.TemporaryFile()
    try:
        if format == 'csv':
            write_csv(output, FIELDS, csv_points(data))
        elif format == 'html':
            content = report(data, theme)
            output.write(gzip.compress(content, compresslevel=5, mtime=0) if compress else content)
        else:
            packaged = {**data, 'package': True}
            with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                with archive.open('performance.csv', 'w') as target:
                    write_csv(target, FIELDS, csv_points(data))
                summary = [{'metric': label, 'unit': unit, **data['summary'][key]} for key, label, unit in METRICS]
                with archive.open('summary.csv', 'w') as target:
                    write_csv(target, [('metric', '指标'), ('unit', '单位'), ('average', '平均值'), ('minimum', '最低值'),
                        ('peak', '峰值'), ('latest', '最后值'), ('count', '有效采样秒数')], summary)
                with tempfile.TemporaryFile() as scenes, tempfile.TemporaryFile() as processes:
                    write_csv(scenes, CONTEXT_FIELDS, [])
                    write_csv(processes, PROCESS_FIELDS, [])
                    if history is not None:
                        history.export_contexts(data, scenes, processes)
                    else:
                        for scene in data.get('context_details', []):
                            append_csv(scenes, CONTEXT_FIELDS, [context_summary(scene)])
                            append_csv(processes, PROCESS_FIELDS, [{**p, 'at': stamp(scene['at']), 'run': scene['run'],
                                'ts': scene['ts'], 'reasons': '；'.join(scene['reasons'])} for p in scene['processes']])
                    for name, source in [('scenes.csv', scenes), ('processes.csv', processes)]:
                        source.seek(0)
                        with archive.open(name, 'w') as target:
                            while chunk := source.read(256 * 1024):
                                target.write(chunk)
                archive.writestr('report.html', report(packaged))
                archive.writestr('说明.txt', (data['notice'] + '\nCSV 时间为 UTC；根分区容量使用 GiB，内存使用 MiB。\n'
                    + data.get('context_notice', '') + '\nscenes.csv 为现场索引，processes.csv 为所有保留现场的进程明细；HTML 内嵌最近及高压力现场最多 240 组。导出期间继续采样，现场明细按实际读取时已保存的记录导出，并发更新可能与 HTML 索引略有差异；以各行实际时间为准。\n'
                    + '平均值按有效 1 秒采样加权；I/O 延迟按采样的 IOPS 加权，不是各设备延迟的简单平均。\n'
                    + '服务器或 Agent 停机期间无法采集；安装后从当前时刻开始，不能补回过去 24 小时。\n'
                    + f"导出范围：{data['since']} 至 {data['until']}；覆盖率：{data['coverage_percent']:.2f}%\n").encode())
        output.seek(0, 2)
        length = output.tell()
        output.seek(0)
        return output, length, {'csv': 'text/csv; charset=utf-8', 'html': 'text/html; charset=utf-8', 'zip': 'application/zip'}[format]
    except Exception:
        output.close()
        raise
