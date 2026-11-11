"""Read-only host compatibility evidence shared by setup and the installer."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import sys


def read_text(path: Path, limit: int = 65536) -> str:
    try:
        with path.open(encoding='utf-8') as stream:
            value = stream.read(limit + 1)
        return value if len(value) <= limit else ''
    except (OSError, UnicodeError):
        return ''


def os_release(root: Path) -> dict[str, str]:
    text = read_text(root / 'etc/os-release') or read_text(root / 'usr/lib/os-release')
    result = {}
    for line in text.splitlines():
        key, separator, value = line.partition('=')
        if not separator or key not in {'ID', 'ID_LIKE', 'VERSION_ID', 'PRETTY_NAME'}:
            continue
        try:
            values = shlex.split(value, comments=True)
        except ValueError:
            continue
        if len(values) == 1 and all(c.isprintable() for c in values[0]):
            result[key] = values[0]
    return result


def distro_tier(identifier: str, version: str) -> str:
    if identifier == 'debian' and version in {'12', '13'}:
        return 'recommended'
    if identifier == 'ubuntu' and version in {'22.04', '24.04'}:
        return 'recommended'
    return 'experimental'


def inspect_host(*, root: Path = Path('/'), platform_name: str | None = None,
                 uid: int | None = None, architecture: str | None = None) -> dict:
    platform_name = sys.platform if platform_name is None else platform_name
    linux = platform_name == 'linux'
    release = os_release(root) if linux else {}
    identifier = release.get('ID', 'unknown')
    if not re.fullmatch(r'[a-z0-9_.-]{1,64}', identifier):
        identifier = 'unknown'
    version = release.get('VERSION_ID', '')
    if not re.fullmatch(r'[0-9A-Za-z_.-]{0,32}', version):
        version = ''
    uid = getattr(os, 'geteuid', lambda: -1)() if uid is None else uid
    systemd = linux and read_text(root / 'proc/1/comm').strip() == 'systemd'
    container = linux and any((root / name).exists() for name in ('.dockerenv', 'run/.containerenv'))
    selinux = read_text(root / 'sys/fs/selinux/enforce').strip() if linux else ''
    selinux = {'1': 'enforcing', '0': 'permissive'}.get(selinux, 'unavailable')
    manager = next((name for name in ('apt-get', 'dnf', 'yum') if linux and shutil.which(name)), '')
    tier = distro_tier(identifier, version)
    errors = []
    if not linux:
        errors.append('标准安装需要 Linux / Linux is required')
    if uid != 0:
        errors.append('标准安装需要 root 权限 / Run the installer with root or sudo')
    if linux and (not systemd or not shutil.which('systemctl') or not shutil.which('systemd-analyze')):
        errors.append('需要由 systemd 启动的主机（PID 1），并提供 systemctl、systemd-analyze / A running systemd host is required')
    if linux and not (root / 'proc/meminfo').is_file():
        errors.append('主机 /proc 不可读，无法采集资源 / Host /proc is unavailable')
    if container:
        errors.append('普通容器不是受支持的主机安装环境 / Installing the host panel inside a container is unsupported')
    name = (release.get('PRETTY_NAME') or f'{identifier} {version}'.strip()) if linux else platform_name
    detail = f'{name} · {architecture or platform.machine()} · Python {platform.python_version()}'
    notices = []
    if tier != 'recommended' and linux:
        notices.append('此发行版或版本尚未完成整机兼容验收；推荐 Debian 12/13、Ubuntu 22.04/24.04 / This distribution needs host acceptance testing')
    if selinux == 'enforcing':
        notices.append('SELinux 为 enforcing；保留现有策略，Nginx 代理和自定义路径须单独验收，失败时检查 AVC 日志 / SELinux policy is preserved; verify proxy access and custom paths')
    if not manager and linux:
        notices.append('未识别自动安装依赖的包管理器，请预装可选组件 / Install optional dependencies manually')
    return {'id': identifier, 'version': version, 'name': name, 'architecture': architecture or platform.machine(),
            'tier': tier, 'systemd': systemd, 'root': uid == 0, 'container': container,
            'selinux': selinux, 'package_manager': manager, 'python': sys.executable,
            'state': 'unsupported' if errors else 'pending' if notices else 'available',
            'detail': '；'.join([detail, *errors, *notices]), 'errors': errors, 'notices': notices}


def main() -> int:
    parser = argparse.ArgumentParser(description='Read-only Linux host compatibility check')
    parser.add_argument('--check', action='store_true', help='exit nonzero when host requirements are missing')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    result = inspect_host()
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f'系统环境 / Host: {result["name"]} ({result["architecture"]})')
        print(f'Python: {result["python"]} ({platform.python_version()})')
        for detail in result['errors']:
            print(f'[FAIL] {detail}')
        for detail in result['notices']:
            print(f'[NOTE] {detail}')
        if not result['errors']:
            print('[OK] 主机前提检查通过；可选功能仍需分别验收 / Host prerequisites passed; verify optional features separately')
    return 1 if args.check and result['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
