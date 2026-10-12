import http.client
import json

import agent
from performance_metrics import ContainerHistory
from test_deployment_removal import http_server as http_server, runtime as runtime


def test_maintenance_and_history_require_login_and_csrf(http_server, monkeypatch, runtime):
    for method, path in (('GET', '/panel-maintenance'), ('POST', '/panel-maintenance'),
                         ('POST', '/panel-maintenance/check'), ('GET', '/performance/containers?id=' + 'a' * 64)):
        assert http_server(method, path, authenticated=False)[0] == 401
    calls = []
    class Manager:
        def status(self):
            return {'supported': True}
        def start(self, data):
            calls.append(data)
            return {'job': 'abc'}
    monkeypatch.setattr(agent, '_panel_maintenance', Manager)
    assert http_server('GET', '/panel-maintenance')[1]['csrf_token']
    connection = http.client.HTTPConnection(http_server.origin.split('//')[1])
    body = json.dumps({'action': 'backup', 'confirm': True})
    connection.request('POST', '/panel-maintenance', body, {'Cookie': f'{agent.COOKIE_NAME}={http_server.cookie}'})
    assert connection.getresponse().status == 403
    connection.close()
    assert not calls
    assert http_server('POST', '/panel-maintenance', {'action': 'backup', 'confirm': True})[0] == 202
    assert len(calls) == 1
    history = ContainerHistory(runtime)
    monkeypatch.setattr(agent, '_performance_history', lambda: history)
    assert http_server('GET', '/performance/containers?id=' + 'a' * 64)[0] == 200
    assert http_server('GET', '/performance/containers?id=../app')[0] == 400
    assert http_server('GET', '/health')[1]['release'] == agent.RUNNING_RELEASE
