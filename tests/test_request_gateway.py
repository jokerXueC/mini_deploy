import http.client
import json
import subprocess
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import agent
import request_gateway as gateway
from certificates import CertificateError


def spec(**changes):
    return gateway.normalize({"key": "api", "name": "API", "upstream": "http://api:8000",
                              "network": "app_default", **changes})


@pytest.fixture
def store(tmp_path):
    return gateway.Store(tmp_path)


@pytest.mark.parametrize("changes", [
    {"key": "../outside"}, {"key": "bad;id"}, {"port": 6868}, {"port": True}, {"port": 80},
    {"bind": "example.com"}, {"bind": []}, {"network": "bridge"}, {"image": "--privileged"},
    {"upstream": "http://api:8000/foo"}, {"upstream": "http://u:secret@api:80"},
    {"upstream": "http://api:80?token=secret"}, {"upstream": "http://api:80#x"},
    {"upstream": "http://api:65536"}, {"upstream": "http://api:0"}, {"upstream": "https://api"},
    {"upstream": "http://127.0.0.1:80"}, {"upstream": "http://localhost:80"},
    {"upstream": "http://mini-gateway-api:10000"},
    {"network": "host", "upstream": "http://127.0.0.1:18080"},
    {"upstream": "http://api;return:80"}, {"trust_proxy": "true"},
])
def test_invalid_configuration_never_reaches_runtime(changes):
    with pytest.raises(CertificateError):
        spec(**changes)


def test_generated_config_preserves_streaming_and_limits_log_contents():
    text = gateway.config(spec())
    for required in ("proxy_buffering off", "proxy_request_buffering off", "proxy_cache off",
                     "proxy_next_upstream off", "proxy_http_version 1.1", "proxy_read_timeout 3600s",
                     "Upgrade $http_upgrade", "resolver 127.0.0.11", "proxy_pass $backend;",
                     "fastcgi_temp_path /tmp/", "uwsgi_temp_path /tmp/", "scgi_temp_path /tmp/"):
        assert required in text
    for secret_field in ("$request_uri", "$args", "$request_body", "$http_cookie", "$http_authorization"):
        assert secret_field not in text
    assert "X-Forwarded-Proto $scheme" in text
    trusted = gateway.config(spec(trust_proxy=True))
    assert "X-Forwarded-Proto $gateway_proto" in trusted
    host = gateway.config(spec(network="host", upstream="http://127.0.0.1:8000"))
    assert "listen 127.0.0.1:18080" in host
    assert "proxy_pass http://127.0.0.1:8000;" in host


def test_save_is_a_draft_and_rejects_stale_edit(store, monkeypatch):
    monkeypatch.setattr(gateway, "run", lambda *_a, **_kw: pytest.fail("draft must not start Docker"))
    saved = store.save(spec())
    assert saved["state"] == "not_created"
    assert saved["caddy_upstream"] == "mini-gateway-api:10000"
    with pytest.raises(CertificateError, match="配置已变化"):
        store.save(spec(name="new"), "old-revision")


def test_running_update_validates_reloads_and_rolls_back(store, monkeypatch):
    saved = store.save(spec())
    old = (store.directory("api") / "conf/nginx.conf").read_text()
    monkeypatch.setattr(store, "inspect", lambda _: {"Id": "a" * 64, "State": {"Running": True, "Status": "running"}})
    calls = []

    def reload(_id):
        calls.append((store.directory("api") / "conf/nginx.conf").read_text())
        if len(calls) == 1:
            raise CertificateError("config failed")

    monkeypatch.setattr(store, "reload", reload)
    with pytest.raises(CertificateError, match="恢复"):
        store.save(spec(upstream="http://new-api:8000"), saved["revision"])
    assert "new-api" in calls[0]
    assert calls[1] == old
    assert store.read("api")["spec"]["upstream"] == "http://api:8000"


def test_failed_first_write_is_recoverable(store, monkeypatch):
    original = gateway.atomic_write

    def fail(path, text):
        if path.name == "nginx.conf":
            raise OSError("disk full")
        original(path, text)

    monkeypatch.setattr(gateway, "atomic_write", fail)
    with pytest.raises(CertificateError):
        store.save(spec())
    assert store.entries() == []
    monkeypatch.setattr(gateway, "atomic_write", original)
    assert store.save(spec())["key"] == "api"


def test_container_ownership_is_checked_before_operations(store, monkeypatch):
    store.save(spec())
    monkeypatch.setattr(gateway.nginx_runtime, "require_local_docker", lambda: None)

    def run(command, **kwargs):
        if command[1] == "ps":
            return "a" * 64
        return json.dumps([{"Config": {"Labels": {gateway.LABEL: "wrong"}}}])

    monkeypatch.setattr(gateway, "run", run)
    with pytest.raises(CertificateError, match="不是此面板"):
        store.inspect(store.read("api"))


