"""Bounded Docker hints, managed Compose generation and secret-safe execution."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
from pathlib import Path, PurePosixPath

ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")
OVERRIDE_FILES = ("compose.override.yaml", "compose.override.yml", "docker-compose.override.yaml", "docker-compose.override.yml")
EXAMPLE_FILES = (".env.example", ".env.sample", "env.example")
INSPECT_FILES = COMPOSE_FILES + ("Dockerfile",) + EXAMPLE_FILES
# These must not override the process that invokes Docker or Compose.
RESERVED_ENV = {"PATH", "HOME", "BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"}


def environment_name(value: str) -> str:
    if (not isinstance(value, str) or not ENV_NAME.fullmatch(value) or value in RESERVED_ENV
            or value.startswith(("DOCKER_", "COMPOSE_", "DEPLOY_", "PYTHON", "LD_", "DYLD_", "GIT_"))):
        raise ValueError("环境变量名称无效或属于部署工具保留变量")
    return value


def relative_file(value: str) -> str:
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,240}", value)
            or value.startswith(("/", "-")) or ".." in value.split("/") or "//" in value):
        raise ValueError("配置文件必须是仓库内的相对路径，不能使用上级目录")
    value = value.removeprefix("./")
    if value in {"", "."}:
        raise ValueError("请选择配置文件，而不是目录")
    return value


def normalize(raw: object) -> dict:
    if not raw:
        return {}
    if not isinstance(raw, dict) or raw.get("mode") not in {"compose", "dockerfile"}:
        raise ValueError("请选择已有 Compose 或 Dockerfile 部署方式")
    mode = raw["mode"]
    result = {"mode": mode, "file": relative_file(raw.get("file", ""))}
    names = raw.get("environment", [])
    if not isinstance(names, list) or len(names) > 80:
        raise ValueError("环境变量最多配置 80 项")
    result["environment"] = list(dict.fromkeys(environment_name(name) for name in names))
    if mode == "compose":
        return result
    for key in ("container_port", "published_port"):
        value = raw.get(key)
        if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,5}", str(value)) or not 1 <= int(value) <= 65535:
            raise ValueError("请填写 1 到 65535 之间的应用端口和服务器端口")
        result[key] = int(value)
    if result["published_port"] == 6868:
        raise ValueError("6868 是面板专用端口，请为业务选择其他端口")
    if raw.get("access", "public") not in {"public", "local"}:
        raise ValueError("请选择公网访问或仅服务器本机访问")
    result["access"] = raw.get("access", "public")
    targets = raw.get("volumes", [])
    if not isinstance(targets, list) or len(targets) > 16:
        raise ValueError("数据目录最多配置 16 项")
    clean_targets = []
    for target in targets:
        if (not isinstance(target, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]{1,239}", target)
                or ".." in target.split("/") or "//" in target):
            raise ValueError("数据目录需填写容器内的绝对路径，例如 /app/uploads")
        target = str(PurePosixPath(target))
        if target == "/" or any(target == path or target.startswith(path + "/") for path in ("/proc", "/sys", "/dev", "/etc", "/bin", "/usr", "/lib", "/sbin")):
            raise ValueError("不能把系统目录作为业务数据目录")
        if any(target == path or target.startswith(path + "/") or path.startswith(target + "/") for path in clean_targets):
            raise ValueError("数据目录不能重复或互相包含")
        clean_targets.append(target)
    result["volumes"] = clean_targets
    return result


def environment_values(raw: object, names: list[str]) -> dict[str, str]:
    if not isinstance(raw, dict) or len(raw) > 80:
        raise ValueError("请以名称和值提供环境变量")
    values = {}
    for name, value in raw.items():
        environment_name(name)
        if name not in names or not isinstance(value, str) or len(value) > 8192 or "\x00" in value:
            raise ValueError("环境变量与确认的配置不匹配或内容过长")
        values[name] = value
    if sum(len(value.encode("utf-8")) for value in values.values()) > 48 * 1024:
        raise ValueError("环境变量总大小不能超过 48 KB")
    if len(json.dumps(values, ensure_ascii=False).encode("utf-8")) > 64 * 1024:
        raise ValueError("环境变量序列化后过大，请减少特殊字符或变量数量")
    if any(not values.get(name) for name in names):
        raise ValueError("请填写所有已添加的环境变量；不需要的变量可删除")
    return values


def detect(files: dict[str, str]) -> dict:
    choices, warnings = [], []
    variables: dict[str, bool] = {}
    by_file: dict[str, dict[str, bool]] = {}
    for name in COMPOSE_FILES + OVERRIDE_FILES:
        if name in files:
            if name in COMPOSE_FILES:
                choices.append({"mode": "compose", "file": name})
            by_file[name] = {}
            # Do not copy defaults or example values; they may contain credentials.
            for match in re.finditer(r"(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*)([^}\n]*)\}", files[name]):
                variable, suffix = match.groups()
                if not suffix or suffix.startswith(("?", ":?")):
                    variables[variable] = True
                    by_file[name][variable] = True
    ports, command = [], False
    if "Dockerfile" in files:
        stages = {}
        active = ""
        for line in re.sub(r"\\\r?\n", " ", files["Dockerfile"]).splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2 or parts[0].startswith("#"):
                continue
            instruction, arguments = parts[0].upper(), parts[1]
            if instruction == "FROM":
                words = arguments.split()
                base = next((word for word in words if not word.startswith("--")), "")
                ports, command = stages.get(base, ([], False))
                ports = list(ports)
                active = ""
                if len(words) >= 3 and words[-2].upper() == "AS":
                    active = words[-1]
                    stages[active] = (ports, command)
            elif instruction == "EXPOSE":
                for word in arguments.split("#", 1)[0].split():
                    match = re.fullmatch(r"([0-9]{1,5})(?:/tcp)?", word)
                    if match and 1 <= int(match[1]) <= 65535:
                        ports.append(int(match[1]))
                # Update the active stage's inherited metadata after each instruction.
            elif instruction in {"CMD", "ENTRYPOINT"}:
                command = True
            if instruction != "FROM" and active:
                stages[active] = (list(ports), command)
        ports = list(dict.fromkeys(ports))
        choices.append({"mode": "dockerfile", "file": "Dockerfile"})
        if not command:
            warnings.append("Dockerfile 未明确声明启动命令；可能继承基础镜像的命令，请确认镜像能够直接启动。")
        if len(ports) != 1:
            warnings.append("无法确定唯一应用端口，请填写程序实际监听的容器端口；EXPOSE 不会自动让程序监听该端口。")
    for filename in EXAMPLE_FILES:
        for line in files.get(filename, "").splitlines():
            match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
            if match:
                variables.setdefault(match[1], False)
    environment = []
    for name, required in variables.items():
        try:
            environment_name(name)
        except ValueError:
            warnings.append("示例配置包含部署工具保留变量，请在项目自己的部署脚本中处理。")
            continue
        environment.append({"name": name, "required": required})
    example_names = {match[1] for filename in EXAMPLE_FILES
                     for match in re.finditer(r"(?m)^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", files.get(filename, ""))}
    for choice in choices:
        selected = dict(by_file.get(choice["file"], {}))
        if choice["mode"] == "compose":
            family = "docker-compose" if choice["file"].startswith("docker-compose") else "compose"
            for suffix in ("yaml", "yml"):
                override = family + ".override." + suffix
                if override in files:
                    selected.update(by_file.get(override, {}))
                    break
        choice["environment"] = [{"name": item["name"], "required": bool(selected.get(item["name"]))}
                                 for item in environment if item["name"] in selected or item["name"] in example_names][:80]
    if choices:
        environment = choices[0]["environment"]
    return {"choices": choices, "ports": ports, "environment": environment[:80], "warnings": warnings}


def compose_text(config: dict, workdir: Path, directory: Path) -> str:
    import yaml

    config = normalize(config)
    host = "0.0.0.0" if config["access"] == "public" else "127.0.0.1"
    service = {
        "build": {"context": workdir.as_posix().replace("$", "$$"), "dockerfile": config["file"]},
        "restart": "unless-stopped",
        "ports": [f"{host}:{config['published_port']}:{config['container_port']}"],
    }
    if config["environment"]:
        service["environment"] = {name: "${" + name + ":?请在面板填写环境变量}" for name in config["environment"]}
    document = {"services": {"app": service}}
    if config["volumes"]:
        # Named volumes survive container replacement and preserve image directory ownership.
        service["volumes"] = [{"type": "volume", "source": f"data_{index}", "target": target}
                              for index, target in enumerate(config["volumes"])]
        document["volumes"] = {f"data_{index}": {"name": project_name(directory.name) + "-data-" + str(index)}
                               for index, _target in enumerate(config["volumes"])}
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False)


def project_name(key: str) -> str:
    safe = re.sub(r"[^a-z0-9_-]", "-", key.lower()).strip("-")
    return "mini-" + (safe if safe == key and safe else hashlib.sha256(key.encode()).hexdigest()[:20])


def port_ready(config: dict, key: str) -> bool:
    host = "0.0.0.0" if config["access"] == "public" else "127.0.0.1"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind((host, config["published_port"]))
        return True
    except OSError:
        # Existing instances of this managed project may legitimately own the port.
        try:
            result = subprocess.run(["docker", "ps", "--filter", "label=com.docker.compose.project=" + project_name(key),
                                     "--format", "{{.Ports}}"], capture_output=True, text=True, timeout=5, check=False)
            return result.returncode == 0 and bool(re.search(r":" + str(config["published_port"]) + r"->\d+/tcp", result.stdout))
        except (OSError, subprocess.SubprocessError):
            return False


def command(workdir: Path, config: dict, directory: Path, key: str) -> list[str]:
    args = ["docker", "compose", "--project-directory", str(workdir)]
    if config["mode"] == "dockerfile":
        args += ["--project-name", project_name(key), "--file", str(directory / "compose.yaml")]
    else:
        args += ["--file", str(workdir / config["file"])]
        # Explicit -f disables Compose's default override discovery; retain it ourselves.
        if config["file"] in COMPOSE_FILES:
            family = "docker-compose" if config["file"].startswith("docker-compose") else "compose"
            for suffix in ("yaml", "yml"):
                override = workdir / f"{family}.override.{suffix}"
                if override.is_file():
                    args += ["--file", str(override)]
                    break
    return args


def execution_environment(path: Path, names: list[str]) -> dict[str, str]:
    values = {}
    if names:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
            raise ValueError("环境变量尚未准备或文件不安全，请重新初始化项目")
        values = environment_values(json.loads(path.read_text(encoding="utf-8")), names)
    env = dict(os.environ)
    # Pass secrets through process environment, never shell source or command arguments.
    env.update(values)
    return env


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--key", required=True)
    args = parser.parse_args()
    try:
        plan = args.directory / "plan.json"
        if plan.is_symlink() or plan.stat().st_size > 64 * 1024:
            raise ValueError("部署计划文件不安全")
        config = normalize(json.loads(plan.read_text(encoding="utf-8")))
        env = execution_environment(args.directory / "environment.json", config["environment"])
        compose = command(args.workdir, config, args.directory, args.key)
        for operation in (["config", "--quiet"], ["up", "-d", "--build"], ["ps"]):
            result = subprocess.run(compose + operation, cwd=args.workdir, env=env, check=False)
            if result.returncode:
                return result.returncode
        return 0
    except (OSError, ValueError):
        print("容器运行配置或环境变量文件不可用，请在面板重新检查项目。", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
