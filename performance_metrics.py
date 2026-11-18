"""Linux counter deltas and bounded, persistent container resource samples."""
from __future__ import annotations

import math
import json
import re
import sqlite3
import stat
import threading
import time
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path

from certificates import trusted_path
from container_resources import METRICS, valid


class DiskSampler:
    def __init__(self, proc=Path('/proc'), sys=Path('/sys/block')):
        self.proc, self.sys = proc, sys
        self.previous = None

    def counters(self):
        devices = {p.name for p in self.sys.iterdir() if not p.name.startswith(('loop', 'ram', 'sr'))
                   and not list((p / 'slaves').iterdir())}
        disks = {}
        for line in (self.proc / 'diskstats').read_text().splitlines():
            fields = line.split()
            if len(fields) >= 14 and fields[2] in devices:
                disks[fields[2]] = tuple(int(fields[n]) for n in (3, 5, 6, 7, 9, 10, 12))
        cpu = next(line.split()[1:] for line in (self.proc / 'stat').read_text().splitlines() if line.startswith('cpu '))
        values = [int(v) for v in cpu[:8]]
        return disks, (sum(values), values[4])

    def sample(self, now=None):
        now = time.monotonic() if now is None else now
        try:
            disks, cpu = self.counters()
        except (OSError, ValueError, StopIteration, IndexError):
            self.previous = None
            return {'available': False, 'devices': [], 'iowait_percent': None}
        previous, self.previous = self.previous, (now, disks, cpu)
        if not previous or now <= previous[0]:
            return {'available': True, 'devices': [], 'iowait_percent': None}
        elapsed, old, prev_cpu = now - previous[0], previous[1], previous[2]
        total, wait = cpu[0] - prev_cpu[0], cpu[1] - prev_cpu[1]
        result = []
        for name, values in sorted(disks.items()):
            if name not in old:
                continue
            delta = [a - b for a, b in zip(values, old[name])]
            if min(delta) < 0:
                continue
            reads, sectors_read, read_ms, writes, sectors_written, write_ms, busy_ms = delta
            ops = reads + writes
            result.append({'device': name, 'read_kib_s': sectors_read / 2 / elapsed,
                           'write_kib_s': sectors_written / 2 / elapsed, 'iops': ops / elapsed,
                           'await_ms': (read_ms + write_ms) / ops if ops else None,
                           'busy_percent': min(100, busy_ms / elapsed / 10)})
        return {'available': True, 'devices': result,
                'iowait_percent': wait / total * 100 if total > 0 and 0 <= wait <= total else None}


