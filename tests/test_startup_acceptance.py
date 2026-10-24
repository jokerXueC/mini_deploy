"""Startup retry and actual-release acceptance without a running systemd host."""
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.check_agent_health import wait_for_health
from test_installer_operations import bash_run, function, INSTALLER


def simulate(tmp_path, replies, seconds=5):
    path = tmp_path / 'agent.py'
    path.write_text('installed agent')
    release = hashlib.sha256(path.read_bytes()).hexdigest()
    calls, sleeps, clock = [], [], [0]

    def open_health(url, timeout):
        calls.append((url, timeout))
        value = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(value, Exception):
            raise value
        raw = value(release) if callable(value) else value
        response = io.BytesIO(raw if isinstance(raw, bytes) else json.dumps(raw).encode())
        response.status = 200
        return response

    def sleep(seconds):
        clock[0] += seconds
        sleeps.append(seconds)

    options = dict(seconds=seconds, opener=SimpleNamespace(open=open_health), clock=lambda: clock[0], sleep=sleep)
    return path, options, calls, sleeps


def test_retries_until_exact_running_release_and_healthy_status(tmp_path):
    path, options, calls, sleeps = simulate(tmp_path, [OSError('connection refused'),
        {'status': 'ok', 'release': 'old'}, lambda release: {'status': 'ok', 'release': release}])
    wait_for_health(path, **options)
    assert len(calls) == 3 and sleeps == [1, 1]
    assert all(url == 'http://127.0.0.1:6868/health' and 0 < timeout <= 2 for url, timeout in calls)


@pytest.mark.parametrize('reply', [{'status': 'ok', 'release': 'old'}, b'<html>login</html>', b'[]', b'x' * 5000,
                                   OSError('refused'), lambda release: {'status': 'failed', 'release': release}])
def test_bad_or_old_health_cannot_be_reported_as_install_success(tmp_path, reply):
    path, options, calls, sleeps = simulate(tmp_path, [reply], seconds=3)
    with pytest.raises(RuntimeError, match='acceptance failed'):
        wait_for_health(path, **options)
    assert len(calls) == 3 and sum(sleeps) == 3


def test_shell_acceptance_failure_stops_before_success_and_shows_custom_paths():
    script = '\n'.join([function('verify_agent_startup'), 'systemctl() { return 0; }',
                        'verify_agent_startup', 'printf "INSTALL_SUCCESS"'])
    result = bash_run(script, PYTHON_BIN='/bin/false', APP_HOME='/opt/custom-panel',
                      ENV_FILE='/etc/custom-panel.env', SERVICE_NAME='custom-panel')
    assert result.returncode != 0
    assert 'INSTALL_SUCCESS' not in result.stdout
    assert 'journalctl -u custom-panel' in result.stderr
    assert '/etc/custom-panel.env bash /opt/custom-panel/scripts/doctor.sh' in result.stderr


def test_shell_acceptance_requires_active_service_even_after_valid_health():
    result = bash_run(function('verify_agent_startup') + '\nsystemctl() { return 1; }\nverify_agent_startup',
                      PYTHON_BIN='/bin/true', APP_HOME='/opt/custom-panel', ENV_FILE='/etc/custom-panel.env',
                      SERVICE_NAME='custom-panel')
    assert result.returncode != 0
    assert 'not active: custom-panel' in result.stderr


def test_acceptance_precedes_disarming_and_success_and_helpers_are_shipped():
    start = INSTALLER.index('\nsystemctl restart "$SERVICE_NAME"\n', INSTALLER.index('\nwrite_systemd_service\n'))
    assert start < INSTALLER.index('\nverify_agent_startup\n', start) < INSTALLER.index('\ndisarm_upgrade_service_failsafe\n', start)
    final = INSTALLER.index('\ninstall_docker_if_requested\n', start)
    assert final < INSTALLER.index('\nverify_agent_startup\n', final) < INSTALLER.index('mini_deploy installed.', final)
    manifest = json.loads(Path('release.json').read_text())['program_entries']
    assert {'setup_checks.py', 'scripts/check_agent_health.py'} <= set(manifest)
