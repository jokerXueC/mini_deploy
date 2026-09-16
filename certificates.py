"""Project-scoped PEM certificates and transactional Nginx activation."""

from __future__ import annotations

import json
import os
import re
import secrets
import ssl
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


MARKER = "# mini-deploy-managed: project-certificate-v1"
MAX_PEM_BYTES = 128 * 1024


class CertificateError(ValueError):
    pass


def trusted_path(path: Path) -> None:
    """Reject links and writable ancestors before touching root-owned files."""
    for item in reversed((path, *path.parents)):
        if item.is_symlink():
            raise CertificateError("证书或 Nginx 路径不能包含符号链接")
        if item.exists():
            info = item.stat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise CertificateError("路径必须是普通文件或目录")
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise CertificateError("证书或 Nginx 文件不能包含硬链接")
            sticky_parent = item != path and stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX
            if os.name == "posix" and (info.st_uid not in {0, os.geteuid()} or (info.st_mode & 0o022 and not sticky_parent)):
                raise CertificateError("证书或 Nginx 路径的所有权或写权限不安全")


def atomic_write(path: Path, content: str) -> None:
    trusted_path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".certificate-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(command: list[str], *, timeout: float = 25) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CertificateError(f"{command[0]} 无法执行或超时，请检查服务器环境") from exc
    if result.returncode:
        # Never return tool output: it may contain credentials from nginx -T.
        raise CertificateError(f"{command[0]} {command[1]} 检查或执行失败，请检查服务器日志")
    return result.stdout


def inspect_pair(cert: Path, key: Path, domain: str) -> dict[str, Any]:
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key), password=lambda: "")
    except (ssl.SSLError, OSError) as exc:
        raise CertificateError("证书或私钥无效、不匹配，或私钥已加密；请上传 PEM 完整证书链及未加密私钥") from exc
    # OpenSSL checkhost prints a mismatch but some versions exit successfully.
    match = run(["openssl", "x509", "-in", str(cert), "-noout", "-checkhost", domain])
    if "does match certificate" not in match:
        raise CertificateError("证书不包含当前项目域名")
    output = run([
        "openssl", "x509", "-in", str(cert), "-noout", "-startdate", "-enddate",
        "-subject", "-issuer", "-fingerprint", "-sha256",
    ])
    fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    try:
        starts = ssl.cert_time_to_seconds(fields["notBefore"])
        expires = ssl.cert_time_to_seconds(fields["notAfter"])
    except (KeyError, ValueError) as exc:
        raise CertificateError("无法读取证书有效期") from exc
    if not starts <= time.time() < expires:
        raise CertificateError("证书尚未生效或已经过期")
    return {
        "domain": domain, "subject": fields.get("subject", ""), "issuer": fields.get("issuer", ""),
        "not_before": fields["notBefore"], "not_after": fields["notAfter"], "expires_at": expires,
        "fingerprint": fields.get("sha256 Fingerprint", fields.get("SHA256 Fingerprint", "")),
    }


