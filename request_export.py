"""Private snapshots and offline request analysis, without optional runtimes."""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import tempfile
import threading
import time
from datetime import datetime, timezone
from collections import Counter
import zipfile

import nginx_requests
import request_gateway
import request_history

SLOT = threading.BoundedSemaphore(1)
DETAIL_FIELDS = [('record_id', '记录 ID'), ('source', '来源标识'), ('source_name', '来源名称'), ('at', '时间 (UTC)'),
    ('host', '域名'), ('method', '方法'), ('path', '原始路径'), ('route', '归并路径'), ('status', '状态码'),
    ('duration_ms', '请求耗时 (ms)'), ('upstream_ms', '上游耗时 (ms)'), ('client_ip', '来源 IP'),
    ('peer_ip', '连接 IP'), ('protocol', '协议'), ('response_bytes', '响应大小 (bytes)'),
    ('request_id', '请求 ID'), ('request_id_source', '请求 ID 来源'), ('upstream_address', '上游地址'),
    ('request_headers', '常用请求头 (JSON)'), ('upstream_attempts', '上游尝试 (JSON)')]
GROUP_FIELDS = [('source_name', '来源'), ('host', '域名'), ('method', '方法'), ('path', '路径'),
    ('count', '请求次数'), ('successes', '成功次数'), ('errors', '失败次数'), ('success_rate', '成功率 (%)'),
    ('slow_count', '慢请求次数'), ('duration_count', '有效耗时样本'), ('average', '平均耗时 (ms)'),
    ('p95', 'P95 (ms)'), ('p99', 'P99 (ms)'), ('maximum', '最大耗时 (ms)'),
    ('upstream_average', '平均上游 (ms)'), ('response_bytes', '已记录响应大小 (bytes)'), ('path_count', '原始路径数')]


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def numeric(value):
    return value if type(value) in {int, float} and math.isfinite(value) and value >= 0 else None


def safe_cell(value):
    if value is None:
        return ''
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    if isinstance(value, str):
        value = ''.join(c for c in value if c in '\n\t\r' or ord(c) >= 32)
        if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')):
            value = "'" + value
    return value


def record(item):
    """Reapply the metadata allowlist; a report never adds raw credential fields."""
    allowed = {key: item[key] for key, _ in DETAIL_FIELDS if key in item and key not in {'request_headers', 'upstream_attempts'}}
    allowed['path'] = str(allowed.get('path', '')).split('?', 1)[0].split('#', 1)[0]
    allowed['route'] = request_history.normalize_path(allowed['path'])
    metadata = nginx_requests.request_metadata(item)
    allowed.update(metadata)
    attempts = item.get('upstream_attempts')
    if isinstance(attempts, list):
        allowed['upstream_attempts'] = [{k: numeric(attempt.get(k)) for k in ('status', 'connect_ms', 'header_ms', 'response_ms')}
                                      for attempt in attempts if isinstance(attempt, dict)]
    for key in ('duration_ms', 'upstream_ms', 'response_bytes'):
        allowed[key] = numeric(item.get(key))
    return allowed


def validate(options):
    key, period = options.get('key', ''), options.get('period', 'all')
    layout, status, search = options.get('layout', 'tree'), options.get('status', 'all'), options.get('search', '')
    slow = options.get('slow_ms', 1000)
    if (not isinstance(key, str) or key and not request_gateway.KEY.fullmatch(key)
            or period not in {'all', *request_history.PERIODS} or layout not in {'tree', 'flat'}
            or status not in {'all', 'success', 'error', 'slow'} or not isinstance(search, str) or len(search) > 2048
            or type(slow) is not int or not 1 <= slow <= 3_600_000):
        raise ValueError('请求导出范围或筛选参数无效')
    return {'key': key, 'period': period, 'layout': layout, 'status': status, 'search': search, 'slow_ms': slow}


