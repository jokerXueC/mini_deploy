"""Project discovery and confined, read-only source inspection on the panel host."""
from __future__ import annotations

from contextlib import contextmanager
import fnmatch
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import threading
import time


class CodeError(ValueError):
    pass


BASES = ('/opt', '/srv', '/var/www', '/home', '/root')
MARKERS = ('.git', 'pyproject.toml', 'package.json', 'go.mod', 'Cargo.toml', 'pom.xml',
           'composer.json', 'requirements.txt', 'Dockerfile', 'compose.yml', 'compose.yaml',
           'docker-compose.yml', 'docker-compose.yaml')
SKIP = {'.git', '.svn', '.hg', '.ssh', '.aws', '.azure', '.kube', '.gnupg', '.config',
        'node_modules', '__pycache__', '.venv', 'venv', 'vendor', '.next', '.cache',
        'dist', 'build', 'coverage', 'logs', 'backups', 'secrets', 'credentials'}
SUFFIXES = {'.py', '.js', '.jsx', '.ts', '.tsx', '.vue', '.svelte', '.go', '.rs', '.java',
            '.kt', '.kts', '.c', '.h', '.cc', '.cpp', '.hpp', '.cs', '.fs', '.php', '.rb',
            '.ex', '.exs', '.erl', '.scala', '.swift', '.dart', '.sh', '.bash', '.sql',
            '.html', '.css', '.scss', '.less', '.json', '.toml', '.yaml', '.yml', '.xml',
            '.ini', '.conf', '.cfg', '.properties', '.md', '.txt', '.graphql', '.proto'}
NAMES = {'dockerfile', 'containerfile', 'makefile', 'caddyfile', 'gemfile', 'procfile'}
MAX_FILE = 2 * 1024 * 1024
MAX_ENTRIES = 10000
SEARCH_BYTES = 16 * 1024 * 1024


def supported():
    return (os.open in os.supports_dir_fd and os.scandir in os.supports_fd
            and all(hasattr(os, name) for name in ('O_NOFOLLOW', 'O_DIRECTORY', 'O_CLOEXEC', 'O_NONBLOCK')))


def hidden(name):
    low = name.lower()
    return (low in SKIP or low.startswith('.') or
            re.search(r'(^|[._-])(secrets?|credentials?|passwords?|tokens?|id_rsa|id_ed25519)([._-]|$)', low) or
            low.endswith(('.pem', '.key', '.p12', '.pfx', '.db', '.sqlite', '.sqlite3', '.bak', '.log', '.lock')) or
            '.env' in low or '.mini-deploy-' in low)


def source_file(name):
    return not hidden(name) and (Path(name).suffix.lower() in SUFFIXES or name.lower() in NAMES)


def real_directory(path):
    path = Path(path)
    if not path.is_absolute():
        raise CodeError('代码目录必须是绝对路径')
    for part in [*reversed(path.parents), path]:
        info = part.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise CodeError('代码目录包含链接或不是普通目录')
    return path


def _project_identity(path, info):
    return hashlib.sha256(f'{path}:{info.st_dev}:{info.st_ino}'.encode()).hexdigest()[:32]


