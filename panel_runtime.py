"""Host transport for shared diagnostic tools and discovered panel evidence."""
from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import PurePosixPath
import re
import tarfile

import nginx_runtime
import panel_code
import server_operations
import terminal_tools

TEXT = {'type': 'string', 'maxLength': 200}
PAGE = {'type': 'integer', 'minimum': 1, 'maximum': 100}
WINDOW = {'since': terminal_tools.LOG_TIME, 'until': terminal_tools.LOG_TIME}
DEFINITIONS = [
    ('get_host_inventory', '读取本机进程、监听端口、服务及日志来源，不读取进程命令参数',
     {'sort': {'type': 'string', 'enum': ['cpu', 'memory']}}, []),
    ('list_log_sources', '列出当前可读取的系统服务、容器和常见日志文件来源；先发现再读取', {}, []),
    ('read_logs', '读取已发现日志来源，可按故障时间、字面关键词和级别筛选；不要求已选异常',
     {'source': TEXT, **WINDOW, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 500},
      'keyword': {'type': 'string', 'maxLength': 160}, 'level': {'type': 'string', 'enum': ['all', 'error', 'warning']}}, ['source']),
    ('list_proxy_configs', '列出已发现的本机及 Docker 代理配置，含来源、容器 ID、路径和扫描时间', {}, []),
    ('read_proxy_config', '按发现的配置 ID 分页读取代理实际文件，不改配置、不重载；不能据此证明故障时配置相同',
     {'id': TEXT, 'start_line': {'type': 'integer', 'minimum': 1, 'maximum': 1000000},
      'lines': terminal_tools.LINES}, ['id']),
    ('list_request_sources', '列出已采集请求来源及采集状态，供按故障时间查询请求明细', {}, []),
    ('get_request_records', '分页查询故障时间内每条请求的时间、状态码、耗时、上游和请求 ID；5xx 用 status=5xx',
     {'source': TEXT, **WINDOW, 'status': {'type': 'string', 'enum': ['all', '4xx', '5xx']},
      'search': TEXT, 'limit': PAGE, 'cursor': {'type': 'object', 'properties': {
          'ts': {'type': 'number'}, 'id': {'type': 'integer'}}, 'required': ['ts', 'id'], 'additionalProperties': False}},
     ['source', 'since', 'until']),
    ('get_panel_events', '读取指定时间的监控生命周期事件和面板操作记录；不覆盖外部部署工具的操作记录',
     {**WINDOW, 'limit': PAGE, 'offset': {'type': 'integer', 'minimum': 0, 'maximum': 100000}}, ['since', 'until']),
]


