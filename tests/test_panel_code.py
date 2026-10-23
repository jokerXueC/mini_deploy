import os
import threading

import pytest

import panel_code as code


def test_discovery_collapses_compose_directory_to_repository_and_excludes_panel(tmp_path):
    root = tmp_path / 'opt'
    app = root / 'aimore'
    deploy = app / 'deploy' / 'cloud'
    deploy.mkdir(parents=True)
    (app / '.git').mkdir()
    (deploy / 'compose.yaml').write_text('services: {}')
    panel = root / 'mini_deploy'
    panel.mkdir()
    (panel / 'pyproject.toml').write_text('')
    data = root / 'database-data'
    data.mkdir()
    (data / 'db.sqlite3').write_text('private')
    calls = []

    def docker():
        calls.append(True)
        return [{'id': 'a' * 64, 'working_dir': str(deploy), 'binds': [str(app), '/etc']}]

    catalog = code.Catalog(bases=[root], excluded=[panel], docker=docker)
    projects = catalog.snapshot()
    assert len(projects) == 1 and projects[0]['path'] == str(app)
    # Docker uses Linux absolute paths; host-only discovery also works in this unit test on Windows.
    if os.name != 'nt':
        assert projects[0]['containers'] == ['a' * 64]
    assert catalog.project(projects[0]['id']) == projects[0]
    assert catalog.snapshot() == projects and len(calls) == 1
    with pytest.raises(code.CodeError):
        catalog.project('/etc')
    moved = root / 'old-aimore'
    app.rename(moved)
    app.mkdir()
    with pytest.raises(code.CodeError, match='替换'):
        catalog.project(projects[0]['id'])


@pytest.mark.parametrize('path', ['/etc/passwd', '../other/app.py', 'src/../../app.py', '.git/config',
                                 '.env', 'settings.env', '.ssh/config', 'src\\app.py', 'src/\0.py'])
def test_code_paths_cannot_escape_or_read_credentials(path):
    with pytest.raises(code.CodeError):
        code.arguments('read_code_file', {'path': path})


@pytest.mark.parametrize('name,args', [
    ('read_code_file', {'path': 'app.py', 'lines': True}),
    ('read_code_file', {'path': 'app.py', 'start_line': 0}),
    ('search_code', {'query': ''}), ('search_code', {'query': 'claim', 'pattern': '../*'}),
    ('search_code', {'query': 'claim', 'command': 'cat /etc/shadow'}),
    ('list_code_directory', {'path': '.', 'offset': -1}),
    ('delete_file', {'path': 'app.py'}),
])
def test_code_arguments_reject_commands_writes_and_invalid_paging(name, args):
    with pytest.raises(code.CodeError):
        code.arguments(name, args)


@pytest.fixture
def reader(tmp_path):
    if not code.supported():
        pytest.skip('Linux descriptor-relative filesystem operations required')
    app = tmp_path / 'aimore'
    app.mkdir()
    (app / '.git').mkdir()
    (app / 'api.py').write_text('def claim():\n    return "waiting"\npassword = "secret-value"\n', encoding='utf-8')
    return code.Reader(code.Catalog(bases=[tmp_path]).snapshot()[0])


def test_read_and_search_real_sources_preserve_line_numbers_and_redact(reader):
    result = code.execute(reader, 'read_code_file', {'path': 'api.py', 'lines': 2}, lambda: None)
    assert result['lines'][1]['text'] == '    return "waiting"'
    assert result['lines'][1]['line'] == 2 and result['next_line'] == 3
    secret = code.execute(reader, 'read_code_file', {'path': 'api.py', 'start_line': 3}, lambda: None)
    assert 'secret-value' not in str(secret) and '[已隐藏]' in str(secret)
    search = code.execute(reader, 'search_code', {'query': 'claim'}, lambda: None)
    assert search['matches'][0]['path'] == 'api.py' and search['matches'][0]['matches'][0]['line'] == 1
    assert search['matches'][0]['sha256'] == result['sha256']
    assert (reader.root / 'api.py').read_text() == 'def claim():\n    return "waiting"\npassword = "secret-value"\n'


