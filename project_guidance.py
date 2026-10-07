"""Read-only repository hints and bounded, rule-based failure advice."""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import entrypoint_checks
import docker_onboarding


def discover_local_projects(run) -> dict[str, Any]:
    """Read only selected Compose metadata; never return container environments."""
    code, output = run(["docker", "ps", "-a", "-q", "--no-trunc", "--filter",
                        "label=com.docker.compose.project"], timeout=4)
    if code:
        return {"projects": [], "notice": "无法查询 Docker。可手动填写代码目录；本机 systemd 项目请使用下方查询指令。"}
    ids = [value for value in output.splitlines() if re.fullmatch(r"[a-f0-9]{64}", value)]
    if not ids:
        return {"projects": [], "notice": "未发现 Compose 项目。普通容器或本机服务可手动接入。"}
    fields = ['.Name', '.State.Status'] + [f'index .Config.Labels "com.docker.compose.project{suffix}"'
        for suffix in ['', '.working_dir', '.config_files', '.environment_file']]
    template = '[' + ','.join('{{json (' + field + ')}}' for field in fields) + ']'
    code, output = run(["docker", "inspect", "--type", "container", "--format", template, *ids[:100]], timeout=5)
    if code:
        return {"projects": [], "notice": "容器状态可能发生了变化，请重新检查或手动填写。"}
    groups = {}
    for line in output.splitlines():
        try:
            values = json.loads(line)
        except ValueError:
            continue
        if not isinstance(values, list) or len(values) != 6:
            continue
        name, status, project, directory, files, env_files = [str(value or '') for value in values]
        if (not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", project)
                or not directory.startswith('/') or len(directory) > 512
                or len(files) + len(env_files) > 8192
                or any(ord(char) < 32 for char in directory + files + env_files)):
            continue
        key = (project, directory, files, env_files)
        entry = groups.setdefault(key, {"name": project, "directory": directory, "config_files": files,
            "environment_files": env_files, "containers": [], "repo": "", "git_directory": "", "command": ""})
        entry["containers"].append({"name": name.lstrip('/')[:128], "state": status[:32]})
    projects = list(groups.values())[:12]
    deadline = time.monotonic() + 8
    for entry in projects:
        directory = Path(entry["directory"])
        if not directory.is_dir() or time.monotonic() >= deadline:
            continue
        code, root = run(["git", "--no-optional-locks", "-C", str(directory), "rev-parse", "--show-toplevel"], timeout=2)
        if code or not root.startswith('/') or len(root) > 512 or any(ord(char) < 32 for char in root):
            continue
        entry["git_directory"] = root
        code, repo = run(["git", "--no-optional-locks", "-C", root, "config", "--local", "--get", "remote.origin.url"], timeout=2)
        if not code:
            try:
                validate_repository(repo, "main")
                entry["repo"] = repo
            except ValueError:
                pass
        files = [Path(value) for value in entry["config_files"].split(',') if value]
        env_files = [Path(value) for value in entry["environment_files"].split(',') if value]
        if not files or len(files) + len(env_files) > 16 or not all(path.is_absolute() and path.is_file() for path in files + env_files):
            continue
        args = ["docker", "compose", "--project-directory", str(directory), "--project-name", entry["name"]]
        for path in files:
            args += ["--file", str(path)]
        for path in env_files:
            args += ["--env-file", str(path)]
        command = shlex.join(args + ["up", "-d", "--build"])
        if len(command) <= 4096:
            entry["command"] = command
    return {"projects": projects, "notice": "读取 Compose 标签和本机 Git 信息，未执行更新。请选择项目并核对原来的启动参数。"
            + (" 本次最多检查 100 个容器、展示 12 个项目。" if len(ids) > 100 or len(groups) > 12 else "")}


