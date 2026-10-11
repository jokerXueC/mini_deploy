from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent
import nginx_requests as requests
from certificates import CertificateError, CertificateStore


def runtime(tmp_path: Path, mode: str = "local") -> SimpleNamespace:
    conf_root = tmp_path / "conf.d"
    conf_root.mkdir()
    return SimpleNamespace(mode=mode, conf_root=conf_root, data_home=tmp_path,
                           container_id="a" * 64, profile={"container": "edge"})


def line(**changes) -> str:
    data = {"at": "2026-09-30T12:34:56+08:00", "host": "example.test", "method": "GET",
            "path": "/api/items", "status": "200", "duration": "0.123", "upstream": "0.100"}
    data.update(changes)
    return requests.PREFIX + json.dumps(data)


def test_config_uses_selected_log_destination_without_sensitive_request_fields():
    local = requests.config_text("local")
    docker = requests.config_text("docker")
    assert "access_log /var/log/nginx/mini-deploy-requests.log" in local
    assert "access_log /dev/stdout" in docker
    for value in (local, docker):
        assert "escape=json" in value
        assert "$uri" in value
        assert not any(field in value for field in ("$request_uri", "$args", "$request_body", "$cookie_"))


def test_parse_accepts_valid_record_and_ignores_untrusted_lines():
    assert requests.parse(line()) == {
        "at": "2026-09-30T12:34:56+08:00", "host": "example.test", "method": "GET",
        "path": "/api/items", "status": 200, "duration_ms": 123.0, "upstream_ms": 100.0,
    }
    assert requests.parse("application says " + line()) is None
    for invalid in (line(status="700"), line(duration="-1"), line(duration="NaN"),
                    line(path="not-a-path"), line(path="/" + "x" * 2048),
                    line(path="/bad\npath"), line(method="GET /"), line() + " " * 8192):
        assert requests.parse(invalid) is None


def test_metadata_is_bounded_and_credentials_never_reach_records():
    record = requests.parse(line(client_ip="2001:db8::1", protocol="HTTP/2.0",
        response_bytes="1234", upstream_address="10.0.0.2:8000", request_headers={
            "User-Agent": "client " + "a" * 300, "accept": "application/json",
            "Authorization": "Bearer secret", "Cookie": "session=secret",
            "X-Api-Key": "secret", "X-Request-ID": "bad\nvalue"}))
    assert record["client_ip"] == "2001:db8::1" and record["response_bytes"] == 1234
    assert record["upstream_address"] == "10.0.0.2:8000" and record["protocol"] == "HTTP/2.0"
    assert record["request_headers"]["Accept"] == "application/json"
    assert len(record["request_headers"]["User-Agent"]) == 256
    assert "secret" not in json.dumps(record) and "X-Request-ID" not in record["request_headers"]
    malformed = requests.parse(line(client_ip="forged", protocol=[], response_bytes=True,
                                   request_headers={"Host": 123, "Accept": ["text/html", 2]}))
    assert "client_ip" not in malformed and "response_bytes" not in malformed
    assert malformed["request_headers"] == {"Accept": "text/html"}


def test_request_identity_and_phase_times_preserve_attempts_and_missing_values():
    record = requests.parse(line(request_id="trace-123", request_headers={"X-Request-ID": "trace-123"},
        upstream_status="502, 200", upstream_connect="0.010, 0.020", upstream_header="-, 0.120", upstream="0.010, 0.220"))
    assert record["request_id"] == "trace-123" and record["request_id_source"] == "incoming"
    first, second = record["upstream_attempts"]
    assert first["status"] == 502 and first["wait_ms"] is None and first["transfer_ms"] is None
    assert second == {"status": 200, "connect_ms": 20, "header_ms": 120, "response_ms": 220,
                      "wait_ms": 100, "transfer_ms": 100}
    generated = requests.parse(line(request_id="a" * 32, upstream_status="200", upstream_connect="0.100", upstream_header="0.050"))
    assert generated["request_id_source"] == "generated"
    assert generated["upstream_attempts"][0]["wait_ms"] is None
    assert "request_id" not in requests.parse(line(request_id="bad id\n"))
    for raw in ("NaN", "-1", "0.1 nope", "0.1, " * 20, [], True):
        assert requests.upstream_values(raw) == []


def test_diagnostic_config_accepts_bounded_ids_and_captures_cumulative_timings():
    text = requests.config_text("docker")
    for variable in ("$request_id", "$upstream_status", "$upstream_connect_time", "$upstream_header_time"):
        assert variable in text
    assert "{0,127}" in text
    assert "$mini_deploy_request_id" not in requests.config_text("docker", diagnostics=False)


