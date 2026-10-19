"""Bounded, owner-scoped SSH PTYs. Credentials and terminal traffic stay in memory."""
from __future__ import annotations

import base64
from collections import deque
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import tempfile
import threading
import time

import ssh_files
import ssh_file_manager

try:
    import paramiko
except ImportError:
    paramiko = None


class SSHError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def target(data):
    host = str(data.get("host", "")).strip().strip("[]")
    username = str(data.get("username", "")).strip()
    if not re.fullmatch(r"[a-zA-Z0-9_.:%-]{1,253}", host):
        raise SSHError("请填写服务器 IP 或主机名，不要包含协议、路径或端口。")
    if not username or len(username) > 128 or any(ord(c) < 33 for c in username):
        raise SSHError("请填写有效的 SSH 用户名，例如 root 或 ubuntu。")
    try:
        port = int(data.get("port", 22))
    except (TypeError, ValueError):
        raise SSHError("SSH 端口必须为 1–65535。") from None
    if not 1 <= port <= 65535:
        raise SSHError("SSH 端口必须为 1–65535。")
    return {"host": host.lower(), "port": port, "username": username}


def dimensions(data):
    try:
        cols, rows = int(data.get("cols", 100)), int(data.get("rows", 30))
    except (ValueError, TypeError):
        raise SSHError("终端尺寸无效。") from None
    if not 20 <= cols <= 300 or not 5 <= rows <= 120:
        raise SSHError("终端尺寸超出范围。")
    return cols, rows


def fingerprint(key):
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


def endpoint(spec):
    return f"[{spec['host']}]:{spec['port']}"


def secure_request(peer, headers, *, trusted_proxy=False, tls=False):
    """Never trust client-supplied proxy headers on a public HTTP socket."""
    import ipaddress
    from urllib.parse import urlsplit
    try:
        loopback = ipaddress.ip_address(peer).is_loopback
        host = urlsplit("//" + headers.get("Host", "")).hostname or ""
    except ValueError:
        return False
    proto = headers.get("X-Forwarded-Proto", "").strip().lower()
    if tls or (trusted_proxy and loopback and proto == "https"):
        return True
    try:
        local_host = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        local_host = host == "localhost"
    return loopback and local_host and not proto and not headers.get("Forwarded") and not headers.get("X-Real-IP")