PROVIDERS = {
    "github": {"label": "GitHub", "keys": "仓库 Settings → Deploy keys → Add deploy key，添加服务器公钥，仅授予读取权限。",
               "hooks": "仓库 Settings → Webhooks → Add webhook；Content type 选择 application/json，事件选择 Just the push event。"},
    "gitee": {"label": "Gitee", "keys": "仓库管理 → 部署公钥，添加服务器公钥，并确认该公钥有仓库读取权限。",
              "hooks": "仓库管理 → WebHooks → 添加；填写 URL 和 WebHook 密码 / Token，选择 Push 事件。"},
    "gitlab": {"label": "GitLab", "keys": "项目 Settings → Repository → Deploy keys，添加服务器公钥并启用读取权限。",
               "hooks": "项目 Settings → Webhooks；填写 URL 和 Secret token，勾选 Push events。"},
    "gitea": {"label": "Gitea", "keys": "仓库设置 → 部署密钥，添加服务器公钥，保留只读权限。",
              "hooks": "仓库设置 → Webhooks → 添加 Gitea 类型；填写 URL 和 Secret，选择 Push 事件。"},
    "generic": {"label": "其他 Git 平台", "keys": "在代码平台为该仓库添加服务器 SSH 公钥；具体菜单以平台文档为准。",
                "hooks": "使用平台支持的 WebHook 验签方式；未兼容的平台可以先手动更新，不必更换仓库。"},
}


def provider_info(repo: str, provider: str = "auto") -> dict[str, str]:
    if provider not in {"auto", *PROVIDERS}:
        raise ValueError("请选择支持的平台类型，未知平台可选择其他 Git 平台")
    if provider == "auto":
        parsed = urlsplit(repo)
        host = parsed.hostname or (repo.split("@", 1)[-1].split(":", 1)[0] if "@" in repo else "")
        provider = {"github.com": "github", "gitee.com": "gitee", "gitlab.com": "gitlab"}.get(host.lower(), "generic")
    return {"type": provider, **PROVIDERS[provider]}


def repository_identity(repo: str) -> tuple[str, str]:
    parsed = urlsplit(repo)
    if parsed.hostname:
        host, path = parsed.hostname, parsed.path
        if parsed.port and parsed.port != {"ssh": 22, "https": 443}.get(parsed.scheme):
            host = f"{host}:{parsed.port}"
    else:
        host, _, path = repo.split("@", 1)[-1].partition(":")
    return host.lower(), path.strip("/").removesuffix(".git")


def deployment_plan(raw: object) -> dict[str, str]:
    if not raw:
        return {}
    if (not isinstance(raw, dict) or not isinstance(raw.get("situation"), str) or
            raw["situation"] not in {"new", "existing", "unsure"}):
        raise ValueError("请选择首次部署、接入已有服务或帮助检查")
    if not isinstance(raw.get("method"), str) or raw["method"] not in {"template", "script", "commands"}:
        raise ValueError("请选择沿用配置、现有脚本或自定义部署步骤")
    result = {"situation": raw["situation"], "method": raw["method"]}
    if result["method"] == "commands":
        for field in ("build", "restart"):
            value = raw.get(field, "")
            if not isinstance(value, str) or len(value) > 4096:
                raise ValueError("部署步骤过长或包含不支持的控制字符")
            value = value.replace("\r\n", "\n")
            if any(ord(char) < 32 and char not in {"\n", "\t"} for char in value):
                raise ValueError("部署步骤过长或包含不支持的控制字符")
            result[field] = value.strip()
        if not result["restart"]:
            raise ValueError("请提供实际的启动或重启步骤，不会自动猜测服务名称")
    return result


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
         "业务端口被占用", "在项目中点击检查项目，核对 Compose 映射与端口归属；也可执行 ss -ltnp 和 docker ps。项目自带 Nginx 时无需重复安装；使用统一入口时需调整业务映射。不要直接停止已有服务。"),
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


