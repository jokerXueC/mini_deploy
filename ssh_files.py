"""Bounded SFTP inspection and backup-first, version-checked text updates."""
from __future__ import annotations

from collections import deque
import difflib
import errno
import fnmatch
import hashlib
import posixpath
import secrets
import stat
import time


MAX_FILE = 1024 * 1024
PAGE_BYTES = 8192


class FileError(ValueError):
    pass


def _regular(client, path):
    info = client.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise FileError("仅支持普通文件；目录、符号链接和设备文件不会读取或修改。")
    return info


def _read(client, path):
    info = _regular(client, path)
    if info.st_size > MAX_FILE:
        raise FileError("文件超过 1 MiB，请分页读取；整文件搜索或编辑需缩小范围。")
    with client.open(path, 'rb') as stream:
        content = stream.read(MAX_FILE + 1)
    if len(content) > MAX_FILE:
        raise FileError("读取期间文件增长，已停止整文件操作，请重新检查。")
    return info, content


def _text(content):
    if b'\0' in content:
        raise FileError("该文件含二进制内容，不支持文本编辑或搜索。")
    try:
        return content.decode('utf-8')
    except UnicodeDecodeError:
        raise FileError("文件不是 UTF-8 文本，未修改；请使用原编码处理。") from None


def _guard_write(client, path):
    real = client.normalize(path)
    path = posixpath.normpath(path)
    if real != path:
        raise FileError("修改路径含符号链接或已改变，未写入；请先核对真实路径。")
    if any(path == p or path.startswith(p + '/') for p in ('/proc', '/sys', '/dev', '/run')):
        raise FileError("系统虚拟文件和运行时目录不允许通过文件编辑工具修改。")
    protected = ('/etc/passwd', '/etc/shadow', '/etc/group', '/etc/gshadow', '/etc/sudoers',
                 '/etc/sudoers.d', '/etc/pam.d', '/etc/ssh', '/etc/mini-deploy-agent.env')
    if '/.ssh/' in path or path.endswith('/.ssh') or any(
            path == p or path.startswith(p + '/') for p in protected):
        raise FileError("账号、SSH、提权及面板认证配置不允许通过 AI 文件工具修改。")


def _write_exclusive(client, path, content, mode=0o600):
    # Exclusive creation prevents overwriting even when another process wins a race.
    with client.open(path, 'wx') as stream:
        client.chmod(path, 0o600)
        stream.write(content)
        stream.flush()
    client.chmod(path, mode)


def _prepare_update(client, name, args):
    path = args['path']
    _guard_write(client, path)
    info, old = None, None
    try:
        info, old = _read(client, path)
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            raise
    expected = args.get('expected_sha256', '')
    if old is not None:
        _text(old)
    if info is None:
        if name != 'write_file' or expected:
            raise FileError("原文件不存在，未编辑；创建新文件请使用 write_file 并省略版本摘要。")
        content = args['content'].encode('utf-8')
    elif not expected or hashlib.sha256(old).hexdigest() != expected:
        raise FileError("文件版本与预期不符，未修改；先 inspect_file 获取最新摘要并重新核对内容。")
    elif name == 'edit_file':
        original = _text(old)
        if original.count(args['old_text']) != 1:
            raise FileError("待替换片段必须恰好匹配一次，未修改；请提供唯一的完整上下文。")
        content = original.replace(args['old_text'], args['new_text'], 1).encode('utf-8')
    elif name == 'restore_file':
        _guard_write(client, args['backup_path'])
        _, content = _read(client, args['backup_path'])
        _text(content)
        if hashlib.sha256(content).hexdigest() != args['backup_sha256']:
            raise FileError('备份版本已改变，未恢复；请重新核对备份摘要。')
    else:
        _text(old)
        content = args['content'].encode('utf-8')
    if len(content) > MAX_FILE:
        raise FileError("修改后的文件超过 1 MiB，未写入。")
    return info, old, content


