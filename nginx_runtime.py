"""Validated local/Docker Nginx targets, without arbitrary command or path inputs."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import socket
from pathlib import Path, PurePosixPath
from typing import Any

from certificates import CertificateError, atomic_write, run, trusted_path


CONF_DEST = "/etc/nginx/conf.d"
CERT_DEST = "/etc/mini-deploy/certificates"


def local_setup_plan() -> dict[str, Any]:
    if os.name != "posix" or os.geteuid() != 0 or not shutil.which("systemctl"):
        raise CertificateError("自动配置需要以 root 运行的 Linux systemd 服务器")
    installed = bool(shutil.which("nginx"))
    try:
        active = run(["systemctl", "is-active", "nginx"], timeout=5).strip() == "active"
    except CertificateError:
        active = False
    manager = ""
    if not installed:
        manager = next((name for name in ("apt-get", "dnf", "yum") if shutil.which(name)), "")
        if not manager:
            raise CertificateError("此系统暂不支持网页安装 Nginx，请先安装 Nginx，再使用已有实例接入")
    if not active:
        # Docker may publish a port using NAT without a host listening socket.
        if shutil.which("docker"):
            require_local_docker()
            ports = run(["docker", "ps", "--format", "{{.Ports}}"], timeout=5)
            if re.search(r":80->", ports):
                raise CertificateError("80 端口已由 Docker 容器使用，请在高级接入中选择已有 Nginx，不会停止现有容器")
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("0.0.0.0", 80))
        except OSError as exc:
            raise CertificateError("80 端口已被其他服务占用，请选择已有 Nginx 或由管理员处理端口冲突") from exc
    if installed:
        run(["nginx", "-t"])
    return {"installed": installed, "active": active, "package_manager": manager}


def prepare_local(plan: dict[str, Any]) -> None:
    if local_setup_plan() != plan:
        raise CertificateError("服务器环境已变化，请重新检查后确认")
    if not plan["installed"]:
        manager = plan["package_manager"]
        if manager == "apt-get":
            run(["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "update"], timeout=180)
            run(["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "nginx"], timeout=300)
        elif manager in {"dnf", "yum"}:
            run([manager, "install", "-y", "nginx"], timeout=300)
        else:
            raise CertificateError("不支持的包管理器，请重新检查环境")
    run(["nginx", "-t"])
    if not plan["active"]:
        run(["systemctl", "enable", "--now", "nginx"], timeout=30)


def check_domain_conflict(runtime: Runtime, domain: str, own_path: Path) -> None:
    dump = run(runtime.command("dump"))
    sections = re.split(r"(?m)^# configuration file (.+):\s*$", dump)
    chunks = [("", sections[0]), *zip(sections[1::2], sections[2::2])]
    own = own_path.as_posix() if runtime.mode == "local" else f"{CONF_DEST}/{own_path.name}"
    for current_file, content in chunks:
        if current_file == own:
            continue
        content = re.sub(r"(?m)#.*$", "", content)
        for match in re.finditer(r"\bserver_name\s+([^;]+);", content):
            names = [name.strip("\"'").lower() for name in match.group(1).split()]
            if domain in names:
                raise CertificateError("此域名已存在于其他 Nginx 站点，请先核对已有配置，避免覆盖或访问到错误项目")


def require_local_docker() -> None:
    endpoint = os.environ.get("DOCKER_HOST", "")
    if endpoint and not endpoint.startswith("unix:///"):
        raise CertificateError("仅支持本机 Unix Socket 的 Docker，不支持远程 Docker daemon")
    context = os.environ.get("DOCKER_CONTEXT", "")
    if not endpoint or context:
        args = ["docker", "context", "inspect"]
        if context:
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", context):
                raise CertificateError("Docker context 名称无效")
            args.append(context)
        endpoint = json.loads(run([*args, "--format", "{{json .Endpoints.docker.Host}}"], timeout=3))
        if not isinstance(endpoint, str) or not endpoint.startswith("unix:///"):
            raise CertificateError("仅支持本机 Unix Socket 的 Docker，不支持远程 Docker daemon")


def inspect_container(name: str) -> dict[str, Any]:
    if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", name):
        raise CertificateError("请选择有效的 Nginx 容器名称")
    require_local_docker()
    # Only selected fields are returned: never expose container environment secrets.
    fmt = '{"id":{{json .Id}},"name":{{json .Name}},"running":{{json .State.Running}},"mounts":{{json .Mounts}},"network":{{json .HostConfig.NetworkMode}},"ports":{{json .NetworkSettings.Ports}}}'
    try:
        item = json.loads(run(["docker", "inspect", "--type", "container", "--format", fmt, name], timeout=3))
    except (ValueError, TypeError) as exc:
        raise CertificateError("无法读取容器，请确认 Docker 已启动且容器存在") from exc
    if not isinstance(item, dict) or not re.fullmatch(r"[a-f0-9]{64}", str(item.get("id", ""))):
        raise CertificateError("容器信息无效")
    if not item.get("running"):
        raise CertificateError("所选 Nginx 容器尚未启动")
    if item.get("network") in {"none", ""} or str(item.get("network", "")).startswith("container:"):
        raise CertificateError("暂不支持此容器网络模式，请使用 bridge、自定义网络或 host")
    run(["docker", "exec", item["id"], "nginx", "-v"], timeout=3)
    return item


def directory_mount(item: dict[str, Any], destination: str) -> Path:
    mounts = item.get("mounts", [])
    selected = [m for m in mounts if m.get("Destination") == destination]
    if len(selected) != 1 or selected[0].get("Type") != "bind":
        raise CertificateError(f"请将宿主机目录 bind mount 到容器 {destination}；不支持单文件挂载或命名 Volume")
    for mount in mounts:
        target = PurePosixPath(str(mount.get("Destination", "/")))
        if target != PurePosixPath(destination) and target.is_relative_to(destination):
            raise CertificateError(f"{destination} 下存在嵌套挂载，不能安全管理")
    source = Path(selected[0].get("Source", ""))
    if not source.is_absolute() or not source.is_dir():
        raise CertificateError("容器挂载目录必须在本机存在；不支持远程 Docker daemon")
    trusted_path(source)
    return source


def validate_host(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 253:
        raise CertificateError("后端地址无效")
    value = value.strip().lower()
    try:
        address = ipaddress.ip_address(value)
        if address.is_unspecified or address.is_multicast or address.is_link_local:
            raise CertificateError("后端地址不能是未指定、多播或链路本地地址")
        if address.version != 4:
            raise CertificateError("当前后端地址支持 IPv4 或主机名")
        return str(address)
    except ValueError as exc:
        if isinstance(exc, CertificateError):
            raise
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9_.-]{0,251}[a-z0-9])?", value):
        raise CertificateError("填写 IP、容器服务名或主机名，不要包含协议、端口或路径")
    return value


class Runtime:
    def __init__(self, profile: dict[str, Any], data_home: Path, *, live: bool = True):
        self.profile = profile
        self.mode = profile.get("mode", "local")
        self.data_home = data_home
        self.container_id = ""
        self.conf_root = Path("/etc/nginx/conf.d")
        if self.mode == "none":
            raise CertificateError("尚未接入 Nginx，请先在 Nginx 证书页选择运行环境")
        if self.mode not in {"local", "docker"}:
            raise CertificateError("Nginx 模式无效")
        if self.mode == "docker":
            self.conf_root = Path(str(profile.get("conf_root", "")))
            if not self.conf_root.is_absolute():
                raise CertificateError("Nginx 挂载记录无效，请重新检测")
            if live:
                item = inspect_container(profile.get("container", ""))
                root = directory_mount(item, CONF_DEST)
                cert_root = directory_mount(item, CERT_DEST)
                if root != self.conf_root or cert_root != data_home / "certificates" or item["network"] != profile.get("network"):
                    raise CertificateError("容器挂载或网络已变化，请重新检测并保存 Nginx 设置")
                self.container_id = item["id"]
                # Verify the active process can actually read the configured paths.
                run(["docker", "exec", self.container_id, "nginx", "-t"])
        trusted_path(self.conf_root)

    def command(self, action: str) -> list[str]:
        args = {"test": ["nginx", "-t"], "dump": ["nginx", "-T"], "reload": ["nginx", "-s", "reload"]}[action]
        if self.mode == "docker":
            if not self.container_id:
                raise CertificateError("Nginx 容器尚未验证")
            return ["docker", "exec", self.container_id, *args]
        return ["systemctl", "reload", "nginx"] if action == "reload" else args

    def visible_path(self, path: Path) -> str:
        if self.mode == "local":
            return path.as_posix()
        relative = path.relative_to(self.data_home / "certificates")
        return str(PurePosixPath(CERT_DEST) / relative.as_posix())

    def host_reference(self, value: str) -> Path:
        if self.mode == "docker":
            path = PurePosixPath(value)
            if not path.is_relative_to(CERT_DEST):
                raise CertificateError("容器还引用了托管证书目录以外的证书，无法安全确认引用，拒绝删除")
            return self.data_home / "certificates" / str(path.relative_to(CERT_DEST))
        return Path(value)

    def upstream(self, host: str | None) -> str:
        value = validate_host(host or "127.0.0.1")
        if self.mode == "docker" and self.profile.get("network") != "host":
            if not host or value == "localhost" or value.startswith("127."):
                raise CertificateError("桥接网络中的 Nginx 不能用 127.0.0.1 访问宿主机业务，请填写同网络服务名或可达的宿主机地址")
        return value

    def probe(self, host: str, port: int) -> None:
        host = self.upstream(host)
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise CertificateError("后端端口必须在 1-65535 之间")
        if self.mode == "local" or self.profile.get("network") == "host":
            try:
                with socket.create_connection((host, port), timeout=4):
                    return
            except OSError as exc:
                raise CertificateError("Nginx 所在网络无法连接业务端口，请先启动业务并核对监听地址") from exc
        # Fixed script, input travels as a positional argument, never interpolated shell code.
        script = 'if command -v curl >/dev/null 2>&1; then exec curl --noproxy "*" -sS -o /dev/null --connect-timeout 4 --max-time 6 "$1"; elif command -v wget >/dev/null 2>&1; then exec wget -q -T 6 -O /dev/null "$1"; else exit 127; fi'
        try:
            run(["docker", "exec", self.container_id, "sh", "-c", script, "probe", f"http://{host}:{port}/"])
        except CertificateError as exc:
            raise CertificateError("容器内连通检查失败：确认 curl/wget 可用、业务已启动且共享网络；wget 检查要求业务 / 返回成功状态") from exc


class Settings:
    def __init__(self, data_home: Path):
        self.data_home = data_home
        self.path = data_home / "nginx.json"

    def read(self) -> dict[str, Any]:
        trusted_path(self.path)
        if not self.path.exists():
            return {"profile": {"mode": "local"}, "upstreams": {}, "configured": False}
        try:
            if self.path.stat().st_size > 128 * 1024:
                raise ValueError("oversize")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("profile"), dict) or not isinstance(data.get("upstreams"), dict):
                raise ValueError("invalid settings")
            mode = data["profile"].get("mode")
            if not isinstance(mode, str) or mode not in {"local", "docker", "none"}:
                raise ValueError("invalid mode")
            for key, value in data["upstreams"].items():
                if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", key):
                    raise ValueError("invalid project key")
                validate_host(value)
            return {**data, "configured": True}
        except (ValueError, OSError) as exc:
            raise CertificateError("Nginx 接入配置损坏，请检查数据目录 nginx.json") from exc

    def runtime(self, *, live: bool = True) -> Runtime:
        return Runtime(self.read()["profile"], self.data_home, live=live)

    def candidate(self, mode: str, name: str = "") -> dict[str, Any]:
        if mode == "none":
            return {"mode": "none"}
        if mode == "local":
            if not shutil.which("nginx"):
                raise CertificateError("本机未安装 Nginx；可在安装向导中安装，或选择 Docker 容器")
            run(["nginx", "-t"])
            dump = run(["nginx", "-T"])
            if not re.search(r"include\s+/etc/nginx/conf\.d/\*\.conf\s*;", dump):
                raise CertificateError("本机 nginx.conf 必须包含 include /etc/nginx/conf.d/*.conf;")
            return {"mode": "local"}
        if mode != "docker":
            raise CertificateError("请选择本机、Docker 或暂不配置")
        item = inspect_container(name)
        root = directory_mount(item, CONF_DEST)
        cert_root = directory_mount(item, CERT_DEST)
        expected = self.data_home / "certificates"
        if cert_root != expected:
            raise CertificateError(f"证书目录应挂载 {expected} 到 {CERT_DEST}，不能挂载整个数据目录")
        forbidden = [Path("/"), Path("/etc"), Path("/usr"), Path("/var"), Path("/root"), Path("/home"), Path("/run")]
        if root in forbidden or root.is_relative_to(self.data_home) or self.data_home.is_relative_to(root):
            raise CertificateError("Nginx 配置挂载需使用独立目录，不能与 Agent 数据目录重叠")
        run(["docker", "exec", item["id"], "nginx", "-t"])
        dump = run(["docker", "exec", item["id"], "nginx", "-T"])
        if not re.search(r"include\s+/etc/nginx/conf\.d/\*\.conf\s*;", dump):
            raise CertificateError("容器 nginx.conf 必须包含 include /etc/nginx/conf.d/*.conf;")
        return {"mode": "docker", "container": name, "conf_root": str(root), "network": item["network"]}

    def discover(self) -> dict[str, Any]:
        result: dict[str, Any] = {"local": bool(shutil.which("nginx")), "containers": [], "errors": []}
        if shutil.which("docker"):
            try:
                names = run(["docker", "ps", "--format", "{{.Names}}"], timeout=5)
                if len(names.splitlines()) > 8:
                    result["errors"].append("容器较多，本次检测前 8 个；可手动填写其他 Nginx 容器名称")
                for name in names.splitlines()[:8]:
                    # Probe running containers, not only images whose names contain nginx.
                    try:
                        item = inspect_container(name)
                    except CertificateError:
                        continue
                    result["containers"].append({"name": name, "network": item["network"],
                        "ports": item.get("ports", {}), "mounts": [
                            {k: m.get(k) for k in ("Type", "Source", "Destination", "RW")} for m in item.get("mounts", [])
                        ]})
            except CertificateError as exc:
                result["errors"].append(str(exc))
        result["certificate_mount"] = f"{self.data_home / 'certificates'}:{CERT_DEST}:ro"
        return result

    def save(self, profile: dict[str, Any], upstreams: dict[str, str]) -> None:
        trusted_path(self.data_home)
        self.data_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write(self.path, json.dumps({"profile": profile, "upstreams": upstreams}, ensure_ascii=False))
