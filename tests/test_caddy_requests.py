from __future__ import annotations

import json
import http.client
import subprocess
import threading
from http.server import ThreadingHTTPServer

import pytest

import agent
import caddy_requests
from certificates import CertificateError


def access_line(**changes):
    record = {"level": "info", "ts": 1790742896.5, "logger": "http.log.access.log0",
              "msg": "handled request", "request": {"method": "GET", "host": "example.test",
              "uri": "/api/items?token=secret"}, "status": 200, "duration": 0.123}
    record.update(changes)
    return json.dumps(record)


def test_parses_caddy_access_log_without_query_or_credentials():
    record = caddy_requests.parse(access_line())
    assert record == {"at": "2026-09-30T04:34:56Z", "host": "example.test", "method": "GET",
                      "path": "/api/items", "status": 200, "duration_ms": 123.0, "upstream_ms": None}
    assert "secret" not in json.dumps(record)


@pytest.mark.parametrize("line", [
    '{"level":"info","msg":"started"}',
    access_line(logger="http.handlers.reverse_proxy"),
    access_line(status=700),
    access_line(duration=-1),
    access_line(duration="NaN"),
    access_line(request={"method": "GET", "host": "example.test", "uri": "https://example.test/"}),
    access_line(request={"method": "GET", "host": "example.test", "uri": "/bad\npath"}),
    access_line(request={"method": "GET", "host": "example.test", "uri": "/" + "x" * 2048}),
    access_line(ts=True),
])
def test_ignores_non_access_or_invalid_records(line):
    assert caddy_requests.parse(line) is None


def test_discovery_and_read_use_verified_id_only(monkeypatch):
    container_id = "a" * 64
    commands = []
    monkeypatch.setattr(caddy_requests.nginx_runtime, "require_local_docker", lambda: None)

    def run(command, **kwargs):
        commands.append(command)
        if command[1] == "ps":
            return "aimore-caddy-1\nother-service\n"
        if command[1] == "inspect":
            return json.dumps({"id": container_id, "running": True})
        return "v2.10.0"

    monkeypatch.setattr(caddy_requests, "run", run)
    docker_calls = []

    def logs(command, **kwargs):
        docker_calls.append(command)
        return subprocess.CompletedProcess(command, 0, access_line().encode(), b'{"msg":"startup"}\n')

    monkeypatch.setattr(caddy_requests.subprocess, "run", logs)
    result = caddy_requests.recent()
    assert result["container"] == "aimore-caddy-1"
    assert result["records"][0]["path"] == "/api/items"
    assert docker_calls == [["docker", "logs", "--tail", "1000", container_id]]
    assert all("other-service" not in str(command) for command in commands if command[1] == "exec")


def test_invalid_container_name_never_reaches_docker(monkeypatch):
    monkeypatch.setattr(caddy_requests, "run", lambda *args, **kwargs: pytest.fail("unexpected Docker call"))
    with pytest.raises(CertificateError):
        caddy_requests.recent(container="caddy; touch /tmp/x")


def test_missing_access_logs_report_configuration_state(monkeypatch):
    monkeypatch.setattr(caddy_requests, "_verified_id", lambda name: "b" * 64)
    monkeypatch.setattr(caddy_requests.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 0, b"", b""))
    result = caddy_requests.recent(container="my-caddy")
    assert result["records"] == []
    assert "访问日志" in result["notice"]


def test_http_endpoint_requires_login(monkeypatch):
    monkeypatch.setattr(agent, "UI_PASSWORD_HASH", agent._hash_password("test-password-123"))
    monkeypatch.setattr(agent, "UI_SESSION_SECRET", "test-secret-0123456789abcdef0123456789abcdef")
    calls = []
    monkeypatch.setattr(caddy_requests, "recent", lambda limit, container: (
        calls.append((limit, container)) or {"mode": "caddy", "records": []}))
    server = ThreadingHTTPServer(("127.0.0.1", 0), agent.Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    connection = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        connection.request("GET", "/caddy-requests?container=aimore-caddy-1")
        response = connection.getresponse()
        assert response.status == 401
        response.read()
        assert calls == []
        cookie = agent._make_session_cookie()
        connection.request("GET", "/caddy-requests?container=aimore-caddy-1",
                           headers={"Cookie": f"{agent.COOKIE_NAME}={cookie}"})
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["mode"] == "caddy"
        assert calls == [(100, "aimore-caddy-1")]
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
