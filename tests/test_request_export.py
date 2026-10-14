import base64
import csv
import io
import json
import re
import time
import zipfile

import pytest

import agent
import request_export as export
import request_history
from test_deployment_removal import http_server as http_server, runtime as runtime
from test_request_history import NOW, line


def ingest(history, lines, key='api', name='API', now=NOW):
    history.ingest(key, name, 'container', lines, now, now=now)


def table(raw):
    return list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))


def test_export_all_sources_all_pages_and_rows_with_consistent_snapshot(tmp_path):
    history = request_history.History(tmp_path)
    ingest(history, [line(f'/route-{n}') for n in range(110)] + [line('/busy') for _ in range(1001)])
    ingest(history, [line('/old', ts=NOW - 2 * 86400)], key='web', name='网站')
    stream, meta = export.snapshot(history, {}, now=NOW)
    try:
        assert meta['count'] == 1112 and len(meta['sources']) == 2
        ingest(history, [line('/new')], key='web', name='网站')
        out, size, mime = export.build(stream, meta)
        with out, zipfile.ZipFile(out) as archive:
            requests = table(archive.read('requests.csv'))
            routes = table(archive.read('routes.csv'))
            assert len(requests) == 1112 and len(routes) == 112
            assert len({r['记录 ID'] for r in requests}) == 1112
            assert all(r['原始路径'] != '/new' for r in requests)
            assert archive.read('requests.csv').startswith(b'\xef\xbb\xbf')
            assert 'report.html' in archive.namelist() and 'timeline.csv' in archive.namelist()
            assert sum(int(r['请求次数']) for r in routes) == len(requests)
            assert size > 0 and mime == 'application/zip'
            encoded = re.search(r'id="reportData" type="application/json">(.*?)</script>', archive.read('report.html').decode()).group(1)
            assert any(source['name'] == '网站' for source in json.loads(base64.b64decode(encoded))['sources'])
    finally:
        stream.close()


@pytest.mark.parametrize('layout,status,search', [('tree', 'all', ''), ('flat', 'all', ''),
    ('tree', 'error', ''), ('tree', 'slow', ''), ('tree', 'success', ''), ('tree', 'all', '/users/2')])
def test_export_filtered_whole_groups_match_ui_statistics(tmp_path, layout, status, search):
    history = request_history.History(tmp_path)
    ingest(history, [line('/users/1'), line('/users/2', status=500, duration='1.5'), line('/okay'),
                     line('/old', ts=NOW - 7200)])
    options = {'key': 'api', 'period': '1h', 'layout': layout, 'status': status, 'search': search}
    selected = history.query('api', now=NOW, period='1h', layout=layout, status=status, search=search)
    stream, meta = export.snapshot(history, options, now=NOW)
    with stream:
        analysis = export.analyze(stream, meta)
    assert meta['count'] == selected['total_requests']
    assert len(analysis['groups']) == selected['total_groups']
    assert analysis['totals']['count'] == sum(g['count'] for g in selected['groups'])


def test_percentiles_weighted_means_missing_and_zero_values(tmp_path):
    items = [{'at': '2026-10-14T00:00:00Z', 'host': 'api.test', 'method': 'GET', 'path': '/api/1',
              'status': 200, 'duration_ms': value, 'upstream_ms': None} for value in [None, 0, 10, 20, 1000, 5000]]
    items.append({**items[-1], 'path': '/api/2', 'status': 500, 'duration_ms': 20, 'upstream_ms': 10})
    stream, meta = export.samples(items, {'scope': '样本', 'notice': 'sample', 'filters': {'layout': 'tree', 'slow_ms': 1000}})
    with stream:
        result = export.analyze(stream, meta)
    stats = result['totals']
    assert stats['count'] == 7 and stats['duration_count'] == 6 and stats['average'] == pytest.approx(6050 / 6)
    assert stats['p95'] == 5000 and stats['p99'] == 5000 and stats['slow_count'] == 2
    assert stats['upstream_average'] == 10 and stats['errors'] == 1
    assert result['groups'][0]['path_count'] == 2 and stats['response_bytes'] is None
    assert sum(p['count'] for p in result['timeline']) == 7


