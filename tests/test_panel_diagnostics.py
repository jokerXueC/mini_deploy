import json
import threading
import time
from types import SimpleNamespace

import pytest

import agent
import panel_diagnostics as diagnostics
import panel_code
import panel_runtime
import terminal_ai
import terminal_tools
import test_terminal_ai as ai_tests
from test_terminal_agent import tool_response, answer
from test_ssh_terminal import request
from test_deployment_removal import http_server as http_server, runtime as runtime
from test_server_performance import sample
from server_performance import History
from test_panel_runtime import runtime_evidence as runtime_evidence, CONTAINER, WINDOW

manager = ai_tests.manager
model_server = ai_tests.model_server


@pytest.fixture
def code_projects(monkeypatch):
    projects = [{'id': 'a' * 32, 'name': 'aimore', 'path': '/opt/aimore', 'containers': ['c' * 64]},
                {'id': 'b' * 32, 'name': 'other', 'path': '/srv/other', 'containers': []}]
    reads = []

    def project(identity):
        item = next((p for p in projects if p['id'] == identity), None)
        if not item:
            raise panel_code.CodeError('项目无效')
        return item

    def reader(scope, *, excluded):
        def search(path, query, pattern, offset, limit, check):
            check()
            reads.append((scope['id'], 'search', query))
            return {'matches': [{'path': 'backend/api.py', 'matches': [{'line': 42, 'text': 'async def claim():'}]}]}

        def read(path, start, lines, check):
            check()
            reads.append((scope['id'], 'read', path))
            return {'path': scope['path'] + '/' + path, 'lines': [{'line': 42, 'text': 'async def claim():'}]}

        return SimpleNamespace(project=scope, search=search, read=read)

    monkeypatch.setattr(agent, '_panel_code_catalog', lambda: SimpleNamespace(
        snapshot=lambda **kwargs: projects, project=project, excluded=()))
    monkeypatch.setattr(panel_code, 'supported', lambda: True)
    monkeypatch.setattr(panel_code, 'Reader', reader)
    return SimpleNamespace(projects=projects, reads=reads)


@pytest.fixture
def evidence(monkeypatch, tmp_path):
    history = History(tmp_path / 'performance')
    now = int(time.time())
    for offset, cpu in [(90, 20), (60, 98), (30, 34)]:
        at = now - offset
        history.observe(sample(cpu), at)
        history.record_context({'at': at, 'available': True, 'warnings': [], 'scanned': 2, 'readable': 2,
                               'processes': [{'pid': 123, 'name': 'python', 'cpu_percent': cpu,
                                              'rss_mib': 1024, 'host_cpu_percent': cpu / 4}]}, sample(cpu))
    history.flush(now, force=True)
    logs = []
    incident = {'incident': {'id': 'alert-test', 'title': 'CPU 持续升高', 'metadata': {'token': 'private-incident'}},
                'window': {'since': now - 300, 'until': now}, 'window_available': True,
                'log_source': 'container:' + 'a' * 64, 'timeline': [], 'related_events': [],
                'checked_at': now, 'process_scenes': [], 'resource_samples': [],
                'server_now': {'cpu_percent': 34, 'sampled_ts': now}}
    def read_logs(source, **options):
        logs.append((source, options))
        return {'lines': ['ERROR slow worker password=private-log'], 'truncated': True, 'since': options['since'], 'until': options['until']}
    def get_incident(key):
        if key != 'alert-test':
            raise ValueError('异常已不在保留范围')
        return incident
    monkeypatch.setattr(agent, '_incident_diagnostics', get_incident)
    monkeypatch.setattr(agent, '_server_performance', lambda: history)
    monkeypatch.setattr(agent, '_monitor', lambda: SimpleNamespace(snapshot=lambda: {'alerts': [], 'events': []}))
    monkeypatch.setattr(agent, '_realtime_metrics_payload', lambda: {'server': {'cpu_percent': 34, 'sampled_ts': now}})
    monkeypatch.setattr(agent, '_operations', lambda: SimpleNamespace(logs=read_logs))
    monkeypatch.setattr(agent, '_ssh_manager', lambda: pytest.fail('Panel evidence must never use SSH'))
    def backend(selection):
        return diagnostics.Backend(selection, '面板主机：test / 123', incident=get_incident,
            history=lambda: history, monitor=lambda: {'alerts': [], 'events': []}, realtime=lambda: {}, logs=read_logs)
    return SimpleNamespace(history=history, incident=incident, logs=logs, backend=backend)


