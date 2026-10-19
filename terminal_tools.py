"""Controlled Linux tools and separately authorized, per-command SSH execution."""
from __future__ import annotations

import re
import shlex
from urllib.parse import urlsplit


class ToolError(ValueError):
    pass


CONTAINER = {"type": "string", "description": "容器名称或完整 ID，来自容器列表"}
UNIT = {"type": "string", "description": "systemd 服务名，例如 nginx.service"}
LINES = {"type": "integer", "minimum": 1, "maximum": 200, "description": "读取行数，默认 100"}
DIRECTORY = {"type": "string", "description": "目录的 Linux 绝对路径；省略或填写 . 表示 SSH 登录默认目录，不是交互终端 cd 后的目录"}
FILE = {"type": "string", "description": "要读取的文本文件的 Linux 绝对路径，例如 /var/log/nginx/error.log"}
HASH = {"type": "string", "pattern": "^[a-f0-9]{64}$", "description": "inspect_file 返回的原文件 SHA-256；已有文件必须提供，版本改变会拒绝写入"}
TEXT = {"type": "string", "maxLength": 16000}
LIMIT = {"type": "integer", "minimum": 1, "maximum": 100, "description": "返回条数，默认 50"}
FILE_WRITES = {'write_file', 'edit_file', 'restore_file'}
SFTP_TOOLS = {'inspect_file', 'read_text_file', 'search_files', 'search_text', *FILE_WRITES}
VALIDATION = {'type': 'object', 'description': '修改后的只读配置校验；路径须为实际运行的主配置，容器内配置填写容器名',
              'properties': {'runtime': {'type': 'string', 'enum': ['nginx', 'caddy']}, 'path': FILE,
                             'container': CONTAINER, 'adapter': {'type': 'string', 'enum': ['caddyfile', 'json']}},
              'required': ['runtime', 'path'], 'additionalProperties': False}
