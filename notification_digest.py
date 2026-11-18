"""Fixed-window operational summaries with a persistent schedule and delivery queue."""
from contextlib import contextmanager, nullcontext
import json
from pathlib import Path
import sqlite3
import stat
import threading
import time

from certificates import trusted_path
from incident_diagnosis import destinations, split_body, stamp
import observation_analysis as analysis
from panel_diagnostics import scrub

TITLE = 'mini_deploy · 最近 15 分钟运行统计'
FIELDS = ('cpu_percent', 'memory_percent', 'disk_percent', 'swap_percent',
          'network_rx_kib_s', 'network_tx_kib_s')


def settings(raw, existing=None):
    result = {'enabled': False, 'interval_minutes': 15, **(existing or {})}
    if raw is None:
        return result
    if not isinstance(raw, dict):
        raise ValueError('定时统计配置必须为对象。')
    enabled = raw.get('enabled', result['enabled'])
    interval = raw.get('interval_minutes', result['interval_minutes'])
    if type(enabled) is not bool or type(interval) is not int or not 1 <= interval <= 1440:
        raise ValueError('定时统计开关必须为布尔值，发送间隔必须为 1～1440 的整数分钟。')
    return {'enabled': enabled, 'interval_minutes': interval}


def number(value, unit=''):
    return '暂无样本' if value is None else f'{value:.2f}{unit}'


