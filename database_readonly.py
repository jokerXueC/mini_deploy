"""Encrypted connection profiles and bounded, cancellable read-only database jobs."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, time as datetime_time
from decimal import Decimal
import hashlib
import importlib.util
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import stat
import subprocess
import tempfile
import threading
import time

from certificates import atomic_write, trusted_path
from database_sql import DatabaseError, select_sql, sqlglot
import database_schema

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:
    Fernet = None
    InvalidToken = ValueError

KEY = re.compile(r'[a-f0-9]{24}')
LOCK = threading.RLock()
ROWS, CELL_BYTES, RESULT_BYTES = 200, 16384, 2 * 1024**2
EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix='database-readonly')


def dependencies():
    encryption = Fernet is not None
    return {engine: {'available': encryption and importlib.util.find_spec(module) is not None,
                     'driver': module} for engine, module in (('mysql', 'pymysql'), ('postgres', 'psycopg2'))} | {
        'sql': sqlglot is not None, 'encryption': encryption}


def text(value, label, maximum=128, *, empty=False):
    if not isinstance(value, str) or len(value.encode('utf-8')) > maximum or any(ord(c) < 32 for c in value):
        raise DatabaseError(f'{label}无效或过长。')
    if not empty and not value.strip():
        raise DatabaseError(f'请填写{label}。')
    return value


def normalize(data):
    if not isinstance(data, dict):
        raise DatabaseError('连接参数无效。')
    engine = data.get('engine')
    if engine not in {'mysql', 'postgres'}:
        raise DatabaseError('请选择 MySQL / MariaDB 或 PostgreSQL。')
    host = text(data.get('host', ''), '服务器地址', 253).strip()
    if not re.fullmatch(r'[A-Za-z0-9_.:-]+', host) or host.startswith('-'):
        raise DatabaseError('服务器地址只填写 IP 或主机名，不填写 URL、路径或端口。')
    try:
        port = int(data.get('port', 3306 if engine == 'mysql' else 5432))
    except (TypeError, ValueError):
        raise DatabaseError('数据库端口无效。') from None
    if isinstance(data.get('port'), bool) or not 1 <= port <= 65535:
        raise DatabaseError('数据库端口应为 1～65535。')
    tls = data.get('tls', True)
    if type(tls) is not bool:
        raise DatabaseError('TLS 设置无效。')
    ca = data.get('ca_pem', '')
    if not isinstance(ca, str) or len(ca.encode()) > 65536 or (ca and '-----BEGIN CERTIFICATE-----' not in ca):
        raise DatabaseError('CA 证书应为 PEM 格式，最多 64 KiB。')
    if ca:
        try:
            if 'PRIVATE KEY' in ca:
                raise ValueError()
            ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=ca)
        except (ValueError, ssl.SSLError):
            raise DatabaseError('CA 内容无效；仅填写公开 CA 证书，不填写私钥。') from None
    return {'engine': engine, 'name': text(data.get('name', ''), '连接名称', 100).strip(),
            'host': host, 'port': port, 'user': text(data.get('user', ''), '数据库账号', 128),
            'database': text(data.get('database', ''), '默认数据库', 128, empty=True), 'tls': tls, 'ca_pem': ca}


def private_bytes(path, maximum):
    trusted_path(path)
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (os.name == 'posix' and info.st_mode & 0o077):
            raise DatabaseError('数据库连接文件必须为私有普通文件，请检查数据目录权限。', 503)
        value = stream.read(maximum + 1)
    if len(value) > maximum:
        raise DatabaseError('数据库连接文件异常，原文件保留。', 503)
    return value


class Profiles:
    def __init__(self, home):
        self.home = Path(home) / 'databases'
        self.path, self.key = self.home / 'connections.json', self.home / 'credentials.key'

    def read(self):
        trusted_path(self.path)
        if not self.path.exists():
            return {}
        try:
            data = json.loads(private_bytes(self.path, 1024**2))
            if not isinstance(data, dict) or len(data) > 50:
                raise ValueError()
            for key, item in data.items():
                if not KEY.fullmatch(key) or not isinstance(item, dict) or not isinstance(item.get('password'), str):
                    raise ValueError()
                normalize(item)
            return data
        except (ValueError, UnicodeError):
            raise DatabaseError('数据库连接记录无法读取，原文件保留；请检查数据目录。', 503) from None

    def cipher(self, *, create=False):
        if Fernet is None:
            raise DatabaseError('凭据加密组件未安装，请按数据库指南安装依赖。', 503)
        trusted_path(self.key)
        if not self.key.exists():
            if not create or self.path.exists():
                raise DatabaseError('数据库凭据密钥缺失；请从同一份备份恢复，不要重建密钥。', 503)
            self.home.mkdir(parents=True, mode=0o700, exist_ok=True)
            with os.fdopen(os.open(self.key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
                stream.write(Fernet.generate_key())
                stream.flush()
                os.fsync(stream.fileno())
        try:
            return Fernet(private_bytes(self.key, 128))
        except ValueError:
            raise DatabaseError('数据库凭据密钥无法读取，原文件保留。', 503) from None

    @staticmethod
    def public(key, item):
        return {**{k: v for k, v in item.items() if k != 'password'}, 'id': key,
                'password_saved': bool(item.get('password')), 'read_only': True}

    def list(self):
        with LOCK:
            return [self.public(key, item) for key, item in self.read().items()]

    def get(self, key):
        if not isinstance(key, str) or not KEY.fullmatch(key):
            raise DatabaseError('数据库连接不存在。', 404)
        with LOCK:
            item = self.read().get(key)
            if item is None:
                raise DatabaseError('数据库连接不存在。', 404)
            try:
                password = self.cipher().decrypt(item['password'].encode()).decode()
            except (InvalidToken, UnicodeError):
                raise DatabaseError('数据库凭据无法解密，请检查密钥或重新填写密码。', 503) from None
            return {**item, 'password': password}

    def draft(self, data):
        spec = normalize(data)
        password = data.get('password', '')
        if not isinstance(password, str) or len(password.encode()) > 4096 or '\x00' in password:
            raise DatabaseError('数据库密码无效或过长。')
        if not password and data.get('id'):
            old = self.get(data['id'])
            if any(old[k] != spec[k] for k in ('engine', 'host', 'port', 'user')):
                raise DatabaseError('服务器、端口、类型或账号已改变，请重新填写密码。')
            password = old['password']
        return {**spec, 'password': password}

    def save(self, data):
        with LOCK:
            items = self.read()
            key = data.get('id') or secrets.token_hex(12)
            if not isinstance(key, str) or not KEY.fullmatch(key):
                raise DatabaseError('连接编号无效。')
            old = items.get(key)
            if data.get('id') and old is None:
                raise DatabaseError('连接已被移除，请刷新。', 409)
            if old and data.get('revision') != old.get('revision'):
                raise DatabaseError('连接配置已变化，请刷新后重新编辑。', 409)
            if not old and len(items) >= 50:
                raise DatabaseError('最多保存 50 个数据库连接。')
            spec = self.draft(data)
            token = self.cipher(create=True).encrypt(spec.pop('password').encode()).decode()
            items[key] = {**spec, 'password': token, 'revision': secrets.token_hex(12)}
            atomic_write(self.path, json.dumps(items, ensure_ascii=False))
            return self.public(key, items[key])

    def remove(self, key, revision):
        if not isinstance(key, str) or not KEY.fullmatch(key):
            raise DatabaseError('连接编号无效。')
        with LOCK:
            items = self.read()
            if key not in items or items[key].get('revision') != revision:
                raise DatabaseError('连接已变化，请刷新后重试。', 409)
            del items[key]
            atomic_write(self.path, json.dumps(items, ensure_ascii=False))


def identifier(value, engine):
    value = text(value, '数据库对象名称', 256)
    mark = '"' if engine == 'postgres' else '`'
    return mark + value.replace(mark, mark * 2) + mark


def cell(value):
    if value is None or type(value) in (int, bool):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f'[二进制 {len(value)} 字节]'
    if isinstance(value, (datetime, date, datetime_time)):
        value = value.isoformat()
    elif isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    elif isinstance(value, Decimal):
        value = str(value)
    value = str(value)
    return value[:CELL_BYTES] + ('… [内容已截断]' if len(value) > CELL_BYTES else '')


class Session:
    def __init__(self, spec, cancel):
        self.spec, self.cancelled = spec, cancel
        self.connection = None
        self.access = {}
        self._lock = threading.Lock()

    def interrupt(self):
        with self._lock:
            conn = self.connection
            if conn is None:
                return
            try:
                if self.spec['engine'] == 'postgres':
                    conn.cancel()
                else:
                    # MySQL permits cancelling the account's own connection without admin grants.
                    # Closing a socket alone does not promptly interrupt blocking reads on Windows.
                    import pymysql
                    spec = self.spec
                    tls = ssl.create_default_context(cadata=spec['ca_pem'] or None) if spec['tls'] else None
                    target = conn.thread_id()
                    if type(target) is not int or target <= 0:
                        return
                    control = pymysql.connect(host=spec['host'], port=spec['port'], user=spec['user'],
                        password=spec['password'], connect_timeout=3, read_timeout=3, write_timeout=3,
                        ssl=tls, local_infile=False, autocommit=True)
                    try:
                        with control.cursor() as cursor:
                            cursor.execute(f'KILL QUERY {target}')
                    finally:
                        control.close()
            except Exception:
                if self.spec['engine'] != 'postgres' and getattr(conn, '_sock', None):
                    try:
                        conn._sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

    @contextmanager
    def opened(self):
        spec = self.spec
        available = dependencies()
        if not available[spec['engine']]['available']:
            raise DatabaseError('数据库驱动或加密组件未安装，请打开数据库使用指南安装依赖。', 503)
        ca_file = None
        conn = None
        try:
            if spec['engine'] == 'postgres':
                import psycopg2
                options = {}
                if spec['tls']:
                    if spec['ca_pem']:
                        descriptor, ca_file = tempfile.mkstemp(prefix='mini-deploy-db-ca-', suffix='.pem')
                        with os.fdopen(descriptor, 'w', encoding='ascii') as stream:
                            stream.write(spec['ca_pem'])
                    else:
                        ca_file = ssl.get_default_verify_paths().cafile
                        if not ca_file:
                            raise DatabaseError('当前系统没有可用的 CA 文件，请提供数据库 CA 证书。')
                    options = {'sslmode': 'verify-full', 'sslrootcert': ca_file}
                else:
                    options['sslmode'] = 'disable'
                conn = psycopg2.connect(host=spec['host'], port=spec['port'], user=spec['user'],
                    password=spec['password'], dbname=spec['database'] or 'postgres', connect_timeout=5,
                    application_name='mini_deploy_readonly', options='-c default_transaction_read_only=on '
                    '-c statement_timeout=15000 -c lock_timeout=3000 -c idle_in_transaction_session_timeout=20000', **options)
                conn.set_session(readonly=True, autocommit=False)
            else:
                import pymysql
                tls = ssl.create_default_context(cadata=spec['ca_pem'] or None) if spec['tls'] else None
                conn = pymysql.connect(host=spec['host'], port=spec['port'], user=spec['user'],
                    password=spec['password'], database=spec['database'] or None, connect_timeout=5,
                    read_timeout=20, write_timeout=5, charset='utf8mb4', autocommit=False, ssl=tls,
                    local_infile=False, cursorclass=pymysql.cursors.SSCursor)
                with conn.cursor() as cursor:
                    cursor.execute('SELECT VERSION()')
                    version = str(cursor.fetchone()[0])
                    cursor.fetchall()
                    if 'mariadb' in version.lower():
                        cursor.execute('SET SESSION max_statement_time=15')
                    else:
                        cursor.execute('SET SESSION MAX_EXECUTION_TIME=15000')
                    cursor.execute('SET SESSION TRANSACTION READ ONLY')
                    cursor.execute('START TRANSACTION READ ONLY')
            with conn.cursor() as cursor:
                cursor.execute("SELECT current_user,current_setting('transaction_read_only')" if spec['engine'] == 'postgres'
                               else 'SELECT CURRENT_USER(),@@SESSION.' + ('tx_read_only' if 'mariadb' in version.lower() else 'transaction_read_only'))
                account, read_only = cursor.fetchone()
                cursor.fetchall()
            if str(read_only).lower() not in {'on', '1', 'true'}:
                raise DatabaseError('数据库未确认只读事务，已停止连接，请检查数据库版本与事务设置。')
            self.access = {'account': str(account), 'transaction_read_only': True,
                           'detail': '已验证只读事务；面板不改变数据库账号原有权限。'}
            with self._lock:
                self.connection = conn
            if self.cancelled.is_set():
                raise DatabaseError('查询已取消。')
            yield self
        finally:
            with self._lock:
                self.connection = None
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
                conn.close()
            if ca_file and spec['ca_pem'] and os.path.isfile(ca_file):
                os.unlink(ca_file)

    def query(self, sql, params=None, *, maximum=ROWS):
        if self.cancelled.is_set():
            raise DatabaseError('查询已取消。')
        rows, size, truncated, reason = [], 0, False, ''
        with self.connection.cursor() as cursor:
            cursor.execute(sql, params)
            columns = [str(d[0]) for d in cursor.description or []]
            if len(columns) > 256:
                raise DatabaseError('结果超过 256 列，请只查询需要的字段。')
            for _ in range(maximum + 1):
                if self.cancelled.is_set():
                    raise DatabaseError('查询已取消。')
                row = cursor.fetchone()
                if row is None:
                    break
                if len(rows) == maximum:
                    truncated, reason = True, 'rows'
                    break
                values = [cell(v) for v in row]
                size += len(json.dumps(values, ensure_ascii=False).encode())
                if size > RESULT_BYTES:
                    truncated, reason = True, 'bytes'
                    break
                rows.append(values)
        return {'columns': columns, 'rows': rows, 'truncated': truncated, 'truncation_reason': reason,
                'returned_rows': len(rows), 'read_only': True}

    def objects(self, database):
        pg = self.spec['engine'] == 'postgres'
        result = self.query('SELECT datname FROM pg_database WHERE NOT datistemplate '
            "AND datallowconn AND has_database_privilege(oid,'CONNECT') ORDER BY datname LIMIT 501" if pg else
            "SELECT schema_name FROM information_schema.schemata WHERE schema_name NOT IN "
            "('information_schema','mysql','performance_schema','sys') ORDER BY schema_name LIMIT 501", maximum=500)
        databases = [r[0] for r in result['rows']]
        current = database or self.spec['database'] or ('postgres' if pg else '')
        if not current:
            return {'databases': databases, 'database': '', 'tables': [], 'truncated': result['truncated']}
        if pg:
            tables = self.query("SELECT table_schema,table_name,table_type FROM information_schema.tables "
                "WHERE table_schema NOT IN ('pg_catalog','information_schema') ORDER BY table_schema,table_name LIMIT 1001", maximum=1000)
        else:
            tables = self.query('SELECT table_schema,table_name,table_type FROM information_schema.tables '
                'WHERE table_schema=%s ORDER BY table_name LIMIT 1001', (current,), maximum=1000)
        return {'databases': databases, 'database': current,
                'tables': [dict(zip(('schema', 'name', 'type'), row)) for row in tables['rows']],
                'truncated': tables['truncated'] or result['truncated']}

    def columns(self, schema, table):
        pg = self.spec['engine'] == 'postgres'
        return self.query('SELECT column_name,data_type,is_nullable,column_default,'
            + ('character_maximum_length' if pg else 'column_key') + ' FROM information_schema.columns '
            'WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position LIMIT 257', (schema, table), maximum=256)

    def structure(self, schema, table):
        result = self.columns(schema, table)
        if not result['rows']:
            raise DatabaseError('表不存在或当前账号无权查看。')
        if self.spec['engine'] == 'postgres':
            indexes = self.query('SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=%s AND tablename=%s '
                                 'ORDER BY indexname LIMIT 201', (schema, table))
        else:
            indexes = self.query('SELECT index_name,column_name,non_unique,seq_in_index FROM information_schema.statistics '
                                 'WHERE table_schema=%s AND table_name=%s ORDER BY index_name,seq_in_index LIMIT 201', (schema, table))
        return {'fields': result, 'indexes': indexes}

    def browse(self, data):
        engine, schema, table = self.spec['engine'], data['schema'], data['table']
        fields = self.columns(schema, table)
        names = [r[0] for r in fields['rows']]
        if not names or len(names) > 128:
            raise DatabaseError('表不存在、无权访问或字段过多；请在 SQL 中选择需要的字段。')
        page = data.get('page', 0)
        if type(page) is not int or not 0 <= page <= 1000:
            raise DatabaseError('分页参数无效，最多浏览前 1001 页；更深的数据请使用筛选。')
        size = 50
        column, value, operator = data.get('filter_column', ''), data.get('filter_value', ''), data.get('filter_operator', 'contains')
        where, params = '', []
        if column:
            if column not in names or operator not in {'contains', 'equals', 'is_null'}:
                raise DatabaseError('筛选字段或条件无效。')
            field = identifier(column, engine)
            if operator == 'is_null':
                where = f' WHERE {field} IS NULL'
            else:
                value = text(value, '筛选内容', 1024, empty=True)
                cast = 'TEXT' if engine == 'postgres' else 'CHAR'
                where = f' WHERE CAST({field} AS {cast}) ' + ('LIKE %s' if operator == 'contains' else '= %s')
                params.append('%' + value.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%' if operator == 'contains' else value)
                if operator == 'contains':
                    where += " ESCAPE '!'"
        order = data.get('sort', '')
        direction = data.get('direction', 'asc')
        if direction not in {'asc', 'desc'} or (order and order not in names):
            raise DatabaseError('排序字段无效。')
        if not order:
            order = next((row[0] for row in fields['rows'] if row[1] not in {'json', 'jsonb', 'xml', 'ARRAY', 'USER-DEFINED'}), '')
        if not order:
            raise DatabaseError('此表没有可默认排序的字段，请在 SQL 中选择字段查询。')
        sql = f'SELECT * FROM {identifier(schema, engine)}.{identifier(table, engine)}{where} '
        sql += f'ORDER BY {identifier(order, engine)} {direction.upper()} LIMIT %s OFFSET %s'
        result = self.query(sql, tuple([*params, size + 1, page * size]), maximum=size)
        byte_limit = result['truncation_reason'] == 'bytes'
        return {**result, 'page': page, 'page_size': size, 'has_more': result['truncated'] and not byte_limit, 'sort': order,
                'notice': '本页内容超过 2 MiB，请使用 SQL 选择较少字段或增加筛选，避免跨页遗漏。' if byte_limit else
                '分页不锁表；并发写入或排序字段不唯一时，跨页结果可能变化。'}


class Manager:
    def __init__(self, home):
        self.profiles = Profiles(home)
        self.jobs = {}
        self.lock = threading.RLock()
        self._expiry_timer = None

    def _schedule_expiry(self):
        if self._expiry_timer is None and self.jobs:
            self._expiry_timer = threading.Timer(30, self._expire_jobs)
            self._expiry_timer.daemon = True
            self._expiry_timer.start()

    def _expire_jobs(self):
        with self.lock:
            self._expiry_timer = None
            now = time.time()
            self.jobs = {k: v for k, v in self.jobs.items() if v['state'] == 'running' or now - v['created_at'] < 300}
            self._schedule_expiry()

    def snapshot(self):
        return {'connections': self.profiles.list(), 'dependencies': dependencies(), 'read_only': True}

    def start(self, owner, data, *, draft=False):
        operation = 'test' if draft else data.get('operation')
        if operation not in {'test', 'catalog', 'structure', 'browse', 'query', 'relationships'}:
            raise DatabaseError('不支持的只读数据库操作。')
        spec = self.profiles.draft(data) if draft else self.profiles.get(data.get('id'))
        database = text(data.get('database', spec['database']), '数据库名称', 128, empty=True)
        spec = {**spec, 'database': database}
        if operation in {'browse', 'structure'}:
            data = {**data, 'schema': text(data.get('schema', ''), 'Schema', 256),
                    'table': text(data.get('table', ''), '数据表', 256)}
        if operation == 'query':
            data = {**data, 'sql': select_sql(data.get('sql'), spec['engine'])}
        if operation == 'relationships':
            data = {**data, 'schema': text(data.get('schema', ''), 'Schema', 256, empty=True)}
        if not dependencies()[spec['engine']]['available']:
            raise DatabaseError('数据库驱动或加密组件未安装，请按数据库指南安装依赖。', 503)
        with self.lock:
            now = time.time()
            self.jobs = {k: v for k, v in self.jobs.items() if v['state'] == 'running' or now - v['created_at'] < 300}
            running = [j for j in self.jobs.values() if j['state'] == 'running']
            if len(running) >= 4 or any(j['owner'] == owner for j in running):
                raise DatabaseError('已有查询正在运行，请等待完成或取消后再执行。', 409)
            if len(self.jobs) >= 32:
                oldest = next((k for k, v in self.jobs.items() if v['state'] != 'running'), None)
                if oldest:
                    del self.jobs[oldest]
            key = secrets.token_hex(12)
            cancel = threading.Event()
            session = Session(spec, cancel)
            self.jobs[key] = {'id': key, 'owner': owner, 'created_at': now, 'state': 'running',
                              'operation': operation, 'cancel': cancel, 'session': session}
            self._schedule_expiry()
            EXECUTOR.submit(self.worker, key, session, operation, data)
        return self.job(owner, key)

    def worker(self, key, session, operation, data):
        started = time.monotonic()
        try:
            with session.opened() as connection:
                if operation == 'test':
                    result = connection.query('SELECT 1 AS connection_test')
                    result['detail'] = '连接成功，只读事务已建立。'
                elif operation == 'catalog':
                    result = connection.objects(session.spec['database'])
                elif operation == 'structure':
                    result = connection.structure(data['schema'], data['table'])
                elif operation == 'browse':
                    result = connection.browse(data)
                elif operation == 'relationships':
                    result = database_schema.relationships(connection, data['schema'])
                else:
                    result = connection.query('SELECT * FROM (' + data['sql'] + ') AS panel_readonly_result LIMIT 201')
                result['access'] = connection.access
            state, error = 'succeeded', ''
        except Exception as exc:
            result, state = None, 'failed'
            code = str(getattr(exc, 'pgcode', '') or '')
            number = exc.args[0] if exc.args and type(exc.args[0]) is int else None
            if isinstance(exc, DatabaseError):
                error = str(exc)
            elif code == '57014' or number in {3024, 1969, 2013} or isinstance(exc, (TimeoutError, socket.timeout)):
                error = '查询超时或连接已中断，请缩小查询范围并检查网络。'
            elif code in {'28P01', '28000'} or number in {1044, 1045}:
                error = '数据库认证失败或账号没有访问权限。请检查密码、数据库来源地址授权和认证方式；SSH 的 root 登录不等于数据库 root 可通过 TCP 登录。'
            else:
                # Driver errors may embed passwords, SQL literals or returned data.
                error = '数据库操作失败，请检查地址、账号权限、TLS 证书和查询字段；服务器错误详情不回显。'
        with self.lock:
            job = self.jobs.get(key)
            if job:
                if job['cancel'].is_set():
                    state, result, error = 'cancelled', None, '查询已取消。'
                job.update(state=state, result=result, error=error,
                           elapsed_ms=round((time.monotonic() - started) * 1000))
                job.pop('session', None)

    def job(self, owner, key):
        with self.lock:
            job = self.jobs.get(key) if isinstance(key, str) else None
            if not job or job['owner'] != owner:
                raise DatabaseError('查询任务不存在或已过期。', 404)
            if job['state'] != 'running' and time.time() - job['created_at'] >= 300:
                del self.jobs[key]
                raise DatabaseError('查询结果已过期，请重新执行。', 404)
            return {k: v for k, v in job.items() if k not in {'owner', 'session', 'cancel'}}

    def cancel(self, owner, key):
        with self.lock:
            self.job(owner, key)
            job = self.jobs[key]
            if job['state'] == 'running' and not job['cancel'].is_set():
                job['cancel'].set()
                threading.Thread(target=job['session'].interrupt, daemon=True, name='database-cancel').start()
        return self.job(owner, key)


def discover():
    """Inspect only names, image identities, published ports and network addresses."""
    try:
        result = subprocess.run(['docker', 'ps', '--format', '{{.ID}}\t{{.Names}}\t{{.Image}}'],
                                capture_output=True, text=True, timeout=5)
        if result.returncode or len(result.stdout) > 1024**2:
            raise OSError()
        candidates = []
        deadline = time.monotonic() + 7
        for line in result.stdout.splitlines()[:200]:
            parts = line.split('\t')
            if len(parts) != 3:
                continue
            identity, name, image = parts
            engine = 'postgres' if re.search(r'(^|[/_-])postgres(?:ql)?([:@/_-]|$)', image, re.I) else 'mysql' if re.search(r'(^|[/_-])(mysql|mariadb)([:@/_-]|$)', image, re.I) else ''
            if not engine or not re.fullmatch(r'[a-f0-9]{12,64}', identity):
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            default = '5432' if engine == 'postgres' else '3306'
            # Do not read Config.Env, which commonly contains application credentials.
            query = subprocess.run(['docker', 'inspect', '--format',
                '{{json .NetworkSettings.Ports}}\t{{json .NetworkSettings.Networks}}', identity],
                capture_output=True, text=True, timeout=min(3, remaining))
            if query.returncode or len(query.stdout) > 65536:
                continue
            ports_text, networks_text = query.stdout.strip().split('\t', 1)
            ports, networks = json.loads(ports_text) or {}, json.loads(networks_text) or {}
            bindings = ports.get(default + '/tcp') or []
            targets = [(('::1' if ':' in item.get('HostIp', '') else '127.0.0.1')
                        if item.get('HostIp', '') in {'', '0.0.0.0', '::'} else item['HostIp'], int(item['HostPort']))
                       for item in bindings if item.get('HostPort')]
            if not targets:
                targets = [(v.get('IPAddress'), int(default)) for v in networks.values() if v.get('IPAddress')]
            for host, port in targets[:3]:
                ipaddress.ip_address(host)
                candidates.append({'name': name, 'engine': engine, 'host': host, 'port': port,
                                   'database': 'postgres' if engine == 'postgres' else '', 'tls': False})
            if len(candidates) >= 30:
                break
        return {'candidates': candidates, 'detail': '仅发现连接地址；账号、密码和可达性需要另行确认。'}
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError):
        return {'candidates': [], 'detail': '未能读取本机 Docker 数据库地址，可手动填写；不会读取业务配置或密码。'}


def owner(session):
    return hashlib.sha256(session.encode()).hexdigest()
