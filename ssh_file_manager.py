"""Session-scoped SFTP browser and bounded transfers. No server deletion APIs."""
from __future__ import annotations

import base64
import codecs
import errno
import hashlib
import posixpath
import re
import secrets
import stat
import tempfile
import threading
import time

import ssh_files

MAX_TRANSFER = 32 * 1024**2
CHUNK = 256 * 1024
HASH = re.compile('[a-f0-9]{64}')


def path_value(value, home=False):
    if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ssh_files.FileError('路径无效，请填写远程 Linux 路径')
    if home and value == '.':
        return value
    if not value.startswith('/'):
        raise ssh_files.FileError('请填写绝对路径，例如 /var/log')
    return posixpath.normpath(value)


def metadata(info):
    mode = info.st_mode or 0
    return {'size': info.st_size, 'modified': info.st_mtime, 'mode': stat.filemode(mode),
            'uid': info.st_uid, 'gid': info.st_gid, 'kind': 'directory' if stat.S_ISDIR(mode) else
            'file' if stat.S_ISREG(mode) else 'link' if stat.S_ISLNK(mode) else 'special'}


def hash_remote(client, path, check, output=None, progress=None):
    info = ssh_files._regular(client, path)
    if info.st_size > MAX_TRANSFER:
        raise ssh_files.FileError('文件超过 32 MiB，本轮不进行传输或覆盖')
    digest, size = hashlib.sha256(), 0
    with client.open(path, 'rb') as stream:
        while True:
            check()
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_TRANSFER:
                raise ssh_files.FileError('读取期间文件超过 32 MiB，已停止')
            digest.update(chunk)
            if output:
                output.write(chunk)
            if progress:
                progress(size)
    after = client.lstat(path)
    if (info.st_size, info.st_mtime, info.st_mode) != (after.st_size, after.st_mtime, after.st_mode) or size != info.st_size:
        raise ssh_files.FileError('读取期间文件发生变化，请重新读取')
    return info, digest.hexdigest(), size


