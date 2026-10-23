"""Opt-in host connection protection and reversible limits on existing Nginx entries."""
from contextlib import nullcontext
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import subprocess
import threading
import time

from certificates import trusted_path
import gateway_connections as proxy
from login_guard import address
import nginx_runtime
from security_observation import listeners

TABLE = 'mini_deploy_basic_guard_v1'
OWNER = 'mini_deploy basic guard v1'
MARKER = '# mini-deploy basic protection v1'
STATE_KEY = 'basic:protection:v1'


class ConnectionsFirewall:
    def __init__(self, runner=subprocess.run, which=shutil.which, supported=None):
        self.run, self.which = runner, which
        self.supported = os.name == 'posix' if supported is None else supported

    def apply(self, ports, ignores, enabled):
        if not self.supported or not self.which('nft'):
            raise RuntimeError('主机没有可用的 nftables，连接防护未生效；登录防护仍可使用')
        args = dict(capture_output=True, text=True, timeout=8, check=False)
        result = self.run(['nft', '-j', 'list', 'tables'], **args)
        if result.returncode:
            raise RuntimeError('无法读取主机防火墙权限，连接防护未生效')
        exists = any(item.get('table', {}).get('name') == TABLE and item['table'].get('family') == 'inet'
                     for item in json.loads(result.stdout).get('nftables', []))
        commands = []
        if exists:
            result = self.run(['nft', '-j', 'list', 'table', 'inet', TABLE], **args)
            if result.returncode or not any(item.get('table', {}).get('comment') == OWNER
                                            for item in json.loads(result.stdout).get('nftables', [])):
                raise RuntimeError('同名防火墙表无法核验归属，已保留原规则')
            commands.append(f'delete table inet {TABLE}')
        if enabled and ports:
            ports = sorted({int(port) for port in ports if type(port) is int and 1 <= port <= 65535})
            portset = ', '.join(map(str, ports))
            commands.append(f'add table inet {TABLE} {{ comment "{OWNER}"; }}')
            for chain in ('input', 'forward'):
                commands.append(f'add chain inet {TABLE} {chain} {{ type filter hook {chain} priority -6; policy accept; }}')
                commands.append(f'add rule inet {TABLE} {chain} iifname "lo" return')
                for version in (4, 6):
                    family = 'ip' if version == 4 else 'ip6'
                    nets = [str(ipaddress.ip_network(net)) for net in ignores if ipaddress.ip_network(net).version == version]
                    if nets:
                        commands.append(f'add rule inet {TABLE} {chain} {family} saddr {{ ' + ', '.join(nets) + ' } return')
                    name = f'syn{version}_{chain}'
                    commands.append(f'add set inet {TABLE} {name} {{ type ipv{version}_addr; flags dynamic,timeout; timeout 1m; size 65536; }}')
                    # Docker published traffic uses forward after DNAT; never match container egress.
                    target = (f'tcp dport {{ {portset} }}' if chain == 'input' else
                              f'ct direction original ct status dnat ct original proto-dst {{ {portset} }}')
                    commands.append(f'add rule inet {TABLE} {chain} meta l4proto tcp {target} ct state new '
                                    f'tcp flags & (syn | ack) == syn update @{name} {{ {family} saddr '
                                    'limit rate over 60/second burst 120 packets } counter drop')
        if not commands:
            return
        content = '\n'.join(commands) + '\n'
        result = self.run(['nft', '-c', '-f', '-'], input=content, **args)
        if result.returncode:
            raise RuntimeError('主机不支持所需连接限制规则，原防火墙未变')
        result = self.run(['nft', '-f', '-'], input=content, **args)
        if result.returncode:
            raise RuntimeError('连接限制规则未能应用，原防火墙未变')


