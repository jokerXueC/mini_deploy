"""Exercise real SSH on loopback; the test peer never runs OS commands."""
import base64
import http.client
import io
import json
import socket
import threading
import time

import pytest

import agent
import ssh_terminal as ssh
from test_deployment_removal import http_server as http_server, runtime as runtime

paramiko = pytest.importorskip("paramiko")


@pytest.fixture
def manager(tmp_path, monkeypatch):
    audits = []
    instance = ssh.Manager(tmp_path / "ssh", audit=lambda *event: audits.append(event))
    monkeypatch.setattr(agent, "_ssh_manager_instance", instance)
    instance.audits = audits
    yield instance
    instance.shutdown()


@pytest.fixture
def peer():
    host_key = paramiko.RSAKey.generate(2048)
    user_key = paramiko.RSAKey.generate(2048)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    listener.settimeout(.2)
    done = threading.Event()
    state = {"key": host_key, "auth": [], "input": [], "sizes": [], "transports": [], "channels": [],
             "exec": [], "exec_output": b"mock inspection result\n", "exec_hang": False}

    class Server(paramiko.ServerInterface):
        def check_auth_password(self, username, password):
            state["auth"].append((username, "password"))
            return paramiko.AUTH_SUCCESSFUL if username == "demo" and password == "safe-password" else paramiko.AUTH_FAILED

        def check_auth_publickey(self, username, key):
            state["auth"].append((username, "key"))
            return paramiko.AUTH_SUCCESSFUL if username == "demo" and key == user_key else paramiko.AUTH_FAILED

        def get_allowed_auths(self, username):
            return "password,publickey"

        def check_channel_request(self, kind, channel_id):
            return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

        def check_channel_pty_request(self, channel, term, width, height, *args):
            state["sizes"].append((width, height))
            return True

        def check_channel_window_change_request(self, channel, width, height, *args):
            state["sizes"].append((width, height))
            return True

        def check_channel_shell_request(self, channel):
            threading.Thread(target=echo, args=(channel,), daemon=True).start()
            return True

        def check_channel_exec_request(self, channel, command):
            state["exec"].append(command)
            def respond():
                if state["exec_hang"]:
                    return
                time.sleep(.02)
                try:
                    channel.sendall(state["exec_output"])
                    channel.send_exit_status(0)
                    channel.close()
                except (OSError, EOFError):
                    pass
            threading.Thread(target=respond, daemon=True).start()
            return True

    def echo(channel):
        try:
            channel.sendall("\x1b[32m本地测试终端\x1b[0m\r\ndemo@local:~$ ".encode())
            while not done.is_set():
                data = channel.recv(16384)
                if not data:
                    break
                state["input"].append(data)
                channel.sendall(data)
                if b"\r" in data:
                    channel.sendall(b"\ndemo@local:~$ ")
        except (OSError, EOFError):
            pass

    def handle(sock):
        transport = paramiko.Transport(sock)
        state["transports"].append(transport)
        transport.add_server_key(state["key"])
        try:
            transport.start_server(server=Server())
            while transport.is_active() and not done.wait(.05):
                channel = transport.accept(.05)
                if channel is not None:
                    state["channels"].append(channel)
        except (paramiko.SSHException, OSError, EOFError):
            pass
        finally:
            transport.close()

    def accept():
        while not done.is_set():
            try:
                sock, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=handle, args=(sock,), daemon=True).start()

    worker = threading.Thread(target=accept, daemon=True)
    worker.start()
    state["spec"] = {"host": "127.0.0.1", "port": listener.getsockname()[1], "username": "demo"}
    state["user_key"] = user_key
    yield state
    done.set()
    listener.close()
    for transport in state["transports"]:
        transport.close()
    worker.join(timeout=2)


def connect(manager, peer, owner="owner", **kwargs):
    probe = manager.probe(owner, peer["spec"])
    result = manager.connect(owner, {"ticket": probe["ticket"], "accept_host_key": True,
        "auth": "password", "password": "safe-password", **kwargs}, time.time() + 300)
    return manager.session(owner, result["id"])


