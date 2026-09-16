"""Read-only repository hints and bounded, rule-based failure advice."""

from __future__ import annotations

import ast
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def diagnose(output: str, *, exit_code: int = 1) -> list[dict[str, str]]:
    """Return advice only; never copy credentials or run suggested commands."""
    if exit_code == 0:
        return []
    text = str(output)[-65536:].lower()
    rules = [
        ("host_key", ("host key verification failed", "remote host identification has changed"),
         "SSH 主机身份未确认", "先向代码平台核对 SSH 主机指纹，再由管理员更新 known_hosts；不要关闭主机校验。"),
        ("repo_auth", ("permission denied (publickey)", "authentication failed", "could not read username", "repository not found", "access denied"),
         "仓库地址或访问权限有问题", "核对仓库地址。SSH 仓库需把服务器公钥加入平台部署密钥；HTTPS 私有仓库需为运行面板的用户配置凭据。"),
        ("branch", ("couldn't find remote ref", "remote branch", "did not match any file(s) known to git"),
         "部署分支可能不存在", "在代码平台确认分支名称，检查 main/master 和大小写，再修改项目的部署分支。"),
        ("git_conflict", ("not possible to fast-forward", "would be overwritten", "unmerged files", "local changes"),
         "服务器代码与仓库存在冲突", "先备份服务器上的修改并核对差异，再处理冲突；不要直接强制重置代码。"),
        ("dns", ("could not resolve host", "could not resolve hostname", "temporary failure in name resolution", "getaddrinfo failed"),
         "服务器域名解析失败", "检查服务器 DNS 和出站网络；本机浏览器能访问不代表服务器能访问。"),
        ("network", ("connection timed out", "failed to connect", "network is unreachable", "connection reset", "operation timed out"),
         "服务器网络连接失败", "检查仓库或镜像源是否可从服务器访问，以及代理、防火墙和出站规则。"),
        ("port", ("address already in use", "port is already allocated", "bind: address already in use"),
         "业务端口被占用", "核对项目端口和已有服务。可执行 ss -ltnp 查看监听进程；不要直接停止不明服务。"),
        ("disk", ("no space left on device", "disk quota exceeded"),
         "磁盘空间或 inode 不足", "执行 df -h 和 df -i 检查空间，优先清理确认不再需要的日志；不要删除数据库或 Docker 数据卷。"),
        ("docker", ("cannot connect to the docker daemon", "is the docker daemon running"),
         "Docker 服务不可用", "检查 Docker 是否安装、服务是否启动，以及当前用户是否有权访问 Docker socket。"),
        ("dependency", ("modulenotfounderror", "no matching distribution", "could not find a version that satisfies", "could not resolve dependencies", "npm err!", "npm error"),
         "依赖安装或加载失败", "核对依赖文件、运行时版本和包源网络；Python 启动命令应使用项目虚拟环境。"),
        ("command", ("command not found", "no such file or directory", "unable to access jarfile", "status=203/exec"),
         "命令或文件不存在", "核对运行环境、脚本路径和构建产物；确认 Python 虚拟环境、Go 可执行文件或 Java JAR 已生成。"),
        ("permission", ("permission denied", "operation not permitted"),
         "文件或服务权限不足", "检查脚本执行权限、目录所有者和服务运行用户，避免使用 chmod 777。"),
        ("health", ("health failed", "health check failed", "connection refused", "curl: (22)"),
         "服务启动或健康检查失败", "核对启动日志、监听端口和健康检查路径；应用可能尚未启动完成，HTTP 404 也可能只是路径填写错误。"),
    ]
    matches = [{"code": code, "title": title, "advice": advice}
               for code, needles, title, advice in rules if any(needle in text for needle in needles)]
    if exit_code == 124:
        matches.insert(0, {"code": "timeout", "title": "部署超过设定时限",
                           "advice": "先检查最后一个执行阶段是否卡住；确认是正常构建耗时后，再调整项目超时时间。"})
    return matches[:3] or [{"code": "unknown", "title": "暂时无法确定失败原因",
                           "advice": "打开本次部署日志，查找第一条错误，并检查应用服务日志；退出码本身不能说明具体原因。"}]


def validate_repository(repo: str, branch: str) -> None:
    if not repo or len(repo) > 512 or any(char.isspace() or ord(char) < 32 for char in repo):
        raise ValueError("请填写有效的 HTTPS 或 SSH 仓库地址")
    parsed = urlsplit(repo)
    url_ok = (parsed.scheme in {"https", "ssh"} and parsed.hostname and parsed.path
              and not parsed.password and not parsed.query and not parsed.fragment)
    scp_ok = re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9][A-Za-z0-9.-]*:[A-Za-z0-9_./-]+", repo)
    if not url_ok and not scp_ok:
        raise ValueError("仅支持 HTTPS 或 SSH 仓库地址；请勿在地址中填写密码或 Token")
    if parsed.scheme == "https" and parsed.username:
        raise ValueError("请通过 Git 凭据管理器配置访问权限，不要把凭据放进仓库地址")
    if (not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,119}", branch)
            or ".." in branch or "//" in branch or branch.endswith(("/", ".", ".lock"))):
        raise ValueError("请填写有效的部署分支，例如 main 或 release/v1")