class Session:
    def __init__(self, owner, spec, transport, channel, expires, audit):
        self.owner, self.spec = owner, spec
        self.transport, self.channel = transport, channel
        self.expires, self.audit = expires, audit
        self.id = secrets.token_urlsafe(32)
        self.condition = threading.Condition()
        self.io_lock, self.poll_lock = threading.Lock(), threading.Lock()
        self.chunks, self.size, self.sequence = deque(), 0, 0
        self.closed, self.reason, self.closed_at = False, "", 0
        self.activity = self.last_poll = time.monotonic()
        self.files = ssh_file_manager.Files(self)
        self.file_slots = threading.BoundedSemaphore(2)

    def start(self):
        threading.Thread(target=self._read, name="ssh-pty-reader", daemon=True).start()

    def _read(self):
        try:
            while not self.closed:
                try:
                    chunk = self.channel.recv(16384)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                with self.condition:
                    self.activity = time.monotonic()
                    self.sequence += 1
                    self.chunks.append((self.sequence, chunk))
                    self.size += len(chunk)
                    while self.size > 256 * 1024:
                        self.size -= len(self.chunks.popleft()[1])
                    self.condition.notify_all()
        except Exception:
            self.close("远程连接中断，请重新连接。")
        finally:
            self.close("远程会话已结束。")

    def close(self, reason):
        self.files.close()
        with self.condition:
            if self.closed:
                return
            self.closed, self.reason, self.closed_at = True, reason, time.monotonic()
            self.condition.notify_all()
        try:
            self.transport.close()
        except (OSError, EOFError):
            pass
        finally:
            try:
                self.channel.close()
            except (OSError, EOFError):
                pass
            self.audit("ssh_disconnect", self.spec)

    def poll(self, cursor, wait=20):
        if not self.poll_lock.acquire(blocking=False):
            raise SSHError("该终端已有正在读取的请求。", 409)
        try:
            return self._poll(cursor, wait)
        finally:
            self.poll_lock.release()

    def _poll(self, cursor, wait):
        with self.condition:
            if cursor < 0 or cursor > self.sequence:
                raise SSHError("终端读取位置无效。")
            self.last_poll = time.monotonic()
            self.condition.wait_for(lambda: self.sequence > cursor or self.closed, timeout=wait)
            self.last_poll = time.monotonic()
            gap = bool(self.chunks and cursor < self.chunks[0][0] - 1)
            chunks = [chunk for seq, chunk in self.chunks if seq > cursor]
            return {"data": base64.b64encode(b"".join(chunks)).decode(), "cursor": self.sequence,
                    "closed": self.closed, "reason": self.reason, "gap": gap}

    def stream(self, cursor, duration=25):
        if not self.poll_lock.acquire(blocking=False):
            raise SSHError("该终端已有正在读取的请求。", 409)
        try:
            deadline = time.monotonic() + duration
            result = self._poll(cursor, 0)
            while True:
                yield result
                remaining = deadline - time.monotonic()
                if result["closed"] or remaining <= 0:
                    return
                result = self._poll(result["cursor"], min(1, remaining))
        finally:
            self.poll_lock.release()

    def write(self, value):
        if not isinstance(value, str) or len(value) > 24000:
            raise SSHError("单次输入过大，请分段粘贴。")
        try:
            raw = base64.b64decode(value, validate=True)
        except ValueError:
            raise SSHError("终端输入编码无效。") from None
        if len(raw) > 16384:
            raise SSHError("单次输入过大，请分段粘贴。")
        with self.io_lock:
            if self.closed:
                raise SSHError(self.reason, 410)
            try:
                self.channel.sendall(raw)
                self.activity = time.monotonic()
            except Exception:
                self.close("发送失败，连接已关闭。")
                raise SSHError("输入未能完整发送，请重新连接并核对远程状态。", 502) from None

    def resize(self, data):
        cols, rows = dimensions(data)
        with self.io_lock:
            if self.closed:
                raise SSHError(self.reason, 410)
            try:
                self.channel.resize_pty(width=cols, height=rows)
            except Exception:
                raise SSHError("调整终端尺寸失败，请检查连接。", 502) from None

    def execute_plan(self, plan, cancel, *, timeout=20):
        if plan.get('transport') != 'sftp':
            if plan.get('name') == 'execute_command':
                return self.execute_tool(plan['command'], cancel, timeout=plan['timeout_seconds'], max_bytes=65536)
            return self.execute_tool(plan['command'], cancel, timeout=timeout)
        return self.execute_sftp(lambda client, check: ssh_files.execute(client, plan, check), cancel, timeout=timeout)

    def execute_sftp(self, operation, cancel, *, timeout=20):
        if not self.file_slots.acquire(blocking=False):
            raise SSHError('已有两个文件通道正在使用，请稍后重试', 409)
        channel, client, finished = None, None, threading.Event()
        deadline = time.monotonic() + timeout
        def check():
            if self.closed or time.time() >= self.expires or cancel.is_set():
                raise SSHError("连接已关闭、登录已过期或任务已停止。", 410)
            if time.monotonic() >= deadline:
                raise SSHError("文件操作超时；若已提交修改，请核对原文件和备份。", 504)
        try:
            check()
            channel = self.transport.open_session(timeout=min(5, timeout))
            def watch():
                while not finished.wait(.05):
                    try:
                        check()
                    except SSHError:
                        channel.close()
                        return
            threading.Thread(target=watch, name='ssh-files-watch', daemon=True).start()
            check()
            channel.settimeout(min(5, timeout))
            channel.invoke_subsystem('sftp')
            client = paramiko.SFTPClient(channel)
            result = operation(client, check)
            check()
            self.activity = time.monotonic()
            return {'exit_code': 0, **result}
        except (SSHError, ssh_files.FileError):
            raise
        except Exception:
            check()
            raise SSHError("SFTP 操作失败，请核对服务器是否启用 SFTP、路径和账号权限；"
                           "未自动重试。若已提交修改，请检查原文件及保留的备份/候选文件。", 502) from None
        finally:
            finished.set()
            try:
                if client is not None:
                    client.close()
            finally:
                try:
                    if channel is not None:
                        channel.close()
                finally:
                    self.file_slots.release()

    def execute_tool(self, command, cancel, *, timeout=20, max_bytes=24000):
        """Run a backend-built command on a separate channel, never in the PTY."""
        if self.closed or time.time() >= self.expires or cancel.is_set():
            raise SSHError("连接已关闭、登录已过期或任务已停止。", 410)
        channel, finished = None, threading.Event()
        started = time.monotonic()
        deadline = started + timeout
        output, truncated = bytearray(), False
        try:
            channel = self.transport.open_session(timeout=min(5, timeout))
            def watch():
                while not finished.wait(.05):
                    if cancel.is_set() or self.closed or time.time() >= self.expires or time.monotonic() >= deadline:
                        channel.close()
                        return
            threading.Thread(target=watch, name="ssh-tool-watch", daemon=True).start()
            if cancel.is_set() or self.closed:
                raise SSHError("任务已停止或连接已关闭。", 410)
            channel.settimeout(.2)
            channel.exec_command(command)
            channel.shutdown_write()
            while True:
                if cancel.is_set() or self.closed or time.time() >= self.expires:
                    raise SSHError("任务已停止或连接已关闭；已提交的操作可能仍在远端执行。", 410)
                if time.monotonic() >= deadline:
                    raise SSHError("服务器检查超时；已提交的操作可能仍在远端执行，请核对状态后再操作。", 504)
                read = False
                for ready, receive in ((channel.recv_ready, channel.recv), (channel.recv_stderr_ready, channel.recv_stderr)):
                    if ready():
                        piece = receive(8192)
                        remaining = max_bytes - len(output)
                        output.extend(piece[:remaining])
                        truncated |= len(piece) > remaining
                        read = True
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break
                if not read:
                    cancel.wait(.03)
            self.activity = time.monotonic()
            return {"output": output.decode("utf-8", errors="replace"), "truncated": truncated,
                    "exit_code": channel.recv_exit_status() if channel.exit_status_ready() else None,
                    "duration_ms": round((time.monotonic() - started) * 1000, 2)}
        except SSHError:
            raise
        except Exception:
            if cancel.is_set() or self.closed:
                raise SSHError("任务已停止或连接已关闭。", 410) from None
            if time.monotonic() >= deadline:
                raise SSHError("服务器检查超时，请核对远端状态。", 504) from None
            raise SSHError("SSH 工具执行失败，请检查连接与账号权限；操作不会自动重试。", 502) from None
        finally:
            finished.set()
            if channel is not None:
                channel.close()


