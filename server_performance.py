"""Continuous server samples, durable 30-second statistics and offline reports."""
from __future__ import annotations

import base64
from contextlib import closing, contextmanager, nullcontext
import hashlib
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

from certificates import trusted_path
from request_export import stamp, numeric, write_csv

INTERVAL = 30
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
                    PRIMARY KEY(run,ts)); CREATE INDEX IF NOT EXISTS server_age ON samples(ts);''')
                yield db

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
            if not batch:
                return
            try:
                with self.connect() as db:
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
                for bucket in batch:
                    key = (bucket['run'], bucket['ts'])
                    self.persisted[key] = bucket['observations']
                    if bucket['ts'] + INTERVAL <= now and self.pending.get(key, {}).get('observations') == bucket['observations']:
                        self.pending.pop(key, None)
                        self.persisted.pop(key, None)
                self.last_flush, self.error = now, ''

    def query(self, period='24h', now=None):
        if period not in PERIODS:
            raise ValueError('请选择 1 小时、24 小时或 7 天')
        now = time.time() if now is None else now
        since = now - PERIODS[period]
        with self.connect() as db:
            raw = [dict(row) for row in db.execute('SELECT * FROM samples WHERE ts>=? AND first<=? ORDER BY first,run', (since, now))]
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
            'notice': '每秒采集、每 30 秒持久保存平均值和峰值；无需打开网页。仅分析已保存采样，缺失不补零；峰值为实际 1 秒采样峰值。网络统计沿用所有非回环接口之和，可能包含虚拟接口流量。'}


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


def build(data, format='zip', theme='light'):
    if format not in {'csv', 'html', 'zip'} or theme not in {'light', 'dark'}:
        raise ValueError('性能导出格式无效')
    output = tempfile.TemporaryFile()
    try:
        if format == 'csv':
            write_csv(output, FIELDS, csv_points(data))
        elif format == 'html':
            output.write(report(data, theme))
        else:
            packaged = {**data, 'package': True}
            with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                with archive.open('performance.csv', 'w') as target:
                    write_csv(target, FIELDS, csv_points(data))
                summary = [{'metric': label, 'unit': unit, **data['summary'][key]} for key, label, unit in METRICS]
                with archive.open('summary.csv', 'w') as target:
                    write_csv(target, [('metric', '指标'), ('unit', '单位'), ('average', '平均值'), ('minimum', '最低值'),
                        ('peak', '峰值'), ('latest', '最后值'), ('count', '有效采样秒数')], summary)
                archive.writestr('report.html', report(packaged))
                archive.writestr('说明.txt', (data['notice'] + '\nCSV 时间为 UTC；根分区容量使用 GiB，内存使用 MiB。\n'
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
