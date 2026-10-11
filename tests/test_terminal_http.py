"""Explicit HTTP grants stay bound to the login and do not weaken other gates."""
import http.client
import json
from http.cookies import SimpleCookie

import agent
import test_terminal_ai as ai_tests
from test_ssh_terminal import http_server as http_server, runtime as runtime, manager as manager, peer as peer

ai_manager = ai_tests.manager
model_server = ai_tests.model_server


def request(server, path, *, method='GET', data=None, cookie=None, grant='', csrf=True, origin=True):
    login = server.cookie if cookie is None else cookie
    host = 'mini-deploy.test:' + server.origin.rsplit(':', 1)[1]
    fields = {'Host': host}
    if login:
        fields['Cookie'] = f'{agent.COOKIE_NAME}={login}; {agent.TERMINAL_HTTP_COOKIE}={grant}'
    if csrf:
        fields['X-CSRF-Token'] = agent._csrf_token(login)
    if origin:
        fields['Origin'] = 'http://' + host
    body = json.dumps(data or {}).encode()
    fields['Content-Length'] = str(len(body))
    conn = http.client.HTTPConnection(server.origin.removeprefix('http://'), timeout=5)
    conn.connect()
    head = f'{method} {path} HTTP/1.1\r\n' + ''.join(f'{k}: {v}\r\n' for k, v in fields.items())
    conn.sock.sendall(head.encode() + b'\r\n' + body)
    response = http.client.HTTPResponse(conn.sock)
    response.begin()
    raw = response.read()
    result = response.status, json.loads(raw) if raw else {}, response.getheaders()
    conn.close()
    return result


def test_http_confirmation_requires_auth_csrf_origin_and_explicit_consent(http_server, manager):
    path = '/ssh/allow-http'
    assert request(http_server, path, method='POST', data={'consent': True}, cookie='')[0] == 401
    assert request(http_server, path, method='POST', data={'consent': True}, csrf=False)[0] == 403
    assert request(http_server, path, method='POST', data={'consent': True}, origin=False)[0] == 403
    assert request(http_server, path, method='POST', data={'consent': 'true'})[0] == 400
    status = request(http_server, '/ssh')[1]
    assert status['secure'] is False and status['http_enabled'] is False
    assert request(http_server, '/ssh/probe', method='POST')[0] == 403
    assert not manager.tickets


def test_http_grant_allows_ssh_and_ai_but_not_other_login_or_forged_cookie(http_server, manager, peer, ai_manager, model_server):
    status, _, headers = request(http_server, '/ssh/allow-http', method='POST', data={'consent': True})
    assert status == 200
    jar = SimpleCookie()
    jar.load(dict(headers)['Set-Cookie'])
    grant = jar[agent.TERMINAL_HTTP_COOKIE].value
    assert jar[agent.TERMINAL_HTTP_COOKIE]['httponly'] and jar[agent.TERMINAL_HTTP_COOKIE]['samesite'] == 'Strict'
    access = request(http_server, '/ssh', grant=grant)[1]
    assert access['http_enabled'] is True and access['secure'] is False
    assert request(http_server, '/ssh', grant='forged')[1]['http_enabled'] is False
    assert request(http_server, '/ssh', grant=grant, cookie=agent._make_session_cookie())[1]['http_enabled'] is False
    assert request(http_server, '/ssh/probe', method='POST', data=peer['spec'], grant=grant, csrf=False)[0] == 403
    assert request(http_server, '/ssh/probe', method='POST', data=peer['spec'], grant=grant, origin=False)[0] == 403
    assert request(http_server, '/ssh/probe', method='POST', data=peer['spec'], grant=grant)[0] == 200
    assert request(http_server, '/terminal-ai/settings', grant=grant)[1]['http_enabled'] is True
    assert request(http_server, '/terminal-ai/settings', method='POST', data=ai_tests.config(model_server), grant=grant)[0] == 200
    assert request(http_server, '/terminal-ai/test', method='POST', data=ai_tests.config(model_server), grant=grant)[0] == 200
    assert request(http_server, '/terminal-ai/settings', method='POST', data=ai_tests.config(model_server))[0] == 403
    assert request(http_server, '/terminal-ai/settings', method='POST', data=ai_tests.config(model_server), grant='forged')[0] == 403
    _, _, headers = request(http_server, '/logout', grant=grant)
    assert any(k == 'Set-Cookie' and agent.TERMINAL_HTTP_COOKIE in v and 'Max-Age=0' in v for k, v in headers)
