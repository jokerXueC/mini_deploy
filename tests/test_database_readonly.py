"""Read-only policy, credential isolation and bounded database jobs."""
from contextlib import contextmanager
from decimal import Decimal
import json
import threading
import time
from types import SimpleNamespace

import pytest

import database_readonly as db
from database_sql import DatabaseError, select_sql


def profile(**extra):
    return dict(name='Test', engine='postgres', host='127.0.0.1', port=55432,
                user='panel_reader', password='private-test-password', database='example', tls=False, **extra)


@pytest.mark.parametrize('engine', ['postgres', 'mysql'])
@pytest.mark.parametrize('sql', [
    'SELECT 1', 'SELECT id, COUNT(*) FROM orders GROUP BY id HAVING COUNT(*) > 1',
    'WITH sample AS (SELECT id FROM orders) SELECT * FROM sample LIMIT 20',
    'SELECT COALESCE(name, \'unknown\'), CAST(id AS TEXT) FROM orders',
    'SELECT CASE WHEN id > 1 THEN 2 ELSE 3 END FROM orders',
    'SELECT ROW_NUMBER() OVER (ORDER BY id) FROM orders',
    'SELECT id FROM orders UNION ALL SELECT id FROM archived',
])
def test_safe_selects(engine, sql):
    assert select_sql(sql, engine)


@pytest.mark.parametrize('sql', [
    'DELETE FROM orders', 'UPDATE orders SET id=1', 'DROP TABLE orders',
    'SELECT 1; SELECT 2', 'SELECT 1 INTO new_table', 'SELECT * FROM orders FOR UPDATE',
    "SELECT * INTO OUTFILE '/tmp/export' FROM orders", 'SELECT id INTO @value FROM orders',
    'WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d',
    "SELECT pg_read_file('/etc/passwd')", "SELECT load_file('/etc/passwd')",
    "SELECT dblink('host=somewhere', 'SELECT 1')", 'SELECT pg_sleep(10)',
    'SELECT SLEEP(10)', "SELECT set_config('transaction_read_only','off',false)",
    'SELECT nextval(\'sequence\')', 'SELECT my_schema.count(id) FROM orders',
    'SELECT * FROM generate_series(1,100)', "SELECT CAST('orders' AS REGCLASS)",
    'SELECT 1 /* comment */', 'SELECT /*!50000 SLEEP(5) */ 1',
    'EXPLAIN SELECT 1', 'CALL anything()', 'SET TRANSACTION READ WRITE',
    'SELECT @value', 'SELECT @@version', "SELECT * FROM read_csv('/tmp/a')",
])
def test_unsafe_sql_rejected(sql):
    for engine in ('postgres', 'mysql'):
        with pytest.raises(DatabaseError):
            select_sql(sql, engine)


def test_profiles_encrypt_reuse_revision_and_lost_key(tmp_path):
    store = db.Profiles(tmp_path)
    saved = store.save(profile())
    assert 'password' not in saved and saved['password_saved']
    assert b'private-test-password' not in store.path.read_bytes()
    assert store.get(saved['id'])['password'] == 'private-test-password'
    update = {**saved, 'password': '', 'name': 'Renamed'}
    second = store.save(update)
    assert store.get(second['id'])['password'] == 'private-test-password'
    with pytest.raises(DatabaseError, match='变化'):
        store.save(update)
    with pytest.raises(DatabaseError, match='重新填写密码'):
        store.draft({**second, 'host': 'other-server', 'password': ''})
    original = store.path.read_bytes()
    store.key.unlink()
    with pytest.raises(DatabaseError, match='密钥缺失'):
        store.get(second['id'])
    assert store.path.read_bytes() == original and not store.key.exists()


def test_rejected_command_never_logs_sql_literals(caplog):
    with pytest.raises(DatabaseError):
        select_sql("VACUUM secret_literal_do_not_log", 'postgres')
    assert 'secret_literal_do_not_log' not in caplog.text


def test_idle_results_are_actively_expired(tmp_path):
    manager = db.Manager(tmp_path)
    manager.jobs['expired'] = dict(state='succeeded', created_at=time.time() - 301, result={'private': 'result'})
    manager._expire_jobs()
    assert manager.jobs == {} and manager._expiry_timer is None


def test_private_key_is_not_accepted_as_ca():
    with pytest.raises(DatabaseError, match='CA'):
        db.normalize({**profile(), 'ca_pem': '-----BEGIN CERTIFICATE-----\n-----BEGIN PRIVATE KEY-----'})


