"""Request origin summaries and a bounded, asynchronous IP location cache."""
from collections import defaultdict
from datetime import datetime
import ipaddress
import json
import sqlite3
import threading
import time

from login_guard import address, locate
from request_history import PERIODS
from request_gateway import KEY


def canonical(value):
    try:
        return address(value)
    except (ValueError, TypeError):
        return ''


class Locations:
    def __init__(self, history, *, locator=locate, clock=time.time):
        self.history, self.locator, self.clock = history, locator, clock
        self.lock = threading.Lock()
        self.running = False
        self.next_lookup = 0
        with history.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS request_locations '
                       '(ip TEXT PRIMARY KEY, checked REAL NOT NULL, value TEXT NOT NULL)')

    def history_summary(self, key, *, period='24h', search='', status='all', slow_ms=1000, now=None):
        if (not isinstance(key, str) or not KEY.fullmatch(key) or period not in PERIODS
                or status not in {'all', 'success', 'error', 'slow'} or not isinstance(search, str)
                or len(search) > 2048 or type(slow_ms) is not int or not 1 <= slow_ms <= 3_600_000):
            raise ValueError('请求地区筛选参数无效')
        now = self.clock() if now is None else now
        where, params = ['source=?', 'ts BETWEEN ? AND ?'], [key, now - min(PERIODS[period], self.history.retention), now]
        if status != 'all':
            where.append({'success': 'status>=200 AND status<400', 'error': 'status>=400', 'slow': 'duration>=?'}[status])
            if status == 'slow':
                params.append(slow_ms)
        if search:
            where.append("INSTR(LOWER(host || ' ' || path || ' ' || route || ' ' || "
                         "COALESCE(json_extract(data,'$.client_ip'),'') || ' ' || "
                         "COALESCE(json_extract(data,'$.request_id'),'')),?)>0")
            params.append(search.lower())
        with self.history.connect() as db:
            db.create_function('request_ip', 1, canonical, deterministic=True)
            rows = [dict(row) for row in db.execute(
                "WITH grouped AS (SELECT request_ip(json_extract(data,'$.client_ip')) AS ip,"
                "COUNT(*) AS count,SUM(status>=400) AS errors,SUM(status>=200 AND status<400) AS successes,"
                "MAX(ts) AS last_at,host AS last_host,path AS last_path,method AS last_method "
                "FROM requests WHERE " + ' AND '.join(where) + " GROUP BY ip) "
                "SELECT *,SUM(count) OVER() AS total_requests,COUNT(*) OVER() AS ip_groups,"
                "SUM(ip='') OVER() AS missing_group FROM grouped ORDER BY (ip='') DESC,count DESC,ip LIMIT 501", params)]
        total = rows[0]['total_requests'] if rows else 0
        missing = next((row['count'] for row in rows if not row['ip']), 0)
        ips = rows[0]['ip_groups'] - rows[0]['missing_group'] if rows else 0
        return self._enrich([row for row in rows if row['ip']][:500], total, missing, ips, 'history')

    def sample_summary(self, records):
        groups = defaultdict(lambda: {'count': 0, 'errors': 0, 'successes': 0, 'last_at': 0})
        missing = 0
        for item in records:
            ip = canonical(item.get('client_ip'))
            if not ip:
                missing += 1
                continue
            row = groups[ip]
            row['ip'] = ip
            row.setdefault('aliases', set()).add(item['client_ip'])
            row['count'] += 1
            code = item.get('status', 0)
            row['errors'] += int(isinstance(code, int) and code >= 400)
            row['successes'] += int(isinstance(code, int) and 200 <= code < 400)
            try:
                at = float(item['ts']) if 'ts' in item else datetime.fromisoformat(item['at'].replace('Z', '+00:00')).timestamp()
            except (ValueError, TypeError, KeyError):
                at = 0
            if at >= row['last_at']:
                row.update(last_at=at, last_host=item.get('host', ''), last_path=item.get('path', '/'), last_method=item.get('method', ''))
        rows = sorted(groups.values(), key=lambda row: (-row['count'], row['ip']))[:500]
        return self._enrich(rows, len(records), missing, len(groups), 'sample')

    def _enrich(self, rows, total, missing, ips, mode):
        now, pending = self.clock(), []
        with self.history.connect() as db:
            cache = {row['ip']: (row['checked'], json.loads(row['value'])) for row in db.execute('SELECT * FROM request_locations')}
        for row in rows:
            if 'aliases' in row:
                row['aliases'] = sorted(row['aliases'])
            if not ipaddress.ip_address(row['ip']).is_global:
                row['location'] = {'label': '内网 / 保留地址'}
            else:
                checked, value = cache.get(row['ip'], (0, {'label': '待定位'}))
                row['location'] = value
                if now - checked > (7 * 86400 if 'latitude' in value else 3600):
                    pending.append(row['ip'])
            for key in ('total_requests', 'ip_groups', 'missing_group'):
                row.pop(key, None)
        self._schedule(pending, now)
        return {'items': rows, 'total_requests': total, 'missing_ip_requests': missing, 'total_ips': ips,
                'mode': mode, 'checked_at': now, 'limited': ips > len(rows)}

    def _schedule(self, ips, now):
        with self.lock:
            if self.running or now < self.next_lookup or not ips:
                return
            self.running = True
            self.next_lookup = now + 10
        threading.Thread(target=self._resolve, args=(ips[:2],), name='request-ip-locations', daemon=True).start()

    def _resolve(self, ips):
        try:
            for ip in ips:
                try:
                    value = self.locator(ip)
                except (OSError, ValueError, RuntimeError, TypeError):
                    value = {'label': '归属地暂不可用'}
                with self.history.connect() as db:
                    db.execute('INSERT OR REPLACE INTO request_locations VALUES (?,?,?)', (ip, self.clock(), json.dumps(value)))
                    db.execute('DELETE FROM request_locations WHERE ip IN '
                               '(SELECT ip FROM request_locations ORDER BY checked DESC,ip LIMIT -1 OFFSET 2048)')
        except (OSError, ValueError, sqlite3.Error):
            pass
        finally:
            with self.lock:
                self.running = False