def mapped_path(runtime, visible):
    if not visible.startswith('/') or '..' in PurePosixPath(visible).parts:
        raise ValueError('代理配置路径无效')
    if runtime['mode'] == 'local':
        result = Path(visible)
        boundary = Path('/etc/nginx')
    else:
        result = None
        for mount in sorted(runtime['mounts'], key=lambda item: len(item.get('Destination', '')), reverse=True):
            dest = mount.get('Destination', '').rstrip('/')
            if dest and (visible == dest or visible.startswith(dest + '/')):
                if mount.get('Type') not in {'bind', 'volume'}:
                    break
                result = Path(mount['Source']) / visible[len(dest):].lstrip('/')
                boundary = Path(mount['Source'])
                break
        if result is None:
            raise ValueError('配置未挂载到宿主机，保留容器内原文件')
    # Debian's sites-enabled commonly contains root-owned links into sites-available.
    if result.is_symlink():
        trusted_path(result.parent)
        if os.name == 'posix' and result.lstat().st_uid not in {0, os.geteuid()}:
            raise ValueError('配置链接所有者不可信，未修改')
        resolved = result.resolve(strict=True)
        if not resolved.is_relative_to(boundary.resolve()):
            raise ValueError('配置链接指向挂载或常见配置目录之外，未修改')
        result = resolved
    trusted_path(result)
    return result


def nginx_candidates(runtime, dump, ignores):
    """Only touch files reported by the running Nginx; retain imports and every business rule."""
    paths = re.findall(r'^# configuration file (/[^\r\n]+):\s*$', dump, re.M)
    if not paths or len(paths) > 64:
        raise ValueError('无法完整识别正在使用的 Nginx 配置文件')
    contents = {}
    for visible in dict.fromkeys(paths):
        path = mapped_path(runtime, visible)
        contents[str(path)] = proxy.read_config(path)
    main = mapped_path(runtime, paths[0])
    tree = proxy.parse(contents[str(main)], 'nginx')
    http = [node for node in tree if node.words == ['http']]
    if len(http) != 1:
        raise ValueError('未找到唯一的 HTTP 配置块，保留原配置')
    for content in contents.values():
        nodes = list(proxy.walk(proxy.parse(content, 'nginx')))
        if any(node.words[:1] in (['real_ip_header'], ['set_real_ip_from'], ['limit_req'], ['limit_conn'],
                                  ['limit_rate'], ['limit_rate_after']) for node in nodes):
            raise ValueError('已有真实 IP 或限流配置，避免覆盖；请先核对现有入口策略')
        if MARKER in content or 'mini_basic_' in content:
            raise ValueError('配置已包含基础防护标记，需要核对保存的防护记录')
    whitelist = '\n'.join('        ' + str(ipaddress.ip_network(net)) + ' 1;' for net in ignores)
    definitions = f'''
    {MARKER}
    geo $mini_basic_allow {{
        default 0;
{whitelist}
    }}
    map $mini_basic_allow $mini_basic_key {{ 0 $binary_remote_addr; 1 ""; }}
    limit_req_zone $mini_basic_key zone=mini_basic_requests:10m rate=100r/s;
    limit_conn_zone $mini_basic_key zone=mini_basic_connections:10m;
'''
    edits = {str(main): [(http[0].body, definitions)]}
    servers = 0
    for filename, content in contents.items():
        nodes = list(proxy.walk(proxy.parse(content, 'nginx')))
        # A stream/mail configuration is not an HTTP entry.
        if any(node.words[:1] in (['stream'], ['mail']) for node in nodes):
            raise ValueError('配置同时包含非 HTTP 服务，本轮保留原配置')
        for node in nodes:
            if node.words != ['server'] or not node.body:
                continue
            if any(child.words[:1] == ['listen'] and any('proxy_protocol' == value for value in child.words) for child in node.children):
                raise ValueError('入口使用 PROXY 协议，需先确认可信代理范围')
            servers += 1
            edits.setdefault(filename, []).append((node.body, f'''
        {MARKER}
        limit_req zone=mini_basic_requests burst=200 nodelay;
        limit_req_status 429;
        limit_conn mini_basic_connections 100;
        limit_conn_status 429;
'''))
    if not servers:
        raise ValueError('未识别 HTTP 站点，不自动修改')
    changed = []
    for filename, inserts in edits.items():
        original = contents[filename]
        candidate = original
        for offset, snippet in sorted(inserts, reverse=True):
            candidate = candidate[:offset] + snippet + candidate[offset:]
        changed.append({'path': filename, 'before': original, 'after': candidate})
    return changed, servers


