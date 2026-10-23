import csv
import io
import json
import sqlite3
import time
import zipfile
from types import SimpleNamespace
from collections import deque

import pytest

import agent
import server_performance as performance
from server_performance import History, values, METRICS, build
from test_deployment_removal import runtime as runtime, http_server as http_server
from test_request_export import get

NOW = 1_800_000_000


def sample(cpu=20, memory=50):
    return {'cpu_percent': cpu, 'cpu': {'logical_count': 8, 'physical_cores': 4, 'model': 'CPU'},
        'memory': {'percent': memory, 'used_mb': 4096, 'total_mb': 8192, 'swap': {'used_mb': 0, 'total_mb': 0}},
        'load': {'one': 1, 'five': 2, 'fifteen': 3},
        'disk': {'percent': 40, 'used_gb': 40, 'total_gb': 100, 'path': '/', 'inode': {'percent': 10}},
        'network': {'rx_kbps': 120, 'tx_kbps': 80},
        'disk_io': {'available': True, 'iowait_percent': 2, 'devices': [
            {'read_kib_s': 10, 'write_kib_s': 20, 'iops': 10, 'await_ms': 2, 'busy_percent': 20},
            {'read_kib_s': 40, 'write_kib_s': 50, 'iops': 30, 'await_ms': 6, 'busy_percent': 50}]}}


def test_one_second_observations_preserve_peaks_weighted_averages_missing_and_no_io(tmp_path, monkeypatch):
    history = History(tmp_path)
    for index, cpu in enumerate([0, 10, None, 90]):
        history.observe(sample(cpu), NOW + index)
    history.observe(sample(999), NOW + 3.5)  # Same second is not counted twice.
    assert not history.path.exists()
    history.flush(NOW + 4)
    data = history.query(now=NOW + 4)
    stats = data['summary']['cpu_percent']
    assert stats['average'] == pytest.approx(100 / 3) and stats['peak'] == 90
    assert stats['minimum'] == 0 and stats['count'] == 3 and stats['high'] == 1
    assert data['sample_seconds'] == 4 and data['coverage_seconds'] == 4
    assert data['summary']['swap_percent'] == {} and data['summary']['disk_await_ms']['average'] == 5
    assert data['summary']['disk_read_kib_s']['average'] == 50
    assert data['summary']['disk_busy_percent']['peak'] == 50 and data['metadata']['logical_cores'] == 8
    # Public JSON never contains NaN or Infinity.
    history.observe(sample(float('nan'), float('inf')), NOW + 5)
    history.flush(NOW + 6, force=True)
    json.dumps(history.query(now=NOW + 6), allow_nan=False)


def test_interval_flush_restarts_gaps_retention_and_coverage_without_erasing(tmp_path):
    first = History(tmp_path)
    for index in range(40):
        first.observe(sample(10), NOW + index)
    first.flush(NOW + 40)
    restarted = History(tmp_path)
    for index in range(41, 45):
        restarted.observe(sample(90), NOW + index)
    restarted.flush(NOW + 45)
    data = History(tmp_path).query(now=NOW + 45)
    assert data['sample_seconds'] == 44 and data['session_count'] == 2 and len(data['points']) == 3
    assert data['summary']['cpu_percent']['average'] == pytest.approx((40 * 10 + 4 * 90) / 44)
    assert data['coverage_seconds'] == 44
    restarted.observe(sample(70), NOW + 600)
    restarted.flush(NOW + 600)
    points = restarted.query(now=NOW + 600)['points']
    assert points[-1]['first'] - points[-2]['last'] > 500
    assert restarted.query(now=NOW + 1000)['stale']
    restarted.observe(sample(30), NOW + 8 * 86400)
    restarted.flush(NOW + 8 * 86400)
    assert restarted.query(now=NOW + 8 * 86400)['sample_seconds'] == 1
    assert len(restarted.pending) <= 1


def test_same_second_restart_coverage_deduplicates_and_periods_validate(tmp_path):
    first, second = History(tmp_path), History(tmp_path)
    first.observe(sample(), NOW)
    first.flush(NOW)
    second.observe(sample(40), NOW)
    second.flush(NOW)
    data = second.query(now=NOW + 1)
    assert data['sample_seconds'] == 2 and data['coverage_seconds'] == 1
    for period in ('1h', '24h', '7d'):
        assert second.query(period, NOW + 1)['points']
    with pytest.raises(ValueError):
        second.query('../data')