def detect(files: dict[str, str]) -> dict[str, Any]:
    """Inspect known root manifests without importing or executing repository code."""
    candidates: list[dict[str, Any]] = []
    warnings: list[str] = []
    compose = next((name for name in ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml") if name in files), "")
    if compose:
        candidates.append({"template": "docker", "label": "Docker Compose", "evidence": [compose], "entry": ""})
    manifests = [name for name in ("requirements.txt", "pyproject.toml", "Pipfile", "setup.py") if name in files]
    if manifests:
        entries = []
        for name in ("main.py", "app.py", "app/main.py", "src/main.py"):
            if name not in files:
                continue
            try:
                tree = ast.parse(files[name])
            except (SyntaxError, ValueError, RecursionError):
                continue
            for node in tree.body:
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call):
                    func = node.value.func
                    if isinstance(func, ast.Name) and func.id == "FastAPI":
                        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                        entries.extend(f"{name[:-3].replace('/', '.')}:{target.id}"
                                       for target in targets if isinstance(target, ast.Name))
        candidates.append({"template": "python", "label": "Python / systemd", "evidence": manifests,
                           "entry": entries[0] if len(entries) == 1 else ""})
        if len(entries) != 1:
            warnings.append("Python 启动入口无法唯一确定，请填写实际启动命令。")
        if "requirements.txt" not in files:
            warnings.append("当前 Python 初版脚本使用 requirements.txt；请按项目的 Poetry/uv 等依赖管理方式调整脚本。")
    if "go.mod" in files:
        candidates.append({"template": "go", "label": "Go / systemd", "evidence": ["go.mod"], "entry": ""})
        warnings.append("Go 模板编译 cmd/server 或仓库根目录；请确认程序读取端口的方式和实际入口。")
    java = [name for name in ("pom.xml", "build.gradle", "build.gradle.kts") if name in files]
    if java:
        candidates.append({"template": "java", "label": "Java / systemd", "evidence": java, "entry": ""})
        warnings.append("Java 模板适用于单模块可执行 JAR；多模块、WAR 或非 Spring Boot 项目需调整构建和启动命令。")
    if "package.json" in files:
        candidates.append({"template": "node", "label": "Node / PM2", "evidence": ["package.json"], "entry": ""})
    if not candidates:
        warnings.append("未识别到根目录的常见构建文件。子目录或特殊项目请选择自定义脚本。")
    if len(candidates) > 1:
        warnings.insert(0, "检测到多种部署方式，请选择实际使用的一种。")
    script = next((name for name in ("deploy/deploy.sh", "deploy.sh") if name in files), "")
    return {"candidates": candidates, "warnings": warnings, "existing_script": script}


INSPECT_FILES = (
    "compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml", "Dockerfile",
    "requirements.txt", "pyproject.toml", "Pipfile", "setup.py", "main.py", "app.py", "app/main.py", "src/main.py",
    "go.mod", "pom.xml", "build.gradle", "build.gradle.kts", "package.json", "deploy.sh", "deploy/deploy.sh",
)
_inspection_lock = threading.Lock()


def inspect_repository(repo: str, branch: str, *, env: dict[str, str] | None = None) -> dict[str, Any]:
    if not _inspection_lock.acquire(blocking=False):
        return {"ok": False, "diagnosis": [{"code": "busy", "title": "已有仓库正在识别",
                                            "advice": "请等待当前识别完成后重试。"}]}
    try:
        return _inspect_repository(repo, branch, env=env)
    finally:
        _inspection_lock.release()


def _inspect_repository(repo: str, branch: str, *, env: dict[str, str] | None = None) -> dict[str, Any]:
    validate_repository(repo, branch)
    environment = dict(os.environ if env is None else env)
    environment.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
                        "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oConnectTimeout=10 -oStrictHostKeyChecking=yes"})
    deadline = time.monotonic() + 60

    def git(arguments: list[str]) -> tuple[int, str]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired("git", 60)
        with tempfile.TemporaryFile() as output:
            proc = subprocess.Popen(
                ["git", "-c", "protocol.file.allow=never", "-c", "protocol.ext.allow=never", *arguments],
                stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env=environment, start_new_session=os.name != "nt",
            )
            try:
                code = proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
                proc.wait()
                raise
            output.seek(0)
            return code, output.read(131072).decode("utf-8", errors="replace")

    try:
        with tempfile.TemporaryDirectory(prefix="mini-deploy-inspect-", ignore_cleanup_errors=True) as temp:
            checkout = str(Path(temp) / "repo")
            code, output = git(["clone", "--depth", "1", "--single-branch", "--no-checkout", "--quiet",
                                "--branch", branch, "--", repo, checkout])
            if code:
                return {"ok": False, "diagnosis": diagnose(output)}
            files: dict[str, str] = {}
            code, listing = git(["-C", checkout, "ls-tree", "-r", "-l", "HEAD", "--", *INSPECT_FILES])
            if code:
                return {"ok": False, "diagnosis": diagnose(listing)}
            for line in listing.splitlines():
                fields = line.split(None, 4)
                if len(fields) != 5:
                    continue
                mode, kind, oid, size, name = fields
                if mode not in {"100644", "100755"} or kind != "blob" or name not in INSPECT_FILES:
                    continue
                files[name] = ""
                # Only Python source is parsed. Manifests and scripts are presence hints.
                if name.endswith(".py") and int(size) <= 131072:
                    code, content = git(["-C", checkout, "cat-file", "blob", oid])
                    if code == 0:
                        files[name] = content
            return {"ok": True, **detect(files)}
    except FileNotFoundError:
        return {"ok": False, "diagnosis": [{"code": "git_missing", "title": "服务器未安装 Git",
                                            "advice": "请先安装 git。Debian/Ubuntu 可执行 apt update && apt install -y git。"}]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "diagnosis": diagnose("operation timed out", exit_code=124)}