class Files:
    def __init__(self, session):
        self.session = session
        self.lock, self.operation_lock = threading.RLock(), threading.Lock()
        self.jobs = {}

    def close(self):
        with self.lock:
            for job in self.jobs.values():
                job['cancel'].set()
                if job['state'] != 'running':
                    job['stream'].close()
            self.jobs = {key: job for key, job in self.jobs.items() if job['state'] == 'running'}

    def purge(self):
        for key, job in list(self.jobs.items()):
            if time.monotonic() - job['touched'] > 600:
                job['cancel'].set()
                if job['state'] != 'running':
                    job['stream'].close()
                    self.jobs.pop(key)

    def operate(self, data):
        action = data.get('action')
        if not isinstance(action, str):
            raise ssh_files.FileError('文件操作类型无效')
        if action in {'upload_begin', 'upload_chunk', 'upload_commit', 'download_begin', 'transfer', 'cancel', 'download_chunk'}:
            return self.transfer(data)
        if not self.operation_lock.acquire(blocking=False):
            raise ssh_files.FileError('正在读取或保存文件，请稍后重试')
        try:
            return self.session.execute_sftp(lambda client, check: self.browse(client, check, data), threading.Event(), timeout=30)
        except ssh_files.FileError as exc:
            if 'inspect_file' in str(exc):
                raise ssh_files.FileError('远程文件版本已变化，未覆盖；请重新打开文件，核对最新内容后再保存。') from None
            raise
        finally:
            self.operation_lock.release()

    def browse(self, client, check, data):
        action = data.get('action')
        path = path_value(data.get('path', '.'), home=action == 'list')
        if action == 'list':
            real = client.normalize(path)
            info = client.stat(real)
            if not stat.S_ISDIR(info.st_mode):
                raise ssh_files.FileError('该路径不是目录')
            offset = data.get('offset', 0)
            if type(offset) is not int or not 0 <= offset <= 2000:
                raise ssh_files.FileError('目录分页参数无效')
            items, limited, scanned = [], False, 0
            iterator = client.listdir_iter(real, read_aheads=1)
            try:
                for info in iterator:
                    check()
                    if scanned >= 2000:
                        limited = True
                        break
                    scanned += 1
                    name = info.filename
                    if not name or name in {'.', '..'} or '/' in name:
                        continue
                    if data.get('hidden') is not True and name.startswith('.'):
                        continue
                    items.append({'name': name, **metadata(info)})
            finally:
                iterator.close()
            items.sort(key=lambda i: (i['kind'] != 'directory', i['name'].casefold()))
            return {'path': real, 'entries': items[offset:offset + 200], 'total': len(items),
                    'next_offset': offset + 200 if offset + 200 < len(items) else None,
                    'limited': limited, 'notice': '目录读取最多 2000 项；符号链接只展示，不打开或修改。' if limited else ''}
        if action in {'read', 'inspect'}:
            info = ssh_files._regular(client, path)
            result = {'path': path, **metadata(info)}
            if action == 'inspect':
                _, digest, _ = hash_remote(client, path, check)
                return {**result, 'sha256': digest}
            if info.st_size > ssh_files.MAX_FILE:
                with client.open(path, 'rb') as stream:
                    raw = stream.read(64 * 1024)
                try:
                    text = codecs.getincrementaldecoder('utf-8')().decode(raw, final=False)
                except UnicodeDecodeError:
                    raise ssh_files.FileError('文件不是 UTF-8 文本，请下载查看') from None
                return {**result, 'content': ssh_files._text(text.encode()), 'editable': False,
                        'notice': '文件超过 1 MiB，仅预览开头 64 KiB；请下载查看完整内容。'}
            _, content = ssh_files._read(client, path)
            return {**result, 'content': ssh_files._text(content), 'sha256': hashlib.sha256(content).hexdigest(), 'editable': True}
        if action in {'preview', 'save', 'new_file'}:
            content = data.get('content')
            expected = data.get('expected_sha256', '')
            if not isinstance(content, str) or len(content.encode('utf-8')) > ssh_files.MAX_FILE or '\0' in content:
                raise ssh_files.FileError('仅支持至多 1 MiB 的 UTF-8 文本')
            if not isinstance(expected, str) or expected and not HASH.fullmatch(expected):
                raise ssh_files.FileError('文件版本摘要无效')
            if action == 'new_file' and expected:
                raise ssh_files.FileError('新文件不得覆盖现有文件')
            args = {'path': path, 'content': content, 'expected_sha256': expected}
            plan = {'name': 'preview_file', 'operation': 'write_file', 'arguments': args}
            if action == 'preview':
                return ssh_files.execute(client, plan, check)
            if data.get('confirm') is not True:
                raise ssh_files.FileError('请先确认保存')
            preview = data.get('preview')
            if action == 'save' and (not isinstance(preview, dict) or preview.get('before_sha256') != expected
                                     or preview.get('after_sha256') != hashlib.sha256(content.encode()).hexdigest()):
                raise ssh_files.FileError('保存内容与预览不一致，请重新预览')
            return ssh_files._update(client, 'write_file', args, check, preview if action == 'save' else None)
        if action == 'mkdir':
            if data.get('confirm') is not True:
                raise ssh_files.FileError('请确认创建文件夹')
            parent, name = posixpath.split(path)
            if not name:
                raise ssh_files.FileError('请填写新文件夹名称')
            # The target does not exist yet; validate its canonical parent.
            ssh_files._guard_write(client, parent)
            if client.normalize(parent) != parent:
                raise ssh_files.FileError('文件夹路径含符号链接')
            ssh_files._guard_write(client, path)
            check()
            client.mkdir(path, mode=0o755)
            return {'path': path, 'created': True}
        raise ssh_files.FileError('不支持该文件操作；不提供删除或移动')

    def public(self, job):
        return {k: job.get(k) for k in ('id', 'action', 'path', 'state', 'phase', 'size', 'received', 'progress', 'error', 'result')}

    def transfer(self, data):
        with self.lock:
            self.purge()
            action = data['action']
            if action in {'upload_begin', 'download_begin'}:
                if len(self.jobs) >= 2:
                    raise ssh_files.FileError('已有两个传输任务，请完成或取消后重试')
                path = path_value(data.get('path'))
                size = data.get('size', 0)
                if type(size) is not int or not 0 <= size <= MAX_TRANSFER:
                    raise ssh_files.FileError('每个文件最多 32 MiB')
                expected = data.get('expected_sha256', '')
                if not isinstance(expected, str) or expected and not HASH.fullmatch(expected):
                    raise ssh_files.FileError('覆盖文件前须核对有效的原文件摘要')
                job = {'id': secrets.token_urlsafe(24), 'action': 'upload' if action == 'upload_begin' else 'download',
                       'path': path, 'size': size, 'expected': expected, 'state': 'receiving', 'phase': '接收文件',
                       'received': 0, 'progress': 0, 'cancel': threading.Event(), 'stream': tempfile.TemporaryFile(),
                       'touched': time.monotonic()}
                self.jobs[job['id']] = job
                if action == 'download_begin':
                    self.launch(job)
                return self.public(job)
            job = self.jobs.get(str(data.get('job', '')))
            if job is None:
                raise ssh_files.FileError('传输记录不存在或已过期')
            job['touched'] = time.monotonic()
            if action == 'cancel':
                job['cancel'].set()
                if job['state'] != 'running':
                    job['stream'].close()
                    self.jobs.pop(job['id'])
                return {'cancelled': True, 'notice': '已停止等待或请求取消；已提交的写入不会撤销，远程备份与候选文件保留。'}
            if action == 'upload_chunk':
                if job['action'] != 'upload' or job['state'] != 'receiving' or data.get('offset') != job['received']:
                    raise ssh_files.FileError('上传顺序或状态无效，不自动重试重复分块')
                raw = data.get('data')
                if not isinstance(raw, str) or len(raw) > CHUNK * 4 // 3 + 8:
                    raise ssh_files.FileError('上传分块过大')
                try:
                    chunk = base64.b64decode(raw, validate=True)
                except ValueError:
                    raise ssh_files.FileError('上传分块编码无效') from None
                if not chunk or len(chunk) > CHUNK or job['received'] + len(chunk) > job['size']:
                    raise ssh_files.FileError('上传分块大小与预期不符')
                job['stream'].write(chunk)
                job['received'] += len(chunk)
            elif action == 'upload_commit':
                if job['state'] != 'receiving' or job['action'] != 'upload' or job['received'] != job['size'] or data.get('confirm') is not True:
                    raise ssh_files.FileError('文件未接收完整或未确认上传')
                self.launch(job)
            elif action == 'download_chunk':
                offset = data.get('offset')
                if job['action'] != 'download' or job['state'] != 'succeeded' or type(offset) is not int or not 0 <= offset <= job['size']:
                    raise ssh_files.FileError('下载状态或分块位置无效')
                job['stream'].seek(offset)
                chunk = job['stream'].read(CHUNK)
                return {'data': base64.b64encode(chunk).decode(), 'offset': offset,
                        'next_offset': offset + len(chunk), 'eof': offset + len(chunk) >= job['size']}
            elif action != 'transfer':
                raise ssh_files.FileError('不支持该传输操作')
            return self.public(job)

    def launch(self, job):
        job.update(state='running', phase='准备 SFTP 传输')
        threading.Thread(target=self.worker, args=(job,), name='ssh-file-transfer', daemon=True).start()

    def worker(self, job):
        try:
            result = self.session.execute_sftp(lambda client, check: self.download(client, check, job)
                if job['action'] == 'download' else self.upload(client, check, job), job['cancel'], timeout=300)
            with self.lock:
                job.update(state='succeeded', result=result, phase='传输完成')
        except Exception as exc:
            with self.lock:
                job.update(state='cancelled' if job['cancel'].is_set() else 'failed', error=str(exc),
                           phase='传输停止；远程文件、备份和候选文件保留')
        finally:
            with self.lock:
                if job['action'] == 'upload' or job['state'] != 'succeeded' or self.session.closed or job['cancel'].is_set():
                    job['stream'].close()
                if self.session.closed or job['cancel'].is_set():
                    self.jobs.pop(job['id'], None)
            self.session.audit('ssh_file_transfer', {**self.session.spec, 'path': job['path'],
                               'action': job['action'], 'state': job['state'], 'size': job['size']})

    def update(self, job, **values):
        with self.lock:
            job.update(values)

    def download(self, client, check, job):
        self.update(job, phase='读取远程文件', size=ssh_files._regular(client, job['path']).st_size)
        _, digest, size = hash_remote(client, job['path'], check, job['stream'], lambda done: self.update(job, progress=done))
        job['stream'].flush()
        self.update(job, size=size)
        return {'sha256': digest, 'size': size}

    def upload(self, client, check, job):
        path, stream = job['path'], job['stream']
        ssh_files._guard_write(client, path)
        info, previous = None, ''
        try:
            info, previous, _ = hash_remote(client, path, check)
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise
        if previous != job['expected']:
            raise ssh_files.FileError('原文件版本改变，未覆盖；请重新读取并确认')
        suffix = time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(6)
        backup = path + '.mini-deploy-backup.' + suffix if info else ''
        staged = path + '.mini-deploy-pending.' + suffix
        self.update(job, result={'backup': backup, 'candidate': staged})
        if info:
            self.update(job, phase='备份原文件')
            with client.open(backup, 'wx') as out:
                client.chmod(backup, 0o600)
                _, saved, _ = hash_remote(client, path, check, out)
                out.flush()
            if saved != previous or hash_remote(client, backup, check)[1] != previous:
                raise ssh_files.FileError(f'备份校验失败，未覆盖；备份保留：{backup}')
        self.update(job, phase='上传候选文件', progress=0)
        stream.seek(0)
        digest, done = hashlib.sha256(), 0
        with client.open(staged, 'wx') as out:
            client.chmod(staged, 0o600)
            while True:
                check()
                chunk = stream.read(CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                self.update(job, progress=done)
            out.flush()
        expected = digest.hexdigest()
        self.update(job, phase='回读校验候选文件')
        if hash_remote(client, staged, check)[1] != expected:
            raise ssh_files.FileError('候选文件校验失败，原文件和候选文件保留')
        ssh_files._guard_write(client, path)
        if info:
            current, current_hash, _ = hash_remote(client, path, check)
            if current_hash != previous or (current.st_uid, current.st_gid, current.st_mode) != (info.st_uid, info.st_gid, info.st_mode):
                raise ssh_files.FileError('提交前原文件发生变化，未替换；备份和候选文件保留')
            client.chown(staged, info.st_uid, info.st_gid)
            client.chmod(staged, stat.S_IMODE(info.st_mode))
            check()
            client.posix_rename(staged, path)
        else:
            # SFTP standard rename refuses an existing destination; no overwrite fallback.
            client.chmod(staged, 0o600)
            check()
            client.rename(staged, path)
        self.update(job, phase='校验最终文件')
        if hash_remote(client, path, check)[1] != expected:
            raise ssh_files.FileError('最终文件校验未通过，请核对文件；备份保留')
        return {'path': path, 'backup': backup, 'sha256': expected,
                'notice': '内容已回读校验；业务配置不会自动重载。'}
