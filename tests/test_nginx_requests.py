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
