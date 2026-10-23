"""Host Fail2ban management; never flush firewall rules or touch business files."""
from __future__ import annotations

import copy
from contextlib import nullcontext
import configparser
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid

from certificates import trusted_path, atomic_write
import security_observation as observation
from security_center import Center
from login_guard import Guard

JAIL = 'mini-deploy-panel'
SSH_JAIL = 'mini-deploy-sshd'
MARKER = '# Managed by mini_deploy: panel login protection'
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')
FILTER = MARKER + r'''
[Definition]
failregex = ^\{"at":"[^"]*","action":"login","actor":"<ADDR>","target":"","success":false,"detail":\{"reason":"bad_password"\}\}$
ignoreregex =
datepattern = %%Y-%%m-%%d %%H:%%M:%%S
'''


def address(value):
    if not isinstance(value, str) or len(value) > 64 or '%' in value:
        raise ValueError('请输入单个 IPv4 或 IPv6 地址，不支持网段或主机名')
    try:
        result = ipaddress.ip_address(value.strip())
    except ValueError as exc:
        raise ValueError('请输入有效的 IPv4 或 IPv6 地址') from exc
    return result.ipv4_mapped if isinstance(result, ipaddress.IPv6Address) and result.ipv4_mapped else result


def networks(text):
    header = 'These IP addresses/networks are ignored:'
    text = text.strip()
    if text.startswith(header):
        text = text[len(header):]
    result = []
    for token in re.split(r"[\s,\[\]'\"|]+", text):
        token = token.lstrip('`+-')
        if not token:
            continue
        try:
            result.append(ipaddress.ip_network(token, strict=False))
        except ValueError as exc:
            raise ValueError('白名单含有无法核实的地址或域名，请先在 Fail2ban 中检查') from exc
    return result


def run(command, *, timeout=15):
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace',
                                timeout=timeout, check=False,
                                env={**os.environ, 'LC_ALL': 'C', 'DEBIAN_FRONTEND': 'noninteractive', 'NEEDRESTART_MODE': 'l'})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f'{command[0]} 无法执行或超时') from exc
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or 'Fail2ban 操作失败')[-1800:])
    if len(result.stdout) > 128 * 1024:
        raise RuntimeError('Fail2ban 返回内容过多，请在服务器查看完整列表；未使用截断的结果')
    return result.stdout