def snapshot(history, options, *, now=None):
    options = validate(options)
    now = time.time() if now is None else now
    since = now - (history.retention if options['period'] == 'all' else min(history.retention, request_history.PERIODS[options['period']]))
    column = 'route' if options['layout'] == 'tree' else 'path'
    conditions, params = ['ts>=?', 'ts<=?'], [since, now]
    if options['key']:
        conditions.append('source=?')
        params.append(options['key'])
    having = ['1']
    if options['search']:
        having.append("""MAX(INSTR(LOWER(host || ' ' || path || ' ' || route || ' ' ||
            COALESCE(json_extract(data,'$.request_id'),'') || ' ' ||
            COALESCE(json_extract(data,'$.client_ip'),'')), ?) > 0)""")
        params.append(options['search'].lower())
    if options['status'] == 'success':
        having.append('SUM(status>=200 AND status<400)=COUNT(*)')
    elif options['status'] == 'error':
        having.append('SUM(status>=400)>0')
    elif options['status'] == 'slow':
        having.append('SUM(duration>=?)>0')
        params.append(options['slow_ms'])
    stream = tempfile.TemporaryFile()
    try:
        with history.connect() as db:
            # Freeze source health, selection and rows in one short read snapshot.
            # File generation and HTTP transfer happen after this lock is released.
            db.execute('BEGIN')
            sources = [dict(row) for row in db.execute('SELECT * FROM sources ORDER BY key')]
            names = {source['key']: source['name'] for source in sources}
            query = f"""WITH selected AS (
                SELECT source,host,method,{column} AS selected_path FROM requests
                WHERE {' AND '.join(conditions)} GROUP BY source,host,method,{column}
                HAVING {' AND '.join(having)})
                SELECT r.id,r.source,r.ts,r.data FROM requests r JOIN selected s ON
                r.source=s.source AND r.host=s.host AND r.method=s.method AND r.{column}=s.selected_path
                WHERE r.ts>=? AND r.ts<=? ORDER BY r.ts,r.id"""
            count, used = 0, set()
            for row in db.execute(query, (*params, since, now)):
                item = record({**json.loads(row['data']), 'record_id': row['id'], 'source': row['source'],
                    'source_name': names.get(row['source'], row['source']), 'at': stamp(row['ts'])})
                item['_ts'] = row['ts']
                stream.write((json.dumps(item, ensure_ascii=False, separators=(',', ':')) + '\n').encode())
                count += 1
                used.add(row['source'])
            rollups = []
            if not options['search'] and options['status'] == 'all':
                for source in sources:
                    if options['key'] and source['key'] != options['key']:
                        continue
                    for group in history.rollups(source['key'], since, now, db=db):
                        used.add(source['key'])
                        rollups.append({**group, 'source_name': source['name'],
                            'duration_count': group['durationCount'],
                            'average': group['durationSum'] / group['durationCount'] if group['durationCount'] else None,
                            'upstream_average': group['upstreamSum'] / group['upstreamCount'] if group['upstreamCount'] else None,
                            'success_rate': group['successes'] / group['count'] * 100,
                            'slow_count': sum(n for bound, n in group['histogram'].items() if float(bound) >= options['slow_ms'])})
                        rollups[-1].pop('histogram', None)
        selected_sources = [s for s in sources if s['key'] == options['key'] or not options['key'] and s['key'] in used]
        warnings = []
        if sum(group['count'] for group in rollups) > count:
            warnings.append('明细已部分过期；保留统计独立保存，次数可能多于明细。P95/P99 和慢请求采用约 10% 桶宽估计，时间边界按分钟。')
        for source in selected_sources:
            name = source['name']
            if source.get('error'):
                warnings.append(f"{name}：{source['error']}")
            if source.get('limited'):
                warnings.append(f'{name}：采集曾达到单批上限，可能存在缺口。')
            if source.get('rollups_limited'):
                warnings.append(f'{name}：保留统计曾达到容量上限，较早部分已过期。')
            if source.get('checked') and now - source['checked'] > 60:
                warnings.append(f'{name}：采集延迟，近期数据可能不完整。')
        stream.seek(0)
        return stream, {'count': count, 'generated_at': stamp(now), 'since': stamp(since), 'until': stamp(now),
            'scope': '全部入口已保留历史' if not options['key'] else names.get(options['key'], options['key']),
            'kind': 'history', 'retention_days': history.retention / 86400,
            'sources': [{'key': s['key'], 'name': s['name']} for s in selected_sources], 'warnings': warnings,
            'rollups': rollups,
            'notice': '仅包含导出时已采集且仍保留的请求，无法补回过期、未采集或正在进行中的请求。筛选选择整类地址，明细和统计包含该类全部记录。',
            'filters': options}
    except Exception:
        stream.close()
        raise