def _preview(client, plan):
    args = plan['arguments']
    info, old, content = _prepare_update(client, plan['operation'], args)
    diff = ''.join(difflib.unified_diff([line + '\n' for line in _text(old or b'').splitlines()],
                                     [line + '\n' for line in _text(content).splitlines()],
                                     fromfile=args['path'] + ' (当前)', tofile=args['path'] + ' (修改后)'))
    return {'preview': {'path': args['path'], 'created': info is None, 'changed': old != content,
                        'before_sha256': hashlib.sha256(old).hexdigest() if old is not None else '',
                        'after_sha256': hashlib.sha256(content).hexdigest(),
                        'diff': diff[:12000], 'truncated': len(diff) > 12000,
                        'notice': '只预览，不写入；确认后重新校验版本。' +
                                  (' 新文件不提供删除式回退。' if info is None else ' 原文件将保留备份。')}}


def _update(client, name, args, check, preview=None):
    path = args['path']
    info, old, content = _prepare_update(client, name, args)
    expected = args.get('expected_sha256', '')
    digest = hashlib.sha256(content).hexdigest()
    if preview and (preview['before_sha256'] != (hashlib.sha256(old).hexdigest() if old is not None else '')
                    or preview['after_sha256'] != digest):
        raise FileError('文件与已确认的预览不一致，未修改；请重新预览。')
    if info is None:
        check()
        _write_exclusive(client, path, content)
        _, saved = _read(client, path)
        valid = hashlib.sha256(saved).hexdigest() == digest
        return {'path': path, 'created': True, 'sha256': digest, 'exit_code': 0 if valid else 1,
                'verification': {'status': 'passed' if valid else 'failed', 'detail': '新文件回读摘要校验'}}
    if content == old:
        return {'path': path, 'changed': False, 'sha256': expected}
    suffix = f"{int(time.time())}.{secrets.token_hex(8)}"
    backup, staged = path + '.mini-deploy-backup.' + suffix, path + '.mini-deploy-pending.' + suffix
    check()
    try:
        _write_exclusive(client, backup, old)
        _, saved_backup = _read(client, backup)
        if saved_backup != old:
            raise FileError('备份回读内容不一致，原文件未替换。')
    except Exception as exc:
        raise FileError(f"原文件未替换；备份写入未确认成功，请核对 {backup} 和账号权限。") from exc
    try:
        check()
        _write_exclusive(client, staged, content)
        if info.st_uid is not None and info.st_gid is not None:
            client.chown(staged, info.st_uid, info.st_gid)
        client.chmod(staged, stat.S_IMODE(info.st_mode))
        _, saved_staged = _read(client, staged)
        if saved_staged != content:
            raise FileError('候选文件回读内容不一致，原文件未替换。')
        # Recheck after staging: never overwrite a file changed since inspection.
        _guard_write(client, path)
        current_info, current = _read(client, path)
        if (hashlib.sha256(current).hexdigest() != expected
                or (current_info.st_uid, current_info.st_gid, current_info.st_mode)
                != (info.st_uid, info.st_gid, info.st_mode)):
            raise FileError("准备写入时原文件发生变化，未替换。")
        check()
        client.posix_rename(staged, path)
    except Exception as exc:
        raise FileError(f"未确认文件替换成功，请核对原文件；备份 {backup}，候选文件 {staged} 已保留。"
                        "SFTP 服务须支持原子替换且账号有权限。") from exc
    recovery = {'path': path, 'backup_path': backup, 'expected_sha256': digest,
                'backup_sha256': expected}
    try:
        _, saved = _read(client, path)
        valid = hashlib.sha256(saved).hexdigest() == digest
    except Exception:
        valid = False
    return {'path': path, 'changed': True, 'backup': backup, 'sha256': digest, 'recovery': recovery,
            'exit_code': 0 if valid else 1,
            'verification': {'status': 'passed' if valid else 'failed', 'detail':
                '原文备份、候选文件与最终文件已回读校验；尚不代表业务已生效。' if valid else
                '文件替换已提交，但最终内容未确认；保留备份，请先检查当前文件。'}}