def entry_command(template: str, kind: str, entry: str, app_object: str, workdir: str, port: int) -> str:
    """Generate systemd arguments from repository-relative, validated entry fields."""
    allowed = {"python": {"fastapi", "python"}, "go": {"go"}, "java": {"java"}}
    if kind not in allowed.get(template, set()):
        raise ValueError("运行类型与项目类型不匹配")
    if (not entry or len(entry) > 240 or not re.fullmatch(r"[A-Za-z0-9_./-]+", entry)
            or entry.startswith(("/", "-")) or ".." in entry.split("/") or "//" in entry):
        raise ValueError("启动入口需为仓库内的相对路径，例如 main.py 或 cmd/server；不能使用绝对路径或上级目录")
    entry = entry.removeprefix("./") if entry != "." else entry
    if kind in {"python", "fastapi"} and not entry.endswith(".py"):
        raise ValueError("Python 启动入口应为 .py 文件，例如 app/main.py")
    if kind == "fastapi":
        if not all(part.isidentifier() and part.isascii() for part in entry[:-3].split("/")):
            raise ValueError("FastAPI 入口的目录和文件名需为有效 Python 模块名")
        if not app_object.isidentifier() or not app_object.isascii():
            raise ValueError("请填写 FastAPI 应用对象名，例如 app")
    if kind == "java" and not entry.endswith(".jar"):
        raise ValueError("Java 启动入口应为构建生成的可执行 .jar 文件")
    if kind == "go" and entry.endswith(".go"):
        raise ValueError("Go 请选择入口所在目录，例如 cmd/server，而不是单个 .go 文件")
    def quote(value: str) -> str:
        return json.dumps(value.replace("%", "%%").replace("$", "$$"), ensure_ascii=False)
    root = workdir.rstrip("/")
    if kind == "fastapi":
        return f'{quote(root + "/.venv/bin/uvicorn")} {entry[:-3].replace("/", ".")}:{app_object} --host 127.0.0.1 --port {port}'
    if kind == "python":
        return f'{quote(root + "/.venv/bin/python")} {quote(root + "/" + entry)}'
    if kind == "go":
        return quote(root + "/bin/app")
    return f'/usr/bin/java -jar {quote(root + "/target/deploy/app.jar")} --server.port={port}'


