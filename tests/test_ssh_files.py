"""Real SSH/SFTP file tools and preservation failures without OS command execution."""
import hashlib
import threading
import time

import pytest

import ssh_files
import terminal_tools as tools
import test_ssh_terminal as ssh_tests
from ssh_sftp_peer import LocalFiles, enable_sftp

manager = ssh_tests.manager
peer = ssh_tests.peer


@pytest.fixture
def file_session(manager, peer, tmp_path):
    session = ssh_tests.connect(manager, peer)
    root = tmp_path / 'remote'
    enable_sftp(peer, root)
    return session, root


def run(session, name, **args):
    return session.execute_plan(tools.plan(name, args), threading.Event())


def test_real_sftp_inspection_pagination_search_and_backup_edit(file_session):
    session, root = file_session
    path = root / 'app.conf'
    path.write_text('port=8080\n' + 'log_line\n' * 1300, encoding='utf-8')
    original = path.read_bytes()
    info = run(session, 'inspect_file', path='/app.conf')
    assert info['sha256'] == hashlib.sha256(original).hexdigest()
    first = run(session, 'read_text_file', path='/app.conf', bytes=8192)
    second = run(session, 'read_text_file', path='/app.conf', bytes=8192, offset=first['next_offset'])
    assert first['bytes_read'] == 8192 and not first['eof'] and second['eof']
    assert (first['output'] + second['output']).encode() == original
    files = run(session, 'search_files', path='/', pattern='*.conf')
    assert files['files'] == ['/app.conf']
    found = run(session, 'search_text', path='/app.conf', query='port=')
    assert found['matches'] == [{'line': 1, 'text': 'port=8080', 'line_truncated': False}]
    updated = run(session, 'edit_file', path='/app.conf', old_text='port=8080', new_text='port=8081',
                  expected_sha256=info['sha256'])
    assert path.read_bytes() == original.replace(b'8080', b'8081')
    assert (root / updated['backup'].lstrip('/')).read_bytes() == original
    assert run(session, 'inspect_file', path='/app.conf')['sha256'] == updated['sha256']
    assert not session.closed


def test_new_file_exclusive_creation_and_existing_file_requires_digest(file_session):
    session, root = file_session
    created = run(session, 'write_file', path='/new.conf', content='retained')
    assert created['created'] and (root / 'new.conf').read_text() == 'retained'
    with pytest.raises(ssh_files.FileError, match='版本'):
        run(session, 'write_file', path='/new.conf', content='overwritten')
    assert (root / 'new.conf').read_text() == 'retained'
    replaced = run(session, 'write_file', path='/new.conf', content='new', expected_sha256=created['sha256'])
    assert replaced['changed'] and (root / replaced['backup'].lstrip('/')).read_text() == 'retained'


@pytest.mark.parametrize('change', ['stale', 'ambiguous', 'missing'])
def test_edit_failures_preserve_source(file_session, change):
    session, root = file_session
    path = root / 'config'
    path.write_text('port=8080\nport=8080\n', encoding='utf-8')
    original = path.read_bytes()
    digest = run(session, 'inspect_file', path='/config')['sha256']
    if change == 'stale':
        path.write_text('user changed', encoding='utf-8')
        original = path.read_bytes()
    with pytest.raises(ssh_files.FileError):
        run(session, 'edit_file', path='/config', old_text='port=8080' if change != 'missing' else 'absent',
            new_text='port=8081', expected_sha256=digest)
    assert path.read_bytes() == original
    assert len(list(root.iterdir())) == 1


def test_large_and_binary_files_are_bounded_and_not_edited(file_session):
    session, root = file_session
    (root / 'large').write_bytes(b'x' * (ssh_files.MAX_FILE + 1))
    assert 'sha256' not in run(session, 'inspect_file', path='/large')
    assert len(run(session, 'read_text_file', path='/large')['output']) == 65536
    with pytest.raises(ssh_files.FileError, match='1 MiB'):
        run(session, 'search_text', path='/large', query='x')
    (root / 'binary').write_bytes(b'abc\0def')
    with pytest.raises(ssh_files.FileError, match='二进制'):
        run(session, 'read_text_file', path='/binary')


def test_cancellation_never_starts_sftp_operation(file_session):
    session, root = file_session
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ssh_tests.ssh.SSHError):
        session.execute_plan(tools.plan('write_file', {'path': '/untouched', 'content': 'new'}), cancel)
    assert not list(root.iterdir())
    assert not session.closed


def test_search_limits_depth_output_and_reports_truncation(file_session):
    session, root = file_session
    (root / 'nested').mkdir()
    (root / 'nested' / 'hidden.conf').write_text('value=1', encoding='utf-8')
    (root / 'first.conf').write_text('ERROR\n' * 1000, encoding='utf-8')
    assert run(session, 'search_files', path='/', pattern='*.conf', depth=0)['files'] == ['/first.conf']
    found = run(session, 'search_files', path='/', pattern='*.conf', limit=1)
    assert len(found['files']) == 1 and found['truncated']
    result = run(session, 'search_text', path='/first.conf', query='ERROR', limit=3)
    assert len(result['matches']) == 3 and result['total_matches'] == 1000 and result['truncated']


