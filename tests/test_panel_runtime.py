import hashlib
import io
import json
import tarfile
import threading
from types import SimpleNamespace

import pytest

import panel_code
import panel_runtime
import terminal_tools
from request_history import History
from test_request_history import NOW, ingest, line


CONTAINER = 'c' * 64
WINDOW = {'since': NOW - 300, 'until': NOW}


def archive(content, *, name='Caddyfile', kind=tarfile.REGTYPE):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as stream:
        entry = tarfile.TarInfo(name)
        entry.type = kind
        entry.linkname = '/etc/shadow' if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ''
        entry.size = len(content) if kind == tarfile.REGTYPE else 0
        stream.addfile(entry, io.BytesIO(content) if entry.size else None)
    return output.getvalue()


@pytest.fixture
def runtime_evidence(tmp_path):
    history = History(tmp_path / 'requests')
    ingest(history, [line('/cloud/runtime/commands/claim', status=502, duration='26.5')] * 9
           + [line('/cloud/healthy'), line('/other', status=500)])
    commands, docker_checks, logs = [], [], []
    discovery = SimpleNamespace(lock=threading.RLock(), last_scan=NOW, running=False, issues=[], rows=[{
        'config_path': '/etc/caddy/Caddyfile', 'target': {
            'kind': 'caddy', 'location': 'docker', 'container_id': CONTAINER, 'active': True}}])
    config = {'content': archive(b'aimore.test {\n reverse_proxy aimore-cloud:8766\n}\npassword=config-secret\n')}

    def runner(argv, **options):
        commands.append((argv, options))
        if argv[:2] == ['docker', 'cp']:
            return 0, config['content'], False
        if argv[:2] == ['docker', 'ps']:
            return 0, CONTAINER + '\taimore-caddy-1\tUp 1 hour', False
        return 0, '2027-01-15T07:59:50Z ERROR dial tcp: connection refused password=log-secret', False

    def inventory(**options):
        return {'processes': [{'pid': 123, 'name': 'python'}], 'sources': [
            {'id': 'container:' + CONTAINER, 'kind': 'container', 'name': 'aimore-caddy-1'}],
            'checked_at': NOW, 'warnings': []}

    def read_logs(source, **options):
        logs.append((source, options))
        if source != 'container:' + CONTAINER:
            raise ValueError('日志来源未发现')
        return {'lines': ['2027-01-15T07:59:50Z ERROR connection refused'], 'since': options['since'],
                'until': options['until'], 'source': source, 'truncated': False}

    def monitor():
        return {'alerts': [], 'events': [{'id': f'rebuild-{n}', 'at_ts': NOW - n} for n in range(1, 7)]}

    def audit():
        return [{'action': 'ai_tool_executed', 'at': '2027-01-15 08:00:00+00:00'}]
    dependencies = dict(inventory=inventory, logs=read_logs, requests=lambda: history,
                        discovery=lambda: discovery, monitor=monitor, audit=audit)
    options = dict(runner=runner, local_docker=lambda: docker_checks.append(True))
    runtime = panel_runtime.Runtime(**dependencies, **options)
    return SimpleNamespace(runtime=runtime, dependencies=dependencies, options=options,
                           commands=commands, docker_checks=docker_checks, logs=logs,
                           discovery=discovery, history=history, config=config)


def execute(runtime, name, args=None):
    return runtime.execute(runtime.plan(name, args or {}), threading.Event())