class Manager:
    def __init__(self, home, audit_log, *, config_root=Path('/etc/fail2ban'), runner=run, which=shutil.which,
                 maintenance=nullcontext, audit=None, supported=None, inspector=None, history=None):
        self.home, self.audit_log, self.config_root = Path(home), Path(audit_log), Path(config_root)
        self.run, self.which, self.maintenance, self.audit = runner, which, maintenance, audit
        self.supported = os.name == 'posix' if supported is None else supported
        self.lock = threading.RLock()
        self.job = {}
        self.worker = None
        self.inspector = inspector or observation.Inspector()
        self.history = history or observation.History(home)
        self.center = Center(self.history, runner=self.inspector.run, which=self.which)
        self.guard = Guard(self.history, maintenance=self.maintenance)
        self.scan_lock = threading.Lock()
        self.scan_worker = None
        self.observation = {}

    def request_scan(self):
        if not self.supported:
            return
        with self.lock:
            if self.scan_worker and self.scan_worker.is_alive():
                return
            self.scan_worker = threading.Thread(target=self.scan, daemon=True, name='security-discovery')
            self.scan_worker.start()

    def scan(self):
        if not self.supported or not self.scan_lock.acquire(blocking=False):
            return
        try:
            with self.maintenance():
                facts = self.inspector.scan()
                facts['ssh']['fingerprint'] = ssh_fingerprint(facts['ssh'])
                facts['center'] = self.center.scan(facts)
                checks = [{'name': '监听端口', 'state': 'known' if facts['host_readable'] else 'unknown',
                           'detail': '已读取当前 TCP 监听；不等于公网可达'}]
                if self.which('fail2ban-client'):
                    try:
                        jails = self._jails()
                        checks.append({'name': 'Fail2ban 服务', 'state': 'known', 'detail': f'响应正常，{len(jails)} 条活跃规则'})
                        for jail in (JAIL, SSH_JAIL):
                            if jail in jails:
                                actions = self._client('get', jail, 'actions')
                                checks.append({'name': jail, 'state': 'known' if actions.strip() and actions.strip() != 'OK' else 'unknown',
                                               'detail': '规则运行中；动作配置可读，实际拦截需从另一客户端验证' if actions.strip() != 'OK' else '动作配置未能确认'})
                        target = self._client('get', 'logtarget').strip()
                        self.history.collect(target)
                    except (OSError, ValueError, RuntimeError) as exc:
                        checks.append({'name': 'Fail2ban 服务或日志', 'state': 'unknown', 'detail': str(exc)[:300]})
                else:
                    checks.append({'name': 'Fail2ban', 'state': 'unknown', 'detail': '尚未安装，未启用防护'})
                try:
                    if self.which('nft'):
                        rules = json.loads(self.inspector.run(['nft', '-j', 'list', 'ruleset']))
                        found = any('f2b' in str(item.get('table', {}).get('name', '')) or
                                    'f2b' in str(item.get('chain', {}).get('name', '')) for item in rules.get('nftables', []))
                    elif self.which('iptables'):
                        rules = self.inspector.run(['iptables', '-S'])
                        found = bool(re.search(r'\bf2b[-_]', rules))
                    else:
                        found = False
                    checks.append({'name': '防火墙对象', 'state': 'known' if found else 'unknown',
                                   'detail': '检测到 Fail2ban 防火墙对象；不代表所有入口或 IPv6 已被覆盖' if found else '未确认 Fail2ban 防火墙对象；无封禁时动作可能尚未创建'})
                except (OSError, ValueError, RuntimeError, AttributeError):
                    checks.append({'name': '防火墙对象', 'state': 'unknown', 'detail': '无法读取规则；不代表已受保护'})
                facts['checks'] = checks
                with self.lock:
                    self.observation = facts
        except Exception as exc:
            with self.lock:
                self.observation = {**self.observation, 'scan_error': str(exc)[:300]}
        finally:
            self.scan_lock.release()

    def _client(self, *args):
        return self.run(['fail2ban-client', *args])

    def _jails(self):
        output = self._client('status')
        match = re.search(r'Jail list:\s*([^\n]*)', output)
        if not match:
            raise RuntimeError('无法识别 Fail2ban 规则列表，请检查服务状态')
        values = [name.strip() for name in match[1].split(',') if name.strip()]
        if len(values) > 256 or any(not NAME.fullmatch(name) for name in values):
            raise RuntimeError('Fail2ban 规则列表异常或过长')
        return values

    def status(self, jail='', job_only=False, history_page=0, activity_page=0, activity_source='all'):
        with self.lock:
            job = copy.deepcopy(self.job)
        if job_only:
            return {'job': job}
        result = {'supported': self.supported, 'installed': bool(self.which('fail2ban-client')),
                  'running': False, 'jails': [], 'job': job, 'managed_jail': JAIL,
                  'notice': '封禁范围由所选 Fail2ban 规则决定。主机端口规则不保证拦截 Docker 发布端口；面板经反向代理访问时需保护相应入口端口。'}
        with self.lock:
            result['observation'] = copy.deepcopy(self.observation)
            result['scanning'] = bool(self.scan_worker and self.scan_worker.is_alive())
        result['history'] = self.history.snapshot(history_page)
        result['activity'] = self.center.activity(activity_page, activity_source)
        if not self.supported or not result['installed']:
            return result
        try:
            jails = self._jails()
        except RuntimeError as exc:
            result['detail'] = str(exc)
            return result
        result.update(running=True, jails=jails, panel_enabled=JAIL in jails, ssh_enabled=SSH_JAIL in jails,
                      existing_ssh='sshd' in jails)
        if jail:
            if jail not in jails:
                raise ValueError('规则已不存在，请刷新列表')
            banned = self._client('get', jail, 'banip').split()
            ips = [str(address(item)) for item in banned]
            ignores = self._client('get', jail, 'ignoreip')
            values = {key: self._client('get', jail, key).strip() for key in ('bantime', 'findtime', 'maxretry')}
            result['selected'] = {'name': jail, 'banned': ips, 'ignoreip': [str(item) for item in networks(ignores)], **values}
            try:
                result['selected']['ban_details'] = observation.timed_bans(self._client('get', jail, 'banip', '--with-time'))
            except RuntimeError:
                result['selected']['ban_details'] = {}
        for name, field in ((JAIL, 'settings'), (SSH_JAIL, 'ssh_settings')):
            path = self.config_root / 'jail.d' / f'{name}.local'
            trusted_path(path)
            if not path.exists():
                continue
            if path.stat().st_size > 64 * 1024:
                raise ValueError(f'{path.name} 超过配置读取上限，原文件已保留')
            try:
                content = path.read_text(encoding='utf-8')
                if content.startswith(MARKER):
                    parser = configparser.ConfigParser(interpolation=None)
                    parser.read_string(content)
                    result[field] = dict(parser[name])
            except (configparser.Error, KeyError, UnicodeError) as exc:
                raise ValueError(f'{path.name} 防护配置格式异常，请核对配置；原文件已保留') from exc
        return result

    def start(self, data, client_ip):
        action = data.get('action')
        if not isinstance(action, str) or action not in {'configure', 'configure_ssh', 'disable', 'disable_ssh', 'ban', 'unban'} or data.get('confirmed') is not True:
            raise ValueError('请确认本次 IP 防护操作')
        if not self.supported:
            raise ValueError('IP 防护需要在 Linux 主机上使用')
        if action in {'ban', 'unban'}:
            ip = address(data.get('ip'))
            jail = data.get('jail')
            if not isinstance(jail, str) or not NAME.fullmatch(jail):
                raise ValueError('请选择有效的防护规则')
            if action == 'ban' and (ip == address(client_ip) or ip.is_loopback or ip.is_unspecified or ip.is_multicast):
                raise ValueError('不能封禁当前访问 IP、回环地址、未指定地址或组播地址')
            options = {'ip': str(ip), 'jail': jail}
        elif action in {'configure', 'configure_ssh'}:
            options = self._settings(data, client_ip)
            if action == 'configure_ssh':
                expected = data.get('expected_ssh')
                if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
                    raise ValueError('请先刷新 SSH 检测结果后确认保护范围')
                options['expected_ssh'] = expected
        else:
            options = {}
        with self.lock:
            if self.job.get('state') == 'running':
                raise ValueError('已有防护操作正在执行，请稍候')
            self.job = {'id': uuid.uuid4().hex, 'action': action, 'state': 'running', 'started_at': time.time(), 'stage': '检查服务器环境'}
            self.worker = threading.Thread(target=self._work, args=(action, options, client_ip), daemon=True, name='fail2ban-management')
            self.worker.start()
            return {'job': copy.deepcopy(self.job)}

    def _settings(self, data, client_ip):
        result = {}
        for key, default, low, high in [('maxretry', 5, 3, 50), ('findtime', 600, 60, 86400), ('bantime', 3600, 60, 604800)]:
            value = data.get(key, default)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'{key} 请填写 {low} 到 {high} 之间的整数')
            result[key] = value
        raw = data.get('ignoreip', [])
        if not isinstance(raw, list) or len(raw) > 50 or any(not isinstance(item, str) or len(item) > 80 for item in raw):
            raise ValueError('白名单最多 50 个 IP 或网段')
        allowed = []
        for item in raw:
            if '%' in item or '\n' in item or '\r' in item:
                raise ValueError('白名单不支持接口作用域或换行配置')
            try:
                net = ipaddress.ip_network(item.strip(), strict=False)
            except ValueError as exc:
                raise ValueError('白名单只支持 IP 或 CIDR 网段') from exc
            if net.prefixlen == 0:
                raise ValueError('白名单不能包含全部互联网地址')
            allowed.append(str(net))
        allowed.extend(['127.0.0.0/8', '::1', str(address(client_ip))])
        result['ignoreip'] = list(dict.fromkeys(allowed))
        ports = data.get('ports', [6868])
        if ports not in ([6868], [6868, 80, 443]):
            raise ValueError('请选择面板端口，或同时保护网页入口 80/443')
        result['ports'] = ports
        increment = data.get('increment', False)
        if type(increment) is not bool:
            raise ValueError('递增封禁必须是开关值')
        result['increment'] = increment
        return result

    def _stage(self, text):
        with self.lock:
            self.job['stage'] = text

    def _work(self, action, options, client_ip):
        try:
            with self.maintenance():
                if action == 'configure':
                    self._configure(options)
                    message = '面板规则已启用，当前访问 IP 已加入白名单；实际拦截需外部验证'
                elif action == 'configure_ssh':
                    self._configure_ssh(options)
                    message = 'SSH 专属规则已启用，未修改 SSH 登录配置；实际拦截需外部验证'
                elif action in {'disable', 'disable_ssh'}:
                    self._disable(SSH_JAIL if action == 'disable_ssh' else JAIL)
                    message = '所选专属防护已暂停；配置、备份及其他防护规则保留'
                else:
                    jail, ip = options['jail'], options['ip']
                    if jail not in self._jails():
                        raise ValueError('规则已不存在，请刷新列表')
                    if action == 'ban':
                        allowed = networks(self._client('get', jail, 'ignoreip'))
                        if any(address(ip) in net for net in allowed):
                            raise ValueError('该 IP 在所选规则的白名单内，未执行封禁')
                    self._client('set', jail, 'banip' if action == 'ban' else 'unbanip', ip)
                    after = {str(address(value)) for value in self._client('get', jail, 'banip').split()}
                    if (ip in after) != (action == 'ban'):
                        raise RuntimeError('Fail2ban 未确认操作结果，请刷新规则检查')
                    message = '已加入所选规则的封禁列表' if action == 'ban' else '已从所选规则解封；其他规则仍可能封禁该 IP'
            if self.audit:
                self.audit(action, True, {**options, 'actor': client_ip})
            with self.lock:
                self.job.update(state='succeeded', message=message, finished_at=time.time())
            self.request_scan()
        except Exception as exc:  # noqa: BLE001 - preserve async failure and never retry writes
            if self.audit:
                try:
                    self.audit(action, False, {'actor': client_ip})
                except Exception:
                    pass
            with self.lock:
                self.job.update(state='failed', message=str(exc)[:1800], finished_at=time.time())

    def _install(self):
        if not self.which('fail2ban-client'):
            self._stage('安装主机 Fail2ban，已有服务保持运行')
            if self.which('apt-get'):
                self.run(['apt-get', 'update'], timeout=180)
                self.run(['apt-get', 'install', '-y', 'fail2ban'], timeout=240)
            elif self.which('dnf') or self.which('yum'):
                self.run(['dnf' if self.which('dnf') else 'yum', 'install', '-y', 'fail2ban'], timeout=240)
            else:
                raise RuntimeError('无法识别软件包管理器，请先安装 fail2ban')
    def _action(self):
        action = 'nftables-multiport' if self.which('nft') else 'iptables-multiport' if self.which('iptables') else ''
        if not action:
            raise RuntimeError('未找到 nft 或 iptables，请先安装主机防火墙工具；未修改已有规则')
        return action

    def _configure(self, options):
        self._install()
        self._check_increment(options)
        self._stage('备份并校验面板专属规则')
        action = self._action()
        path = str(self.audit_log)
        if not self.audit_log.is_absolute() or any(char in path for char in '\r\n%[]*?<>'):
            raise ValueError('审计日志路径无法安全用于 Fail2ban 配置')
        trusted_path(self.audit_log)
        if not self.audit_log.is_file():
            raise ValueError('审计日志尚未创建，请刷新后重试')
        jail = MARKER + f'''\n[{JAIL}]
enabled = true
filter = mini-deploy-panel
port = {','.join(map(str, options['ports']))}
protocol = tcp
banaction = {action}
backend = polling
usedns = no
logpath = {path}
maxretry = {options['maxretry']}
findtime = {options['findtime']}
bantime = {options['bantime']}
ignoreip = {' '.join(options['ignoreip'])}
bantime.increment = {'true' if options['increment'] else 'false'}
bantime.maxtime = 604800
'''
        files = [(self.config_root / 'filter.d' / 'mini-deploy-panel.conf', FILTER),
                 (self.config_root / 'jail.d' / 'mini-deploy-panel.local', jail)]
        self._apply(files, JAIL)

    def _configure_ssh(self, options):
        ssh = self.inspector.scan()['ssh']
        if not ssh['ready'] or not ssh['ports']:
            raise ValueError(ssh['detail'])
        if ssh_fingerprint(ssh) != options['expected_ssh']:
            raise ValueError('SSH 端口或日志来源已变化，请刷新检测后重新确认；未写入规则')
        self._install()
        self._check_increment(options)
        dump = self._client('-d')
        if re.search(r"\[['\"]add['\"],\s*['\"]sshd['\"]", dump):
            raise ValueError('已有 sshd 规则正在运行，请沿用现有规则；未新增重复防护')
        try:
            active = self._jails()
        except RuntimeError:
            active = []  # Full configuration validation precedes starting a stopped service.
        if 'sshd' in active:
            raise ValueError('已有 sshd 规则正在运行，请沿用现有规则；未新增重复防护')
        action = self._action()
        source = 'journalmatch = _SYSTEMD_UNIT=ssh.service + _SYSTEMD_UNIT=sshd.service + _COMM=sshd + _COMM=sshd-session\n'
        if ssh['backend'] == 'polling':
            trusted_path(Path(ssh['logpath']))
            source = 'logpath = ' + ssh['logpath'] + '\n'
        jail = MARKER + f'''\n[{SSH_JAIL}]
enabled = true
filter = sshd[mode=normal]
port = {','.join(map(str, ssh['ports']))}
protocol = tcp
banaction = {action}
backend = {ssh['backend']}
{source}usedns = no
maxretry = {options['maxretry']}
findtime = {options['findtime']}
bantime = {options['bantime']}
ignoreip = {' '.join(options['ignoreip'])}
bantime.increment = {'true' if options['increment'] else 'false'}
bantime.maxtime = 604800
'''
        self._apply([(self.config_root / 'jail.d' / f'{SSH_JAIL}.local', jail)], SSH_JAIL)

    def _check_increment(self, options):
        if options['increment']:
            match = re.search(r'(\d+)\.(\d+)', self._client('version'))
            if not match or (int(match[1]), int(match[2])) < (0, 11):
                raise ValueError('递增封禁需要 Fail2ban 0.11 或更新版本；请关闭此选项或升级后重试')

    def _apply(self, files, jail):
        previous = {}
        for file, _ in files:
            trusted_path(file)
            text = file.read_text(encoding='utf-8') if file.exists() else None
            if text is not None and not text.startswith(MARKER):
                raise ValueError(f'检测到同名外部配置，未覆盖：{file}')
            previous[file] = text
        backup = self.home / 'ip-security-backups' / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
        trusted_path(backup)
        backup.mkdir(parents=True, mode=0o700)
        with self.lock:
            self.job['backup'] = str(backup)
        written = {}
        try:
            for file, content in files:
                if previous[file] is not None:
                    atomic_write(backup / file.name, previous[file])
                file.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(file, content)
                written[file] = content
            self.run(['fail2ban-client', '-t'], timeout=30)
            self.run(['systemctl', 'enable', '--now', 'fail2ban'], timeout=30)
            self._client('reload', '--if-exists', jail)
            if jail not in self._jails():
                raise RuntimeError('未能确认专属防护规则运行')
        except Exception:
            for file, content in written.items():
                if file.read_text(encoding='utf-8') != content:
                    raise RuntimeError('配置被外部修改，停止恢复；请核对保留的配置和备份') from None
                # Keep new files; disabling a newly created jail avoids deleting server files.
                replacement = previous[file] if previous[file] is not None else MARKER + f'\n[{jail}]\nenabled = false\n' if file.suffix == '.local' else content
                atomic_write(file, replacement)
            try:
                self._client('reload', '--if-exists', jail)
            except RuntimeError:
                pass
            raise

    def _disable(self, jail=JAIL):
        path = self.config_root / 'jail.d' / f'{jail}.local'
        trusted_path(path)
        original = path.read_text(encoding='utf-8')
        if not original.startswith(MARKER):
            raise ValueError('这不是面板托管的规则，未修改')
        content, count = re.subn(r'^enabled\s*=.*$', 'enabled = false', original, count=1, flags=re.M)
        if count != 1:
            raise ValueError('面板规则格式无法确认，未修改')
        backup = self.home / 'ip-security-backups' / ('pause-' + uuid.uuid4().hex)
        trusted_path(backup)
        backup.mkdir(parents=True, mode=0o700)
        atomic_write(backup / path.name, original)
        atomic_write(path, content)
        try:
            self.run(['fail2ban-client', '-t'], timeout=30)
            self._client('reload', '--if-exists', jail)
            if jail in self._jails():
                raise RuntimeError('未能确认面板规则已暂停')
        except Exception:
            if path.read_text(encoding='utf-8') != content:
                raise RuntimeError('配置被外部修改，停止恢复，请检查备份') from None
            atomic_write(path, original)
            self._client('reload', '--if-exists', jail)
            raise