def test_failed_database_keeps_pending_and_recovers_without_double_count(tmp_path, monkeypatch):
    history = History(tmp_path)
    history.observe(sample(), NOW)
    original = history.connect
    monkeypatch.setattr(history, 'connect', lambda: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        history.flush(NOW)
    assert history.pending and history.error
    history.observe(sample(100), NOW + 1)
    monkeypatch.setattr(history, 'connect', original)
    history.flush(NOW + 2)
    assert not history.error
    history.flush(NOW + 32)
    data = history.query(now=NOW + 32)
    assert data['sample_seconds'] == 2 and data['summary']['cpu_percent']['average'] == 60


def fill_history_to_limit(history):
    """An actual SQLITE_FULL, with unexpired rows and no dependency on disk capacity."""
    start = NOW - 6 * 86400
    history.observe(sample(), start)
    history.record_context({'at': start, 'available': True, 'warnings': [], 'processes': []}, sample())
    history.flush(start + 30)
    with history.connect() as db:
        saved = tuple(db.execute('SELECT * FROM samples').fetchone())
        context = tuple(db.execute('SELECT * FROM process_context').fetchone())
        for index in range(1, 10000):
            at = start + index * 30
            try:
                db.execute('INSERT INTO samples VALUES(?,?,?,?,?,?,?)',
                           (saved[0], at, at, at, *saved[4:]))
                db.execute('INSERT INTO process_context VALUES(?,?,?,?,?,?)',
                           (context[0], at, at, *context[3:]))
                db.commit()
            except sqlite3.OperationalError as exc:
                assert exc.sqlite_errorcode == sqlite3.SQLITE_FULL
                db.rollback()
                return
        pytest.fail('fixture did not reach the real SQLite page limit')


def test_full_database_reuses_pages_and_saves_backlog_without_recreating_file(tmp_path, monkeypatch):
    monkeypatch.setattr(performance, 'DATABASE_PAGES', 512)
    monkeypatch.setattr(performance, 'WRITE_BATCH_SIZE', 8)
    history = History(tmp_path)
    fill_history_to_limit(history)
    inode = history.path.stat().st_ino
    business = tmp_path / 'business.txt'
    business.write_text('preserve')
    with history.connect() as db:
        before = db.execute('SELECT COUNT(*) FROM samples').fetchone()[0]
        assert db.execute('PRAGMA freelist_count').fetchone()[0] == 0
    # Reopening an old full database must recover without resetting it.
    history = History(tmp_path)
    for index in range(40):
        history.observe(sample(index), NOW + index * 30)
    history.flush(NOW + 1200)
    data = history.query('1h', NOW + 1200)
    assert not history.error and not history.pending
    assert data['sample_seconds'] == 40 and len(data['points']) == 40
    assert data['summary']['cpu_percent']['average'] == 19.5
    with history.connect() as db:
        assert 0 < db.execute('SELECT COUNT(*) FROM samples WHERE first < ?', (NOW,)).fetchone()[0] < before
        assert db.execute('PRAGMA page_count').fetchone()[0] <= 512
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert history.path.stat().st_ino == inode and business.read_text() == 'preserve'
    # Further writes and restart continue from the retained data.
    history = History(tmp_path)
    history.observe(sample(99), NOW + 1230)
    history.flush(NOW + 1231)
    assert history.query('1h', NOW + 1231)['sample_seconds'] == 41


def test_partial_backlog_commit_retains_only_unwritten_samples_for_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(performance, 'WRITE_BATCH_SIZE', 2)
    history = History(tmp_path)
    for index in range(5):
        history.observe(sample(index), NOW + index * 30)
    original, calls = history._prepare_write, []

    def prepare(*args):
        calls.append(True)
        if len(calls) == 2:
            raise OSError(28, 'No space left on device')
        return original(*args)

    monkeypatch.setattr(history, '_prepare_write', prepare)
    with pytest.raises(OSError):
        history.flush(NOW + 150)
    assert len(history.pending) == 3 and '空间不足' in history.error
    assert history.query('1h', NOW + 150)['sample_seconds'] == 2
    monkeypatch.setattr(history, '_prepare_write', original)
    history.flush(NOW + 151)
    assert not history.error and not history.pending
    assert history.query('1h', NOW + 151)['sample_seconds'] == 5


def test_continuous_sampling_rotates_by_total_pages_including_summaries(tmp_path, monkeypatch):
    monkeypatch.setattr(performance, 'DATABASE_PAGES', 512)
    monkeypatch.setattr(performance, 'WRITE_BATCH_SIZE', 8)
    history = History(tmp_path)
    for index in range(800):
        at = NOW + index * 30
        history.observe(sample(index % 100), at)
        history.record_context({'at': at, 'available': True, 'warnings': [], 'processes': []}, sample())
        if index % 8 == 7:
            history.flush(at + 1)
    with history.connect() as db:
        assert 0 < db.execute('SELECT COUNT(*) FROM samples').fetchone()[0] < 800
        assert db.execute('SELECT MAX(last) FROM samples').fetchone()[0] == at
        assert db.execute('SELECT MAX(at) FROM process_context').fetchone()[0] == at
        assert db.execute('SELECT SUM(length(detail)) FROM process_context').fetchone()[0] < performance.EVIDENCE_BUDGET
        assert db.execute('PRAGMA page_count').fetchone()[0] <= 512
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    data = history.query('1h', at + 1)
    assert len(data['points']) == 120 and len(data['contexts']) == 120
    assert history.context(history.run, at)['at'] == at
    assert not history.error


def test_empty_recent_window_reports_stalled_writer_and_last_saved_time(tmp_path):
    history = History(tmp_path)
    history.observe(sample(), NOW)
    history.flush(NOW + 30)
    data = history.query('1h', NOW + 7200)
    assert not data['points'] and data['stale'] and data['last_saved_at']
    assert not History(tmp_path / 'new').query('1h', NOW)['stale']


def test_disk_latency_weighting_and_missing_devices():
    result = values(sample())
    assert result['disk_await_ms'] == 5 and result['disk_iops'] == 40
    assert result['disk_write_kib_s'] == 70
    assert values({'disk_io': {'available': False, 'devices': []}})['disk_read_kib_s'] is None


def test_report_csv_zip_and_database_preservation(tmp_path):
    history = History(tmp_path)
    for second in range(65):
        history.observe(sample(second), NOW + second)
    history.flush(NOW + 65)
    data = history.query(now=NOW + 65)
    with history.connect() as db:
        before = db.execute('SELECT COUNT(*) FROM samples').fetchone()[0]
    for format in ('html', 'csv', 'zip'):
        output, length, mime = build(data, format)
        with output:
            raw = output.read()
        assert len(raw) == length and length > 0
        if format == 'zip':
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                records = list(csv.DictReader(io.StringIO(archive.read('performance.csv').decode('utf-8-sig'))))
                summary = list(csv.DictReader(io.StringIO(archive.read('summary.csv').decode('utf-8-sig'))))
                assert len(records) == 3 and len(summary) == len(METRICS)
                assert sum(int(row['采样秒数']) for row in records) == 65
                assert records[0]['CPU峰值 (%)'] == '29'
                assert records[0]['CPU有效采样数'] == '30'
                assert float(records[0]['磁盘延迟 IOPS 权重（用于加权平均）']) == 1200
                assert 'report.html' in archive.namelist() and '说明.txt' in archive.namelist()
                assert mime == 'application/zip'
        elif format == 'csv':
            assert raw.startswith(b'\xef\xbb\xbf') and len(list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))) == 3
    with history.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM samples').fetchone()[0] == before


