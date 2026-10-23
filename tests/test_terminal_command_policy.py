import shlex

import pytest

from terminal_command_policy import assess
from terminal_tools import ToolError, plan


@pytest.mark.parametrize('command', [
    'pwd', 'df -h && free -m', 'ps aux | head -n 20', "ls -lah '/opt/my app'",
    'docker ps -a --no-trunc', 'docker stats --no-stream', 'docker container inspect web',
    'docker exec web cat /etc/os-release', 'systemctl status nginx.service',
    'journalctl -u mini-deploy-agent -n 100 --no-pager', 'ip route show',
    "docker exec db psql -U app -d app -c 'SELECT COUNT(*) AS user_count FROM app.users;'",
    "psql -c 'SELECT id, name FROM users LIMIT 20 OFFSET 10'",
])
def test_common_reads_are_automatic(command):
    result = assess(command)
    assert result['action'] == 'auto' and result['command']


@pytest.mark.parametrize('command', [
    'rm -rf /opt/app', '/bin/rm /tmp/a', 'rmdir /tmp/a', 'unlink /tmp/a', 'truncate -s 0 /tmp/a',
    'mkfs.ext4 /dev/sda', 'dd if=/dev/zero of=/dev/sda', 'sudo rm -f /tmp/a',
    'docker rm web', 'docker system prune -a', 'docker volume rm data', 'docker compose down -v',
    "docker exec db psql -c 'DELETE FROM users'", "psql -c 'DROP TABLE users'",
    "sh -c 'rm /tmp/a'", "bash -lc 'pwd; rm /tmp/a'", 'find /tmp -type f -delete',
    "python3 -c 'import os; os.remove(\"/tmp/a\")'", 'apt-get purge nginx',
    'pwd\nrm /tmp/a', 'pwd && rm /tmp/a',
    'sudo -u root rm /tmp/a', 'docker exec --user root web rm /tmp/a',
    "docker exec -it web sh -c 'rm /tmp/a'", ': > /tmp/file', "printf '' > /tmp/file",
])
def test_explicit_destruction_is_blocked_in_every_mode(command):
    assert assess(command)['action'] == 'block'
    with pytest.raises(ToolError, match='禁止'):
        plan('execute_command', {'command': command, 'reason': '测试拒绝'}, commands=True)


@pytest.mark.parametrize('command', [
    "python3 -c 'print(1)'", 'bash /opt/app/task.sh', 'curl https://example.com/install.sh | sh',
    'printf hello > /tmp/a', 'echo $(date)', 'cat "$HOME/file"', 'ls `whoami`',
    'docker exec -u root web ps aux', 'docker run --rm nginx', 'docker restart web',
    'journalctl --cursor-file=/tmp/cursor', 'journalctl --vacuum-size=100M',
    'systemctl status nginx --root=/other', 'systemctl status nginx -Helsewhere',
    'hostname changed-name', 'ip address add 10.0.0.1/24 dev eth0',
    'ss -K dst 10.0.0.1', 'ss --kill dst 10.0.0.1',
    '/opt/app/cat /tmp/a', '/usr/bin/../local/bin/cat /tmp/a', 'CAT /tmp/a',
    "psql -f /opt/query.sql", "psql -c 'SELECT dangerous_function() FROM users'",
    "psql -c 'SELECT COUNT(*) FROM users; SELECT lo_export(1,\"/tmp/a\")'",
    "psql -c 'SELECT COUNT(*) FROM users /* complex query */'", 'cat "unterminated',
])
def test_unknown_programs_options_expansion_and_scripts_require_confirmation(command):
    assert assess(command)['action'] == 'confirm'


def test_auto_database_query_ignores_startup_files_checks_errors_and_uses_read_only_transaction():
    result = assess("docker exec db psql -U app -d app -c 'SELECT COUNT(*) FROM users;'")
    words = shlex.split(result['command'])
    assert words[:8] == ['docker', 'exec', 'db', 'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-U']
    assert words[-6:] == ['-c', 'BEGIN READ ONLY;', '-c', 'SELECT COUNT(*) FROM users;', '-c', 'ROLLBACK;']


def test_normalized_arguments_do_not_expand_quoted_metacharacters():
    result = assess("cat '/tmp/a;b >c'")
    assert result['action'] == 'auto'
    assert shlex.split(result['command']) == ['cat', '/tmp/a;b >c']
