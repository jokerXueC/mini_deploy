"""Persistent history for panel-owned request gateways, independent of UI polling."""
from __future__ import annotations

import hashlib
import json
import math
import queue
import re
import sqlite3
import stat
import subprocess
import threading
import time
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path

import nginx_requests
import request_gateway
from certificates import trusted_path

RETENTION = 7 * 86400
MAX_ROWS = 100_000
MAX_BYTES = 96 * 1024 * 1024  # Conservative row/index budget below the SQLite file limit.
MAX_LINES = 5000
MAX_ROLLUPS = 120_000
LATENCY_BOUNDS = [0, *[round(1.1 ** i, 4) for i in range(170)], 3_600_000_000]
PERIODS = {"1h": 3600, "24h": 86400, "7d": RETENTION}


class LatencyStats:
    """Nearest-rank percentiles over every valid latency in a selected group."""
    def __init__(self):
        self.values = []

    def step(self, value):
        if value is not None and math.isfinite(value) and value >= 0:
            self.values.append(value)

    def finalize(self):
        values = sorted(self.values)
        return json.dumps({'p95': values[math.ceil(len(values) * .95) - 1] if values else None,
                           'p99': values[math.ceil(len(values) * .99) - 1] if values else None})


def normalize_path(path):
    def part(value):
        if re.fullmatch(r"[0-9]+", value):
            return ":number"
        if re.fullmatch(r"(?:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{24}|[0-9a-f]{32})", value, re.I):
            return ":id"
        return value
    return "/".join(part(value) for value in path.split("/"))


def log_second(line):
    return int(datetime.fromisoformat(line.split(' ', 1)[0].replace('Z', '+00:00')).timestamp())