class Manager:
    def __init__(self, directory: Path, audit=None):
        self.directory = directory
        self.lock = threading.RLock()
        self.sessions, self.tickets, self.connecting, self.attempts = {}, {}, {}, {}
        self.revoked = {}
        self.audit = audit or (lambda *_: None)
        self.stop = threading.Event()
        self.worker = None
        self.profiles, self.known = [], {}
        self._load()

    def _load(self):
        path = self.directory / "connections.json"
        if not path.exists():
            return
        self._check_path(path)
        try:
            if path.stat().st_size > 128 * 1024:
                raise ValueError()
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("known", {}), dict):
                raise ValueError()
            profiles = data.get("profiles", [])
            if not isinstance(profiles, list) or len(profiles) > 50:
                raise ValueError()
            self.profiles = [dict(target(p), id=str(p["id"]), name=str(p.get("name", ""))[:80]) for p in profiles]
            self.known = data.get("known", {})
        except (ValueError, KeyError, TypeError):
            raise SSHError("SSH 连接配置无法读取，原文件已保留。", 503) from None

    def _check_path(self, path):
        for item in (self.directory, path):
            if item.is_symlink():
                raise SSHError("SSH 配置路径不能为符号链接。", 503)
            if item.exists() and os.name != "nt":
                st = item.stat()
                if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o077:
                    raise SSHError("SSH 配置权限不安全，请设置目录 700、文件 600。", 503)

    def _save(self, profiles, known):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.directory / "connections.json"
        self._check_path(path)
        fd, temporary = tempfile.mkstemp(dir=self.directory, prefix=".connections-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"profiles": profiles, "known": known}, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self.profiles, self.known = profiles, known

    def status(self, owner):
        with self.lock:
            self.check_owner(owner)
            return {"available": paramiko is not None, "profiles": list(self.profiles),
                    "sessions": [{"id": s.id, **s.spec} for s in self.sessions.values() if s.owner == owner and not s.closed],
                    "idle_minutes": 1440, "csrf_token": ""}

    def profile(self, data):
        with self.lock:
            key = str(data.get("id", ""))
            profiles = [p for p in self.profiles if p["id"] != key]
            if data.get("action") == "save":
                if len(profiles) >= 50:
                    raise SSHError("最多保存 50 个连接。")
                spec = target(data)
                profiles.append({**spec, "id": key if any(p["id"] == key for p in self.profiles) else secrets.token_hex(12),
                                 "name": str(data.get("name", "")).strip()[:80] or spec["host"]})
            elif data.get("action") != "delete":
                raise SSHError("不支持的连接操作。")
            self._save(profiles, self.known)
            return {"profiles": list(self.profiles)}

    def _open(self, spec):
        if paramiko is None:
            raise SSHError("尚未安装 SSH 依赖，请重新运行安装脚本，或安装 python3-paramiko 后重启面板。", 503)
        transport = None
        sock = None
        try:
            sock = socket.create_connection((spec["host"], spec["port"]), timeout=8)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            transport = paramiko.Transport(sock)
            transport.banner_timeout = transport.auth_timeout = 8
            transport.start_client(timeout=8)
            key = transport.get_remote_server_key()
            return transport, fingerprint(key), key.get_name()
        except Exception:
            if transport:
                transport.close()
            elif sock:
                sock.close()
            raise SSHError("无法连接 SSH：请检查 IP、端口、防火墙及 SSH 服务；目标需能从面板服务器访问。", 502) from None

    def _reserve(self, owner):
        with self.lock:
            self.check_owner(owner)
            if self.stop.is_set():
                raise SSHError("终端服务正在关闭。", 503)
            self._cleanup()
            now = time.monotonic()
            hits = [t for t in self.attempts.get(owner, []) if now - t < 60]
            if len(hits) >= 12:
                raise SSHError("连接尝试过于频繁，请稍后再试。", 429)
            live = [s for s in self.sessions.values() if not s.closed]
            total = sum(self.connecting.values())
            if len(live) + total >= 12 or sum(s.owner == owner for s in live) + self.connecting.get(owner, 0) >= 4:
                raise SSHError("终端连接已达上限，请先断开已有连接。", 429)
            if len(self.attempts) >= 128 and owner not in self.attempts:
                raise SSHError("连接尝试过多，请稍后再试。", 429)
            self.attempts[owner] = hits + [now]
            self.connecting[owner] = self.connecting.get(owner, 0) + 1
            if self.worker is None:
                self.worker = threading.Thread(target=self._watch, name="ssh-session-cleanup", daemon=True)
                self.worker.start()

    def _release(self, owner):
        with self.lock:
            self.connecting[owner] -= 1
            if not self.connecting[owner]:
                del self.connecting[owner]

    def probe(self, owner, data):
        spec = target(data)
        self._reserve(owner)
        try:
            transport, fp, kind = self._open(spec)
            transport.close()
            with self.lock:
                if len(self.tickets) >= 24:
                    raise SSHError("待确认连接过多，请稍后重试。", 429)
                ticket = secrets.token_urlsafe(32)
                expected = self.known.get(endpoint(spec), "")
                state = "known" if expected == fp else "changed" if expected else "new"
                self.tickets[ticket] = {"owner": owner, "spec": spec, "fingerprint": fp,
                                        "expected": expected, "time": time.monotonic()}
                return {"ticket": ticket, "fingerprint": fp, "algorithm": kind, "state": state,
                        "expected": expected, **spec}
        finally:
            self._release(owner)

    def connect(self, owner, data, expires):
        cols, rows = dimensions(data)
        method = data.get("auth", "password")
        if method not in {"password", "key"}:
            raise SSHError("请选择密码或私钥登录。")
        self._reserve(owner)
        transport = channel = None
        try:
            with self.lock:
                ticket_key = str(data.get("ticket", ""))
                ticket = self.tickets.get(ticket_key)
                if not ticket or ticket["owner"] != owner or time.monotonic() - ticket["time"] > 120:
                    raise SSHError("主机验证已过期，请重新检查服务器。", 409)
                del self.tickets[ticket_key]
                spec, fp = ticket["spec"], ticket["fingerprint"]
                expected = self.known.get(endpoint(spec), "")
                if expected != ticket["expected"]:
                    raise SSHError("主机指纹记录已变化，请重新检查。", 409)
                if expected != fp and data.get("accept_host_key") is not True:
                    raise SSHError("请核对并确认主机指纹后再连接。", 409)
            transport, actual, _ = self._open(spec)
            if actual != fp:
                raise SSHError("服务器指纹在连接前发生变化，已阻止凭据发送，请重新核对。", 409)
            self.check_owner(owner)
            if self.stop.is_set() or time.time() >= expires:
                raise SSHError("面板登录已过期或服务正在关闭，请重新登录。", 401)
            if method == "password":
                password = data.get("password", "")
                if not isinstance(password, str) or not password or len(password) > 4096:
                    raise SSHError("请输入 SSH 密码。")
                transport.auth_password(spec["username"], password, fallback=False)
            else:
                private = data.get("private_key", "")
                passphrase = data.get("passphrase", "")
                if not isinstance(private, str) or len(private) > 32768 or not isinstance(passphrase, str) or len(passphrase) > 4096:
                    raise SSHError("私钥内容或口令无效。")
                key = None
                for key_class in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
                    try:
                        key = key_class.from_private_key(io.StringIO(private), password=passphrase or None)
                        break
                    except (paramiko.SSHException, ValueError):
                        continue
                if key is None:
                    raise SSHError("无法读取私钥：请检查 OpenSSH/PEM 格式及私钥口令。")
                transport.auth_publickey(spec["username"], key)
            if not transport.is_authenticated():
                raise SSHError("SSH 认证未完成，请检查账号及认证方式。", 401)
            channel = transport.open_session(timeout=8)
            channel.set_combine_stderr(True)
            channel.settimeout(8)
            channel.get_pty(term="xterm-256color", width=cols, height=rows)
            channel.invoke_shell()
            channel.settimeout(0.25)
            transport.set_keepalive(20)
            session = Session(owner, spec, transport, channel, expires, self.audit)
            with self.lock:
                self.check_owner(owner)
                if self.stop.is_set():
                    raise SSHError("终端服务正在关闭。", 503)
                if self.known.get(endpoint(spec), "") not in {expected, fp}:
                    raise SSHError("主机指纹已被其他连接更新，请重新核对。", 409)
                known = dict(self.known)
                known[endpoint(spec)] = fp
                if len(known) > 200:
                    raise SSHError("主机指纹记录已达上限。")
                self._save(self.profiles, known)
                self.sessions[session.id] = session
            session.start()
            self.audit("ssh_connect", spec)
            transport = channel = None
            return {"id": session.id, **spec}
        except SSHError:
            raise
        except Exception:
            self.audit("ssh_connect_failed", {})
            raise SSHError("SSH 登录或终端启动失败，请检查账号、密码/私钥、服务器认证策略及是否允许交互终端。", 502) from None
        finally:
            if channel is not None:
                channel.close()
            if transport is not None:
                transport.close()
            self._release(owner)

    def session(self, owner, key):
        with self.lock:
            self.check_owner(owner)
            result = self.sessions.get(str(key))
            if result is None or result.owner != owner:
                raise SSHError("终端会话不存在或已过期。", 404)
            return result

    def check_owner(self, owner):
        with self.lock:
            self.revoked = {k: until for k, until in self.revoked.items() if until > time.time()}
            if owner in self.revoked:
                raise SSHError("此登录已退出，请重新登录面板。", 401)
            if len(self.revoked) >= 4096:
                raise SSHError("终端登录保护记录已达上限，请稍后重试。", 503)

    def close_owner(self, owner, expires=None):
        with self.lock:
            self.revoked[owner] = expires if expires is not None else time.time() + 24 * 3600
            for session in self.sessions.values():
                if session.owner == owner:
                    session.close("已退出面板，连接已关闭。")
            self.tickets = {k: v for k, v in self.tickets.items() if v["owner"] != owner}

    def _cleanup(self):
        now = time.monotonic()
        self.tickets = {k: v for k, v in self.tickets.items() if now - v["time"] < 120}
        self.attempts = {k: v for k, v in self.attempts.items() if v and now - v[-1] < 60}
        for key, session in list(self.sessions.items()):
            if time.time() >= session.expires:
                session.close("面板登录已过期，请重新登录。")
            elif now - session.last_poll > 24 * 3600:
                session.close("页面已离线，连接已关闭。")
            elif now - session.activity > 24 * 3600:
                session.close("空闲超过 24 小时，连接已关闭。")
            if session.closed and now - session.closed_at > 60:
                del self.sessions[key]

    def _watch(self):
        while not self.stop.wait(5):
            with self.lock:
                self._cleanup()
                for session in self.sessions.values():
                    with session.files.lock:
                        session.files.purge()

    def shutdown(self):
        self.stop.set()
        with self.lock:
            for session in self.sessions.values():
                session.close("面板服务已关闭。")
        if self.worker:
            self.worker.join(timeout=2)
