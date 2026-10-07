"""Persistent history for panel-owned request gateways, independent of UI polling."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import stat
import subprocess
import tempfile
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
PERIODS = {"1h": 3600, "24h": 86400, "7d": RETENTION}


def normalize_path(path):
    def part(value):
        if re.fullmatch(r"[0-9]+", value):
            return ":number"
        if re.fullmatch(r"(?:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{24}|[0-9a-f]{32})", value, re.I):
            return ":id"
        return value
    return "/".join(part(value) for value in path.split("/"))


class History:
    def __init__(self, home: Path, *, max_rows=MAX_ROWS, max_bytes=MAX_BYTES, retention=RETENTION, maintenance=nullcontext):
        self.home = home
        self.path = home / "request-history" / "history.sqlite3"
        self.max_rows, self.max_bytes, self.retention = max_rows, max_bytes, retention
        self.lock = threading.RLock()
        self.initialized = False
        self.maintenance = maintenance

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
                    """)
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

    def failure(self, key, name, message):
        with self.connect() as db:
            db.execute("""INSERT INTO sources(key,name,stream,error) VALUES(?,?,'',?)
                ON CONFLICT(key) DO UPDATE SET error=excluded.error""", (key, name, message[:300]))

    def _prune(self, db, now):
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

    def ingest(self, key, name, stream, lines, until, *, limited=False, now=None):
        now = time.time() if now is None else now
        occurrences, batch = {}, []
        for line in lines:
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
            token = hashlib.sha256(f"{key}:{stream}:{signature}:{ordinal}".encode()).hexdigest()
            data = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            batch.append((key, token, ts, item["host"], item["method"], item["path"], normalize_path(item["path"]),
                          item["status"], item["duration_ms"], item["upstream_ms"], data, len(data.encode()) * 6 + 1024))
        with self.connect() as db:
            self._prune(db, now)
            db.executemany("""INSERT OR IGNORE INTO requests
                (source,token,ts,host,method,path,route,status,duration,upstream,data,cost)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", batch)
            self._prune(db, now)
            # The cursor and rows commit together: failures never acknowledge unpersisted logs.
            db.execute("""INSERT INTO sources(key,name,stream,cursor,checked,limited,error) VALUES(?,?,?,?,?,?,'')
                ON CONFLICT(key) DO UPDATE SET name=excluded.name,stream=excluded.stream,
                cursor=excluded.cursor,checked=excluded.checked,error='',
                limited=MAX(sources.limited,excluded.limited)""", (key, name, stream, until, now, now if limited else 0))

    def query(self, key, *, period="24h", layout="tree", search="", status="all", page=0, now=None):
        if not request_gateway.KEY.fullmatch(key) or period not in PERIODS or layout not in {"tree", "flat"}:
            raise ValueError("请求历史查询参数无效")
        if status not in {"all", "success", "error"} or not 0 <= page <= 1000 or len(search) > 2048:
            raise ValueError("请求历史筛选参数无效")
        now = time.time() if now is None else now
        since = now - min(PERIODS[period], self.retention)
        column = "route" if layout == "tree" else "path"
        # Search selects whole groups, retaining their true counts and error rates.
        having = ["MAX(INSTR(LOWER(host || ' ' || path || ' ' || route), ?) > 0)"]
        if status == "error":
            having.append("SUM(status >= 400) > 0")
        if status == "success":
            having.append("SUM(status >= 200 AND status < 400) = COUNT(*)")
        grouped = f"""SELECT host,method,{column} AS path,COUNT(*) AS count,
            SUM(status>=200 AND status<400) AS successes,SUM(status>=400) AS errors,
            COALESCE(SUM(duration),0) AS durationSum,COUNT(duration) AS durationCount,
            COALESCE(SUM(upstream),0) AS upstreamSum,COUNT(upstream) AS upstreamCount,
            COUNT(DISTINCT path) AS pathCount
            FROM requests WHERE source=? AND ts>=? AND ts<=? GROUP BY host,method,{column}
            HAVING {' AND '.join(having)}"""
        params = (key, since, now, search.lower())
        with self.connect() as db:
            groups = [dict(row) for row in db.execute(f"""WITH grouped AS ({grouped})
                SELECT *,COUNT(*) OVER() AS total_groups,SUM(count) OVER() AS total_requests FROM grouped
                ORDER BY host,path,method LIMIT 100 OFFSET ?""", (*params, page * 100))]
            total = (groups[0]["total_groups"], groups[0]["total_requests"]) if groups else db.execute(
                f"SELECT COUNT(*),COALESCE(SUM(count),0) FROM ({grouped})", params).fetchone()
            # At most 100 groups per page and 30 individual examples per group.
            for group in groups:
                samples = db.execute(f"""SELECT data FROM requests INDEXED BY request_{column}
                    WHERE source=? AND ts>=? AND ts<=?
                    AND host=? AND method=? AND {column}=? ORDER BY ts DESC,id DESC LIMIT 30""",
                    (key, since, now, group["host"], group["method"], group["path"]))
                group["items"] = [json.loads(row[0]) for row in samples]
                group["key"] = json.dumps([group["host"], group["method"], group["path"]], ensure_ascii=False)
                group["average"] = group["durationSum"] / group["durationCount"] if group["durationCount"] else None
                group["upstreamAverage"] = group["upstreamSum"] / group["upstreamCount"] if group["upstreamCount"] else None
                group["successRate"] = group["successes"] / group["count"] * 100
            source = db.execute("SELECT * FROM sources WHERE key=?", (key,)).fetchone()
            earliest = db.execute("SELECT MIN(ts) FROM requests WHERE source=?", (key,)).fetchone()[0]
        state = dict(source) if source else {}
        return {"mode": "gateway", "history": True, "groups": groups, "total_groups": total[0],
                "total_requests": total[1], "page": page, "has_more": (page + 1) * 100 < total[0],
                "checked_at": state.get("checked", 0), "earliest_at": earliest,
                "limited_at": state.get("limited", 0), "error": state.get("error", ""),
                "retention_days": self.retention // 86400,
                "notice": "历史仅包含已采集且仍保留的请求；每类展开显示最近最多 30 条明细。"}


class Collector:
    def __init__(self, history: History):
        self.history = history
        self.position = 0

    def collect(self, store, entry, now=None):
        now = time.time() if now is None else now
        key, name = entry["spec"]["key"], entry["spec"]["name"]
        item = store.inspect(entry)
        if not item:
            self.history.failure(key, name, "采集容器不存在，已保存的历史仍可查询")
            return
        stream = item["Id"]
        previous = self.history.source(key)
        until = int(now) - 1
        since = max(now - self.history.retention, previous.get("cursor", 0) - 1) if previous.get("stream") == stream else now - self.history.retention
        if previous.get("stream") == stream and previous.get("cursor", 0) >= until:
            return
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(["docker", "logs", "--timestamps", "--since", str(int(since)),
                "--until", str(until), "--tail", str(MAX_LINES), stream], stdout=output, stderr=subprocess.STDOUT,
                timeout=8, check=False)
            if result.returncode:
                raise ValueError("Docker 日志读取失败，稍后重试；已有历史保留")
            size = output.tell()
            limit = 8 * 1024 * 1024
            output.seek(max(0, size - limit))
            lines = output.read(limit).decode("utf-8", errors="replace").splitlines()
        if size > limit:
            lines = lines[1:]
        self.history.ingest(key, name, stream, lines, until, limited=len(lines) >= MAX_LINES or size > limit, now=now)

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
