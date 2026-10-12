import base64
import hashlib
import threading
import time

import pytest

import ssh_file_manager as fm
import ssh_files
from test_ssh_files import file_session as file_session, manager as manager, peer as peer


def run(session, action, **kwargs):
    return session.files.operate({'action': action, **kwargs})


def wait(session, job):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = run(session, 'transfer', job=job)
        if status['state'] not in {'receiving', 'running'}:
            return status
        time.sleep(.02)
    pytest.fail('transfer did not finish')


def upload(session, path, data, expected=''):
    job = run(session, 'upload_begin', path=path, size=len(data), expected_sha256=expected)['id']
    for offset in range(0, len(data), fm.CHUNK):
        run(session, 'upload_chunk', job=job, offset=offset, data=base64.b64encode(data[offset:offset + fm.CHUNK]).decode())
    run(session, 'upload_commit', job=job, confirm=True)
    result = wait(session, job)
    run(session, 'cancel', job=job)
    return result


def test_real_directory_home_hidden_pagination_and_mkdir(file_session):
    session, root = file_session
    (root / 'logs').mkdir()
    (root / '.env').write_text('hidden')
    for i in range(205):
        (root / f'{i:03d}.txt').write_text('a')
    first = run(session, 'list', path='.')
    assert first['path'] == '/' and len(first['entries']) == 200
    assert first['entries'][0]['name'] == 'logs' and first['total'] == 206
    assert len(run(session, 'list', path='/', offset=200)['entries']) == 6
    assert run(session, 'list', path='/', hidden=True)['total'] == 207
    assert run(session, 'mkdir', path='/new', confirm=True)['created']
    assert (root / 'new').is_dir()
    with pytest.raises(ssh_files.FileError):
        run(session, 'mkdir', path='/no-confirm')
    assert not (root / 'no-confirm').exists()


def test_real_text_preview_save_conflict_backup_and_binary_rejection(file_session):
    session, root = file_session
    (root / 'app.conf').write_text('port=8080\n')
    read = run(session, 'read', path='/app.conf')
    preview = run(session, 'preview', path='/app.conf', content='port=8081\n', expected_sha256=read['sha256'])['preview']
    assert '-port=8080' in preview['diff'] and (root / 'app.conf').read_text() == 'port=8080\n'
    saved = run(session, 'save', path='/app.conf', content='port=8081\n', expected_sha256=read['sha256'], preview=preview, confirm=True)
    assert (root / saved['backup'].lstrip('/')).read_text() == 'port=8080\n'
    assert (root / 'app.conf').read_text() == 'port=8081\n'
    with pytest.raises(ssh_files.FileError):
        run(session, 'save', path='/app.conf', content='port=8081\n', expected_sha256=read['sha256'], preview=preview, confirm=True)
    (root / 'binary').write_bytes(b'a\0b')
    with pytest.raises(ssh_files.FileError, match='二进制'):
        run(session, 'read', path='/binary')
    assert run(session, 'inspect', path='/binary')['sha256'] == hashlib.sha256(b'a\0b').hexdigest()
    (root / 'large').write_bytes(b'a' * (ssh_files.MAX_FILE + 1))
    result = run(session, 'read', path='/large')
    assert not result['editable'] and len(result['content']) == 65536
    (root / 'utf8-large').write_bytes(b'a' * 65535 + '中文'.encode() * 200000)
    assert run(session, 'read', path='/utf8-large')['content'] == 'a' * 65535
    with pytest.raises(ssh_files.FileError, match='类型'):
        session.files.operate({'action': []})


def test_real_binary_upload_download_backup_and_conflict(file_session):
    session, root = file_session
    content = bytes(range(256)) * 2200
    first = upload(session, '/archive.bin', content)
    assert first['state'] == 'succeeded' and (root / 'archive.bin').read_bytes() == content
    conflict = upload(session, '/archive.bin', b'new')
    assert conflict['state'] == 'failed' and (root / 'archive.bin').read_bytes() == content
    digest = hashlib.sha256(content).hexdigest()
    second = upload(session, '/archive.bin', b'changed\0binary', digest)
    assert second['state'] == 'succeeded'
    assert (root / second['result']['backup'].lstrip('/')).read_bytes() == content
    job = run(session, 'download_begin', path='/archive.bin')['id']
    result = wait(session, job)
    assert result['state'] == 'succeeded'
    chunk = run(session, 'download_chunk', job=job, offset=0)
    assert chunk['eof'] and base64.b64decode(chunk['data']) == b'changed\0binary'
    run(session, 'cancel', job=job)
    assert not session.files.jobs
    # File channels never consume terminal input/output.
    session.write(base64.b64encode(b'after-files').decode())
    result = session.poll(0)
    assert result['data'] and not session.closed


def test_cancel_receiving_upload_creates_no_remote_files_and_validates_chunks(file_session):
    session, root = file_session
    job = run(session, 'upload_begin', path='/new', size=4)['id']
    with pytest.raises(ssh_files.FileError, match='顺序'):
        run(session, 'upload_chunk', job=job, offset=1, data='YQ==')
    with pytest.raises(ssh_files.FileError, match='未接收'):
        run(session, 'upload_commit', job=job, confirm=True)
    run(session, 'cancel', job=job)
    assert not list(root.iterdir())
    with pytest.raises(ssh_files.FileError):
        run(session, 'upload_begin', path='/new', size=fm.MAX_TRANSFER + 1)
    for action in ('delete', 'move', 'chmod', 'shell'):
        with pytest.raises(ssh_files.FileError):
            run(session, action, path='/')


def test_upload_commit_rechecks_remote_version_and_keeps_candidates(file_session, monkeypatch):
    session, root = file_session
    (root / 'file').write_bytes(b'old')
    original = fm.hash_remote
    def race(client, path, check, output=None, progress=None):
        result = original(client, path, check, output, progress)
        if '.mini-deploy-pending.' in path:
            (root / 'file').write_bytes(b'user updated')
        return result
    monkeypatch.setattr(fm, 'hash_remote', race)
    result = upload(session, '/file', b'new', hashlib.sha256(b'old').hexdigest())
    assert result['state'] == 'failed'
    assert (root / 'file').read_bytes() == b'user updated'
    assert next(root.glob('file.mini-deploy-backup.*')).read_bytes() == b'old'
    assert next(root.glob('file.mini-deploy-pending.*')).read_bytes() == b'new'


def test_transfer_channel_cancel_and_session_close(file_session, monkeypatch):
    session, root = file_session
    entered, release = threading.Event(), threading.Event()
    original = fm.hash_remote
    def slow(client, path, check, output=None, progress=None):
        entered.set()
        release.wait(2)
        check()
        return original(client, path, check, output, progress)
    (root / 'download').write_bytes(b'content')
    monkeypatch.setattr(fm, 'hash_remote', slow)
    job = run(session, 'download_begin', path='/download')['id']
    assert entered.wait(2)
    run(session, 'cancel', job=job)
    release.set()
    deadline = time.monotonic() + 3
    while job in session.files.jobs and time.monotonic() < deadline:
        time.sleep(.02)
    assert job not in session.files.jobs
    assert (root / 'download').read_bytes() == b'content' and not session.closed
    receiving = run(session, 'upload_begin', path='/unfinished', size=0)['id']
    stream = session.files.jobs[receiving]['stream']
    session.close('done')
    assert stream.closed and not session.files.jobs
