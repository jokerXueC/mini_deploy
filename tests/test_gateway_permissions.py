"""Exercise permission recovery against descriptor-based POSIX filesystem responses."""
import errno
import json
import os
import stat
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest

import certificates
import gateway_connections as connections


@pytest.fixture
def permissions(tmp_path, monkeypatch):
    target = PurePosixPath('/opt/aimore/deploy/aimore-cloud/Caddyfile')
    nodes = {str(path): SimpleNamespace(st_mode=(stat.S_IFREG | 0o644) if path == target else (stat.S_IFDIR | 0o755),
                                       st_uid=0, st_gid=0, st_nlink=1, st_dev=1, st_ino=index + 1)
             for index, path in enumerate(reversed((target, *target.parents)))}
    directories = ['/opt/aimore/deploy', '/opt/aimore/deploy/aimore-cloud']
    for path in directories:
        nodes[path].st_mode = stat.S_IFDIR | 0o775
    handles, writes = {}, []
    flags = dict(O_RDONLY=0, O_NOFOLLOW=0x20000, O_CLOEXEC=0x80000, O_NONBLOCK=0x800, O_DIRECTORY=0x10000)

    def open_fd(path, options, *, dir_fd=None):
        path = str(PurePosixPath(handles[dir_fd]) / path) if dir_fd is not None else path
        node = nodes[path]
        assert options & flags['O_NOFOLLOW'] and options & flags['O_NONBLOCK']
        if stat.S_ISLNK(node.st_mode):
            raise OSError(errno.ELOOP, 'symlink rejected')
        if options & flags['O_DIRECTORY'] and not stat.S_ISDIR(node.st_mode):
            raise OSError(errno.ENOTDIR, 'not a directory')
        fd = len(handles) + 10
        handles[fd] = path
        return fd

    def fchmod(fd, mode):
        path = handles[fd]
        writes.append((path, mode))
        nodes[path].st_mode = stat.S_IFMT(nodes[path].st_mode) | mode

    def check(path):
        for item in reversed((path, *path.parents)):
            info = nodes[str(item)]
            if info.st_mode & 0o022:
                raise certificates.PathPermissionError(path, item, info, 'writable')

    fake_os = SimpleNamespace(name='posix', geteuid=lambda: 0, open=open_fd,
                              fstat=lambda fd: SimpleNamespace(**vars(nodes[handles[fd]])),
                              fchmod=fchmod, close=lambda fd: handles.pop(fd), **flags)
    monkeypatch.setattr(connections, 'os', fake_os)
    monkeypatch.setattr(connections, 'trusted_path', check)
    store = connections.Connections(tmp_path / 'data')
    monkeypatch.setattr(store, 'directory', lambda key: tmp_path)
    error = certificates.PathPermissionError(target, PurePosixPath(directories[0]), nodes[directories[0]], 'writable')
    return SimpleNamespace(store=store, target=target, nodes=nodes, directories=directories,
                           handles=handles, writes=writes, os=fake_os, error=error, root=tmp_path)


def test_repair_only_removes_group_write_from_root_owned_ancestors(permissions):
    p = permissions
    p.store.repair_config_permissions({'spec': {'key': 'api'}}, p.error)
    assert p.writes == [(path, 0o755) for path in p.directories]
    assert p.nodes[str(p.target)].st_mode == stat.S_IFREG | 0o644
    assert not p.handles
    audit = json.loads(next(p.root.glob('permissions-before-repair-*.json')).read_text(encoding='utf-8'))
    assert audit['state'] == 'completed' and audit['target'] == str(p.target)
    assert audit['directories'] == p.store.permission_repairs
    assert all(item['before'] == '0775' and item['after'] == '0755' for item in audit['directories'])


@pytest.mark.parametrize('attribute,value', [
    ('st_uid', 1000), ('st_gid', 1000), ('st_mode', stat.S_IFDIR | 0o777),
    ('st_mode', stat.S_IFLNK | 0o777),
])
def test_untrusted_descendant_prevents_all_permission_changes(permissions, attribute, value):
    p = permissions
    setattr(p.nodes[p.directories[-1]], attribute, value)
    with pytest.raises(certificates.CertificateError):
        p.store.repair_config_permissions({'spec': {'key': 'api'}}, p.error)
    assert not p.writes and not p.handles
    assert not list(p.root.glob('permissions-before-repair-*.json'))


@pytest.mark.parametrize('attribute,value', [
    ('st_uid', 1000), ('st_nlink', 2), ('st_mode', stat.S_IFREG | 0o664),
    ('st_mode', stat.S_IFIFO | 0o644),
])
def test_unsafe_config_file_prevents_ancestor_changes(permissions, attribute, value):
    p = permissions
    setattr(p.nodes[str(p.target)], attribute, value)
    with pytest.raises(certificates.CertificateError):
        p.store.repair_config_permissions({'spec': {'key': 'api'}}, p.error)
    assert not p.writes and not p.handles


