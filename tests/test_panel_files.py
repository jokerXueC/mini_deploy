from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import stat

import pytest

import panel_code
import panel_files as files
from terminal_tools import ToolError


def check():
    pass


def test_text_pages_preserve_lines_and_multiline_key_redaction_across_pages():
    stream = io.BytesIO(b'hello\npassword=do-not-send\n-----BEGIN PRIVATE KEY-----\nprivate-body\n-----END PRIVATE KEY-----\nend\n')
    first, offset, line, private = files.text_page(stream, check, count=3)
    assert line == 4 and private and offset == stream.tell()
    assert 'do-not-send' not in str(first)
    second, offset, line, private = files.text_page(stream, check, line=line, private=private, count=3)
    assert not private and second[-1] == {'line': 6, 'text': 'end'}
    assert 'private-body' not in str(second)
    assert files.text_page(stream, check, line=line)[1] is None


def test_text_search_continues_after_empty_page_without_inventing_matches():
    stream = io.BytesIO(b'unrelated\nmatch target\n')
    rows, cursor, line, private = files.text_page(stream, check, count=1, query='target')
    assert not rows and cursor is not None
    assert files.text_page(stream, check, line=line, private=private, query='target')[0] == [
        {'line': 2, 'text': 'match target'}]


@pytest.mark.parametrize('content', [b'binary\0data', b'\xff\xfe', b'x' * (files.PAGE_BYTES + 1)],
                         ids=['binary', 'non-utf8', 'long-line'])
def test_non_text_and_oversized_lines_are_not_sent_as_truncated_evidence(content):
    with pytest.raises(ToolError):
        files.text_page(io.BytesIO(content), check)


def test_signed_cursor_is_bound_to_operation_and_rejects_tampering():
    cursor = files.token({'context': 'original', 'offset': 200})
    assert files.continuation(cursor, 'original')['offset'] == 200
    for value, context in [(cursor, 'other'), (cursor + 'x', 'original'), ('not-json', 'original')]:
        with pytest.raises(ToolError, match='游标'):
            files.continuation(value, context)


def test_path_scope_includes_hidden_files_logs_and_unusual_extensions_without_project_requirement():
    reader = files.Reader(excluded=['/var/lib/panel-private'])
    for path in ('/etc/hosts', '/root/.config/custom', '/opt/app/.env', '/var/log/app.log', '/proc/123/status'):
        assert reader.path(path) == Path(path).absolute()
    for path in ('relative.py', '/opt/../etc/hosts', '/etc/host\0s', '/var/lib/panel-private/config.json'):
        with pytest.raises(ToolError):
            reader.path(path)
    scoped = files.Reader(root='/opt/aimore')
    assert scoped.path('/opt/aimore/settings.env') == Path('/opt/aimore/settings.env').absolute()
    for path in ('/opt/other/api.py', '/opt/aimore-other/api.py', '/etc/hosts'):
        with pytest.raises(ToolError, match='所选项目'):
            scoped.path(path)


@pytest.fixture
def portable_reader(tmp_path, monkeypatch):
    reader = files.Reader()

    @contextmanager
    def opened(value, directory=False):
        # Exercise actual file paging on Windows too; Linux-only tests below cover fd traversal.
        target = tmp_path / Path(value).name
        with target.open('rb') as stream:
            yield stream.fileno(), os.fstat(stream.fileno())

    monkeypatch.setattr(reader, 'opened', opened)
    return reader


def test_large_file_has_no_whole_file_limit_and_stale_or_cross_file_cursor_is_rejected(portable_reader, tmp_path):
    path = tmp_path / 'large.log'
    content = b'first\n' + b'ordinary log line\n' * 200000
    path.write_bytes(content)
    first = portable_reader.execute('read_host_file', {'path': '/var/log/large.log', 'lines': 1}, check)
    assert first['size'] > panel_code.MAX_FILE and first['lines'] == [{'line': 1, 'text': 'first'}]
    second = portable_reader.execute('read_host_file', {
        'path': '/var/log/large.log', 'cursor': first['next_cursor'], 'lines': 1}, check)
    assert second['lines'][0]['line'] == 2 and second['next_cursor']
    with pytest.raises(ToolError, match='游标'):
        portable_reader.execute('read_host_file', {'path': '/var/log/other.log', 'cursor': first['next_cursor']}, check)
    path.write_bytes(content + b'changed\n')
    with pytest.raises(ToolError, match='文件已变化'):
        portable_reader.execute('read_host_file', {'path': '/var/log/large.log', 'cursor': first['next_cursor']}, check)


def test_directory_paging_search_paging_and_cancellation(portable_reader, monkeypatch):
    rows = [{'name': f'{index:04}.py', 'path': f'/opt/app/{index:04}.py', 'kind': 'file'} for index in range(220)]
    monkeypatch.setattr(portable_reader, 'entries', lambda *args: (rows, [1, 2, 3, 4]))
    first = portable_reader.execute('list_host_directory', {'path': '/opt/app'}, check)
    assert first['total'] == 220 and len(first['entries']) == 100
    second = portable_reader.execute('list_host_directory', {'path': '/opt/app', 'cursor': first['next_cursor']}, check)
    assert second['entries'][0]['name'] == '0100.py'
    found = []
    args = {'path': '/opt/app', 'pattern': '*.py'}
    while True:
        result = portable_reader.execute('search_host_files', args, check)
        found.extend(result['matches'])
        if not result['next_cursor']:
            break
        args['cursor'] = result['next_cursor']
    assert found == rows
    def cancelled():
        raise RuntimeError('cancelled')
    with pytest.raises(RuntimeError, match='cancelled'):
        portable_reader.execute('read_host_file', {'path': '/opt/app/a.py'}, cancelled)


@pytest.mark.skipif(not panel_code.supported(), reason='Requires Linux descriptor-safe file access')
def test_linux_reads_real_files_and_rejects_links_fifo_and_scope_escape(tmp_path):
    root = tmp_path / 'app'
    root.mkdir()
    (root / '.settings').write_text('port=8000\npassword=hidden-value\n')
    (root / 'link').symlink_to('/etc/passwd')
    os.mkfifo(root / 'pipe')
    reader = files.Reader(root=str(root))
    data = reader.execute('read_host_file', {'path': str(root / '.settings')}, check)
    assert 'hidden-value' not in json.dumps(data)
    entries = reader.execute('list_host_directory', {'path': str(root)}, check)['entries']
    assert {item['name'] for item in entries} == {'.settings', 'link', 'pipe'}
    assert next(item for item in entries if item['name'] == 'link')['target'] == '/etc/passwd'
    assert stat.S_ISREG((root / '.settings').stat().st_mode)
    for path in (root / 'link', root / 'pipe', '/etc/passwd'):
        with pytest.raises(ToolError):
            reader.execute('read_host_file', {'path': str(path)}, check)


@pytest.mark.skipif(not panel_code.supported(), reason='Requires Linux procfs and descriptor-safe file access')
def test_linux_can_pin_a_process_root_without_running_commands(tmp_path):
    path = tmp_path / 'container-file.txt'
    path.write_text('container namespace content\n')
    reader = files.Reader(namespace_pid=os.getpid(), namespace_id='test-container:started')
    result = reader.execute('read_host_file', {'path': str(path)}, check)
    assert result['lines'][0]['text'] == 'container namespace content'