class Catalog:
    def __init__(self, bases=BASES, excluded=(), docker=lambda: []):
        self.bases = tuple(Path(p) for p in bases)
        self.excluded = tuple(Path(p).absolute() for p in excluded)
        self.docker = docker
        self.lock, self.checked, self.projects = threading.Lock(), 0, []

    def snapshot(self, refresh=False):
        with self.lock:
            if self.checked and time.monotonic() - self.checked < (3 if refresh else 30):
                return self.projects
            found, containers, scanned = {}, [], 0

            def add(path, container=''):
                nonlocal scanned
                scanned += 1
                if scanned > 600 or len(found) >= 100:
                    return
                try:
                    path = Path(path)
                    if not any(path != base and base in path.parents for base in self.bases):
                        return
                    if any(path == p or p in path.parents for p in self.excluded):
                        return
                    relative = next(path.relative_to(base) for base in self.bases if base in path.parents)
                    if any(hidden(p) for p in relative.parts):
                        return
                    real_directory(path)
                    # A compose deployment nested in a repository belongs to that repository.
                    for parent in [path, *path.parents]:
                        if parent in self.bases:
                            break
                        if parent.joinpath('.git').exists() and not parent.joinpath('.git').is_symlink():
                            path = real_directory(parent)
                            break
                    if any(path == p or p in path.parents for p in self.excluded):
                        return
                    if not any(path.joinpath(marker).exists() for marker in MARKERS):
                        return
                    info = path.stat()
                    identity = _project_identity(path, info)
                    item = found.setdefault(str(path), {'id': identity, 'name': path.name, 'path': str(path),
                                                       'containers': []})
                    if container and container not in item['containers']:
                        item['containers'].append(container)
                except (OSError, ValueError):
                    return

            try:
                containers = self.docker()
            except (OSError, ValueError, RuntimeError):
                pass
            for item in containers[:256]:
                identity = item.get('id', '')
                if not isinstance(identity, str) or not re.fullmatch('[a-f0-9]{64}', identity):
                    continue
                for path in [item.get('working_dir'), *(item.get('binds') or [])]:
                    if isinstance(path, str) and path.startswith('/'):
                        add(path, identity)
            for base in self.bases:
                try:
                    real_directory(base)
                    with os.scandir(base) as entries:
                        for index, entry in enumerate(entries):
                            if scanned >= 600 or index >= 600:
                                break
                            if not hidden(entry.name) and entry.is_dir(follow_symlinks=False):
                                add(entry.path)
                                # /home/<user>/<repo> is a common checkout layout.
                                if base.name == 'home':
                                    with os.scandir(entry.path) as children:
                                        for index, child in enumerate(children):
                                            if scanned >= 600 or index >= 600:
                                                break
                                            if not hidden(child.name) and child.is_dir(follow_symlinks=False):
                                                add(child.path)
                except (OSError, CodeError):
                    continue
            self.projects = sorted(found.values(), key=lambda p: p['path'])
            self.checked = time.monotonic()
            return self.projects

    def project(self, identity):
        if not isinstance(identity, str) or not re.fullmatch('[a-f0-9]{32}', identity):
            raise CodeError('请选择已发现的代码项目')
        item = next((p for p in self.snapshot() if p['id'] == identity), None)
        if item is None:
            raise CodeError('代码项目已变化，请重新扫描并选择')
        try:
            path = real_directory(item['path'])
            info = path.stat()
        except OSError as exc:
            raise CodeError('代码目录已不可用，请重新扫描') from exc
        if _project_identity(path, info) != identity:
            raise CodeError('代码项目目录已被替换，请重新扫描')
        return dict(item)