def detect(files: dict[str, str]) -> dict[str, Any]:
    """Inspect known root manifests without importing or executing repository code."""
    candidates: list[dict[str, Any]] = []
    warnings: list[str] = []
    compose = next((name for name in ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml") if name in files), "")
    if compose:
        candidates.append({"template": "docker", "label": "Docker Compose", "evidence": [compose], "entry": ""})
        warnings.extend(entrypoint_checks.entry_advice(entrypoint_checks.inspect_files(files), check_live=False))
    elif "Dockerfile" in files:
        candidates.append({"template": "docker", "label": "Dockerfile", "evidence": ["Dockerfile"], "entry": ""})
    docker = docker_onboarding.detect(files)
    if docker["choices"]:
        warnings.extend(docker["warnings"])
    manifests = [name for name in ("requirements.txt", "pyproject.toml", "Pipfile", "setup.py") if name in files]
    if manifests or any(name.endswith('.py') for name in files):
        entries = []
        entry_choices = []
        for name in PYTHON_ENTRY_FILES:
            if name not in files:
                continue
            try:
                tree = ast.parse(files[name])
            except (SyntaxError, ValueError, RecursionError):
                continue
            factories = {"FastAPI"}
            modules = {"fastapi"}
            for node in tree.body:
                if isinstance(node, ast.ImportFrom) and node.module == "fastapi":
                    factories.update(alias.asname or alias.name for alias in node.names if alias.name == "FastAPI")
                elif isinstance(node, ast.Import):
                    modules.update(alias.asname or alias.name for alias in node.names if alias.name == "fastapi")
            found = False
            for node in tree.body:
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call):
                    func = node.value.func
                    if ((isinstance(func, ast.Name) and func.id in factories) or
                            (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                             and func.value.id in modules and func.attr == "FastAPI")):
                        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                        for target in targets:
                            if isinstance(target, ast.Name):
                                found = True
                                entries.append(f"{name[:-3].replace('/', '.')}:{target.id}")
                                entry_choices.append({"kind": "fastapi", "path": name, "object": target.id})
            if not found:
                entry_choices.append({"kind": "python", "path": name, "object": ""})
        candidates.append({"template": "python", "label": "Python / systemd", "evidence": manifests,
                           "entry": entries[0] if len(entries) == 1 else "", "entries": entry_choices})
        if len(entries) != 1:
            warnings.append("请确认运行类型和入口文件；有多个入口时请选择实际需要运行的程序。")
        if "requirements.txt" not in files:
            warnings.append("Python 建议方案主要使用 requirements.txt；项目使用 Poetry/uv 时可选择自己的构建和重启步骤或已有脚本。")
    if "go.mod" in files:
        candidates.append({"template": "go", "label": "Go / systemd", "evidence": ["go.mod"], "entry": "",
            "entries": [{"kind": "go", "path": str(Path(name).parent).replace('\\', '/'), "object": ""}
                        for name in GO_ENTRY_FILES if name in files]})
        warnings.append("请选择 Go 主程序所在目录；程序监听端口仍以业务自身配置为准。")
    java = [name for name in ("pom.xml", "build.gradle", "build.gradle.kts") if name in files]
    if java:
        candidates.append({"template": "java", "label": "Java / systemd", "evidence": java, "entry": "",
                           "entries": [{"kind": "java", "path": "target/deploy/app.jar", "object": ""}]})
        warnings.append("Java 模板适用于单模块可执行 JAR；多模块、WAR 或非 Spring Boot 项目需调整构建和启动命令。")
    if "package.json" in files:
        candidates.append({"template": "node", "label": "Node / PM2", "evidence": ["package.json"], "entry": ""})
    if not candidates:
        warnings.append("未识别到根目录的常见构建文件。子目录或特殊项目可以选择已有脚本，或填写实际构建和重启步骤。")
    if len(candidates) > 1:
        warnings.insert(0, "检测到多种部署方式，请选择实际使用的一种。")
    script = next((name for name in ("deploy/deploy.sh", "deploy.sh") if name in files), "")
    return {"candidates": candidates, "warnings": warnings, "existing_script": script,
            "ingress": entrypoint_checks.inspect_files(files), "docker": docker}


PYTHON_ENTRY_FILES = ("main.py", "app.py", "server.py", "app/main.py", "src/main.py", "src/app.py")
GO_ENTRY_FILES = ("main.go", "cmd/server/main.go", "cmd/api/main.go")
INSPECT_FILES = (
    "compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml", "Dockerfile",
    "requirements.txt", "pyproject.toml", "Pipfile", "setup.py", "main.py", "app.py", "app/main.py", "src/main.py",
    "go.mod", "pom.xml", "build.gradle", "build.gradle.kts", "package.json", "deploy.sh", "deploy/deploy.sh",
) + entrypoint_checks.OVERRIDE_FILES + PYTHON_ENTRY_FILES + GO_ENTRY_FILES + docker_onboarding.EXAMPLE_FILES
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
    validate_repository(repo, branch or "main")
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
        branches: list[str] = []
        if not branch:
            code, refs = git(["ls-remote", "--symref", "--", repo, "HEAD", "refs/heads/*"])
            if code:
                return {"ok": False, "diagnosis": diagnose(refs)}
            for line in refs.splitlines():
                if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
                    branch = line.split("\t", 1)[0][len("ref: refs/heads/"):]
                elif "\trefs/heads/" in line:
                    branches.append(line.split("\trefs/heads/", 1)[1])
            if not branch:
                return {"ok": False, "diagnosis": [{"code": "branch", "title": "无法确定默认分支",
                    "advice": "仓库可能尚无提交。请确认仓库已有代码，并填写要部署的分支后重试。"}]}
            validate_repository(repo, branch)
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
                # Read manifests as data only; never execute repository code.
                if (name.endswith(".py") or name in entrypoint_checks.COMPOSE_FILES + entrypoint_checks.OVERRIDE_FILES
                        or name in ("Dockerfile",) + docker_onboarding.EXAMPLE_FILES) and int(size) <= 131072:
                    code, content = git(["-C", checkout, "cat-file", "blob", oid])
                    if code == 0:
                        files[name] = content
            return {"ok": True, "branch": branch, "branches": branches[:200], **detect(files)}
    except FileNotFoundError:
        return {"ok": False, "diagnosis": [{"code": "git_missing", "title": "服务器未安装 Git",
                                            "advice": "请先安装 git。Debian/Ubuntu 可执行 apt update && apt install -y git。"}]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "diagnosis": diagnose("operation timed out", exit_code=124)}