def discover():
    runtimes, ports, errors = [], set(), []
    try:
        ports.update(row['port'] for row in listeners(proxy.command(['ss', '-H', '-lntp']))
                     if row['port'] in {80, 443} and row['scope'] != 'loopback')
    except (ValueError, OSError):
        errors.append('主机网站监听读取失败；只处理能确认的 Docker 发布端口')
    if shutil.which('nginx'):
        try:
            proxy.command(['systemctl', 'is-active', 'nginx'])
            startup = proxy.command(['systemctl', 'show', 'nginx', '--property=ExecStart', '--value'])
            if re.search(r'(?:^|\s)-(?:c|p)(?:\s|/)', startup):
                raise ValueError('本机 Nginx 使用自定义启动配置，暂不自动修改')
            runtimes.append({'mode': 'local', 'kind': 'nginx', 'label': '本机 Nginx', 'prefix': []})
        except (ValueError, OSError) as exc:
            errors.append(str(exc))
    if shutil.which('caddy'):
        try:
            proxy.command(['systemctl', 'is-active', 'caddy'])
            runtimes.append({'mode': 'local', 'kind': 'caddy', 'label': '本机 Caddy', 'prefix': []})
        except (ValueError, OSError):
            pass
    if shutil.which('docker'):
        try:
            nginx_runtime.require_local_docker()
            lines = proxy.command(['docker', 'ps', '--format', '{{json .}}']).splitlines()
            if len(lines) > 40:
                errors.append('容器超过 40 个，本轮只扫描前 40 个')
            for line in lines[:40]:
                row = json.loads(line)
                name = row.get('Names', '')
                if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', name):
                    continue
                # Only published web entries, never private inserted recording gateways.
                item = json.loads(proxy.command(['docker', 'inspect', '--type', 'container', name]))[0]
                published = {int(binding['HostPort']) for values in (item.get('NetworkSettings', {}).get('Ports') or {}).values()
                             for binding in values or [] if binding.get('HostPort', '').isdigit()}
                ports.update(published & {80, 443})
                tag = (row.get('Image', '') + ' ' + name).lower()
                kind = 'caddy' if 'caddy' in tag else 'nginx' if 'nginx' in tag else ''
                if not published.intersection({80, 443}) and item.get('HostConfig', {}).get('NetworkMode') != 'host':
                    continue
                ident = item.get('Id', '')
                if not re.fullmatch(r'[a-f0-9]{64}', ident):
                    continue
                argv = (item.get('Config', {}).get('Entrypoint') or []) + (item.get('Config', {}).get('Cmd') or [])
                runtimes.append({'mode': 'docker', 'kind': kind or 'unknown', 'label': name, 'container_id': ident,
                                 'mounts': item.get('Mounts', []), 'custom': any(re.match(r'^-[cp]', str(arg)) for arg in argv),
                                 'prefix': ['docker', 'exec', ident]})
        except (ValueError, OSError, KeyError, TypeError) as exc:
            errors.append('Docker 入口扫描不完整：' + str(exc)[:160])
    return runtimes, sorted(ports), errors


