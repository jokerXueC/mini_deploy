"""Bounded discovery of website configuration and public certificates."""
from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import ssl
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath

import gateway_connections
import certificates
import monitoring
import nginx_runtime


COMMON_ROOTS = ("/etc/letsencrypt/live", "/etc/nginx/ssl", "/etc/nginx/certs", "/etc/caddy/certs",
                "/var/lib/caddy/.local/share/caddy/certificates")
MAX_FILES = 200
MAX_CONTAINERS = 40
MAX_ITEMS = 500


class DiscoveryError(certificates.CertificateError):
    """A bounded, public diagnostic that never contains command output."""


def matches_host(host, pattern):
    host, pattern = host.lower().rstrip("."), pattern.lower().rstrip(".")
    return host == pattern or (pattern.startswith("*.") and host.count(".") == pattern.count(".")
                               and host.endswith(pattern[1:]))


def command(args, timeout=5):
    # Configuration output can contain secrets; never put command output in errors or logs.
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(args, stdout=output, stderr=subprocess.DEVNULL, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("读取失败或超时") from exc
        if result.returncode or output.tell() > 2 * 1024 * 1024:
            raise ValueError("无法读取，或配置输出超过 2MB")
        output.seek(0)
        return output.read().decode("utf-8", errors="replace")


def option(args, name, default=""):
    for index, arg in enumerate(args):
        if arg == name and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
        if name in {"-c", "-p"} and arg.startswith(name) and len(arg) > 2:
            return arg[2:]
    return default


def plain(value):
    if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def process_arguments(raw):
    if "\0" in raw and not raw.startswith("nginx: master process "):
        return [arg for arg in raw.split("\0") if arg]
    args = shlex.split(raw.replace("\0", " ").removeprefix("nginx: master process "))
    # Nginx rewrites its process title without quotes around -g directives.
    if "-g" in args:
        start = args.index("-g") + 1
        end = next((i for i in range(start, len(args)) if args[i] in {"-c", "-p", "-e"}), len(args))
        args[start:end] = [" ".join(args[start:end])]
    return args


def nginx_flags(args):
    return [value for flag in ("-c", "-p", "-g") if option(args, flag)
            for value in (flag, option(args, flag))]


def certificate_metadata(pem):
    match = re.search(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", pem, re.S)
    if not match:
        raise ValueError("文件中没有可读取的 PEM 公钥证书")
    descriptor, name = tempfile.mkstemp(suffix=".pem")
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as output:
            output.write(match.group())
        cert = ssl._ssl._test_decode_cert(name)
        expires = ssl.cert_time_to_seconds(cert["notAfter"])
        begins = ssl.cert_time_to_seconds(cert["notBefore"])
        domains = [value for kind, value in cert.get("subjectAltName", ()) if kind in {"DNS", "IP Address"}]
        issuer = ", ".join(value for group in cert.get("issuer", ()) for key, value in group if key in {"organizationName", "commonName"})
        fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(match.group())).hexdigest()
        return {"domains": domains, "issuer": issuer, "expires_at": expires, "not_before": begins,
                "fingerprint": fingerprint, "checked_at": time.time()}
    finally:
        os.unlink(name)


def public_pem(path):
    if not path.is_file() or path.stat().st_size > 128 * 1024:
        raise ValueError("证书不是普通文件或超过 128KB")
    parts = []
    with path.open("r", encoding="ascii", errors="replace") as source:
        for _ in range(2048):
            line = source.readline(4096)
            if not line:
                break
            if "PRIVATE KEY" in line:
                raise ValueError("跳过包含私钥的文件")
            parts.append(line)
            if "-----END CERTIFICATE-----" in line:
                return "".join(parts)
    raise ValueError("无法读取完整的公钥证书")


def bounded_files(root):
    """Only walk configured certificate directories, never the Docker storage layer."""
    pending = [(Path(root), 0)]
    visited = 0
    while pending and visited < 1200:
        directory, depth = pending.pop(0)
        if not directory.is_dir() or directory.is_symlink():
            continue
        with os.scandir(directory) as entries:
            for entry in entries:
                visited += 1
                if visited > 1200:
                    break
                if entry.is_dir(follow_symlinks=False) and depth < 4:
                    pending.append((Path(entry.path), depth + 1))
                elif entry.name.lower().endswith((".crt", ".cer", ".pem")) and not any(
                        word in entry.name.lower() for word in ("privkey", "private")) and entry.name.lower() != "chain.pem":
                    # Certbot public cert.pem links are resolved only inside its own tree.
                    path = Path(entry.path)
                    if path.is_symlink() and not str(path.resolve()).startswith("/etc/letsencrypt/"):
                        continue
                    yield path


def nginx_sites(dump, prefix=None):
    segments = re.split(r"(?m)^# configuration file (.+):\s*$", dump)
    if len(segments) < 3:
        raise ValueError("无法识别配置文件来源")
    files = {segments[i]: gateway_connections.parse(segments[i + 1], "nginx") for i in range(1, len(segments), 2)}
    first = segments[1]
    prefix = prefix or str(PurePosixPath(first).parent)
    unresolved = []
    count = [0]

    def expand(nodes, source, chain):
        result = []
        for node in nodes:
            count[0] += 1
            if count[0] > 10000 or len(chain) > 12:
                raise ValueError("配置层级或指令数量超过扫描限制")
            if node.words[:1] == ["include"] and len(node.words) == 2:
                pattern = plain(node.words[1])
                if not pattern.startswith("/"):
                    pattern = str(PurePosixPath(prefix) / pattern)
                matches = [path for path in files if fnmatch.fnmatchcase(path, pattern)]
                if not matches:
                    unresolved.append(pattern)
                for path in matches:
                    if path in chain:
                        raise ValueError("配置包含循环引用")
                    result.extend(expand(files[path], path, chain + [path]))
            else:
                result.append({"words": [plain(word) for word in node.words], "source": source,
                               "children": expand(node.children, source, chain)})
        return result

    def values(nodes, word):
        return [n["words"][1:] for n in nodes if n["words"][:1] == [word]]

    def walk(nodes, inherited_cert=None, inherited_key=None):
        certs = values(nodes, "ssl_certificate") or inherited_cert or []
        keys = values(nodes, "ssl_certificate_key") or inherited_key or []
        for node in nodes:
            if node["words"] == ["server"]:
                body = node["children"]
                names = [name for group in values(body, "server_name") for name in group]
                local_certs = values(body, "ssl_certificate") or certs
                local_keys = values(body, "ssl_certificate_key") or keys
                listens = values(body, "listen")
                for index in range(max(1, len(local_certs))):
                    cert = local_certs[index][0] if index < len(local_certs) and local_certs[index] else ""
                    key = local_keys[index][0] if index < len(local_keys) and local_keys[index] else ""
                    if cert and not cert.startswith("/") and "$" not in cert:
                        cert = str(PurePosixPath(prefix) / cert)
                    if key and not key.startswith("/") and "$" not in key:
                        key = str(PurePosixPath(prefix) / key)
                    yield {"domains": names, "certificate_path": cert, "key_path": key,
                           "config_path": node["source"], "tls": bool(local_certs or any("ssl" in v for v in listens)),
                           "listens": listens, "notes": ["部分 include 未展开"] if unresolved else []}
            elif node["children"]:
                yield from walk(node["children"], certs, keys)
    expanded = expand(files[first], first, [first])
    return [site for node in expanded if node["words"] == ["http"] for site in walk(node["children"])]


def caddy_sites(config, path):
    http = (config.get("apps") or {}).get("http") or {}
    tls = (config.get("apps") or {}).get("tls") or {}
    loaded = (tls.get("certificates") or {}).get("load_files") or []
    certificates = [(item.get("certificate", ""), item.get("key", "")) for item in loaded if isinstance(item, dict)]

    def hosts(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "host" and isinstance(child, list):
                    yield from (host for host in child if isinstance(host, str))
                elif isinstance(child, (dict, list)):
                    yield from hosts(child)
        elif isinstance(value, list):
            for child in value:
                yield from hosts(child)

    rows = []
    for server in (http.get("servers") or {}).values():
        names = sorted(set(hosts(server.get("routes") or [])))
        listens = server.get("listen") or []
        is_tls = bool(server.get("tls_connection_policies")) or any(str(p).endswith(":443") for p in listens)
        if not server.get("automatic_https", {}).get("disable") and any(not str(p).endswith(":80") for p in listens):
            is_tls = True
        for name in names or [""]:
            rows.append({"domains": [name] if name else [], "certificate_path": "", "key_path": "", "tls": is_tls,
                     "config_path": path, "listens": [[p] for p in listens], "notes": [],
                     "automatic": is_tls and not server.get("automatic_https", {}).get("disable")
                     and not server.get("automatic_https", {}).get("disable_certificates")})
    return rows, certificates


class Discovery:
    def __init__(self, data_home, *, run=command):
        self.home, self.run = Path(data_home), run
        self.lock = threading.RLock()
        self.running = False
        self.last_scan = 0
        self.rows = []
        self.issues = []
        self.progress = "等待首次扫描"
        self.worker = None
        self.online = {}
        self.deadline = 0
        self.operation_lock = threading.Lock()

    def budget(self):
        if self.deadline and time.monotonic() >= self.deadline:
            raise TimeoutError("本轮扫描达到 60 秒预算，未完成的来源保留上次结果")

    def call(self, args):
        self.budget()
        return self.run(args, timeout=max(.1, min(5, self.deadline - time.monotonic())) if self.deadline else 5)

    def snapshot(self):
        with self.lock:
            rows = copy.deepcopy(self.rows)
            for row in rows:
                cert = row.get("certificate")
                if cert:
                    cert["days_remaining"] = int((cert["expires_at"] - time.time()) // 86400)
                row["online"] = copy.deepcopy(self.online.get(row["id"]))
                row.pop("key_path", None)
                row.pop("target", None)
                row.pop("replacement_paths", None)
            return {"items": rows, "running": self.running, "last_scan": self.last_scan,
                    "progress": self.progress, "issues": list(self.issues)}

    def request_scan(self):
        with self.lock:
            if self.running or (self.last_scan and time.time() - self.last_scan < 10):
                return
            self.running = True
            self.progress = "正在发现服务和容器"
            self.worker = threading.Thread(target=self.scan, name="certificate-discovery", daemon=True)
            self.worker.start()

    def execute(self, source, args):
        prefix = ["docker", "exec", source["container_id"]] if source.get("container_id") else []
        return self.call([*prefix, *args])

    def read_certificate(self, source, path):
        if not path.startswith("/") or any(c in path for c in ("$", "{", "\0", "\n")):
            raise ValueError("证书路径包含变量或不是明确的绝对路径")
        if source.get("container_id"):
            pem = self.execute(source, ["head", "-c", "131072", "--", path])
        else:
            pem = public_pem(Path(path))
        return certificate_metadata(pem)

    def targets(self, issues):
        for kind in ("nginx", "caddy"):
            if not shutil.which(kind):
                continue
            source = {"kind": kind, "location": "服务器", "container_id": "", "arguments": [], "active": False}
            try:
                pid = self.call(["systemctl", "show", kind, "--property=MainPID", "--value"]).strip()
                if pid.isdecimal() and int(pid) > 1:
                    raw = (Path("/proc") / pid / "cmdline").read_bytes().decode()
                    source["arguments"] = process_arguments(raw)
                    source["active"] = True
            except (ValueError, OSError):
                issues.append(f"{kind}：无法确认运行状态，将读取默认磁盘配置")
            yield source
        if not shutil.which("docker"):
            return
        try:
            nginx_runtime.require_local_docker()
            rows = self.call(["docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}"])
            containers = [json.loads(line) for line in rows.splitlines() if line.strip()]
            if len(containers) > MAX_CONTAINERS:
                issues.append(f"容器数量超过 {MAX_CONTAINERS}，本轮仅扫描前 {MAX_CONTAINERS} 个")
            for item in containers[:MAX_CONTAINERS]:
                self.budget()
                identity = item.get("ID", "")
                if not re.fullmatch(r"[a-f0-9]{64}", identity):
                    continue
                info = json.loads(self.call(["docker", "inspect", "--format",
                    '{"name":{{json .Name}},"image":{{json .Config.Image}},"args":{{json .Args}},"path":{{json .Path}},"mounts":{{json .Mounts}},"running":{{json .State.Running}}}', identity]))
                signature = " ".join([info["name"], info["image"], info["path"], *info["args"]]).lower()
                kinds = [k for k in ("nginx", "caddy") if k in signature]
                processes = ""
                if info["running"]:
                    # Custom images and neutral container names can still run known services.
                    try:
                        processes = self.call(["docker", "top", identity, "-eo", "args"])
                        kinds = sorted(set(kinds + [k for k in ("nginx", "caddy") if re.search(rf"(?:^|[/\s]){k}(?:\s|:|$)", processes, re.M)]))
                    except ValueError:
                        issues.append(info["name"].lstrip("/") + "：无法读取进程参数，按容器启动参数扫描")
                for kind in kinds:
                    source = {"kind": kind, "location": "Docker · " + info["name"].lstrip("/"), "container_id": identity,
                              "arguments": [info["path"], *info["args"]], "active": info["running"], "mounts": info["mounts"]}
                    if not info["running"]:
                        issues.append(source["location"] + "：容器已停止，无法确认容器内实际配置")
                        continue
                    for process in processes.splitlines():
                        process = process.strip()
                        if (kind == "nginx" and process.startswith("nginx: master process ")) or (kind == "caddy" and re.match(r"(?:/\S*/)?caddy\s+run(?:\s|$)", process)):
                            source["arguments"] = process_arguments(process)
                            break
                    yield source
        except (ValueError, OSError, KeyError, TypeError):
            issues.append("Docker：无法完整读取本机容器信息，请检查 Docker 服务及权限")

    def source_sites(self, source):
        args, kind = source["arguments"], source["kind"]
        if kind == "nginx":
            extra = nginx_flags(args)
            dump = self.execute(source, ["nginx", "-T", *extra])
            return nginx_sites(dump)
        if "--resume" in args:
            path = "/config/caddy/autosave.json" if source.get("container_id") else "/var/lib/caddy/.config/caddy/autosave.json"
            config = json.loads(self.execute(source, ["head", "-c", "2097152", "--", path]))
        else:
            path = option(args, "--config", "/etc/caddy/Caddyfile")
            if path.endswith(".json"):
                config = json.loads(self.execute(source, ["head", "-c", "2097152", "--", path]))
            else:
                adapter = option(args, "--adapter", "caddyfile")
                config = json.loads(self.execute(source, ["caddy", "adapt", "--config", path, "--adapter", adapter]))
        rows, loaded = caddy_sites(config, path)
        # File certificates are matched to virtual hosts by their SANs, not guessed by order.
        for cert_path, key_path in loaded:
            try:
                cert = self.read_certificate(source, cert_path)
            except (ValueError, OSError, ssl.SSLError):
                for row in rows:
                    row["notes"].append("配置含手动证书，但无法读取或确认其对应域名")
                continue
            for row in rows:
                if any(matches_host(host, name) for host in row["domains"] for name in cert["domains"]):
                    row.update(certificate_path=cert_path, key_path=key_path, certificate=cert, automatic=False)
        return rows

    def scan(self):
        with self.operation_lock:
            self._scan()

    @staticmethod
    def replacement_paths(row):
        if row.get("kind") != "nginx" or not row.get("active") or row.get("automatic") or not row.get("certificate"):
            raise ValueError("保留原服务的证书管理方式")
        source = row["target"]
        paths = []
        for name in ("certificate_path", "key_path"):
            path = row.get(name, "")
            if not path.startswith("/") or any(v in path for v in ("$", "..", "\n", "\0", "/letsencrypt/")):
                raise ValueError("自动续签或动态路径由原服务管理")
            if source.get("container_id"):
                mounts = sorted(source.get("mounts", []), key=lambda m: len(m.get("Destination", "")), reverse=True)
                mount = next((m for m in mounts if path == m.get("Destination") or path.startswith(m.get("Destination", "").rstrip("/") + "/")), None)
                if not mount or not mount.get("RW") or mount.get("Type") not in {"bind", "volume"}:
                    raise ValueError("证书未挂载到可写目录，保留原容器管理方式")
                if path == mount["Destination"] or not Path(mount["Source"]).is_dir():
                    raise ValueError("单文件挂载不能可靠热替换，保留原容器管理方式")
                path = str(Path(mount["Source"]) / PurePosixPath(path).relative_to(mount["Destination"]))
            file = Path(path)
            certificates.trusted_path(file)
            if not file.is_file() or file.stat().st_size > 128 * 1024:
                raise ValueError("证书文件不可写或超出大小限制")
            paths.append(file)
        if paths[0] == paths[1]:
            raise ValueError("合并证书与私钥文件由原服务管理")
        if not row.get("domains") or any(not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]*", d) for d in row["domains"]):
            raise ValueError("含通配、正则或默认站点，无法完整确认替换范围")
        if not shutil.which("openssl"):
            raise ValueError("服务器未安装 OpenSSL，暂不可校验证书替换")
        return paths

    def _scan(self):
        self.deadline = time.monotonic() + 60
        rows, issues, read = [], [], {}
        with self.lock:
            previous = copy.deepcopy(self.rows)

        def publish():
            current = {r["id"] for r in rows}
            with self.lock:
                self.rows = copy.deepcopy(rows) + [{**r, "stale": True} for r in previous if r["id"] not in current]

        try:
            for source in self.targets(issues):
                self.budget()
                with self.lock:
                    self.progress = "正在读取 " + source["location"]
                try:
                    sites = self.source_sites(source)
                except (ValueError, OSError, KeyError, TypeError, RecursionError):
                    issues.append(source["location"] + f" · {source['kind']}：无法解析实际配置，原配置未修改")
                    continue
                for site in sites[:200]:
                    self.budget()
                    if len(rows) >= MAX_ITEMS:
                        raise DiscoveryError("本轮达到 500 项上限")
                    cert_path = site["certificate_path"]
                    row = {**site, "source": source["location"], "kind": source["kind"], "active": source["active"],
                           "referenced": True, "target": source, "certificate": site.get("certificate"),
                           "renewal": "自动管理" if site.get("automatic") or "/letsencrypt/" in cert_path else "原服务管理"}
                    row["id"] = hashlib.sha256(json.dumps([source["location"], site["config_path"], site["domains"], cert_path]).encode()).hexdigest()[:24]
                    if cert_path and not row["certificate"]:
                        cache_key = (source.get("container_id"), cert_path)
                        try:
                            if cache_key not in read:
                                read[cache_key] = self.read_certificate(source, cert_path)
                            row["certificate"] = read[cache_key]
                        except (ValueError, OSError, ssl.SSLError) as exc:
                            row["notes"].append(str(exc) if isinstance(exc, ValueError) else "无法读取证书文件")
                    if row.get("automatic"):
                        row["notes"].append("由原服务自动申请和续签，可检查线上证书")
                    row["seen_at"] = time.time()
                    rows.append(row)
                if len(sites) > 200:
                    issues.append(source["location"] + "：配置站点超过 200 项，部分站点未扫描")
                publish()
            roots = [(root, None) for root in COMMON_ROOTS]
            for source in {r["source"]: r["target"] for r in rows if r["kind"] == "caddy"}.values():
                for mount in source.get("mounts", []):
                    if mount.get("Destination") == "/data" and mount.get("Type") in {"bind", "volume"}:
                        roots.append((str(Path(mount["Source"]) / "caddy" / "certificates"), source))
            seen = {r["certificate_path"] for r in rows if not r["target"].get("container_id")}
            fingerprints = {r["certificate"]["fingerprint"] for r in rows if r.get("certificate")}
            count = 0
            for root, source in roots:
                self.budget()
                try:
                    for path in bounded_files(root):
                        self.budget()
                        if count >= MAX_FILES:
                            issues.append("本轮证书文件扫描达到限制，列表可能不完整")
                            break
                        count += 1
                        if str(path) in seen:
                            continue
                        seen.add(str(path))
                        try:
                            cert = certificate_metadata(public_pem(path))
                        except (ValueError, OSError, ssl.SSLError):
                            continue
                        associated = False
                        for row in rows:
                            same_source = row.get("target", {}).get("container_id", "") == (source or {}).get("container_id", "")
                            if not same_source or not row.get("automatic") or row.get("kind") != "caddy":
                                continue
                            if any(matches_host(host, name) for host in row["domains"] for name in cert["domains"]):
                                associated = True
                                if not row.get("certificate") or cert["expires_at"] > row["certificate"]["expires_at"]:
                                    row.update(certificate=cert, certificate_path=str(path), certificate_candidate=True)
                        if associated or cert["fingerprint"] in fingerprints:
                            continue
                        fingerprints.add(cert["fingerprint"])
                        if len(rows) >= MAX_ITEMS:
                            raise DiscoveryError("本轮达到 500 项上限，部分文件未扫描")
                        rows.append({"id": hashlib.sha256(str(path).encode()).hexdigest()[:24], "domains": cert["domains"],
                                     "certificate_path": str(path), "config_path": "", "certificate": cert,
                                     "source": "常见证书目录", "kind": "file", "active": False, "referenced": False,
                                     "renewal": "待确认", "seen_at": time.time(),
                                     "notes": ["发现证书文件，尚未确认由哪个网站使用"]})
                except OSError:
                    issues.append(f"目录无法读取：{root}")
                publish()
        except TimeoutError:
            issues.append("本轮扫描达到 60 秒预算，未完成的来源保留上次结果")
        except DiscoveryError as exc:
            issues.append(str(exc))
        except Exception:
            issues.append("部分扫描失败，未完成的来源保留上次结果；服务器文件未修改")
        finally:
            for row in rows:
                try:
                    row["replacement_paths"] = [str(p) for p in self.replacement_paths(row)]
                    managed_root = self.home / "certificates"
                    if any(p.is_relative_to(managed_root) for p in map(Path, row["replacement_paths"])):
                        raise DiscoveryError("面板已有托管证书，请在下方手动配置中管理")
                    if issues or row.get("notes"):
                        raise DiscoveryError("部分配置尚未确认，重新扫描完整后可替换")
                    row["can_replace"] = True
                except (ValueError, OSError) as exc:
                    row["can_replace"] = False
                    row["replacement_note"] = str(exc) if isinstance(exc, ValueError) else "证书文件或权限无法确认"
            with self.lock:
                current = {r["id"] for r in rows}
                stale = [{**r, "stale": True} for r in previous if r["id"] not in current] if issues else []
                self.rows, self.issues = (rows + stale)[:MAX_ITEMS], issues
                keep = {r["id"] for r in self.rows}
                self.online = {key: value for key, value in self.online.items() if key in keep or value.get("status") == "checking"}
                self.last_scan = time.time()
                self.running = False
                self.progress = f"已发现 {len(rows)} 项" + ("，部分来源待确认" if issues else "")
            self.deadline = 0

    def replace(self, identity, fingerprint, pem, key):
        if not all(isinstance(value, str) and 0 < len(value.encode()) <= 128 * 1024 for value in (pem, key)):
            raise DiscoveryError("请上传不超过 128KB 的证书链和未加密私钥")
        if not self.operation_lock.acquire(blocking=False):
            raise DiscoveryError("扫描或证书操作正在进行，请稍后重试")
        try:
            self.deadline = time.monotonic() + 30
            with self.lock:
                row = next((copy.deepcopy(r) for r in self.rows if r["id"] == identity), None)
            if not row or row.get("stale") or not row.get("can_replace"):
                raise DiscoveryError("此记录暂不支持直接替换，请重新扫描")
            paths = self.replacement_paths(row)
            if row["certificate"]["fingerprint"] != fingerprint or certificate_metadata(public_pem(paths[0]))["fingerprint"] != fingerprint:
                raise DiscoveryError("证书已变化，请重新扫描后操作")
            source = row["target"]
            issues = []
            live = next((s for s in self.targets(issues) if s["kind"] == source["kind"] and s.get("container_id") == source.get("container_id")), None)
            if issues or live != source:
                raise DiscoveryError("运行服务或挂载已变化，请重新扫描")
            sites = self.source_sites(source)
            affected = [site for site in sites if site["certificate_path"] == row["certificate_path"] or site["key_path"] == row["key_path"]]
            if not affected or any(site["certificate_path"] != row["certificate_path"] or site["key_path"] != row["key_path"] for site in affected):
                raise DiscoveryError("证书与私钥被不同配置共用，请通过原服务管理")
            domains = sorted({d for site in affected for d in site["domains"]})
            if any(not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]*", d) for d in domains) or not domains:
                raise DiscoveryError("存在无法验证的站点域名，无法直接替换")
            with self.lock:
                if any(other.get("target") != source and set(other.get("replacement_paths", [])) & set(map(str, paths)) for other in self.rows):
                    raise DiscoveryError("证书被多个服务共用，请通过原服务管理")
            extra = nginx_flags(source["arguments"])
            self.execute(source, ["nginx", "-t", *extra])
            self.deadline = 0
            backup = self.home / "certificate-backups"
            certificates.trusted_path(backup)
            backup.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory = Path(tempfile.mkdtemp(prefix="replacement-", dir=backup))
            originals = [p.read_bytes() for p in paths]
            infos = [p.stat() for p in paths]
            for name, content in zip(("previous.crt", "previous.key"), originals):
                with (directory / name).open("xb") as stream:
                    os.chmod(stream.name, 0o600)
                    stream.write(content)
            cert_file, key_file = directory / "new.crt", directory / "new.key"
            certificates.atomic_write(cert_file, pem)
            certificates.atomic_write(key_file, key)
            certificates.inspect_pair(cert_file, key_file, domains[0])
            new_cert = certificate_metadata(pem)
            if any(not any(matches_host(domain, name) for name in new_cert["domains"]) for domain in domains):
                raise DiscoveryError("新证书未覆盖共用文件的全部域名，未修改原证书")

            def write(index, content):
                path, info = paths[index], infos[index]
                certificates.trusted_path(path)
                descriptor, temporary = tempfile.mkstemp(prefix=".mini-certificate-", dir=path.parent)
                try:
                    with os.fdopen(descriptor, "wb") as output:
                        if os.name == "posix":
                            os.fchown(output.fileno(), info.st_uid, info.st_gid)
                            os.fchmod(output.fileno(), stat.S_IMODE(info.st_mode))
                        output.write(content)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)

            def reload_service():
                self.execute(source, ["nginx", "-t", *extra])
                if source.get("container_id"):
                    self.execute(source, ["nginx", "-s", "reload", *extra])
                else:
                    self.call(["systemctl", "reload", "nginx"])

            try:
                for index, content in enumerate((pem.encode(), key.encode())):
                    write(index, content)
                reload_service()
            except (OSError, ValueError) as exc:
                try:
                    for index, content in enumerate(originals):
                        write(index, content)
                    reload_service()
                except (OSError, ValueError) as rollback:
                    raise DiscoveryError(f"恢复加载失败，请检查服务；原文件备份保留在 {directory}") from rollback
                raise DiscoveryError("替换未完成，已恢复原文件并重新加载服务") from exc
            with self.lock:
                self.last_scan = 0
            return {"message": "证书已替换，配置校验及重载命令已完成，可检查线上证书", "backup": str(directory)}
        finally:
            self.deadline = 0
            self.operation_lock.release()

    def check(self, identity):
        with self.lock:
            row = next((copy.deepcopy(r) for r in self.rows if r["id"] == identity), None)
            if row is None:
                raise DiscoveryError("发现记录已变化，请刷新列表")
            if not row.get("referenced") or row.get("stale"):
                raise DiscoveryError("仅检查本轮发现的网站；目录文件不代表线上网站")
            domains = [d for d in row["domains"] if re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", d) and "." in d]
            if not domains:
                raise DiscoveryError("没有可直接检查的明确域名")
            if self.online.get(identity, {}).get("status") == "checking":
                return
            if time.time() - self.online.get(identity, {}).get("checked_at", 0) < 10:
                raise DiscoveryError("刚刚已检查，请稍候重试")
            if sum(v.get("status") == "checking" for v in self.online.values()) >= 2:
                raise DiscoveryError("正在检查其他网站，请稍候")
            self.online[identity] = {"status": "checking"}

        def worker():
            try:
                url = monitoring.normalize_url(("https://" if row.get("tls", True) else "http://") + domains[0])
                result = monitoring.probe({"url": url})
                result["url"] = url
                remote = result.get("certificate") or {}
                result["note"] = "检查域名标准端口的线上结果；CDN、端口映射或未重载的配置可能与磁盘证书不同"
                result["matches_disk"] = (remote.get("sha256") == row["certificate"]["fingerprint"]
                                          if remote.get("sha256") and row.get("certificate") else None)
            except Exception:
                result = {"status": "failed", "detail": "访问检查失败", "checked_at": time.time()}
            with self.lock:
                self.online[identity] = result
        threading.Thread(target=worker, name="discovered-website-check", daemon=True).start()

    def observe(self, monitor):
        with self.lock:
            rows = copy.deepcopy(self.rows)
            fresh = self.last_scan and time.time() - self.last_scan < 1200 and not self.running
        for row in rows:
            cert = row.get("certificate")
            if not row.get("referenced") or not row.get("active") or not cert or row.get("certificate_candidate"):
                continue
            days = (cert["expires_at"] - time.time()) / 86400
            monitor.observe("discovery:" + row["id"], (days <= monitor.settings["certificate_days"] or cert["not_before"] > time.time()) if fresh and not row.get("stale") else None,
                            "证书需要关注：" + ", ".join(row["domains"] or cert["domains"]),
                            f"{row['source']} · 配置引用证书剩余 {int(days)} 天", "certificate", "critical" if days <= 7 else "warning")
        if fresh and not self.issues:
            valid = {"discovery:" + row["id"] for row in rows if row.get("referenced") and row.get("active") and row.get("certificate") and not row.get("certificate_candidate")}
            with monitor.lock:
                for key in set(monitor.incidents) | set(monitor.pending):
                    if key.startswith("discovery:") and key not in valid:
                        monitor.incidents.pop(key, None)
                        monitor.pending.pop(key, None)