def execute(backend, name, args=None):
    return json.loads(backend.execute(backend.plan(name, args or {}), threading.Event())['output'])['evidence']


def test_performance_evidence_includes_peak_bucket_and_persisted_processes(evidence):
    backend = evidence.backend({'kind': 'performance', 'period': '24h'})
    data = execute(backend, 'get_panel_performance')
    assert data['summary']['cpu_percent']['peak'] == 98
    assert data['peak_samples']['cpu_percent']['cpu_percent_peak'] == 98
    assert 0 < data['coverage_percent'] < 100
    assert data['process_scenes'][0]['cpu_name'] == 'python'
    scene = data['process_scenes'][0]
    detail = execute(backend, 'get_panel_process_scene', {'run': scene['run'], 'ts': scene['ts']})
    assert detail['processes'][0]['pid'] == 123
    assert execute(backend, 'get_panel_process_scene', {'run': 'a' * 32, 'ts': 100})['available'] is False


def test_incident_and_logs_are_timed_redacted_and_selected_server_only(evidence):
    backend = evidence.backend({'kind': 'incident', 'id': 'alert-test'})
    data = execute(backend, 'get_panel_incident')
    assert data['incident']['metadata']['token'] == '[已隐藏]'
    data = execute(backend, 'get_panel_incident_logs')
    assert 'private-log' not in json.dumps(data)
    assert data['truncated'] is True
    assert evidence.logs == [('container:' + 'a' * 64, {**evidence.incident['window'], 'limit': 100})] * 2


def test_missing_log_source_never_guesses_or_reads_current_logs(evidence):
    backend = evidence.backend({'kind': 'incident', 'id': 'alert-test'})
    evidence.incident['log_source'] = ''
    assert execute(backend, 'get_panel_incident_logs')['available'] is False
    assert evidence.logs == []
    with pytest.raises(terminal_tools.ToolError):
        execute(evidence.backend({'kind': 'performance'}), 'get_panel_incident_logs')


@pytest.mark.parametrize('name,args', [
    ('restart_container', {'container': 'api'}), ('write_file', {'path': '/root/a'}),
    ('execute_shell', {'command': 'rm -rf /'}), ('get_panel_incident_logs', {'path': '/etc/shadow'}),
    ('get_panel_performance', {'period': []}), ('get_panel_process_scene', {'run': 'a' * 32, 'ts': True}),
])
def test_readonly_tools_reject_writes_paths_and_invalid_arguments(evidence, name, args):
    with pytest.raises(terminal_tools.ToolError):
        evidence.backend({'kind': 'overview'}).plan(name, args)


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
def test_real_model_loop_reads_evidence_and_logs_before_answer_and_persists(
        http_server, manager, model_server, evidence, protocol):
    manager.save(ai_tests.config(model_server, protocol))
    def responses(body, count):
        text = json.dumps(body, ensure_ascii=False)
        assert 'private-incident' not in text and 'private-log' not in text
        assert 'CPU 持续升高' in text
        names = {t.get('name') or t.get('function', {}).get('name') for t in body['tools']}
        assert 'get_panel_incident_logs' in names and 'restart_container' not in names
        return tool_response(protocol, 'get_panel_incident_logs', {}) if count == 1 else answer(protocol)
    model_server.content = responses
    data = {**ai_tests.chat_data(manager, permission='readonly'), 'selection': {'kind': 'incident', 'id': 'alert-test'}}
    status, result = request(http_server, 'POST', '/terminal-ai/panel', data)
    assert status == 200
    done = ai_tests.finished(manager, terminal_ai.owner(http_server.cookie), result['id'])
    assert done['status'] == 'done' and done['permission'] == 'readonly'
    assert len(done['events']) == 2 and all(event['status'] == 'done' for event in done['events'])
    assert result['target'].startswith('面板主机：') and evidence.logs
    detail = manager.history.detail(result['history_id'])
    assert len(detail['turns']) == 1 and detail['conversation']['target'] == result['target']
    assert 'private-incident' not in json.dumps(detail) and 'private-log' not in json.dumps(detail)
    # Continue from durable history with the same host identity, independent of SSH.
    model_server.content = answer(protocol)
    status, followup = request(http_server, 'POST', '/terminal-ai/panel', {**data, 'history_id': result['history_id']})
    assert status == 200
    assert ai_tests.finished(manager, terminal_ai.owner(http_server.cookie), followup['id'])['status'] == 'done'
    assert len(manager.history.detail(result['history_id'])['turns']) == 2