def request(http_server, method, path, data=None, *, origin=True, csrf=True, authenticated=True, headers=None):
    address = http_server.origin.removeprefix("http://")
    fields = {"Host": address, **(headers or {})}
    if authenticated:
        fields["Cookie"] = f"{agent.COOKIE_NAME}={http_server.cookie}"
    if csrf:
        fields["X-CSRF-Token"] = agent._csrf_token(http_server.cookie)
    if origin:
        fields["Origin"] = http_server.origin
    body = json.dumps(data or {}).encode()
    fields["Content-Length"] = str(len(body))
    connection = http.client.HTTPConnection(address, timeout=30)
    connection.connect()
    head = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in fields.items())
    connection.sock.sendall(head.encode() + b"\r\n" + body)
    response = http.client.HTTPResponse(connection.sock)
    response.begin()
    result = response.status, json.loads(response.read())
    connection.close()
    return result


@pytest.mark.parametrize("peer,headers,trusted,tls,expected", [
    ("1.2.3.4", {"Host": "example.com"}, False, False, False),
    ("1.2.3.4", {"Host": "example.com", "X-Forwarded-Proto": "https"}, True, False, False),
    ("127.0.0.1", {"Host": "example.com", "X-Forwarded-Proto": "https"}, False, False, False),
    ("127.0.0.1", {"Host": "example.com", "X-Forwarded-Proto": "https"}, True, False, True),
    ("127.0.0.1", {"Host": "localhost:6868"}, False, False, True),
    ("::1", {"Host": "[::1]:6868"}, False, False, True),
    ("127.0.0.1", {"Host": "127.0.0.1", "X-Real-IP": "1.2.3.4"}, False, False, False),
    ("127.0.0.1", {"Host": "127.0.0.1", "Forwarded": "proto=http"}, False, False, False),
    ("127.0.0.1", {"Host": "attacker.example"}, False, False, False),
    ("1.2.3.4", {"Host": "example.com"}, False, True, True),
])
def test_credential_transport_gate(peer, headers, trusted, tls, expected):
    assert ssh.secure_request(peer, headers, trusted_proxy=trusted, tls=tls) is expected


@pytest.mark.parametrize("data", [{"host": "http://example.com", "username": "root"},
    {"host": "server\nproxy", "username": "root"}, {"host": "host", "port": 0, "username": "root"},
    {"host": "host", "username": ""}, {"host": "host", "username": "root\n" + "bad"}])
def test_invalid_destination_rejected(data):
    with pytest.raises(ssh.SSHError):
        ssh.target(data)


def test_fingerprint_verified_before_auth_and_persisted_only_after_success(manager, peer):
    probe = manager.probe("owner", peer["spec"])
    assert probe["state"] == "new" and probe["fingerprint"] == ssh.fingerprint(peer["key"])
    assert not peer["auth"] and not manager.known
    with pytest.raises(ssh.SSHError, match="确认主机指纹"):
        manager.connect("owner", {"ticket": probe["ticket"], "password": "safe-password"}, time.time() + 60)
    assert not peer["auth"]
    session = connect(manager, peer)
    assert manager.probe("owner", peer["spec"])["state"] == "known"
    stored = (manager.directory / "connections.json").read_text()
    assert "safe-password" not in stored and "PRIVATE KEY" not in stored
    restored = ssh.Manager(manager.directory)
    assert restored.known == manager.known
    session.close("test done")


def test_changed_key_before_auth_never_sends_password(manager, peer):
    probe = manager.probe("owner", peer["spec"])
    peer["key"] = paramiko.RSAKey.generate(2048)
    with pytest.raises(ssh.SSHError, match="发送"):
        manager.connect("owner", {"ticket": probe["ticket"], "accept_host_key": True,
            "password": "safe-password"}, time.time() + 60)
    assert not peer["auth"] and not manager.known and not manager.sessions