class Reader:
    def __init__(self, project, *, excluded=()):
        if not supported():
            raise CodeError('本机源码读取需要 Linux 的目录隔离能力')
        self.project = project
        self.root = real_directory(project['path'])
        info = self.root.stat()
        if _project_identity(self.root, info) != project['id']:
            raise CodeError('代码项目目录已被替换，请重新扫描')
        self.identity = (info.st_dev, info.st_ino)
        self.excluded = tuple(Path(p).absolute() for p in excluded)

    def _excluded(self, path):
        return any(path == p or p in path.parents for p in self.excluded)

    @contextmanager
    def _open(self, relative='.', directory=False):
        if not isinstance(relative, str) or len(relative) > 2048 or '\\' in relative or any(ord(c) < 32 for c in relative):
            raise CodeError('项目相对路径无效')
        parts = PurePosixPath(relative).parts
        if relative.startswith('/') or '..' in parts or any(hidden(p) for p in parts):
            raise CodeError('只允许读取所选项目内的源码，凭据和隐藏目录不可访问')
        if self._excluded(self.root.joinpath(*parts)):
            raise CodeError('面板程序和私有数据目录不属于可读取的业务源码')
        descriptors = []
        try:
            # Resolve every component through directory descriptors, never a symlink.
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
            fd = os.open(self.root.anchor, flags | os.O_DIRECTORY)
            descriptors.append(fd)
            for part in self.root.parts[1:]:
                fd = os.open(part, flags | os.O_DIRECTORY, dir_fd=fd)
                descriptors.append(fd)
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != self.identity:
                raise CodeError('代码目录已变化，请重新选择项目')
            for index, part in enumerate(parts):
                is_dir = directory or index < len(parts) - 1
                fd = os.open(part, flags | (os.O_DIRECTORY if is_dir else os.O_NONBLOCK), dir_fd=fd)
                descriptors.append(fd)
            info = os.fstat(fd)
            if directory:
                if not stat.S_ISDIR(info.st_mode):
                    raise CodeError('不是普通目录')
            elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise CodeError('仅可读取普通源码文件，不读取硬链接或特殊文件')
            yield fd, info
        except OSError as exc:
            raise CodeError('源码无法读取：路径不存在、权限不足或包含链接；请重新浏览项目目录') from exc
        finally:
            for fd in reversed(descriptors):
                os.close(fd)

    def entries(self, path, check):
        with self._open(path, directory=True) as (fd, _):
            result = []
            with os.scandir(fd) as entries:
                for index, item in enumerate(entries):
                    check()
                    if index >= MAX_ENTRIES:
                        raise CodeError('目录条目过多，请直接指定更具体的子目录')
                    if hidden(item.name) or item.is_symlink() or self._excluded(self.root / path / item.name):
                        continue
                    info = item.stat(follow_symlinks=False)
                    kind = 'directory' if stat.S_ISDIR(info.st_mode) else 'file'
                    if kind == 'file' and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not source_file(item.name)):
                        continue
                    result.append({'path': str(PurePosixPath(path) / item.name), 'kind': kind, 'size': info.st_size})
            return sorted(result, key=lambda e: (e['kind'] != 'directory', e['path']))

    def _content(self, path):
        if not source_file(PurePosixPath(path).name):
            raise CodeError('只读取源码与文本配置，不读取凭据、数据库、日志或二进制文件')
        with self._open(path) as (fd, before):
            if before.st_size > MAX_FILE:
                raise CodeError('源码文件超过 2 MiB，请检查是否为构建产物或选择拆分后的源文件')
            with os.fdopen(os.dup(fd), 'rb') as stream:
                content = stream.read(MAX_FILE + 1)
            after = os.fstat(fd)
            if (len(content) > MAX_FILE or after.st_nlink != 1
                    or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
                raise CodeError('读取期间源码发生变化，请重新读取')
        if b'\0' in content:
            raise CodeError('文件含二进制内容')
        try:
            text = content.decode('utf-8-sig')
        except UnicodeError:
            raise CodeError('源码不是 UTF-8 文本') from None
        # Redact the whole file before paging, preserving original line numbers.
        def mask(match):
            return '[已隐藏私钥]' + '\n' * match[0].count('\n')
        text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|\Z)', mask, text, flags=re.S)
        import terminal_ai
        lines = [line[:len(line) - len(line.lstrip())] + terminal_ai.redact(line) for line in text.splitlines()]
        return lines, hashlib.sha256(content).hexdigest(), len(content)

    def read(self, path, start, count, check):
        check()
        lines, digest, _ = self._content(path)
        chosen, size = [], 0
        for number in range(start, min(len(lines) + 1, start + count)):
            check()
            original = lines[number - 1]
            line = original[:12000]
            if size + len(line.encode()) > 48000:
                break
            chosen.append({'line': number, 'text': line, 'line_truncated': len(line) < len(original)})
            size += len(line.encode())
        following = start + len(chosen)
        return {'path': str(self.root / path), 'sha256': digest, 'lines': chosen, 'total_lines': len(lines),
                'next_line': following if following <= len(lines) else None,
                'notice': '当前宿主机文件，可能不同于故障时或镜像内运行的版本；常见凭据已脱敏。'}

    def search(self, path, query, pattern, offset, limit, check):
        queue, files, visited, inaccessible = [path], [], 0, 0
        while queue:
            check()
            directory = queue.pop()
            try:
                entries = self.entries(directory, check)
            except CodeError:
                if directory == path:
                    raise
                inaccessible += 1
                continue
            for item in entries:
                visited += 1
                if visited > MAX_ENTRIES:
                    raise CodeError('项目较大，请缩小到子目录搜索')
                if item['kind'] == 'directory':
                    queue.append(item['path'])
                elif fnmatch.fnmatchcase(PurePosixPath(item['path']).name, pattern):
                    files.append(item)
        files.sort(key=lambda e: e['path'])
        matches, size, skipped, index = [], 0, 0, offset
        for index in range(offset, len(files)):
            check()
            item = files[index]
            if item['size'] > MAX_FILE:
                skipped += 1
                continue
            if size + item['size'] > SEARCH_BYTES:
                break
            size += item['size']
            try:
                lines, digest, _ = self._content(item['path'])
            except CodeError:
                skipped += 1
                continue
            hits = []
            for number, line in enumerate(lines, 1):
                if query in line:
                    hits.append({'line': number, 'text': line[:800]})
                    if len(hits) > 10:
                        break
            if hits:
                matches.append({'path': item['path'], 'sha256': digest, 'matches': hits[:10],
                                'more_matches': len(hits) > 10})
            if len(matches) >= limit:
                index += 1
                break
        else:
            index = len(files)
        return {'matches': matches, 'skipped_files': skipped, 'skipped_directories': inaccessible,
                'next_offset': index if index < len(files) else None,
                'files': len(files), 'notice': '字面文本搜索；有分页或跳过文件时不能据此判断项目中不存在相关逻辑。'}