class ContainerHistory:
    def __init__(self, home, maintenance=nullcontext, max_rows=200_000):
        self.path = Path(home) / 'performance' / 'containers.sqlite3'
        self.maintenance, self.max_rows = maintenance, max_rows
        self.lock = threading.RLock()

    @contextmanager
    def connect(self):
        with self.maintenance(), self.lock:
            for suffix in ('', '-journal', '-wal', '-shm'):
                path = Path(str(self.path) + suffix)
                trusted_path(path)
                if path.exists() and (not stat.S_ISREG(path.stat().st_mode) or path.stat().st_nlink != 1):
                    raise ValueError('性能数据库路径不安全，原文件已保留')
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
                self.path.chmod(0o600)
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('PRAGMA max_page_count=32768')
                db.executescript('''CREATE TABLE IF NOT EXISTS samples (
                    container TEXT NOT NULL, ts INTEGER NOT NULL, name TEXT NOT NULL,
                    cpu REAL, memory REAL, PRIMARY KEY(container,ts));
                    CREATE INDEX IF NOT EXISTS sample_age ON samples(ts);
                    CREATE TABLE IF NOT EXISTS resource_samples(container TEXT,ts INTEGER,data TEXT,
                        PRIMARY KEY(container,ts),FOREIGN KEY(container,ts) REFERENCES samples(container,ts) ON DELETE CASCADE);
                    CREATE TABLE IF NOT EXISTS container_rollups(container TEXT,ts INTEGER,name TEXT,
                        samples INTEGER,first INTEGER,last INTEGER,data TEXT,PRIMARY KEY(container,ts));
                    CREATE INDEX IF NOT EXISTS container_rollup_age ON container_rollups(ts);
                    CREATE TABLE IF NOT EXISTS container_history_meta(key TEXT PRIMARY KEY,value REAL);''')
                yield db

    @staticmethod
    def _merge(destination, source):
        for key, (total, count, peak) in source.items():
            if not count:
                continue
            old = destination.setdefault(key, [0, 0, peak])
            old[0] += total
            old[1] += count
            old[2] = max(old[2], peak)

    @staticmethod
    def _raw(row):
        values = {'cpu_percent': row['cpu'], 'memory_percent': row['memory']}
        values.update(json.loads(row['data']) if row['data'] else {})
        return {key: [value, 1, value] for key, value in values.items() if valid(value)}

    def _compact(self, db, rows):
        groups = {}
        for row in rows:
            key = row['container'], int(row['ts'] // 300) * 300
            if key not in groups:
                old = db.execute('SELECT * FROM container_rollups WHERE container=? AND ts=?', key).fetchone()
                groups[key] = (dict(old) if old else {'container': key[0], 'ts': key[1], 'name': row['name'],
                    'samples': 0, 'first': row['ts'], 'last': row['ts'], 'data': '{}'})
                groups[key]['stats'] = json.loads(groups[key]['data'])
            group = groups[key]
            self._merge(group['stats'], self._raw(row))
            group['samples'] += 1
            group['first'] = min(group['first'], row['ts'])
            group['last'] = max(group['last'], row['ts'])
        for group in groups.values():
            db.execute('INSERT OR REPLACE INTO container_rollups VALUES(?,?,?,?,?,?,?)',
                (group['container'], group['ts'], group['name'], group['samples'], group['first'], group['last'],
                 json.dumps(group['stats'], separators=(',', ':'), allow_nan=False)))
        db.executemany('DELETE FROM samples WHERE container=? AND ts=?', [(row['container'], row['ts']) for row in rows])

    def _capacity(self, db, now):
        # Leave write headroom and reuse freed pages; never delete or recreate a history file.
        pages = db.execute('PRAGMA page_count').fetchone()[0] - db.execute('PRAGMA freelist_count').fetchone()[0]
        crowded = pages * db.execute('PRAGMA page_size').fetchone()[0] > 96 * 1024**2
        count = db.execute('SELECT COUNT(*) FROM container_rollups').fetchone()[0]
        if crowded or count > self.max_rows:
            trim = max(count - self.max_rows, count // 5 if crowded else 0)
            db.execute('DELETE FROM container_rollups WHERE rowid IN (SELECT rowid FROM container_rollups ORDER BY ts LIMIT ?)', (trim,))
            if crowded:
                count_raw = db.execute('SELECT COUNT(*) FROM samples').fetchone()[0]
                db.execute('DELETE FROM samples WHERE rowid IN (SELECT rowid FROM samples ORDER BY ts LIMIT ?)', (max(1, count_raw // 5),))
            db.execute("INSERT OR REPLACE INTO container_history_meta VALUES('limited_at',?)", (now,))

    def record(self, containers, now=None):
        now = time.time() if now is None else now
        rows = []
        for item in containers:
            if not re.fullmatch('[0-9a-f]{64}', str(item.get('id', ''))):
                continue
            values = [item.get('cpu_percent'), item.get('memory_percent')]
            values = [v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0
                      else None for v in values]
            resource = item.get('resources') or {}
            extra = {key: resource[key] for key, _, _ in METRICS if valid(resource.get(key))}
            if all(v is None for v in values) and not any(key != 'memory_limit_bytes' for key in extra):
                continue
            rows.append(((item['id'], int(now // 30) * 30, str(item.get('name', ''))[:256], *values), extra))
        with self.connect() as db:
            db.execute('DELETE FROM samples WHERE ts < ?', (now - 7 * 86400,))
            db.execute('DELETE FROM container_rollups WHERE last < ?', (now - 7 * 86400,))
            self._capacity(db, now)
            for row, extra in rows:
                inserted = db.execute('INSERT OR IGNORE INTO samples VALUES(?,?,?,?,?)', row).rowcount
                if inserted and extra:
                    db.execute('INSERT INTO resource_samples VALUES(?,?,?)',
                               (row[0], row[1], json.dumps(extra, separators=(',', ':'), allow_nan=False)))
            old = list(db.execute('SELECT s.*,r.data FROM samples s LEFT JOIN resource_samples r USING(container,ts) '
                                  'WHERE s.ts<? ORDER BY s.ts LIMIT 4000', (now - 6 * 3600,)))
            if old:
                self._compact(db, old)
            count = db.execute('SELECT COUNT(*) FROM samples').fetchone()[0]
            if count > self.max_rows:
                old = list(db.execute('SELECT s.*,r.data FROM samples s LEFT JOIN resource_samples r USING(container,ts) '
                                      'ORDER BY s.ts LIMIT ?', (count - self.max_rows,)))
                self._compact(db, old)

    def _records(self, db, since, until, target=''):
        for row in db.execute('SELECT s.*,r.data FROM samples s LEFT JOIN resource_samples r USING(container,ts) '
                              "WHERE s.ts>=? AND s.ts<? AND (?='' OR s.container=?)", (since, until, target, target)):
            yield {'container': row['container'], 'ts': row['ts'], 'name': row['name'], 'samples': 1,
                   'first': row['ts'], 'last': row['ts'], 'stats': self._raw(row)}
        for row in db.execute("SELECT * FROM container_rollups WHERE ts>=? AND ts<? AND (?='' OR container=?)",
                              (since, until, target, target)):
            yield {**dict(row), 'stats': json.loads(row['data'])}

    def aggregate(self, since, until, target=''):
        groups, edge = {}, min(300, max(30, (until - since) / 4))
        with self.connect() as db:
            for row in self._records(db, since, until, target):
                group = groups.setdefault(row['container'], {'container': row['container'], 'name': row['name'],
                    'samples': 0, 'stats': {}, 'start': {}, 'end': {}})
                group['samples'] += row['samples']
                self._merge(group['stats'], row['stats'])
                if row['ts'] < since + edge:
                    self._merge(group['start'], row['stats'])
                if row['ts'] >= until - edge:
                    self._merge(group['end'], row['stats'])
        for group in groups.values():
            for key, field in [('cpu_percent', 'cpu'), ('memory_percent', 'memory')]:
                total, count, peak = group['stats'].get(key, (0, 0, None))
                group[field] = total / count if count else None
                group[field + '_samples'] = count
                for part in ('start', 'end'):
                    a, b, _ = group[part].get(key, (0, 0, None))
                    group[part + '_' + field] = a / b if b else None
            for key in ('stats', 'start', 'end'):
                del group[key]
        return list(groups.values())

    def query(self, container, period='24h', now=None):
        if not re.fullmatch('[0-9a-f]{64}', container) or period not in {'1h', '24h', '7d'}:
            raise ValueError('请选择完整容器 ID 和有效的时间范围')
        now = time.time() if now is None else now
        seconds = {'1h': 3600, '24h': 86400, '7d': 604800}[period]
        bucket = {'1h': 30, '24h': 300, '7d': 3600}[period]
        groups, overall, first, last, samples = {}, {}, None, None, 0
        with self.connect() as db:
            for row in self._records(db, now - seconds, now + .001, container):
                group = groups.setdefault(int(row['ts'] // bucket) * bucket, {'stats': {}, 'samples': 0})
                group['samples'] += row['samples']
                samples += row['samples']
                self._merge(group['stats'], row['stats'])
                self._merge(overall, row['stats'])
                first = min(first, row['first']) if first is not None else row['first']
                last = max(last, row['last']) if last is not None else row['last']
            limited = db.execute("SELECT value FROM container_history_meta WHERE key='limited_at'").fetchone()
        points = []
        fields = [key for key, _, _ in METRICS] + ['memory_percent']
        for ts, group in sorted(groups.items()):
            point = {'ts': ts, 'samples': group['samples'], 'coverage_percent': min(100, group['samples'] * 30 / bucket * 100)}
            for key in fields:
                total, count, peak = group['stats'].get(key, (0, 0, None))
                point[key] = total / count if count else None
                point[key + '_peak'] = peak
            points.append(point)
        summary = {key: {'average': total / count, 'peak': peak, 'samples': count,
                         'coverage_percent': min(100, count * 30 / seconds * 100)}
                   for key, (total, count, peak) in overall.items() if count}
        return {'container': container, 'period': period, 'points': points, 'interval_seconds': bucket,
                'since': now - seconds, 'until': now, 'summary': summary, 'metrics': METRICS,
                'first_at': first, 'latest_at': last, 'coverage_percent': min(100, samples * 30 / seconds * 100),
                'capacity_limited': bool(limited and limited[0] >= now - seconds),
                'retention_days': 7, 'notice': '每 30 秒保存一次；近 6 小时保留原始采样，较早数据按 5 分钟聚合。'
                '趋势展示平均值并保留采样峰值；缺失不补零，重建按新 ID 记录。覆盖率按有效采样槽估计，'
                '稀疏历史不表示持续正常；较早数据的区间边界精度为 5 分钟。容量不足会裁剪较早历史并标明。'}