def test_model_cannot_escalate_panel_diagnosis_to_modify_server(http_server, manager, model_server, evidence):
    manager.save(ai_tests.config(model_server))
    model_server.content = lambda body, count: tool_response('openai', 'restart_service', {'service': 'nginx'}) if count == 1 else answer('openai')
    status, result = request(http_server, 'POST', '/terminal-ai/panel', {
        **ai_tests.chat_data(manager, permission='readonly'), 'selection': {'kind': 'overview'}})
    assert status == 200
    done = ai_tests.finished(manager, terminal_ai.owner(http_server.cookie), result['id'])
    assert done['events'][1]['status'] == 'blocked'
    assert done['status'] == 'done'


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
def test_panel_reads_runtime_evidence_and_code_in_one_conversation_without_ssh(
        http_server, manager, model_server, evidence, code_projects, runtime_evidence, monkeypatch, protocol):
    manager.save(ai_tests.config(model_server, protocol))
    fake = runtime_evidence
    original = panel_runtime.Runtime
    monkeypatch.setattr(panel_runtime, 'Runtime', lambda **kwargs: original(**kwargs, **fake.options))
    monkeypatch.setattr(agent, '_operations_inventory', fake.dependencies['inventory'])
    monkeypatch.setattr(agent, '_request_history', fake.dependencies['requests'])
    monkeypatch.setattr(agent, '_certificate_discovery', fake.dependencies['discovery'])
    monkeypatch.setattr(agent, '_operations', lambda: SimpleNamespace(logs=fake.dependencies['logs']))
    monkeypatch.setattr(agent, '_monitor', lambda: SimpleNamespace(snapshot=fake.dependencies['monitor']))
    monkeypatch.setattr(agent, '_audit_tail', lambda **kwargs: fake.dependencies['audit']())
    config_id = fake.runtime.configurations()['items'][0]['id']
    sequence = [
        ('list_containers', {}),
        ('get_container_status', {'container': CONTAINER}),
        ('get_container_logs', {'container': CONTAINER, **WINDOW}),
        ('list_log_sources', {}),
        ('read_logs', {'source': 'container:' + CONTAINER, **WINDOW}),
        ('get_service_logs', {'service': 'docker', **WINDOW}),
        ('list_proxy_configs', {}),
        ('read_proxy_config', {'id': config_id}),
        ('list_request_sources', {}),
        ('get_request_records', {'source': 'api', **WINDOW, 'status': '5xx', 'search': '/cloud/runtime/commands/claim'}),
        ('get_panel_events', WINDOW),
        ('search_code', {'query': '/cloud/runtime/commands/claim'}),
        ('read_code_file', {'path': 'backend/api.py'}),
    ]
    def responses(body, count):
        names = {t.get('name') or t.get('function', {}).get('name') for t in body['tools']}
        assert {name for name, _ in sequence} <= names
        assert not names & {'execute_command', 'write_file', 'restart_container', 'manage_service'}
        text = json.dumps(body, ensure_ascii=False)
        assert 'log-secret' not in text and 'config-secret' not in text
        if count <= len(sequence):
            name, args = sequence[count - 1]
            return tool_response(protocol, name, args, call_id=f'runtime-{count}')
        for evidence_text in ('connection refused', 'aimore-cloud:8766', 'matching_records', '502', 'rebuild-1', 'async def claim'):
            assert evidence_text in text
        assert 'available_tools' in text and 'next_cursor' in text
        return answer(protocol)
    model_server.content = responses
    data = {**ai_tests.chat_data(manager, permission='readonly'), 'code_project': 'a' * 32,
            'question': '继续查 aimore 的 5xx，对照故障时间日志和配置，再检查代码；只读',
            'selection': {'kind': 'overview'}}
    status, job = request(http_server, 'POST', '/terminal-ai/panel', data)
    assert status == 200
    done = ai_tests.finished(manager, terminal_ai.owner(http_server.cookie), job['id'])
    assert done['status'] == 'done' and len(done['events']) == len(sequence) + 1, done
    assert all(event['status'] == 'done' and not event['write'] for event in done['events']), done
    stored = json.dumps(manager.history.detail(job['history_id']))
    assert 'log-secret' not in stored and 'config-secret' not in stored
    # Stored conversations retain tool access on a later request, not just first launch.
    model_server.content = lambda body, count: tool_response(protocol, 'list_containers', {}, 'followup') if count == 15 else answer(protocol)
    status, followup = request(http_server, 'POST', '/terminal-ai/panel', {**data, 'history_id': job['history_id']})
    assert status == 200
    done = ai_tests.finished(manager, terminal_ai.owner(http_server.cookie), followup['id'])
    assert done['status'] == 'done' and done['events'][-1]['status'] == 'done', done
    assert len(manager.history.detail(job['history_id'])['turns']) == 2


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
def test_model_receives_project_tools_and_searches_then_reads_without_ssh(
        http_server, manager, model_server, evidence, code_projects, protocol):
    manager.save(ai_tests.config(model_server, protocol))

    def responses(body, count):
        names = {t.get('name') or t.get('function', {}).get('name') for t in body['tools']}
        assert {'search_code', 'read_code_file', 'list_code_directory'} <= names
        assert 'write_file' not in names and 'execute_command' not in names
        if count == 1:
            return tool_response(protocol, 'search_code', {'query': '/cloud/runtime/commands/claim'})
        if count == 2:
            return tool_response(protocol, 'read_code_file', {'path': 'backend/api.py', 'start_line': 42}, call_id='call_read')
        text = json.dumps(body, ensure_ascii=False)
        assert 'async def claim' in text and '/opt/aimore/backend/api.py' in text and '42' in text
        return answer(protocol)

    model_server.content = responses
    status, result = request(http_server, 'POST', '/terminal-ai/panel', {
        **ai_tests.chat_data(manager, permission='readonly'), 'code_project': 'a' * 32,
        'question': '只检查 aimore 的 claim 接口代码，不修改', 'selection': {'kind': 'overview'}})
    assert status == 200
    done = ai_tests.finished(manager, terminal_ai.owner(http_server.cookie), result['id'])
    assert done['status'] == 'done' and all(e['status'] == 'done' for e in done['events']), done
    assert code_projects.reads == [('a' * 32, 'search', '/cloud/runtime/commands/claim'), ('a' * 32, 'read', 'backend/api.py')]
    detail = manager.history.detail(result['history_id'])
    assert '/opt/aimore' in detail['conversation']['target']
    assert 'backend/api.py' in str(detail['turns'][0]['events'])


