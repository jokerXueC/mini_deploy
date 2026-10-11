"""The local runner retains login/CSRF and can connect beyond the demo restriction."""
import http.client
import threading
from types import SimpleNamespace
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

import pytest

import agent
from scripts import local_ssh
from test_ssh_terminal import peer as peer, request


@pytest.fixture
def local_server(tmp_path, monkeypatch):
    for name in ("UI_PASSWORD_HASH", "UI_SESSION_SECRET", "COOKIE_NAME", "COOKIE_SECURE_MODE",
                 "TRUST_LOOPBACK_PROXY_HEADERS", "STATE_FILE", "AUDIT_LOG_FILE", "LOG_FILE", "_ssh_manager_instance"):
        monkeypatch.setattr(agent, name, getattr(agent, name))
    password_path = local_ssh.configure(tmp_path / "local")
    server = ThreadingHTTPServer(("127.0.0.1", 0), local_ssh.Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield SimpleNamespace(origin=f"http://127.0.0.1:{server.server_port}",
        password=password_path.read_text().strip(), cookie="")
    if agent._ssh_manager_instance is not None:
        agent._ssh_manager_instance.shutdown()
    server.shutdown()
    server.server_close()
    worker.join(timeout=3)


def login(server, password):
    conn = http.client.HTTPConnection(server.origin.removeprefix("http://"))
    conn.request("POST", "/login", urlencode({"password": password}),
        {"Content-Type": "application/x-www-form-urlencoded"})
    response = conn.getresponse()
    status, cookie, location = response.status, response.getheader("Set-Cookie"), response.getheader("Location")
    response.read()
    conn.close()
    return status, cookie, location


def test_local_runner_requires_login_and_connects_real_peer(local_server, peer):
    assert request(local_server, "GET", "/ssh", authenticated=False)[0] == 401
    assert login(local_server, "incorrect")[0] == 401
    status, cookie, location = login(local_server, local_server.password)
    assert status == 303 and location == "/ui?view=terminal"
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    local_server.cookie = cookie.split(";", 1)[0].split("=", 1)[1]
    status, payload = request(local_server, "GET", "/ssh")
    assert status == 200 and payload["secure"] and payload["available"]
    assert request(local_server, "POST", "/ssh/probe", peer["spec"], csrf=False)[0] == 403
    status, probe = request(local_server, "POST", "/ssh/probe", peer["spec"])
    assert status == 200
    status, session = request(local_server, "POST", "/ssh/connect", {"ticket": probe["ticket"],
        "accept_host_key": True, "auth": "password", "password": "safe-password"})
    assert status == 200 and peer["auth"]
    assert request(local_server, "POST", "/ssh/disconnect", {"id": session["id"]})[0] == 200
    assert request(local_server, "POST", "/docker/action", {"action": "stop"})[0] == 403


def test_local_configuration_persists_without_replacing_auth(local_server):
    secret, password_hash = agent.UI_SESSION_SECRET, agent.UI_PASSWORD_HASH
    path = local_ssh.configure(agent.STATE_FILE.parent)
    assert path.read_text().strip() == local_server.password
    assert agent.UI_SESSION_SECRET == secret and agent.UI_PASSWORD_HASH == password_hash
