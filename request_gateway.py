"""Independent, panel-owned reverse proxies with bounded JSON request logs."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import nginx_requests
import nginx_runtime
from certificates import CertificateError, atomic_write, run, trusted_path


IMAGE = "nginx:stable-alpine"
LABEL = "io.mini-deploy.request-gateway"
LOCK = threading.RLock()
KEY = re.compile(r"[a-z][a-z0-9-]{0,39}")
DOCKER_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}")
IMAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}")


def normalize(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CertificateError("请填写网关配置")
    key = raw.get("key", "")
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise CertificateError("入口标识需以小写字母开头，只含小写字母、数字和短横线，最多 40 位")
    name = raw.get("name") or key
    if not isinstance(name, str) or len(name) > 80 or any(ord(c) < 32 for c in name):
        raise CertificateError("入口名称无效")
    port = raw.get("port", 18080)
    if type(port) is not int or not 1024 <= port <= 65535 or port == 6868:
        raise CertificateError("监听端口需为 1024-65535，且不能使用面板端口 6868")
    bind = raw.get("bind", "127.0.0.1")
    if not isinstance(bind, str) or bind not in {"127.0.0.1", "0.0.0.0"}:
        raise CertificateError("请选择本机访问或所有网卡")
    network = raw.get("network", "host")
    if not isinstance(network, str) or not DOCKER_NAME.fullmatch(network) or network in {"none", "bridge"}:
        raise CertificateError("请选择主机网络或已有的自定义 Docker bridge 网络")
    image = raw.get("image", IMAGE)
    if not isinstance(image, str) or not IMAGE_NAME.fullmatch(image) or "://" in image:
        raise CertificateError("镜像地址无效，请填写官方 Nginx 镜像或兼容的镜像副本")
    upstream = raw.get("upstream", "")
    if not isinstance(upstream, str) or len(upstream) > 512 or any(c.isspace() for c in upstream):
        raise CertificateError("后端地址无效")
    try:
        url = urlsplit(upstream)
        host, upstream_port = url.hostname, 80 if url.port is None else url.port
    except ValueError as exc:
        raise CertificateError("后端地址或端口无效") from exc
    if (url.scheme != "http" or not host or url.username or url.password or url.query or url.fragment
            or url.path not in {"", "/"} or not 1 <= upstream_port <= 65535):
        raise CertificateError("后端需为 http://主机:端口，不包含账号、路径或查询参数；HTTPS 继续由外层代理处理")
    try:
        ip = ipaddress.ip_address(host)
        if ip.version != 4 or ip.is_unspecified or ip.is_multicast:
            raise CertificateError("后端 IP 需为具体 IPv4 地址")
        if network != "host" and ip.is_loopback:
            raise CertificateError("Docker 网络中请使用业务服务名；访问宿主机服务请选择主机网络")
    except ValueError as exc:
        if isinstance(exc, CertificateError):
            raise
        if not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9_.-]{0,251}[a-zA-Z0-9])?", host):
            raise CertificateError("后端主机名无效") from exc
        if network != "host" and host == "localhost":
            raise CertificateError("Docker 网络中不能用 localhost 指向宿主机")
    if host == f"mini-gateway-{key}" or (network == "host" and host in {"localhost", "127.0.0.1"} and upstream_port == port):
        raise CertificateError("后端不能指向网关自身")
    trust = raw.get("trust_proxy", False)
    if type(trust) is not bool:
        raise CertificateError("代理请求头选项无效")
    return dict(key=key, name=name, port=port, bind=bind, network=network, image=image,
                upstream=f"http://{host}:{upstream_port}", trust_proxy=trust)


def revision(spec: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()


def config(spec: dict[str, Any]) -> str:
    listener = f'{spec["bind"]}:{spec["port"]}' if spec["network"] == "host" else "10000"
    # Custom bridge networks use Docker DNS on each new resolution, surviving backend recreation.
    upstream = (f'resolver 127.0.0.11 valid=10s ipv6=off;\n'
                f'set $backend "{spec["upstream"]}";\nproxy_pass $backend;'
                if spec["network"] != "host" else f'proxy_pass {spec["upstream"]};')
    forwarded = "$proxy_add_x_forwarded_for" if spec["trust_proxy"] else "$remote_addr"
    proto = "$gateway_proto" if spec["trust_proxy"] else "$scheme"
    return f'''# mini-deploy-managed: request-gateway-v1
worker_processes 1;
pid /tmp/nginx.pid;
error_log /dev/stderr crit;
events {{ worker_connections 2048; }}
http {{
    client_body_temp_path /tmp/client-body;
    proxy_temp_path /tmp/proxy;
    fastcgi_temp_path /tmp/fastcgi;
    uwsgi_temp_path /tmp/uwsgi;
    scgi_temp_path /tmp/scgi;
    map $http_upgrade $gateway_connection {{ default upgrade; '' ''; }}
    map $http_x_forwarded_proto $gateway_proto {{ default $scheme; http http; https https; }}
    {nginx_requests.config_text("docker")}
    server {{
        listen {listener};
        server_name _;
        client_max_body_size 64m;
        location / {{
            {upstream}
            proxy_http_version 1.1;
            proxy_set_header Host $http_host;
            proxy_set_header X-Forwarded-For {forwarded};
            proxy_set_header X-Forwarded-Proto {proto};
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $gateway_connection;
            proxy_buffering off;
            proxy_request_buffering off;
            proxy_cache off;
            proxy_next_upstream off;
            proxy_connect_timeout 5s;
            proxy_read_timeout 3600s;
            proxy_send_timeout 3600s;
        }}
    }}
}}
'''


class Store:
    def __init__(self, data_home: Path):
        self.root = data_home / "request-gateways"

    def directory(self, key: str) -> Path:
        if not isinstance(key, str) or not KEY.fullmatch(key):
            raise CertificateError("网关标识无效")
        path = self.root / key
        trusted_path(path)
        return path

    def read(self, key: str) -> dict[str, Any]:
        path = self.directory(key) / "entry.json"
        trusted_path(path)
        if not path.is_file() or path.stat().st_size > 8192:
            raise CertificateError("网关配置不存在或异常")
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or value.get("format") != 1 or
                not re.fullmatch(r"[a-f0-9]{32}", str(value.get("owner", "")))):
            raise CertificateError("网关管理标记无效")
        value["spec"] = normalize(value.get("spec"))
        if value["spec"]["key"] != key:
            raise CertificateError("网关配置标识不一致")
        return value

    def entries(self) -> list[dict[str, Any]]:
        trusted_path(self.root)
        if not self.root.exists():
            return []
        return [self.read(path.name) for path in sorted(self.root.iterdir())
                if KEY.fullmatch(path.name) and (path / "entry.json").exists()]

    def inspect(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        nginx_runtime.require_local_docker()
        name = f'mini-gateway-{entry["spec"]["key"]}'
        ids = run(["docker", "ps", "-a", "--no-trunc", "--filter", f"name=^/{name}$", "--format", "{{.ID}}"], timeout=5).splitlines()
        if not ids:
            return None
        if len(ids) != 1 or not re.fullmatch(r"[a-f0-9]{64}", ids[0]):
            raise CertificateError("网关容器信息异常")
        item = json.loads(run(["docker", "inspect", ids[0]], timeout=5))[0]
        if item.get("Config", {}).get("Labels", {}).get(LABEL) != entry["owner"]:
            raise CertificateError("同名容器不是此面板托管的网关，不会操作")
        return item

    def describe(self, entry: dict[str, Any], *, live: bool = True) -> dict[str, Any]:
        from gateway_connections import Connections
        spec = entry["spec"]
        item, error = None, ""
        if live:
            try:
                item = self.inspect(entry)
            except (ValueError, OSError) as exc:
                error = str(exc)
        state = item.get("State", {}).get("Status", "unknown") if item else "not_created"
        address = f'127.0.0.1:{spec["port"]}'
        docker_address = f'mini-gateway-{spec["key"]}:10000' if spec["network"] != "host" else ""
        return {**spec, "revision": revision(spec), "state": "unknown" if error else state,
                "container": f'mini-gateway-{spec["key"]}', "error": error,
                "local_address": address, "docker_address": docker_address,
                "connection": Connections(self.root.parent).status(spec['key']),
                "caddy_upstream": docker_address or address}

    def save(self, raw: Any, expected: str = "") -> dict[str, Any]:
        spec = normalize(raw)
        from gateway_connections import Connections
        if Connections(self.root.parent).status(spec['key'])['state'] != 'not_connected':
            raise CertificateError("此入口已接入网站，请先撤销接入后再修改网关配置")
        self.recover(spec["key"])
        directory = self.directory(spec["key"])
        old = self.read(spec["key"]) if (directory / "entry.json").exists() else None
        if not old and directory.exists() and any(directory.iterdir()):
            raise CertificateError("入口目录存在未完成的配置，请核对后使用新的入口标识")
        if old and expected != revision(old["spec"]):
            raise CertificateError("配置已变化，请刷新后重新编辑")
        if not old and expected:
            raise CertificateError("入口已被删除，请刷新")
        if not old and len(self.entries()) >= 20:
            raise CertificateError("最多管理 20 个网关入口")
        item = self.inspect(old) if old else None
        if item and any(spec[k] != old["spec"][k] for k in ("port", "bind", "network", "image")):
            raise CertificateError("修改端口、网络或镜像请新建入口并切换流量，确认后删除旧入口")
        for entry in self.entries():
            if entry["spec"]["key"] != spec["key"] and entry["spec"]["port"] == spec["port"]:
                raise CertificateError("监听端口已分配给其他入口")
        if item and item["State"]["Status"] not in {"running", "exited", "created"}:
            raise CertificateError("请等待网关恢复运行或停止后再编辑")
        entry = {"format": 1, "owner": old["owner"] if old else secrets.token_hex(16), "spec": spec}
        conf_dir = directory / "conf"
        trusted_path(conf_dir)
        conf_dir.mkdir(parents=True, mode=0o755, exist_ok=True)
        os.chmod(conf_dir, 0o755)
        path = conf_dir / "nginx.conf"
        trusted_path(path)
        previous = path.read_text(encoding="utf-8") if path.exists() else None
        if previous is not None and (not old or previous != config(old["spec"])):
            raise CertificateError("网关文件已被外部修改，不会覆盖")
        journal = directory / "pending.json"
        committed = False
        try:
            # Publish draft metadata first so an interrupted first save remains visible and recoverable.
            if not old:
                atomic_write(directory / "entry.json", json.dumps(entry, ensure_ascii=False))
            else:
                atomic_write(journal, json.dumps({"previous": old, "next": entry}, ensure_ascii=False))
            atomic_write(path, config(spec))
            os.chmod(path, 0o644)
            if item and item["State"]["Running"]:
                self.reload(item["Id"])
            atomic_write(directory / "entry.json", json.dumps(entry, ensure_ascii=False))
            committed = True
            journal.unlink(missing_ok=True)
        except (ValueError, OSError) as exc:
            if committed:
                raise CertificateError("配置已保存，但事务清理未完成，请刷新后重试") from exc
            if previous is not None:
                atomic_write(path, previous)
                os.chmod(path, 0o644)
                if item and item["State"]["Running"]:
                    try:
                        self.reload(item["Id"])
                    except (ValueError, OSError) as rollback:
                        raise CertificateError("旧配置已恢复，但重新加载失败，请检查网关容器") from rollback
            else:
                path.unlink(missing_ok=True)
                if not old:
                    (directory / "entry.json").unlink(missing_ok=True)
                    conf_dir.rmdir()
                    directory.rmdir()
            journal.unlink(missing_ok=True)
            raise CertificateError("保存失败，已恢复原配置") from exc
        return self.describe(entry, live=False)

    def recover(self, key: str) -> None:
        directory = self.directory(key)
        journal = directory / "pending.json"
        trusted_path(journal)
        if not journal.exists():
            return
        if journal.stat().st_size > 24576:
            raise CertificateError("网关恢复记录异常，请人工核对")
        transaction = json.loads(journal.read_text(encoding="utf-8"))
        if not isinstance(transaction, dict):
            raise CertificateError("网关恢复记录异常，请人工核对")
        current = self.read(key)
        versions = [transaction.get("previous"), transaction.get("next")]
        for entry in versions:
            if (not isinstance(entry, dict) or entry.get("format") != 1 or
                    entry.get("owner") != current["owner"] or normalize(entry.get("spec"))["key"] != key):
                raise CertificateError("网关恢复记录不匹配，不会覆盖文件")
        if current not in versions:
            raise CertificateError("网关配置不属于待恢复事务，不会覆盖文件")
        path = directory / "conf" / "nginx.conf"
        trusted_path(path)
        if path.exists() and path.read_text(encoding="utf-8") not in [config(e["spec"]) for e in versions]:
            raise CertificateError("网关文件已被外部修改，不会自动恢复")
        # The manifest is the commit point. Before commit restore old; after commit keep new.
        atomic_write(path, config(current["spec"]))
        os.chmod(path, 0o644)
        item = self.inspect(current)
        if item and item["State"]["Status"] not in {"running", "exited", "created", "dead"}:
            raise CertificateError("旧配置文件已恢复，但容器尚未稳定或已暂停；请先停止网关，再启动以完成恢复")
        if item and item["State"]["Status"] == "running":
            self.reload(item["Id"])
        journal.unlink()

    @staticmethod
    def reload(container_id: str) -> None:
        run(["docker", "exec", container_id, "nginx", "-t", "-c", "/gateway/nginx.conf"], timeout=10)
        run(["docker", "exec", container_id, "nginx", "-s", "reload", "-c", "/gateway/nginx.conf"], timeout=10)

    def start(self, entry: dict[str, Any]) -> None:
        if os.name != "posix" or os.geteuid() != 0:
            raise CertificateError("网关运行需要 Linux root 和本机 Docker")
        spec = entry["spec"]
        conf_dir = self.directory(spec["key"]) / "conf"
        trusted_path(conf_dir / "nginx.conf")
        if "," in str(conf_dir):
            raise CertificateError("网关数据路径不能包含逗号")
        if (conf_dir / "nginx.conf").read_text(encoding="utf-8") != config(spec):
            raise CertificateError("配置文件已被外部修改，请核对后重试")
        item = self.inspect(entry)
        base = ["docker", "run", "--network", spec["network"], "--read-only", "--user", "101:101",
                "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit", "64", "--memory", "128m",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m,mode=1777", "--entrypoint", "nginx",
                "--mount", f"type=bind,src={conf_dir},dst=/gateway,readonly"]
        if item:
            if item["State"]["Status"] == "running" and not item["State"].get("Paused") and not item["State"].get("Restarting"):
                self.wait_listener(spec["port"])
                return
            if item["State"]["Status"] not in {"exited", "created"}:
                raise CertificateError("容器尚未稳定或已暂停，请先停止网关再重试")
            import nginx_install
            nginx_install._check_ports([spec["port"]])
            run([*base, "--rm", item["Image"], "-t", "-c", "/gateway/nginx.conf"], timeout=20)
            run(["docker", "start", item["Id"]], timeout=30)
        else:
            if spec["network"] != "host":
                network = json.loads(run(["docker", "network", "inspect", spec["network"]], timeout=5))[0]
                if network.get("Driver") != "bridge":
                    raise CertificateError("请选择自定义 bridge 网络")
            import nginx_install
            nginx_install._check_ports([spec["port"]])
            try:
                run(["docker", "image", "inspect", spec["image"]], timeout=5)
            except CertificateError:
                try:
                    run(["docker", "pull", spec["image"]], timeout=180)
                except CertificateError as exc:
                    raise CertificateError("网关镜像拉取失败，请检查 Docker 镜像源或填写兼容镜像地址后重试") from exc
            run([*base, "--rm", spec["image"], "-t", "-c", "/gateway/nginx.conf"], timeout=20)
            ports = [] if spec["network"] == "host" else ["-p", f'{spec["bind"]}:{spec["port"]}:10000']
            run([*base, "-d", "--name", f'mini-gateway-{spec["key"]}', "--label", f'{LABEL}={entry["owner"]}',
                 "--restart", "unless-stopped", "--log-driver", "local", "--log-opt", "max-size=10m",
                 "--log-opt", "max-file=3", *ports, spec["image"], "-c", "/gateway/nginx.conf",
                 "-g", "daemon off;"], timeout=30)
        item = self.inspect(entry)
        if not item or not item["State"]["Running"]:
            raise CertificateError("网关未成功运行，请检查容器状态；配置已保留，可重试")
        run(["docker", "exec", item["Id"], "nginx", "-t", "-c", "/gateway/nginx.conf"], timeout=10)
        self.wait_listener(spec["port"])

    @staticmethod
    def wait_listener(port: int) -> None:
        for attempt in range(5):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    return
            except OSError:
                if attempt < 4:
                    time.sleep(0.2)
        raise CertificateError("网关监听端口尚不可达，请检查容器状态和端口映射；配置已保留")

    def operate(self, data: dict[str, Any]) -> dict[str, Any]:
        if data.get("confirmed") is not True:
            raise CertificateError("请确认网关操作")
        action = data.get("action")
        with LOCK:
            if action == "save":
                return self.save(data.get("spec"), data.get("revision", ""))
            if action == "start":
                self.recover(data.get("key"))
            entry = self.read(data.get("key"))
            if data.get("revision") != revision(entry["spec"]):
                raise CertificateError("配置已变化，请刷新后重试")
            if action == "start":
                self.start(entry)
            elif action in {"stop", "delete"}:
                from gateway_connections import Connections
                if Connections(self.root.parent).status(entry['spec']['key'])['state'] != 'not_connected':
                    raise CertificateError('此网关仍有网站接入或待恢复操作，请先撤销接入，再停止或删除')
                item = self.inspect(entry)
                if action == "stop" and item:
                    if item["State"]["Status"] == "paused":
                        run(["docker", "unpause", item["Id"]], timeout=10)
                    run(["docker", "stop", "--time", "30", item["Id"]], timeout=40)
                elif action == "delete":
                    if item and item["State"]["Status"] not in {"exited", "created", "dead"}:
                        raise CertificateError("请先切回原业务上游并停止网关，再删除入口")
                    if item:
                        run(["docker", "rm", item["Id"]], timeout=10)
                    directory = self.directory(entry["spec"]["key"])
                    for path in (directory / "conf" / "nginx.conf", directory / "entry.json", directory / "pending.json"):
                        trusted_path(path)
                        path.unlink(missing_ok=True)
                    (directory / "conf").rmdir()
                    directory.rmdir()
                    return {"deleted": True}
            else:
                raise CertificateError("未知网关操作")
            return self.describe(entry)

    def records(self, key: str, limit: int = 100) -> dict[str, Any]:
        if not 1 <= limit <= 300:
            raise CertificateError("请选择 1-300 条请求")
        with LOCK:
            entry = self.read(key)
            item = self.inspect(entry)
        if not item:
            return {"records": [], "notice": "入口尚未启动"}
        # Disk-backed capture avoids loading arbitrary Docker log lines into RAM.
        with tempfile.TemporaryFile() as stream:
            try:
                result = subprocess.run(["docker", "logs", "--tail", "1000", item["Id"]],
                                        stdout=stream, stderr=subprocess.STDOUT, timeout=8, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise CertificateError("读取网关记录超时或失败") from exc
            if result.returncode:
                raise CertificateError("无法读取网关日志")
            size = stream.tell()
            stream.seek(max(0, size - 2 * 1024 * 1024))
            lines = stream.read(2 * 1024 * 1024).decode("utf-8", errors="replace").splitlines()
        if size > 2 * 1024 * 1024:
            lines = lines[1:]
        records = [record for line in lines if (record := nginx_requests.parse(line))]
        return {"mode": "gateway", "container": f'mini-gateway-{key}', "records": records[-limit:][::-1],
                "notice": "请求结束后显示记录；长连接在关闭后记录。仅展示最近日志，不包含查询参数、请求体或认证头。"}


def networks() -> list[str]:
    nginx_runtime.require_local_docker()
    rows = run(["docker", "network", "ls", "--filter", "driver=bridge", "--format", "{{.Name}}"], timeout=5)
    return [name for name in rows.splitlines() if DOCKER_NAME.fullmatch(name) and name != "bridge"]