def test_project_scope_is_bound_to_live_and_archived_conversations(
        http_server, manager, model_server, evidence, code_projects):
    manager.save(ai_tests.config(model_server))
    model_server.content = answer('openai')
    data = {**ai_tests.chat_data(manager, permission='readonly'), 'code_project': 'a' * 32,
            'selection': {'kind': 'overview'}}
    status, result = request(http_server, 'POST', '/terminal-ai/panel', data)
    assert status == 200
    ai_tests.finished(manager, terminal_ai.owner(http_server.cookie), result['id'])
    for project_id in ('b' * 32, ''):
        assert request(http_server, 'POST', '/terminal-ai/panel', {
            **data, 'code_project': project_id, 'conversation_id': result['conversation_id']})[0] == 409
        assert request(http_server, 'POST', '/terminal-ai/panel', {
            **data, 'code_project': project_id, 'history_id': result['history_id']})[0] == 400
    assert len(model_server.calls) == 1


def test_project_discovery_requires_login_and_recommends_only_unique_container_match(
        http_server, evidence, code_projects, monkeypatch):
    assert request(http_server, 'GET', '/terminal-ai/code-projects', authenticated=False)[0] == 401
    monkeypatch.setattr(agent, '_monitor', lambda: SimpleNamespace(snapshot=lambda: {
        'alerts': [{'id': 'incident-code', 'metadata': {'container_id': 'c' * 64}}], 'events': []}))
    status, result = request(http_server, 'GET', '/terminal-ai/code-projects?incident_id=incident-code')
    assert status == 200 and result['recommended'] == 'a' * 32
    code_projects.projects[1]['containers'] = ['c' * 64]
    assert request(http_server, 'GET', '/terminal-ai/code-projects?incident_id=incident-code')[1]['recommended'] == ''


