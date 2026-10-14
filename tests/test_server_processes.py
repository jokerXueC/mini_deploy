import csv
import io
import sqlite3
import zipfile

import pytest

import agent
import server_performance as performance
from server_processes import Sampler
from test_server_performance import sample, NOW
from test_deployment_removal import runtime as runtime, http_server as http_server
from test_request_export import get


def process(proc, pid=20, ticks=100, identity=200, read=1000, write=2000, name='python (worker)'):
    path = proc / str(pid)
    path.mkdir(exist_ok=True)
    fields = ['0'] * 30
    for index, value in {0: 'R', 1: 1, 11: ticks, 12: 0, 17: 6, 19: identity, 21: 100}.items():
        fields[index] = str(value)
    (path / 'stat').write_text(f'{pid} ({name}) ' + ' '.join(fields))
    (path / 'status').write_text('Uid:\t1000 1000 1000 1000\n')
    (path / 'io').write_text(f'read_bytes: {read}\nwrite_bytes: {write}\n')
    (path / 'cgroup').write_text('0::/system.slice/docker-' + 'a' * 64 + '.scope\n')
    return path


def scene(at=NOW, cpu=10):
    return {'at': at, 'available': True, 'scanned': 1, 'readable': 1, 'warnings': [],
        'processes': [{'pid': 20, 'name': '=sensitive formula', 'cpu_percent': cpu, 'rss_mib': 20,
                       'write_kib_s': 10, 'read_kib_s': 5, 'user': 'root', 'state': 'R', 'threads': 2}]}


def test_counter_deltas_container_identity_and_pid_reuse(tmp_path):
    (tmp_path / 'stat').write_text('cpu 0')
    path = process(tmp_path)
    clock = [10.0]
    sampler = Sampler(tmp_path, clock=lambda: clock[0])
    first = sampler.sample(sample(), now=NOW)
    row = first['processes'][0]
    assert row['name'] == 'python (worker)' and row['cpu_percent'] is None and row['read_kib_s'] is None
    assert row['container_id'] == 'a' * 64 and row['ppid'] == 1 and row['threads'] == 6
    clock[0] += 5
    process(tmp_path, ticks=100 + sampler.ticks * 10, read=1000 + 5120, write=2000 + 10240)
    second = sampler.sample(sample(), {'containers': [{'id': 'a' * 64, 'name': 'api', 'state': 'running'}]}, NOW + 5)
    row = second['processes'][0]
    assert row['cpu_percent'] == 200 and row['host_cpu_percent'] == 25
    assert row['read_kib_s'] == 1 and row['write_kib_s'] == 2
    assert row['container'] == 'api' and row['container_state'] == 'running'
    assert row['interval_seconds'] == 5 and row['uid'] == 1000
    assert row['rss_change_mib'] == 0 and row['cpu_change'] is None
    clock[0] += 5
    process(tmp_path, ticks=100 + sampler.ticks * 25, read=1000 + 10240, write=2000 + 20480)
    growing = sampler.sample(sample(), now=NOW + 10)['processes'][0]
    assert growing['cpu_percent'] == 300 and growing['cpu_change'] == 100
    # Process reuse must establish a fresh baseline, not compare with its previous owner.
    clock[0] += 5
    process(tmp_path, ticks=1000000, identity=300)
    third = sampler.sample(sample(), now=NOW + 15)['processes'][0]
    assert third['cpu_percent'] is None and third['read_kib_s'] is None
    assert not any(key in third for key in ('cmdline', 'environ', 'arguments'))
    assert not (path / 'cmdline').exists()


def test_missing_io_permissions_scan_limit_and_exited_process(tmp_path, monkeypatch):
    (tmp_path / 'stat').write_text('cpu 0')
    process(tmp_path, pid=20)
    process(tmp_path, pid=30)
    sampler = Sampler(tmp_path, limit=1)
    original = sampler.read
    def read(path):
        if path.name == 'io':
            raise PermissionError('denied')
        return original(path)
    monkeypatch.setattr(sampler, 'read', read)
    result = sampler.sample(sample(), now=NOW)
    assert result['limited'] and result['scanned'] == 1 and result['warnings']
    assert result['processes'][0]['write_kib_s'] is None
    process(tmp_path, pid=20, name='gone')
    def disappeared(path):
        if path.name == 'stat':
            raise FileNotFoundError()
        return original(path)
    monkeypatch.setattr(sampler, 'read', disappeared)
    result = sampler.sample(sample(), now=NOW + 5)
    assert not result['processes'] and not sampler.previous
    assert Sampler(tmp_path / 'absent').sample(sample())['available'] is False


