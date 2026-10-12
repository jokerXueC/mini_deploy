"""Real SFTP preservation, approval previews, recovery persistence and state verification."""
import hashlib
import json
import threading

import pytest

import ssh_files
import terminal_ai as ai
import terminal_tools as tools
from test_ssh_files import file_session as file_session, manager as manager, peer as peer, run
import test_terminal_ai as ai_tests
from test_terminal_ai import model_server as model_server, config
from test_terminal_agent import submit, wait_status, tool_response, answer

ai_manager = ai_tests.manager


def preview(session, operation):
    return session.execute_plan({'name': 'preview_file', 'operation': operation['name'],
                                 'arguments': operation['arguments'], 'transport': 'sftp'}, threading.Event())['preview']


def test_real_preview_restore_roundtrip_retains_all_versions(file_session):
    session, root = file_session
    path = root / 'app.conf'
    path.write_text('port=8080\n', encoding='utf-8')
    digest = run(session, 'inspect_file', path='/app.conf')['sha256']
    operation = tools.plan('edit_file', {'path': '/app.conf', 'old_text': '8080', 'new_text': '8081',
                                        'expected_sha256': digest})
    operation['preview'] = preview(session, operation)
    assert '-port=8080' in operation['preview']['diff'] and '+port=8081' in operation['preview']['diff']
    assert len(list(root.iterdir())) == 1 and path.read_text() == 'port=8080\n'
    changed = session.execute_plan(operation, threading.Event())
    assert changed['verification']['status'] == 'passed'
    recovery = tools.plan('restore_file', changed['recovery'])
    recovery['preview'] = preview(session, recovery)
    assert '+port=8080' in recovery['preview']['diff']
    restored = session.execute_plan(recovery, threading.Event())
    assert restored['verification']['status'] == 'passed' and path.read_text() == 'port=8080\n'
    assert sorted(p.read_text() for p in root.glob('*.mini-deploy-backup.*')) == ['port=8080\n', 'port=8081\n']
    redone = run(session, 'restore_file', **restored['recovery'])
    assert path.read_text() == 'port=8081\n' and redone['verification']['status'] == 'passed'


@pytest.mark.parametrize('changed', ['current', 'backup'])
def test_restore_refuses_changed_current_or_backup_after_preview(file_session, changed):
    session, root = file_session
    path = root / 'app.conf'
    path.write_text('old', encoding='utf-8')
    updated = run(session, 'write_file', path='/app.conf', content='new',
                  expected_sha256=hashlib.sha256(b'old').hexdigest())
    operation = tools.plan('restore_file', updated['recovery'])
    operation['preview'] = preview(session, operation)
    target = path if changed == 'current' else root / updated['backup'].lstrip('/')
    target.write_text('user updated', encoding='utf-8')
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    with pytest.raises(ssh_files.FileError):
        session.execute_plan(operation, threading.Event())
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before


@pytest.mark.parametrize('backup', ['/elsewhere', '/app.conf.mini-deploy-backup.1.zz',
                                     '/app.conf.mini-deploy-backup.1.' + 'a' * 16 + '/subfile'])
def test_restore_rejects_unrelated_paths(backup):
    with pytest.raises(tools.ToolError):
        tools.plan('restore_file', {'path': '/app.conf', 'backup_path': backup,
                                   'expected_sha256': 'a' * 64, 'backup_sha256': 'b' * 64})