def test_system_directory_is_not_hardened_implicitly(permissions):
    p = permissions
    p.nodes['/opt'].st_mode |= 0o020
    with pytest.raises(certificates.CertificateError):
        p.store.repair_config_permissions({'spec': {'key': 'api'}}, p.error)
    assert not p.writes and not p.handles


def test_non_root_agent_cannot_repair_permissions(permissions):
    p = permissions
    p.os.geteuid = lambda: 1000
    with pytest.raises(certificates.CertificateError):
        p.store.repair_config_permissions({'spec': {'key': 'api'}}, p.error)
    assert not p.writes and not p.handles


def test_partial_permission_failure_rolls_back_only_our_changes(permissions):
    p = permissions
    original = p.os.fchmod

    def fail_second(fd, mode):
        if p.handles[fd] == p.directories[1]:
            raise PermissionError('denied')
        original(fd, mode)

    p.os.fchmod = fail_second
    with pytest.raises(certificates.CertificateError, match='无法自动修复'):
        p.store.repair_config_permissions({'spec': {'key': 'api'}}, p.error)
    assert all(stat.S_IMODE(p.nodes[path].st_mode) == 0o775 for path in p.directories)
    assert not p.handles and not p.store.permission_repairs
    audit = json.loads(next(p.root.glob('permissions-before-repair-*.json')).read_text(encoding='utf-8'))
    assert audit['state'] == 'rolled_back'


def test_failure_to_persist_audit_prevents_permission_changes(permissions, monkeypatch):
    def fail(*_):
        raise OSError('disk full')

    monkeypatch.setattr(connections, 'atomic_write', fail)
    with pytest.raises(certificates.CertificateError, match='无法自动修复'):
        permissions.store.repair_config_permissions({'spec': {'key': 'api'}}, permissions.error)
    assert not permissions.writes and not permissions.handles


def test_concurrent_permission_change_is_preserved(permissions):
    p = permissions
    fstat, reads = p.os.fstat, {}

    def changed(fd):
        path = p.handles[fd]
        reads[path] = reads.get(path, 0) + 1
        if path == p.directories[1] and reads[path] == 2:
            p.nodes[path].st_mode = stat.S_IFDIR | 0o700
        return fstat(fd)

    p.os.fstat = changed
    with pytest.raises(certificates.CertificateError, match='修复前发生变化'):
        p.store.repair_config_permissions({'spec': {'key': 'api'}}, p.error)
    assert stat.S_IMODE(p.nodes[p.directories[0]].st_mode) == 0o775
    assert stat.S_IMODE(p.nodes[p.directories[1]].st_mode) == 0o700
    assert not p.handles


@pytest.mark.skipif(os.name != 'posix' or getattr(os, 'geteuid', lambda: -1)() != 0,
                    reason='Native POSIX root permission recovery')
def test_native_root_directory_permission_repair(tmp_path):
    parent = tmp_path / 'business' / 'deploy'
    directory = parent / 'cloud'
    directory.mkdir(parents=True)
    target = directory / 'Caddyfile'
    target.write_text('example.test {\n    respond 200\n}\n')
    before = target.read_bytes()
    parent.chmod(0o775)
    directory.chmod(0o775)
    store = connections.Connections(tmp_path / 'data')
    store.directory('api').mkdir(parents=True)
    with pytest.raises(certificates.PathPermissionError) as result:
        certificates.trusted_path(target)
    store.repair_config_permissions({'spec': {'key': 'api'}}, result.value)
    certificates.trusted_path(target)
    assert stat.S_IMODE(parent.stat().st_mode) == stat.S_IMODE(directory.stat().st_mode) == 0o755
    assert target.read_bytes() == before


@pytest.mark.parametrize('reason,attribute,value', [('writable', 'st_mode', stat.S_IFDIR | 0o775), ('owner', 'st_uid', 1000)])
def test_path_diagnostic_identifies_the_unsafe_ancestor(permissions, monkeypatch, reason, attribute, value):
    p = permissions

    class CheckedPath(PurePosixPath):
        def is_symlink(self):
            return stat.S_ISLNK(self.stat().st_mode)

        def exists(self):
            return str(self) in p.nodes

        def stat(self):
            return p.nodes[str(self)]

    monkeypatch.setattr(certificates, 'os', SimpleNamespace(name='posix', geteuid=lambda: 0))
    for path in p.directories:
        p.nodes[path].st_mode &= ~0o020
    setattr(p.nodes[p.directories[-1]], attribute, value)
    with pytest.raises(certificates.PathPermissionError) as result:
        certificates.trusted_path(CheckedPath(p.target))
    error = result.value
    assert error.reason == reason and error.path == PurePosixPath(p.directories[-1])
    assert error.target == p.target and 'UID' in str(error) and 'GID' in str(error)
    assert str(error.path) in str(error) and 'Nginx' not in str(error)
