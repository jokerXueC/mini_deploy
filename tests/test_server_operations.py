import json
import sys

import pytest

import agent
import server_operations as operations
from test_deployment_removal import http_server as http_server, runtime as runtime


def manager(tmp_path, clock=lambda: 1800000000):
    calls = []

    def run(command):
        calls.append(command)
        if command[0] == "ps":
            return 0, "12 1 0 20.0 1.2 20480 python\n13 1 1000 2.0 6.0 102400 postgres", False
        if command[0] == "ss":
            return 0, 'tcp LISTEN 0 128 0.0.0.0:8080 0.0.0.0:* users:(("python",pid=12,fd=4))', False
        if command[0] == "systemctl":
            return 0, json.dumps([{"unit": "api.service", "active": "failed"}]), False
        return 0, "2027-01-15T08:00:00Z error password=secret\n2027-01-15T08:01:00Z healthy", False

    instance = operations.Operations(runner=run, log_file=tmp_path / "agent.log", log_roots=[tmp_path], clock=clock)
    return instance, calls


def test_inventory_is_cached_sorted_and_links_ports_without_command_arguments(tmp_path):
    instance, calls = manager(tmp_path)
    first = instance.inventory({"containers": [{"id": "a" * 64, "name": "api"}]})
    assert first["processes"][0]["pid"] == 12
    assert first["ports"][0]["owners"] == [{"pid": 12, "name": "python"}]
    assert "args" not in str(calls)
    assert instance.inventory(sort="memory")["processes"][0]["pid"] == 13
    assert len(calls) == 3
    assert instance.inventory(pid=13)["processes"][0]["name"] == "postgres"
    assert first["sources"][0]["kind"] == "service"
    first["processes"].clear()
    assert instance.inventory()["processes"]


def test_logs_only_allow_discovered_sources_and_preserve_files(tmp_path):
    instance, calls = manager(tmp_path)
    logfile = tmp_path / "api.log"
    contents = "2027-01-15T08:00:00Z error token=private\nuntimed traceback\n2027-01-15T08:01:00Z ok\n"
    logfile.write_text(contents)
    sources = instance.inventory()["sources"]
    key = next(s["id"] for s in sources if s["kind"] == "file")
    result = instance.logs(key, since=1799999900, until=1800000100, level="error")
    assert len(result["lines"]) == 1 and "private" not in result["lines"][0]
    assert result["ignored_untimed"] == 1
    assert logfile.read_text() == contents
    for invalid in (str(logfile), "file:../../etc/shadow", "service:--help", "container:-x"):
        with pytest.raises(ValueError):
            instance.logs(invalid)
    assert len(calls) == 3


def test_file_symlinks_hardlinks_and_directories_are_not_log_sources(tmp_path):
    original = tmp_path / "secret.txt"
    original.write_text("secret")
    try:
        (tmp_path / "link.log").symlink_to(original)
    except OSError:
        pass
    import os
    os.link(original, tmp_path / "hard.log")
    (tmp_path / "directory.log").mkdir()
    instance, _ = manager(tmp_path)
    assert not [s for s in instance.inventory()["sources"] if s["kind"] == "file"]


def test_log_time_bounds_filters_and_command_flags(tmp_path):
    instance, calls = manager(tmp_path)
    instance.inventory({"containers": [{"id": "a" * 64, "name": "api"}]})
    result = instance.logs("service:api.service", since=1799999900, until=1800000100, keyword="error")
    assert len(result["lines"]) == 1 and "secret" not in result["lines"][0]
    assert "--since" in calls[-1] and "--until" in calls[-1]
    instance.logs("container:" + "a" * 64, limit=1000)
    assert calls[-1] == ["docker", "logs", "--timestamps", "--tail", "1000", "a" * 64]
    for kwargs in ({"since": float("nan"), "until": 1800000000}, {"since": 1, "until": 1800000000},
                   {"limit": 1001}, {"level": "fatal"}, {"keyword": "a" * 161}):
        with pytest.raises(ValueError):
            instance.logs("service:api.service", **kwargs)


def test_huge_file_read_is_limited_and_missing_timestamp_is_never_assumed_in_window(tmp_path):
    instance, _ = manager(tmp_path)
    logfile = tmp_path / "api.log"
    logfile.write_bytes(b"untimed\n" * 100000)
    key = next(s["id"] for s in instance.inventory()["sources"] if s["kind"] == "file")
    result = instance.logs(key, since=1799999900, until=1800000100)
    assert result["truncated"] and not result["lines"] and result["ignored_untimed"]
    assert logfile.stat().st_size == 800000


def test_runner_enforces_output_limit_and_reports_timeout(monkeypatch):
    monkeypatch.setattr(operations, "MAX_BYTES", 4096)
    code, output, limited = operations.run([sys.executable, "-c", "print('x'*100000)"])
    assert limited and len(output) == 4096
    with pytest.raises(RuntimeError, match="超时"):
        operations.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=.1)


@pytest.mark.parametrize("line", ["2027-01-15T08:00:00Z message", "[15/Jan/2027:08:00:00 +0000] GET /",
                                 '{"ts":1800000000,"msg":"message"}'])
def test_supported_log_timestamps(line):
    assert operations.timestamp(line) == 1800000000
    assert operations.timestamp("no time") is None


def test_diagnostic_endpoints_require_login_and_reject_unlisted_files(http_server, tmp_path, monkeypatch):
    instance, _ = manager(tmp_path)
    monkeypatch.setattr(agent, "_operations", lambda: instance)
    for route in ("/operations/inventory", "/operations/logs", "/operations/incident"):
        assert http_server("GET", route, authenticated=False)[0] == 401
    status, data = http_server("GET", "/operations/inventory")
    assert status == 200 and data["ports"][0]["address"] == "0.0.0.0:8080"
    assert http_server("GET", "/operations/logs?source=/etc/shadow")[0] == 400
    assert http_server("GET", "/operations/inventory?sort=arbitrary")[0] == 400
    assert http_server("GET", "/operations/incident?id=missing")[0] == 400


def test_incident_uses_saved_evidence_and_never_links_recreated_container_by_name(runtime, tmp_path, monkeypatch):
    instance, _ = manager(tmp_path, clock=lambda: 10000)
    monkeypatch.setattr(agent, "_operations", lambda: instance)
    monkeypatch.setattr(agent, "_system_status_cache", {"payload": {"docker": {
        "containers": [{"name": "api", "id": "b" * 64}]}}})
    monitor = agent._monitor()
    monitor.observe("docker:api:oom", True, "API OOM", "exit 137", "docker", immediate=True,
                    metadata={"container": "api", "container_id": "a" * 64, "exit_code": 137, "oom_killed": True})
    alert = monitor.snapshot()["alerts"][0]
    data = agent._incident_diagnostics(alert["id"])
    assert data["incident"]["metadata"]["exit_code"] == 137
    assert data["log_source"] == ""  # A different instance with the same name is not fault-time evidence.
    assert data["timeline"]
    assert "server_now" in data and "resource_samples" in data