def read_log_batch(stream, since, until, checkpoint=None):
    """Read an ordered prefix; resume within a second using an anchored line offset."""
    checkpoint = checkpoint or {}
    process = subprocess.Popen(['docker', 'logs', '--timestamps', '--since', str(int(since)),
        '--until', str(until), stream], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    pending, stopped = queue.Queue(maxsize=32), threading.Event()

    def reader():
        try:
            while not stopped.is_set():
                value = process.stdout.readline(65537)
                while not stopped.is_set():
                    try:
                        pending.put(value, timeout=.1)
                        break
                    except queue.Full:
                        pass
                if not value:
                    return
        except (OSError, ValueError):
            pass

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    lines, identities = [], []
    second, offset, anchor = int(since), 0, ''
    resume_offset = checkpoint.get('offset', 0)
    verified = not resume_offset
    deadline = time.monotonic() + 8
    size, complete = 0, False
    try:
        while len(lines) < MAX_LINES and size < 8 * 1024**2:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                raw = pending.get(timeout=remaining)
            except queue.Empty:
                break
            if not raw:
                if process.wait(timeout=1):
                    raise ValueError('Docker 日志读取失败，游标未推进')
                complete = True
                break
            if len(raw) > 65536 or not raw.endswith(b'\n'):
                raise ValueError('Docker 单条日志过长或不完整，游标未推进')
            text = raw.decode('utf-8', errors='replace').rstrip('\r\n')
            try:
                current = log_second(text)
            except (ValueError, OverflowError):
                continue
            if current < second:
                raise ValueError('Docker 日志时间倒退，暂停采集以避免跳过请求')
            if current >= until:
                complete = True
                break
            if current != second:
                if not verified:
                    raise ValueError('Docker 日志已轮转，续读位置缺失；请核对采集缺口')
                second, offset, anchor = current, 0, ''
            offset += 1
            anchor = hashlib.sha256(anchor.encode() + raw).hexdigest()
            if not verified:
                if offset == resume_offset:
                    if anchor != checkpoint.get('anchor'):
                        raise ValueError('Docker 日志已轮转或改变，续读位置不一致；已保存历史保留')
                    verified = True
                continue
            lines.append(text)
            identities.append(f'{second}:{offset}')
            size += len(raw)
        if not verified:
            raise ValueError('Docker 续读位置暂不可用，游标未推进')
        if not complete and not lines:
            raise subprocess.TimeoutExpired('docker logs', 8)
        return lines, identities, ({} if complete else {'second': second, 'offset': offset, 'anchor': anchor})
    finally:
        stopped.set()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        thread.join(timeout=2)
        process.stdout.close()


class History:
    def __init__(self, home: Path, *, max_rows=MAX_ROWS, max_bytes=MAX_BYTES, retention=RETENTION, maintenance=nullcontext):
        self.home = home
        self.path = home / "request-history" / "history.sqlite3"
        self.max_rows, self.max_bytes, self.retention = max_rows, max_bytes, retention
        self.lock = threading.RLock()
        self.initialized = False
        self.maintenance = maintenance
        self.rollup_cache = {}

    @contextmanager
    def connect(self):
        with self.maintenance(), self.lock:
            trusted_path(self.path)
            if self.path.exists():
                info = self.path.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("历史数据库必须是独立的普通文件，原文件已保留")
            for suffix in ("-journal", "-wal", "-shm"):
                trusted_path(Path(str(self.path) + suffix))
            self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            self.path.parent.chmod(0o700)
            db = sqlite3.connect(self.path, timeout=3)
            try:
                self.path.chmod(0o600)
                db.row_factory = sqlite3.Row
                db.create_aggregate('latency_stats', 1, LatencyStats)
                db.execute("PRAGMA max_page_count=65536")  # 256 MiB with the default 4 KiB pages.
                if not self.initialized:
                    db.executescript("""
                        CREATE TABLE IF NOT EXISTS sources (
                            key TEXT PRIMARY KEY, name TEXT NOT NULL, stream TEXT NOT NULL,
                            cursor REAL NOT NULL DEFAULT 0, checked REAL NOT NULL DEFAULT 0,
                            limited REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '');
                        CREATE TABLE IF NOT EXISTS requests (
                            id INTEGER PRIMARY KEY, source TEXT NOT NULL, token TEXT NOT NULL UNIQUE,
                            ts REAL NOT NULL, host TEXT NOT NULL, method TEXT NOT NULL,
                            path TEXT NOT NULL, route TEXT NOT NULL, status INTEGER NOT NULL,
                            duration REAL, upstream REAL, data TEXT NOT NULL, cost INTEGER NOT NULL);
                        CREATE INDEX IF NOT EXISTS request_time ON requests(source, ts);
                        CREATE INDEX IF NOT EXISTS request_age ON requests(ts);
                        CREATE INDEX IF NOT EXISTS request_route ON requests(source,host,method,route,ts DESC);
                        CREATE INDEX IF NOT EXISTS request_path ON requests(source,host,method,path,ts DESC);
                        CREATE TABLE IF NOT EXISTS request_rollups (
                            source TEXT, minute INTEGER, host TEXT, method TEXT, route TEXT, data TEXT NOT NULL,
                            PRIMARY KEY(source,minute,host,method,route));
                        CREATE INDEX IF NOT EXISTS rollup_age ON request_rollups(minute);
                        CREATE TABLE IF NOT EXISTS request_schema (key TEXT PRIMARY KEY, value INTEGER);
                    """)
                    columns = {row[1] for row in db.execute('PRAGMA table_info(sources)')}
                    for field, definition in [('checkpoint', "TEXT NOT NULL DEFAULT '{}'"),
                                              ('rollups_since', 'REAL NOT NULL DEFAULT 0'),
                                              ('rollups_limited', 'REAL NOT NULL DEFAULT 0')]:
                        if field not in columns:
                            db.execute(f'ALTER TABLE sources ADD COLUMN {field} {definition}')
                    with db:
                        if not db.execute("SELECT 1 FROM request_schema WHERE key='rollups-backfilled'").fetchone():
                            cursor = db.execute('SELECT source,token,ts,host,method,path,route,status,duration,upstream,data,cost FROM requests')
                            while rows := cursor.fetchmany(1000):
                                self._merge_rollups(db, rows)
                            db.execute('UPDATE sources SET rollups_since=COALESCE((SELECT MIN(ts) FROM requests WHERE source=sources.key),0) WHERE rollups_since=0')
                            db.execute("INSERT INTO request_schema VALUES ('rollups-backfilled',1)")
                    self.initialized = True
                with db:
                    yield db
            finally:
                db.close()

    def source(self, key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM sources WHERE key=?", (key,)).fetchone()
            return dict(row) if row else {}

    def sources(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT key,name,checked,error FROM sources ORDER BY key")]

    def collection_status(self, key, connection, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            row = db.execute('SELECT checked,error,cursor FROM sources WHERE key=?', (key,)).fetchone()
            samples = db.execute('SELECT ts,data FROM requests WHERE source=? AND ts>=? ORDER BY ts DESC LIMIT 30',
                                 (key, connection.get('applied_at') or 0)).fetchall()
        latest = next((item['ts'] for item in samples if not connection.get('probe_id') or
                       json.loads(item['data']).get('request_id') != connection['probe_id']), None)
        error = connection.get('error') or (row['error'] if row else '')
        if error:
            state, detail = 'error', error
        elif row is None or not row['checked']:
            state, detail = 'pending', '等待后台首次采集，通常需要数秒'
        elif now - row['checked'] > 60:
            state, detail = 'stale', '后台采集超过一分钟未成功，请检查面板服务与 Docker 状态'
        elif now - row['cursor'] > 120:
            state, detail = 'catching_up', '正在补采历史日志，新请求将随后显示'
        elif latest is None:
            state, detail = 'waiting', '尚未收到真实请求；请通过原域名访问网站或调用接口，检测请求不计入验证'
        elif now - latest > 60:
            state, detail = 'idle', '采集正常，最近一分钟没有新请求；不代表业务故障'
        else:
            state, detail = 'active', '已收到真实请求并写入历史'
        return {'state': state, 'detail': detail, 'latest_at': latest, 'checked_at': row['checked'] if row else None}

    def failure(self, key, name, message):
        with self.connect() as db:
            db.execute("""INSERT INTO sources(key,name,stream,error) VALUES(?,?,'',?)
                ON CONFLICT(key) DO UPDATE SET error=excluded.error""", (key, name, message[:300]))

    def _prune(self, db, now):
        self.rollup_cache.clear()
        db.execute('DELETE FROM request_rollups WHERE minute < ?', (now - self.retention - 60,))
        count, size = db.execute('SELECT COUNT(*),COALESCE(SUM(length(data)+length(host)+length(method)+length(route)+256),0) FROM request_rollups').fetchone()
        if count > MAX_ROLLUPS or size > 24 * 1024**2:
            db.execute('UPDATE sources SET rollups_limited=?', (now,))
            doomed = []
            for row in db.execute('SELECT rowid,length(data)+length(host)+length(method)+length(route)+256 FROM request_rollups ORDER BY minute'):
                if count <= MAX_ROLLUPS and size <= 24 * 1024**2:
                    break
                doomed.append((row[0],))
                count, size = count - 1, size - row[1]
            db.executemany('DELETE FROM request_rollups WHERE rowid=?', doomed)
        db.execute("DELETE FROM requests WHERE ts < ?", (now - self.retention,))
        usage = db.execute("SELECT COUNT(*),COALESCE(SUM(cost),0) FROM requests").fetchone()
        count, size = usage
        if count <= self.max_rows and size <= self.max_bytes:
            return
        # Delete only panel-owned historical rows; never touch Docker or business files.
        doomed = []
        for row in db.execute("SELECT id,cost FROM requests ORDER BY ts,id"):
            if count <= self.max_rows and size <= self.max_bytes:
                break
            doomed.append((row[0],))
            count -= 1
            size -= row[1]
        db.executemany("DELETE FROM requests WHERE id=?", doomed)

    def ingest(self, key, name, stream, lines, until, *, limited=False, now=None, error="", identities=None, checkpoint=None):
        now = time.time() if now is None else now
        occurrences, batch = {}, []
        for index, line in enumerate(lines):
            stamp, sep, payload = line.strip().partition(" ")
            if not sep:
                continue
            item = nginx_requests.parse(payload)
            if not item:
                continue
            try:
                ts = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
            except (ValueError, OverflowError):
                continue
            if not now - self.retention <= ts <= until + 1:
                continue
            signature = hashlib.sha256((stamp + " " + payload).encode()).hexdigest()
            ordinal = occurrences.get(signature, 0)
            occurrences[signature] = ordinal + 1
            identity = identities[index] if identities is not None else f'{signature}:{ordinal}'
            token = hashlib.sha256(f"{key}:{stream}:{identity}".encode()).hexdigest()
            data = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            batch.append((key, token, ts, item["host"], item["method"], item["path"], normalize_path(item["path"]),
                          item["status"], item["duration_ms"], item["upstream_ms"], data, len(data.encode()) * 6 + 1024))
        with self.connect() as db:
            self._prune(db, now)
            inserted_rows = []
            for row in batch:
                inserted = db.execute("""INSERT OR IGNORE INTO requests
                    (source,token,ts,host,method,path,route,status,duration,upstream,data,cost)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", row).rowcount
                if inserted:
                    inserted_rows.append(row)
            self._merge_rollups(db, inserted_rows)
            self._prune(db, now)
            # The cursor and rows commit together: failures never acknowledge unpersisted logs.
            db.execute("""INSERT INTO sources(key,name,stream,cursor,checked,limited,error) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET name=excluded.name,stream=excluded.stream,
                cursor=excluded.cursor,checked=excluded.checked,error=excluded.error,
                limited=MAX(sources.limited,excluded.limited)""", (key, name, stream, until, now, now if limited else 0, error[:300]))
            db.execute('UPDATE sources SET checkpoint=?,rollups_since=CASE WHEN rollups_since=0 THEN ? ELSE rollups_since END WHERE key=?',
                       (json.dumps(checkpoint or {}), min([row[2] for row in batch], default=now), key))

    @staticmethod
    def _merge_rollups(db, rows):
        buckets = {}
        for row in rows:
            bucket = (row[0], int(row[2] // 60) * 60, row[3], row[4], row[6])
            buckets.setdefault(bucket, []).append(row)
        for bucket, rows in buckets.items():
            previous = db.execute('SELECT data FROM request_rollups WHERE source=? AND minute=? AND host=? AND method=? AND route=?', bucket).fetchone()
            data = json.loads(previous[0]) if previous else {'count': 0, 'successes': 0, 'errors': 0, 'server_errors': 0,
                'timeouts': 0, 'durationSum': 0, 'durationCount': 0, 'upstreamSum': 0, 'upstreamCount': 0, 'histogram': {}}
            for row in rows:
                code, duration, upstream = row[7:10]
                data['count'] += 1
                data['successes'] += 200 <= code < 400
                data['errors'] += code >= 400
                data['server_errors'] += code >= 500
                data['timeouts'] += code in {504, 524}
                if duration is not None:
                    data['durationSum'] += duration
                    data['durationCount'] += 1
                    bound = next((value for value in LATENCY_BOUNDS if value >= duration), duration)
                    name_bin = str(bound)
                    data['histogram'][name_bin] = data['histogram'].get(name_bin, 0) + 1
                if upstream is not None:
                    data['upstreamSum'] += upstream
                    data['upstreamCount'] += 1
            db.execute('INSERT OR REPLACE INTO request_rollups VALUES (?,?,?,?,?,?)', (*bucket, json.dumps(data, separators=(',', ':'))))

    def rollups(self, key, since, until, *, db=None):
        if db is None:
            with self.connect() as connection:
                return self.rollups(key, since, until, db=connection)
        cache_key = (key, int(since // 60), int(until // 60))
        if cache_key in self.rollup_cache:
            return json.loads(self.rollup_cache[cache_key])
        groups = {}
        for row in db.execute('SELECT host,method,route,data FROM request_rollups WHERE source=? AND minute>=? AND minute<=?',
                              (key, int(since // 60) * 60, until)):
            group_key = (row['host'], row['method'], row['route'])
            data = json.loads(row['data'])
            group = groups.setdefault(group_key, {'host': row['host'], 'method': row['method'], 'path': row['route'], 'histogram': {}})
            for field, value in data.items():
                if field == 'histogram':
                    for bound, count in value.items():
                        group[field][bound] = group[field].get(bound, 0) + count
                else:
                    group[field] = group.get(field, 0) + value
        for group in groups.values():
            group['p95'] = self.percentile(group['histogram'], .95)
            group['p99'] = self.percentile(group['histogram'], .99)
        result = list(groups.values())
        encoded = json.dumps(result, separators=(',', ':'))
        # Bound cached strings as well as entry count on high-cardinality sites.
        if len(encoded) <= 512 * 1024:
            while self.rollup_cache and (len(self.rollup_cache) >= 8 or
                    sum(map(len, self.rollup_cache.values())) + len(encoded) > 1024 * 1024):
                self.rollup_cache.pop(next(iter(self.rollup_cache)))
            self.rollup_cache[cache_key] = encoded
        return result

    @staticmethod
    def percentile(histogram, percentile):
        rank = math.ceil(sum(histogram.values()) * percentile)
        for bound, count in sorted(histogram.items(), key=lambda item: float(item[0])):
            rank -= count
            if rank <= 0:
                return float(bound)
        return None

    def evaluate(self, monitor, now=None):
        """Evaluate completed five-minute windows without rereading raw details."""
        now = time.time() if now is None else now
        until = int(now // 60) * 60 - 1
        with self.connect() as db:
            sources = [dict(row) for row in db.execute('SELECT * FROM sources')]
            windows = [(source, self.rollups(source['key'], until - 299, until, db=db)) for source in sources]
        with monitor.lock:
            seen = set()
            for source, groups in windows:
                fresh = (not source['error'] and now - source['checked'] <= 60 and source['cursor'] >= until
                         and source['rollups_since'] <= until - 299 and source['rollups_limited'] < until - 299)
                for group in groups:
                    identity = hashlib.sha256(json.dumps([source['key'], group['host'], group['method'], group['path']]).encode()).hexdigest()[:24]
                    checks = [('errors', group['server_errors'] / group['count'] >= .05, '5xx 错误率偏高', 'critical'),
                              ('timeouts', group['timeouts'] >= 5 and group['timeouts'] / group['count'] >= .02, '上游超时偏多', 'critical'),
                              ('latency', group['p95'] is not None and group['p95'] >= monitor.settings['slow_ms'], 'P95 响应缓慢', 'warning')]
                    for reason, bad, title, level in checks:
                        key = f'requests:{identity}:{reason}'
                        seen.add(key)
                        usable = fresh and group['count'] >= 20
                        monitor.observe(key, bad if usable else None, f'{title}：{group["path"]}',
                            f'{source["name"]} · {group["method"]} {group["host"]}{group["path"]} · 近 5 分钟 {group["count"]} 次；'
                            f'5xx {group["server_errors"]} 次，超时 {group["timeouts"]} 次；P95 约 {group["p95"] or 0:.2f} ms',
                            'website', level, metadata={'reason': reason, 'gateway': source['key'], 'request_count': group['count']})
            for key in list(monitor.incidents):
                if key.startswith('requests:') and key not in seen:
                    monitor.observe(key, None, '', '', 'website')

    def records(self, key, *, since, until, status='all', search='', limit=50, cursor=None):
        """Read actual request rows in a fixed window, with stable keyset pagination."""
        if (not isinstance(key, str) or not request_gateway.KEY.fullmatch(key)
                or any(type(t) not in (int, float) or not math.isfinite(t) for t in (since, until))
                or not 0 <= since <= until or until - since > 86400
                or status not in {'all', '4xx', '5xx'} or type(limit) is not int or not 1 <= limit <= 100
                or not isinstance(search, str) or len(search) > 200):
            raise ValueError('请求明细筛选参数无效')
        where, parameters = ['source=?', 'ts>=?', 'ts<=?'], [key, since, until]
        if status != 'all':
            where.append('status>=? AND status<?')
            low = 400 if status == '4xx' else 500
            parameters.extend([low, low + 100])
        if search:
            where.append("INSTR(LOWER(host || ' ' || path || ' ' || COALESCE(json_extract(data,'$.request_id'),'')),?)>0")
            parameters.append(search.lower())
        base = ' AND '.join(where)
        continuation, extra = '', []
        if cursor is not None:
            if (not isinstance(cursor, dict) or set(cursor) != {'ts', 'id'}
                    or type(cursor['id']) is not int or not 0 < cursor['id'] < 2**63
                    or type(cursor['ts']) not in (int, float) or not math.isfinite(cursor['ts'])
                    or not since <= cursor['ts'] <= until):
                raise ValueError('请求明细续读位置无效')
            continuation = ' AND (ts<? OR (ts=? AND id<?))'
            extra = [cursor['ts'], cursor['ts'], cursor['id']]
        with self.connect() as db:
            source = db.execute('SELECT key,name,checked,error,limited,cursor FROM sources WHERE key=?', (key,)).fetchone()
            if not source:
                raise ValueError('请求来源不存在，请先列出已采集来源')
            count = db.execute(f'SELECT COUNT(*) FROM requests WHERE {base}', parameters).fetchone()[0]
            rows = db.execute(f'SELECT id,ts,status,data FROM requests WHERE {base}{continuation} '
                              'ORDER BY ts DESC,id DESC LIMIT ?', [*parameters, *extra, limit + 1]).fetchall()
            retained = db.execute('SELECT MIN(ts),MAX(ts) FROM requests WHERE source=?', (key,)).fetchone()
        selected = rows[:limit]
        return {'source': dict(source), 'since': since, 'until': until, 'status': status, 'matching_records': count,
                'items': [{**json.loads(row['data']), 'record_id': row['id'], 'ts': row['ts'], 'status': row['status']}
                          for row in selected],
                'next_cursor': {'ts': selected[-1]['ts'], 'id': selected[-1]['id']} if len(rows) > limit else None,
                'earliest_retained_at': retained[0], 'latest_retained_at': retained[1],
                'notice': '仅查询仍保留的单条请求，不用聚合统计冒充明细。已过期或未采集的请求无法还原；代理错误原因需对照同时间日志。'}

    def query(self, key, *, period="24h", layout="tree", search="", status="all", slow_ms=1000, page=0, now=None):
        if not request_gateway.KEY.fullmatch(key) or period not in PERIODS or layout not in {"tree", "flat"}:
            raise ValueError("请求历史查询参数无效")
        if (status not in {"all", "success", "error", "slow"} or not 0 <= page <= 1000 or len(search) > 2048
                or isinstance(slow_ms, bool) or not isinstance(slow_ms, int) or not 1 <= slow_ms <= 3_600_000):
            raise ValueError("请求历史筛选参数无效")
        now = time.time() if now is None else now
        since = now - min(PERIODS[period], self.retention)
        column = "route" if layout == "tree" else "path"
        # Search selects whole groups, retaining their true counts and error rates.
        having = ["1"]
        search_params = ()
        search_expression = """INSTR(LOWER(host || ' ' || path || ' ' || route || ' ' ||
            COALESCE(json_extract(data,'$.request_id'),'') || ' ' ||
            COALESCE(json_extract(data,'$.client_ip'),'')), ?) > 0"""
        if search:
            having = [f"MAX({search_expression})"]
            search_params = (search.lower(),)
        if status == "error":
            having.append("SUM(status >= 400) > 0")
        if status == "success":
            having.append("SUM(status >= 200 AND status < 400) = COUNT(*)")
        if status == "slow":
            having.append("SUM(duration >= ?) > 0")
        grouped = f"""SELECT host,method,{column} AS path,COUNT(*) AS count,
            SUM(status>=200 AND status<400) AS successes,SUM(status>=400) AS errors,
            COALESCE(SUM(duration),0) AS durationSum,COUNT(duration) AS durationCount,
            COALESCE(SUM(upstream),0) AS upstreamSum,COUNT(upstream) AS upstreamCount,
            COUNT(DISTINCT path) AS pathCount,SUM(duration >= ?) AS slowCount,latency_stats(duration) AS latency
            FROM requests WHERE source=? AND ts>=? AND ts<=? GROUP BY host,method,{column}
            HAVING {' AND '.join(having)}"""
        params = (slow_ms, key, since, now, *search_params, *((slow_ms,) if status == "slow" else ()))
        with self.connect() as db:
            groups = [dict(row) for row in db.execute(f"""WITH grouped AS ({grouped})
                SELECT *,COUNT(*) OVER() AS total_groups,SUM(count) OVER() AS total_requests FROM grouped
                ORDER BY host,path,method LIMIT 100 OFFSET ?""", (*params, page * 100))]
            total = (groups[0]["total_groups"], groups[0]["total_requests"]) if groups else db.execute(
                f"SELECT COUNT(*),COALESCE(SUM(count),0) FROM ({grouped})", params).fetchone()
            aggregate_mode = False
            if layout == 'tree':
                rollups = self.rollups(key, since, now, db=db)
                raw_count = db.execute('SELECT COUNT(*) FROM requests WHERE source=? AND ts>=? AND ts<=?', (key, since, now)).fetchone()[0]
                aggregate_mode = sum(group['count'] for group in rollups) > raw_count
                if aggregate_mode:
                    matched = set()
                    if search:
                        matched = {tuple(row) for row in db.execute(f'SELECT DISTINCT host,method,route FROM requests WHERE source=? AND ts>=? AND ts<=? AND {search_expression}',
                                                                  (key, since, now, search.lower()))}
                    selected = []
                    for group in rollups:
                        group['slowCount'] = sum(count for bound, count in group['histogram'].items() if float(bound) >= slow_ms)
                        if search and search.lower() not in ' '.join((group['host'], group['method'], group['path'])).lower() and (group['host'], group['method'], group['path']) not in matched:
                            continue
                        if ((status == 'error' and not group['errors']) or
                            (status == 'success' and group['successes'] != group['count']) or
                            (status == 'slow' and not group['slowCount'])):
                            continue
                        group.update(pathCount=None, approximate=True)
                        selected.append(group)
                    selected.sort(key=lambda group: (group['host'], group['path'], group['method']))
                    total = (len(selected), sum(group['count'] for group in selected))
                    groups = selected[page * 100:(page + 1) * 100]
            # At most 100 groups per page and 30 individual examples per group.
            for group in groups:
                if not aggregate_mode:
                    group.update(json.loads(group.pop('latency') or '{"p95":null,"p99":null}'))
                group.pop('histogram', None)
                samples = db.execute(f"""SELECT id,data FROM requests INDEXED BY request_{column}
                    WHERE source=? AND ts>=? AND ts<=?
                    AND host=? AND method=? AND {column}=?
                    {'AND duration>=?' if status == 'slow' else ''}
                    {f'AND {search_expression}' if search else ''} ORDER BY ts DESC,id DESC LIMIT 30""",
                    (key, since, now, group["host"], group["method"], group["path"],
                     *((slow_ms,) if status == "slow" else ()), *search_params))
                group["items"] = [{**json.loads(row["data"]), "record_id": row["id"]} for row in samples]
                group["key"] = json.dumps([group["host"], group["method"], group["path"]], ensure_ascii=False)
                group["average"] = group["durationSum"] / group["durationCount"] if group["durationCount"] else None
                group["upstreamAverage"] = group["upstreamSum"] / group["upstreamCount"] if group["upstreamCount"] else None
                group["successRate"] = group["successes"] / group["count"] * 100
            source = db.execute("SELECT * FROM sources WHERE key=?", (key,)).fetchone()
            retained = db.execute("SELECT MIN(ts),MAX(ts),COUNT(*) FROM requests WHERE source=?", (key,)).fetchone()
        state = dict(source) if source else {}
        return {"mode": "gateway", "history": True, "groups": groups, "total_groups": total[0],
                "total_requests": total[1], "page": page, "has_more": (page + 1) * 100 < total[0],
                "checked_at": state.get("checked", 0), "earliest_at": retained[0],
                "latest_at": retained[1], "retained_requests": retained[2],
                "limited_at": state.get("limited", 0), "error": state.get("error", ""),
                "retention_days": self.retention // 86400, "slow_ms": slow_ms, "detail_filter": status,
                "aggregate_mode": aggregate_mode, "rollups_since": state.get('rollups_since', 0),
                "rollups_limited_at": state.get('rollups_limited', 0),
                "pending": bool(json.loads(state.get('checkpoint') or '{}')),
                "notice": ("统计独立于明细保留；时间边界按分钟、P95/P99及慢请求为直方图估计（桶宽约 10%）。IP/请求 ID 搜索只匹配尚存明细。"
                           if aggregate_mode else "历史仅包含已采集且仍保留的请求；每类展开显示最近最多 30 条明细。")}


class Collector:
    def __init__(self, history: History):
        self.history = history
        self.position = 0
        self.maintained = {}

    def collect(self, store, entry, now=None):
        from gateway_connections import Connections

        now = time.time() if now is None else now
        key, name = entry["spec"]["key"], entry["spec"]["name"]
        with self.history.maintenance(), request_gateway.LOCK:
            entry = store.read(key)
            name = entry["spec"]["name"]
            item = store.inspect(entry)
            if item:
                store.upgrade_logging(entry, item)
        if not item:
            self.history.failure(key, name, "采集容器不存在，已保存的历史仍可查询")
            return
        connections = Connections(self.history.home)
        connection = connections.status(key, verify=True)
        if now - self.maintained.get(key, 0) >= 60:
            self.maintained[key] = now
            try:
                with self.history.maintenance(), request_gateway.LOCK:
                    connection = connections.maintain(key, connection)
            except (OSError, ValueError) as exc:
                connection['error'] = f'自动核对未完成：{exc}；原配置和请求历史保留'
        stream = item["Id"]
        previous = self.history.source(key)
        until = int(now) - 1
        same_stream = previous.get('stream') == stream
        since = max(now - self.history.retention, previous.get("cursor", 0)) if same_stream else now - self.history.retention
        if previous.get("stream") == stream and previous.get("cursor", 0) >= until:
            return
        checkpoint = json.loads(previous.get('checkpoint') or '{}') if same_stream else {}
        lines, identities, resume = read_log_batch(stream, since, until, checkpoint)
        cursor = resume['second'] if resume else until
        self.history.ingest(key, name, stream, lines, cursor, now=now, error=connection.get('error', ''),
                            identities=identities, checkpoint=resume)

    def cycle(self):
        store = request_gateway.Store(self.history.home)
        entries = store.entries()
        deadline = time.monotonic() + 20
        for _ in range(min(len(entries), 40)):
            if time.monotonic() >= deadline:
                break
            entry = entries[self.position % len(entries)]
            self.position += 1
            try:
                self.collect(store, entry)
            except (OSError, ValueError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
                self.history.failure(entry["spec"]["key"], entry["spec"]["name"], f"采集暂不可用：{exc}")
        with self.history.connect() as db:
            self.history._prune(db, time.time())

    def run(self, stop, log):
        while not stop.is_set():
            try:
                self.cycle()
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                log(f"request history collection failed: {type(exc).__name__}: {exc}")
            stop.wait(5)