class Runtime:
    def __init__(self, *, inventory, logs, requests, discovery, monitor, audit,
                 runner=server_operations.run, local_docker=nginx_runtime.require_local_docker):
        self.inventory, self.logs, self.requests = inventory, logs, requests
        self.discovery, self.monitor, self.audit = discovery, monitor, audit
        self.runner, self.local_docker = runner, local_docker

    def schemas(self):
        shared = [s for s in terminal_tools.schemas() if s['function']['name'] in terminal_tools.LOCAL_READ_TOOLS]
        return shared + [{'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': fields, 'required': required, 'additionalProperties': False}}}
            for name, description, fields, required in DEFINITIONS]

    def plan(self, name, arguments):
        if name in terminal_tools.LOCAL_READ_TOOLS:
            return terminal_tools.plan(name, arguments)
        definition = next((d for d in DEFINITIONS if d[0] == name), None)
        if not definition:
            raise terminal_tools.ToolError('未开放该操作；本机诊断不执行任意命令或修改文件')
        _, title, fields, required = definition
        if not isinstance(arguments, dict) or set(arguments) - set(fields) or set(required) - set(arguments):
            raise terminal_tools.ToolError('诊断参数不完整或包含未支持字段')
        args = dict(arguments)
        for key, value in args.items():
            if key in WINDOW:
                continue
            spec = fields[key]
            if (spec['type'] == 'string' and (not isinstance(value, str) or len(value) > spec.get('maxLength', 200)
                                            or any(ord(c) < 32 for c in value))
                    or spec['type'] == 'integer' and (type(value) is not int or not spec['minimum'] <= value <= spec['maximum'])
                    or spec['type'] == 'object' and not isinstance(value, dict)
                    or 'enum' in spec and value not in spec['enum']):
                raise terminal_tools.ToolError(f'{key} 参数无效')
        args.update(terminal_tools.log_window(args))
        return {'name': name, 'arguments': args, 'title': title, 'write': False,
                'command': name + ' ' + json.dumps(args, ensure_ascii=False), 'reason': '读取当前面板主机的运维证据'}

    @staticmethod
    def check(cancel):
        if cancel.is_set():
            raise RuntimeError('诊断已停止')

    def command(self, argv, cancel, **options):
        self.check(cancel)
        if argv[0] == 'docker':
            self.local_docker()
        code, text, truncated = self.runner(argv, timeout=8, **options)
        self.check(cancel)
        if code and not truncated:
            error = text[-1000:].decode('utf-8', 'replace') if isinstance(text, bytes) else text[-1000:]
            raise terminal_tools.ToolError('本机诊断命令读取失败：' + server_operations.redact(error))
        return text, truncated

    def configurations(self):
        discovery = self.discovery()
        with discovery.lock:
            rows, scanned, running, issues = copy.deepcopy(discovery.rows), discovery.last_scan, discovery.running, list(discovery.issues)
        sources = {}
        for row in rows:
            path, source = row.get('config_path'), row.get('target') or {}
            if not path or source.get('kind') not in {'nginx', 'caddy'}:
                continue
            key = hashlib.sha256(json.dumps([source['kind'], source.get('container_id', ''), path]).encode()).hexdigest()[:24]
            sources[key] = {'id': key, 'path': path, 'kind': source['kind'], 'location': source['location'],
                            'container_id': source.get('container_id', ''), 'active_at_scan': bool(source.get('active'))}
        if not scanned and not running:
            discovery.request_scan()
            running = True
        return {'items': list(sources.values()), 'scanned_at': scanned, 'scan_running': running, 'issues': issues}

    def config(self, args, cancel):
        catalog = self.configurations()
        item = next((p for p in catalog['items'] if p['id'] == args['id']), None)
        if not item:
            raise terminal_tools.ToolError('配置来源不存在或已变化，请重新列出代理配置')
        path = PurePosixPath(item['path'])
        if (not path.is_absolute() or '..' in path.parts or not panel_code.source_file(path.name)
                or any(panel_code.hidden(part) for part in path.parts[1:])
                or any(ord(c) < 32 for c in str(path))):
            raise terminal_tools.ToolError('发现的配置路径不安全，不能读取')
        start, count = args.get('start_line', 1), args.get('lines', 100)
        if item['container_id']:
            if not re.fullmatch('[a-f0-9]{64}', item['container_id']):
                raise terminal_tools.ToolError('配置容器编号无效')
            # Docker returns a tar snapshot; never extract paths or follow archived links.
            raw, limited = self.command(['docker', 'cp', item['container_id'] + ':' + str(path), '-'], cancel,
                                        max_bytes=panel_code.MAX_FILE + 65536, binary=True)
            if limited:
                raise terminal_tools.ToolError('代理配置超过 2 MiB，不能把截断文件作为完整配置分析')
            try:
                with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as archive:
                    member = archive.next()
                    if (member is None or not member.isfile() or member.name != path.name
                            or member.size > panel_code.MAX_FILE or member.issparse()):
                        raise ValueError('不是普通配置文件或文件过大')
                    content = archive.extractfile(member).read(panel_code.MAX_FILE + 1)
                    text = content.decode('utf-8-sig')
                    if archive.next() is not None or '\0' in text:
                        raise ValueError('不是单个文本配置文件')
            except (tarfile.TarError, ValueError, OSError) as exc:
                raise terminal_tools.ToolError('无法读取普通 UTF-8 代理配置，不读取链接或特殊文件') from exc
            # Redact before pagination, including multi-line private keys.
            text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|\Z)',
                          lambda m: '[已隐藏私钥]' + '\n' * m[0].count('\n'), text, flags=re.S)
            lines = [server_operations.redact(line) for line in text.splitlines()]
            chosen, size = [], 0
            for number in range(start, min(len(lines) + 1, start + count)):
                line = lines[number - 1][:12000]
                if size + len(line.encode()) > 48000:
                    break
                chosen.append({'line': number, 'text': line, 'line_truncated': len(line) < len(lines[number - 1])})
                size += len(line.encode())
            following = start + len(chosen)
            data = {'lines': chosen, 'next_line': following if following <= len(lines) else None,
                    'truncated': any(line['line_truncated'] for line in chosen), 'total_lines': len(lines),
                    'sha256': hashlib.sha256(content).hexdigest()}
        else:
            root = panel_code.real_directory(str(path.parent))
            reader = panel_code.Reader({'path': str(root), 'id': panel_code._project_identity(root, root.stat())})
            data = reader.read(path.name, start, count, lambda: self.check(cancel))
        return {**item, **data, 'scanned_at': catalog['scanned_at'],
                'notice': '当前发现的配置文件，可能尚未重载，不证明内存中或故障时版本相同。截断时不能判断配置完整；常见凭据已隐藏。'}

    def execute(self, plan, cancel):
        plan = self.plan(plan['name'], plan['arguments'])
        self.check(cancel)
        name, args = plan['name'], plan['arguments']
        if name in terminal_tools.LOCAL_READ_TOOLS:
            text, limited = self.command(plan['argv'], cancel)
            lines = text.splitlines()
            truncated = limited or ('lines' in args and len(lines) >= args['lines'])
            return {'command': plan['command'], 'output': server_operations.redact(text),
                    'truncated': truncated, 'since': args.get('since'), 'until': args.get('until'),
                    'notice': '当前本机只读查询。日志达到上限时请缩小时间窗口继续查询；已轮转日志无法还原。'}
        if name in {'get_host_inventory', 'list_log_sources'}:
            data = self.inventory(sort=args.get('sort', 'cpu'))
            return data if name == 'get_host_inventory' else {k: data.get(k) for k in ('sources', 'checked_at', 'warnings')}
        if name == 'read_logs':
            self.inventory()
            source = args['source']
            if source.startswith('container:'):
                self.local_docker()
            return self.logs(source, since=args.get('since'), until=args.get('until'), limit=args.get('limit', 100),
                             keyword=args.get('keyword', ''), level=args.get('level', 'all'))
        if name == 'list_proxy_configs':
            return self.configurations()
        if name == 'read_proxy_config':
            return self.config(args, cancel)
        if name == 'list_request_sources':
            return {'sources': self.requests().sources()}
        if name == 'get_request_records':
            return self.requests().records(args['source'], since=args['since'], until=args['until'],
                status=args.get('status', 'all'), search=args.get('search', ''), limit=args.get('limit', 50), cursor=args.get('cursor'))
        snapshot, audit = self.monitor(), self.audit()
        def at(event):
            return event.get('at_ts') or server_operations.timestamp(str(event.get('at', ''))) or 0
        offset, limit = args.get('offset', 0), args.get('limit', 100)
        events = sorted([e for e in snapshot.get('events', []) if args['since'] <= at(e) <= args['until']], key=at, reverse=True)
        actions = sorted([e for e in audit if args['since'] <= at(e) <= args['until']], key=at, reverse=True)
        more = len(events) > offset + limit or len(actions) > offset + limit
        return {'events': events[offset:offset + limit], 'actions': actions[offset:offset + limit],
                'next_offset': offset + limit if more else None, 'truncated': more, 'audit_scanned': len(audit),
                'notice': '仅面板保留的事件和最近操作记录；外部发布、人工终端命令和已过期记录可能不在其中，不能据此确定重建操作者。'}