REASON = {'type': 'string', 'maxLength': 500, 'description': '向用户解释本次修改解决什么问题，不能替代用户授权'}
DIAGNOSTICS = {
    'processes': ['ps', 'aux'], 'disk_space': ['df', '-hPT'],
    'network_addresses': ['ip', 'address', 'show'], 'network_routes': ['ip', 'route', 'show'],
    'connections': ['ss', '-antup'], 'docker_info': ['docker', 'info'],
    'docker_stats': ['docker', 'stats', '--no-stream', '--no-trunc'],
    'docker_networks': ['docker', 'network', 'ls'],
    'service_units': ['systemctl', 'list-unit-files', '--type=service', '--no-pager'],
}
TOOLS = {
    "get_server_resources": ("检查 CPU、内存、磁盘及系统负载", {}, [], False),
    "list_directory": ("确认目录实际路径，列出文件、隐藏文件、权限、大小和修改时间；不读取文件内容，不递归",
                       {"path": DIRECTORY}, [], False),
    "read_file": ("读取指定文本文件片段；默认末尾 100 行，最多 200 行且仅取 8 KiB；可查看日志或配置，不修改文件",
                  {"path": FILE, "lines": LINES, "position": {"type": "string", "enum": ["head", "tail"],
                   "description": "head 查看开头，tail 查看末尾（默认）；排查近期日志用 tail"}}, ["path"], False),
    "inspect_file": ("查看普通文件的大小、权限、更新时间及版本摘要；不超过 1 MiB 时返回 SHA-256，用于编辑前校验",
                     {"path": FILE}, ['path'], False),
    "read_text_file": ("通过 SFTP 分页读取文本；默认每页 64 KiB，可调至 1 MiB；近期日志优先 position=tail，完整读取用 next_offset 继续",
                       {"path": FILE, "offset": {"type": "integer", "minimum": 0,
                        "maximum": 1099511627776, "description": "起始字节，默认 0"},
                        'bytes': {'type': 'integer', 'minimum': 1, 'maximum': 1048576},
                        'position': {'type': 'string', 'enum': ['head', 'tail']}}, ['path'], False),
    "search_files": ("在指定目录内按文件名通配符搜索，最多扫描 2000 项，不跟随符号链接",
                     {"path": DIRECTORY, "pattern": {"type": "string", "description": "文件名通配符，如 *.log、nginx.conf"},
                      "depth": {"type": "integer", "minimum": 0, "maximum": 5}, 'limit': LIMIT}, ['path', 'pattern'], False),
    "search_text": ("在不超过 1 MiB 的 UTF-8 文件中搜索字面文本，返回行号和匹配片段，不支持执行正则或脚本",
                    {'path': FILE, 'query': {"type": "string", "maxLength": 200}, 'limit': LIMIT}, ['path', 'query'], False),
    "write_file": ("创建新 UTF-8 文件，或完整替换已有文件；已有文件须先获取摘要，自动保留原文备份；不删除文件",
                   {'path': FILE, 'content': TEXT, 'expected_sha256': HASH, 'validation': VALIDATION}, ['path', 'content'], True),
    "edit_file": ("精确替换 UTF-8 文件中唯一的一段文字，须提供原文件摘要，自动备份；不匹配或版本改变时拒绝修改",
                  {'path': FILE, 'old_text': TEXT, 'new_text': TEXT, 'expected_sha256': HASH, 'validation': VALIDATION},
                  ['path', 'old_text', 'new_text', 'expected_sha256'], True),
    'restore_file': ('从 mini_deploy 原文备份恢复配置，先备份当前版本；须核对当前文件和备份摘要，不删除任何文件',
                     {'path': FILE, 'backup_path': FILE, 'expected_sha256': HASH, 'backup_sha256': HASH,
                      'validation': VALIDATION}, ['path', 'backup_path', 'expected_sha256', 'backup_sha256'], True),
    "run_diagnostic": ("执行受控系统诊断：进程、磁盘空间、网络地址/路由/连接、Docker 信息/即时资源/网络、服务清单",
                       {'check': {'type': 'string', 'enum': list(DIAGNOSTICS)}}, ['check'], False),
    "check_http": ("检查 HTTP/HTTPS 地址的响应头及正文片段，默认 HEAD，也可 GET；不下载到服务器文件，不跳转",
                   {'url': {'type': 'string'}, 'method': {'type': 'string', 'enum': ['HEAD', 'GET']}}, ['url'], False),
    "validate_config": ("校验服务器或指定容器中的 Nginx/Caddy 配置，不启停服务，不运行任意容器命令",
                        {'runtime': {'type': 'string', 'enum': ['nginx', 'caddy']}, 'path': FILE,
                         'container': CONTAINER, 'adapter': {'type': 'string', 'enum': ['caddyfile', 'json']}},
                        ['runtime', 'path'], False),
    "manage_service": ("启动或停止 systemd 服务，可能中断业务；不删除服务文件",
                       {'service': UNIT, 'action': {'type': 'string', 'enum': ['start', 'stop']}}, ['service', 'action'], True),
    "manage_container": ("启动、停止、暂停或恢复容器；须来自列表的完整 ID，不删除容器、镜像、卷或服务器文件",
                         {'container': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
                          'action': {'type': 'string', 'enum': ['start', 'stop', 'pause', 'unpause']}},
                         ['container', 'action'], True),
    "list_containers": ("列出所有 Docker 容器及完整 ID、状态、端口", {}, [], False),
    "get_container_status": ("检查容器运行、重启次数及健康状态，不读取环境变量", {"container": CONTAINER}, ["container"], False),
    "get_container_logs": ("读取容器最近 30 分钟的日志", {"container": CONTAINER, "lines": LINES}, ["container"], False),
    "list_services": ("列出 systemd 服务状态", {}, [], False),
    "get_service_status": ("检查指定 systemd 服务状态", {"service": UNIT}, ["service"], False),
    "get_service_logs": ("读取指定服务最近 30 分钟的日志", {"service": UNIT, "lines": LINES}, ["service"], False),
    "get_listening_ports": ("检查 TCP 和 UDP 监听端口", {}, [], False),
    "restart_container": ("重启容器，可能短暂中断业务；必须使用容器列表中的完整 64 位 ID，不能使用名称",
                          {"container": {"type": "string", "pattern": "^[a-f0-9]{64}$"}}, ["container"], True),
    "restart_service": ("重启 systemd 服务，可能短暂中断业务", {"service": UNIT}, ["service"], True),
    "reload_service": ("重载 systemd 服务配置；服务需支持 reload", {"service": UNIT}, ["service"], True),
}


