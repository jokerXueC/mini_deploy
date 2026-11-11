"""Database endpoints require panel authentication, CSRF and transport consent."""
from contextlib import nullcontext
import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

import agent
from test_database_readonly import profile
from test_deployment_removal import runtime as runtime


@pytest.fixture
def api(runtime, monkeypatch):
    monkeypatch.setattr(agent, 'UI_PASSWORD_HASH', agent._hash_password('fixture-panel-password'))
    monkeypatch.setattr(agent, 'UI_SESSION_SECRET', 'a' * 64)
    monkeypatch.setattr(agent, '_database_manager_instance', None)
    monkeypatch.setattr(agent, '_maintenance_shared_lock', nullcontext)
    monkeypatch.setattr(agent.Handler, '_ssh_secure', lambda _: False)
    server = ThreadingHTTPServer(('127.0.0.1', 0), agent.Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    session = agent._make_session_cookie()
    session_cookie = f'{agent.COOKIE_NAME}={session}'

    def request(method, path, payload=None, *, authenticated=True, csrf=True, origin=True, grant=''):
        client = http.client.HTTPConnection(*server.server_address, timeout=5)
        body = json.dumps(payload or {}).encode()
        host = f'127.0.0.1:{server.server_port}'
        headers = {'Host': host, 'Content-Length': str(len(body))}
        if authenticated:
            headers['Cookie'] = session_cookie + ('; ' + grant if grant else '')
        if csrf:
            headers['X-CSRF-Token'] = agent._csrf_token(session)
        if origin:
            headers['Origin'] = f'http://{host}' if origin is True else origin
        head = f'{method} {path} HTTP/1.1\r\n' + ''.join(f'{k}: {v}\r\n' for k, v in headers.items())
        client.connect()
        client.sock.sendall(head.encode() + b'\r\n' + body)
        response = http.client.HTTPResponse(client.sock)
        response.begin()
        result = response.status, json.loads(response.read()), dict(response.getheaders())
        client.close()
        return result

    yield request
    server.shutdown()
    server.server_close()
    worker.join(timeout=5)


def test_database_auth_origin_csrf_and_distinct_http_grant(api, monkeypatch):
    audit = []
    monkeypatch.setattr(agent, '_audit_event', lambda *args, **kwargs: audit.append((args, kwargs)))
    assert api('GET', '/databases', authenticated=False)[0] == 401
    assert api('POST', '/databases', profile(), authenticated=False)[0] == 401
    status, snapshot, _ = api('GET', '/databases')
    assert status == 200 and not snapshot['secure'] and not snapshot['http_enabled']
    assert api('POST', '/databases', {**profile(), 'action': 'save'}, csrf=False)[0] == 403
    assert api('POST', '/databases', {**profile(), 'action': 'save'}, origin=False)[0] == 403
    assert api('POST', '/databases', {**profile(), 'action': 'save'}, origin='https://other.test')[0] == 403
    assert api('POST', '/databases', {**profile(), 'action': 'save'})[0] == 403
    assert api('POST', '/databases/allow-http', {'consent': False})[0] == 400
    status, _, headers = api('POST', '/databases/allow-http', {'consent': True})
    assert status == 200
    grant = headers['Set-Cookie'].split(';')[0]
    assert grant.startswith(agent.DATABASE_HTTP_COOKIE + '=') and 'HttpOnly' in headers['Set-Cookie']
    status, saved, _ = api('POST', '/databases', {**profile(), 'action': 'save'}, grant=grant)
    assert status == 200 and 'password' not in saved['connection']
    status, snapshot, _ = api('GET', '/databases', grant=grant)
    assert status == 200 and snapshot['http_enabled'] and len(snapshot['connections']) == 1
    assert 'private-test-password' not in json.dumps(snapshot) + json.dumps(audit)
    assert api('GET', '/databases/job?id=missing', grant=grant)[0] == 404
    assert api('POST', '/databases/query', dict(id=saved['connection']['id'], operation='query',
                                              sql='DELETE FROM orders'), grant=grant)[0] == 400
    assert api('POST', '/databases', dict(action='remove', id=saved['connection']['id'],
                                        revision=saved['connection']['revision']), grant=grant)[0] == 200
    assert agent._database_manager().snapshot()['connections'] == []
