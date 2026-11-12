"""AI database bridge uses saved credentials and the real read-only query manager."""
import json
import threading
from types import SimpleNamespace

import pytest

import database_readonly
import panel_database
from terminal_tools import ToolError
from test_database_integration import postgres_spec as postgres_spec, mysql_spec as mysql_spec


@pytest.mark.parametrize('fixture', ['postgres_spec', 'mysql_spec'])
def test_ai_reads_real_saved_database_and_rejects_writes(request, tmp_path, fixture):
    spec = request.getfixturevalue(fixture)
    manager = database_readonly.Manager(tmp_path)
    profile = manager.profiles.save(spec)
    reader = panel_database.Reader(lambda: manager, 'ai-owner')
    cancel = threading.Event()
    schema = 'public' if spec['engine'] == 'postgres' else 'fixture'
    args = dict(connection=profile['id'], database=spec['database'], schema=schema, table='orders')
    try:
        listed = reader.execute('list_database_connections', {}, cancel)
        assert listed['connections'][0]['id'] == profile['id']
        assert spec['password'] not in json.dumps(listed) and 'password' not in listed['connections'][0]
        catalog = reader.execute('get_database_catalog', {'connection': profile['id']}, cancel)
        assert any(table['name'] == 'orders' for table in catalog['tables'])
        structure = reader.execute('get_database_structure', args, cancel)
        assert structure['indexes']['rows'] and structure['access']['transaction_read_only']
        relations = reader.execute('get_database_relationships', {k: v for k, v in args.items() if k != 'table'}, cancel)
        assert len(relations['relationships']) >= 5
        page = reader.execute('read_database_table', {**args, 'page': 1}, cancel)
        assert len(page['rows']) == 50 and page['has_more'] and page['read_only']
        result = reader.execute('query_database', {'connection': profile['id'], 'database': spec['database'],
            'sql': f"SELECT\n COUNT(*) AS total, 'test-secret' AS api_token FROM {schema}.orders"}, cancel)
        assert result['rows'] == [[123, '[已隐藏]']]
        assert 'test-secret' not in json.dumps(result)
        assert result['connection'] == profile['id'] and result['database'] == spec['database']
        with pytest.raises(ToolError):
            reader.execute('query_database', {'connection': profile['id'], 'sql': f'DELETE FROM {schema}.orders'}, cancel)
    finally:
        if manager._expiry_timer:
            manager._expiry_timer.cancel()


@pytest.mark.parametrize('allowed,scoped,reason', [(False, False, 'HTTP'), (True, True, '限定源码项目')])
def test_unapproved_http_or_project_scope_never_opens_database(allowed, scoped, reason):
    reader = panel_database.Reader(lambda: pytest.fail('must not access manager'), 'owner', allowed=allowed, scoped=scoped)
    assert not reader.status()['available']
    with pytest.raises(ToolError, match=reason):
        reader.execute('list_database_connections', {}, threading.Event())


def test_cancellation_propagates_and_abandoned_jobs_are_cancelled():
    cancel, cancelled = threading.Event(), []
    def start(owner, data):
        cancel.set()
        return {'id': 'job', 'state': 'running'}
    manager = SimpleNamespace(start=start, cancel=lambda owner, key: cancelled.append((owner, key)))
    reader = panel_database.Reader(lambda: manager, 'owner')
    with pytest.raises(ToolError, match='已停止'):
        reader.execute('query_database', {'connection': 'a' * 24, 'sql': 'SELECT 1'}, cancel)
    assert cancelled == [('owner', 'job')]
    reader.manager = lambda: pytest.fail('must not open another query after stop')
    with pytest.raises(ToolError, match='已停止'):
        reader.execute('list_database_connections', {}, cancel)


def test_timeout_cancels_job_and_keeps_failure_explicit(monkeypatch):
    cancelled, tick = [], iter([0, 46])
    monkeypatch.setattr(panel_database.time, 'monotonic', lambda: next(tick))
    manager = SimpleNamespace(start=lambda *a: {'id': 'job', 'state': 'running'},
                              cancel=lambda owner, key: cancelled.append(key))
    reader = panel_database.Reader(lambda: manager, 'owner')
    with pytest.raises(ToolError, match='请求取消'):
        reader.execute('query_database', {'connection': 'a' * 24, 'sql': 'SELECT 1'}, threading.Event())
    assert cancelled == ['job']