def collect(server, docker, traffic, monitor, target, now=None):
    now = time.time() if now is None else now
    until = int(now // 60) * 60
    since = until - 900
    errors, data = [], {}
    for name, read in (
        ('主机资源', lambda: analysis.resources(server(), since, until, FIELDS)),
        ('Docker 容器', lambda: analysis.containers(docker(), since, until, since - 900, since)),
        ('请求', lambda: analysis.requests(traffic(), since, until)),
        ('异常', lambda: monitor().snapshot()),
    ):
        try:
            data[name] = read()
        except (OSError, RuntimeError, ValueError, sqlite3.Error, TypeError):
            errors.append(f'{name}：来源暂不可读，请检查采集状态；其他统计继续保留。')
    lines = [f'# {TITLE}', '', target, f'统计区间：{stamp(since)} 至 {stamp(until)}（不含结束时刻）',
             '范围：最近 15 个完整分钟；发送间隔独立设置。', '', '## 主机资源']
    resource = data.get('主机资源')
    if resource is not None:
        lines.append(f"采样覆盖率：{resource['coverage_percent']:.1f}%；覆盖 {resource['coverage_seconds']} / 900 秒。")
        for key, label, unit in (
            ('cpu_percent', 'CPU', '%'), ('memory_percent', '内存', '%'),
            ('disk_percent', '磁盘占用', '%'), ('swap_percent', 'Swap 占用', '%'),
            ('network_rx_kib_s', '网络入站', ' KiB/s'), ('network_tx_kib_s', '网络出站', ' KiB/s'),
        ):
            metric = resource['metrics'].get(key, {})
            lines.append(f"- {label}：平均 {number(metric.get('average'), unit)}；峰值 {number(metric.get('peak'), unit)}")
        if resource.get('error'):
            lines.append('注意：历史采样写入异常，本次统计可能不完整，请检查性能历史的采集状态。')
    else:
        lines.append('资源来源不可读，不能判断资源是否正常。')
    lines += ['', '## Docker 容器']
    containers = data.get('Docker 容器')
    if containers is not None:
        lines.append(f'本窗口采集到 {len(containers)} 个容器；以下为平均 CPU 最高的前 5 个，不代表当前运行容器总数。')
        for item in sorted(containers, key=lambda c: c['cpu'] or 0, reverse=True)[:5]:
            lines.append(f"- {item['name']} ({item['container'][:12]})：CPU {number(item['cpu'], '%')}；"
                         f"内存 {number(item['memory'], '%')}；采样覆盖 {item['coverage_percent']:.1f}%")
        if not containers:
            lines.append('本窗口没有容器样本；不能据此判断所有容器正常或没有容器。')
    else:
        lines.append('容器来源不可读。')
    lines += ['', '## 网站与接口请求']
    requests = data.get('请求')
    if requests is not None:
        lines += [f"已采集请求：{requests['count']}；4xx：{requests['errors'] - requests['server_errors']}；"
                  f"5xx：{requests['server_errors']}；4xx + 5xx 错误率：{number(requests['error_rate'], '%')}",
                  f"平均耗时：{number(requests['average'], ' ms')}；P95：{number(requests['p95'], ' ms')}；"
                  f"有耗时记录：{requests['duration_count']} 条。",
                  '采集状态：' + ('不完整或尚未接入；不能据此判断全部业务流量。' if requests['incomplete'] else '已接入来源在本窗口内持续采集。'),
                  requests['notice']]
        for source in requests['sources']:
            if source.get('error') or source.get('cursor', 0) < until - 60:
                lines.append(f"- 来源 {source['name']}：读取异常或采集进度落后，请检查请求接入。")
        if requests['slow_endpoints']:
            lines.append('耗时最高的前 5 个接口（P95）：')
            for item in requests['slow_endpoints'][:5]:
                lines.append(f"- {item['host']} {item['method']} {item['path']}：{item['count']} 次，P95 {number(item['p95'], ' ms')}")
    else:
        lines.append('请求来源不可读，不能判断业务是否有流量或错误。')
    lines += ['', '## 异常与恢复']
    monitor_data = data.get('异常')
    if monitor_data is not None:
        events = [e for e in monitor_data.get('events', []) if since <= e.get('at_ts', 0) < until]
        active = monitor_data.get('alerts', [])
        lines.append(f"本窗口已保留事件：新增异常 {sum(e.get('kind') == 'alert' for e in events)}；"
                     f"恢复 {sum(e.get('kind') == 'recovery' for e in events)}；报告生成时仍有 {len(active)} 项异常。")
        lines.append('以下为最近 5 条窗口事件；事件为当前保留记录，采集缺失不能视作无异常。')
        for event in sorted(events, key=lambda e: e['at_ts'], reverse=True)[:5]:
            lines.append(f"- {stamp(event['at_ts'])} {'恢复' if event.get('kind') == 'recovery' else '异常'}：{event.get('title', '')}")
        for item in active[:5]:
            lines.append(f"- 当前异常：{item.get('title', '')}" + ('（采样已过期）' if item.get('stale') or item.get('sample_unknown') else ''))
    else:
        lines.append('异常来源不可读。')
    if errors:
        lines += ['', '## 数据缺口', *errors]
    lines += ['', '本报告仅汇总已采集数据，不调用 AI，不执行服务器变更。']
    return {'since': since, 'until': until, 'body': scrub('\n'.join(lines))}


class Store:
    def __init__(self, home, maintenance=nullcontext, clock=time.time):
        self.path = Path(home) / 'notification-digest' / 'digest.sqlite3'
        self.lock, self.maintenance, self.clock = threading.RLock(), maintenance, clock

    @contextmanager
    def connect(self):
        with self.maintenance(), self.lock:
            for suffix in ('', '-journal', '-wal', '-shm'):
                path = Path(str(self.path) + suffix)
                trusted_path(path)
                if path.exists() and (not stat.S_ISREG(path.stat().st_mode) or path.stat().st_nlink != 1):
                    raise ValueError('定时统计数据库必须是独立普通文件，原文件保留。')
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.path.parent.chmod(0o700)
            db = sqlite3.connect(self.path, timeout=3)
            try:
                self.path.chmod(0o600)
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('PRAGMA max_page_count=32768')
                db.executescript('''CREATE TABLE IF NOT EXISTS schedule(id INTEGER PRIMARY KEY,signature TEXT,
                        next_at REAL,error TEXT DEFAULT '');
                    INSERT OR IGNORE INTO schedule(id,signature,next_at) VALUES(1,'',0);
                    CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY,created REAL,since REAL,until REAL,body TEXT);
                    CREATE TABLE IF NOT EXISTS deliveries(report INTEGER REFERENCES reports(id) ON DELETE CASCADE,
                        channel TEXT,destination TEXT,part INTEGER,total INTEGER,body TEXT,status TEXT DEFAULT 'pending',
                        attempts INTEGER DEFAULT 0,retry_at REAL DEFAULT 0,attempt_at REAL DEFAULT 0,
                        PRIMARY KEY(report,channel,part));
                    CREATE INDEX IF NOT EXISTS digest_delivery_due ON deliveries(status,retry_at);''')
                with db:
                    yield db
            finally:
                db.close()

    def sync(self, config):
        options = settings(config.get('digest'))
        signature = json.dumps(options, sort_keys=True)
        with self.connect() as db:
            row = dict(db.execute('SELECT * FROM schedule WHERE id=1').fetchone())
            if row['signature'] != signature:
                db.execute("UPDATE schedule SET signature=?,next_at=?,error='' WHERE id=1",
                           (signature, self.clock() + options['interval_minutes'] * 60 if options['enabled'] else 0))
            # A new recipient must not receive a leftover fragment of an old report.
            for channel, destination in destinations(config).items():
                db.execute("UPDATE deliveries SET status='cancelled' WHERE channel=? AND destination!=? "
                           "AND status IN ('pending','failed')", (channel, destination))
            return dict(db.execute('SELECT * FROM schedule WHERE id=1').fetchone())

    def restart(self):
        with self.connect() as db:
            db.execute("UPDATE deliveries SET status='pending',retry_at=? WHERE status='sending'", (self.clock() + 10,))

    def fail(self):
        with self.connect() as db:
            db.execute("UPDATE schedule SET error='统计生成失败，将在一分钟后重试；请检查采集与数据目录。' WHERE id=1")

    def publish(self, report, config, ticket):
        options, now = settings(config.get('digest')), self.clock()
        with self.connect() as db:
            current = dict(db.execute('SELECT * FROM schedule WHERE id=1').fetchone())
            if current != ticket or not options['enabled'] or not destinations(config):
                return
            pending = db.execute("SELECT COUNT(DISTINCT report) FROM deliveries WHERE status IN ('pending','failed','sending')").fetchone()[0]
            if pending >= 100:
                db.execute("UPDATE schedule SET error='待投递统计已达 100 份，暂停生成；请检查通知渠道。' WHERE id=1")
                return
            # Bound only completed history; failed deliveries and their full bodies are retained.
            db.execute("DELETE FROM reports WHERE id NOT IN (SELECT report FROM deliveries WHERE status IN ('pending','failed','sending')) "
                       'AND id NOT IN (SELECT id FROM reports ORDER BY id DESC LIMIT 100)')
            key = db.execute('INSERT INTO reports(created,since,until,body) VALUES(?,?,?,?)',
                             (now, report['since'], report['until'], report['body'])).lastrowid
            for channel, destination in destinations(config).items():
                budget = {'wecom': 3400, 'dingtalk': 17000, 'email': None}[channel]
                pieces = split_body(report['body'], budget) if budget else [report['body']]
                for part, piece in enumerate(pieces, 1):
                    body = f'统计 #{key} · 第 {part}/{len(pieces)} 条\n\n{piece}'
                    db.execute('INSERT INTO deliveries(report,channel,destination,part,total,body) VALUES(?,?,?,?,?,?)',
                               (key, channel, destination, part, len(pieces), body))
            db.execute("UPDATE schedule SET next_at=?,error='' WHERE id=1", (now + options['interval_minutes'] * 60,))

    def next_delivery(self, config):
        if not settings(config.get('digest'))['enabled']:
            return None
        now = self.clock()
        with self.connect() as db:
            for channel, destination in destinations(config).items():
                row = db.execute("SELECT * FROM deliveries WHERE channel=? AND destination=? AND status IN ('pending','failed','sending') "
                                 'ORDER BY report,part LIMIT 1', (channel, destination)).fetchone()
                if not row or row['status'] == 'sending' or row['retry_at'] > now:
                    continue
                recent = db.execute('SELECT MAX(attempt_at) FROM deliveries WHERE channel=? AND destination=?',
                                    (channel, destination)).fetchone()[0]
                if recent and recent + 3.2 > now:
                    continue
                db.execute("UPDATE deliveries SET status='sending',attempts=attempts+1,attempt_at=? "
                           'WHERE report=? AND channel=? AND part=?', (now, row['report'], channel, row['part']))
                return dict(row)
        return None

    def receipt(self, delivery, ok):
        retry = self.clock() + min(3600, 60 * 2 ** min(delivery['attempts'], 6))
        with self.connect() as db:
            db.execute('UPDATE deliveries SET status=?,retry_at=? WHERE report=? AND channel=? AND part=?',
                       ('sent' if ok else 'failed', 0 if ok else retry,
                        delivery['report'], delivery['channel'], delivery['part']))

    def snapshot(self, config):
        with self.connect() as db:
            schedule = dict(db.execute('SELECT * FROM schedule WHERE id=1').fetchone())
            last = db.execute('SELECT * FROM reports ORDER BY id DESC LIMIT 1').fetchone()
            pending = db.execute("SELECT COUNT(DISTINCT report) FROM deliveries WHERE status IN ('pending','failed','sending')").fetchone()[0]
            parts = [dict(row) for row in db.execute('SELECT channel,destination,status,COUNT(*) AS parts '
                'FROM deliveries WHERE report=? GROUP BY channel,destination,status', (last['id'] if last else 0,))]
        current = destinations(config)
        for item in parts:
            item['destination_changed'] = current.get(item['channel']) != item.pop('destination')
        return {'next_at': schedule['next_at'], 'error': schedule['error'], 'channels_ready': bool(current),
                'pending_reports': pending, 'last': dict(last) if last else None, 'deliveries': parts}


class Worker:
    def __init__(self, store, config, collect, send, log=lambda _: None):
        self.store, self.config, self.collect, self.send, self.log = store, config, collect, send, log
        self.error, self.build_retry_at = '', 0

    def step(self):
        config, now = self.config(), self.store.clock()
        ticket = self.store.sync(config)
        options = settings(config.get('digest'))
        if not options['enabled'] or not destinations(config):
            return
        if now >= ticket['next_at'] and now >= self.build_retry_at:
            try:
                report = self.collect(now)
                # A save during collection can cancel this report or change its destinations.
                latest = self.config()
                self.store.sync(latest)
                if settings(latest.get('digest')) == options:
                    self.store.publish(report, latest, ticket)
                self.build_retry_at = now + 60
            except Exception as exc:
                self.build_retry_at = now + 60
                self.store.fail()
                self.log(f'notification digest collection failed: {type(exc).__name__}')
        delivery = self.store.next_delivery(self.config())
        if delivery:
            fresh = self.config()
            if (not settings(fresh.get('digest'))['enabled'] or
                    destinations(fresh).get(delivery['channel']) != delivery['destination']):
                self.store.receipt(delivery, False)
                return
            try:
                self.send(delivery['channel'], fresh[delivery['channel']], TITLE, delivery['body'])
            except Exception as exc:
                self.store.receipt(delivery, False)
                self.log(f"notification digest delivery failed channel={delivery['channel']}: {type(exc).__name__}")
            else:
                self.store.receipt(delivery, True)

    def run(self, stop):
        restarted = False
        while not stop.is_set():
            try:
                if not restarted:
                    self.store.restart()
                    restarted = True
                self.step()
                self.error = ''
            except Exception as exc:
                if not self.error:
                    self.log(f'notification digest worker failed: {type(exc).__name__}')
                self.error = '定时统计后台暂不可用，请检查服务日志和数据目录；已有报告保留。'
            stop.wait(5)
