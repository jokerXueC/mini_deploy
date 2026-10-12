"""Linux counter deltas and bounded, persistent container resource samples."""
from __future__ import annotations

import math
import re
import sqlite3
import stat
import threading
import time
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path

from certificates import trusted_path


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
                db.execute('PRAGMA max_page_count=32768')
                db.executescript('''CREATE TABLE IF NOT EXISTS samples (
                    container TEXT NOT NULL, ts INTEGER NOT NULL, name TEXT NOT NULL,
                    cpu REAL, memory REAL, PRIMARY KEY(container,ts));
                    CREATE INDEX IF NOT EXISTS sample_age ON samples(ts);''')
                yield db

    def record(self, containers, now=None):
        now = time.time() if now is None else now
        rows = []
        for item in containers:
            if not re.fullmatch('[0-9a-f]{64}', str(item.get('id', ''))):
                continue
            values = [item.get('cpu_percent'), item.get('memory_percent')]
            values = [v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0
                      else None for v in values]
            if all(v is None for v in values):
                continue
            rows.append((item['id'], int(now // 30) * 30, str(item.get('name', ''))[:256], *values))
        with self.connect() as db:
            db.executemany('INSERT OR IGNORE INTO samples VALUES(?,?,?,?,?)', rows)
            db.execute('DELETE FROM samples WHERE ts < ?', (now - 7 * 86400,))
            count = db.execute('SELECT COUNT(*) FROM samples').fetchone()[0]
            if count > self.max_rows:
                db.execute('DELETE FROM samples WHERE rowid IN (SELECT rowid FROM samples ORDER BY ts LIMIT ?)',
                           (count - self.max_rows,))

    def query(self, container, period='24h', now=None):
        if not re.fullmatch('[0-9a-f]{64}', container) or period not in {'1h', '24h', '7d'}:
            raise ValueError('请选择完整容器 ID 和有效的时间范围')
        now = time.time() if now is None else now
        seconds = {'1h': 3600, '24h': 86400, '7d': 604800}[period]
        bucket = max(30, seconds // 240)
        with self.connect() as db:
            points = [dict(row) for row in db.execute('''SELECT CAST(ts / ? AS INTEGER) * ? AS ts,
                AVG(cpu) AS cpu_percent, AVG(memory) AS memory_percent, COUNT(*) AS samples
                FROM samples WHERE container=? AND ts>=? AND ts<=? GROUP BY CAST(ts / ? AS INTEGER) ORDER BY ts''',
                (bucket, bucket, container, now - seconds, now, bucket))]
        return {'container': container, 'period': period, 'points': points, 'interval_seconds': bucket,
                'retention_days': 7, 'notice': '每 30 秒采样；重建容器按新 ID 单独记录。缺失采样不补零；较长时间范围按桶取平均。'}