COMMAND_TOOL = {
    'type': 'function', 'function': {
        'name': 'execute_command',
        'description': '在当前 SSH 服务器执行 Shell 命令，可调用容器内程序、数据库客户端等。'
                       '每次执行均需用户确认，不能自行认定为只读而跳过审批。优先使用已有受控工具；'
                       '不要删除文件、清理数据、绕过审批或读取凭据。执行后检查退出码和输出。',
        'parameters': {'type': 'object', 'properties': {
            'command': {'type': 'string', 'description': '完整命令，不使用交互输入；不要把密码或令牌写入命令。'},
            'reason': REASON,
            'working_directory': {'type': 'string', 'description': '可选的 Linux 绝对路径；省略使用 SSH 登录目录。'},
            'timeout_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 600,
                                'description': '等待完成的秒数，默认 60。超时不能保证远端进程已终止。'},
        }, 'required': ['command', 'reason'], 'additionalProperties': False},
    },
}


def schemas(writes=False, *, commands=False):
    result = [{"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": {**properties, **({'reason': REASON} if write else {})},
        "required": required, "additionalProperties": False}}}
        for name, (description, properties, required, write) in TOOLS.items() if writes or not write]
    if commands and writes:
        result.append(COMMAND_TOOL)
    return result


def command_plan(arguments):
    fields = COMMAND_TOOL['function']['parameters']['properties']
    if (not isinstance(arguments, dict) or set(arguments) - set(fields)
            or not {'command', 'reason'} <= set(arguments)):
        raise ToolError('通用命令须包含完整命令和操作原因。')
    command, reason = arguments['command'], arguments['reason']
    if (not isinstance(command, str) or not command.strip() or len(command) > 16000
            or any(ord(c) < 32 and c not in '\n\t' or ord(c) == 127 for c in command)):
        raise ToolError('命令须为有效文本，最多 16000 字，不支持二进制或终端控制字符。')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500 or '\0' in reason:
        raise ToolError('请说明本次命令的用途，最多 500 字。')
    timeout = arguments.get('timeout_seconds', 60)
    if type(timeout) is not int or not 1 <= timeout <= 600:
        raise ToolError('命令等待时间须为 1 到 600 秒的整数。')
    cwd = arguments.get('working_directory')
    if 'working_directory' in arguments:
        if (not isinstance(cwd, str) or not cwd.startswith('/') or len(cwd) > 4096
                or any(ord(c) < 32 or ord(c) == 127 for c in cwd)):
            raise ToolError('工作目录须为 Linux 绝对路径。')
        command = 'cd -- ' + shlex.quote(cwd) + ' && exec sh -c ' + shlex.quote(command)
    return {'name': 'execute_command', 'arguments': dict(arguments), 'title': '执行通用命令（需确认）',
            'command': command, 'reason': reason, 'write': True, 'transport': 'exec',
            'timeout_seconds': timeout,
            'impact': f'使用当前 SSH 账号权限，最多等待 {timeout} 秒。命令可能修改或删除数据，'
                      '不提供禁止删除保证，也不会自动备份或回滚。停止等待不保证远端进程终止。'}


