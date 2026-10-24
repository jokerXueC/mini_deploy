"""Bounded, read-only Linux inventory and logs from discovered sources."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import socket
import stat
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

MAX_BYTES = 512 * 1024
MAX_LINES = 1000
UNIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@:\\-]{0,220}\.service")
CONTAINER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def run(command, timeout=5, *, max_bytes=None, binary=False):
    """Stop oversized output without buffering it or spooling to a server file."""
    max_bytes = MAX_BYTES if max_bytes is None else max_bytes
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          stdin=subprocess.DEVNULL, env={**os.environ, "LC_ALL": "C"}) as process:
        chunks, errors = [], []
        limited = threading.Event()

        def read():
            size = 0
            try:
                while chunk := process.stdout.read1(16384):
                    keep = min(len(chunk), max_bytes - size)
                    chunks.append(chunk[:keep])
                    size += keep
                    if len(chunk) > keep:
                        limited.set()
                        process.kill()
                        break
            except OSError as exc:
                errors.append(exc)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            raise RuntimeError("读取超时，请稍后重试") from exc
        finally:
            reader.join(timeout=2)
        if reader.is_alive() or errors:
            raise RuntimeError("无法完整读取命令输出")
        output = b"".join(chunks)
        return process.returncode, output if binary else output.decode("utf-8", "replace"), limited.is_set()


def safe_file(path):
    path = Path(path)
    if not path.is_absolute():
        return False
    try:
        for parent in [*reversed(path.parents), path]:
            if stat.S_ISLNK(parent.lstat().st_mode):
                return False
        info = path.stat()
        return stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    except OSError:
        return False


def redact(line):
    line = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|\Z)",
                  "[已隐藏私钥]", line, flags=re.S)
    line = re.sub(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@", r"\1[已隐藏]@", line)
    line = re.sub(r"(?im)^(\s*(?:set-cookie|cookie)\s*:\s*).+$", r"\1[已隐藏]", line)
    line = re.sub(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9+/_.=:-]+", r"\1 [已隐藏]", line)
    line = re.sub(r'(?i)((?:password|passwd|token|secret|api[_-]?key|authorization|cookie)[\"\s]*[:=]\s*)("[^"\n]*"|[^\s,;&]+)',
                  r"\1[已隐藏]", line)
    # Discard terminal control characters; log content is always displayed as text.
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", line)


def timestamp(line):
    match = re.match(r"\[?(\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:?\d\d)?)", line)
    if match:
        try:
            parsed = datetime.fromisoformat(match[1].replace("Z", "+00:00"))
            return parsed.timestamp()  # Naive file timestamps use the server's local timezone.
        except ValueError:
            pass
    match = re.search(r"\[(\d{2}/[A-Za-z]{3}/\d{4}:\d\d:\d\d:\d\d [+-]\d{4})\]", line)
    if match:
        try:
            return datetime.strptime(match[1], "%d/%b/%Y:%H:%M:%S %z").timestamp()
        except ValueError:
            pass
    if line.startswith("{"):
        try:
            data = json.loads(line)
            value = data.get("ts", data.get("time"))
            if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value):
                return value
            if isinstance(value, str):
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError, AttributeError):
            pass
    return None


class Operations:
    def __init__(self, *, runner=run, log_file=Path("/var/log/mini_deploy/mini-deploy-agent.log"),
                 service="mini-deploy-agent", log_roots=None, clock=time.time):
        self.run, self.log_file, self.service, self.clock = runner, Path(log_file), service, clock
        self.log_roots = tuple(Path(p) for p in (log_roots if log_roots is not None else
                              ("/var/log/nginx", "/var/log/caddy", "/var/log/mini_deploy")))
        self.lock = threading.RLock()
        self.cache, self.sources = {}, {}

    def _command(self, command):
        try:
            code, output, limited = self.run(command)
        except (OSError, RuntimeError) as exc:
            return "", False, str(exc)
        if code and not limited:
            return "", False, "读取失败：" + redact(output[-500:])
        return output, limited, ""

    def inventory(self, docker=None, sort="cpu", pid=None):
        if sort not in {"cpu", "memory"}:
            raise ValueError("进程排序无效")
        if pid is not None and (not isinstance(pid, int) or isinstance(pid, bool) or not 1 <= pid <= 2 ** 31):
            raise ValueError("进程 PID 无效")
        with self.lock:
            now = self.clock()
            if self.cache and now - self.cache["checked_at"] < 10:
                data = copy.deepcopy(self.cache)
            else:
                processes, ports, services, warnings = [], [], [], []
                output, limited, error = self._command([
                    "ps", "-eo", "pid=,ppid=,uid=,pcpu=,pmem=,rss=,comm=", "--sort=-pcpu"])
                for line in output.splitlines()[:4096]:
                    fields = line.split(None, 6)
                    if len(fields) != 7:
                        continue
                    try:
                        cpu, memory = float(fields[3]), float(fields[4])
                        if math.isfinite(cpu) and math.isfinite(memory):
                            processes.append(dict(pid=int(fields[0]), ppid=int(fields[1]), uid=int(fields[2]),
                                                  cpu=cpu, memory=memory,
                                                  rss_mib=round(int(fields[5]) / 1024, 1), name=fields[6][:160]))
                    except ValueError:
                        continue
                if error:
                    warnings.append("进程：" + error)
                if limited or len(processes) >= 4096:
                    warnings.append("进程列表达到读取上限")
                output, limited, error = self._command(["ss", "-H", "-lntup"])
                names = {p["pid"]: p["name"] for p in processes}
                for line in output.splitlines()[:4096]:
                    fields = line.split(None, 6)
                    if len(fields) < 6:
                        continue
                    owners = [{"pid": int(pid), "name": names.get(int(pid), name)}
                              for name, pid in re.findall(r'\("([^"\n]+)",pid=(\d+)', line)]
                    ports.append({"protocol": fields[0], "state": fields[1], "address": fields[4], "owners": owners})
                if error:
                    warnings.append("端口：" + error)
                if limited:
                    warnings.append("端口列表达到读取上限")
                output, limited, error = self._command([
                    "systemctl", "list-units", "--type=service", "--all", "--no-pager", "--output=json"])
                try:
                    units = json.loads(output or "[]")
                    if isinstance(units, list):
                        services = [{"unit": u["unit"], "state": str(u.get("active", "unknown"))}
                                    for u in units[:4096] if isinstance(u, dict) and UNIT.fullmatch(str(u.get("unit", "")))]
                except (ValueError, TypeError):
                    error = "服务清单不可用（需要支持 JSON 输出的 systemd）"
                if error:
                    warnings.append("服务：" + error)
                self.cache = {"checked_at": now, "hostname": socket.gethostname(), "processes": processes,
                              "ports": ports, "services": services, "warnings": warnings}
                data = copy.deepcopy(self.cache)
            self._sources(data["services"], docker or {})
            data["sources"] = [{k: v for k, v in s.items() if k != "path"} for s in self.sources.values()]
            data["process_count"] = len(data["processes"])
            data["processes"] = sorted([p for p in data["processes"] if pid is None or p["pid"] == pid],
                                       key=lambda p: p["cpu"] if sort == "cpu" else p["rss_mib"], reverse=True)[:100]
            for process in data["processes"]:
                try:
                    with open(f"/proc/{process['pid']}/cgroup", encoding="utf-8") as handle:
                        cgroup = handle.read(8192)
                    unit = re.search(r"/([^/\n]+\.service)(?:/|$)", cgroup)
                    container = re.search(r"(?:docker[-/])([a-f0-9]{64})(?:\.scope)?(?:/|$)", cgroup)
                    key = "container:" + container[1] if container else "service:" + unit[1] if unit else ""
                    if key in self.sources:
                        process["log_source"] = key
                except OSError:
                    pass
            data["cpu_semantics"] = "CPU 为进程存活期间的平均占用；100% 表示一个逻辑核"
            return data

    def _sources(self, services, docker):
        sources = {}
        units = {s["unit"] for s in services}
        if UNIT.fullmatch(self.service + ".service"):
            units.add(self.service + ".service")
        for unit in sorted(units):
            sources["service:" + unit] = {"id": "service:" + unit, "kind": "service", "name": unit}
        for c in (docker.get("containers") or [])[:500]:
            identifier = str(c.get("id") or c.get("name") or "")
            if CONTAINER.fullmatch(identifier):
                key = "container:" + identifier
                sources[key] = {"id": key, "kind": "container", "name": str(c.get("name") or identifier)[:160]}
        paths = {self.log_file}
        for root in self.log_roots:
            try:
                if root.is_symlink():
                    continue
                for index, path in enumerate(root.iterdir()):
                    if index >= 100:
                        break
                    if path.suffix in {".log", ".jsonl"}:
                        paths.add(path)
            except OSError:
                continue
        for path in sorted(paths):
            if safe_file(path):
                key = "file:" + hashlib.sha256(str(path).encode()).hexdigest()[:24]
                sources[key] = {"id": key, "kind": "file", "name": str(path), "path": path}
        self.sources = sources

    def logs(self, source, *, since=None, until=None, limit=300, keyword="", level="all"):
        if (not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LINES
                or level not in {"all", "error", "warning"} or len(keyword) > 160):
            raise ValueError("日志筛选参数无效")
        now = self.clock()
        if since is not None or until is not None:
            if (since is None or until is None or not math.isfinite(since) or not math.isfinite(until)
                    or not now - 7 * 86400 <= since <= until <= now + 300 or until - since > 86400):
                raise ValueError("请选择最近七天内、跨度不超过一天的日志时间范围")
        with self.lock:
            item = dict(self.sources.get(source) or {})
        if not item:
            raise ValueError("日志来源未发现或已变化，请刷新来源列表")
        limited, complete_range, ignored = False, False, 0
        if item["kind"] == "file":
            path = item["path"]
            if not safe_file(path):
                raise ValueError("日志文件已变化或不是独立普通文件，请刷新")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("日志来源不再是独立普通文件")
                limited = info.st_size > MAX_BYTES
                handle.seek(max(0, info.st_size - MAX_BYTES))
                raw = handle.read(MAX_BYTES)
            if limited:
                raw = raw.partition(b"\n")[2]
            lines = redact(raw.decode("utf-8", "replace")).splitlines()
        else:
            if item["kind"] == "container":
                command = ["docker", "logs", "--timestamps", "--tail", str(MAX_LINES)]
                if since is not None:
                    command += ["--since", datetime.fromtimestamp(since, timezone.utc).isoformat(),
                                "--until", datetime.fromtimestamp(until, timezone.utc).isoformat()]
                command += [source.removeprefix("container:")]
            else:
                command = ["journalctl", "--no-pager", "--output=short-iso-precise", "-n", str(MAX_LINES),
                           "-u", item["name"]]
                if since is not None:
                    command += ["--since", "@" + str(int(since)), "--until", "@" + str(int(until))]
            output, limited, error = self._command(command)
            if error:
                raise RuntimeError(error)
            lines = redact(output).splitlines()
            limited = limited or len(lines) >= MAX_LINES
            complete_range = not limited
        filtered = []
        for line in lines:
            if since is not None and item["kind"] == "file":
                ts = timestamp(line)
                if ts is None:
                    ignored += 1
                    continue
                if not since <= ts <= until:
                    continue
            line = redact(line)
            if keyword and keyword.casefold() not in line.casefold():
                continue
            if level == "error" and not re.search(r"error|failed|fatal|exception|traceback|panic|\b5\d\d\b", line, re.I):
                continue
            if level == "warning" and not re.search(r"warn|timeout|retry", line, re.I):
                continue
            filtered.append(line[:8000])
        truncated = limited or len(filtered) > limit
        return {"source": source, "name": item["name"], "lines": filtered[-limit:], "checked_at": now,
                "since": since, "until": until, "truncated": truncated, "ignored_untimed": ignored,
                "notice": "仅显示有界读取范围内的匹配日志；已轮转或被清理的日志无法还原。" if not complete_range or truncated
                          else "日志来自当前服务器；常见凭据字段已隐藏。"}