def test_links_special_files_and_credentials_are_not_read(reader, tmp_path):
    outside = tmp_path / 'other-project'
    outside.mkdir()
    (outside / 'private.py').write_text('outside-secret')
    (reader.root / 'linked').symlink_to(outside, target_is_directory=True)
    (reader.root / 'linked.py').symlink_to(outside / 'private.py')
    (reader.root / 'hard.py').hardlink_to(outside / 'private.py')
    (reader.root / '.env').write_text('password=private')
    os.mkfifo(reader.root / 'pipe.py')
    entries = reader.entries('.', lambda: None)
    assert [e['path'] for e in entries] == ['api.py']
    for path in ('linked/private.py', 'linked.py', 'hard.py', 'pipe.py', '.env'):
        with pytest.raises(code.CodeError):
            reader.read(path, 1, 100, lambda: None)
    assert 'outside-secret' not in str(reader.search('.', 'secret', '*', 0, 20, lambda: None))


def test_parent_swap_is_rejected_at_open_time(reader, tmp_path, monkeypatch):
    folder = reader.root / 'src'
    folder.mkdir()
    (folder / 'app.py').write_text('safe')
    other = tmp_path / 'other'
    other.mkdir()
    (other / 'app.py').write_text('outside')
    original = os.open

    def swapped(path, flags, *args, **kwargs):
        if path == 'src' and kwargs.get('dir_fd') is not None and not folder.is_symlink():
            folder.rename(reader.root / 'saved-src')
            folder.symlink_to(other, target_is_directory=True)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, 'open', swapped)
    with pytest.raises(code.CodeError):
        reader.read('src/app.py', 1, 50, lambda: None)


def test_replaced_project_is_rejected_before_creating_reader(reader):
    reader.root.rename(reader.root.with_name('previous'))
    reader.root.mkdir()
    with pytest.raises(code.CodeError, match='替换'):
        code.Reader(reader.project)


def test_panel_private_directories_inside_project_are_excluded(reader):
    private = reader.root / 'panel-data'
    private.mkdir()
    (private / 'config.json').write_text('{"value": "private-state"}')
    guarded = code.Reader(reader.project, excluded=[private])
    assert [e['path'] for e in guarded.entries('.', lambda: None)] == ['api.py']
    assert not guarded.search('.', 'private-state', '*', 0, 20, lambda: None)['matches']
    with pytest.raises(code.CodeError, match='私有数据'):
        guarded.read('panel-data/config.json', 1, 100, lambda: None)


def test_search_paging_large_files_and_private_key_pages_make_progress(reader, monkeypatch):
    for i in range(30):
        (reader.root / f'worker-{i:02}.py').write_text('claim = True\n')
    first = reader.search('.', 'claim', '*.py', 0, 20, lambda: None)
    second = reader.search('.', 'claim', '*.py', first['next_offset'], 20, lambda: None)
    assert len(first['matches']) + len(second['matches']) == 31 and second['next_offset'] is None
    # Oversized files must not leave the cursor pointing at the same unsearchable file.
    (reader.root / '000-large.py').write_text('x' * (code.MAX_FILE + 1))
    assert reader.search('.', 'claim', '*', 0, 20, lambda: None)['skipped_files'] == 1
    (reader.root / 'key-test.py').write_text('first\n-----BEGIN RSA PRIVATE KEY-----\nprivate-body\n-----END RSA PRIVATE KEY-----\nlast\n')
    result = reader.read('key-test.py', 3, 3, lambda: None)
    assert 'private-body' not in str(result) and result['lines'][-1] == {'line': 5, 'text': 'last', 'line_truncated': False}
    (reader.root / 'long.py').write_text('x' * 60000)
    result = reader.read('long.py', 1, 10, lambda: None)
    assert result['lines'][0]['line_truncated'] and result['next_line'] is None


def test_cancellation_stops_before_reading_file(reader):
    stop = threading.Event()
    stop.set()

    def check():
        if stop.is_set():
            raise RuntimeError('cancelled')

    with pytest.raises(RuntimeError, match='cancelled'):
        reader.read('api.py', 1, 10, check)
