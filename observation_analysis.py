"""Read retained observations in aligned windows without collecting new host data."""
import json
import math
import re
import sqlite3
import time
import zlib

from request_history import History as RequestHistory
from request_gateway import KEY as SOURCE_KEY

RETENTION = 7 * 86400
FIELDS = ('cpu_percent', 'memory_percent', 'network_rx_kib_s', 'network_tx_kib_s')


def window(since, until, now=None):
    now = time.time() if now is None else now
    since, until = float(since), float(until)
    if (not all(math.isfinite(v) for v in (since, until)) or since < 0
            or since >= until or until - since > RETENTION or until > now + 60):
        raise ValueError('请选择不超过 7 天的有效时间区间')
    # Requests are retained as minute rollups. All linked views use the same boundaries.
    end = math.ceil(until / 60) * 60
    return max(math.floor(since / 60) * 60, end - RETENTION), end


def change(current, reference):
    if current is None or reference is None:
        return {'delta': None, 'percent': None}
    return {'delta': current - reference,
            'percent': (current - reference) / abs(reference) * 100 if reference else None}


def resources(history, since, until, fields=FIELDS):
    totals = {key: {'sum': 0, 'weight': 0, 'peak': None} for key in fields}
    covered, count = {}, 0
    with history.connect() as db:
        for row in db.execute('SELECT ts,data FROM samples WHERE ts>=? AND ts<? ORDER BY ts', (since, until)):
            count += 1
            data = json.loads(row['data'])
            covered[row['ts']] = covered.get(row['ts'], 0) | data.get('_seconds', 0)
            for key in fields:
                value = data.get(key) or {}
                if not value.get('weight'):
                    continue
                total = totals[key]
                total['sum'] += value['sum']
                total['weight'] += value['weight']
                total['peak'] = max(total['peak'] if total['peak'] is not None else value['peak'], value['peak'])
    seconds = sum(mask.bit_count() for mask in covered.values())
    metrics = {key: {'average': value['sum'] / value['weight'] if value['weight'] else None,
                     'peak': value['peak'], 'observations': value['weight']} for key, value in totals.items()}
    return {'since': since, 'until': until, 'statistics': count, 'coverage_seconds': seconds,
            'coverage_percent': min(100, seconds / (until - since) * 100), 'metrics': metrics,
            'error': history.error}


def containers(history, since, until, ref_since, ref_until, target=''):
    edge = min(300, max(30, (until - since) / 4))
    selected = history.aggregate(since, until, target)
    reference = {row['container']: row for row in history.aggregate(ref_since, ref_until, target)}
    for item in selected:
        baseline = reference.get(item['container'], {})
        item['reference'] = baseline
        item['cpu_change'] = change(item['cpu'], baseline.get('cpu'))
        item['memory_change'] = change(item['memory'], baseline.get('memory'))
        item['cpu_growth'] = change(item.pop('end_cpu'), item.pop('start_cpu'))['delta']
        item['memory_growth'] = change(item.pop('end_memory'), item.pop('start_memory'))['delta']
        item['coverage_percent'] = min(100, item['samples'] * 30 / (until - since) * 100)
    return selected


def processes(history, since, until, target=''):
    with history.connect() as db:
        rows = [db.execute('SELECT run,at,detail FROM process_context WHERE at>=? AND at<? '
                           f'ORDER BY at {order},run {order},ts {order} LIMIT 1', (since, until)).fetchone()
                for order in ('ASC', 'DESC')]
    if not all(rows):
        return {'items': [], 'notice': '所选时段没有保留的进程现场。'}
    first, last = [json.loads(zlib.decompress(row['detail'])) for row in rows]
    if first['at'] == last['at']:
        return {'items': [], 'notice': '只有一次进程现场，无法计算区间变化。'}
    def identity(scene, process):
        return scene['run'], process.get('pid'), process.get('start_ticks')
    starts = {identity(first, p): p for p in first.get('processes', []) if p.get('start_ticks') is not None}
    items = []
    for process in last.get('processes', []):
        if target and process.get('container_id') != target:
            continue
        previous = starts.get(identity(last, process)) if process.get('start_ticks') is not None else None
        item = {key: process.get(key) for key in ('pid', 'name', 'service', 'container', 'container_id',
                'host_cpu_percent', 'rss_mib')}
        item.update(comparable=previous is not None,
                    cpu_growth=change(process.get('host_cpu_percent'), (previous or {}).get('host_cpu_percent'))['delta'],
                    memory_growth=change(process.get('rss_mib'), (previous or {}).get('rss_mib'))['delta'])
        items.append(item)
    return {'items': items, 'first_at': first['at'], 'last_at': last['at'],
            'notice': '比较区间首末保留现场；仅匹配同一采集批次、PID 和启动标识。现场只保存部分活跃进程；未匹配不代表新增，排名不等于根因。',
            'warnings': list(dict.fromkeys(first.get('warnings', []) + last.get('warnings', [])))}