def test_changed_saved_key_requires_explicit_reverification(manager, peer):
    connect(manager, peer).close("done")
    old = dict(manager.known)
    peer["key"] = paramiko.RSAKey.generate(2048)
    probe = manager.probe("owner", peer["spec"])
    assert probe["state"] == "changed" and probe["expected"] == next(iter(old.values()))
    with pytest.raises(ssh.SSHError):
        manager.connect("owner", {"ticket": probe["ticket"], "password": "safe-password"}, time.time() + 60)
    assert manager.known == old


def test_real_pty_unicode_control_resize_and_owner_isolation(manager, peer):
    session = connect(manager, peer)
    output = session.poll(0, wait=3)
    assert "本地测试终端" in base64.b64decode(output["data"]).decode()
    with pytest.raises(ssh.SSHError) as error:
        manager.session("another", session.id)
    assert error.value.status == 404
    payload = "你好\r\x03\x1b[A".encode()
    session.write(base64.b64encode(payload).decode())
    output = session.poll(output["cursor"], wait=3)
    assert payload in base64.b64decode(output["data"])
    session.resize({"cols": 120, "rows": 40})
    deadline = time.monotonic() + 2
    while (120, 40) not in peer["sizes"] and time.monotonic() < deadline:
        time.sleep(.01)
    assert (120, 40) in peer["sizes"]
    assert b"".join(peer["input"]) == payload
    session.close("test disconnected")
    assert session.poll(output["cursor"], wait=0)["closed"]
    assert [event[0] for event in manager.audits].count("ssh_disconnect") == 1


def test_encrypted_private_key_authentication(manager, peer):
    private = io.StringIO()
    peer["user_key"].write_private_key(private, password="key-secret")
    session = connect(manager, peer, auth="key", private_key=private.getvalue(), passphrase="key-secret")
    assert peer["auth"][-1] == ("demo", "key")
    assert session.poll(0, wait=3)["data"]
    stored = (manager.directory / "connections.json").read_text()
    assert "key-secret" not in stored and "PRIVATE KEY" not in stored


def test_bad_credentials_do_not_persist_or_leak(manager, peer):
    with pytest.raises(ssh.SSHError) as error:
        connect(manager, peer, password="never-log-this-secret")
    assert "never-log-this-secret" not in str(error.value)
    assert not manager.sessions and not manager.known and not manager.connecting


def test_expired_foreign_and_consumed_ticket_cannot_connect(manager, peer):
    probe = manager.probe("owner", peer["spec"])
    payload = {"ticket": probe["ticket"], "accept_host_key": True, "password": "safe-password"}
    with pytest.raises(ssh.SSHError):
        manager.connect("another", payload, time.time() + 60)
    assert probe["ticket"] in manager.tickets
    manager.tickets[probe["ticket"]]["time"] -= 121
    with pytest.raises(ssh.SSHError):
        manager.connect("owner", payload, time.time() + 60)
    assert not peer["auth"]


def test_session_caps_idle_expiry_and_logout(manager, peer):
    sessions = [connect(manager, peer) for _ in range(4)]
    with pytest.raises(ssh.SSHError) as error:
        manager.probe("owner", peer["spec"])
    assert error.value.status == 429
    sessions[0].activity -= 901
    sessions[1].last_poll -= 61
    sessions[2].expires = time.time() - 1
    with manager.lock:
        manager._cleanup()
    assert all(s.closed for s in sessions[:3])
    manager.close_owner("owner")
    assert sessions[3].closed and not manager.tickets


def test_profile_strips_credentials_and_deletion_keeps_host_pin_and_business_files(manager, tmp_path):
    marker = tmp_path / "business.txt"
    marker.write_text("preserve")
    manager.known = {"[host]:22": "SHA256:pin"}
    profile = manager.profile({"action": "save", "host": "host", "username": "root", "name": "<script>name</script>",
        "password": "secret-password", "private_key": "secret-key"})["profiles"][0]
    assert not ({"password", "private_key"} & profile.keys())
    manager.profile({"action": "delete", "id": profile["id"]})
    assert not manager.profiles and manager.known == {"[host]:22": "SHA256:pin"}
    assert marker.read_text() == "preserve"


