"""Read-only host discovery and durable, bounded Fail2ban event collection."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time

from certificates import trusted_path
from server_operations import run as bounded_run, safe_file, timestamp

LIMIT = 256 * 1024
RETENTION = 30 * 86400
EVENT = re.compile(r'\[([A-Za-z0-9][A-Za-z0-9_.-]{0,127})\]\s+(Restore Ban|Ban|Unban|Found)\s+([0-9a-fA-F:.]+)(?:\s|$)')
SENSITIVE = {2375: 'Docker API', 2376: 'Docker API（需核对 TLS）', 3306: 'MySQL', 5432: 'PostgreSQL',
             6379: 'Redis', 11211: 'Memcached', 27017: 'MongoDB', 9200: 'Elasticsearch', 9300: 'Elasticsearch 集群'}


def command(args, *, timeout=5):
    code, output, limited = bounded_run(args, timeout=timeout)
    if limited or code:
        raise RuntimeError(f'{args[0]} 读取失败或超过上限')
    return output


def endpoint(value):
    host, separator, port = value.rpartition(':')
    if not separator or not port.isdecimal() or not 1 <= int(port) <= 65535:
        return None
    host = host.strip('[]')
    if host == '*':
        return host, int(port), 'wildcard'
    try:
        ip = ipaddress.ip_address(host.split('%')[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return host, int(port), 'loopback' if ip.is_loopback else 'unspecified' if ip.is_unspecified else 'interface'


def listeners(output):
    rows = []
    lines = output.splitlines()
    if len(lines) > 4096:
        raise ValueError('主机监听端口超过本轮读取上限')
    for line in lines:
        fields = line.split()
        # ss -H -lntp: State Recv-Q Send-Q Local Peer Process.
        if len(fields) < 5 or fields[0] != 'LISTEN':
            continue
        value = endpoint(fields[3])
        if not value:
            continue
        host, port, scope = value
        owners = sorted(set(re.findall(r'\("([^"\n]{1,100})",pid=', line)))
        rows.append({'host': host, 'port': port, 'protocol': 'tcp', 'scope': scope, 'owners': owners, 'kind': 'host'})
    return rows


def docker_ports(output):
    rows = []
    lines = output.splitlines()
    if len(lines) > 500:
        raise ValueError('Docker 容器超过本轮读取上限')
    for line in lines:
        item = json.loads(line)
        if not isinstance(item, dict):
            raise ValueError('Docker 端口返回格式异常')
        for published in str(item.get('Ports', '')).split(','):
            if '->' not in published:
                continue
            match = re.fullmatch(r'\s*(\[[0-9a-fA-F:]+\]|[0-9.]+):(\d+)(?:-(\d+))?->(\d+)(?:-(\d+))?/(tcp|udp)\s*', published)
            if not match:
                raise ValueError('Docker 发布端口格式无法完整识别')
            low, high = int(match[2]), int(match[3] or match[2])
            target, target_high = int(match[4]), int(match[5] or match[4])
            if not 1 <= low <= high <= 65535 or not 1 <= target <= target_high <= 65535 or high - low != target_high - target:
                raise ValueError('Docker 发布端口范围无法完整识别')
            # Inspect only sensitive offsets; a large range must not create 65535 rows.
            offsets = {port - low for port in SENSITIVE if low <= port <= high}
            offsets.update(port - target for port in SENSITIVE if target <= port <= target_high)
            for offset in sorted(offsets):
                value = endpoint(match[1] + ':' + str(low + offset))
                if value:
                    host, port, scope = value
                    rows.append({'host': host, 'port': port, 'scope': scope, 'protocol': match[6], 'kind': 'docker',
                                 'container_port': target + offset, 'container': str(item.get('Names', ''))[:128], 'owners': []})
    return rows


class Inspector:
    def __init__(self, runner=command, which=shutil.which, log_root=Path('/var/log')):
        self.run, self.which, self.log_root = runner, which, Path(log_root)

    def scan(self):
        warnings, rows = [], []
        host_ok = False
        try:
            rows = listeners(self.run(['ss', '-H', '-lntp']))
            host_ok = True
        except (OSError, ValueError, RuntimeError):
            warnings.append('主机监听端口读取失败，不能确认 SSH 实际端口')
        ssh_ports = sorted({row['port'] for row in rows if any(name in {'sshd', 'sshd-session'} for name in row['owners'])})
        ssh = {'ports': ssh_ports, 'ready': False, 'backend': '', 'logpath': '', 'detail': '未确认正在监听的主机 SSH；不猜测端口'}
        if ssh_ports:
            if self.which('journalctl'):
                try:
                    output = self.run(['journalctl', '--quiet', '--no-pager', '-n', '1', '-o', 'json',
                                       '_SYSTEMD_UNIT=ssh.service', '+', '_SYSTEMD_UNIT=sshd.service', '+',
                                       '_COMM=sshd', '+', '_COMM=sshd-session'])
                    records = [json.loads(line) for line in output.splitlines() if line.strip()]
                    if records and (records[-1].get('_COMM') in {'sshd', 'sshd-session'} or
                                    records[-1].get('_SYSTEMD_UNIT') in {'ssh.service', 'sshd.service'}):
                        ssh.update(ready=True, backend='systemd', detail='已识别 SSH 监听端口与 systemd 日志')
                except (OSError, ValueError, RuntimeError):
                    pass
            if not ssh['ready']:
                for name in ('auth.log', 'secure'):
                    path = self.log_root / name
                    if safe_file(path):
                        with path.open('rb') as stream:
                            stream.seek(max(0, path.stat().st_size - LIMIT))
                            sample = stream.read(LIMIT).decode('utf-8', 'replace')
                        if re.search(r'\bsshd(?:-session)?\[\d+\]:', sample):
                            ssh.update(ready=True, backend='polling', logpath=str(path), detail=f'已识别 SSH 监听端口与 {path}')
                            break
            if not ssh['ready']:
                ssh['detail'] = '已发现 SSH 端口，但未确认日志来源；保持现有配置，可在服务器执行 journalctl -u ssh -u sshd -n 20 --no-pager 核对'
        docker_ok = not bool(self.which('docker'))
        if not docker_ok:
            try:
                rows.extend(docker_ports(self.run(['docker', 'ps', '--format', '{{json .}}'])))
                docker_ok = True
            except (OSError, ValueError, RuntimeError):
                warnings.append('Docker 发布端口读取不完整，未将它视为没有暴露端口')
        risks = []
        for row in rows:
            if row['scope'] == 'loopback':
                continue
            label = SENSITIVE.get(row.get('container_port')) if row['kind'] == 'docker' else None
            label = label or SENSITIVE.get(row['port'])
            if label:
                risks.append({**row, 'service': label, 'level': 'warning',
                              'detail': '监听于非回环地址；是否可从公网到达还取决于路由、安全组与防火墙。请核对是否需要公开。'})
        return {'checked_at': time.time(), 'ssh': ssh, 'ports': risks[:200], 'warnings': warnings,
                'host_readable': host_ok, 'docker_readable': docker_ok,
                'notice': '只读检查 TCP 监听与 Docker 发布端口，不修改网络规则；未执行公网连通探测。'}


def read_log(db, path, key):
    """Consume complete lines within one bounded read; retain the cursor across restarts."""
    info = path.stat()
    saved = db.execute('SELECT value FROM cursors WHERE key=?', (key,)).fetchone()
    cursor = json.loads(saved['value']) if saved else {}
    same = cursor.get('inode') == [info.st_dev, info.st_ino] and info.st_size >= cursor.get('offset', 0)
    offset = cursor['offset'] if same else max(0, info.st_size - LIMIT)
    discard = bool(cursor.get('discard')) if same else bool(offset)
    with path.open('rb') as stream:
        stream.seek(offset)
        content = stream.read(LIMIT)
        start = 0
        if discard:
            boundary = content.find(b'\n')
            start = boundary + 1 if boundary >= 0 else len(content)
            discard = boundary < 0
        end = content.rfind(b'\n') + 1
        if discard or (not end and len(content) == LIMIT):
            position, discard = stream.tell(), True
        else:
            position = offset + end
    lines, position_in_file = [], offset + start
    for line in content[start:end].splitlines(keepends=True):
        lines.append((f'{info.st_dev}:{info.st_ino}:{position_in_file}', line.decode('utf-8', 'replace').rstrip('\r\n')))
        position_in_file += len(line)
    db.execute('INSERT OR REPLACE INTO cursors VALUES (?,?)',
               (key, json.dumps({'inode': [info.st_dev, info.st_ino], 'offset': position, 'discard': discard})))
    return lines, {'limited': position < info.st_size or discard, 'gap': bool(saved and not same),
                   'discard': discard, 'initial_tail': not same and offset > 0}


def read_journal(db, runner, key, matches, now, limit):
    """Read oldest pending entries and commit the cursor only after parsing the batch."""
    saved = db.execute('SELECT value FROM cursors WHERE key=?', (key,)).fetchone()
    since = max(now - RETENTION, float(saved['value']) - 1 if saved else now - 86400)
    saved_cursor = db.execute('SELECT value FROM cursors WHERE key=?', (key + ':cursor',)).fetchone()
    cursor = saved_cursor['value'] if saved_cursor else ''
    base = ['journalctl', '--quiet', '--no-pager', '-n', f'+{limit}', '-o', 'json',
            '--output-fields=MESSAGE,__REALTIME_TIMESTAMP,__CURSOR']
    fallback = ['--since', '@' + str(int(since))]
    gap = False
    try:
        output = runner([*base, *(['--after-cursor', cursor] if cursor else fallback), *matches])
    except RuntimeError:
        if not cursor:
            raise
        # A vacuumed journal cursor can no longer be sought; deduplication makes
        # replay from the saved timestamp safe, including entries in that second.
        output = runner([*base, *fallback, *matches])
        gap = True
    lines = output.splitlines()
    records, latest, next_cursor = [], since, ''
    for line in lines:
        item = json.loads(line)
        if not isinstance(item, dict):
            raise ValueError('日志条目格式异常')
        at = int(item.get('__REALTIME_TIMESTAMP', 0)) / 1e6
        position = item.get('__CURSOR', '')
        if not isinstance(position, str) or len(position) > 4096 or any(ord(char) < 32 for char in position):
            raise ValueError('日志游标格式异常')
        if position:
            next_cursor = position
        if 0 < at <= now + 60:
            latest = max(latest, at)
            records.append((item, at))
    limited = len(lines) >= limit
    if limited and not next_cursor:
        raise ValueError('日志达到读取上限但未返回游标，保留原读取位置')
    if next_cursor:
        db.execute('INSERT OR REPLACE INTO cursors VALUES (?,?)', (key + ':cursor', next_cursor))
    elif gap:
        db.execute('DELETE FROM cursors WHERE key=?', (key + ':cursor',))
    db.execute('INSERT OR REPLACE INTO cursors VALUES (?,?)', (key, str(latest if lines else max(latest, now - 1))))
    return records, {'limited': limited, 'gap': gap}


def parse_event(line, at=None):
    match = EVENT.search(line)
    if not match:
        return None
    try:
        ip = ipaddress.ip_address(match[3])
    except ValueError:
        return None
    if at is None:
        prefix = re.match(r'(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3,6})', line)
        try:
            at = datetime.fromisoformat(prefix[1].replace(',', '.')).timestamp() if prefix else timestamp(line)
        except ValueError:
            return None
    if at is None or not 0 < at < time.time() + 86400:
        return None
    kind = {'Ban': 'ban', 'Restore Ban': 'restore', 'Unban': 'unban', 'Found': 'failure'}[match[2]]
    jail = match[1]
    return {'ts': at, 'jail': jail, 'kind': kind, 'ip': str(ip),
            'reason': '规则匹配到认证失败' if kind == 'failure' else 'Fail2ban 恢复原封禁' if kind == 'restore'
            else 'Fail2ban 规则封禁（自动或客户端操作）' if kind == 'ban' else 'Fail2ban 解封'}


def timed_bans(output):
    result = {}
    for line in output.splitlines():
        match = re.fullmatch(r'([0-9a-fA-F:.]+)\s+(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s+\+\s+(-?\d+)\s+=\s+(.+)', line.strip())
        if not match:
            continue
        try:
            ip = str(ipaddress.ip_address(match[1]))
            started = datetime.fromisoformat(match[2]).timestamp()
            duration = int(match[3])
            result[ip] = {'started_at': started, 'duration': duration,
                          'expires_at': None if duration < 0 else datetime.fromisoformat(match[4]).timestamp(),
                          'permanent': duration < 0}
        except ValueError:
            continue
    return result


class History:
    def __init__(self, home, runner=command, log_root=Path('/var/log'), clock=time.time):
        self.path = Path(home) / 'security/history.sqlite3'
        self.run, self.log_root, self.clock = runner, Path(log_root), clock
        self.lock = threading.RLock()

    @contextmanager
    def connection(self, readonly=False):
        with self.lock:
            trusted_path(self.path)
            if not readonly:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5) if readonly else sqlite3.connect(self.path, timeout=5)
            try:
                db.row_factory = sqlite3.Row
                if not readonly:
                    os.chmod(self.path, 0o600)
                    db.executescript('''CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, ts REAL NOT NULL, jail TEXT NOT NULL, kind TEXT NOT NULL,
                    ip TEXT NOT NULL, reason TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS events_time ON events(ts);
                    CREATE INDEX IF NOT EXISTS events_pair ON events(jail,ip,kind,ts);
                    CREATE TABLE IF NOT EXISTS cursors (key TEXT PRIMARY KEY, value TEXT NOT NULL);''')
                yield db
                db.commit()
            finally:
                db.close()

    def collect(self, logtarget=''):
        with self.connection() as db:
            now = self.clock()
            records, detail, readable, limited = [], '', False, False
            path = Path(logtarget) if logtarget.startswith('/') else self.log_root / 'fail2ban.log'
            # Discover only the standard log root, never read an arbitrary configured business path.
            if '..' not in path.parts and path.is_relative_to(self.log_root) and safe_file(path):
                lines, state = read_log(db, path, 'file:' + str(path))
                limited = state['limited'] or state['initial_tail'] or state['gap']
                for _, line in lines:
                    event = parse_event(line)
                    if event:
                        records.append(event)
                readable, detail = True, f'增量读取 {path}；仅保留结构化事件，不保存原始日志'
                if state['discard']:
                    detail += '；超长或不完整日志行分段跳过'
                if state['gap']:
                    detail += '；日志轮转或截断，可能存在采集空档'
            else:
                try:
                    entries, state = read_journal(db, self.run, 'journal', ['-u', 'fail2ban.service'], now, 1000)
                    limited = state['limited'] or state['gap']
                    for item, at in entries:
                        message = item.get('MESSAGE', '')
                        event = parse_event(message, at) if isinstance(message, str) else None
                        if event:
                            records.append(event)
                    readable, detail = True, '增量读取 Fail2ban systemd 日志；首次从最近 24 小时开始补采'
                    if state['gap']:
                        detail += '；原游标不可用，已按时间恢复读取，可能存在空档'
                except (OSError, ValueError, RuntimeError, TypeError, OverflowError):
                    detail = '未能读取 Fail2ban 文件或 systemd 日志；保留已采集历史，不代表没有攻击'
            for item in records:
                if now - RETENTION <= item['ts'] <= now + 60:
                    identity = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
                    db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)',
                               (identity, item['ts'], item['jail'], item['kind'], item['ip'], item['reason']))
            db.execute('DELETE FROM events WHERE ts < ?', (now - RETENTION,))
            db.execute('DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY ts DESC LIMIT -1 OFFSET 20000)')
            state = {'checked_at': now, 'readable': readable, 'limited': limited, 'detail': detail,
                     'notice': '保留已采集的最近 30 天、最多 20000 条事件；日志轮转、采集上限及停机可能留下空档。'}
            db.execute("INSERT OR REPLACE INTO cursors VALUES ('status',?)", (json.dumps(state),))
            db.commit()
        return state

    def snapshot(self, page=0):
        if type(page) is not int or not 0 <= page <= 400:
            raise ValueError('历史页码无效')
        if not self.path.exists():
            return {'items': [], 'total': 0, 'page': page, 'detail': '等待首次后台采集', 'readable': False}
        with self.connection(readonly=True) as db:
            cutoff = self.clock() - RETENTION
            status = db.execute("SELECT value FROM cursors WHERE key='status'").fetchone()
            result = json.loads(status['value']) if status else {}
            rows = db.execute("SELECT * FROM events WHERE kind != 'failure' AND ts >= ? ORDER BY ts DESC,id DESC LIMIT 50 OFFSET ?",
                              (cutoff, page * 50)).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                # Observed matches, not an assertion of the original jail's exact failure threshold.
                item['observed_failures_10m'] = db.execute(
                    "SELECT COUNT(*) FROM events WHERE jail=? AND ip=? AND kind='failure' AND ts BETWEEN ? AND ?",
                    (row['jail'], row['ip'], row['ts'] - 600, row['ts'])).fetchone()[0]
                items.append(item)
            total = db.execute("SELECT COUNT(*) FROM events WHERE kind != 'failure' AND ts >= ?", (cutoff,)).fetchone()[0]
            recent = db.execute("SELECT COUNT(*), COUNT(DISTINCT ip) FROM events WHERE kind='ban' AND ts BETWEEN ? AND ?",
                                (self.clock() - 600, self.clock())).fetchone()
            return {**result, 'items': items, 'total': total, 'page': page,
                    'recent_bans': recent[0], 'recent_ips': recent[1]}