def test_start_uses_hardened_independent_container(store, monkeypatch):
    store.save(spec())
    monkeypatch.setattr(gateway, "os", SimpleNamespace(name="posix", geteuid=lambda: 0))
    import nginx_install
    monkeypatch.setattr(nginx_install, "_check_ports", lambda ports: None)
    running = {"Id": "a" * 64, "State": {"Status": "running", "Running": True}}
    inspections = iter([None, running])
    monkeypatch.setattr(store, "inspect", lambda _: next(inspections))
    monkeypatch.setattr(store, "wait_listener", lambda port: None)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1:3] == ["network", "inspect"]:
            return '[{"Driver":"bridge"}]'
        return ""

    monkeypatch.setattr(gateway, "run", run)
    store.start(store.read("api"))
    creation = next(command for command in calls if "-d" in command)
    assert creation[creation.index("--restart") + 1] == "unless-stopped"
    assert "--read-only" in creation and "--cap-drop=ALL" in creation
    assert "127.0.0.1:18080:10000" in creation
    assert "max-size=10m" in creation and "max-file=3" in creation
    assert not any("docker.sock" in arg for arg in creation)
    assert any("--rm" in command and "-t" in command for command in calls)


def test_modified_config_cannot_start_existing_container(store, monkeypatch):
    store.save(spec())
    (store.directory("api") / "conf/nginx.conf").write_text("unexpected content")
    monkeypatch.setattr(gateway, "os", SimpleNamespace(name="posix", geteuid=lambda: 0))
    monkeypatch.setattr(gateway, "run", lambda *_a, **_kw: pytest.fail("must not start"))
    with pytest.raises(CertificateError, match="外部修改"):
        store.start(store.read("api"))


def test_delete_running_gateway_is_rejected(store, monkeypatch):
    saved = store.save(spec())
    monkeypatch.setattr(store, "inspect", lambda _: {"Id": "a" * 64, "State": {"Status": "running", "Running": True}})
    monkeypatch.setattr(gateway, "run", lambda *_a, **_kw: pytest.fail("must not remove"))
    with pytest.raises(CertificateError, match="停止"):
        store.operate(dict(action="delete", key="api", revision=saved["revision"], confirmed=True))


def test_interrupted_reload_is_recovered_from_journal(store, monkeypatch):
    saved = store.save(spec())
    old = store.read('api')
    monkeypatch.setattr(store, 'inspect', lambda _: {'Id': 'a' * 64, 'State': {'Status': 'running', 'Running': True}})

    def interrupted(_id):
        raise KeyboardInterrupt()

    monkeypatch.setattr(store, 'reload', interrupted)
    with pytest.raises(KeyboardInterrupt):
        store.save(spec(upstream='http://new-api:8000'), saved['revision'])
    assert (store.directory('api') / 'pending.json').exists()
    assert 'new-api' in (store.directory('api') / 'conf/nginx.conf').read_text()
    monkeypatch.setattr(store, 'reload', lambda _id: None)
    store.recover('api')
    assert store.read('api') == old
    assert (store.directory('api') / 'conf/nginx.conf').read_text() == gateway.config(old['spec'])
    assert not (store.directory('api') / 'pending.json').exists()


def test_committed_update_journal_keeps_new_config(store, monkeypatch):
    saved = store.save(spec())
    old = store.read('api')
    monkeypatch.setattr(store, 'inspect', lambda _: None)
    store.save(spec(upstream='http://new-api:8000'), saved['revision'])
    current = store.read('api')
    gateway.atomic_write(store.directory('api') / 'pending.json', json.dumps({'previous': old, 'next': current}))
    store.recover('api')
    assert store.read('api') == current
    assert (store.directory('api') / 'conf/nginx.conf').read_text() == gateway.config(current['spec'])


def test_paused_recovery_keeps_journal_until_runtime_can_reconcile(store, monkeypatch):
    store.save(spec())
    old = store.read('api')
    new = {**old, 'spec': spec(upstream='http://new-api:8000')}
    directory = store.directory('api')
    gateway.atomic_write(directory / 'pending.json', json.dumps({'previous': old, 'next': new}))
    gateway.atomic_write(directory / 'conf/nginx.conf', gateway.config(new['spec']))
    monkeypatch.setattr(store, 'inspect', lambda _: {'Id': 'a' * 64, 'State': {'Status': 'paused'}})
    monkeypatch.setattr(store, 'reload', lambda _: pytest.fail('cannot reload a paused process'))
    with pytest.raises(CertificateError, match='先停止'):
        store.recover('api')
    assert (directory / 'pending.json').exists()
    monkeypatch.setattr(store, 'inspect', lambda _: {'Id': 'a' * 64, 'State': {'Status': 'exited'}})
    store.recover('api')
    assert not (directory / 'pending.json').exists()
    assert (directory / 'conf/nginx.conf').read_text() == gateway.config(old['spec'])