class CertificateStore:
    def __init__(self, root: Path, runtime: Any = None):
        self.root = root
        self.runtime = runtime

    def command(self, action: str) -> list[str]:
        if self.runtime is not None:
            return self.runtime.command(action)
        return {"test": ["nginx", "-t"], "reload": ["systemctl", "reload", "nginx"], "dump": ["nginx", "-T"]}[action]

    def visible_path(self, path: Path) -> str:
        return self.runtime.visible_path(path) if self.runtime is not None else path.as_posix()

    def directory(self, project: Any) -> Path:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", project.key):
            raise CertificateError("项目标识无效")
        directory = self.root / project.key
        trusted_path(directory)
        return directory

    def read(self, project: Any) -> dict[str, Any] | None:
        path = self.directory(project) / "current.json"
        trusted_path(path)
        if not path.exists():
            return None
        try:
            if path.stat().st_size > 16384:
                raise ValueError("oversize")
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict) or not re.fullmatch(r"[a-f0-9]{24}", str(record.get("revision", ""))):
                raise ValueError("invalid revision")
            if not isinstance(record.get("expires_at"), (int, float)) or not 0 < record["expires_at"] < 32503680000:
                raise ValueError("invalid expiration")
            if any(not isinstance(record.get(field), str) for field in (
                "label", "domain", "subject", "issuer", "not_before", "not_after", "fingerprint",
            )):
                raise ValueError("invalid metadata")
            return record
        except (ValueError, OSError) as exc:
            raise CertificateError("证书记录损坏，请先检查数据目录") from exc

    def paths(self, project: Any, record: dict[str, Any]) -> tuple[Path, Path]:
        directory = self.directory(project) / record["revision"]
        cert, key = directory / "fullchain.pem", directory / "privkey.pem"
        trusted_path(cert)
        trusted_path(key)
        if key.exists() and os.name == "posix" and key.stat().st_mode & 0o077:
            raise CertificateError("证书私钥必须使用 0600 权限")
        return cert, key

    def config(self, path: Path) -> str:
        trusted_path(path)
        if not path.exists():
            return ""
        if path.stat().st_size > 256 * 1024:
            raise CertificateError("Nginx 配置过大，拒绝覆盖")
        return path.read_text(encoding="utf-8")

    def active(self, project: Any, record: dict[str, Any], conf: str) -> bool:
        cert, key = self.paths(project, record)
        return self.visible_path(cert) in conf or self.visible_path(key) in conf

    def describe(self, project: Any, conf_path: Path) -> dict[str, Any]:
        record = self.read(project)
        if not record:
            return {"project": project.key, "name": project.name, "domain": project.app_domain, "certificate": None}
        public = {key: record.get(key) for key in (
            "label", "domain", "subject", "issuer", "not_before", "not_after", "expires_at", "fingerprint",
        )}
        public["active"] = self.active(project, record, self.config(conf_path))
        public["days_left"] = int((float(record["expires_at"]) - time.time()) // 86400)
        return {"project": project.key, "name": project.name, "domain": project.app_domain, "certificate": public}

    def commit_config(self, path: Path, content: str, previous: str) -> None:
        trusted_path(path.parent)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, content)
        try:
            run(self.command("test"))
            run(self.command("reload"))
        except CertificateError as exc:
            if previous:
                atomic_write(path, previous)
            else:
                path.unlink()
            try:
                run(self.command("test"))
                run(self.command("reload"))
            except CertificateError as rollback_error:
                raise CertificateError("旧配置已写回，但 Nginx 恢复加载失败，请立即检查服务；证书文件已保留") from rollback_error
            raise CertificateError("Nginx 检查或加载失败，已恢复原配置") from exc

    def https_config(self, project: Any, record: dict[str, Any], http: str) -> str:
        cert, key = self.paths(project, record)
        if any(char in cert.as_posix() for char in ('"', '$', '\n', '\r', '\\')):
            raise CertificateError("证书目录包含不支持的 Nginx 路径字符")
        tls = http.replace("    listen 80;", "\n".join([
            "    listen 443 ssl;", f'    ssl_certificate "{self.visible_path(cert)}";',
            f'    ssl_certificate_key "{self.visible_path(key)}";', "    ssl_protocols TLSv1.2 TLSv1.3;",
        ]), 1)
        return MARKER + "\n" + "\n".join([
            "server {", "    listen 80;", f"    server_name {project.app_domain};",
            f"    return 301 https://{project.app_domain}$request_uri;", "}", tls,
        ])

    def operate(self, project: Any, action: str, data: dict[str, Any], conf_path: Path, http: str) -> None:
        if not isinstance(action, str) or action not in {"upload", "enable", "disable", "rename", "delete"}:
            raise CertificateError("不支持的证书操作")
        if not re.fullmatch(r"(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+", project.app_domain):
            raise CertificateError("请先为项目填写有效的业务域名")
        if not 1 <= project.service_port <= 65535:
            raise CertificateError("项目端口无效")
        record = self.read(project)
        previous = self.config(conf_path)
        active = bool(record and self.active(project, record, previous))
        directory = self.directory(project)
        metadata = directory / "current.json"
        if action in {"enable", "disable"} or (action == "upload" and active):
            if previous and not previous.startswith(MARKER + "\n") and previous not in {http, "# mini-deploy-managed: project-http-v1\n" + http}:
                raise CertificateError("该站点配置不是本面板生成的 HTTP 或证书配置，拒绝覆盖；请先核对现有 Nginx 配置")
        if action == "upload":
            label = self.label(data)
            cert_text, key_text = data.get("certificate", ""), data.get("private_key", "")
            for value in (cert_text, key_text):
                if not isinstance(value, str) or not value or len(value.encode("utf-8")) > MAX_PEM_BYTES:
                    raise CertificateError("证书和私钥均为必填，单个文件不能超过 128 KB")
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.mkdir(mode=0o700, exist_ok=True)
            revision = secrets.token_hex(12)
            candidate = directory / revision
            candidate.mkdir(mode=0o700)
            cert, key = candidate / "fullchain.pem", candidate / "privkey.pem"
            atomic_write(cert, cert_text)
            atomic_write(key, key_text)
            try:
                details = inspect_pair(cert, key, project.app_domain)
            except Exception:
                # These are newly created files, never referenced by Nginx.
                cert.unlink(missing_ok=True)
                key.unlink(missing_ok=True)
                candidate.rmdir()
                raise
            new_record = {**details, "revision": revision, "label": label}
            if active:
                self.commit_config(conf_path, self.https_config(project, new_record, http), previous)
            try:
                atomic_write(metadata, json.dumps(new_record, ensure_ascii=False))
            except Exception:
                if active:
                    self.commit_config(conf_path, previous, self.config(conf_path))
                raise
            # Previous revisions remain private recovery material until explicit deletion.
            return
        if not record:
            raise CertificateError("该项目尚未上传证书")
        if action == "rename":
            record["label"] = self.label(data)
            atomic_write(metadata, json.dumps(record, ensure_ascii=False))
        elif action == "enable":
            inspect_pair(*self.paths(project, record), project.app_domain)
            self.commit_config(conf_path, self.https_config(project, record, http), previous)
        elif action == "disable":
            if not active:
                raise CertificateError("当前 Nginx 未使用该证书，无需停用")
            self.commit_config(conf_path, http, previous)
        elif action == "delete":
            if active:
                raise CertificateError("证书正在使用，请先停用 HTTPS 后再删除")
            configuration = run(self.command("dump"))
            if str(directory) in configuration or self.visible_path(directory) in configuration:
                raise CertificateError("Nginx 仍引用此项目的证书文件，不能删除")
            for match in re.finditer(r'\bssl_certificate(?:_key)?\s+(?:"([^"]+)"|\x27([^\x27]+)\x27|([^;\s]+))\s*;', configuration):
                reference = next(value for value in match.groups() if value is not None)
                if "$" in reference:
                    raise CertificateError("Nginx 使用动态证书路径，无法确认引用，请先核对配置")
                path = self.runtime.host_reference(reference) if self.runtime is not None else Path(reference)
                if not path.is_absolute():
                    raise CertificateError("Nginx 使用相对证书路径，无法确认引用，请先核对配置")
                if path.resolve().is_relative_to(directory.resolve()):
                    raise CertificateError("Nginx 仍引用此项目的证书文件，不能删除")
            if directory.is_mount():
                raise CertificateError("证书目录为挂载点，拒绝删除")
            # Only remove known files, never recurse through unknown directories or mounts.
            revisions = []
            for child in directory.iterdir():
                trusted_path(child)
                if child.name == "current.json" and child.is_file():
                    continue
                if not child.is_dir() or child.is_mount() or not re.fullmatch(r"[a-f0-9]{24}", child.name):
                    raise CertificateError("证书目录包含未知内容，拒绝删除")
                for part in child.iterdir():
                    trusted_path(part)
                    if part.name not in {"fullchain.pem", "privkey.pem"} or not part.is_file():
                        raise CertificateError("证书版本目录包含未知内容，拒绝删除")
                revisions.append(child)
            for revision in revisions:
                for part in revision.iterdir():
                    part.unlink()
                revision.rmdir()
            metadata.unlink()
            directory.rmdir()

    @staticmethod
    def label(data: dict[str, Any]) -> str:
        label = data.get("label", "")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80 or any(ord(c) < 32 for c in label):
            raise CertificateError("证书名称需为 1-80 个字符")
        return label.strip()