def test_shared_tools_are_readonly_fixed_argv_and_historical_logs_are_time_bounded(runtime_evidence):
    runtime = runtime_evidence.runtime
    execute(runtime, 'get_container_logs', {'container': CONTAINER,
            'since': '2026-10-23T15:37:00+08:00', 'until': '2026-10-23T15:43:00+08:00'})
    command = runtime_evidence.commands[-1][0]
    assert command == ['docker', 'logs', '--timestamps', '--since', '2026-10-23T07:37:00+00:00',
                       '--until', '2026-10-23T07:43:00+00:00', '--tail', '100', CONTAINER]
    assert runtime_evidence.docker_checks == [True]
    data = execute(runtime, 'get_service_logs', {'service': 'docker', **WINDOW})
    assert runtime_evidence.commands[-1][0][-2:] == ['-u', 'docker.service']
    assert data['since'] == NOW - 300 and 'log-secret' not in data['output']
    assert not runtime.plan('run_diagnostic', {'check': 'processes'})['write']
    assert 'args' not in terminal_tools.DIAGNOSTICS['processes']


@pytest.mark.parametrize('name,args', [
    ('execute_command', {'command': 'id'}), ('restart_service', {'service': 'docker'}),
    ('write_file', {'path': '/opt/aimore/x.py', 'content': ''}),
    ('read_file', {'path': '/etc/shadow'}), ('read_proxy_config', {'path': '/etc/shadow'}),
    ('read_proxy_config', {'id': 'x', 'lines': True}),
    ('get_container_logs', {'container': 'x;reboot'}),
    ('get_container_logs', {'container': 'api', 'since': 100}),
    ('get_container_logs', {'container': 'api', 'since': True, 'until': 100}),
    ('get_container_logs', {'container': 'api', 'since': '2026-10-23T15:37:00', 'until': NOW}),
    ('get_service_logs', {'service': 'api', 'since': '$(reboot)', 'until': NOW}),
    ('read_logs', {'source': 'x', 'since': NOW, 'until': NOW - 1}),
    ('read_logs', {'source': 'x', 'since': float('nan'), 'until': NOW}),
    ('read_logs', {'source': 'x', 'since': NOW - 86401, 'until': NOW}),
    ('read_logs', {'source': 'x', 'limit': 1.1}),
    ('get_request_records', {'source': 'api', **WINDOW, 'cursor': 'bad'}),
])
def test_runtime_rejects_writes_paths_injection_and_invalid_arguments(runtime_evidence, name, args):
    with pytest.raises(ValueError):
        execute(runtime_evidence.runtime, name, args)
    assert not runtime_evidence.commands


def test_execution_replans_ignores_forged_argv_and_honors_cancellation(runtime_evidence):
    runtime = runtime_evidence.runtime
    plan = runtime.plan('list_containers', {})
    runtime.execute({**plan, 'argv': ['rm', '-rf', '/'], 'write': True}, threading.Event())
    assert runtime_evidence.commands[-1][0][:2] == ['docker', 'ps']
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(RuntimeError, match='停止'):
        runtime.execute(plan, cancel)
    assert len(runtime_evidence.commands) == 1


def test_command_failures_remote_daemon_and_truncation_are_not_reported_as_complete(runtime_evidence):
    runtime = runtime_evidence.runtime
    runtime.runner = lambda *a, **k: (1, 'missing password=hide-this', False)
    with pytest.raises(terminal_tools.ToolError, match='读取失败') as error:
        execute(runtime, 'get_container_logs', {'container': CONTAINER})
    assert 'hide-this' not in str(error.value)
    runtime.runner = lambda *a, **k: (-9, 'log fragment', True)
    assert execute(runtime, 'get_container_logs', {'container': CONTAINER})['truncated']
    def remote():
        raise ValueError('remote Docker forbidden')
    runtime.local_docker = remote
    with pytest.raises(ValueError, match='remote Docker'):
        execute(runtime, 'list_containers')