def test_unselected_or_invalid_project_cannot_read_source(http_server, manager, model_server, evidence, code_projects):
    manager.save(ai_tests.config(model_server))
    data = {**ai_tests.chat_data(manager, permission='readonly'), 'selection': {'kind': 'overview'}}
    for invalid in ('/etc', 'f' * 32, None, []):
        assert request(http_server, 'POST', '/terminal-ai/panel', {**data, 'code_project': invalid})[0] == 400
    model_server.content = lambda body, count: tool_response('openai', 'read_code_file', {'path': 'backend/api.py'}) if count == 1 else answer('openai')
    status, result = request(http_server, 'POST', '/terminal-ai/panel', data)
    assert status == 200
    done = ai_tests.finished(manager, terminal_ai.owner(http_server.cookie), result['id'])
    assert done['events'][1]['status'] == 'blocked' and '选择代码项目' in done['events'][1]['result']
    assert not code_projects.reads


def test_panel_route_enforces_auth_origin_csrf_consent_and_scope(http_server, manager, model_server, evidence):
    manager.save(ai_tests.config(model_server))
    data = {**ai_tests.chat_data(manager, permission='readonly'), 'selection': {'kind': 'performance', 'period': '24h'}}
    for kwargs, expected in [({'authenticated': False}, 401), ({'csrf': False}, 403), ({'origin': False}, 403)]:
        assert request(http_server, 'POST', '/terminal-ai/panel', data, **kwargs)[0] == expected
    for changes in [{'permission': 'auto'}, {'session_id': 'remote'}, {'include_terminal': True},
                    {'consent': False}, {'selection': {'kind': 'performance', 'period': []}},
                    {'selection': {'kind': []}}, {'selection': {'kind': 'incident'}},
                    {'selection': {'kind': 'overview', 'command': 'rm /'}}]:
        assert request(http_server, 'POST', '/terminal-ai/panel', {**data, **changes})[0] == 400
    assert model_server.calls == []


def test_history_from_another_host_is_never_sent_to_model(http_server, manager, model_server, evidence):
    manager.save(ai_tests.config(model_server))
    manager.history.begin('foreign-history', 'foreign-job', '其他主机诊断', '面板主机：other / 456', 'test-model', 'foreign evidence')
    manager.history.finish('foreign-job', 'foreign answer', 'done', [])
    status, _ = request(http_server, 'POST', '/terminal-ai/panel', {
        **ai_tests.chat_data(manager, permission='readonly'), 'history_id': 'foreign-history',
        'selection': {'kind': 'overview'}})
    assert status == 400 and not model_server.calls


def test_log_failure_preserves_incident_and_missing_data_not_success(evidence):
    backend = evidence.backend({'kind': 'incident', 'id': 'alert-test'})
    def failure(*args, **kwargs):
        raise RuntimeError('Docker logs unavailable')
    backend.logs = failure
    data = execute(backend, 'get_panel_incident')
    assert data['incident']['id'] == 'alert-test' and data['logs']['available'] is False
    with pytest.raises(terminal_tools.ToolError, match='证据读取失败'):
        execute(backend, 'get_panel_incident_logs')