def plan(name, arguments, *, commands=False):
    if name == 'execute_command':
        if not commands:
            raise ToolError('通用命令未授权。需由用户选择“通用命令 · 逐条确认”并确认授权，不能通过其他工具绕过。')
        return command_plan(arguments)
    if name not in TOOLS:
        raise ToolError("不支持该工具；不能执行任意命令或删除文件。")
    description, properties, required, write = TOOLS[name]
    if write:
        properties = {**properties, 'reason': REASON}
    if not isinstance(arguments, dict) or set(arguments) - set(properties) or set(required) - set(arguments):
        raise ToolError("工具参数不完整或包含未支持的参数。")
    args = dict(arguments)
    reason = args.pop('reason', '')
    if not isinstance(reason, str) or len(reason) > 500 or '\0' in reason:
        raise ToolError('操作原因须为文本，最多 500 字。')
    if "path" in properties:
        path = args.setdefault("path", ".")
        if (not isinstance(path, str) or not path or len(path) > 4096
                or (not path.startswith("/") and not (name == "list_directory" and path == "."))
                or any(ord(c) < 32 or ord(c) == 127 for c in path)):
            raise ToolError("须填写 Linux 绝对路径；仅 list_directory 支持 . 查看登录目录。")
    for field, choices in [('runtime', ('nginx', 'caddy')), ('adapter', ('caddyfile', 'json')),
                           ('check', tuple(DIAGNOSTICS)), ('method', ('HEAD', 'GET')),
                           ('action', ('start', 'stop', 'pause', 'unpause') if name == 'manage_container' else ('start', 'stop'))]:
        if field in properties and field in args and args[field] not in choices:
            raise ToolError(f"{field} 参数无效。")
    for field, default, low, high in [('offset', 0, 0, 1099511627776), ('depth', 3, 0, 5), ('limit', 50, 1, 100),
                                    ('bytes', 65536, 1, 1048576)]:
        if field in properties:
            value = args.setdefault(field, default)
            if type(value) is not int or not low <= value <= high:
                raise ToolError(f"{field} 须为 {low} 到 {high} 的整数。")
    if name == 'read_text_file':
        args.setdefault('position', 'head')
        if args.get('position', 'head') not in {'head', 'tail'}:
            raise ToolError('读取位置须为 head 或 tail')
        if args.get('position') == 'tail' and args['offset']:
            raise ToolError('tail 与 offset 不能同时指定，请使用 next_offset 继续读取')
    for field in ('content', 'old_text', 'new_text', 'query', 'pattern'):
        if field in args:
            value = args[field]
            maximum = 200 if field in ('query', 'pattern') else 16000
            if (not isinstance(value, str) or len(value) > maximum or '\0' in value
                    or (field in ('old_text', 'query', 'pattern') and not value)):
                raise ToolError(f"{field} 须为有效文本，最多 {maximum} 字，不允许空匹配或二进制内容。")
            if field in ('content', 'old_text', 'new_text') and '[已隐藏' in value:
                raise ToolError("不能将脱敏占位符写回服务器；请仅编辑未脱敏的具体片段，保留凭据原文。")
    if 'pattern' in args and '/' in args['pattern']:
        raise ToolError("搜索通配符只填写文件名，不包含目录路径。")
    for field in ('expected_sha256', 'backup_sha256'):
        if field in args and (not isinstance(args[field], str) or not re.fullmatch(r'[a-f0-9]{64}', args[field])):
            raise ToolError("文件摘要须为完整 SHA-256。")
    if name == 'restore_file':
        backup = args['backup_path']
        if (not isinstance(backup, str) or not re.fullmatch(re.escape(args['path']) +
                r'\.mini-deploy-backup\.\d+\.[a-f0-9]{16}', backup)):
            raise ToolError('只能恢复同一文件的 mini_deploy 备份，不支持任意文件复制。')
    validation = plan('validate_config', args['validation']) if 'validation' in args else None
    if "position" in properties:
        if args.setdefault("position", "tail") not in ("head", "tail"):
            raise ToolError("读取位置须为 head（开头）或 tail（末尾）。")
    if "container" in args:
        container = args["container"]
        pattern = r"[a-f0-9]{64}" if write else r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}"
        if not isinstance(container, str) or not re.fullmatch(pattern, container):
            raise ToolError("容器参数无效；重启必须使用完整容器 ID。")
    if "service" in properties:
        service = args["service"]
        if not isinstance(service, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.@:-]{0,180}", service):
            raise ToolError("服务名无效。")
        if not service.endswith('.service'):
            service += '.service'
        args["service"] = service
    if "lines" in properties:
        lines = args.setdefault("lines", 100)
        if type(lines) is not int or not 1 <= lines <= 200:
            raise ToolError("日志行数须为 1 到 200 的整数。")
    container, service = args.get("container", ""), args.get("service", "")
    commands = {
        "list_containers": ["docker", "ps", "-a", "--no-trunc", "--format",
                            "{{.ID}}\t{{.Names}}\t{{.Status}}\t{{.Ports}}"],
        "get_container_status": ["docker", "inspect", "--format",
            '{{.Id}} {{.Name}} status={{.State.Status}} running={{.State.Running}} restarts={{.RestartCount}} '
            'paused={{.State.Paused}} exit={{.State.ExitCode}} error={{.State.Error}} auto_remove={{.HostConfig.AutoRemove}} '
            'mounts={{json .Mounts}} {{if .State.Health}}health={{.State.Health.Status}}{{end}}',
            "--", container],
        "get_container_logs": ["docker", "logs", "--timestamps", "--since", "30m", "--tail", str(args.get("lines", 100)), container],
        "list_services": ["systemctl", "list-units", "--type=service", "--all", "--no-pager", "--plain"],
        "get_service_status": ["systemctl", "show", "--no-pager", "--property=Id,LoadState,ActiveState,SubState,MainPID,Result", "--", service],
        "get_service_logs": ["journalctl", "--no-pager", "--since", "30 minutes ago", "-n", str(args.get("lines", 100)), "-u", service],
        "get_listening_ports": ["ss", "-lntup"],
        "restart_container": ["docker", "restart", "--time", "10", container],
        "restart_service": ["systemctl", "--no-ask-password", "restart", "--", service],
        "reload_service": ["systemctl", "--no-ask-password", "reload", "--", service],
    }
    if name in SFTP_TOOLS:
        operation = {'inspect_file': '检查文件', 'read_text_file': '分页读取文件', 'search_files': '搜索文件',
                     'search_text': '搜索文件内容', 'write_file': '写入文件', 'edit_file': '替换文件片段',
                     'restore_file': '恢复配置备份'}[name]
        command = operation + ' ' + args['path']
        if name == 'write_file':
            command += '\n新内容：\n' + args['content']
        elif name == 'edit_file':
            command += '\n原片段：\n' + args['old_text'] + '\n新片段：\n' + args['new_text']
        elif name == 'restore_file':
            command += '\n备份：' + args['backup_path']
    elif name == 'run_diagnostic':
        command = shlex.join(DIAGNOSTICS[args['check']])
    elif name == 'check_http':
        try:
            url = args['url']
            parts = urlsplit(url) if isinstance(url, str) else None
            if (not parts or parts.scheme not in ('http', 'https') or not parts.hostname or parts.username
                    or parts.password or parts.fragment or len(url) > 2048
                    or any(ord(c) <= 32 for c in url) or parts.port == 0):
                raise ValueError()
        except ValueError:
            raise ToolError("须填写有效的 HTTP/HTTPS 地址，不包含账号密码或控制字符。") from None
        method = args.setdefault('method', 'HEAD')
        command = shlex.join(['curl', '--silent', '--show-error', '--max-time', '15', '--max-filesize', '8192',
                             '--proto', '=http,https', '--head' if method == 'HEAD' else '--include', '--', url])
    elif name == 'validate_config':
        runtime = args['runtime']
        argv = ['nginx', '-t', '-c', args['path']] if runtime == 'nginx' else ['caddy', 'validate', '--config', args['path']]
        if runtime == 'caddy' and args.get('adapter', 'caddyfile') == 'caddyfile':
            args.setdefault('adapter', 'caddyfile')
            argv += ['--adapter', 'caddyfile']
        if runtime == 'nginx' and 'adapter' in args:
            raise ToolError("Nginx 配置不使用 adapter 参数。")
        if 'container' in args:
            argv = ['docker', 'exec', args['container'], *argv]
        command = shlex.join(argv)
    elif name in ('manage_service', 'manage_container'):
        command = shlex.join(['systemctl', '--no-ask-password', args['action'], '--', service]
                            if name == 'manage_service' else ['docker', args['action'], container])
    elif name == "list_directory":
        command = "cd -- " + shlex.quote(args["path"]) + " && pwd -P && LC_ALL=C ls -lah -- ."
    elif name == "read_file":
        path, position = shlex.quote(args["path"]), args["position"]
        # Bound bytes before lines so an unusually long log line cannot flood the SSH channel.
        command = (f"[ -f {path} ] && [ -r {path} ] || "
                   "{ printf '%s\\n' '文件不存在、不是普通文件或无读取权限。' >&2; exit 1; }; "
                   f"printf '%s\\n' '文件片段：{position}，最多 {args['lines']} 行 / 8192 字节；并非完整文件。'; "
                   f"chunk=$(LC_ALL=C {position} -c 8192 -- {path}) || exit; "
                   f"printf '%s\\n' \"$chunk\" | {position} -n {args['lines']}")
    else:
        command = "uname -srm; nproc; uptime; free -b; df -hP" if name == "get_server_resources" else shlex.join(commands[name])
    if name == 'restart_container' or (name == 'manage_container' and args['action'] == 'stop'):
        inspect = shlex.join(['docker', 'inspect', '--format', '{{.HostConfig.AutoRemove}}', '--', container])
        command = (f"auto_remove=$({inspect}) || exit; "
                   '[ "$auto_remove" = false ] || '
                   "{ printf '%s\\n' '容器启用了自动删除或无法确认，拒绝停止/重启以保留文件。' >&2; exit 1; }; " + command)
    return {"name": name, "arguments": args, "title": description.split('，')[0].split('；')[0],
            "command": command, "write": write, 'reason': reason, 'transport': 'sftp' if name in SFTP_TOOLS else 'exec',
            'validation': validation,
            "impact": ("已有文件先保存原文备份并校验版本，可能改变业务配置；不删除文件、不自动重载服务。"
                       if name in FILE_WRITES else
                       "可能短暂中断业务；使用当前 SSH 账号权限，不自动提权。" if write else
                       "只列出目录及文件信息，不读取文件内容。" if name == "list_directory" else
                       "只读取指定文件片段，常见凭据脱敏后发送给模型，不修改或删除文件。" if name == "read_file" else
                       "只读取状态和近期日志。")}


