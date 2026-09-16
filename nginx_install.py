"""Create an isolated HTTP-only Nginx container after a reviewed install plan."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import secrets
import shutil
import socket
import time
from pathlib import Path
from typing import Any

import nginx_runtime
from certificates import CertificateError, atomic_write, run, trusted_path


INSTALL_ROOT = Path("/srv/mini-deploy-nginx")
IMAGE = "nginx:stable-alpine"
OWNER_LABEL = "io.mini-deploy.install-token"
HTTP_CONFIG = """# mini-deploy-managed: nginx-http-welcome-v1
server {
    listen 80 default_server;
    server_name _;
    default_type text/plain;
    return 200 'mini_deploy Nginx HTTP ready\\n';
}
"""


def check_http(port: int, *, welcome: bool = False) -> int:
    for attempt in range(5):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request("GET", "/", headers={"Host": "127.0.0.1"})
            response = connection.getresponse()
            body = response.read(512)
            if welcome:
                if response.status == 200 and b"mini_deploy Nginx HTTP ready" in body:
                    return response.status
            elif 200 <= response.status < 500:
                return response.status
        except (OSError, http.client.HTTPException):
            pass
        finally:
            connection.close()
        if attempt < 4:
            time.sleep(0.4)
    raise CertificateError(f"HTTP 端口 {port} 未通过本机访问检查，请检查 Nginx 状态和端口映射")


def _check_ports(ports: list[int]) -> None:
    published = run(["docker", "ps", "--format", "{{.Ports}}"], timeout=5)
    for port in ports:
        if re.search(rf":{port}->", published):
            raise CertificateError(f"端口 {port} 已由其他容器使用，请更换端口或取消预留 HTTPS 端口")
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("0.0.0.0", port))
        except OSError as exc:
            raise CertificateError(f"端口 {port} 已被占用，请选择其他端口，不会停止现有服务") from exc


def docker_plan(data_home: Path, name: Any, port: Any, reserve_https: Any) -> dict[str, Any]:
    if os.name != "posix" or os.geteuid() != 0:
        raise CertificateError("Docker Nginx 安装需要以 root 运行的 Linux 服务器")
    if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", name):
        raise CertificateError("请使用字母、数字、点、短横线或下划线填写容器名称")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise CertificateError("HTTP 端口必须在 1-65535 之间")
    if not isinstance(reserve_https, bool) or (reserve_https and port == 443):
        raise CertificateError("HTTP 端口不能与预留的 HTTPS 443 端口重叠")
    if not shutil.which("docker"):
        raise CertificateError("服务器未安装 Docker。请先运行 install.sh 并选择安装 Docker，或改选服务器本机安装")
    nginx_runtime.require_local_docker()
    run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=5)
    names = run(["docker", "ps", "-a", "--format", "{{.Names}}"], timeout=5).splitlines()
    if name in names:
        raise CertificateError("此名称的容器已经存在，请通过高级接入选择它，或使用新的名称；不会覆盖或重建已有容器")
    _check_ports([port, 443] if reserve_https else [port])
    root = INSTALL_ROOT / name
    conf_root = root / "conf.d"
    cert_root = data_home / "certificates"
    for path in (root, conf_root, cert_root):
        trusted_path(path)
        if "," in str(path) or not path.is_absolute():
            raise CertificateError("托管目录路径不支持 Docker 挂载，请检查安装路径")
        if path.exists() and not path.is_dir():
            raise CertificateError("Nginx 托管目录被普通文件占用，请检查安装路径")
    if root.is_relative_to(data_home) or data_home.is_relative_to(root):
        raise CertificateError("容器配置目录必须与面板数据目录分开")
    marker = root / ".mini-deploy-nginx.json"
    metadata = {"format": 1, "name": name, "port": port, "reserve_https": reserve_https,
                "certificate_root": str(cert_root), "image": IMAGE}
    if root.exists():
        trusted_path(marker)
        if not marker.is_file() or marker.stat().st_size > 4096:
            raise CertificateError("此配置目录已经存在且不是本次安装的托管目录，请换用新的容器名称")
        try:
            previous = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CertificateError("容器安装记录损坏，请人工检查") from exc
        if previous != metadata:
            raise CertificateError("已有容器目录的安装参数不同，请使用新的容器名称")
        allowed = {marker.name, "conf.d"}
        if any(path.name not in allowed for path in root.iterdir()):
            raise CertificateError("安装目录存在额外文件，请核对后使用新的容器名称")
        if conf_root.exists():
            for path in conf_root.iterdir():
                trusted_path(path)
                if path.name != "00-welcome.conf" or not path.is_file() or path.stat().st_size > 4096 or path.read_text(encoding="utf-8") != HTTP_CONFIG:
                    raise CertificateError("容器配置已经修改，拒绝覆盖，请通过高级接入管理或使用新的名称")
    return {"mode": "docker", "container": name, "port": port, "reserve_https": reserve_https,
            "image": IMAGE, "conf_root": str(conf_root), "certificate_root": str(cert_root),
            "metadata": metadata}


def _remove_owned_container(name: str, owner: str) -> bool:
    try:
        details = json.loads(run(["docker", "inspect", "--type", "container", "--format",
                                 '{{json .Config.Labels}}', name], timeout=5))
        if not isinstance(details, dict) or details.get(OWNER_LABEL) != owner:
            return False
        container_id = run(["docker", "inspect", "--type", "container", "--format", "{{.Id}}", name], timeout=5).strip()
        if not re.fullmatch(r"[a-f0-9]{64}", container_id):
            return False
        # Recheck by ID so replacing a name cannot authorize deleting another container.
        labels = json.loads(run(["docker", "inspect", "--type", "container", "--format", '{{json .Config.Labels}}', container_id], timeout=5))
        if labels.get(OWNER_LABEL) != owner:
            return False
        run(["docker", "rm", "-f", container_id], timeout=15)
        return True
    except (CertificateError, ValueError, AttributeError):
        return False


def install_docker(data_home: Path, plan: dict[str, Any]) -> dict[str, Any]:
    if docker_plan(data_home, plan["container"], plan["port"], plan["reserve_https"]) != plan:
        raise CertificateError("安装环境已变化，请重新检查")
    run(["docker", "pull", IMAGE], timeout=180)
    # Check again after the download, before filesystem or container mutations.
    if docker_plan(data_home, plan["container"], plan["port"], plan["reserve_https"]) != plan:
        raise CertificateError("镜像下载期间环境已变化，请重新检查")
    conf_root, cert_root = Path(plan["conf_root"]), Path(plan["certificate_root"])
    root = conf_root.parent
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write(root / ".mini-deploy-nginx.json", json.dumps(plan["metadata"], sort_keys=True))
    conf_root.mkdir(mode=0o755, exist_ok=True)
    cert_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    welcome_file = conf_root / "00-welcome.conf"
    if not welcome_file.exists():
        atomic_write(welcome_file, HTTP_CONFIG)
    owner = secrets.token_hex(24)
    command = ["docker", "create", "--name", plan["container"], "--restart", "unless-stopped",
               "--label", f"{OWNER_LABEL}={owner}", "--network", "bridge",
               "--add-host", "host.docker.internal:host-gateway",
               "--publish", f"{plan['port']}:80",
               "--mount", f"type=bind,src={conf_root},dst={nginx_runtime.CONF_DEST},readonly",
               "--mount", f"type=bind,src={cert_root},dst={nginx_runtime.CERT_DEST},readonly"]
    if plan["reserve_https"]:
        command.extend(["--publish", "443:443"])
    try:
        container_id = run([*command, IMAGE], timeout=30).strip()
        if not re.fullmatch(r"[a-f0-9]{64}", container_id):
            raise CertificateError("Docker 未返回有效容器 ID")
        run(["docker", "start", container_id], timeout=30)
        run(["docker", "exec", container_id, "nginx", "-t"], timeout=15)
        status = check_http(plan["port"], welcome=True)
        return {"container": plan["container"], "container_id": container_id,
                "port": plan["port"], "http_status": status}
    except CertificateError as exc:
        removed = _remove_owned_container(plan["container"], owner)
        detail = "本次创建的容器已移除，可按原参数重试" if removed else "请检查容器状态；不会删除无法确认归属的容器"
        raise CertificateError(f"{exc}；{detail}，配置目录和镜像已保留") from exc


def plan_token(plan: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