def test_discovered_proxy_snapshot_is_paged_redacted_and_read_without_exec(runtime_evidence):
    runtime = runtime_evidence.runtime
    content = b'-----BEGIN PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----\nreverse_proxy api:8080\n'
    content += b'# padding\n' * 60000 + b'final_config_line\n'
    runtime_evidence.config['content'] = archive(content)
    catalog = execute(runtime, 'list_proxy_configs')
    identity = catalog['items'][0]['id']
    first = execute(runtime, 'read_proxy_config', {'id': identity, 'lines': 3})
    assert first['next_line'] == 4 and first['total_lines'] == 60005
    assert 'private-material' not in str(first)
    last = execute(runtime, 'read_proxy_config', {'id': identity, 'start_line': 60005})
    assert last['lines'][0]['text'] == 'final_config_line' and last['next_line'] is None
    assert last['sha256'] == hashlib.sha256(content).hexdigest()
    assert first['sha256'] == last['sha256'] and last['container_id'] == CONTAINER
    assert runtime_evidence.commands[-1][0] == ['docker', 'cp', CONTAINER + ':/etc/caddy/Caddyfile', '-']
    assert runtime_evidence.commands[-1][1]['binary'] is True
    runtime_evidence.discovery.rows.clear()
    with pytest.raises(ValueError, match='来源不存在'):
        execute(runtime, 'read_proxy_config', {'id': identity})


@pytest.mark.parametrize('kind', [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.FIFOTYPE])
def test_proxy_reader_rejects_archived_links_directories_and_devices(runtime_evidence, kind):
    runtime_evidence.config['content'] = archive(b'private', kind=kind)
    identity = execute(runtime_evidence.runtime, 'list_proxy_configs')['items'][0]['id']
    with pytest.raises(ValueError, match='不读取链接'):
        execute(runtime_evidence.runtime, 'read_proxy_config', {'id': identity})


@pytest.mark.parametrize('path', ['/root/.ssh/config.json', '/etc/caddy/../../shadow', '/run/secrets/config.json', '/etc/caddy/private.key'])
def test_discovery_does_not_grant_access_to_credentials(runtime_evidence, path):
    runtime_evidence.discovery.rows[0]['config_path'] = path
    identity = execute(runtime_evidence.runtime, 'list_proxy_configs')['items'][0]['id']
    with pytest.raises(ValueError, match='路径不安全'):
        execute(runtime_evidence.runtime, 'read_proxy_config', {'id': identity})
    assert not runtime_evidence.commands


@pytest.mark.skipif(not panel_code.supported(), reason='Requires Linux descriptor-safe file access')
def test_host_proxy_reader_preserves_file_and_rejects_replaced_link(runtime_evidence, tmp_path):
    config = tmp_path / 'Caddyfile'
    config.write_text('reverse_proxy api:8080\npassword=never-send\n')
    runtime_evidence.discovery.rows[0].update(config_path=str(config), target={'kind': 'caddy', 'location': 'host'})
    identity = execute(runtime_evidence.runtime, 'list_proxy_configs')['items'][0]['id']
    data = execute(runtime_evidence.runtime, 'read_proxy_config', {'id': identity})
    assert 'never-send' not in json.dumps(data) and 'never-send' in config.read_text()
    config.rename(tmp_path / 'saved.conf')
    config.symlink_to(tmp_path / 'saved.conf')
    with pytest.raises(ValueError, match='链接'):
        execute(runtime_evidence.runtime, 'read_proxy_config', {'id': identity})


def test_discovered_logs_requests_and_events_are_available_without_selected_incident(runtime_evidence):
    runtime = runtime_evidence.runtime
    source = execute(runtime, 'list_log_sources')['sources'][0]['id']
    assert execute(runtime, 'read_logs', {'source': source, **WINDOW})['since'] == WINDOW['since']
    assert execute(runtime, 'list_request_sources')['sources'][0]['key'] == 'api'
    records = execute(runtime, 'get_request_records', {'source': 'api', **WINDOW,
                      'status': '5xx', 'search': '/cloud/runtime/commands/claim'})
    assert records['matching_records'] == 9 and len(records['items']) == 9
    events = execute(runtime, 'get_panel_events', {**WINDOW, 'limit': 2})
    assert events['next_offset'] == 2 and events['audit_scanned'] == 1
    page = execute(runtime, 'get_panel_events', {**WINDOW, 'limit': 2, 'offset': 2})
    assert page['events'][0]['id'] == 'rebuild-3'