def test_failed_atomic_replacement_preserves_original_backup_and_candidate(file_session, monkeypatch):
    session, root = file_session
    path = root / 'config'
    path.write_text('original', encoding='utf-8')
    digest = run(session, 'inspect_file', path='/config')['sha256']
    def fail(*args):
        raise OSError('unsupported extension')
    monkeypatch.setattr(ssh_tests.paramiko.SFTPClient, 'posix_rename', fail)
    with pytest.raises(ssh_files.FileError, match='备份'):
        run(session, 'write_file', path='/config', content='changed', expected_sha256=digest)
    assert path.read_text() == 'original'
    assert next(root.glob('config.mini-deploy-backup.*')).read_text() == 'original'
    assert next(root.glob('config.mini-deploy-pending.*')).read_text() == 'changed'


def test_file_changed_during_staging_is_not_replaced(file_session, monkeypatch):
    session, root = file_session
    path = root / 'config'
    path.write_text('original', encoding='utf-8')
    digest = run(session, 'inspect_file', path='/config')['sha256']
    writer = ssh_files._write_exclusive
    def race(client, target, content, mode=0o600):
        writer(client, target, content, mode)
        if '.mini-deploy-pending.' in target:
            path.write_text('user updated', encoding='utf-8')
    monkeypatch.setattr(ssh_files, '_write_exclusive', race)
    with pytest.raises(ssh_files.FileError, match='备份'):
        run(session, 'write_file', path='/config', content='changed', expected_sha256=digest)
    assert path.read_text() == 'user updated'
    assert next(root.glob('config.mini-deploy-backup.*')).read_text() == 'original'


@pytest.mark.parametrize('path', ['/proc/value', '/etc/shadow', '/etc/ssh/sshd_config', '/root/.ssh/config',
                                  '/etc/sudoers.d/agent', '/etc/mini-deploy-agent.env', '/etc/../proc/value',
                                  '/etc/unused/../shadow'])
def test_credential_and_virtual_paths_cannot_be_modified(file_session, path):
    session, root = file_session
    with pytest.raises(ssh_files.FileError):
        run(session, 'write_file', path=path, content='new')
    assert not list(root.iterdir())


def test_symbols_are_not_followed_for_writes_or_recursive_search(file_session):
    session, root = file_session
    path = root / 'actual'
    path.write_text('original', encoding='utf-8')
    try:
        (root / 'link').symlink_to(path)
    except OSError:
        pytest.skip('Symlink creation requires host support')
    with pytest.raises(ssh_files.FileError, match='符号链接'):
        run(session, 'write_file', path='/link', content='new')
    with pytest.raises(ssh_files.FileError):
        run(session, 'read_text_file', path='/link')
    assert run(session, 'search_files', path='/', pattern='*')['files'] == ['/actual']
    assert path.read_text() == 'original'


def test_sftp_timeout_closes_only_tool_channel_and_preserves_terminal(file_session, monkeypatch):
    session, root = file_session
    (root / 'config').write_text('retained', encoding='utf-8')
    gate = threading.Event()
    reader = LocalFiles.lstat
    def wait(server, path):
        gate.wait(2)
        return reader(server, path)
    monkeypatch.setattr(LocalFiles, 'lstat', wait)
    start = time.monotonic()
    try:
        with pytest.raises(ssh_tests.ssh.SSHError) as error:
            session.execute_plan(tools.plan('inspect_file', {'path': '/config'}), threading.Event(), timeout=.2)
        assert error.value.status == 504 and time.monotonic() - start < 1.5
    finally:
        gate.set()
    assert not session.closed and session.transport.is_active()
    assert run(session, 'read_text_file', path='/config')['output'] == 'retained'


def test_cancel_after_staging_preserves_original_and_backup(file_session, monkeypatch):
    session, root = file_session
    path = root / 'config'
    path.write_text('original', encoding='utf-8')
    digest = run(session, 'inspect_file', path='/config')['sha256']
    writer, cancel = ssh_files._write_exclusive, threading.Event()
    def cancel_after_write(client, target, content, mode=0o600):
        writer(client, target, content, mode)
        if '.mini-deploy-pending.' in target:
            cancel.set()
    monkeypatch.setattr(ssh_files, '_write_exclusive', cancel_after_write)
    with pytest.raises((ssh_files.FileError, ssh_tests.ssh.SSHError)):
        session.execute_plan(tools.plan('write_file', {'path': '/config', 'content': 'changed',
                                                      'expected_sha256': digest}), cancel)
    assert path.read_text() == 'original'
    assert next(root.glob('config.mini-deploy-backup.*')).read_text() == 'original'