def test_no_dependency_or_bad_profile_file_leaves_panel_importable(manager, monkeypatch, tmp_path):
    monkeypatch.setattr(ssh, "paramiko", None)
    assert manager.status("owner")["available"] is False
    with pytest.raises(ssh.SSHError, match="依赖"):
        manager.probe("owner", {"host": "host", "username": "root"})
    config = manager.directory / "connections.json"
    manager.directory.mkdir(exist_ok=True)
    config.write_text("broken")
    with pytest.raises(ssh.SSHError, match="原文件已保留"):
        ssh.Manager(manager.directory)
    assert config.read_text() == "broken"


def test_http_auth_csrf_origin_and_public_http_gate(http_server, manager, monkeypatch):
    assert request(http_server, "GET", "/ssh", authenticated=False)[0] == 401
    assert request(http_server, "POST", "/ssh/probe", csrf=False)[0] == 403
    assert request(http_server, "POST", "/ssh/probe", origin=False)[0] == 403
    assert request(http_server, "GET", "/ssh")[1]["secure"]
    monkeypatch.setattr(agent.Handler, "_ssh_secure", lambda _: False)
    result = request(http_server, "POST", "/ssh/connect", {"password": "never-log-this"})
    assert result[0] == 403 and "never-log-this" not in str(result)
    assert not manager.sessions and not manager.tickets


def test_http_pty_roundtrip_and_logout_cleanup(http_server, manager, peer):
    status, probe = request(http_server, "POST", "/ssh/probe", peer["spec"])
    assert status == 200
    status, connection = request(http_server, "POST", "/ssh/connect", {"ticket": probe["ticket"],
        "accept_host_key": True, "password": "safe-password"})
    assert status == 200
    key = connection["id"]
    status, result = request(http_server, "GET", f"/ssh/output?id={key}&cursor=0")
    assert status == 200 and result["data"]
    assert request(http_server, "POST", "/ssh/input", {"id": key, "data": "aGVsbG8="})[0] == 200
    output = request(http_server, "GET", f"/ssh/output?id={key}&cursor={result['cursor']}")[1]
    assert b"hello" in base64.b64decode(output["data"])
    # Logout returns a redirect; inspect it without JSON helper.
    connection = http.client.HTTPConnection(http_server.origin.removeprefix("http://"))
    connection.request("GET", "/logout", headers={"Cookie": f"{agent.COOKIE_NAME}={http_server.cookie}"})
    assert connection.getresponse().status == 303
    connection.close()
    assert list(manager.sessions.values())[0].closed
    assert request(http_server, "GET", "/ssh")[0] == 401


def test_logout_during_handshake_blocks_pending_authentication(manager, peer, monkeypatch):
    probe = manager.probe("owner", peer["spec"])
    original = manager._open
    def open_then_logout(spec):
        result = original(spec)
        manager.close_owner("owner")
        return result
    monkeypatch.setattr(manager, "_open", open_then_logout)
    with pytest.raises(ssh.SSHError, match="退出"):
        manager.connect("owner", {"ticket": probe["ticket"], "accept_host_key": True,
            "password": "safe-password"}, time.time() + 60)
    assert not peer["auth"] and not manager.sessions
    with pytest.raises(ssh.SSHError):
        manager.probe("owner", peer["spec"])
    assert manager.status("new-login")["available"]


def test_legacy_logout_still_redirects_when_ssh_manager_exists(http_server, manager):
    expires = int(time.time()) + 300
    cookie = base64.urlsafe_b64encode(f"{expires}:{agent._session_signature(expires)}".encode()).decode()
    connection = http.client.HTTPConnection(http_server.origin.removeprefix("http://"))
    connection.request("GET", "/logout", headers={"Cookie": f"{agent.COOKIE_NAME}={cookie}"})
    assert connection.getresponse().status == 303
    connection.close()