def test_failed_config_validation_blocks_reload_and_recovery_survives_restart(
        file_session, ai_manager, model_server):
    session, root = file_session
    path = root / 'app.conf'
    path.write_text('port=8080\n', encoding='utf-8')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    arguments = {'path': '/app.conf', 'old_text': '8080', 'new_text': '8081', 'expected_sha256': digest,
                 'validation': {'runtime': 'nginx', 'path': '/app.conf'}}
    ai_manager.save(config(model_server))
    model_server.content = lambda body, n: (
        tool_response('openai', 'edit_file', arguments) if n == 1 else
        tool_response('openai', 'reload_service', {'service': 'nginx'}, 'reload') if n == 2 else answer('openai'))
    executed = []
    def execute(plan, cancel):
        executed.append(plan['name'])
        if plan['name'] == 'validate_config':
            return {'exit_code': 1, 'output': 'syntax invalid'}
        return session.execute_plan(plan, cancel)
    job = submit(ai_manager, mode='approve', execute=execute)
    pending = wait_status(ai_manager, 'owner', job['id'], {'awaiting_approval'})
    assert executed == ['preview_file'] and path.read_text() == 'port=8080\n'
    assert '+port=8081' in pending['approval']['preview']['diff']
    ai_manager.approve('owner', job['id'], pending['approval']['id'], True)
    result = wait_status(ai_manager, 'owner', job['id'], {'done', 'failed'})
    assert [e['status'] for e in result['events']] == ['failed', 'blocked']
    assert result['events'][0]['verification']['status'] == 'failed'
    assert 'reload_service' not in executed and path.read_text() == 'port=8081\n'
    restarted = ai.Manager(ai_manager.directory)
    try:
        data = {'job_id': job['id'], 'event_id': 'call_one'}
        with pytest.raises(ai.terminal_history.HistoryError):
            restarted.submit('owner', action='restore', data=data, session_id='different', target='other@test:22', executor=execute)
        restarted.set_access('owner', 'ssh', {'mode': 'readonly'})
        with pytest.raises(ai.AIError, match='修改权限'):
            restarted.submit('owner', action='restore', data=data, session_id='ssh', target='demo@test:22', executor=execute)
        restarted.set_access('owner', 'ssh', {'mode': 'approve', 'consent': True})
        def recover(plan, cancel):
            return {'exit_code': 0, 'output': 'valid'} if plan['name'] == 'validate_config' else session.execute_plan(plan, cancel)
        recovered = restarted.submit('owner', action='restore', data=data, session_id='ssh', target='demo@test:22', executor=recover)
        pending = wait_status(restarted, 'owner', recovered['id'], {'awaiting_approval'})
        assert '+port=8080' in pending['approval']['preview']['diff']
        restarted.approve('owner', recovered['id'], pending['approval']['id'], True)
        assert wait_status(restarted, 'owner', recovered['id'], {'done', 'failed'})['status'] == 'done'
        assert path.read_text() == 'port=8080\n' and len(list(root.glob('*.mini-deploy-backup.*'))) == 2
        assert len(model_server.calls) == 3
    finally:
        restarted.shutdown()


def test_failed_service_state_verification_prevents_later_write(ai_manager, model_server):
    ai_manager.save(config(model_server))
    model_server.content = lambda body, n: tool_response('openai', 'restart_service', {'service': 'nginx'}) if n == 1 else (
        tool_response('openai', 'reload_service', {'service': 'nginx'}, 'next') if n == 2 else answer('openai'))
    operations = []
    def execute(plan, cancel):
        operations.append(plan['name'])
        return {'exit_code': 0, 'output': 'LoadState=loaded\nActiveState=failed\n'}
    job = submit(ai_manager, mode='auto', execute=execute)
    result = wait_status(ai_manager, 'owner', job['id'], {'done', 'failed'})
    assert operations == ['restart_service', 'get_service_status']
    assert result['events'][0]['verification']['status'] == 'failed'
    assert result['events'][1]['status'] == 'blocked'


def test_preview_redaction_is_valid_structured_data_and_does_not_expose_credentials(ai_manager):
    value = {'diff': '-password=one\n+password=two\n', 'nested': {'api_key': 'sk-' + 'x' * 20}}
    safe = ai_manager._safe_data({'api_key': 'one'}, value)
    serialized = json.dumps(safe)
    assert 'password=two' not in serialized and 'sk-' not in serialized
    assert json.loads(serialized) == safe


def test_health_without_model_is_readonly_and_saved(ai_manager):
    operations = []
    job = ai_manager.submit('owner', action='health', data={}, session_id='ssh', target='demo@test:22',
                            executor=lambda plan, cancel: operations.append(plan) or {'exit_code': 0, 'output': 'collected'})
    result = wait_status(ai_manager, 'owner', job['id'], {'done', 'failed'})
    assert result['status'] == 'done' and len(operations) == 4 and all(not p['write'] for p in operations)
    assert ai_manager.history.detail(job['history_id'])['turns'][0]['answer'] == result['text']


def test_health_reports_only_observed_abnormal_states():
    result = {'output': 'Mem: 1000 950 10 0 40 50\n/dev/sda 100G 93G 7G 93% /\n'}
    assert len(tools.health_findings(tools.plan('get_server_resources', {}), result)) == 2
    assert tools.health_findings(tools.plan('list_containers', {}),
                                 {'output': 'id\tapi\tUp 3 hours (unhealthy)\t80/tcp'})
    assert tools.health_findings(tools.plan('list_services', {}),
                                 {'output': 'app.service loaded failed failed Application'})
    assert not tools.health_findings(tools.plan('get_server_resources', {}), {'output': 'unrecognized'})


def test_operation_reason_does_not_change_command_or_bypass_repeat_signature():
    original = tools.plan('restart_service', {'service': 'nginx'})
    explained = tools.plan('restart_service', {'service': 'nginx', 'reason': '修复当前进程的异常状态'})
    assert original['arguments'] == explained['arguments'] and original['command'] == explained['command']
    assert explained['reason'] == '修复当前进程的异常状态'