def requests(history, since, until, source='', watches=()):
    if source and not SOURCE_KEY.fullmatch(source):
        raise ValueError('请求来源无效')
    if not isinstance(watches, list) and watches != ():
        raise ValueError('关注接口无效')
    if len(watches) > 8 or any(not isinstance(w, list) or len(w) != 4
                             or any(not isinstance(v, str) or len(v) > 2048 for v in w) for w in watches):
        raise ValueError('最多关注 8 个接口')
    groups, histogram = {}, {}
    with history.connect() as db:
        sources = [dict(row) for row in db.execute('SELECT key,name,checked,cursor,error,rollups_since,rollups_limited FROM sources')]
        for row in db.execute('''SELECT source,host,method,route,data FROM request_rollups
                WHERE minute>=? AND minute<? AND (?='' OR source=?)''', (since, until, source, source)):
            key = (row['source'], row['host'], row['method'], row['route'])
            data = json.loads(row['data'])
            item = groups.setdefault(key, {'identity': list(key), 'source': row['source'], 'host': row['host'],
                'method': row['method'], 'path': row['route'], 'count': 0, 'errors': 0, 'server_errors': 0,
                'durationSum': 0, 'durationCount': 0, 'histogram': {}})
            for field in ('count', 'errors', 'server_errors', 'durationSum', 'durationCount'):
                item[field] += data.get(field, 0)
            for bound, count in data.get('histogram', {}).items():
                item['histogram'][bound] = item['histogram'].get(bound, 0) + count
                histogram[bound] = histogram.get(bound, 0) + count
    items = list(groups.values())
    total = sum(item['count'] for item in items)
    duration_count = sum(item['durationCount'] for item in items)
    for item in items:
        item['p95'] = RequestHistory.percentile(item.pop('histogram'), .95) if item['durationCount'] else None
        item['average'] = item['durationSum'] / item['durationCount'] if item['durationCount'] else None
    bins = [{'label': label, 'count': 0} for label in ('≤ 100 ms', '100–300 ms', '300 ms–1 s', '1–3 s', '> 3 s')]
    for bound, count in histogram.items():
        index = next((i for i, upper in enumerate((100, 300, 1000, 3000)) if float(bound) <= upper), 4)
        bins[index]['count'] += count
    selected_sources = [s for s in sources if not source or s['key'] == source]
    return {'since': since, 'until': until, 'source': source, 'available': bool(selected_sources),
            'count': total, 'duration_count': duration_count,
            'errors': sum(item['errors'] for item in items),
            'server_errors': sum(item['server_errors'] for item in items),
            'error_rate': sum(item['errors'] for item in items) / total * 100 if total else None,
            'average': sum(item['durationSum'] for item in items) / duration_count if duration_count else None,
            'p95': RequestHistory.percentile(histogram, .95) if duration_count else None,
            'p99': RequestHistory.percentile(histogram, .99) if duration_count else None,
            'distribution': bins, 'sources': sources,
            'slow_endpoints': sorted((item for item in items if item['p95'] is not None),
                                     key=lambda item: (item['p95'], item['count']), reverse=True)[:15],
            'watched': [{'identity': w, 'result': groups.get(tuple(w))} for w in watches],
            'incomplete': not selected_sources or any(s['error'] or s['cursor'] < until - 60
                or s['rollups_since'] > since or s['rollups_limited'] >= since for s in selected_sources),
            'notice': '仅统计已采集的请求；时间边界按分钟，P95/P99 和分布按直方图估计（桶宽约 10%）。无耗时的请求不进入分布；0 条记录不能证明没有流量。'}


def report(server, docker, traffic, monitor, *, since, until, compare='previous', reference=None,
           target='', source='', now=None):
    since, until = window(since, until, now)
    if target and not re.fullmatch('[0-9a-f]{64}', target):
        raise ValueError('请选择完整容器 ID')
    if compare not in ('previous', 'yesterday', 'custom'):
        raise ValueError('请选择有效对比方式')
    ref_until = since if compare == 'previous' else until - 86400
    if compare == 'custom':
        ref_until = window(float(reference) - (until - since), reference, now)[1]
    ref_since = ref_until - (until - since)
    if ref_since < 0:
        raise ValueError('对比时间无效')
    result = {'since': since, 'until': until, 'reference_since': ref_since, 'reference_until': ref_until,
              'generated_at': time.time(), 'errors': {}}
    # A failed data source must not erase the other sources or masquerade as an empty result.
    for name, read in (
        ('resources', lambda: resources(server, since, until)),
        ('reference', lambda: resources(server, ref_since, ref_until)),
        ('containers', lambda: containers(docker, since, until, ref_since, ref_until, target)),
        ('processes', lambda: processes(server, since, until, target)),
        ('requests', lambda: requests(traffic, since, until, source)),
        ('reference_requests', lambda: requests(traffic, ref_since, ref_until, source)),
        ('events', lambda: [e for e in monitor.snapshot()['events'] if since <= e.get('at_ts', 0) < until
                           and (not target or e.get('metadata', {}).get('container_id') == target)][:20]),
    ):
        try:
            result[name] = read()
        except (OSError, RuntimeError, ValueError, sqlite3.Error, zlib.error):
            result[name] = None
            result['errors'][name] = '该来源暂不可读，请检查采集状态与数据目录；原历史保留。'
    return result