def test_scene_persistence_selection_restart_and_failure_preserve(tmp_path, monkeypatch):
    history = performance.History(tmp_path)
    history.observe(sample(), NOW)
    history.record_context(scene(), sample())
    history.record_context(scene(NOW + 5, 80), sample(90))
    history.record_context(scene(NOW + 10, 10), sample(10))
    assert not history.path.exists()
    history.flush(NOW + 11)
    data = history.query(now=NOW + 11)
    assert len(data['contexts']) == 1
    assert data['contexts'][0]['at'] == NOW + 5
    saved = history.context(history.run, NOW)
    assert saved['processes'][0]['cpu_percent'] == 80 and 'CPU 突升' in saved['reasons']
    # A later flush in the same bucket cannot replace its higher-pressure scene.
    history.record_context(scene(NOW + 15), sample())
    history.flush(NOW + 16, force=True)
    assert history.context(history.run, NOW)['at'] == NOW + 5
    restarted = performance.History(tmp_path)
    restarted.record_context(scene(NOW + 20), sample())
    restarted.flush(NOW + 21, force=True)
    assert len(restarted.query(now=NOW + 21)['contexts']) == 2
    original = restarted.connect
    monkeypatch.setattr(restarted, 'connect', lambda: (_ for _ in ()).throw(sqlite3.OperationalError('full')))
    restarted.record_context(scene(NOW + 35), sample())
    with pytest.raises(sqlite3.OperationalError):
        restarted.flush(NOW + 36, force=True)
    assert restarted.contexts and restarted.error
    monkeypatch.setattr(restarted, 'connect', original)
    restarted.flush(NOW + 37, force=True)
    assert len(restarted.query(now=NOW + 37)['contexts']) == 3


def test_full_export_includes_scenes_beyond_embedded_limit_and_safe_csv(tmp_path):
    history = performance.History(tmp_path)
    for i in range(310):
        history.observe(sample(), NOW + i * 30)
        history.record_context(scene(NOW + i * 30), sample())
    history.flush(NOW + 9300)
    data = history.query(now=NOW + 9300)
    assert len(data['contexts']) == 310 and len(data['context_details']) <= 240
    output, _, _ = performance.build(data, history=history)
    with output, zipfile.ZipFile(output) as archive:
        rows = list(csv.DictReader(io.StringIO(archive.read('processes.csv').decode('utf-8-sig'))))
        scenes = list(csv.DictReader(io.StringIO(archive.read('scenes.csv').decode('utf-8-sig'))))
        assert len(rows) == len(scenes) == 310
        assert rows[0]['进程'] == "'=sensitive formula"
        assert len({row['区间'] for row in rows}) == 310


def test_evidence_budget_prunes_only_internal_rows_and_legacy_statistics_survive(tmp_path, monkeypatch):
    history = performance.History(tmp_path)
    history.observe(sample(), NOW)
    history.flush(NOW)
    business = tmp_path / 'business.txt'
    business.write_text('preserve')
    monkeypatch.setattr(performance, 'EVIDENCE_BUDGET', 1500)
    for i in range(12):
        history.record_context(scene(NOW + i * 30), sample())
        history.flush(NOW + i * 30, force=True)
    with history.connect() as db:
        assert db.execute('SELECT SUM(length(detail)) FROM process_context').fetchone()[0] <= 1500
    data = history.query(now=NOW + 400)
    assert len(data['contexts']) < 12 and data['sample_seconds'] == 1
    assert business.read_text() == 'preserve'


def test_context_endpoint_authenticated_and_missing_are_explicit(http_server, runtime, monkeypatch):
    history = performance.History(runtime)
    monkeypatch.setattr(agent, '_server_performance', lambda: history)
    history.record_context(scene(), sample())
    history.flush(NOW, force=True)
    path = f'/performance/server/context?run={history.run}&ts={NOW}'
    assert get(http_server, path, False)[0] == 401
    assert get(http_server, path)[0] == 200
    assert get(http_server, path + 'bad')[0] == 400
    assert get(http_server, f'/performance/server/context?run={history.run}&ts={NOW + 30}')[0] == 404
    assert get(http_server, '/performance/server/context?run=../../&ts=0')[0] == 400


def test_evidence_batch_larger_than_budget_keeps_recent_scenes(tmp_path, monkeypatch):
    monkeypatch.setattr(performance, 'EVIDENCE_BUDGET', 1500)
    history = performance.History(tmp_path)
    for i in range(20):
        history.record_context(scene(NOW + i * 30), sample())
    history.flush(NOW + 600, force=True)
    with history.connect() as db:
        assert db.execute('SELECT SUM(length(detail)) FROM process_context').fetchone()[0] <= 1500
    assert history.query(now=NOW + 600)['contexts'][0]['at'] == NOW + 19 * 30