def samples(items, metadata):
    stream = tempfile.TemporaryFile()
    try:
        count, earliest, latest = 0, None, None
        for item in items:
            safe = record(item)
            try:
                ts = datetime.fromisoformat(item['at'].replace('Z', '+00:00')).timestamp()
                safe['at'] = stamp(ts)
            except (ValueError, KeyError, OverflowError):
                ts = None
            if ts is not None:
                earliest = ts if earliest is None else min(earliest, ts)
                latest = ts if latest is None else max(latest, ts)
            safe['_ts'] = ts
            stream.write((json.dumps(safe, ensure_ascii=False, separators=(',', ':')) + '\n').encode())
            count += 1
        stream.seek(0)
        return stream, {**metadata, 'count': count, 'kind': 'sample', 'generated_at': stamp(time.time()),
            'since': stamp(earliest) if earliest is not None else '', 'until': stamp(latest) if latest is not None else '',
            'warnings': [*metadata.get('warnings', []), '这是近期日志样本，不是全部历史；数量受日志读取窗口限制。']}
    except Exception:
        stream.close()
        raise


def filter_samples(items, options):
    options = validate(options)
    groups = {}
    for item in items:
        path = request_history.normalize_path(item['path']) if options['layout'] == 'tree' else item['path']
        groups.setdefault((item.get('host', ''), item.get('method', ''), path), []).append(item)
    selected = []
    for (host, _, path), group in groups.items():
        if options['status'] == 'success' and any(not 200 <= item['status'] < 400 for item in group):
            continue
        if options['status'] == 'error' and not any(item['status'] >= 400 for item in group):
            continue
        if options['status'] == 'slow' and not any((numeric(item.get('duration_ms')) or 0) >= options['slow_ms'] for item in group):
            continue
        term = options['search'].lower()
        if term and not any(term in f"{host} {path} {item['path']} {item.get('client_ip', '')} {item.get('request_id', '')}".lower() for item in group):
            continue
        selected.extend(group)
    return selected


def rows(stream):
    stream.seek(0)
    for line in stream:
        yield json.loads(line)


class Stats:
    def __init__(self):
        self.count = self.successes = self.errors = self.slow = self.bytes = self.byte_samples = 0
        self.durations, self.upstreams = [], []

    def add(self, item, threshold):
        self.count += 1
        status = item.get('status', 0)
        self.successes += 200 <= status < 400
        self.errors += status >= 400
        duration, upstream, size = (numeric(item.get(k)) for k in ('duration_ms', 'upstream_ms', 'response_bytes'))
        if duration is not None:
            self.durations.append(duration)
            self.slow += duration >= threshold
        if upstream is not None:
            self.upstreams.append(upstream)
        if size is not None:
            self.bytes += size
            self.byte_samples += 1

    def result(self):
        values = sorted(self.durations)
        return {'count': self.count, 'successes': self.successes, 'errors': self.errors,
            'success_rate': self.successes / self.count * 100 if self.count else None,
            'slow_count': self.slow, 'duration_count': len(values),
            'average': math.fsum(values) / len(values) if values else None,
            'p95': values[math.ceil(len(values) * .95) - 1] if values else None,
            'p99': values[math.ceil(len(values) * .99) - 1] if values else None,
            'maximum': values[-1] if values else None,
            'upstream_average': math.fsum(self.upstreams) / len(self.upstreams) if self.upstreams else None,
            'response_bytes': self.bytes if self.byte_samples else None, 'response_samples': self.byte_samples}


