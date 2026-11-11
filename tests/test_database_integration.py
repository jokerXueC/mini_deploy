"""Integration against disposable local instances, never existing database ports."""
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time

import pytest

from database_readonly import Session
from database_sql import DatabaseError, select_sql
import database_schema


def binary(name, windows_directory):
    found = shutil.which(name)
    candidate = Path(windows_directory) / (name + '.exe')
    if not found and candidate.exists():
        found = str(candidate)
    if not found:
        pytest.skip(f'{name} is not installed for disposable integration tests')
    if os.name == 'posix' and os.geteuid() == 0:
        pytest.skip('Disposable database servers must run as an unprivileged test user')
    return found


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def seed_relations(cursor, schema, engine):
    for sql in [
        f'CREATE TABLE {schema}.customers (id integer PRIMARY KEY, name varchar(80))',
        f'CREATE TABLE {schema}.order_links (order_id integer, customer_id integer, PRIMARY KEY(order_id,customer_id), '
        f'CONSTRAINT link_order FOREIGN KEY(order_id) REFERENCES {schema}.orders(id), '
        f'CONSTRAINT link_customer FOREIGN KEY(customer_id) REFERENCES {schema}.customers(id))',
        f'CREATE TABLE {schema}.order_items (order_id integer, line_no integer, PRIMARY KEY(order_id,line_no), '
        f'CONSTRAINT item_order FOREIGN KEY(order_id) REFERENCES {schema}.orders(id))',
        f'CREATE TABLE {schema}.shipments (id integer PRIMARY KEY, order_id integer, line_no integer, '
        f'CONSTRAINT shipment_item FOREIGN KEY(order_id,line_no) REFERENCES {schema}.order_items(order_id,line_no))',
        f'CREATE TABLE {schema}.categories (id integer PRIMARY KEY, parent_id integer, '
        f'CONSTRAINT category_parent FOREIGN KEY(parent_id) REFERENCES {schema}.categories(id))',
        f'CREATE VIEW {schema}.order_totals AS SELECT COUNT(*) AS total FROM {schema}.orders',
    ]:
        cursor.execute(sql)
    cursor.execute('CREATE SCHEMA hidden_fixture' if engine == 'postgres' else 'CREATE DATABASE hidden_fixture')
    cursor.execute('CREATE TABLE hidden_fixture.secrets (id integer PRIMARY KEY)')
    cursor.execute(f'CREATE TABLE {schema}.hidden_link (id integer PRIMARY KEY, '
                   'CONSTRAINT outside_scope FOREIGN KEY(id) REFERENCES hidden_fixture.secrets(id))')


@pytest.fixture(scope='module')
def postgres_spec(tmp_path_factory):
    directory = tmp_path_factory.mktemp('isolated-postgres')
    windows = 'C:/Program Files/PostgreSQL/17/bin'
    initdb, pg_ctl = binary('initdb', windows), binary('pg_ctl', windows)
    data, port = directory / 'data', free_port()
    subprocess.run([initdb, '-D', str(data), '-U', 'fixture_admin', '-A', 'trust', '-E', 'UTF8', '--locale=C'],
                   check=True, capture_output=True, timeout=40)
    # Windows server children inherit pipe handles; use a file to avoid hanging communicate().
    with (directory / 'control.log').open('wb') as control:
        subprocess.run([pg_ctl, '-D', str(data), '-l', str(directory / 'postgres.log'), '-w',
                        '-o', f'-h 127.0.0.1 -p {port} -F', 'start'], check=True,
                       stdout=control, stderr=control, timeout=30)
    try:
        import psycopg2
        with psycopg2.connect(host='127.0.0.1', port=port, user='fixture_admin', dbname='postgres') as admin:
            with admin.cursor() as cursor:
                cursor.execute("CREATE ROLE panel_reader LOGIN PASSWORD 'fixture-only'")
                cursor.execute('CREATE TABLE public.orders (id integer PRIMARY KEY, name text, total numeric(12,2), optional text)')
                cursor.execute("INSERT INTO public.orders SELECT n, 'order-' || n, n * 1.25, NULL FROM generate_series(1,123) n")
                seed_relations(cursor, 'public', 'postgres')
                cursor.execute('GRANT USAGE ON SCHEMA public TO panel_reader')
                cursor.execute('GRANT SELECT ON ALL TABLES IN SCHEMA public TO panel_reader')
        yield dict(name='Isolated PG', engine='postgres', host='127.0.0.1', port=port, user='panel_reader',
                   password='fixture-only', database='postgres', tls=False, ca_pem='')
    finally:
        subprocess.run([pg_ctl, '-D', str(data), '-w', '-m', 'immediate', 'stop'],
                       check=True, capture_output=True, timeout=20)