def test_csv_formula_protection_report_text_safety_and_credential_allowlist(tmp_path):
    items = [{'at': '2026-10-14T00:00:00Z', 'host': '=HYPERLINK("x")', 'path': '/<script>alert(1)</script>?token=private',
        'method': 'GET', 'status': 200, 'duration_ms': 10, 'request_headers': {'Authorization': 'secret', 'Cookie': 'private',
        'User-Agent': 'browser'}, 'api_key': 'private', 'request_body': 'private', 'source_name': '+bad',
        'upstream_address': '@bad'}]
    stream, meta = export.samples(items, {'scope': '<script>alert(1)</script>', 'notice': 'sample'})
    with stream:
        out, _, _ = export.build(stream, meta, 'csv')
        with out:
            raw = out.read()
        row = table(raw)[0]
        assert row['域名'].startswith("'=") and row['来源名称'] == "'+bad" and row['上游地址'] == "'@bad"
        assert json.loads(row['常用请求头 (JSON)']) == {'User-Agent': 'browser'}
        assert b'secret' not in raw and b'private' not in raw
        report = export.report(export.analyze(stream, meta)).decode()
        assert '<script>alert(1)</script>' not in report and 'Content-Security-Policy' in report
        encoded = re.search(r'id="reportData" type="application/json">(.*?)</script>', report).group(1)
        assert json.loads(base64.b64decode(encoded))['scope'] == '<script>alert(1)</script>'
    for text in ['=SUM(A1)', ' +cmd', '\tvalue', '\n=foo', '@cmd', '-cmd']:
        assert export.safe_cell(text).startswith("'")
    assert export.safe_cell(10.5) == 10.5 and export.safe_cell(None) == ''


def test_snapshot_does_not_prune_or_mutate_and_includes_health_warnings(tmp_path):
    history = request_history.History(tmp_path)
    ingest(history, [line('/api'), line('/old', ts=NOW - 3 * 86400)])
    with history.connect() as db:
        db.execute("UPDATE sources SET error='暂不可用',limited=?,checked=?", (NOW - 10, NOW - 100))
        before = db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]
    stream, meta = export.snapshot(history, {'key': 'api', 'period': '1h'}, now=NOW)
    stream.close()
    assert meta['count'] == 1 and len(meta['warnings']) == 3
    with history.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM requests').fetchone()[0] == before


def get(http_server, path, authenticated=True):
    import http.client
    client = http.client.HTTPConnection(http_server.origin.removeprefix('http://'), timeout=15)
    headers = {'Cookie': f'{agent.COOKIE_NAME}={http_server.cookie}'} if authenticated else {}
    client.request('GET', path, headers=headers)
    response = client.getresponse()
    raw = response.read()
    fields = dict(response.getheaders())
    status = response.status
    client.close()
    return status, fields, raw


def test_http_export_auth_validation_samples_and_empty_history(http_server, runtime, monkeypatch):
    assert get(http_server, '/request-export', False)[0] == 401
    for query in ['backend=unknown', 'scope=bad', 'format=xlsx', 'slow_ms=nan', 'scope=source', 'scope=filtered&key=api&period=invalid']:
        assert get(http_server, '/request-export?' + query)[0] == 400
    status, fields, raw = get(http_server, '/request-export?format=csv')
    assert status == 200 and fields['X-Request-Export-Count'] == '0' and not table(raw)
    assert fields['Cache-Control'] == 'no-store' and 'attachment' in fields['Content-Disposition']
    deadline = time.monotonic() + 2
    while not export.SLOT.acquire(blocking=False):
        assert time.monotonic() < deadline
        time.sleep(.01)
    try:
        assert get(http_server, '/request-export')[0] == 409
    finally:
        export.SLOT.release()
    item = {'at': '2026-10-14T00:00:00Z', 'path': '/api', 'host': 'demo', 'method': 'GET', 'status': 200, 'duration_ms': 12}
    monkeypatch.setattr(agent.caddy_requests, 'recent', lambda *args: {'records': [item], 'container': 'caddy', 'notice': '近期样本'})
    assert get(http_server, '/request-export?backend=caddy')[0] == 400
    status, fields, raw = get(http_server, '/request-export?backend=caddy&scope=source&format=zip')
    assert status == 200 and fields['X-Request-Export-Count'] == '1'
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert '近期日志样本' in archive.read('说明.txt').decode()
        assert len(table(archive.read('requests.csv'))) == 1
    monkeypatch.setattr(agent._request_history(), 'connect', lambda: (_ for _ in ()).throw(OSError('full')))
    assert get(http_server, '/request-export')[0] == 503
    deadline = time.monotonic() + 2
    while not export.SLOT.acquire(blocking=False):
        assert time.monotonic() < deadline
        time.sleep(.01)
    export.SLOT.release()