def analyze(stream, metadata):
    threshold = metadata.get('filters', {}).get('slow_ms', 1000)
    normalize = metadata.get('filters', {}).get('layout', 'tree') == 'tree'
    totals, groups, status_codes, methods, ips = Stats(), {}, Counter(), Counter(), Counter()
    times = []
    for item in rows(stream):
        totals.add(item, threshold)
        path = item['route'] if normalize else item['path']
        key = (item.get('source', ''), item.get('host', ''), item.get('method', ''), path)
        if key not in groups:
            groups[key] = {'source_name': item.get('source_name', ''), 'host': key[1], 'method': key[2],
                           'path': path, '_stats': Stats(), '_paths': set(), '_upstreams': set()}
        group = groups[key]
        group['_stats'].add(item, threshold)
        group['_paths'].add(item['path'])
        if item.get('upstream_address'):
            group['_upstreams'].add(item['upstream_address'])
        status_codes[str(item.get('status', ''))] += 1
        methods[item.get('method', '')] += 1
        if item.get('client_ip'):
            ips[item['client_ip']] += 1
        if item.get('_ts') is not None:
            times.append(item['_ts'])
    period = max(times) - min(times) if times else 0
    step = 60 if period <= 2 * 3600 else 3600 if period <= 7 * 86400 else 86400
    timeline = {}
    if times:
        first, last = int(min(times) // step) * step, int(max(times) // step) * step
        timeline = {ts: Stats() for ts in range(first, last + step, step)}
        for item in rows(stream):
            if item.get('_ts') is not None:
                timeline[int(item['_ts'] // step) * step].add(item, threshold)
    result_groups = []
    for group in groups.values():
        stats = group.pop('_stats').result()
        paths, upstreams = group.pop('_paths'), group.pop('_upstreams')
        result_groups.append({**group, **stats, 'path_count': len(paths), 'upstream_addresses': sorted(upstreams)})
    result_groups.sort(key=lambda g: (-g['count'], g['host'], g['path'], g['method'], g['source_name']))
    return {**metadata, 'totals': totals.result(), 'groups': result_groups,
        'timeline': [{'at': stamp(ts), **stats.result()} for ts, stats in timeline.items()], 'bucket_seconds': step,
        'statuses': dict(sorted(status_codes.items())), 'methods': dict(methods), 'unique_ips': len(ips),
        'ips': [{'ip': ip, 'count': count} for ip, count in ips.most_common(20)]}


def write_csv(binary, fields, items):
    wrapper = io.TextIOWrapper(binary, encoding='utf-8-sig', newline='')
    try:
        writer = csv.writer(wrapper)
        writer.writerow([label for _, label in fields])
        for item in items:
            writer.writerow([safe_cell(item.get(key)) for key, _ in fields])
        wrapper.flush()
    finally:
        wrapper.detach()


def report(data):
    directory = Path(__file__).resolve().parent / 'ui'
    javascript = (directory / 'request-report.js').read_text(encoding='utf-8')
    digest = base64.b64encode(hashlib.sha256(javascript.encode()).digest()).decode()
    encoded = base64.b64encode(json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).decode()
    return ((directory / 'request-report.html').read_text(encoding='utf-8')
        .replace('__REPORT_CSS__', (directory / 'request-report.css').read_text(encoding='utf-8'))
        .replace('__REPORT_DATA__', encoded).replace('__SCRIPT_HASH__', digest).replace('__REPORT_JS__', javascript)).encode()


def build(stream, metadata, format='zip'):
    if format not in {'csv', 'html', 'zip'}:
        raise ValueError('请选择 CSV 明细、HTML 报告或 ZIP 分析包')
    output = tempfile.TemporaryFile()
    try:
        if format == 'csv':
            write_csv(output, DETAIL_FIELDS, rows(stream))
        else:
            data = analyze(stream, metadata)
            if format == 'html':
                output.write(report(data))
            else:
                data['package'] = True
                with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                    with archive.open('requests.csv', 'w') as target:
                        write_csv(target, DETAIL_FIELDS, rows(stream))
                    with archive.open('routes.csv', 'w') as target:
                        write_csv(target, GROUP_FIELDS, data['groups'])
                    if data.get('rollups'):
                        with archive.open('retained-statistics.csv', 'w') as target:
                            write_csv(target, GROUP_FIELDS, data['rollups'])
                    with archive.open('timeline.csv', 'w') as target:
                        write_csv(target, [('at', '时间区间起点 (UTC)'), *GROUP_FIELDS[4:-1]], data['timeline'])
                    archive.writestr('report.html', report(data))
                    archive.writestr('说明.txt', ('mini_deploy 请求分析\n' + metadata['notice'] + '\n'
                        + f"范围：{metadata['scope']}；共 {metadata['count']} 条；导出时间：{metadata['generated_at']}\n"
                        + '明细与 CSV 时间均为 UTC，数值耗时单位为毫秒。成功为 2xx/3xx，失败为 4xx/5xx。\n'
                        + '统计按完整导出记录计算；缺失耗时不作为零参与平均或分位数。\n'
                        + '\n'.join(metadata.get('warnings', []))).encode('utf-8'))
        output.seek(0, 2)
        size = output.tell()
        output.seek(0)
        return output, size, {'csv': 'text/csv; charset=utf-8', 'html': 'text/html; charset=utf-8', 'zip': 'application/zip'}[format]
    except Exception:
        output.close()
        raise
