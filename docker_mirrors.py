"""Host Docker registry mirrors with validated, recoverable configuration changes."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit

from certificates import CertificateError, atomic_write, run, trusted_path


def normalize(values):
    if not isinstance(values, list) or len(values) > 20:
        raise ValueError("最多填写 20 个镜像加速地址")
    result = []
    for value in values:
        if not isinstance(value, str) or len(value) > 2048:
            raise ValueError("镜像加速地址格式无效")
        value = value.strip()
        if not value:
            raise ValueError("地址不能为空，请填写或移除空行")
        try:
            parts = urlsplit(value)
            port = parts.port
            host = (parts.hostname or "").encode("idna").decode("ascii").lower()
        except (ValueError, UnicodeError) as exc:
            raise ValueError("加速地址的域名或端口无效") from exc
        if (parts.scheme not in {"http", "https"} or not host or port == 0 or parts.username is not None
                or parts.password is not None or parts.query or parts.fragment or parts.path not in {"", "/"}
                or any(ord(c) <= 32 for c in value) or "\\" in value):
            raise ValueError("请填写 HTTP/HTTPS 镜像站地址，不含账号密码、路径、查询参数或片段")
        authority = f"[{host}]" if ":" in host else host
        if port and port != (443 if parts.scheme == "https" else 80):
            authority += f":{port}"
        url = urlunsplit((parts.scheme, authority, "", "", ""))
        if url not in result:
            result.append(url)
    return result


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Docker 配置存在重复字段，请先核对原文件")
        result[key] = value
    return result


def read_config(path):
    trusted_path(path)
    if not path.exists():
        return "", {}, 0o600
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("Docker 配置必须是小于 1MB 的普通文件")
    raw = path.read_bytes().decode("utf-8")
    try:
        config = json.loads(raw, object_pairs_hook=_object)
    except ValueError as exc:
        raise ValueError("Docker 配置不是有效的 JSON 或存在重复字段；原文件未修改") from exc
    if not isinstance(config, dict):
        raise ValueError("Docker 配置根节点必须是 JSON 对象")
    normalize(config.get("registry-mirrors", []))
    return raw, config, stat.S_IMODE(path.stat().st_mode)


def local_endpoint():
    endpoint = os.environ.get("DOCKER_HOST", "")
    context = os.environ.get("DOCKER_CONTEXT", "")
    if context or not endpoint:
        if context and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", context):
            raise ValueError("Docker context 名称无效")
        endpoint = json.loads(run(["docker", "context", "inspect", *([context] if context else []),
                                   "--format", "{{json .Endpoints.docker.Host}}"], timeout=5))
    if endpoint not in {"unix:///var/run/docker.sock", "unix:///run/docker.sock"}:
        raise ValueError("当前连接不是主机默认 Docker Socket，暂不支持修改远程或自定义实例")
    return endpoint


def daemon_info(endpoint):
    result = json.loads(run(["docker", "--host", endpoint, "info", "--format", "{{json .}}"], timeout=8))
    return {"id": result.get("ID"), "mirrors": normalize((result.get("RegistryConfig") or {}).get("Mirrors") or []),
            "live_restore": bool(result.get("LiveRestoreEnabled")), "security": result.get("SecurityOptions") or []}


def daemon_arguments(arguments):
    path = "/etc/docker/daemon.json"
    for index, arg in enumerate(arguments[1:], 1):
        if arg == "--registry-mirror" or arg.startswith("--registry-mirror="):
            raise ValueError("镜像加速地址由 Docker 启动参数管理，不能同时写入 JSON 配置")
        if arg == "--rootless" or arg.startswith("--rootless="):
            raise ValueError("暂不支持 rootless Docker 配置修改")
        if arg in {"-H", "--host"} or arg.startswith(("--host=", "-H")):
            if "=" in arg:
                host = arg.split("=", 1)[1]
            elif arg.startswith("-H") and len(arg) > 2:
                host = arg[2:]
            else:
                host = arguments[index + 1] if index + 1 < len(arguments) else ""
            if host not in {"fd://", "unix:///var/run/docker.sock", "unix:///run/docker.sock"}:
                raise ValueError("Docker 使用自定义监听地址，无法确认当前连接与服务配置一致")
        if arg == "--config-file":
            if index + 1 >= len(arguments):
                raise ValueError("无法确定 Docker 配置文件")
            path = arguments[index + 1]
        elif arg.startswith("--config-file="):
            path = arg.split("=", 1)[1]
    if not PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
        raise ValueError("Docker 配置文件必须使用明确的绝对路径")
    return Path(path)


def inspect_host():
    if sys.platform != "linux" or getattr(os, "geteuid", lambda: -1)() != 0:
        raise ValueError("镜像加速配置需要主机上的 Linux root 权限")
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        raise ValueError("面板运行在容器中，无法安全修改主机 Docker 配置")
    if not shutil.which("docker") or not shutil.which("systemctl"):
        raise ValueError("未检测到 Docker 或 systemd，请先安装并启动主机 Docker")
    endpoint = local_endpoint()
    pid = run(["systemctl", "show", "docker.service", "--property=MainPID", "--value"], timeout=5).strip()
    if not pid.isdecimal() or int(pid) <= 1:
        raise ValueError("Docker 服务未运行，或不是由 docker.service 管理")
    process = Path("/proc") / pid
    arguments = [arg.decode("utf-8") for arg in (process / "cmdline").read_bytes().split(b"\0") if arg]
    executable = (process / "exe").resolve()
    if not arguments or executable.name != "dockerd" or process.stat().st_uid != 0:
        raise ValueError("无法确认 Docker 服务进程，已禁止修改")
    trusted_path(executable)
    path = daemon_arguments(arguments)
    raw, config, mode = read_config(path)
    configured_hosts = config.get("hosts", [])
    if not isinstance(configured_hosts, list) or any(not isinstance(host, str) or host not in {"fd://", "unix:///var/run/docker.sock", "unix:///run/docker.sock"} for host in configured_hosts):
        raise ValueError("Docker 配置使用自定义监听地址，暂不自动修改该实例")
    live = daemon_info(endpoint)
    if not live["id"] or any("rootless" in str(value) for value in live["security"]):
        raise ValueError("无法确认主机 Docker，或当前为 rootless Docker")
    # Compare the explicit default Socket with the CLI context before allowing a mutation.
    token = hashlib.sha256(json.dumps([str(path), raw, mode, live["id"], arguments], ensure_ascii=False).encode()).hexdigest()
    return {"editable": True, "path": str(path), "mirrors": normalize(config.get("registry-mirrors", [])),
            "effective_mirrors": live["mirrors"], "live_restore": live["live_restore"], "revision": token,
            "endpoint": endpoint, "executable": str(executable), "raw": raw, "config": config, "mode": mode,
            "daemon_id": live["id"], "arguments": arguments[1:]}


def validation_command(current, proposed):
    arguments = iter(current.get("arguments", []))
    clean = []
    for arg in arguments:
        if arg == "--config-file":
            next(arguments, None)
        elif not arg.startswith("--config-file="):
            clean.append(arg)
    return [current["executable"], *clean, "--config-file", str(proposed), "--validate"]


class Manager:
    def __init__(self, data_home, *, inspect=inspect_host, runner=run):
        self.home = Path(data_home)
        self.inspect, self.run = inspect, runner
        self.lock = threading.RLock()
        self.job = {"state": "idle"}
        self.worker = None
        self.record = self.home / "docker-mirrors-last.json"
        trusted_path(self.record)
        if self.record.exists():
            if self.record.stat().st_size > 16384:
                raise ValueError("镜像加速操作记录异常，请检查数据目录")
            self.job = json.loads(self.record.read_text(encoding="utf-8"))
            if not isinstance(self.job, dict) or self.job.get("state") not in {"idle", "running", "succeeded", "failed", "interrupted"}:
                raise ValueError("镜像加速操作记录异常，原文件已保留")
            if self.job.get("state") == "running":
                self.job.update(state="interrupted", detail="上次应用未完成，请重新读取当前配置；备份已保留")

    def status(self, *, job_only=False):
        with self.lock:
            job = dict(self.job)
        if job_only or job.get("state") == "running":
            return {"job": job}
        try:
            current = self.inspect()
            public = {key: current[key] for key in ("editable", "path", "mirrors", "effective_mirrors", "live_restore", "revision")}
            return {**public, "job": job}
        except (OSError, ValueError, RuntimeError) as exc:
            return {"editable": False, "detail": str(exc), "job": job}

    def _progress(self, state, detail, **extra):
        with self.lock:
            self.job.update(state=state, detail=detail, **extra)
            trusted_path(self.home)
            self.home.mkdir(parents=True, exist_ok=True)
            atomic_write(self.record, json.dumps(self.job, ensure_ascii=False))

    def start(self, values, revision, *, operation, audit):
        mirrors = normalize(values)
        if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision):
            raise ValueError("请先读取当前配置，再保存")
        with self.lock:
            if self.job.get("state") == "running" or (self.worker and self.worker.is_alive()):
                raise ValueError("正在应用配置，请等待完成")
            previous = self.job
            self.job = {"state": "running", "started_at": time.time(), "detail": "正在校验当前配置"}
            try:
                self._progress("running", "正在校验当前配置")
            except Exception:
                self.job = previous
                raise

            def work():
                ok = False
                try:
                    operation(lambda: self.apply(mirrors, revision))
                    ok = True
                except Exception as exc:
                    self._progress("failed", str(exc))
                finally:
                    audit(ok)

            self.worker = threading.Thread(target=work, name="docker-mirrors-apply", daemon=True)
            self.worker.start()
            return {"job": dict(self.job)}

    def _wait(self, current, mirrors):
        for _ in range(12):
            try:
                live = json.loads(self.run(["docker", "--host", current["endpoint"], "info", "--format", "{{json .}}"], timeout=3))
                if live.get("ID") == current["daemon_id"] and normalize((live.get("RegistryConfig") or {}).get("Mirrors") or []) == mirrors:
                    return True
            except (OSError, ValueError, RuntimeError):
                pass
            time.sleep(0.5)
        return False

    def _activate(self, current, mirrors):
        self._progress("running", "正在热加载镜像加速配置")
        try:
            self.run(["systemctl", "kill", "--kill-whom=main", "--signal=HUP", "docker.service"], timeout=10)
            if self._wait(current, mirrors):
                return "热加载"
        except CertificateError:
            pass
        running = self.run(["docker", "--host", current["endpoint"], "ps", "--quiet"], timeout=8).strip()
        if running and not current["live_restore"]:
            raise ValueError("热加载未生效；有运行中容器且未开启 live-restore，未自动重启 Docker，以免停止业务")
        self._progress("running", "热加载未生效，正在重启 Docker")
        self.run(["systemctl", "restart", "docker.service"], timeout=90)
        if not self._wait(current, mirrors):
            raise ValueError("Docker 未恢复正常或实际加速地址与保存值不一致")
        return "重启 Docker"

    def apply(self, mirrors, revision):
        current = self.inspect()
        if current["revision"] != revision:
            raise ValueError("Docker 配置或启动参数已经变化，请重新读取后保存")
        path = Path(current["path"])
        if mirrors == current["mirrors"] == current["effective_mirrors"]:
            self._progress("succeeded", "配置没有变化，当前已生效；未重启 Docker")
            return
        candidate = dict(current["config"])
        if mirrors:
            candidate["registry-mirrors"] = mirrors
        else:
            candidate.pop("registry-mirrors", None)
        content = json.dumps(candidate, ensure_ascii=False, indent=2) + "\n"
        backups = self.home / "backups" / "docker-mirrors"
        trusted_path(backups)
        backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = Path(tempfile.mkdtemp(prefix="change-", dir=backups))
        atomic_write(backup / "before.json", current["raw"] or "{}\n")
        atomic_write(backup / "metadata.json", json.dumps({"path": str(path), "existed": bool(current["raw"]), "mode": current["mode"]}))
        proposed = backup / "proposed.json"
        atomic_write(proposed, content)
        self._progress("running", "正在校验新配置，原配置已备份", backup=str(backup))
        try:
            self.run(validation_command(current, proposed), timeout=15)
        except CertificateError as exc:
            raise CertificateError("Docker 配置校验失败，原文件未修改。请检查配置与启动参数是否冲突，或当前版本是否支持 --validate") from exc
        # Validation can take time; do not overwrite edits made outside the panel.
        if self.inspect()["revision"] != revision:
            raise ValueError("校验期间配置发生变化，未覆盖原文件，请重新读取")
        trusted_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, content)
        try:
            os.chmod(path, current["mode"])
            method = self._activate(current, mirrors)
            self._progress("succeeded", f"配置已保存，通过{method}生效；尚未验证实际镜像拉取", activation=method)
        except Exception as exc:
            try:
                unchanged = read_config(path)[0] == content
            except (OSError, ValueError):
                unchanged = False
            if not unchanged:
                raise ValueError("应用失败且配置被其他操作修改，未覆盖该修改；请检查 Docker 服务，原配置备份已保留") from exc
            atomic_write(path, current["raw"] or "{}\n")
            os.chmod(path, current["mode"])
            try:
                self._activate(current, current["effective_mirrors"])
            except Exception as recovery:
                self._progress("failed", "原配置已写回，但 Docker 未恢复，请检查 systemctl status docker；备份已保留", rollback="failed")
                raise ValueError(self.job["detail"]) from recovery
            self._progress("failed", f"{exc}。已恢复原配置及 Docker；可重新读取后重试", rollback="succeeded")
            raise ValueError(self.job["detail"]) from exc