def test_panel_cannot_be_framed_for_terminal_clickjacking(http_server):
    connection = http.client.HTTPConnection(http_server.origin.removeprefix("http://"))
    connection.request("GET", "/ui", headers={"Cookie": f"{agent.COOKIE_NAME}={http_server.cookie}"})
    response = connection.getresponse()
    assert response.status == 200
    assert response.getheader("X-Frame-Options") == "DENY"
    assert response.getheader("Content-Security-Policy") == "frame-ancestors 'none'"
    response.read()
    connection.close()


def test_bounded_output_and_poll_concurrency():
    class Channel:
        def __init__(self):
            self.chunks = [b"x" * 16384] * 40
        def recv(self, _):
            return self.chunks.pop() if self.chunks else b""
        def close(self):
            pass
    channel = Channel()
    session = ssh.Session("owner", {}, channel, channel, time.time() + 60, lambda *_: None)
    session._read()
    assert session.size <= 256 * 1024
    result = session.poll(0, wait=0)
    assert result["gap"] and result["closed"] and len(base64.b64decode(result["data"])) <= 256 * 1024
    assert session.poll(result["cursor"], wait=0)["data"] == ""
    session.poll_lock.acquire()
    try:
        with pytest.raises(ssh.SSHError) as error:
            session.poll(0, wait=0)
        assert error.value.status == 409
    finally:
        session.poll_lock.release()


def test_stream_reserves_reader_preserves_bytes_and_releases_on_close(manager, peer):
    session = connect(manager, peer)
    stream = session.stream(0)
    first = next(stream)
    with pytest.raises(ssh.SSHError) as error:
        session.poll(first["cursor"], wait=0)
    assert error.value.status == 409
    payload = b"\x1b[34m" + "目录\r".encode() + b"\x1b[0m"
    session.write(base64.b64encode(payload).decode())
    output = next(stream)
    assert payload in base64.b64decode(output["data"])
    assert output["cursor"] > first["cursor"]
    stream.close()
    assert session.poll(output["cursor"], wait=0)["data"] == ""
    assert session.transport.sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) == 1
    invalid = session.stream(output["cursor"] + 100)
    with pytest.raises(ssh.SSHError, match="位置无效"):
        next(invalid)
    assert session.poll(output["cursor"], wait=0)["data"] == ""


def test_http_stream_flushes_before_completion_and_ends_on_logout(http_server, manager, peer):
    probe = request(http_server, "POST", "/ssh/probe", peer["spec"])[1]
    key = request(http_server, "POST", "/ssh/connect", {"ticket": probe["ticket"],
        "accept_host_key": True, "password": "safe-password"})[1]["id"]
    assert request(http_server, "GET", f"/ssh/stream?id={key}&cursor=-1")[0] == 400
    assert request(http_server, "GET", f"/ssh/stream?id={key}", authenticated=False)[0] == 401
    conn = http.client.HTTPConnection(http_server.origin.removeprefix("http://"), timeout=3)
    conn.request("GET", f"/ssh/stream?id={key}", headers={"Cookie": f"{agent.COOKIE_NAME}={http_server.cookie}"})
    response = conn.getresponse()
    try:
        assert response.status == 200
        assert response.getheader("X-Accel-Buffering") == "no"
        assert response.getheader("Content-Type").startswith("application/x-ndjson")
        first = json.loads(response.readline())
        assert request(http_server, "GET", f"/ssh/output?id={key}&cursor={first['cursor']}")[0] == 409
        assert request(http_server, "POST", "/ssh/input", {"id": key, "data": base64.b64encode(b"stream-test").decode()})[0] == 200
        output = b""
        while b"stream-test" not in output:
            output += base64.b64decode(json.loads(response.readline())["data"])
        logout = http.client.HTTPConnection(http_server.origin.removeprefix("http://"))
        logout.request("GET", "/logout", headers={"Cookie": f"{agent.COOKIE_NAME}={http_server.cookie}"})
        assert logout.getresponse().status == 303
        logout.close()
        while not json.loads(response.readline())["closed"]:
            pass
        assert response.read() == b""
    finally:
        conn.close()
