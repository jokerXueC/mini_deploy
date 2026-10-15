"""Read-only host checks, bounded SSH activity and configuration change baselines."""
from datetime import datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import time

from security_observation import LIMIT, RETENTION, read_log
from server_operations import safe_file, timestamp

FILES = ('ssh/sshd_config', 'crontab', 'docker/daemon.json', 'fail2ban/jail.local',
         'nginx/nginx.conf', 'caddy/Caddyfile')
DOCKER_FORMAT = ('{"id":{{json .Id}},"name":{{json .Name}},"privileged":{{json .HostConfig.Privileged}},'
                 '"network":{{json .HostConfig.NetworkMode}},"pid":{{json .HostConfig.PidMode}},'
                 '"capabilities":{{json .HostConfig.CapAdd}},"mounts":{{json .Mounts}}}')
SSH_MATCH = ['_SYSTEMD_UNIT=ssh.service', '+', '_SYSTEMD_UNIT=sshd.service', '+', '_COMM=sshd', '+', '_COMM=sshd-session']
ACTIVITY_PAGE_SIZE = 10
SCHEMA = '''CREATE TABLE IF NOT EXISTS security_activity (
 id TEXT PRIMARY KEY, ts REAL NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL,
 ip TEXT NOT NULL, account TEXT NOT NULL, target TEXT NOT NULL, detail TEXT NOT NULL);
 CREATE INDEX IF NOT EXISTS security_activity_time ON security_activity(ts);
 CREATE TABLE IF NOT EXISTS security_baselines (path TEXT PRIMARY KEY, digest TEXT NOT NULL, attributes TEXT NOT NULL);
'''


def login_event(message, at=None, now=None):
    now = time.time() if now is None else now
    if not isinstance(message, str) or len(message) > 16384 or '\n' in message.strip():
        return None
    match = re.search(r'(?:^|sshd(?:-session)?\[\d+\]:\s+)(Accepted|Failed) '
                      r'(password|publickey|keyboard-interactive(?:/pam)?|hostbased|gssapi-with-mic|none) '
                      r'for (?:invalid user )?([^\s\x00-\x1f\x7f]{1,64}) from ([0-9a-fA-F:.]+) port \d+(?:\s|$)', message)
    if not match:
        return None
    try:
        ip = str(ipaddress.ip_address(match[4]))
        if at is None:
            at = timestamp(message)
        if at is None:
            date = re.match(r'([A-Z][a-z]{2})\s+(\d{1,2}) (\d\d:\d\d:\d\d) ', message)
            months = dict(zip(('Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'), range(1, 13)))
            if not date or date[1] not in months:
                return None
            current = datetime.fromtimestamp(now)
            parsed = datetime.fromisoformat(f'{current.year}-{months[date[1]]:02}-{int(date[2]):02}T{date[3]}')
            if parsed.timestamp() > now + 86400:
                parsed = parsed.replace(year=current.year - 1)
            at = parsed.timestamp()
        if not now - RETENTION <= at <= now + 60:
            return None
    except (ValueError, OverflowError, TypeError):
        return None
    return {'ts': at, 'source': 'ssh', 'kind': 'login_success' if match[1] == 'Accepted' else 'login_failure',
            'ip': ip, 'account': match[3], 'target': 'SSH', 'detail': f'SSH {match[2]} 认证' + ('成功' if match[1] == 'Accepted' else '失败')}


def finding(key, title, target, detail, advice, level='warning', category='host'):
    return {'id': key, 'title': title, 'target': target, 'detail': detail, 'advice': advice,
            'level': level, 'category': category}