def test_network_endpoints_require_auth_and_export_boundaries(http_server, runtime, monkeypatch):
    history = History(runtime)
    monkeypatch.setattr(agent, '_server_performance', lambda: history)
    for path in ('/performance/server', '/performance/server/export'):
        assert get(http_server, path, False)[0] == 401
    assert http_server('GET', '/performance/server')[1]['sample_seconds'] == 0
    assert get(http_server, '/performance/server/export?period=oops')[0] == 400
    assert get(http_server, '/performance/server/export?format=oops')[0] == 400
    now = time.time()
    history.observe(sample(), now)
    history.flush(now)
    status, fields, raw = get(http_server, '/performance/server/export')
    assert status == 200 and fields['X-Performance-Samples'] == '1' and fields['Cache-Control'] == 'no-store'
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert len(list(csv.DictReader(io.StringIO(archive.read('performance.csv').decode('utf-8-sig'))))) == 1
    monkeypatch.setattr(history, 'query', lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError('full')))
    assert get(http_server, '/performance/server/export')[0] == 503


def test_database_symlink_and_hardlink_are_not_opened(tmp_path):
    history = History(tmp_path)
    history.path.parent.mkdir(parents=True)
    other = tmp_path / 'business.db'
    other.write_bytes(b'keep')
    try:
        history.path.hardlink_to(other)
    except OSError:
        pytest.skip('hard links unavailable')
    with pytest.raises(ValueError):
        history.query(now=NOW)
    assert other.read_bytes() == b'keep'


def test_agent_pipeline_samples_and_saves_without_any_page_requests(tmp_path, monkeypatch):
    history = History(tmp_path)
    monkeypatch.setattr(agent, '_server_performance', lambda: history)
    monkeypatch.setattr(agent, '_realtime_metrics', deque(maxlen=60))
    monkeypatch.setattr(agent, '_realtime_server', {})
    current, clock = sample(), [NOW]
    monkeypatch.setattr(agent.time, 'time', lambda: clock[0])
    monkeypatch.setattr(agent, '_cpu_percent', lambda: current['cpu_percent'])
    monkeypatch.setattr(agent, '_cpu_metrics', SimpleNamespace(snapshot=current['cpu']))
    monkeypatch.setattr(agent, '_memory_status', lambda: current['memory'])
    monkeypatch.setattr(agent, '_load_average', lambda: current['load'])
    monkeypatch.setattr(agent, '_disk_status', lambda *args: current['disk'])
    monkeypatch.setattr(agent, '_network_status', lambda: current['network'])
    monkeypatch.setattr(agent, '_disk_sampler', SimpleNamespace(sample=lambda: current['disk_io']))
    monkeypatch.setattr(agent, '_system_status_payload', lambda: pytest.fail('writer must not request status'))
    monkeypatch.setattr(agent, '_write_state', lambda: pytest.fail('1s sampling must not write JSON state'))
    class Stop:
        count = 0
        def wait(self, seconds):
            assert seconds == 5
            self.count += 1
            if self.count == 3:
                return True
            for _ in range(30):
                agent._sample_realtime_metrics()
                clock[0] += 1
            return False
    agent._server_performance_sampler(Stop())
    data = history.query(now=clock[0])
    assert data['sample_seconds'] == 60 and len(data['points']) == 2
    assert len(agent._realtime_metrics) == 60