class Cursor:
    def __init__(self, rows, columns=('id',)):
        self.rows = iter(rows)
        self.description = [(c,) for c in columns]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, params):
        self.sql, self.params = sql, params

    def fetchone(self):
        return next(self.rows, None)


def test_results_limit_and_types(monkeypatch):
    session = db.Session(profile(), threading.Event())
    cursor = Cursor([(i, Decimal('1.234'), None, b'abc') for i in range(60)], ('id', 'amount', 'optional', 'binary'))
    session.connection = SimpleNamespace(cursor=lambda: cursor)
    result = session.query('SELECT fixture', maximum=50)
    assert result['returned_rows'] == 50 and result['truncation_reason'] == 'rows'
    assert result['rows'][0] == [0, '1.234', None, '[二进制 3 字节]']
    monkeypatch.setattr(db, 'RESULT_BYTES', 10)
    session.connection = SimpleNamespace(cursor=lambda: Cursor([('long content',)]))
    assert session.query('SELECT fixture')['truncation_reason'] == 'bytes'


def test_browse_parameterizes_filters_and_never_skips_byte_limited_rows(monkeypatch):
    session = db.Session(profile(), threading.Event())
    session.columns = lambda *_: {'rows': [['id', 'integer'], ['name', 'text']]}
    calls = []

    def query(sql, params, **kwargs):
        calls.append((sql, params))
        return dict(columns=['id', 'name'], rows=[[1, 'x']], truncated=True, truncation_reason='bytes')

    session.query = query
    result = session.browse(dict(schema='public', table='odd"table', filter_column='name',
                                 filter_value="x' OR 1=1 --%_!", filter_operator='contains', page=2))
    assert '"odd""table"' in calls[0][0] and "OR 1=1" not in calls[0][0]
    assert calls[0][1] == ("%x' OR 1=1 --!%!_!!%", 51, 100)
    assert not result['has_more'] and result['truncated']
    with pytest.raises(DatabaseError):
        session.browse(dict(schema='public', table='orders', sort='id; DELETE FROM orders'))


def wait_job(manager, owner, key):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = manager.job(owner, key)
        if job['state'] != 'running':
            return job
        threading.Event().wait(.01)
    pytest.fail('database job did not finish')


def test_jobs_isolate_owners_cancel_and_sanitize_failures(tmp_path, monkeypatch):
    manager = db.Manager(tmp_path)
    saved = manager.profiles.save(profile())
    entered = threading.Event()

    @contextmanager
    def opened(session):
        entered.set()
        assert session.cancelled.wait(3)
        raise RuntimeError('password=secret SQL=private')
        yield session

    monkeypatch.setattr(db.Session, 'opened', opened)
    job = manager.start('owner-a', dict(id=saved['id'], operation='catalog'))
    assert entered.wait(1)
    with pytest.raises(DatabaseError) as missing:
        manager.job('owner-b', job['id'])
    assert missing.value.status == 404
    with pytest.raises(DatabaseError) as running:
        manager.start('owner-a', dict(id=saved['id'], operation='catalog'))
    assert running.value.status == 409
    with pytest.raises(DatabaseError):
        manager.cancel('owner-b', job['id'])
    manager.cancel('owner-a', job['id'])
    result = wait_job(manager, 'owner-a', job['id'])
    assert result['state'] == 'cancelled' and 'secret' not in json.dumps(result)

    @contextmanager
    def failing(session):
        raise RuntimeError('password=secret SQL=private')
        yield session

    monkeypatch.setattr(db.Session, 'opened', failing)
    job = manager.start('owner-a', dict(id=saved['id'], operation='catalog'))
    result = wait_job(manager, 'owner-a', job['id'])
    assert result['state'] == 'failed' and 'secret' not in json.dumps(result)


def test_discovery_never_reads_environment(monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[1] == 'ps':
            return SimpleNamespace(returncode=0, stdout='abcdef123456\tdatabase\tpostgres:17\n')
        return SimpleNamespace(returncode=0, stdout=json.dumps({'5432/tcp': [{'HostIp': '::', 'HostPort': '55432'}]}) + '\t{}')

    monkeypatch.setattr(db.subprocess, 'run', run)
    assert db.discover()['candidates'][0]['host'] == '::1'
    assert len(commands) == 2 and all('Env' not in ' '.join(c) for c in commands)