class Center:
    def __init__(self, history, *, runner, which, config_root=Path('/etc'), clock=time.time):
        self.history, self.run, self.which = history, runner, which
        self.root, self.clock = Path(config_root), clock

    def _ssh(self):
        checks, risks = [], []
        binary = self.which('sshd')
        try:
            if not binary:
                raise RuntimeError('未找到 sshd')
            output = self.run([str(binary), '-T'])
            values = dict(line.split(None, 1) for line in output.splitlines() if len(line.split(None, 1)) == 2)
            for option, name in [('passwordauthentication', '密码登录'), ('permitrootlogin', 'root 登录'), ('permitemptypasswords', '空密码登录')]:
                value = values.get(option)
                if value is None:
                    raise ValueError('SSH 返回缺少配置项')
                checks.append({'name': f'SSH {name}', 'state': 'known', 'detail': f'{option} = {value}（默认配置，连接匹配条件可能不同）'})
                if value == 'yes':
                    risks.append(finding('ssh:' + option, f'SSH 允许{name}', 'sshd 默认配置',
                                         f'{option} = yes；不代表发生入侵，实际登录还受认证与 Match 条件约束。',
                                         '先验证密钥登录和备用连接，再由管理员调整；不会自动关闭现有登录方式。',
                                         'critical' if option == 'permitemptypasswords' else 'warning', 'login'))
        except (OSError, ValueError, RuntimeError):
            checks = [{'name': 'SSH 登录配置', 'state': 'unknown', 'detail': '未能读取 sshd -T；没有按安全默认值补齐。'}]
            risks = []
        return checks, risks

    def _docker(self):
        if not self.which('docker'):
            return [{'name': 'Docker 权限', 'state': 'not_installed', 'detail': '未发现 Docker 命令'}], [], []
        risks, count, watched = [], 0, []
        try:
            ids = self.run(['docker', 'ps', '-q', '--no-trunc']).split()
            if len(ids) > 100 or any(not re.fullmatch('[a-f0-9]{64}', value) for value in ids):
                raise ValueError('Docker 清单异常或超过 100 个运行中容器')
            for start in range(0, len(ids), 20):
                batch = ids[start:start + 20]
                lines = self.run(['docker', 'inspect', '--format', DOCKER_FORMAT, *batch]).splitlines()
                if len(lines) != len(batch):
                    raise ValueError('Docker 明细不完整')
                seen = set()
                for line in lines:
                    item = json.loads(line)
                    identity = item['id']
                    if identity not in batch or identity in seen:
                        raise ValueError('Docker 容器身份不匹配')
                    seen.add(identity)
                    name = str(item['name']).lstrip('/')[:128]
                    count += 1
                    for bad, suffix, title, evidence, advice, level in [
                        (item['privileged'] is True, 'privileged', '容器启用了特权模式', 'Privileged = true', '核对是否确需完整主机权限，修改编排后需重建容器。', 'critical'),
                        (item['pid'] == 'host', 'pid', '容器共享主机进程空间', 'PidMode = host', '核对监控或系统工具是否确实需要读取主机进程。', 'warning'),
                        (item['network'] == 'host', 'network', '容器使用主机网络', 'NetworkMode = host', '核对进程监听地址与主机防火墙；主机网络并不等于已公开。', 'warning'),
                        ('SYS_ADMIN' in (item['capabilities'] or []) or 'ALL' in (item['capabilities'] or []), 'capability', '容器具有高权限能力', 'CapAdd 包含 SYS_ADMIN 或 ALL', '核对容器实际需要的能力，避免授予全部权限。', 'critical')]:
                        if bad:
                            risks.append(finding(f'docker:{identity}:{suffix}', title, name, evidence, advice, level, 'docker'))
                    for mount in item['mounts'] or []:
                        source = str(mount.get('Source', '')).rstrip('/') or '/'
                        target = str(mount.get('Destination', ''))[:300]
                        source_path = Path(source)
                        if (mount.get('Type') == 'bind' and '..' not in source_path.parts and
                            source.startswith(('/etc/', '/opt/', '/srv/', '/root/')) and len(watched) < 20):
                            if target in {'/etc/caddy/Caddyfile', '/etc/nginx/nginx.conf'}:
                                watched.append(source_path)
                            elif target in {'/etc/caddy', '/etc/nginx'}:
                                watched.append(source_path / ('Caddyfile' if target == '/etc/caddy' else 'nginx.conf'))
                        if source.endswith('/docker.sock') or target.endswith('/docker.sock'):
                            risks.append(finding(f'docker:{identity}:socket:{target}', '容器挂载了 Docker 控制接口', name,
                                                 f'{source} → {target}；即使只读挂载也可能调用 Docker API。',
                                                 '确认这是受信任的管理组件；不自动移除挂载或重建容器。', 'critical', 'docker'))
                        elif source in {'/', '/etc', '/proc', '/sys'} and mount.get('RW') is True:
                            risks.append(finding(f'docker:{identity}:mount:{target}', '容器可写入主机敏感目录', name,
                                                 f'{source} → {target}（可写）', '核对是否可以缩小挂载范围或改为只读。', 'critical', 'docker'))
            return [{'name': 'Docker 权限', 'state': 'known', 'detail': f'已只读检查 {count} 个运行中容器，未读取环境变量。'}], risks, watched
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError):
            return [{'name': 'Docker 权限', 'state': 'unknown', 'detail': '容器权限未能完整读取；本轮结果不代表全部容器。'}], risks, watched

    def _files(self, db, now, additional=()):
        checks, risks, changes, watched = [], [], [], 0
        for path in dict.fromkeys([*(self.root / relative for relative in FILES), *additional]):
            if not path.exists() and not path.is_symlink():
                prior = db.execute('SELECT 1 FROM security_baselines WHERE path=?', (str(path),)).fetchone()
                if prior or path in additional:
                    checks.append({'name': str(path), 'state': 'unknown', 'detail': '已监测或已识别挂载的文件当前不可读；没有认定为已删除。'})
                continue
            try:
                if not safe_file(path):
                    raise ValueError('链接或特殊文件仅展示状态')
                info = path.stat()
                if info.st_size > LIMIT:
                    raise ValueError('文件超过读取上限')
                with path.open('rb') as stream:
                    content = stream.read(LIMIT + 1)
                    after = os.fstat(stream.fileno())
                if len(content) > LIMIT or (info.st_ino, info.st_size, info.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                    raise ValueError('读取时文件正在变化')
                digest = hashlib.sha256(content).hexdigest()
                attributes = json.dumps({'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid}, sort_keys=True)
                old = db.execute('SELECT digest,attributes FROM security_baselines WHERE path=?', (str(path),)).fetchone()
                if old and (old['digest'] != digest or old['attributes'] != attributes):
                    detail = '检测到内容摘要变化' if old['digest'] != digest else '检测到所有者或权限变化'
                    changes.append({'ts': now, 'source': 'config', 'kind': 'config_change', 'ip': '', 'account': '',
                                    'target': str(path), 'detail': detail + '；这是发现时间，不代表未授权修改。',
                                    '_identity': str(path) + old['digest'] + old['attributes'] + digest + attributes + str(now)})
                db.execute('INSERT OR REPLACE INTO security_baselines VALUES (?,?,?)', (str(path), digest, attributes))
                watched += 1
                if os.name == 'posix' and info.st_mode & stat.S_IWOTH:
                    risks.append(finding('file:' + str(path), '关键配置允许所有用户写入', str(path),
                                         f'权限 {stat.S_IMODE(info.st_mode):04o}', '核对所有者和权限后手动修复，不自动 chmod。', 'critical'))
            except (OSError, ValueError):
                checks.append({'name': str(path), 'state': 'unknown', 'detail': '未能安全读取或超过 256 KiB，未更新基线。'})
        checks.insert(0, {'name': '配置变化', 'state': 'known' if watched else 'unknown', 'detail': f'本轮读取 {watched} 个常见配置；首次建立摘要基线，不保存文件内容。'})
        return checks, risks, changes

    def _logins(self, db, ssh, now):
        records = []
        state = {'readable': False, 'checked_at': now, 'limited': False, 'detail': '未确认 SSH 日志来源；保留已采集事件。'}
        try:
            if ssh.get('backend') == 'polling':
                path = Path(ssh.get('logpath', ''))
                if path not in (self.history.log_root / 'auth.log', self.history.log_root / 'secure') or not safe_file(path):
                    raise ValueError('不读取任意登录日志路径')
                lines, flags = read_log(db, path, 'auth:file:' + str(path))
                for identity, message in lines:
                    event = login_event(message, now=now)
                    if event:
                        records.append({**event, '_identity': 'auth:file:' + str(path) + identity})
                state.update(readable=True, limited=flags['limited'] or flags['initial_tail'] or flags['gap'],
                             detail=f'增量读取 {path}' + ('；轮转或截断可能留下空档' if flags['gap'] else ''))
            elif ssh.get('backend') == 'systemd':
                saved = db.execute("SELECT value FROM cursors WHERE key='auth:journal'").fetchone()
                since = max(now - RETENTION, float(saved['value']) - 1 if saved else now - 86400)
                output = self.run(['journalctl', '--quiet', '--no-pager', '--since', '@' + str(int(since)), '-n', '500', '-o', 'json', *SSH_MATCH])
                lines, latest = output.splitlines(), since
                for line in lines:
                    item = json.loads(line)
                    at = int(item.get('__REALTIME_TIMESTAMP', 0)) / 1e6
                    if not 0 < at <= now + 60:
                        continue
                    latest = max(latest, at)
                    event = login_event(item.get('MESSAGE'), at, now)
                    if event:
                        records.append({**event, '_identity': 'auth:journal:' + str(item.get('__CURSOR') or line)})
                db.execute("INSERT OR REPLACE INTO cursors VALUES ('auth:journal',?)", (str(max(latest, now - 1)),))
                state.update(readable=True, limited=len(lines) >= 500, detail='读取 SSH systemd 日志；首次最多回看 24 小时')
        except (OSError, ValueError, RuntimeError, TypeError, AttributeError):
            records = []
            state['detail'] = 'SSH 日志读取失败或格式不完整；保留历史，不代表没有登录活动。'
        return records, state

    def scan(self, facts):
        now = self.clock()
        checks, risks = self._ssh()
        docker_checks, docker_risks, watched = self._docker()
        checks.extend(docker_checks)
        risks.extend(docker_risks)
        for port in facts.get('ports', []):
            target = f"{port['host']}:{port['port']}"
            risks.append(finding(f"port:{port['kind']}:{target}:{port.get('container', '')}",
                                 f"{port['service']} 监听于非回环地址", port.get('container') or target,
                                 target + (f" → {port['container_port']}/{port['protocol']}" if port['kind'] == 'docker' else ''),
                                 port['detail'], category='exposure'))
        with self.history.connection() as db:
            db.executescript(SCHEMA)
            file_checks, file_risks, changes = self._files(db, now, watched)
            checks.extend(file_checks)
            risks.extend(file_risks)
            logins, auth_state = self._logins(db, facts.get('ssh', {}), now)
            for item in [*changes, *logins]:
                identity = hashlib.sha256(item['_identity'].encode()).hexdigest()
                db.execute('INSERT OR IGNORE INTO security_activity VALUES (?,?,?,?,?,?,?,?)',
                           (identity, item['ts'], item['source'], item['kind'], item['ip'], item['account'], item['target'], item['detail']))
            db.execute('DELETE FROM security_activity WHERE ts < ?', (now - RETENTION,))
            db.execute('DELETE FROM security_activity WHERE id IN (SELECT id FROM security_activity ORDER BY ts DESC LIMIT -1 OFFSET 20000)')
            db.execute("INSERT OR REPLACE INTO cursors VALUES ('auth:status',?)", (json.dumps(auth_state),))
        risks.sort(key=lambda item: (item['level'] != 'critical', item['category'], item['id']))
        return {'checked_at': now, 'risks': risks[:200], 'limited': len(risks) > 200, 'checks': checks,
                'notice': '只读检查常见位置与运行中容器。配置摘要首次建立基线；未覆盖项不代表安全。'}

    def activity(self, page=0, source='all'):
        if type(page) is not int or not 0 <= page <= 4000 or source not in {'all', 'ssh', 'config', 'fail2ban'}:
            raise ValueError('安全事件筛选无效')
        empty = {'items': [], 'total': 0, 'page': page, 'page_size': ACTIVITY_PAGE_SIZE, 'source': source, 'login_success_24h': 0,
                 'login_failure_24h': 0, 'config_changes_24h': 0, 'auth': {'readable': False, 'detail': '等待后台采集'}}
        if not self.history.path.exists():
            return empty
        with self.history.connection(readonly=True) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='security_activity'").fetchone():
                return empty
            cutoff = self.clock() - RETENTION
            combined = "(SELECT * FROM security_activity UNION ALL SELECT id,ts,'fail2ban' AS source,kind,ip,'' AS account,jail AS target,reason AS detail FROM events WHERE kind != 'failure')"
            predicate = 'ts >= ?' + (' AND source=?' if source != 'all' else '')
            args = [cutoff] + ([source] if source != 'all' else [])
            items = db.execute('SELECT * FROM ' + combined + ' WHERE ' + predicate + ' ORDER BY ts DESC,id DESC LIMIT ? OFFSET ?', (*args, ACTIVITY_PAGE_SIZE, page * ACTIVITY_PAGE_SIZE)).fetchall()
            total = db.execute('SELECT COUNT(*) FROM ' + combined + ' WHERE ' + predicate, args).fetchone()[0]
            counts = dict(db.execute('SELECT kind,COUNT(*) FROM security_activity WHERE ts BETWEEN ? AND ? GROUP BY kind',
                                     (self.clock() - 86400, self.clock())).fetchall())
            auth = db.execute("SELECT value FROM cursors WHERE key='auth:status'").fetchone()
            bans = db.execute("SELECT COUNT(*) FROM events WHERE kind='ban' AND ts BETWEEN ? AND ?", (self.clock() - 86400, self.clock())).fetchone()[0]
            concentrated = db.execute("SELECT ip,COUNT(*) AS attempts FROM security_activity WHERE kind='login_failure' AND ts BETWEEN ? AND ? GROUP BY ip HAVING COUNT(*) >= 10 ORDER BY attempts DESC LIMIT 10", (self.clock() - 600, self.clock())).fetchall()
            return {**empty, 'items': [dict(row) for row in items], 'total': total,
                    'login_success_24h': counts.get('login_success', 0), 'login_failure_24h': counts.get('login_failure', 0),
                    'config_changes_24h': counts.get('config_change', 0), 'auth': json.loads(auth['value']) if auth else empty['auth'],
                    'bans_24h': bans, 'concentrated_failures': [dict(row) for row in concentrated],
                    'notice': '仅统计已采集事件；登录/配置与封禁历史各保留最多 30 天或 20000 条。日志轮转、读取上限和停机可能留下空档。'}