def verification(operation):
    """Return a controlled status check and the state required by the requested action."""
    name, args = operation['name'], operation['arguments']
    if name in {'restart_service', 'reload_service', 'manage_service'}:
        action = args.get('action', 'start')
        return plan('get_service_status', {'service': args['service']}), ('inactive' if action == 'stop' else 'active')
    if name in {'restart_container', 'manage_container'}:
        action = args.get('action', 'start')
        return plan('get_container_status', {'container': args['container']}), action
    return None, None


def verified(operation, result, expected):
    if result.get('exit_code') != 0 or result.get('truncated'):
        return False
    text = result.get('output', '')
    if operation['name'] == 'get_service_status':
        fields = dict(re.findall(r'^(\w+)=(.*)$', text, re.M))
        return fields.get('LoadState') == 'loaded' and fields.get('ActiveState') == expected
    required = {'stop': ['running=false', 'status=exited'], 'pause': ['running=true', 'paused=true'],
                'unpause': ['running=true', 'paused=false'], 'start': ['running=true', 'paused=false']}[expected]
    tokens = text.split()
    return all(field in tokens for field in required) and 'health=unhealthy' not in tokens


def health_findings(operation, result):
    """Flag only recognizable evidence; absent evidence never means a healthy server."""
    text, name = result.get('output', ''), operation['name']
    findings = []
    if name == 'get_server_resources':
        memory = re.search(r'^Mem:\s+(\d+)\s+(\d+)\s+\d+\s+\d+\s+\d+\s+(\d+)', text, re.M)
        if memory and int(memory[1]) and int(memory[3]) / int(memory[1]) < .1:
            findings.append('可用内存不足 10%')
        for line in text.splitlines():
            used = re.search(r'\s(\d+)%\s+(.+)$', line)
            if used and int(used[1]) >= 90:
                findings.append(f'磁盘 {used[2]} 已使用 {used[1]}%')
    elif name == 'list_containers':
        for line in text.splitlines():
            fields = line.split('\t')
            if len(fields) >= 3 and re.search(r'\b(Restarting|Dead|unhealthy)\b|Exited \([1-9]', fields[2]):
                findings.append(f'容器 {fields[1]}：{fields[2]}')
    elif name == 'list_services':
        for line in text.splitlines():
            fields = line.split()
            if len(fields) >= 4 and 'failed' in fields[1:4]:
                findings.append('服务异常：' + fields[0])
    return findings[:20]
