"""Root-owned, externally executed panel maintenance. Never prune server files."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.request import ProxyHandler, build_opener

from certificates import trusted_path
from scripts.verify_backup import verify_backup

REMOTES = {'gitee': ('https://gitee.com/XC1960/mini_deploy.git', 'master'),
           'github': ('https://github.com/jokerXueC/mini_deploy.git', 'main')}
ENTRIES = (
    'agent.py', 'ip_security.py', 'security_observation.py', 'security_center.py', 'cpu_metrics.py', 'server_operations.py', 'performance_metrics.py', 'server_performance.py', 'server_processes.py', 'resource_alerts.py', 'prometheus_metrics.py', 'panel_maintenance.py',
    'release.json', 'certificates.py', 'certificate_discovery.py', 'nginx_runtime.py', 'nginx_install.py',
    'nginx_requests.py', 'caddy_requests.py', 'request_gateway.py', 'request_history.py', 'request_export.py', 'gateway_connections.py',
    'monitoring.py', 'container_monitoring.py', 'docker_events.py', 'docker_mirrors.py', 'ssh_terminal.py',
    'ssh_files.py', 'ssh_file_manager.py', 'terminal_ai.py', 'panel_diagnostics.py', 'terminal_model_io.py', 'terminal_tools.py', 'terminal_history.py', 'requirements-ssh.txt',
    'install.sh', 'env.example', 'server.env.example', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'ui',
    'examples/nginx.compose.yml', 'systemd/mini-deploy-agent.service', 'scripts/bootstrap_server.sh',
    'scripts/backup-installation.sh', 'scripts/doctor.sh', 'scripts/verify_backup.py')
KEY = re.compile(r'[a-f0-9]{32}')
BACKUP_KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,120}')
MAX_FILES, MAX_SIZE = 6000, 192 * 1024**2
LOCK = threading.RLock()


def digest(path):
    with path.open('rb') as stream:
        result = hashlib.sha256()
        for block in iter(lambda: stream.read(1024**2), b''):
            result.update(block)
    return result.hexdigest()


def write_json(path, data):
    trusted_path(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_json(path):
    trusted_path(path)
    if not path.is_file() or path.stat().st_size > 2 * 1024**2:
        raise ValueError('维护记录无效或过大，原文件已保留')
    return json.loads(path.read_text(encoding='utf-8'))


def command(argv, timeout=30, env=None):
    # Use a file rather than an unbounded pipe for git/systemd diagnostic output.
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                timeout=timeout, check=False, env=env)
        size = output.tell()
        output.seek(max(0, size - 8192))
        text = output.read(8192).decode('utf-8', errors='replace').strip()
    if result.returncode:
        raise RuntimeError(f'{Path(argv[0]).name} 执行失败：{text}')
    return text


def release_files(root, entries=ENTRIES, required=True):
    files, total = [], 0
    for entry in entries:
        path = root / entry
        trusted_path(path)
        if not path.exists():
            if required:
                raise ValueError(f'更新包缺少 {entry}，请使用安装脚本升级')
            continue
        for item in ([path] if path.is_file() else path.rglob('*')):
            trusted_path(item)
            if item.is_dir():
                continue
            if item.stat().st_size > 32 * 1024**2:
                raise ValueError('程序文件过大，拒绝更新')
            total += item.stat().st_size
            files.append(item.relative_to(root).as_posix())
            if len(files) > MAX_FILES or total > MAX_SIZE:
                raise ValueError('更新包超出面板程序大小限制')
    return sorted(set(files)), total


def allowed(relative):
    parts = PurePosixPath(relative)
    if parts.is_absolute() or '..' in parts.parts or '\\' in relative or not parts.parts:
        return False
    return relative in (*ENTRIES, '.mini-deploy-install', '.mini-deploy-release.json') or relative.startswith('ui/')


def copy_files(source, target, files):
    for relative in files:
        if not allowed(relative):
            raise ValueError('恢复清单包含非面板程序路径')
        src, dst = source / relative, target / relative
        trusted_path(src)
        trusted_path(dst)
        if not src.is_file():
            raise ValueError('程序文件不可读取')
        dst.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.panel-pending-', dir=dst.parent)
        with os.fdopen(descriptor, 'wb') as out, src.open('rb') as inp:
            shutil.copyfileobj(inp, out)
            os.fchmod(out.fileno(), stat.S_IMODE(src.stat().st_mode) & 0o755) if hasattr(os, 'fchmod') else None
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, dst)


class Maintenance:
    def __init__(self, app, home, env, log, service='mini-deploy-agent', backup=Path('/var/backups/mini-deploy-agent')):
        self.app, self.home, self.env, self.log = map(Path, (app, home, env, log))
        self.service, self.backup = service, Path(backup)
        self.directory = self.home / 'panel-maintenance'
        self.settings = dict(app=str(self.app), home=str(self.home), env=str(self.env), log=str(self.log),
                             service=service, backup=str(self.backup))

    def supported(self):
        if os.name != 'posix' or os.geteuid() != 0 or not shutil.which('systemd-run') or not Path('/run/systemd/system').is_dir():
            raise ValueError('面板维护需要 Linux、root 和运行中的 systemd；本地预览不可执行更新')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', self.service):
            raise ValueError('服务名称无效')
        trusted_path(self.app / 'panel_maintenance.py')
        for path in (self.app, self.home, self.env, self.log, self.backup, self.directory):
            trusted_path(path)
        marker = self.app / '.mini-deploy-install'
        trusted_path(marker)
        fields = dict(line.split('=', 1) for line in marker.read_text().splitlines() if '=' in line)
        if fields.get('format') != '2' or fields.get('app_home') != str(self.app) or fields.get('release_fingerprint') != 'sha256:' + digest(self.app / 'agent.py'):
            raise ValueError('安装标记或程序指纹不一致，请先通过 install.sh 升级')
        unit = Path('/etc/systemd/system') / (self.service + '.service')
        trusted_path(unit)
        text = unit.read_text()
        if '# mini_deploy-managed: agent-systemd-v1' not in text:
            raise ValueError('服务不是面板管理的安装，拒绝操作')
        current = command(['systemctl', 'show', self.service, '--property=FragmentPath', '--value'])
        if current != str(unit) or any((Path(str(unit) + '.d')).glob('*.conf')):
            raise ValueError('服务存在自定义覆盖，请使用安装脚本更新')
        working = command(['systemctl', 'show', self.service, '--property=WorkingDirectory', '--value'])
        executable = command(['systemctl', 'show', self.service, '--property=ExecStart', '--value'])
        if working != str(self.app) or str(self.app / 'agent.py') not in executable:
            raise ValueError('服务实际启动路径与本安装不一致')

    def version(self):
        value = {'fingerprint': digest(self.app / 'agent.py') if (self.app / 'agent.py').is_file() else '',
                 'revision': '', 'installed_at': ''}
        marker = self.app / '.mini-deploy-install'
        trusted_path(marker)
        if marker.is_file() and marker.stat().st_size < 8192:
            fields = dict(line.split('=', 1) for line in marker.read_text().splitlines() if '=' in line)
            if fields.get('release_fingerprint') == 'sha256:' + value['fingerprint']:
                revision = fields.get('revision', '')
                value['revision'] = revision if re.fullmatch('[a-f0-9]{40}', revision) else ''
                value['installed_at'] = fields.get('installed_at', '')
        path = self.app / '.mini-deploy-release.json'
        if path.exists():
            info = read_json(path)
            if info.get('fingerprint') == value['fingerprint'] and not value['revision']:
                value.update({k: info.get(k, '') for k in ('revision', 'installed_at')})
        return value

    def status(self):
        warning = ''
        try:
            self.supported()
        except (OSError, ValueError, RuntimeError) as exc:
            warning = str(exc)
        jobs, backups = [], []
        trusted_path(self.directory)
        if self.directory.exists():
            for path in sorted(self.directory.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:100]:
                if KEY.fullmatch(path.stem):
                    job = read_json(path)
                    if not warning and job.get('state') in {'queued', 'running'} and time.time() - job['started_at'] > 30:
                        active = command(['systemctl', 'show', 'mini-deploy-maintenance-' + job['id'], '--property=ActiveState', '--value'])
                        if active not in {'active', 'activating', 'reloading'}:
                            job.update(state='failed', phase='维护进程已中断，请检查服务和保留的备份',
                                       error='不自动重试中断的更新；文件和恢复快照保留')
                    jobs.append({k: job.get(k) for k in ('id', 'action', 'state', 'phase', 'started_at', 'finished_at', 'error', 'backup', 'revision')})
        trusted_path(self.backup)
        if self.backup.exists():
            with os.scandir(self.backup) as entries:
                for index, item in enumerate(entries):
                    if index >= 500:
                        break
                    if not BACKUP_KEY.fullmatch(item.name) or not item.is_dir(follow_symlinks=False):
                        continue
                    path = Path(item.path)
                    trusted_path(path)
                    kind = 'program' if (path / 'program.json').is_file() else 'full' if (path / 'complete').is_file() else ''
                    if kind:
                        backups.append({'id': item.name, 'kind': kind, 'at': item.stat().st_mtime,
                                        'verified': False})
        return {'supported': not warning, 'warning': warning, 'version': self.version(),
                'jobs': sorted(jobs, key=lambda j: j['started_at'] or 0, reverse=True)[:10],
                'backups': sorted(backups, key=lambda b: b['at'], reverse=True)[:20], 'backup_directory': str(self.backup),
                'notice': '只更新或恢复面板程序，不删除文件。完整备份含配置和数据；恢复按钮仅恢复程序版本，保留当前数据与配置。'}

    def git_env(self):
        result = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        result.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                      GIT_CONFIG_COUNT='2', GIT_CONFIG_KEY_0='core.hooksPath', GIT_CONFIG_VALUE_0=os.devnull,
                      GIT_CONFIG_KEY_1='protocol.file.allow', GIT_CONFIG_VALUE_1='never')
        return result

    def check(self, channel):
        if not isinstance(channel, str) or channel not in REMOTES:
            raise ValueError('请选择 GitHub 或 Gitee')
        self.supported()
        url, branch = REMOTES[channel]
        output = command(['git', 'ls-remote', '--heads', url, 'refs/heads/' + branch], env=self.git_env())
        match = re.fullmatch(r'([0-9a-f]{40})\s+refs/heads/' + branch, output)
        if not match:
            raise ValueError('远端版本读取失败')
        result = {'channel': channel, 'revision': match[1], 'checked_at': time.time()}
        write_json(self.directory / 'checked.json', result)
        return result

    def start(self, data):
        with LOCK:
            self.supported()
            action = data.get('action')
            if not isinstance(action, str) or action not in {'backup', 'verify', 'update', 'restore'} or data.get('confirm') is not True:
                raise ValueError('请确认维护操作')
            if any(j['state'] in {'queued', 'running'} for j in self.status()['jobs']):
                raise ValueError('已有维护任务正在执行，请等待完成')
            job = {'id': uuid.uuid4().hex, 'action': action, 'state': 'queued', 'phase': '等待独立维护进程',
                   'started_at': time.time(), 'settings': self.settings}
            if action in {'verify', 'restore'}:
                backup = str(data.get('backup', ''))
                if not BACKUP_KEY.fullmatch(backup):
                    raise ValueError('请选择备份列表中的记录')
                if action == 'restore' and not (self.backup / backup / 'program.json').is_file():
                    raise ValueError('完整备份需单独核对配置；自动恢复只支持程序快照')
                job['backup'] = backup
            if action == 'update':
                checked = read_json(self.directory / 'checked.json')
                if (checked.get('revision') != data.get('revision') or checked.get('channel') != data.get('channel')
                        or checked.get('channel') not in REMOTES or not re.fullmatch('[a-f0-9]{40}', str(checked.get('revision', '')))
                        or not 0 <= time.time() - checked['checked_at'] <= 900):
                    raise ValueError('版本检查已过期，请重新检查更新')
                job.update(revision=checked['revision'], channel=checked['channel'])
            write_json(self.directory / (job['id'] + '.json'), job)
            try:
                command(['systemd-run', '--quiet', '--collect', '--unit=mini-deploy-maintenance-' + job['id'],
                         '--property=Type=exec', '--property=UMask=0077',
                         sys.executable, str(self.app / 'panel_maintenance.py'), '--worker', str(self.directory / (job['id'] + '.json'))])
            except Exception:
                job.update(state='failed', phase='维护进程启动失败', finished_at=time.time())
                write_json(self.directory / (job['id'] + '.json'), job)
                raise
            return {'job': job['id']}

    def snapshot(self):
        files, size = release_files(self.app, required=False)
        for extra in ('.mini-deploy-install', '.mini-deploy-release.json'):
            if (self.app / extra).exists():
                files.append(extra)
        if shutil.disk_usage(self.backup).free < size * 2 + 64 * 1024**2:
            raise ValueError('备份空间不足，未修改程序')
        key = 'program-' + time.strftime('%Y%m%d-%H%M%S', time.gmtime()) + '-' + uuid.uuid4().hex[:8]
        folder = self.backup / key
        folder.mkdir(mode=0o700)
        copy_files(self.app, folder / 'files', files)
        manifest = {'format': 1, 'app': str(self.app), 'created_at': time.time(),
                    'version': self.version(), 'files': {p: digest(folder / 'files' / p) for p in files}}
        write_json(folder / 'program.json', manifest)
        self.verify_program(key)
        return key

    def verify_program(self, key):
        if not BACKUP_KEY.fullmatch(key):
            raise ValueError('备份 ID 无效')
        folder = self.backup / key
        data = read_json(folder / 'program.json')
        if data.get('format') != 1 or data.get('app') != str(self.app) or not 1 <= len(data.get('files', {})) <= MAX_FILES:
            raise ValueError('程序快照无效或属于另一安装路径')
        total = 0
        for relative, sha in data['files'].items():
            if not allowed(relative) or not re.fullmatch('[a-f0-9]{64}', str(sha)):
                raise ValueError('快照包含非程序路径或无效校验值')
            path = folder / 'files' / relative
            trusted_path(path)
            total += path.stat().st_size
            if total > MAX_SIZE or digest(path) != sha:
                raise ValueError('程序快照校验失败，未恢复')
            trusted_path(self.app / relative)
        if data['files'].get('agent.py') != data.get('version', {}).get('fingerprint'):
            raise ValueError('快照版本指纹不一致')
        return data

    def health(self, expected, seconds=45):
        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                with opener.open('http://127.0.0.1:6868/health', timeout=2) as response:
                    data = json.loads(response.read(4096))
                if data.get('status') == 'ok' and data.get('release') == expected:
                    command(['systemctl', 'is-active', '--quiet', self.service], timeout=5)
                    return
            except (OSError, ValueError, RuntimeError):
                pass
            time.sleep(1)
        raise RuntimeError('服务未通过本地健康与运行版本检查')

    def restore(self, key):
        data = self.verify_program(key)
        self.restore_compatible(key)
        copy_files(self.backup / key / 'files', self.app, data['files'])
        command(['systemctl', 'start', self.service])
        self.health(data['version']['fingerprint'])

    def restore_compatible(self, key):
        contract = self.backup / key / 'files/release.json'
        if read_json(contract) != read_json(self.app / 'release.json'):
            raise ValueError('所选程序版本的数据或运行环境约定不同，拒绝自动恢复')

    def stage(self, job):
        folder = self.backup / ('update-' + job['id'])
        folder.mkdir(mode=0o700)
        url, branch = REMOTES[job['channel']]
        source = folder / 'source'
        command(['git', 'clone', '--depth=1', '--single-branch', '--branch', branch, url, str(source)],
                timeout=180, env=self.git_env())
        revision = command(['git', '-C', str(source), 'rev-parse', 'HEAD'], env=self.git_env())
        if revision != job['revision']:
            raise ValueError('远端版本已变化，请重新检查更新；未修改已安装程序')
        contract = read_json(source / 'release.json')
        if contract != read_json(self.app / 'release.json'):
            raise ValueError('更新涉及安装或数据兼容变化，请使用 install.sh 升级')
        files, size = release_files(source)
        if shutil.disk_usage(self.app).free < size * 2 + 64 * 1024**2:
            raise ValueError('安装磁盘空间不足')
        for relative in files:
            if relative.endswith('.py'):
                compile((source / relative).read_bytes(), relative, 'exec')
        return source, files

    @contextmanager
    def exclusive(self):
        import fcntl
        lock = Path('/run/mini-deploy-agent/maintenance.lock')
        trusted_path(lock)
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0 or info.st_mode & 0o077:
                raise ValueError('维护锁权限不安全，未修改程序')
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def worker(self, job):
        path = self.directory / (job['id'] + '.json')
        def phase(value):
            job.update(state='running', phase=value)
            write_json(path, job)
        rollback = None
        try:
            self.supported()
            trusted_path(self.backup)
            self.backup.mkdir(mode=0o700, parents=True, exist_ok=True)
            source = None
            if job['action'] == 'update':
                phase('下载并校验官方更新包')
                source, files = self.stage(job)
                # The remaining operation is independent of the downloaded checkout.
            if job['action'] == 'verify':
                phase('校验备份文件')
                folder = self.backup / job['backup']
                if (folder / 'program.json').exists():
                    self.verify_program(job['backup'])
                else:
                    verify_backup(folder)
                job.update(state='succeeded', phase='备份校验通过')
            elif job['action'] == 'backup':
                phase('创建并校验完整备份')
                env = dict(os.environ, APP_HOME=str(self.app), DATA_HOME=str(self.home), ENV_FILE=str(self.env),
                           LOG_HOME=str(self.log), SERVICE_NAME=self.service, BACKUP_DIR=str(self.backup))
                bundle = command(['bash', str(self.app / 'scripts/backup-installation.sh')], timeout=600, env=env)
                verified = verify_backup(Path(bundle))
                job['full_backup'] = verified.bundle.name
                with self.exclusive():
                    job['backup'] = self.snapshot()
                job.update(state='succeeded', phase='完整备份与程序快照已创建并校验')
            else:
                if job['action'] == 'restore':
                    self.verify_program(job['backup'])
                    self.restore_compatible(job['backup'])
                phase('保存当前程序版本')
                with self.exclusive():
                    rollback = self.snapshot()
                    job['recovery_backup'] = rollback
                    if job['action'] == 'update':
                        job['backup'] = rollback
                    phase('停止面板服务并切换程序')
                    try:
                        command(['systemctl', 'stop', self.service])
                        if source:
                            copy_files(source, self.app, files)
                            fingerprint = digest(self.app / 'agent.py')
                            marker = self.app / '.mini-deploy-install'
                            content = f'format=2\ninstalled_at={time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}\napp_home={self.app}\nrelease_fingerprint=sha256:{fingerprint}\nrevision={job["revision"]}\n'
                            descriptor, temp = tempfile.mkstemp(prefix='.panel-marker-', dir=self.app)
                            with os.fdopen(descriptor, 'w') as out:
                                out.write(content)
                                out.flush()
                                os.fsync(out.fileno())
                            os.chmod(temp, 0o644)
                            os.replace(temp, marker)
                            write_json(self.app / '.mini-deploy-release.json', {'revision': job['revision'], 'fingerprint': fingerprint, 'installed_at': time.time()})
                            command(['systemctl', 'start', self.service])
                            phase('检查服务与运行版本')
                            self.health(fingerprint)
                        else:
                            phase('恢复所选程序版本并检查服务')
                            self.restore(job['backup'])
                    except Exception:
                        phase('更新失败，恢复操作前的程序版本')
                        command(['systemctl', 'stop', self.service])
                        self.restore(rollback)
                        job['recovered'] = True
                        raise
                job.update(state='succeeded', phase='程序更新并通过健康检查' if source else '程序版本已恢复，当前数据与配置保留')
        except Exception as exc:
            job.update(state='failed', error=str(exc)[:2000], phase='操作失败，原文件和备份保留' + ('；旧程序已恢复并通过检查' if job.get('recovered') else '；请检查服务状态'))
        finally:
            job['finished_at'] = time.time()
            write_json(path, job)


def main():
    if len(sys.argv) != 3 or sys.argv[1] != '--worker':
        raise SystemExit('This module is executed only by the panel maintenance job.')
    path = Path(sys.argv[2])
    job = read_json(path)
    if not KEY.fullmatch(str(job.get('id', ''))) or path.name != job['id'] + '.json' or job.get('state') != 'queued':
        raise SystemExit('Invalid maintenance job')
    maintenance = Maintenance(**job['settings'])
    if path != maintenance.directory / path.name:
        raise SystemExit('Invalid maintenance directory')
    maintenance.worker(job)


if __name__ == '__main__':
    main()
