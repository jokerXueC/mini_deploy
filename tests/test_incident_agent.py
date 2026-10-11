import json

import agent
import monitoring
from test_deployment_removal import http_server as http_server, runtime as runtime


def test_worker_inspects_only_selected_fields_and_keeps_valid_partial_samples(monkeypatch):
    first, second = "a" * 64, "b" * 64
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["timeout"] <= 3
        return 1, json.dumps({"Id": first, "Name": "/api", "State": {"Status": "exited", "ExitCode": 137,
            "OOMKilled": True, "Error": ""}, "RestartCount": 2}) + "\nError: container not found"
    monkeypatch.setattr(agent, "_run_command", run)
    snapshot = agent._inspect_docker_snapshot({"available": True, "containers": [
        {"id": first, "name": "api"}, {"id": second, "name": "worker"}]})
    assert len(calls) == 1
    assert calls[0][-2:] == [first, second]
    assert ".Env" not in calls[0][calls[0].index("--format") + 1]
    assert snapshot["containers"][0]["inspection"]["oom_killed"]
    assert snapshot["containers"][1]["inspection"] is None


def test_unavailable_docker_does_not_start_inspection(monkeypatch):
    monkeypatch.setattr(agent, "_run_command", lambda *a, **k: (_ for _ in ()).throw(AssertionError("No inspect")))
    assert agent._inspect_docker_snapshot({"available": False}) == {"available": False}


def test_stale_resource_values_cannot_resolve_a_fault_or_assert_coverage(tmp_path):
    monitor = monitoring.Monitor(tmp_path / "monitor.json", clock=lambda: 10000)
    monitor.observe("server:cpu", True, "CPU high", "95%", "server", immediate=True)
    agent._evaluate_resource_alerts(monitor, {"server": {"cpu_percent": 20,
        "memory": {"percent": 20}, "disk": {"percent": 20}, "sampled_ts": 9700},
        "docker": {"available": False, "error": "docker command not found"}})
    assert monitor.snapshot()["alerts"][0]["stale"]
    assert monitor.coverage["server"]["status"] == "unknown"
    assert monitor.coverage["docker"]["status"] == "not_installed"


def test_acknowledgment_api_is_authenticated_persistent_and_rejects_old_incidents(http_server, runtime):
    monitor = agent._monitor()
    monitor.observe("docker:api:oom", True, "API OOM", "exit137", "docker", immediate=True)
    alert = monitor.snapshot()["alerts"][0]
    payload = {"action": "acknowledge", "key": alert["id"], "incident_id": alert["incident_id"]}
    assert http_server("POST", "/monitoring", payload, authenticated=False)[0] == 401
    assert http_server("POST", "/monitoring", {**payload, "incident_id": "old"})[0] == 400
    status, result = http_server("POST", "/monitoring", payload)
    assert status == 200 and result["alerts"][0]["acknowledged_at"]
    assert result["events"][0]["kind"] == "acknowledged"
    restored = monitoring.Monitor(runtime / "website-monitoring.json")
    assert restored.snapshot()["alerts"][0]["acknowledged_at"]
    monitor.observe("docker:api:oom", False, "API OOM", "running", "docker")
    monitor.observe("docker:api:oom", True, "API OOM", "exit137", "docker", immediate=True)
    assert http_server("POST", "/monitoring", payload)[0] == 400
    assert monitor.next_notification(True)