class Protection:
    def __init__(self, home, guard, *, maintenance=nullcontext, serial=nullcontext, firewall=None, discovery=discover, clock=time.time):
        self.home, self.guard, self.maintenance = Path(home), guard, maintenance
        self.serial = serial
        self.firewall, self.discover, self.clock = firewall or ConnectionsFirewall(), discovery, clock
        self.lock = threading.RLock()
        self.worker = None
        self.state = self._load()
        self._published = self._public()

    def _load(self):
        with self.maintenance(), self.guard.history.connection() as db:
            row = db.execute('SELECT value FROM cursors WHERE key=?', (STATE_KEY,)).fetchone()
        return json.loads(row[0]) if row else {'enabled': False, 'phase': 'off', 'checked_at': 0, 'entries': [], 'errors': []}

    def _save(self):
        with self.guard.history.connection() as db:
            db.execute('INSERT OR REPLACE INTO cursors VALUES (?,?)', (STATE_KEY, json.dumps(self.state)))
        self._published = self._public()

    def _public(self):
        return json.loads(json.dumps({key: value for key, value in self.state.items()
                                     if key not in {'records', 'ignores', 'previous_login'}}))

    def status(self):
        # A slow proxy reload must never block status polling on the web thread.
        value = json.loads(json.dumps(self._published))
        value['busy'] = bool(self.worker and self.worker.is_alive())
        if value['phase'] == 'applying' and not value['busy']:
            value['phase'] = 'interrupted'
        return value

    def start(self, enabled, client_ip, ssh):
        if type(enabled) is not bool:
            raise ValueError('基础防护状态无效')
        client_ip = address(client_ip)
        if self.worker and self.worker.is_alive():
            raise ValueError('基础防护正在处理，请稍候')
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ValueError('基础防护正在处理，请稍候')
            self.worker = threading.Thread(target=self._work, args=(enabled, client_ip, dict(ssh)), daemon=True,
                                           name='basic-protection')
            self.worker.start()
        return self.status()

    def _write_record(self, record, enable):
        with self.serial():
            self._write_record_serial(record, enable)

    def _write_record_serial(self, record, enable):
        if record['prefix']:
            # Container recreation invalidates the captured runtime identity.
            proxy.command(['docker', 'exec', record['prefix'][2], 'nginx', '-t'])
        files = record['files']
        expected, desired = ('before', 'after') if enable else ('after', 'before')
        for item in files:
            if proxy.read_config(Path(item['path'])) not in (item[expected], item[desired]):
                raise ValueError('业务配置已被外部修改，未覆盖；请核对保留的备份')
        written = []
        try:
            for item in files:
                path = Path(item['path'])
                current = proxy.read_config(path)
                if current != item[desired]:
                    proxy.Connections.write_live(path, item[expected], item[desired])
                    written.append(item)
            proxy.command([*record['prefix'], 'nginx', '-t'])
            proxy.command([*record['prefix'], 'nginx', '-s', 'reload'])
        except (ValueError, OSError):
            for item in reversed(written):
                proxy.Connections.write_live(Path(item['path']), item[desired], item[expected])
            try:
                proxy.command([*record['prefix'], 'nginx', '-t'])
                proxy.command([*record['prefix'], 'nginx', '-s', 'reload'])
            except (ValueError, OSError) as exc:
                raise RuntimeError('入口应用失败，自动恢复加载未完成，请核对已保留备份') from exc
            raise

    def _work(self, enabled, client_ip, ssh):
        try:
            with self.maintenance(), self.lock:
                self.state.update(phase='applying', errors=[])
                self._save()
            if enabled:
                self._enable(client_ip, ssh)
            else:
                self._disable(client_ip, ssh)
        except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
            with self.maintenance(), self.lock:
                self.state.update(phase='partial', errors=[str(exc)[:300]], checked_at=self.clock())
                self._save()

    def _enable(self, client_ip, ssh):
        runtimes, published, errors = self.discover()
        with self.maintenance(), self.lock:
            old = self.guard.status()['settings']
            if not self.state.get('enabled') and 'previous_login' not in self.state:
                self.state['previous_login'] = old
                self._save()
            self.guard.change({**old, 'action': 'settings', 'enabled': True}, client_ip)
            self.guard.tick(ssh)
            ignores = list(dict.fromkeys(['127.0.0.0/8', '::1/128', *self.guard.status()['settings']['ignoreip']]))
            ports = sorted(set(published))
            self.state.update(enabled=True, ignores=ignores, ports=ports, errors=errors,
                              connections={'state': 'pending', 'detail': '正在检测主机防火墙'}, entries=[])
            self.state.setdefault('records', [])
            self._save()
            try:
                self.firewall.apply(ports, ignores, True)
                self.state['connections'] = {'state': 'active' if ports else 'unavailable',
                    'detail': 'TCP 新连接每来源 60 次/秒，突发 120；含 Docker 发布端口；不限制 UDP/QUIC' if ports else '未发现可保护的 80/443 网站入口'}
            except (ValueError, OSError, RuntimeError) as exc:
                self.state['connections'] = {'state': 'unavailable', 'detail': str(exc)}
            self._save()
        # Each entry is independent: an unsupported proxy never prevents login/connection protection.
        for runtime in runtimes:
            entry = {'label': runtime['label'], 'kind': runtime['kind'], 'state': 'unavailable'}
            try:
                if runtime['kind'] == 'caddy':
                    raise ValueError('标准 Caddy 无内置请求限流；已纳入可用的主机连接防护，不改业务配置')
                if runtime['kind'] != 'nginx':
                    raise ValueError('已发现网站端口，但入口软件尚不支持自动请求限流；原配置保留')
                if runtime.get('custom'):
                    raise ValueError('自定义 Nginx 启动参数，暂不自动修改')
                with self.maintenance(), self.lock:
                    existing = next((item for item in self.state['records'] if item['label'] == runtime['label']), None)
                    if existing:
                        self._write_record(existing, True)
                        record = existing
                    else:
                        dump = proxy.command([*runtime['prefix'], 'nginx', '-T'])
                        files, servers = nginx_candidates(runtime, dump, ignores)
                        record = {'label': runtime['label'], 'prefix': runtime['prefix'], 'files': files, 'servers': servers}
                        # A private journal survives interruption; no server file is removed.
                        self.state['records'].append(record)
                        self._save()
                        self._write_record(record, True)
                    entry.update(state='active', detail=f'{record["servers"]} 个站点 · 每 IP 100 请求/秒，突发 200，并发 100；超额返回 429')
            except (ValueError, OSError, RuntimeError) as exc:
                entry['detail'] = str(exc)[:300]
            with self.maintenance(), self.lock:
                self.state['entries'].append(entry)
                self._save()
        with self.maintenance(), self.lock:
            self.state.update(phase='on', checked_at=self.clock(),
                downloads={'state': 'off', 'detail': '下载限速暂未启用，避免影响正常大文件和流式响应；请求限速不等于带宽限制'})
            self._save()

    def _disable(self, client_ip, ssh):
        with self.maintenance(), self.lock:
            errors = []
            for record in self.state.get('records', []):
                try:
                    self._write_record(record, False)
                    for entry in self.state.get('entries', []):
                        if entry['label'] == record['label']:
                            entry.update(state='off', detail='已恢复开启前的网站配置')
                except (ValueError, OSError, RuntimeError) as exc:
                    errors.append(record['label'] + '：' + str(exc)[:200])
            try:
                if self.state.get('ports'):
                    self.firewall.apply([], [], False)
                self.state['connections'] = {'state': 'off', 'detail': '已撤销面板的连接限制'}
            except (ValueError, OSError, RuntimeError) as exc:
                errors.append(str(exc))
            if not errors:
                previous = self.state.get('previous_login')
                if previous:
                    self.guard.change({**previous, 'action': 'settings'}, client_ip)
                    self.guard.tick(ssh)
                self.state.update(enabled=False, phase='off', entries=[], records=[], errors=[], checked_at=self.clock())
                self.state.pop('previous_login', None)
            else:
                self.state.update(phase='partial', errors=errors, checked_at=self.clock())
            self._save()

    def check(self):
        """Read-only drift checks; never silently overwrite a business redeploy."""
        with self.maintenance(), self.lock:
            if not self.state.get('enabled') or (self.worker and self.worker.is_alive() and threading.current_thread() is not self.worker):
                return
            for entry in self.state.get('entries', []):
                if entry['state'] != 'active':
                    continue
                record = next((item for item in self.state.get('records', []) if item['label'] == entry['label']), None)
                try:
                    if not record or any(proxy.read_config(Path(item['path'])) != item['after'] for item in record['files']):
                        raise ValueError('业务更新改变了配置，网站防护需要重新核对；未自动覆盖')
                    proxy.command([*record['prefix'], 'nginx', '-t'])
                except (ValueError, OSError) as exc:
                    entry.update(state='changed', detail=str(exc)[:250])
            # Reconcile only the isolated firewall object, including after host reboot.
            try:
                self.firewall.apply(self.state.get('ports', []), self.state.get('ignores', []), True)
                if self.state.get('ports'):
                    self.state['connections'] = {'state': 'active', 'detail': 'TCP 新连接每来源 60 次/秒，突发 120；含 Docker 发布端口；不限制 UDP/QUIC'}
            except (ValueError, OSError, RuntimeError) as exc:
                self.state['connections'] = {'state': 'unavailable', 'detail': str(exc)[:250]}
            self.state['checked_at'] = self.clock()
            self._save()

    def request_check(self):
        # Proxy validation must not stall SSH log collection or login-ban expiry.
        if not self._published.get('enabled') or self.worker and self.worker.is_alive():
            return
        if not self.lock.acquire(blocking=False):
            return
        try:
            if not self.worker or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._check_work, name='basic-protection-check', daemon=True)
                self.worker.start()
        finally:
            self.lock.release()

    def _check_work(self):
        try:
            self.check()
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            with self.maintenance(), self.lock:
                self.state.update(errors=['定期核对未完成，请刷新确认实际防护状态'], checked_at=self.clock())
                self._save()
