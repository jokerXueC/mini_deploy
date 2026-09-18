"""Read-only Compose ingress hints; never evaluate project commands or substitutions."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")
OVERRIDE_FILES = ("compose.override.yaml", "compose.override.yml", "docker-compose.override.yml", "docker-compose.override.yaml")
LIMIT = 131072


def inspect_files(files: dict[str, str]) -> dict:
    result = {"services": [], "bindings": [], "warnings": [], "files": []}
    manifests = [name for name in COMPOSE_FILES + OVERRIDE_FILES if name in files]
    if not manifests:
        return result
    result["files"] = manifests
    try:
        import yaml
    except ImportError:
        result["warnings"].append("未安装 YAML 检查依赖，无法检查 Compose 入口；请运行最新版 install.sh 后重试。")
        return result
    if len(manifests) > 1:
        result["warnings"].append("发现多份 Compose 配置，以下为各文件声明，未合并覆盖规则；请以部署脚本实际使用的 -f / profiles 配置为准。")
    for name in manifests:
        text = files[name]
        try:
            if not text or len(text.encode("utf-8")) > LIMIT:
                raise ValueError("unavailable")
            # Refuse aliases rather than expanding repository-controlled graphs.
            if any(isinstance(token, yaml.tokens.AliasToken) for token in yaml.scan(text)):
                raise ValueError("aliases")
            data = yaml.safe_load(text)
            if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
                raise ValueError("services")
            if data.get("include"):
                result["warnings"].append(f"{name} 引用了其他文件，未展开 include；仍需核对最终端口。")
            if len(data["services"]) > 128:
                result["warnings"].append(f"{name} 服务较多，仅检查前 128 个服务。")
            for service, config in list(data["services"].items())[:128]:
                if not isinstance(config, dict):
                    raise ValueError("service")
                label = f"{name} / {service}"
                if "nginx" in (str(service) + " " + str(config.get("image", ""))).lower():
                    result["services"].append(label)
                if config.get("extends") or config.get("profiles"):
                    result["warnings"].append(f"{label} 使用继承或 profiles，以下端口是否生效需核对启动参数。")
                if config.get("network_mode") == "host":
                    result["warnings"].append(f"{label} 使用主机网络：程序会直接占用服务器端口，请检查其监听配置。")
                ports = config.get("ports") or []
                if not isinstance(ports, list):
                    raise ValueError("ports")
                if len(ports) > 128:
                    result["warnings"].append(f"{label} 端口较多，仅检查前 128 个映射。")
                for port in ports[:128]:
                    parsed = parse_binding(port)
                    if parsed is None:
                        result["warnings"].append(f"{label} 有变量、范围或无法确定的端口映射，需核对实际发布端口。")
                    elif parsed:
                        result["bindings"].append({"service": str(service), "file": name, **parsed})
        except (yaml.YAMLError, ValueError, TypeError, RecursionError):
            result["warnings"].append(f"{name} 无法完整静态检查（文件过大、别名或格式问题）；不能据此判断端口空闲。")
    return result


def parse_binding(value) -> dict | None:
    if isinstance(value, dict):
        published = value.get("published")
        if published is None:
            return {}
        host = str(value.get("host_ip") or "0.0.0.0")
        protocol = str(value.get("protocol") or "tcp")
    elif isinstance(value, (str, int)) and not isinstance(value, bool):
        raw, _, protocol = str(value).partition("/")
        protocol = protocol or "tcp"
        if "$" in raw or "-" in raw:
            return None
        parts = raw.rsplit(":", 2)
        if len(parts) == 1:
            return {}  # Container-only port with an automatically allocated host port.
        published = parts[-2]
        host = parts[0].strip("[]") if len(parts) == 3 else "0.0.0.0"
    else:
        return None
    if not str(published).isdigit() or not 1 <= int(published) <= 65535 or "$" in host:
        return None
    return {"host": host, "port": int(published), "protocol": protocol}


def inspect_directory(workdir: Path) -> dict:
    files = {}
    for name in COMPOSE_FILES + OVERRIDE_FILES:
        path = workdir / name
        try:
            if path.is_symlink():
                files[name] = ""
            elif path.is_file():
                with path.open("rb") as stream:
                    content = stream.read(LIMIT + 1)
                files[name] = content.decode("utf-8") if len(content) <= LIMIT else ""
        except (OSError, UnicodeError):
            files[name] = ""
    return inspect_files(files)


def _read(command: list[str]) -> str:
    completed = subprocess.run(command, capture_output=True, text=True, timeout=5, check=True)
    return completed.stdout


def entry_advice(report: dict, *, check_live: bool = True) -> list[str]:
    messages = list(report["warnings"])
    if report["services"]:
        messages.append("检测到可能自带 Nginx 的服务：" + "、".join(report["services"]) +
                        "。可保留项目入口，无需再安装 Nginx；如需统一域名和证书，可让面板入口转发到业务 Nginx。")
    bindings = [binding for binding in report["bindings"] if binding["protocol"] == "tcp"]
    if any(binding["port"] in (80, 443) for binding in bindings):
        messages.append("项目声明发布服务器 80/443 端口，与面板或其他 Nginx 使用相同地址时会冲突。保留项目入口时无需新增 Nginx；共用面板入口时，请自行调整业务映射到其他端口或内部网络。")
    if not check_live or not bindings:
        return messages
    owners: dict[int, set[str]] = {}
    try:
        if shutil.which("docker"):
            for line in _read(["docker", "ps", "--format", "{{json .}}"]).splitlines():
                container = json.loads(line)
                for port in re.findall(r":(\d+)->\d+/tcp", container.get("Ports", "")):
                    owners.setdefault(int(port), set()).add("容器 " + container.get("Names", "未知"))
        if shutil.which("ss"):
            for line in _read(["ss", "-H", "-ltnp"]).splitlines():
                columns = line.split()
                if len(columns) >= 4:
                    port = columns[3].rsplit(":", 1)[-1]
                    if port.isdigit():
                        process = re.search(r'users:\(\("([^"\n]+)"', line)
                        owners.setdefault(int(port), set()).add("进程 " + process[1] if process else "服务器监听服务")
        else:
            messages.append("当前环境缺少 ss，无法完整确认本机监听端口；容器之外的服务需另行核对。")
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
        messages.append("无法完整读取端口占用信息，请检查 Docker 状态及运行权限；这不代表端口空闲。")
    for port in sorted({binding["port"] for binding in bindings} & owners.keys()):
        messages.append(f"服务器 TCP {port} 已有监听：{'、'.join(sorted(owners[port]))}。可能是本项目已运行的实例；请核对绑定地址、服务归属后再部署，不会自动停止或修改它。")
    return messages