PATH = {'type': 'string', 'description': '所选项目内的相对路径；目录默认为 .，不可使用绝对路径或 ..'}
OFFSET = {'type': 'integer', 'minimum': 0, 'maximum': MAX_ENTRIES}
DEFINITIONS = [
    ('list_code_directory', '浏览所选项目的源码目录，返回可读取的文件和子目录；无需 SSH',
     {'path': PATH, 'offset': OFFSET}, []),
    ('search_code', '在所选项目目录内搜索字面文本，例如接口路径或函数名；按文件分页，返回文件路径和行号',
     {'path': PATH, 'query': {'type': 'string', 'maxLength': 200},
      'pattern': {'type': 'string', 'description': '文件名通配符，默认 *，如 *.py'}, 'offset': OFFSET}, ['query']),
    ('read_code_file', '按原始行号读取所选项目内的源码；用 next_line 继续读取，返回文件摘要供版本核对',
     {'path': PATH, 'start_line': {'type': 'integer', 'minimum': 1, 'maximum': 1000000},
      'lines': {'type': 'integer', 'minimum': 1, 'maximum': 300}}, ['path']),
]


def arguments(name, args):
    definitions = {d[0]: d for d in DEFINITIONS}
    if name not in definitions or not isinstance(args, dict):
        raise CodeError('源码工具无效')
    _, _, props, required = definitions[name]
    if set(args) - props.keys() or not set(required) <= args.keys():
        raise CodeError('源码工具参数无效')
    path = args.get('path', '.')
    if (not isinstance(path, str) or len(path) > 2048 or path.startswith('/') or '\\' in path
            or '..' in PurePosixPath(path).parts or any(ord(c) < 32 for c in path)
            or any(hidden(p) for p in PurePosixPath(path).parts)):
        raise CodeError('只允许所选项目的相对路径，不能越界或访问凭据目录')
    result = {**args, 'path': path}
    for key, default, lo, hi in [('offset', 0, 0, MAX_ENTRIES), ('start_line', 1, 1, 1000000), ('lines', 120, 1, 300)]:
        if key in props:
            value = args.get(key, default)
            if type(value) is not int or not lo <= value <= hi:
                raise CodeError('源码分页参数无效')
            result[key] = value
    if name == 'search_code':
        query, pattern = args['query'], args.get('pattern', '*')
        if not isinstance(query, str) or not 1 <= len(query) <= 200 or '\0' in query:
            raise CodeError('请输入 1 至 200 字的搜索文本')
        if not isinstance(pattern, str) or not 1 <= len(pattern) <= 100 or '/' in pattern or '\\' in pattern:
            raise CodeError('文件名通配符无效')
        result['pattern'] = pattern
    return result


def execute(reader, name, args, check):
    args = arguments(name, args)
    if reader is None:
        raise CodeError('请在当前对话上方选择代码项目后发送，无需连接 SSH')
    if name == 'read_code_file':
        return reader.read(args['path'], args['start_line'], args['lines'], check)
    if name == 'search_code':
        return reader.search(args['path'], args['query'], args['pattern'], args['offset'], 20, check)
    entries = reader.entries(args['path'], check)
    start = args['offset']
    return {'path': args['path'], 'entries': entries[start:start + 100],
            'next_offset': start + 100 if start + 100 < len(entries) else None}