def execute(client, plan, check):
    """Client lifecycle/timeout/cancellation belong to the owning SSH session."""
    check()
    name, args = plan['name'], plan['arguments']
    path = args['path']
    if name == 'preview_file':
        return _preview(client, plan)
    if name == 'inspect_file':
        info = _regular(client, path)
        result = {'path': path, 'size': info.st_size, 'mode': oct(stat.S_IMODE(info.st_mode)),
                  'uid': info.st_uid, 'gid': info.st_gid, 'modified': info.st_mtime}
        if info.st_size <= MAX_FILE:
            _, content = _read(client, path)
            result['sha256'] = hashlib.sha256(content).hexdigest()
        else:
            result['notice'] = '文件超过 1 MiB，未计算整文件摘要；使用 read_text_file 分页查看。'
        return result
    if name == 'read_text_file':
        info = _regular(client, path)
        offset = args['offset']
        with client.open(path, 'rb') as stream:
            stream.seek(offset)
            content = stream.read(PAGE_BYTES)
        if b'\0' in content:
            raise FileError("文件片段含二进制内容，未发送给模型。")
        return {'path': path, 'size': info.st_size, 'offset': offset, 'bytes_read': len(content),
                'next_offset': offset + len(content), 'eof': offset + len(content) >= info.st_size,
                'output': content.decode('utf-8', errors='replace'),
                'notice': '按字节分页；文件变化或多字节字符跨页时可能存在边界差异。'}
    if name == 'search_text':
        _, content = _read(client, path)
        matches, count, size = [], 0, 0
        for number, line in enumerate(_text(content).splitlines(), 1):
            check()
            if args['query'] not in line:
                continue
            count += 1
            excerpt = line[:1000]
            if len(matches) < args['limit'] and size + len(excerpt.encode('utf-8')) <= PAGE_BYTES:
                matches.append({'line': number, 'text': excerpt, 'line_truncated': len(line) > 1000})
                size += len(excerpt.encode('utf-8'))
        return {'path': path, 'matches': matches, 'total_matches': count, 'truncated': len(matches) < count}
    if name == 'search_files':
        queue, found, scanned, errors, output_size, bounded = deque([(path, 0)]), [], 0, [], 0, False
        while queue and scanned < 2000 and len(found) < args['limit'] and not bounded:
            check()
            directory, depth = queue.popleft()
            try:
                if not stat.S_ISDIR(client.lstat(directory).st_mode):
                    if depth == 0:
                        raise FileError("搜索起点必须为真实目录，不跟随符号链接。")
                    continue
                for info in client.listdir_iter(directory, read_aheads=1):
                    check()
                    scanned += 1
                    filename = info.filename
                    if filename in ('.', '..') or '/' in filename or '\0' in filename:
                        continue
                    item = posixpath.join(directory, filename)
                    if stat.S_ISREG(info.st_mode) and fnmatch.fnmatchcase(filename, args['pattern']):
                        output_size += len(item.encode('utf-8'))
                        if output_size > PAGE_BYTES:
                            bounded = True
                            break
                        found.append(item)
                    elif stat.S_ISDIR(info.st_mode) and depth < args['depth']:
                        queue.append((item, depth + 1))
                    if scanned >= 2000 or len(found) >= args['limit']:
                        break
            except OSError:
                if len(errors) < 10:
                    errors.append(directory)
        return {'path': path, 'files': found, 'scanned': scanned, 'unreadable': errors,
                'truncated': bounded or bool(queue) or scanned >= 2000 or len(found) >= args['limit'],
                'notice': f"仅搜索指定目录至多 {args['depth']} 层，不跟随符号链接；不是全服务器扫描。"}
    if name in {'write_file', 'edit_file', 'restore_file'}:
        return _update(client, name, args, check, plan.get('preview'))
    raise FileError("不支持该 SFTP 操作。")
