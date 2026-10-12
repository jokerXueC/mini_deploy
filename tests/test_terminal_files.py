"""Execute bounded file reads with real Bash/coreutils and hostile file names."""
import shlex

import pytest

import terminal_tools as tools
from test_installer_operations import bash_run


@pytest.mark.parametrize('path', ['', '.', 'relative.log', '~/.log', '-file', '/tmp/log\nreboot',
                                  '/tmp/log\x00', True, '/' + 'a' * 4096])
def test_file_read_requires_absolute_path(path):
    with pytest.raises(tools.ToolError):
        tools.plan('read_file', {'path': path})


@pytest.mark.parametrize('args', [{}, {'path': '/tmp/log', 'position': 'follow'},
                                  {'path': '/tmp/log', 'position': []},
                                  {'path': '/tmp/log', 'lines': True}, {'path': '/tmp/log', 'lines': 201},
                                  {'path': '/tmp/log', 'command': 'reboot'}])
def test_file_read_rejects_invalid_parameters(args):
    with pytest.raises(tools.ToolError):
        tools.plan('read_file', args)


def linux_path(path):
    result = bash_run('if command -v cygpath >/dev/null; then cygpath -u "$FILE_TEST_PATH"; '
                      'else printf "%s" "$FILE_TEST_PATH"; fi', FILE_TEST_PATH=path.as_posix())
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.parametrize('position,expected', [('head', ['line 0', 'line 1', 'line 2']),
                                              ('tail', ['line 297', 'line 298', 'line 299'])])
def test_file_read_returns_requested_fragment_and_preserves_source(tmp_path, position, expected):
    path = tmp_path / "a b'$(touch injected);`touch injected`.log"
    content = ''.join(f'line {i}\n' for i in range(300))
    path.write_text(content, encoding='utf-8')
    plan = tools.plan('read_file', {'path': linux_path(path), 'position': position, 'lines': 3})
    result = bash_run('cd -- ' + shlex.quote(linux_path(tmp_path)) + '\n' + plan['command'])
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[1:] == expected
    assert '并非完整文件' in result.stdout
    assert path.read_text(encoding='utf-8') == content
    assert not (tmp_path / 'injected').exists()
    assert not plan['write']


@pytest.mark.parametrize('position', ['head', 'tail'])
def test_file_read_bounds_single_long_lines(tmp_path, position):
    path = tmp_path / 'large.log'
    path.write_text('x' * 100000, encoding='utf-8')
    result = bash_run(tools.plan('read_file', {'path': linux_path(path), 'position': position})['command'])
    assert result.returncode == 0, result.stderr
    assert len(result.stdout.splitlines()[1]) == 8192
    assert len(result.stdout) < 8400
    assert path.stat().st_size == 100000


@pytest.mark.parametrize('kind', ['missing', 'directory', 'unreadable'])
def test_file_read_reports_missing_nonregular_and_permission_errors(tmp_path, kind):
    path = tmp_path / 'log'
    if kind == 'directory':
        path.mkdir()
    prefix = ''
    if kind == 'unreadable':
        path.write_text('retained', encoding='utf-8')
        # Root can read chmod 000; simulate the shell permission check instead.
        prefix = '[() { if test "$1" = -r; then return 1; fi; test "$@"; }\n'
    result = bash_run(prefix + tools.plan('read_file', {'path': linux_path(path)})['command'])
    assert result.returncode == 1
    assert '文件不存在、不是普通文件或无读取权限' in result.stderr
    assert not result.stdout


def test_file_read_does_not_hide_reader_failure_in_pipeline(tmp_path):
    path = tmp_path / 'log'
    path.write_text('retained', encoding='utf-8')
    plan = tools.plan('read_file', {'path': linux_path(path)})
    result = bash_run('tail() { return 7; }\n' + plan['command'])
    assert result.returncode == 7
    assert path.read_text(encoding='utf-8') == 'retained'
