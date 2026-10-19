"""Manage one owned swapfile, without altering external swap or deleting files."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import threading
import time
import uuid
from contextlib import nullcontext

from certificates import atomic_write, run, trusted_path

SIZE = 2 * 1024**3
TAG = '# mini-deploy-managed: swap-v1'


class Manager:
    def __init__(self, home, *, proc=Path('/proc'), fstab=Path('/etc/fstab'), runner=run,
                 supported=None, maintenance=nullcontext, directory=Path('/var/lib/mini-deploy-swap')):
        self.home = Path(home)
        # Keep the multi-GiB swapfile outside the panel's installation/data backup tree.
        self.directory = Path(directory)
        if not self.directory.is_absolute() or any(c.isspace() or c == '#' or (c == '\\' and os.name == 'posix') for c in str(self.directory)):
            raise ValueError('Swap 数据目录必须为不含空格的绝对路径')
        self.path = self.directory / 'swapfile'
        self.record = self.directory / 'owner.json'
        self.proc, self.fstab, self.run = Path(proc), Path(fstab), runner
        self.supported = (sys.platform == 'linux' and os.geteuid() == 0) if supported is None else supported
        self.maintenance = maintenance
        self.lock = threading.RLock()
        self.job = {}

    def _memory(self):
        result = {}
        for line in (self.proc / 'meminfo').read_text().splitlines():
            key, _, value = line.partition(':')
            if key in {'MemTotal', 'MemAvailable'}:
                result[key] = int(value.split()[0]) * 1024
        return result

    def _active(self):
        rows = []
        for line in (self.proc / 'swaps').read_text().splitlines()[1:]:
            columns = line.split()
            if len(columns) != 5:
                raise ValueError('无法识别系统 Swap 状态，未修改配置')
            rows.append({'path': columns[0], 'size': int(columns[2]) * 1024, 'used': int(columns[3]) * 1024})
        return rows

    def _owner(self):
        trusted_path(self.directory)
        if not self.record.exists():
            if self.path.exists():
                raise ValueError('预留 Swap 路径已有非托管文件，原文件保留')
            return None
        trusted_path(self.record)
        if self.record.stat().st_size > 4096:
            raise ValueError('Swap 所有权记录无效')
        data = json.loads(self.record.read_text())
        if not isinstance(data, dict) or data.get('marker') != TAG or data.get('size') != SIZE:
            raise ValueError('Swap 所有权记录不匹配，未操作原文件')
        trusted_path(self.path)
        info = self.path.stat()
        if ((info.st_dev, info.st_ino) != (data.get('device'), data.get('inode')) or
                (os.name == 'posix' and stat.S_IMODE(info.st_mode) != 0o600)):
            raise ValueError('Swap 文件身份或权限已变化，未操作原文件')
        return data

    def _fstab(self):
        trusted_path(self.fstab)
        if not self.fstab.is_file() or self.fstab.stat().st_size > 1024**2:
            raise ValueError('无法安全读取 /etc/fstab，未修改配置')
        return self.fstab.read_bytes().decode('utf-8')

    def _entry(self):
        return f'{self.path} none swap sw,nofail 0 0 {TAG}'

    def status(self):
        with self.lock:
            job = copy.deepcopy(self.job)
        result = {'supported': self.supported, 'editable': False, 'enabled': False, 'managed_active': False,
                  'persistent': False, 'owned': False, 'size_bytes': SIZE, 'job': job}
        if not self.supported:
            return {**result, 'detail': '仅支持以 root 运行的 Linux 主机；本地预览不操作 Swap。'}
        try:
            active, owner, fstab = self._active(), self._owner(), self._fstab()
            managed = next((row for row in active if row['path'] == str(self.path)), None)
            external = [row for row in active if row['path'] != str(self.path)]
            foreign_config = any(line.strip() and not line.lstrip().startswith('#') and
                len(line.split()) >= 3 and line.split()[2] == 'swap' and line.rstrip() != self._entry()
                for line in fstab.splitlines())
            result.update(enabled=bool(active), owned=bool(owner), managed_active=bool(managed), persistent=self._entry() in fstab.splitlines(),
                          external_count=len(external), editable=bool(owner) or not (external or foreign_config))
            result['detail'] = ('已有系统 Swap；面板保留原配置。' if (external or foreign_config) and not owner else
                '面板 Swap 已启用。' if managed and result['persistent'] else
                '面板 Swap 已启用，但尚未设置开机生效；重新开启可重试保存配置。' if managed else
                '开启后创建 2 GiB Swap，并设为开机生效。')
        except (OSError, ValueError, KeyError) as exc:
            result['detail'] = str(exc)
        return result

    def start(self, enabled, *, audit=lambda ok: None):
        if type(enabled) is not bool:
            raise ValueError('Swap 开关参数无效')
        with self.lock:
            if self.job.get('state') == 'running':
                raise ValueError('Swap 操作正在进行，请等待完成')
            if not self.status()['editable']:
                raise ValueError(self.status()['detail'])
            self.job = {'state': 'running', 'detail': '正在开启 Swap…' if enabled else '正在关闭面板 Swap…'}
            threading.Thread(target=self._work, args=(enabled, audit), daemon=True, name='host-swap').start()
        return self.status()

    def _persist(self, enabled):
        original = self._fstab()
        # Only the exact owned entry may be removed; all external lines survive byte-for-byte.
        lines = original.splitlines(keepends=True)
        if any(str(self.path) in line and line.rstrip('\r\n') != self._entry() and not line.lstrip().startswith('#') for line in lines):
            raise ValueError('fstab 已有不同的 Swap 文件配置，未覆盖；请核对当前生效状态')
        content = ''.join(line for line in lines if line.rstrip('\r\n') != self._entry())
        if enabled:
            content += ('' if not content or content.endswith('\n') else '\n') + self._entry() + '\n'
        if content == original:
            return
        backup = self.directory / f'fstab-{time.time_ns()}-{uuid.uuid4().hex[:8]}.bak'
        with backup.open('x', encoding='utf-8', newline='') as stream:
            os.chmod(backup, 0o600)
            stream.write(original)
        mode = stat.S_IMODE(self.fstab.stat().st_mode)
        if self._fstab() != original:
            raise ValueError('fstab 在操作期间发生变化，原文件和备份均保留')
        atomic_write(self.fstab, content)
        os.chmod(self.fstab, mode)

    def _allocate(self):
        if shutil.disk_usage(self.directory).free < SIZE + 1024**3:
            raise ValueError('空间不足：创建 2 GiB Swap 后至少需要保留 1 GiB 可用空间')
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(self.path, flags, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            info = os.fstat(stream.fileno())
            owner = {'marker': TAG, 'device': info.st_dev, 'inode': info.st_ino, 'size': SIZE, 'ready': False}
            atomic_write(self.record, json.dumps(owner))
            self._fill(stream)
        self.run(['mkswap', str(self.path)], timeout=30)
        owner['ready'] = True
        atomic_write(self.record, json.dumps(owner))

    def _fill(self, stream):
        block = bytes(8 * 1024**2)
        for _ in range(SIZE // len(block)):
            stream.write(block)
        stream.flush()
        os.fsync(stream.fileno())

    def _apply(self, enabled):
        state = self.status()
        if not state['editable']:
            raise ValueError(state['detail'])
        owner = self._owner()
        if enabled:
            if state['external_count'] and not state['managed_active']:
                raise ValueError('已有系统 Swap，未额外创建或开启交换文件')
            if not owner:
                self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
                trusted_path(self.directory)
                filesystem = self.run(['findmnt', '-n', '-o', 'FSTYPE', '-T', str(self.directory)], timeout=5).strip()
                if filesystem not in {'ext4', 'xfs'}:
                    raise ValueError('当前文件系统暂不支持一键创建 Swap；支持 ext4、XFS，原配置未修改')
                self._allocate()
                owner = self._owner()
            if not owner.get('ready') or self.path.stat().st_size != SIZE:
                raise ValueError('之前创建的 Swap 未完成，文件已保留，未再次格式化；请检查主机日志')
            if not state['managed_active']:
                self.run(['swapon', str(self.path)], timeout=30)
            if not any(row['path'] == str(self.path) for row in self._active()):
                raise RuntimeError('未确认 Swap 已生效，文件保留；请检查主机是否允许 swapon')
            self._persist(True)
        else:
            if not owner:
                raise ValueError('没有可关闭的面板 Swap；不操作系统原有 Swap')
            active = next((row for row in self._active() if row['path'] == str(self.path)), None)
            if active:
                memory = self._memory()
                reserve = max(256 * 1024**2, memory.get('MemTotal', 0) // 10)
                if memory.get('MemAvailable', 0) < active['used'] + reserve:
                    raise ValueError('可用内存不足，已阻止关闭 Swap；请先释放内存后重试')
                self.run(['swapoff', str(self.path)], timeout=120)
                if any(row['path'] == str(self.path) for row in self._active()):
                    raise RuntimeError('Swap 仍在使用，未移除开机配置')
            self._persist(False)

    def _work(self, enabled, audit):
        ok, detail = False, ''
        try:
            with self.maintenance():
                self._apply(enabled)
            ok = True
            detail = '2 GiB Swap 已开启，重启后自动生效。' if enabled else '面板 Swap 已关闭；交换文件保留，再次开启无需重建。'
        except Exception as exc:
            detail = f'操作未完成：{exc}。文件已保留，请核对当前状态后重试。'
        finally:
            try:
                audit(ok)
            finally:
                with self.lock:
                    self.job = {'state': 'succeeded' if ok else 'failed', 'detail': detail}
