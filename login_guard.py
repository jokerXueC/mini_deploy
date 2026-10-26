"""Persistent login failure policy, panel enforcement and isolated SSH nftables set."""
from contextlib import nullcontext
import ipaddress
import json
import math
import os
import shutil
import subprocess
import threading
import time
import urllib.request
import uuid

from security_center import SCHEMA

DEFAULTS = {'enabled': True, 'maxretry': 5, 'findtime': 600, 'bantime': 3600, 'ignoreip': []}
MAX_BANTIME = 7 * 86400
REPEAT_RETENTION = 30 * 86400
TABLE = 'mini_deploy_login_guard_v1'
OWNER = 'mini_deploy login guard v1'
SCHEMA_GUARD = '''
CREATE TABLE IF NOT EXISTS login_guard_settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS login_guard_bans (
 ip TEXT NOT NULL, source TEXT NOT NULL, started REAL NOT NULL, expires REAL NOT NULL, reason TEXT NOT NULL,
 PRIMARY KEY(ip,source));
CREATE TABLE IF NOT EXISTS login_guard_repeats (
 ip TEXT NOT NULL, source TEXT NOT NULL, strikes INTEGER NOT NULL, expires REAL NOT NULL,
 PRIMARY KEY(ip,source));
CREATE INDEX IF NOT EXISTS login_guard_repeat_expiry ON login_guard_repeats(expires);
CREATE TABLE IF NOT EXISTS login_guard_geo (ip TEXT PRIMARY KEY, checked REAL NOT NULL, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS login_guard_source ON security_activity(source,kind,ts,ip);
'''


def address(value):
    if not isinstance(value, str) or len(value) > 64 or '%' in value:
        raise ValueError('请输入 IPv4 或 IPv6 地址')
    ip = ipaddress.ip_address(value)
    return str(ip.ipv4_mapped or ip) if isinstance(ip, ipaddress.IPv6Address) else str(ip)


def locate(ip):
    # Only the public IP is sent; no log, account, credential or request data leaves the host.
    request = urllib.request.Request('https://ipwho.is/' + ip + '?lang=zh-CN', headers={'User-Agent': 'mini-deploy/1'})
    with urllib.request.urlopen(request, timeout=3) as response:
        data = json.loads(response.read(32769))
    lat, lon = data.get('latitude'), data.get('longitude')
    if data.get('success') is not True or address(data.get('ip')) != ip or type(lat) not in (float, int) or type(lon) not in (float, int):
        raise ValueError('归属地暂不可用')
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError('归属地坐标无效')
    return {'latitude': lat, 'longitude': lon, 'label': ' · '.join(str(data.get(key) or '')[:80] for key in ('country', 'region', 'city') if data.get(key))}


class Firewall:
    """Never flush or adopt somebody else's table; timeouts also expire during downtime."""
    def __init__(self, runner=subprocess.run, which=shutil.which, supported=None):
        self.run, self.which = runner, which
        self.supported = os.name == 'posix' if supported is None else supported

    def apply(self, ports, bans, now):
        if not self.supported or not self.which('nft'):
            raise RuntimeError('SSH 自动封禁未生效：主机未提供 nftables；面板登录防护仍可用。')
        args = {'capture_output': True, 'text': True, 'timeout': 8, 'check': False}
        listing = self.run(['nft', '-j', 'list', 'tables'], **args)
        if listing.returncode:
            raise RuntimeError('SSH 自动封禁未生效：无法读取 nftables 权限。')
        tables = json.loads(listing.stdout).get('nftables', [])
        exists = any(item.get('table', {}).get('family') == 'inet' and item.get('table', {}).get('name') == TABLE for item in tables)
        commands = []
        if exists:
            result = self.run(['nft', '-j', 'list', 'table', 'inet', TABLE], **args)
            if result.returncode:
                raise RuntimeError('无法核验专属防火墙规则，未修改任何规则。')
            own = json.loads(result.stdout).get('nftables', [])
            if not any(item.get('table', {}).get('comment') == OWNER for item in own):
                raise RuntimeError('同名防火墙表不属于面板，已保留原规则。')
            commands.append(f'delete table inet {TABLE}')
        commands.extend([f'add table inet {TABLE} {{ comment "{OWNER}"; }}',
                         f'add set inet {TABLE} blocked4 {{ type ipv4_addr; flags timeout; }}',
                         f'add set inet {TABLE} blocked6 {{ type ipv6_addr; flags timeout; }}',
                         f'add chain inet {TABLE} input {{ type filter hook input priority -5; policy accept; }}'])
        if ports:
            portset = ', '.join(str(port) for port in ports)
            for version in (4, 6):
                commands.append(f'add rule inet {TABLE} input ct state new tcp dport {{ {portset} }} '
                                f'{"ip" if version == 4 else "ip6"} saddr @blocked{version} drop')
                elements = [f'{address(item["ip"])} timeout {max(1, math.ceil(item["expires"] - now))}s'
                            for item in bans if ipaddress.ip_address(item['ip']).version == version and item['expires'] > now]
                if elements:
                    commands.append(f'add element inet {TABLE} blocked{version} {{ ' + ', '.join(elements) + ' }')
        result = self.run(['nft', '-f', '-'], input='\n'.join(commands) + '\n', **args)
        if result.returncode:
            raise RuntimeError('SSH 自动封禁未生效：nftables 拒绝专属规则，原规则未变。')


