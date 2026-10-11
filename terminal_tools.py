"""Structured Linux inspection and service actions; no model-supplied shell."""
from __future__ import annotations

import re
import shlex


class ToolError(ValueError):
    pass


CONTAINER = {"type": "string", "description": "容器名称或完整 ID，来自容器列表"}
UNIT = {"type": "string", "description": "systemd 服务名，例如 nginx.service"}
LINES = {"type": "integer", "minimum": 1, "maximum": 200, "description": "日志行数，默认 100"}
DIRECTORY = {"type": "string", "description": "目录的 Linux 绝对路径；省略或填写 . 表示 SSH 登录默认目录，不是交互终端 cd 后的目录"}
TOOLS = {
    "get_server_resources": ("检查 CPU、内存、磁盘及系统负载", {}, [], False),
    "list_directory": ("确认目录实际路径，列出文件、隐藏文件、权限、大小和修改时间；不读取文件内容，不递归",
                       {"path": DIRECTORY}, [], False),
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


def schemas(writes=False):
    return [{"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": required, "additionalProperties": False}}}
        for name, (description, properties, required, write) in TOOLS.items() if writes or not write]


def plan(name, arguments):
    if name not in TOOLS:
        raise ToolError("不支持该工具；不能执行任意命令或删除文件。")
    description, properties, required, write = TOOLS[name]
    if not isinstance(arguments, dict) or set(arguments) - set(properties) or set(required) - set(arguments):
        raise ToolError("工具参数不完整或包含未支持的参数。")
    args = dict(arguments)
    if "path" in properties:
        path = args.setdefault("path", ".")
        if (not isinstance(path, str) or not path or len(path) > 4096
                or (path != "." and not path.startswith("/"))
                or any(ord(c) < 32 or ord(c) == 127 for c in path)):
            raise ToolError("目录须填写 Linux 绝对路径，或使用 . 查看 SSH 登录默认目录。")
    if "container" in properties:
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
            'exit={{.State.ExitCode}} error={{.State.Error}} {{if .State.Health}}health={{.State.Health.Status}}{{end}}',
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
    if name == "list_directory":
        command = "cd -- " + shlex.quote(args["path"]) + " && pwd -P && LC_ALL=C ls -lah -- ."
    else:
        command = "uname -srm; nproc; uptime; free -b; df -hP" if name == "get_server_resources" else shlex.join(commands[name])
    return {"name": name, "arguments": args, "title": description.split('，')[0].split('；')[0],
            "command": command, "write": write,
            "impact": ("可能短暂中断业务；使用当前 SSH 账号权限，不自动提权。" if write else
                       "只列出目录及文件信息，不读取文件内容。" if name == "list_directory" else "只读取状态和近期日志。")}