@pytest.mark.parametrize("legacy", [{"details": False}, {"diagnostics": False}])
def test_legacy_nginx_configuration_is_recognized_and_upgraded_on_enable(tmp_path, monkeypatch, legacy):
    selected = runtime(tmp_path)
    path = requests.config_path(selected)
    original = requests.config_text("local", **legacy)
    path.write_text(original, encoding="utf-8")
    assert requests.enabled(selected)
    calls = []
    monkeypatch.setattr(CertificateStore, "commit_config", lambda self, target, content, previous:
        (calls.append(previous), target.write_text(content, encoding="utf-8")))
    requests.enable(selected)
    assert calls == [original]
    assert "$remote_addr" in path.read_text(encoding="utf-8")


def test_enable_preserves_existing_config_and_is_idempotent(tmp_path, monkeypatch):
    selected = runtime(tmp_path, "docker")
    calls = []
    monkeypatch.setattr(CertificateStore, "commit_config", lambda self, path, content, previous: (
        calls.append((path, content, previous)), path.write_text(content, encoding="utf-8")))
    requests.enable(selected)
    requests.enable(selected)
    assert len(calls) == 1
    assert requests.enabled(selected)
    path = requests.config_path(selected)
    path.write_text("# user config\n", encoding="utf-8")
    with pytest.raises(CertificateError):
        requests.enable(selected)
    assert path.read_text(encoding="utf-8") == "# user config\n"


def test_disable_removes_only_managed_config_and_restores_on_reload_failure(tmp_path, monkeypatch):
    selected = runtime(tmp_path, "docker")
    selected.command = lambda action: ["nginx", action]
    path = requests.config_path(selected)
    path.write_text(requests.config_text("docker"), encoding="utf-8")
    commands = []
    monkeypatch.setattr(requests, "run", lambda command: commands.append(command))
    requests.disable(selected)
    assert not path.exists()
    assert commands == [["nginx", "test"], ["nginx", "reload"]]
    path.write_text("# user config\n", encoding="utf-8")
    with pytest.raises(CertificateError):
        requests.disable(selected)
    assert path.read_text(encoding="utf-8") == "# user config\n"
    path.write_text(requests.config_text("docker"), encoding="utf-8")
    attempts = []

    def fail_once(command):
        attempts.append(command)
        if len(attempts) == 2:
            raise CertificateError("reload failed")

    monkeypatch.setattr(requests, "run", fail_once)
    with pytest.raises(CertificateError):
        requests.disable(selected)
    assert path.read_text(encoding="utf-8") == requests.config_text("docker")


def test_local_reads_only_own_log_and_returns_latest_first(tmp_path, monkeypatch):
    selected = runtime(tmp_path)
    log = tmp_path / "requests.log"
    monkeypatch.setattr(requests, "LOCAL_LOG", log)
    requests.config_path(selected).write_text(requests.config_text("local"), encoding="utf-8")
    log.write_text("old combined log\n" + line(path="/first") + "\n" + line(path="/second") + "\n", encoding="utf-8")
    result = requests.recent(selected, 1)
    assert [record["path"] for record in result["records"]] == ["/second"]
    assert result["mode"] == "local" and result["enabled"]
    with pytest.raises(ValueError):
        requests.recent(selected, 301)


def test_docker_reads_verified_container_id_only(tmp_path, monkeypatch):
    selected = runtime(tmp_path, "docker")
    requests.config_path(selected).write_text(requests.config_text("docker"), encoding="utf-8")
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, line(path="/docker").encode(), b"")

    monkeypatch.setattr(requests.subprocess, "run", run)
    result = requests.recent(selected)
    assert commands == [["docker", "logs", "--tail", "1000", selected.container_id]]
    assert result["container"] == "edge"
    assert result["records"][0]["path"] == "/docker"
    monkeypatch.setattr(requests.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a[0], 1, b"", b"failed"))
    with pytest.raises(CertificateError):
        requests.recent(selected)


def test_api_payload_requires_configured_instance_and_valid_limit(tmp_path, monkeypatch):
    selected = runtime(tmp_path)
    class Settings:
        def __init__(self, configured):
            self.configured = configured

        def read(self):
            return {"configured": self.configured, "profile": {"mode": "local"}}

        def runtime(self):
            return selected

    monkeypatch.setattr(agent, "_nginx_settings", lambda: Settings(False))
    assert agent._nginx_requests_payload()["records"] == []
    with pytest.raises(ValueError):
        agent._nginx_requests_payload(301)
    monkeypatch.setattr(agent, "_nginx_settings", lambda: Settings(True))
    monkeypatch.setattr(agent.nginx_requests, "recent", lambda selected, limit: {"mode": selected.mode, "limit": limit})
    assert agent._nginx_requests_payload(100) == {"mode": "local", "limit": 100}