class Guard:
    def __init__(self, history, *, clock=time.time, firewall=None, locator=locate, maintenance=nullcontext):
        self.history, self.clock = history, clock
        self.maintenance = maintenance
        self.firewall, self.locator = firewall or Firewall(), locator
        self.lock = threading.RLock()
        self.ssh_state = {'ready': False, 'detail': '正在识别 SSH 登录来源和主机防火墙', 'ports': []}
        self._signature = None
        self._applied_at = 0
        self._enforced_ssh = set()
        with self.maintenance(), history.connection() as db:
            db.executescript(SCHEMA + SCHEMA_GUARD)
            # Preserve an existing automatic ban as the first strike when upgrading.
            db.execute('INSERT OR IGNORE INTO login_guard_repeats '
                       "SELECT ip,source,1,expires FROM login_guard_bans WHERE reason<>'管理员手动封禁'")

    def _settings(self, db):
        row = db.execute('SELECT value FROM login_guard_settings WHERE id=1').fetchone()
        return json.loads(row[0]) if row else dict(DEFAULTS)

    def _ignored(self, ip, settings):
        value = ipaddress.ip_address(ip)
        return value.is_loopback or value.is_unspecified or value.is_multicast or any(value in ipaddress.ip_network(net) for net in settings['ignoreip'])

    def _event(self, db, source, kind, ip, detail, now):
        db.execute('INSERT INTO security_activity VALUES (?,?,?,?,?,?,?,?)',
                   (uuid.uuid4().hex, now, source, kind, ip, '', '面板' if source == 'panel' else 'SSH', detail[:500]))

    def _evaluate(self, db, now):
        settings = self._settings(db)
        for item in db.execute('SELECT * FROM login_guard_bans WHERE expires<=?', (now,)).fetchall():
            self._event(db, item['source'], 'unban', item['ip'], '临时封禁到期', item['expires'])
        db.execute('DELETE FROM login_guard_bans WHERE expires<=?', (now,))
        db.execute('DELETE FROM login_guard_repeats WHERE expires<=?', (now - REPEAT_RETENTION,))
        if not settings['enabled']:
            return
        # Historical backfill cannot ban a visitor: require an actual recent failure.
        candidates = db.execute("SELECT ip,source,COUNT(*) AS attempts,MAX(ts) AS latest FROM security_activity "
                                "WHERE kind='login_failure' AND source IN ('panel','ssh') AND ts BETWEEN ? AND ? "
                                "GROUP BY ip,source HAVING COUNT(*)>=? AND MAX(ts)>=? LIMIT 4096",
                                (now - settings['findtime'], now, settings['maxretry'], now - 90)).fetchall()
        for row in candidates:
            ip, source = row['ip'], row['source']
            if self._ignored(ip, settings) or db.execute('SELECT 1 FROM login_guard_bans WHERE ip=? AND source=?', (ip, source)).fetchone():
                continue
            # An unban, expiry or previous ban consumes older failures; no immediate re-ban.
            last = db.execute("SELECT MAX(ts) FROM security_activity WHERE ip=? AND source=? AND kind IN ('ban','unban')", (ip, source)).fetchone()[0]
            count = db.execute("SELECT COUNT(*) FROM security_activity WHERE ip=? AND source=? AND kind='login_failure' AND ts>? AND ts<=?",
                               (ip, source, max(last or 0, now - settings['findtime']), now)).fetchone()[0]
            if count < settings['maxretry']:
                continue
            previous = db.execute('SELECT strikes FROM login_guard_repeats WHERE ip=? AND source=?', (ip, source)).fetchone()
            strikes = previous['strikes'] + 1 if previous else 1
            duration = min(MAX_BANTIME, settings['bantime'] * (1 << min(strikes - 1, 14)))
            length = f'{duration // 3600} 小时' if duration % 3600 == 0 else f'{duration // 60} 分钟'
            reason = f'{settings["findtime"] // 60} 分钟内认证失败 {count} 次；第 {strikes} 次自动封禁，时长 {length}'
            db.execute('INSERT INTO login_guard_bans VALUES (?,?,?,?,?)', (ip, source, now, now + duration, reason))
            db.execute('INSERT OR REPLACE INTO login_guard_repeats VALUES (?,?,?,?)', (ip, source, strikes, now + duration))
            self._event(db, source, 'ban', ip, reason, now)

    def record_panel(self, ip, success):
        ip, now = address(ip), self.clock()
        with self.maintenance(), self.lock, self.history.connection() as db:
            self._event(db, 'panel', 'login_success' if success else 'login_failure', ip, '面板密码认证' + ('成功' if success else '失败'), now)
            self._evaluate(db, now)

    def blocked(self, ip):
        ip = address(ip)
        with self.history.connection(readonly=True) as db:
            settings = self._settings(db)
            if not settings['enabled'] or self._ignored(ip, settings):
                return False
            return bool(db.execute("SELECT 1 FROM login_guard_bans WHERE ip=? AND source='panel' AND expires>?", (ip, self.clock())).fetchone())

    def change(self, data, client_ip):
        action, now = data.get('action'), self.clock()
        with self.maintenance(), self.lock, self.history.connection() as db:
            applied = db.execute("SELECT value FROM cursors WHERE key='guard:firewall:applied'").fetchone()
            was_applied = bool(applied and applied[0] == '1')
            if action == 'settings':
                settings = dict(DEFAULTS)
                if type(data.get('enabled')) is not bool:
                    raise ValueError('自动封禁状态无效')
                settings['enabled'] = data['enabled']
                for key, low, high in [('maxretry', 3, 50), ('findtime', 60, 86400), ('bantime', 60, 604800)]:
                    value = data.get(key)
                    if type(value) is not int or not low <= value <= high:
                        raise ValueError('防护设置超出范围')
                    settings[key] = value
                ignores = data.get('ignoreip', [])
                if not isinstance(ignores, list) or len(ignores) > 64 or any(not isinstance(net, str) or len(net) > 64 for net in ignores):
                    raise ValueError('白名单最多 64 个 IP 或网段')
                settings['ignoreip'] = list(dict.fromkeys(str(ipaddress.ip_network(net, strict=False)) for net in [*ignores, address(client_ip)]))
                db.execute('INSERT OR REPLACE INTO login_guard_settings VALUES (1,?)', (json.dumps(settings),))
                for row in db.execute('SELECT * FROM login_guard_bans').fetchall():
                    if not settings['enabled'] or self._ignored(row['ip'], settings):
                        db.execute('DELETE FROM login_guard_bans WHERE ip=? AND source=?', (row['ip'], row['source']))
                        self._event(db, row['source'], 'unban', row['ip'], '暂停防护或加入白名单', now)
            elif action == 'ban':
                ip, source = address(data.get('ip')), data.get('source', 'all')
                if not isinstance(source, str) or source not in {'panel', 'ssh', 'all'}:
                    raise ValueError('登录来源无效')
                settings = self._settings(db)
                if not settings['enabled']:
                    raise ValueError('登录防护已暂停，请先开启防护')
                if ip == address(client_ip) or self._ignored(ip, settings):
                    raise ValueError('不能封禁当前访问 IP、白名单或保留地址')
                if source in {'ssh', 'all'} and (not self.ssh_state['ready'] or not self.ssh_state.get('ports')):
                    raise ValueError('SSH 防护尚未就绪，请刷新状态后重试')
                for scope in ('panel', 'ssh') if source == 'all' else (source,):
                    if db.execute('SELECT 1 FROM login_guard_bans WHERE ip=? AND source=? AND expires>?', (ip, scope, now)).fetchone():
                        continue
                    db.execute('INSERT OR REPLACE INTO login_guard_bans VALUES (?,?,?,?,?)',
                               (ip, scope, now, now + settings['bantime'], '管理员手动封禁'))
                    self._event(db, scope, 'ban', ip, '管理员手动封禁', now)
            elif action == 'unban':
                ip, source = address(data.get('ip')), data.get('source')
                if not isinstance(source, str) or source not in {'panel', 'ssh', 'all'}:
                    raise ValueError('登录来源无效')
                for scope in ('panel', 'ssh') if source == 'all' else (source,):
                    removed = db.execute('DELETE FROM login_guard_bans WHERE ip=? AND source=?', (ip, scope))
                    if removed.rowcount:
                        self._event(db, scope, 'unban', ip, '管理员手动解封', now)
            else:
                raise ValueError('不支持的防护操作')
            # Apply SSH removals before committing: a failed kernel operation must
            # not erase the only durable record of a still-active firewall ban.
            active = [dict(row) for row in db.execute("SELECT * FROM login_guard_bans WHERE source='ssh' AND expires>?", (now,))]
            settings = self._settings(db)
            if was_applied or self._signature is not None:
                ports = self.ssh_state.get('ports', []) if settings['enabled'] else []
                self.firewall.apply(ports, active, now)
                db.execute("INSERT OR REPLACE INTO cursors VALUES ('guard:firewall:applied',?)", ('1' if ports else '0',))
                self._enforced_ssh = {(item['ip'], item['started'], item['expires']) for item in active} if ports else set()
            self._signature = None

    def tick(self, ssh):
        now = self.clock()
        with self.maintenance(), self.lock, self.history.connection() as db:
            self._evaluate(db, now)
            settings = self._settings(db)
            ports = sorted({port for port in ssh.get('ports', []) if type(port) is int and 1 <= port <= 65535}) if settings['enabled'] else []
            bans = [dict(row) for row in db.execute("SELECT * FROM login_guard_bans WHERE source='ssh' AND expires>? LIMIT 4096", (now,))]
            signature = json.dumps([ports, bans], sort_keys=True)
            try:
                auth = db.execute("SELECT value FROM cursors WHERE key='auth:status'").fetchone()
                if settings['enabled'] and auth and (not json.loads(auth[0]).get('readable') or now - json.loads(auth[0]).get('checked_at', 0) > 180):
                    raise RuntimeError('SSH 登录日志暂不可读或已过期，无法确认新封禁是否完整。')
                if settings['enabled'] and (not ssh.get('ready') or not ports):
                    raise RuntimeError('尚未识别可用的主机 SSH 登录日志与端口；面板登录防护可用。')
                if signature != self._signature or now - self._applied_at > 60:
                    self.firewall.apply(ports, bans, now)
                    self._signature, self._applied_at = signature, now
                    self._enforced_ssh = {(item['ip'], item['started'], item['expires']) for item in bans} if ports else set()
                    db.execute("INSERT OR REPLACE INTO cursors VALUES ('guard:firewall:applied',?)", ('1' if ports else '0',))
                self.ssh_state = {'ready': bool(ports), 'ports': ports, 'detail': 'SSH 临时封禁已同步到主机防火墙' if ports else '自动封禁已暂停'}
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                self.ssh_state = {'ready': False, 'ports': ports, 'detail': str(exc)[:300]}

    def update_locations(self):
        now = self.clock()
        with self.history.connection(readonly=True) as db:
            rows = db.execute("SELECT a.ip,MAX(a.ts) AS latest FROM security_activity a LEFT JOIN login_guard_geo g ON a.ip=g.ip "
                              "WHERE a.kind IN ('login_success','login_failure') AND a.ts>? "
                              "AND (g.ip IS NULL OR g.checked<?) GROUP BY a.ip ORDER BY latest DESC LIMIT 12", (now - 86400, now - 86400)).fetchall()
        lookups = 0
        for row in rows:
            ip = address(row['ip'])
            if ipaddress.ip_address(ip).is_global:
                if lookups >= 2:
                    break
                lookups += 1
                try:
                    value = self.locator(ip)
                except (OSError, ValueError, RuntimeError, TypeError):
                    value = {'label': '归属地暂不可用'}
            else:
                value = {'label': '内网 / 保留地址'}
            with self.maintenance(), self.history.connection() as db:
                db.execute('INSERT OR REPLACE INTO login_guard_geo VALUES (?,?,?)', (ip, now, json.dumps(value)))
        with self.maintenance(), self.history.connection() as db:
            db.execute('DELETE FROM login_guard_geo WHERE checked<?', (now - 30 * 86400,))

    def status(self, page=0, source='all', outcome='all', ban_page=0):
        if type(page) is not int or type(ban_page) is not int or not 0 <= page <= 2000 or not 0 <= ban_page <= 2000 or source not in {'all', 'panel', 'ssh'} or outcome not in {'all', 'login_success', 'login_failure'}:
            raise ValueError('登录记录筛选无效')
        now = self.clock()
        with self.lock, self.history.connection(readonly=True) as db:
            settings = self._settings(db)
            where = "kind IN ('login_success','login_failure') AND ts>=?" + (' AND source=?' if source != 'all' else '') + (' AND kind=?' if outcome != 'all' else '')
            args = [now - 30 * 86400] + ([source] if source != 'all' else []) + ([outcome] if outcome != 'all' else [])
            total = db.execute('SELECT COUNT(*) FROM security_activity WHERE ' + where, args).fetchone()[0]
            items = [dict(row) for row in db.execute('SELECT * FROM security_activity WHERE ' + where + ' ORDER BY ts DESC,id DESC LIMIT 10 OFFSET ?', (*args, page * 10))]
            geo = {row['ip']: json.loads(row['value']) for row in db.execute('SELECT * FROM login_guard_geo')}
            for item in items:
                item['location'] = geo.get(item['ip'], {'label': '待定位'})
            locations = [dict(row) for row in db.execute("SELECT ip,COUNT(*) AS count,SUM(kind='login_failure') AS failures,MAX(ts) AS last_at FROM security_activity WHERE kind IN ('login_success','login_failure') AND ts BETWEEN ? AND ? GROUP BY ip ORDER BY count DESC LIMIT 500", (now - 86400, now))]
            for item in locations:
                item.update(geo.get(item['ip'], {'label': '待定位'}))
            active_bans = {}
            for row in db.execute('SELECT * FROM login_guard_bans WHERE expires>?', (now,)):
                enforced = settings['enabled'] and (row['source'] == 'panel' or self.ssh_state['ready'] and
                    (row['ip'], row['started'], row['expires']) in self._enforced_ssh)
                active_bans.setdefault(row['ip'], []).append({'source': row['source'], 'enforced': bool(enforced)})
            for item in locations:
                item['bans'] = active_bans.get(item['ip'], [])
                item['protected'] = self._ignored(item['ip'], settings)
            counts = dict(db.execute('SELECT kind,COUNT(*) FROM security_activity WHERE ts BETWEEN ? AND ? GROUP BY kind', (now - 86400, now)))
            location_total = db.execute("SELECT COUNT(DISTINCT ip) FROM security_activity WHERE kind IN ('login_success','login_failure') AND ts BETWEEN ? AND ?", (now - 86400, now)).fetchone()[0]
            # Page by IP so its panel and SSH rules never land on different pages.
            bans = [dict(row) for row in db.execute(
                'WITH selected AS (SELECT ip,MAX(started) AS latest FROM login_guard_bans WHERE expires>? '
                'GROUP BY ip ORDER BY latest DESC,ip LIMIT 10 OFFSET ?) '
                'SELECT b.* FROM login_guard_bans b JOIN selected s ON b.ip=s.ip WHERE b.expires>? '
                'ORDER BY s.latest DESC,b.ip,b.source', (now, ban_page * 10, now))]
            for item in bans:
                item['enforced'] = settings['enabled'] and (item['source'] == 'panel' or
                    self.ssh_state['ready'] and (item['ip'], item['started'], item['expires']) in self._enforced_ssh)
            auth = db.execute("SELECT value FROM cursors WHERE key='auth:status'").fetchone()
            return {'settings': settings, 'ssh': dict(self.ssh_state), 'auth': json.loads(auth[0]) if auth else {'readable': False, 'detail': '等待 SSH 登录日志'},
                    'items': items, 'total': total, 'page': page, 'page_size': 10, 'bans': bans, 'ban_page': ban_page,
                    'ban_total': db.execute('SELECT COUNT(DISTINCT ip) FROM login_guard_bans WHERE expires>?', (now,)).fetchone()[0],
                    'locations': locations, 'location_total': location_total, 'success_24h': counts.get('login_success', 0), 'failure_24h': counts.get('login_failure', 0),
                    'bans_24h': counts.get('ban', 0), 'checked_at': now}
