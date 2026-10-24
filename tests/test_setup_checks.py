"""Truthful enablement evidence without starting probes or mutating stores."""
import json
import io
import sqlite3
from types import SimpleNamespace

import pytest

import agent
import setup_checks

NOW = 1_800_000_000


def check(home, **kwargs):
    options = dict(system={'server': {'sampled_ts': NOW, 'cpu_percent': 5},
                           'docker': {'available': True, 'sampled_at': NOW, 'containers': []}},
                   authenticated=True, ssh_available=True, ai={}, notifications=False,
                   protection={}, started_at=NOW - 300, now=NOW)
    return setup_checks.snapshot(home, **{**options, **kwargs})


def items(home, **kwargs):
    return {row['key']: row for row in check(home, **kwargs)['items']}


def seed(home, name, schema, *args):
    path = home / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript(schema)
        if args:
            db.execute(*args)
    return path


def performance(home, at=NOW):
    return seed(home, 'performance/server.sqlite3', 'CREATE TABLE samples(ts INT, last REAL); CREATE INDEX age ON samples(ts);',
                'INSERT INTO samples VALUES (?,?)', (at, at))


def test_checks_do_not_create_missing_data_and_optional_features_are_not_install_failures(tmp_path):
    absent = tmp_path / 'not-created'
    result = items(absent, started_at=NOW - 10)
    assert not absent.exists()
    assert result['history']['state'] == 'pending'
    assert result['requests']['state'] == 'setup'
    assert result['ai']['state'] == 'setup'
    assert result['ssh']['state'] == 'available'
    assert result['docker']['state'] == 'verified'  # A valid empty Docker result.
    assert result['resources']['state'] == 'verified'


@pytest.mark.parametrize('at,state', [(NOW, 'verified'), (NOW - 91, 'error'), (NOW + 20, 'error')])
def test_only_recent_durable_history_counts_and_read_does_not_change_database(tmp_path, at, state):
    path = performance(tmp_path, at)
    before = path.read_bytes(), path.stat().st_mtime_ns
    result = items(tmp_path)
    assert result['history']['state'] == state
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert sorted(p.name for p in path.parent.iterdir()) == ['server.sqlite3']


def test_live_resources_do_not_hide_persistence_error_or_stale_docker(tmp_path):
    performance(tmp_path)
    result = items(tmp_path, history_error='database full secret path', system={
        'server': {'cpu_percent': 5, 'sampled_ts': NOW},
        'docker': {'available': True, 'sampled_at': NOW - 300, 'stale': True}})
    assert result['resources']['state'] == 'verified'
    assert result['history']['state'] == 'error'
    assert result['docker']['state'] == 'pending'
    assert 'secret path' not in json.dumps(result)


@pytest.mark.parametrize('value', [None, float('nan'), float('inf'), '5'])
def test_invalid_resource_values_never_pass_verification(tmp_path, value):
    assert items(tmp_path, system={'server': {'sampled_ts': NOW, 'cpu_percent': value}})['resources']['state'] == 'error'


def test_configuration_is_not_a_model_connection_or_notification_delivery_test(tmp_path):
    result = check(tmp_path, ai={'configured': True, 'api_key': 'private-key', 'base_url': 'private-url'},
                   notifications=True, protection={'phase': 'on'})
    by_key = {row['key']: row for row in result['items']}
    assert all(by_key[key]['state'] == 'pending' for key in ('ai', 'notifications', 'security'))
    assert 'private-' not in json.dumps(result)


@pytest.mark.parametrize('checked,error,state', [(NOW, '', 'pending'), (NOW - 500, '', 'pending'), (NOW, 'private upstream error', 'error')])
def test_advancing_source_cursor_does_not_prove_real_request_collection(tmp_path, checked, error, state):
    seed(tmp_path, 'request-history/history.sqlite3', 'CREATE TABLE sources(checked REAL,error TEXT);',
         'INSERT INTO sources VALUES (?,?)', (checked, error))
    result = items(tmp_path)['requests']
    assert result['state'] == state
    assert 'private upstream' not in json.dumps(result)


def test_corrupt_store_and_missing_dependency_are_visible(tmp_path):
    path = tmp_path / 'performance/server.sqlite3'
    path.parent.mkdir()
    path.write_text('not a database')
    result = items(tmp_path, ssh_available=False, ai={'error': True}, protection={'phase': 'partial'})
    assert result['history']['state'] == 'error'
    assert result['ssh']['state'] == 'setup'
    assert result['ai']['state'] == result['security']['state'] == 'error'
    assert items(tmp_path, linux=False)['security']['state'] == 'unsupported'


def test_database_connections_are_query_only(tmp_path):
    path = performance(tmp_path)
    with setup_checks.database(path) as db, pytest.raises(sqlite3.OperationalError):
        db.execute('DELETE FROM samples')


def test_database_check_budget_interrupts_expensive_reads(tmp_path):
    path = performance(tmp_path)
    with setup_checks.database(path) as db, pytest.raises(sqlite3.OperationalError, match='interrupted'):
        db.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT SUM(x) FROM n').fetchone()


def test_discovered_site_or_configured_target_is_not_external_reachability(tmp_path):
    result = items(tmp_path, discovered_sites=2, website_targets=3)
    assert result['certificates']['state'] == result['monitoring']['state'] == 'pending'


def test_setup_route_requires_authentication_before_reading_any_configuration(monkeypatch):
    calls = []
    handler = agent.Handler.__new__(agent.Handler)
    handler.path = '/setup/checks'
    handler._require_auth_json = lambda: False
    handler._write_json = lambda *args: calls.append(args)
    monkeypatch.setattr(agent, '_setup_payload', lambda: pytest.fail('unauthenticated configuration read'))
    handler.do_GET()
    assert calls == []
    handler._require_auth_json = lambda: True
    monkeypatch.setattr(agent, '_setup_payload', lambda: {'items': []})
    handler.do_GET()
    assert calls == [(200, {'items': []})]


def test_agent_adapter_redacts_model_details_and_does_not_start_protection(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, 'STATE_FILE', tmp_path / 'state.json')
    monkeypatch.setattr(agent, '_system_status_payload', lambda: {})
    monkeypatch.setattr(agent, '_ai_manager', lambda: SimpleNamespace(settings=lambda: {
        'configured': True, 'base_url': 'secret-url', 'api_key': 'secret-key'}))
    monkeypatch.setattr(agent, '_basic_protection_instance', None)
    monkeypatch.setattr(agent, '_basic_protection', lambda: pytest.fail('started protection'))
    result = agent._setup_payload()
    assert 'secret-' not in json.dumps(result)
    assert not tmp_path.joinpath('performance').exists()


@pytest.mark.parametrize('name,content_type', [('setup-checks.js', 'application/javascript'), ('setup-checks.css', 'text/css')])
def test_new_assets_are_served_by_the_production_handler(name, content_type):
    handler = agent.Handler.__new__(agent.Handler)
    handler.path = '/ui/' + name
    handler.wfile = io.BytesIO()
    statuses, headers = [], {}
    handler.send_response = statuses.append
    handler.send_header = headers.__setitem__
    handler.end_headers = lambda: None
    handler._write_json = lambda *_args: pytest.fail('asset not shipped or allowed')
    handler.do_GET()
    assert statuses == [200]
    assert headers['Content-Type'].startswith(content_type)
    assert headers['Cache-Control'] == 'no-store'
    assert handler.wfile.getvalue() == (agent.UI_DIR / name).read_bytes()