def test_sparse_long_history_keeps_peak_separate_from_sampled_trend(evidence):
    from copy import deepcopy
    original = evidence.history.query('7d', include_details=False)
    template = original['points'][0]
    points = [{**deepcopy(template), 'ts': template['ts'] + i * 30, 'cpu_percent_peak': 12} for i in range(200)]
    points[103]['cpu_percent_peak'] = 100
    fake = {**original, 'period': '7d', 'points': points}
    backend = evidence.backend({'kind': 'performance', 'period': '7d'})
    backend.history = lambda: SimpleNamespace(query=lambda *a, **k: fake)
    data = execute(backend, 'get_panel_performance')
    assert len(data['trend']) == 96 and data['point_count'] == 200
    assert data['peak_samples']['cpu_percent']['ts'] == points[103]['ts']


@pytest.mark.parametrize('period', ['24h', '7d'])
def test_large_performance_evidence_keeps_full_window_statistics_and_events(evidence, monkeypatch, period):
    now = int(time.time())
    monkeypatch.setattr(diagnostics.time, 'time', lambda: now)
    backend = evidence.backend({'kind': 'performance', 'period': period})
    report = evidence.history.query(period, include_details=False)
    events = [{'at_ts': int(time.time()) - n, 'id': f'event-{n}',
               'detail': '历史事件现场 ' * 500, 'token': 'private-event'} for n in range(40)]
    backend.monitor = lambda: {'alerts': [], 'events': events}
    result = backend.execute(backend.plan('get_panel_performance', {}), threading.Event())
    assert len(result['output']) > 100000
    assert result['exit_code'] == 0 and result['truncated'] is False
    assert 'private-event' not in result['output']
    data = json.loads(result['output'])['evidence']
    assert data['period'] == period
    assert data['since'] == report['since'] and data['until'] == report['until']
    assert data['summary'] == report['summary']
    assert data['peak_samples']['cpu_percent']['cpu_percent_peak'] == 98
    assert data['point_count'] == len(report['points'])
    assert len(data['events']) == 40 and data['events'][-1]['id'] == 'event-39'
    scene = data['process_scenes'][0]
    assert execute(backend, 'get_panel_process_scene', {'run': scene['run'], 'ts': scene['ts']})['processes'][0]['pid'] == 123


def test_large_process_scene_can_be_read_without_losing_processes(evidence):
    backend = evidence.backend({'kind': 'performance', 'period': '24h'})
    scene = execute(backend, 'get_panel_performance')['process_scenes'][0]
    original = evidence.history.context(scene['run'], scene['ts'])
    processes = [{**original['processes'][0], 'pid': n + 1, 'name': f'worker-{n}'} for n in range(2000)]
    backend.history = lambda: SimpleNamespace(context=lambda run, ts: {**original, 'processes': processes})
    result = backend.execute(backend.plan('get_panel_process_scene', {'run': scene['run'], 'ts': scene['ts']}), threading.Event())
    assert len(result['output']) > 100000 and result['truncated'] is False
    assert json.loads(result['output'])['evidence']['processes'] == processes


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
def test_large_performance_result_reaches_model(http_server, manager, model_server, evidence, monkeypatch, protocol):
    manager.save(ai_tests.config(model_server, protocol))
    events = [{'at_ts': int(time.time()) - n, 'id': f'large-evidence-{n}',
               'detail': '历史事件现场 ' * 500} for n in range(40)]
    monkeypatch.setattr(agent, '_monitor', lambda: SimpleNamespace(snapshot=lambda: {'alerts': [], 'events': events}))

    def respond(body, count):
        text = json.dumps(body, ensure_ascii=False)
        assert len(text) > 100000 and 'large-evidence-39' in text
        assert '证据过大' not in text and 'cpu_percent_peak' in text
        return answer(protocol)

    model_server.content = respond
    status, result = request(http_server, 'POST', '/terminal-ai/panel', {
        **ai_tests.chat_data(manager, permission='readonly'), 'selection': {'kind': 'performance', 'period': '24h'}})
    assert status == 200
    done = ai_tests.finished(manager, terminal_ai.owner(http_server.cookie), result['id'])
    assert done['status'] == 'done' and done['events'][0]['status'] == 'done'
    assert model_server.calls