def ssh_fingerprint(ssh):
    import hashlib
    return hashlib.sha256(json.dumps({key: ssh.get(key) for key in ('ports', 'backend', 'logpath')}, sort_keys=True).encode()).hexdigest()


def evaluate(monitor, snapshot):
    history = snapshot.get('history', {})
    fresh = history.get('readable') and 0 <= monitor.clock() - history.get('checked_at', 0) <= 120
    bad = history.get('recent_bans', 0) >= 5
    monitor.observe('security:ban-burst', bad if fresh and (bad or not history.get('limited')) else None, '短时间内出现多次 IP 封禁',
                    f"最近 10 分钟记录 {history.get('recent_bans', 0)} 次封禁，涉及 {history.get('recent_ips', 0)} 个 IP；请核对防护历史与规则，记录不一定都是自动封禁。",
                    'security', 'warning', immediate=True,
                    metadata={'reason': 'ban_burst', 'ban_count': history.get('recent_bans', 0), 'ip_count': history.get('recent_ips', 0)})
    with monitor.lock:
        monitor.coverage['security'] = {'status': 'healthy' if fresh and not history.get('limited') else 'unknown',
                                        'checked_at': history.get('checked_at'), 'detail': history.get('detail', '等待防护日志采集')}
    if 'activity' in snapshot:
        activity = snapshot['activity']
        auth = activity.get('auth', {})
        sources = activity.get('concentrated_failures', [])
        available = auth.get('readable') and 0 <= monitor.clock() - auth.get('checked_at', 0) <= 120
        failed = sum(item['attempts'] for item in sources)
        monitor.observe('security:ssh-failures', bool(sources) if available and (sources or not auth.get('limited')) else None,
                        'SSH 出现集中认证失败', f'最近 10 分钟，{len(sources)} 个来源分别至少失败 10 次，共观测到 {failed} 次失败；请核对是否为正常连接重试。',
                        'security', 'warning', immediate=True,
                        metadata={'reason': 'ssh_failure_burst', 'login_failure_count': failed, 'ip_count': len(sources)})
