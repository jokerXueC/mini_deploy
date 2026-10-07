"""Reviewed, reversible insertion of a request gateway into an existing proxy."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import nginx_runtime
import request_gateway as gateway
from certificates import CertificateError, atomic_write, trusted_path

MAX_CONFIG = 256 * 1024


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def command(args: list[str], *, content: str | None = None, timeout: int = 20) -> str:
    try:
        result = subprocess.run(args, input=content, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CertificateError(f'{args[0]} 执行失败或超时，请检查服务状态') from exc
    if result.returncode:
        output = result.stderr.lower()
        if 'host not found' in output or 'no such host' in output:
            reason = '后端或网关名称无法解析，请选择与业务、前置代理共有的 Docker 网络'
        elif 'permission denied' in output:
            reason = '配置文件或目录权限不足，请检查挂载路径与文件权限'
        elif 'address already in use' in output or 'port is already allocated' in output:
            reason = '端口已占用，请更换端口'
        else:
            reason = '代理配置校验或运行操作失败；请按页面的诊断指令查看原始错误'
        # Proxy output may contain secrets from other sites; return classified diagnostics only.
        raise CertificateError(reason)
    if len(result.stdout) > 2 * 1024 * 1024:
        raise CertificateError('代理配置输出过大，请拆分配置后使用高级接入')
    return result.stdout


@dataclass
class Node:
    words: list[str]
    start: int
    end: int = 0
    body: int = 0
    close: int = 0
    children: list[Node] = field(default_factory=list)


TOKEN = re.compile(r'\{\$[^}\n]*\}|\$\{[^}\n]*\}|"(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27|`[^`]*`|#[^\n]*|[ \t\r]+|\n|[{};]|[^\s{};#"\x27`]+')


def parse(text: str, kind: str) -> list[Node]:
    """Locate conservative source spans; the real proxy still validates the complete config."""
    root = Node([], 0)
    stack, words, start, previous = [root], [], 0, 0
    for match in TOKEN.finditer(text):
        if match.start() != previous:
            raise CertificateError('配置使用了暂不支持的语法，请查看手动接入指引')
        previous = match.end()
        token = match.group()
        if token.startswith('#') or token in (' ', '\t', '\r') or (token.isspace() and token != '\n'):
            continue
        if token in (';', '\n'):
            if token == ';' or kind == 'caddy':
                if words:
                    stack[-1].children.append(Node(words, start, match.start() if token == '\n' else match.end()))
                    words = []
            continue
        if token == '{':
            node = Node(words, start if words else match.start(), body=match.end())
            stack[-1].children.append(node)
            stack.append(node)
            words = []
        elif token == '}':
            if words or len(stack) == 1:
                raise CertificateError('配置块无法可靠识别，请检查换行和分号')
            node = stack.pop()
            node.close, node.end = match.start(), match.end()
        else:
            if not words:
                start = match.start()
            words.append(token)
    if words or len(stack) != 1 or previous != len(text):
        raise CertificateError('配置结构不完整，未作修改')
    return root.children


def walk(nodes: list[Node]):
    for node in nodes:
        yield node
        yield from walk(node.children)


def routes(text: str, kind: str) -> list[dict[str, Any]]:
    nodes = parse(text, kind)
    result = []
    imported = kind == 'caddy' and any(n.words[:1] == ['import'] for n in walk(nodes))
    sites = nodes if kind == 'caddy' else [n for n in walk(nodes) if n.words == ['server']]
    for site in sites:
        if not site.body or (kind == 'caddy' and (not site.words or site.words[0].startswith('('))):
            continue
        names = site.words if kind == 'caddy' else next((n.words[1:] for n in site.children if n.words[:1] == ['server_name']), [])
        domain = names[0] if len(names) == 1 else ''
        domain = re.sub(r'^https?://', '', domain) if kind == 'caddy' else domain
        if not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?(?::[0-9]{1,5})?', domain):
            continue
        all_nodes = list(walk(site.children))
        proxies = [n for n in all_nodes if n.words[:1] == (['reverse_proxy'] if kind == 'caddy' else ['proxy_pass'])]
        if not proxies:
            proxies = [None]
        for proxy in proxies:
            reason, upstream = '', ''
            route_kind = 'proxy' if proxy else 'static'
            if imported:
                reason = 'Caddyfile 使用 import；请先确认导入文件和实际生效规则'
            elif proxy:
                if proxy.body or len(proxy.words) != 2:
                    reason = '此规则含多上游、匹配器或高级转发选项，暂不自动改写'
                else:
                    raw = proxy.words[1]
                    upstream = raw if '://' in raw else 'http://' + raw
                    try:
                        gateway.normalize({'key': 'check', 'upstream': upstream, 'network': 'host'})
                    except ValueError:
                        reason = '只自动接入无路径、无变量的单个 HTTP 上游'
                if not reason and kind == 'nginx':
                    scopes = [n for n in all_nodes if n.body and n.start < proxy.start < n.end]
                    scope = scopes[-1] if scopes else site
                    headers = {n.words[1].lower(): n.words[2] for n in scope.children
                               if n.words[:1] == ['proxy_set_header'] and len(n.words) == 3}
                    if (headers.get('host') not in ('$host', '$http_host') or headers.get('x-forwarded-proto') != '$scheme'
                            or headers.get('x-forwarded-for') not in ('$proxy_add_x_forwarded_for', '$remote_addr')):
                        reason = '此规则缺少明确的 Host/转发头，或依赖继承配置；请先由维护者核对请求头，避免接入后改变业务行为'
            elif kind != 'caddy':
                reason = 'Nginx 静态站点暂不自动迁移，请按手动指引接入'
            else:
                allowed = {'encode', 'header', 'root', 'file_server', 'handle', 'handle_path', 'route',
                           'respond', 'try_files', 'rewrite', 'uri', 'path', 'not', 'hide', 'index',
                           'precompressed', 'browse', 'status', 'canonical_uris'}
                # Header fields and matcher definitions are data, not executable routing directives.
                def supported(children, context=''):
                    for node in children:
                        if not node.words:
                            return False
                        word = node.words[0]
                        if word.startswith('@'):
                            if node.body or len(node.words) < 3 or node.words[1] != 'path':
                                return False
                        elif context != 'header' and word not in allowed:
                            return False
                        if not supported(node.children, word):
                            return False
                    return True
                global_safe = {'email', 'admin', 'auto_https', 'acme_ca', 'acme_ca_root', 'local_certs', 'skip_install_trust', 'debug'}
                advanced_global = any(n.words and n.words[0] not in global_safe for group in nodes if not group.words for n in group.children)
                placeholders = re.search(r'\{[^{}\s]+\}', text[site.body:site.close])
                if (advanced_global or placeholders or not any(n.words[:1] == ['file_server'] for n in all_nodes)
                        or not supported(site.children)):
                    reason = '静态站点包含高级配置，暂不自动拆分内部入口'
            if proxy and 'mini-gateway-' in upstream:
                reason = '该规则已指向网关，请使用原入口的接入状态或撤销操作'
            context = [n.words for n in all_nodes if proxy and n.body and n.start < proxy.start < n.end]
            label = ' / '.join(' '.join(w) for w in context) or ('静态网站' if not proxy else '全部请求')
            result.append({'id': digest(f'{site.start}:{proxy.start if proxy else -1}:{text[site.start:site.end]}')[:24],
                           'site': domain, 'label': label, 'kind': route_kind, 'upstream': upstream,
                           'reason': reason, 'supported': not reason, '_site': site, '_proxy': proxy})
    return result


def public_route(route):
    return {key: value for key, value in route.items() if not key.startswith('_')}


def read_config(path: Path) -> str:
    trusted_path(path)
    if not path.is_file() or path.stat().st_size > MAX_CONFIG:
        raise CertificateError('配置文件不存在或超过 256 KiB，请核对实际文件路径')
    with path.open(encoding='utf-8', newline='') as stream:
        return stream.read()


def read_record(path: Path) -> dict:
    trusted_path(path)
    if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise CertificateError('接入记录不存在或异常，请重新检测')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise CertificateError('接入记录格式异常')
    return value


def resolve(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get('kind') not in ('caddy', 'nginx') or raw.get('mode') not in ('docker', 'local'):
        raise CertificateError('请选择代理类型和运行位置')
    kind, mode = raw['kind'], raw['mode']
    config_path = raw.get('config_path') or ('/etc/caddy/Caddyfile' if kind == 'caddy' else '/etc/nginx/conf.d/default.conf')
    if (not isinstance(config_path, str) or len(config_path) > 512 or not config_path.startswith('/') or
            any(c in config_path for c in '\n\r\x00') or '..' in PurePosixPath(config_path).parts):
        raise CertificateError('填写代理实际使用的绝对配置文件路径')
    source = {'kind': kind, 'mode': mode, 'config_path': config_path, 'container': '', 'networks': ['host']}
    if mode == 'local':
        if not shutil.which(kind):
            raise CertificateError(f'未找到本机 {kind}，请确认是否运行在 Docker 中')
        command(['systemctl', 'is-active', kind])
        startup = command(['systemctl', 'show', kind, '--property=ExecStart', '--value'])
        if kind == 'caddy':
            configured = re.findall(r'--config(?:=|\s+)(/[^\s;"\x27]+)', startup)
            if configured != [config_path] or '--resume' in startup:
                raise CertificateError('配置路径与本机 Caddy 服务启动参数不一致，请查询 systemctl 的 ExecStart')
            if '{$' in read_config(Path(config_path)):
                raise CertificateError('本机 Caddy 配置依赖服务环境变量，当前不能复现该环境校验；请按手动指引由维护者接入')
        else:
            if re.search(r'(?:^|\s)-(?:c|p)(?:\s|/)', startup):
                raise CertificateError('本机 Nginx 服务使用自定义主配置或前缀，请先核对实际启动参数')
            dump = command(['nginx', '-T'])
            if f'# configuration file {config_path}:' not in dump:
                raise CertificateError('该文件不在 nginx -T 输出中，请填写实际生效的站点文件')
        source['path'] = config_path
    else:
        name = raw.get('container', '')
        if not isinstance(name, str) or not gateway.DOCKER_NAME.fullmatch(name):
            raise CertificateError('填写 docker ps 的 NAMES 列中的代理容器名称')
        nginx_runtime.require_local_docker()
        items = json.loads(command(['docker', 'inspect', '--type', 'container', name]))
        item = items[0]
        if item['State']['Status'] != 'running' or not re.fullmatch(r'[a-f0-9]{64}', item['Id']):
            raise CertificateError('前置代理容器必须处于运行状态')
        args = (item.get('Config', {}).get('Entrypoint') or []) + (item.get('Config', {}).get('Cmd') or [])
        if '--resume' in args:
            raise CertificateError('Caddy 使用 --resume，文件可能不是运行配置，请先核对启动方式')
        if kind == 'caddy':
            if '--config' not in args or args.index('--config') + 1 >= len(args) or args[args.index('--config') + 1] != config_path:
                raise CertificateError('配置路径与 Caddy 容器启动参数不一致，请按查询指令核对')
            command(['docker', 'exec', item['Id'], 'caddy', 'version'])
        else:
            if '-c' in args:
                raise CertificateError('容器使用自定义 Nginx 主配置启动参数，请按手动指引核对')
            dump = command(['docker', 'exec', item['Id'], 'nginx', '-T'])
            if f'# configuration file {config_path}:' not in dump:
                raise CertificateError('该文件不在容器 nginx -T 输出中，请填写实际生效的站点文件')
        mounts = sorted(item.get('Mounts', []), key=lambda m: len(m.get('Destination', '')), reverse=True)
        for mount in mounts:
            dest = mount.get('Destination', '').rstrip('/')
            if mount.get('Type') != 'bind' or not dest:
                continue
            if config_path == dest or config_path.startswith(dest + '/'):
                relative = config_path[len(dest):].lstrip('/')
                source['path'] = str(Path(mount['Source']) / relative) if relative else mount['Source']
                break
        if 'path' not in source:
            raise CertificateError('配置没有 bind 挂载到宿主机；请查询 Mounts，容器内临时文件不自动修改')
        names = list(item.get('NetworkSettings', {}).get('Networks', {}))
        source.update(container=name, container_id=item['Id'], networks=[n for n in names if n not in ('bridge', 'none')])
        if not source['networks']:
            raise CertificateError('前置代理需要 host 或自定义 Docker 网络，默认 bridge 不支持服务名解析')
    read_config(Path(source['path']))
    return source


def help_items(source: dict | None = None) -> list[dict[str, str]]:
    source = source or {}
    name = source.get('container', '')
    target = shlex.quote(name) if isinstance(name, str) and gateway.DOCKER_NAME.fullmatch(name) else '替换为容器名称'
    return [
        {'title': '不知道代理在哪里运行', 'command': "docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'\nsystemctl is-active caddy nginx",
         'fill': 'Docker 输出的 NAMES 填入容器名称；systemctl 对应项为 active 才表示本机服务正在运行。'},
        {'title': '查配置挂载与容器内路径', 'command': f"docker inspect {target} --format '{{{{range .Mounts}}}}{{{{println .Source \"->\" .Destination}}}}{{{{end}}}}'",
         'mode': 'docker',
         'fill': '箭头左边是宿主机路径，右边是容器内路径。上方配置路径填写右边，例如 /etc/caddy/Caddyfile；挂载目录时还需补上文件名。'},
        {'title': '查 Caddy 使用哪个配置文件', 'command': f"docker inspect {target} --format '{{{{json .Config.Cmd}}}}'",
         'mode': 'docker', 'kind': 'caddy',
         'fill': '--config 后面的路径填入配置路径。若使用 --resume 或配置保存在镜像内，请先由维护者确认配置来源。'},
        {'title': '查 Docker 网络', 'command': f"docker inspect {target} --format '{{{{json .NetworkSettings.Networks}}}}'",
         'mode': 'docker',
         'fill': '选择代理和业务容器共有的网络名称；Docker 服务名不能在“服务器本机”网络中使用。'},
        {'title': '查 Nginx 域名对应哪个文件', 'command': (f'docker exec {target} nginx -T' if source.get('mode') != 'local' else 'sudo nginx -T') + " 2>&1 | awk '/^# configuration file / {file=$0} /^[ \t]*server_name[ \t]/ {print file; print}'",
         'kind': 'nginx',
         'local_command': "sudo nginx -T 2>&1 | awk '/^# configuration file / {file=$0} /^[ \t]*server_name[ \t]/ {print file; print}'",
         'fill': '找到要监控的域名，将其上方 configuration file 后的文件路径填入配置路径，去掉末尾冒号。'},
        {'title': '查本机服务配置路径', 'command': 'systemctl show caddy nginx --property=ExecStart --value',
         'mode': 'local',
         'fill': 'Caddy 的 --config 后面是配置路径。本机 Nginx 自定义 -c/-p、Caddy --resume 或环境变量配置需要维护者确认。'},
        {'title': '查看代理运行错误', 'command': (f'docker logs --tail 80 {target}' if source.get('mode') != 'local'
                                           else f"sudo journalctl -u {source.get('kind', 'caddy')} -n 80 --no-pager"),
         'local_command': 'sudo journalctl -u 替换为代理类型 -n 80 --no-pager',
         'fill': '用于排查应用或重启失败，不需要填入表单；对外提供日志前请遮盖密码、令牌等敏感信息。'},
    ]


def discover() -> dict[str, Any]:
    sources, errors = [], []
    if shutil.which('caddy'):
        sources.append({'kind': 'caddy', 'mode': 'local', 'container': '', 'config_path': '/etc/caddy/Caddyfile', 'label': '本机 Caddy'})
    if shutil.which('nginx'):
        sources.append({'kind': 'nginx', 'mode': 'local', 'container': '', 'config_path': '/etc/nginx/conf.d/default.conf', 'label': '本机 Nginx'})
    try:
        nginx_runtime.require_local_docker()
        rows = command(['docker', 'ps', '--format', '{{json .}}'])
        for line in rows.splitlines()[:40]:
            item = json.loads(line)
            name = item.get('Names', '')
            if name.startswith('mini-gateway-'):
                continue
            tag = (name + ' ' + item.get('Image', '')).lower()
            kind = 'caddy' if 'caddy' in tag else 'nginx' if 'nginx' in tag else ''
            if kind and gateway.DOCKER_NAME.fullmatch(name):
                sources.append({'kind': kind, 'mode': 'docker', 'container': name, 'label': f'Docker {kind} · {name}',
                                'config_path': '/etc/caddy/Caddyfile' if kind == 'caddy' else '/etc/nginx/conf.d/default.conf'})
    except (ValueError, OSError) as exc:
        errors.append(str(exc))
    return {'sources': sources, 'errors': errors, 'help': help_items()}


class Connections:
    def __init__(self, data_home: Path):
        self.root = data_home / 'gateway-connections'
        self.gateways = gateway.Store(data_home)

    def directory(self, key: str) -> Path:
        self.gateways.directory(key)
        path = self.root / key
        trusted_path(path)
        return path

    def status(self, key: str) -> dict:
        path = self.directory(key) / 'connection.json'
        if not path.exists():
            return {'state': 'not_connected'}
        trusted_path(path)
        data = read_record(path)
        return {k: data[k] for k in ('state', 'site', 'kind', 'source', 'token')}

    def inspect(self, raw: dict) -> dict:
        source = resolve(raw)
        text = read_config(Path(source['path']))
        return {'source': source, 'revision': digest(text), 'routes': [public_route(r) for r in routes(text, source['kind'])],
                'networks': source['networks'], 'help': help_items(source)}

    def preview(self, data: dict) -> dict:
        source = resolve(data.get('source'))
        text = read_config(Path(source['path']))
        if data.get('source_revision') != digest(text):
            raise CertificateError('代理配置已变化，请重新检测网站')
        route = next((r for r in routes(text, source['kind']) if r['id'] == data.get('route_id')), None)
        if not route or not route['supported']:
            raise CertificateError(route['reason'] if route else '请选择本次检测出的站点规则')
        key = data.get('key', '')
        directory = self.directory(key)
        if self.status(key)['state'] != 'not_connected':
            raise CertificateError('此网关已有接入或待恢复操作，请先撤销接入')
        for record in self.root.glob('*/connection.json') if self.root.exists() else []:
            existing = read_record(record)
            if existing['source']['path'] == source['path'] and existing['site'] == route['site']:
                raise CertificateError('此站点已有接入记录，请先撤销原接入；同一文件中的其他站点可独立接入')
        network = data.get('network', '')
        if network not in source['networks']:
            raise CertificateError('请选择前置代理已加入的网络')
        if network != 'host':
            details = json.loads(command(['docker', 'network', 'inspect', network]))[0]
            if details.get('Driver') != 'bridge':
                raise CertificateError('当前只自动配置 host 或自定义 bridge 网络')
        site, proxy = route['_site'], route['_proxy']
        before = text[site.start:site.end]
        extra = ''
        upstream = route['upstream']
        if route['kind'] == 'static':
            internal = data.get('internal_port', 18081)
            if type(internal) is not int or not 1024 <= internal <= 65535 or internal in (6868, data.get('port', 18080), 2019):
                raise CertificateError('静态站点内部端口应为未占用的 1024-65535 端口，且与网关、面板及管理端口不同')
            if re.search(rf':{internal}\b', text):
                raise CertificateError('静态站点内部端口已在配置中使用，请更换')
            host = '127.0.0.1' if network == 'host' else source['container']
            upstream = f'http://{host}:{internal}'
            bind = '127.0.0.1' if network == 'host' else '0.0.0.0'
            extra = f'\n\n# mini-deploy static backend: {key}\nhttp://:{internal} {{\n    bind {bind}\n' + text[site.body:site.close] + '\n}\n'
        spec = gateway.normalize({'key': key, 'name': data.get('name') or route['site'], 'port': data.get('port', 18080),
                                  'network': network, 'upstream': upstream, 'image': data.get('image', gateway.IMAGE),
                                  'bind': '127.0.0.1', 'trust_proxy': True})
        probe_path = data.get('probe_path', '/')
        if (not isinstance(probe_path, str) or not re.fullmatch(r'/[!-~]{0,511}', probe_path)
                or any(c in probe_path for c in ('?', '#')) or probe_path.startswith('//')):
            raise CertificateError('检测路径应以 / 开头，不含查询参数；例如 /cloud/health')
        target = f'127.0.0.1:{spec["port"]}' if network == 'host' else f'mini-gateway-{key}:10000'
        if proxy:
            replacement = ('reverse_proxy ' if source['kind'] == 'caddy' else 'proxy_pass http://') + target
            if source['kind'] == 'nginx':
                # A trailing slash controls location-prefix stripping in Nginx.
                replacement += '/;' if proxy.words[1].endswith('/') else ';'
            after = before[:proxy.start - site.start] + replacement + before[proxy.end - site.start:]
        else:
            after = f'{site.words[0]} {{\n    reverse_proxy {target}\n}}'
        candidate = text[:site.start] + after + text[site.end:] + extra
        entry_path = self.gateways.directory(key) / 'entry.json'
        old = self.gateways.read(key) if entry_path.exists() else None
        if old and any(old['spec'][k] != spec[k] for k in ('upstream', 'network', 'port', 'bind', 'image')):
            raise CertificateError('该标识已有不同的网关配置，请使用新的入口标识，或先在网关管理中核对配置')
        plan = {'token': secrets.token_hex(16), 'created': time.time(), 'source': source, 'site': route['site'],
                'kind': route['kind'], 'before': before, 'after': after, 'extra': extra,
                'source_revision': digest(text), 'candidate': candidate, 'spec': spec,
                'probe_path': probe_path,
                'gateway_revision': gateway.revision(old['spec']) if old else ''}
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write(directory / 'plan.json', json.dumps(plan, ensure_ascii=False))
        return {'token': plan['token'], 'gateway': spec, 'site': route['site'], 'kind': route['kind'],
                'before_rule': upstream if proxy else f'{route["site"]} → Caddy 静态文件',
                'after_rule': f'{route["site"]} → {target} → {upstream}',
                'notice': '确认后自动启动网关、备份配置、校验并应用。Caddy 将重启，已有连接可能短暂中断；Nginx 使用重载。'}

    def probe(self, plan: dict) -> None:
        for attempt in range(4):
            connection = http.client.HTTPConnection('127.0.0.1', plan['spec']['port'], timeout=3)
            try:
                connection.request('GET', plan['probe_path'], headers={'Host': plan['site'], 'X-Forwarded-Proto': 'https'})
                response = connection.getresponse()
                if response.status not in (502, 504):
                    return
            except (OSError, http.client.HTTPException):
                pass
            finally:
                connection.close()
            if attempt < 3:
                time.sleep(0.5)
        raise CertificateError('网关尚不能转发到后端，请核对 Docker 网络、服务名和端口；原入口将恢复')

    def validate(self, source: dict, text: str) -> None:
        prefix = ['docker', 'exec', '-i', source['container_id']] if source['mode'] == 'docker' else []
        if source['kind'] == 'caddy':
            command([*prefix, 'caddy', 'validate', '--config', '/dev/stdin', '--adapter', 'caddyfile'], content=text)
        else:
            command([*prefix, 'nginx', '-t'])

    def activate(self, source: dict) -> None:
        if source['mode'] == 'docker':
            if source['kind'] == 'caddy':
                command(['docker', 'restart', source['container_id']], timeout=45)
            else:
                command(['docker', 'exec', source['container_id'], 'nginx', '-s', 'reload'])
        else:
            command(['systemctl', 'restart' if source['kind'] == 'caddy' else 'reload', source['kind']], timeout=45)

    @staticmethod
    def write_live(path: Path, expected: str, updated: str) -> None:
        if read_config(path) != expected:
            raise CertificateError('配置已被其他操作修改，已停止覆盖，请重新检测')
        # Preserve the inode for single-file Docker bind mounts. A private backup precedes this write.
        with path.open('r+', encoding='utf-8', newline='') as stream:
            if stream.read() != expected:
                raise CertificateError('配置在写入前发生变化，未修改')
            stream.seek(0)
            stream.write(updated)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())

    def apply(self, data: dict) -> dict:
        directory = self.directory(data.get('key', ''))
        plan = read_record(directory / 'plan.json')
        if data.get('token') != plan['token'] or time.time() - plan['created'] > 900:
            raise CertificateError('接入预览已过期，请重新生成')
        if self.status(plan['spec']['key'])['state'] != 'not_connected':
            raise CertificateError('此入口已有接入或待恢复操作，请刷新状态')
        source = resolve(plan['source'])
        if source != plan['source']:
            raise CertificateError('代理容器或挂载已变化，请重新检测')
        path = Path(source['path'])
        original = read_config(path)
        if digest(original) != plan['source_revision']:
            raise CertificateError('配置已变化，原预览失效，请重新检测')
        if source['kind'] == 'caddy':
            self.validate(source, plan['candidate'])
        self.gateways.save(plan['spec'], plan['gateway_revision'])
        self.gateways.start(self.gateways.read(plan['spec']['key']))
        if plan['kind'] == 'proxy':
            self.probe(plan)
        record = {**plan, 'state': 'applying'}
        atomic_write(directory / 'before.conf', original)
        atomic_write(directory / 'connection.json', json.dumps(record, ensure_ascii=False))
        try:
            self.write_live(path, original, plan['candidate'])
            self.validate(source, plan['candidate'])
            self.activate(source)
            self.probe(plan)
            record['state'] = 'connected'
            atomic_write(directory / 'connection.json', json.dumps(record, ensure_ascii=False))
        except (ValueError, OSError) as exc:
            try:
                current = read_config(path)
                if current != original:
                    self.write_live(path, plan['candidate'], original)
                self.validate(source, original)
                self.activate(source)
                (directory / 'connection.json').unlink()
            except (ValueError, OSError) as rollback:
                record['state'] = 'recovery_required'
                atomic_write(directory / 'connection.json', json.dumps(record, ensure_ascii=False))
                raise CertificateError(f'应用失败，自动恢复未完成。请尝试撤销接入；如配置或容器已被外部修改，请由维护者核对 {directory / "before.conf"} 后恢复。网关不会被停止') from rollback
            raise CertificateError(f'接入失败，原代理配置已恢复。{exc}') from exc
        return {'connection': self.status(plan['spec']['key'])}

    def disconnect(self, data: dict) -> dict:
        directory = self.directory(data.get('key', ''))
        record = read_record(directory / 'connection.json')
        if data.get('token') != record['token']:
            raise CertificateError('接入状态已变化，请刷新后再撤销')
        source = resolve(record['source'])
        if source != record['source']:
            raise CertificateError('代理容器或挂载已变化，请人工核对后恢复备份')
        path = Path(source['path'])
        current = read_config(path)
        if current.count(record['after']) == 1:
            restored = current.replace(record['after'], record['before'], 1)
            if record['extra']:
                if restored.count(record['extra']) != 1:
                    raise CertificateError('静态内部入口已变化，不会覆盖外部修改')
                restored = restored.replace(record['extra'], '', 1)
        elif current.count(record['before']) == 1 and not (record['extra'] and record['extra'] in current):
            restored = current
        else:
            raise CertificateError('接入规则已被外部修改，请核对备份；其他站点不会被覆盖')
        atomic_write(directory / 'disconnect-before.conf', current)
        previous_state = record['state']
        record['state'] = 'disconnecting'
        atomic_write(directory / 'connection.json', json.dumps(record, ensure_ascii=False))
        try:
            if source['kind'] == 'caddy':
                self.validate(source, restored)
            self.write_live(path, current, restored)
            self.validate(source, restored)
            self.activate(source)
            (directory / 'connection.json').unlink()
        except (ValueError, OSError) as exc:
            try:
                if read_config(path) != current:
                    self.write_live(path, restored, current)
                self.validate(source, current)
                self.activate(source)
                record['state'] = previous_state
                atomic_write(directory / 'connection.json', json.dumps(record, ensure_ascii=False))
            except (ValueError, OSError) as rollback:
                record['state'] = 'recovery_required'
                atomic_write(directory / 'connection.json', json.dumps(record, ensure_ascii=False))
                raise CertificateError(f'撤销及自动恢复未完成，网关保留运行。请核对代理状态和 {directory / "disconnect-before.conf"}，再重试撤销') from rollback
            raise CertificateError('撤销失败，已恢复撤销前配置，网关仍保留运行。请检查代理日志后重试') from exc
        return {'connection': {'state': 'not_connected'}}

    def operate(self, data: dict) -> dict:
        with gateway.LOCK:
            action = data.get('action')
            if action == 'inspect':
                return self.inspect(data.get('source'))
            if action == 'preview':
                return self.preview(data)
            if data.get('confirmed') is not True:
                raise CertificateError('请确认接入操作')
            if action == 'apply':
                return self.apply(data)
            if action == 'disconnect':
                return self.disconnect(data)
            raise CertificateError('未知接入操作')