@pytest.mark.parametrize('status', ['paused', 'restarting', 'removing'])
def test_start_rejects_unstable_container(store, monkeypatch, status):
    store.save(spec())
    monkeypatch.setattr(gateway, 'os', SimpleNamespace(name='posix', geteuid=lambda: 0))
    monkeypatch.setattr(store, 'inspect', lambda _: {'Id': 'a' * 64, 'State': {'Status': status, 'Running': True}})
    monkeypatch.setattr(gateway, 'run', lambda *_a, **_kw: pytest.fail('must not start'))
    with pytest.raises(CertificateError, match='先停止'):
        store.start(store.read('api'))


def test_restart_checks_port_before_starting(store, monkeypatch):
    import nginx_install
    store.save(spec())
    monkeypatch.setattr(gateway, 'os', SimpleNamespace(name='posix', geteuid=lambda: 0))
    monkeypatch.setattr(store, 'inspect', lambda _: {'Id': 'a' * 64, 'State': {'Status': 'exited', 'Running': False}})

    def occupied(_ports):
        raise CertificateError('port occupied')

    monkeypatch.setattr(nginx_install, '_check_ports', occupied)
    monkeypatch.setattr(gateway, 'run', lambda *_a, **_kw: pytest.fail('must not start'))
    with pytest.raises(CertificateError, match='occupied'):
        store.start(store.read('api'))


def test_paused_gateway_can_be_stopped_from_panel(store, monkeypatch):
    saved = store.save(spec())
    monkeypatch.setattr(store, 'inspect', lambda _: {'Id': 'a' * 64, 'State': {'Status': 'paused'}})
    calls = []
    monkeypatch.setattr(gateway, 'run', lambda command, **kwargs: calls.append(command))
    store.operate({'action': 'stop', 'key': 'api', 'revision': saved['revision'], 'confirmed': True})
    assert [command[1] for command in calls] == ['unpause', 'stop']
    assert all(command[-1] == 'a' * 64 for command in calls)


def test_read_records_only_parses_owned_access_lines(store, monkeypatch):
    store.save(spec())
    monkeypatch.setattr(store, "inspect", lambda _: {"Id": "a" * 64})

    def logs(command, **kwargs):
        assert command[-1] == "a" * 64
        assert kwargs["stdout"] != subprocess.PIPE
        kwargs["stdout"].write(b'noise\nmini_deploy_req {"at":"2026-10-07","host":"site.test","method":"GET","path":"/ok","status":200,"duration":0.01}\n')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(gateway.subprocess, "run", logs)
    assert store.records("api")["records"][0]["duration_ms"] == 10


@pytest.mark.parametrize('endpoint', ['/request-gateways', '/gateway-connections'])
def test_gateway_api_requires_auth_csrf_and_confirmation(monkeypatch, tmp_path, endpoint):
    monkeypatch.setattr(agent, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(agent, "UI_SESSION_SECRET", "gateway-test-session")
    monkeypatch.setattr(agent, "_audit_event", lambda *args, **kwargs: None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), agent.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    import gateway_connections
    monkeypatch.setattr(gateway_connections, 'discover', lambda: {'sources': []})
    monkeypatch.setattr(gateway_connections.Connections, 'apply', lambda self, data: {'connection': {'state': 'connected'}})

    def request(method, headers=None, payload=None):
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        try:
            path = endpoint + ('/discover' if endpoint == '/gateway-connections' and method == 'GET' else '')
            connection.request(method, path, body=json.dumps(payload) if payload else None,
                               headers={"Content-Type": "application/json", **(headers or {})})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    try:
        assert request("GET")[0] == 401
        cookie = agent._make_session_cookie()
        headers = {"Cookie": f"{agent.COOKIE_NAME}={cookie}"}
        assert request("GET", headers)[1] == ({"entries": []} if endpoint == '/request-gateways' else {'sources': []})
        payload = {"action": "save", "spec": spec()} if endpoint == '/request-gateways' else {'action': 'apply'}
        assert request("POST", headers, payload)[0] == 403
        headers["X-CSRF-Token"] = agent._csrf_token(cookie)
        assert request("POST", headers, payload)[0] == 400
        payload["confirmed"] = True
        assert request("POST", headers, payload)[0] == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