@pytest.fixture(scope='module')
def mysql_spec(tmp_path_factory):
    directory = tmp_path_factory.mktemp('isolated-mysql')
    mysqld = binary('mysqld', 'C:/Program Files/MySQL/MySQL Server 8.0/bin')
    data, port = directory / 'data', free_port()
    subprocess.run([mysqld, '--no-defaults', '--initialize-insecure', f'--datadir={data}'],
                   check=True, capture_output=True, timeout=60)
    log = (directory / 'console.log').open('wb')
    process = subprocess.Popen([mysqld, '--no-defaults', f'--datadir={data}', f'--port={port}',
                                '--bind-address=127.0.0.1', '--mysqlx=OFF', '--skip-log-bin', '--console'],
                               stdout=log, stderr=log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    admin = None
    try:
        import pymysql
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                admin = pymysql.connect(host='127.0.0.1', port=port, user='root', connect_timeout=1, autocommit=True)
                break
            except pymysql.Error:
                if process.poll() is not None:
                    pytest.fail('Isolated MySQL exited: ' + (directory / 'console.log').read_text(errors='replace'))
                threading.Event().wait(.1)
        assert admin is not None, 'Isolated MySQL did not start'
        with admin.cursor() as cursor:
            cursor.execute('CREATE DATABASE fixture')
            cursor.execute("CREATE USER 'panel_reader'@'127.0.0.1' IDENTIFIED BY 'fixture-only'")
            cursor.execute('CREATE TABLE fixture.orders (id integer PRIMARY KEY, name text, total decimal(12,2), optional text)')
            cursor.executemany('INSERT INTO fixture.orders VALUES (%s,%s,%s,NULL)', [(n, f'order-{n}', n * 1.25) for n in range(1, 124)])
            seed_relations(cursor, 'fixture', 'mysql')
            cursor.execute("GRANT SELECT, SHOW VIEW ON fixture.* TO 'panel_reader'@'127.0.0.1'")
        yield dict(name='Isolated MySQL', engine='mysql', host='127.0.0.1', port=port, user='panel_reader',
                   password='fixture-only', database='fixture', tls=False, ca_pem='')
    finally:
        if admin:
            try:
                with admin.cursor() as cursor:
                    cursor.execute('SHUTDOWN')
            except Exception:
                pass
            admin.close()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=10)
        log.close()


@contextmanager
def opened(spec):
    with Session(spec, threading.Event()).opened() as session:
        yield session


@pytest.mark.parametrize('fixture', ['postgres_spec', 'mysql_spec'])
def test_real_metadata_browse_filters_sql_and_readonly(request, fixture):
    spec = request.getfixturevalue(fixture)
    schema = 'public' if spec['engine'] == 'postgres' else 'fixture'
    with opened(spec) as session:
        catalog = session.objects(spec['database'])
        assert any(t['name'] == 'orders' for t in catalog['tables'])
        structure = session.structure(schema, 'orders')
        assert len(structure['fields']['rows']) == 4 and structure['indexes']['rows']
        first = session.browse(dict(schema=schema, table='orders'))
        second = session.browse(dict(schema=schema, table='orders', page=1))
        assert len(first['rows']) == 50 and first['has_more']
        assert first['rows'][0][0] == 1 and second['rows'][0][0] == 51
        last = session.browse(dict(schema=schema, table='orders', page=2))
        assert len(last['rows']) == 23 and not last['has_more']
        filtered = session.browse(dict(schema=schema, table='orders', filter_column='name', filter_value='order-12', filter_operator='equals'))
        assert [r[0] for r in filtered['rows']] == [12]
        sql = select_sql(f'SELECT COUNT(*) AS total FROM {schema}.orders', spec['engine'])
        assert session.query('SELECT * FROM (' + sql + ') AS panel_result LIMIT 201')['rows'] == [[123]]
        with pytest.raises(DatabaseError):
            select_sql(f'DELETE FROM {schema}.orders', spec['engine'])
    # Bypass the parser deliberately: the server transaction and account still deny writes.
    with opened(spec) as session:
        with pytest.raises(Exception):
            session.query(f'UPDATE {schema}.orders SET id=1000 WHERE id=1')
    with opened(spec) as session:
        assert session.query(f'SELECT COUNT(*) FROM {schema}.orders')['rows'] == [[123]]


@pytest.mark.parametrize('fixture', ['postgres_spec', 'mysql_spec'])
def test_real_database_relationships_respect_readonly_visibility(request, fixture):
    spec = request.getfixturevalue(fixture)
    schema = 'public' if spec['engine'] == 'postgres' else 'fixture'
    with opened(spec) as session:
        result = database_schema.relationships(session)
        assert not result['truncated'] and result['read_only']
        tables = {table['name']: table for table in result['tables']}
        assert 'secrets' not in tables and result['omitted_references'] == 1
        assert tables['order_totals']['type'] == 'VIEW'
        keys = {edge['name']: edge for edge in result['relationships']}
        assert len(keys) == 5
        assert keys['shipment_item']['columns'] == ['order_id', 'line_no']
        assert keys['shipment_item']['target_columns'] == ['order_id', 'line_no']
        assert keys['category_parent']['source'] == keys['category_parent']['target']
        fields = {field['name']: field for field in tables['order_links']['columns']}
        assert fields['order_id']['primary_key'] and fields['order_id']['foreign_key']
        filtered = database_schema.relationships(session, schema)
        assert len(filtered['relationships']) == 5


@pytest.mark.parametrize('fixture', ['postgres_spec', 'mysql_spec'])
def test_real_query_cancel(request, fixture):
    spec = request.getfixturevalue(fixture)
    session = Session(spec, threading.Event())
    started, finished, failures = threading.Event(), threading.Event(), []

    def worker():
        try:
            with session.opened():
                started.set()
                # Fixture-only; the public SQL parser rejects sleep functions.
                session.query('SELECT pg_sleep(10)' if spec['engine'] == 'postgres' else 'SELECT SLEEP(10)')
        except Exception as error:
            failures.append(error)
        finally:
            finished.set()

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert started.wait(5)
        threading.Event().wait(.1)
        session.cancelled.set()
        session.interrupt()
        assert finished.wait(3) and failures
    finally:
        session.interrupt()
        thread.join(timeout=15)
