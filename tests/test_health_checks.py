from __future__ import annotations

from dataclasses import replace

import agent


def project():
    return agent._project_from_config({"key": "api", "repo": "https://example.test/api.git"}, "api")


class Response:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return b""


def test_health_probe_reports_success_without_following_redirects(monkeypatch):
    calls = []
    monkeypatch.setattr(agent._HEALTH_OPENER, "open", lambda request, timeout: (calls.append((request, timeout)) or Response()))
    result = agent._probe_health_url(replace(project(), health_url="http://127.0.0.1:8000/health"))
    assert result["status"] == "healthy"
    assert result["code"] == 204
    assert calls[0][0].get_method() == "GET"


def test_health_probe_rejects_credentials_and_fragments():
    for url in ("http://user:pass@example.test/health", "http://example.test/health#private"):
        result = agent._probe_health_url(replace(project(), health_url=url))
        assert result["status"] == "failed"
        assert "健康检查地址" in result["detail"]


def test_health_status_for_unconfigured_project():
    result = agent._health_status_for(replace(project(), health_url=""))
    assert result["status"] == "not_configured"


def test_health_transition_notification_only_reports_real_state_changes():
    current_project = project()
    failed = {"status": "failed", "code": 503, "duration_ms": 12.5, "detail": "HTTP 状态码 503"}
    healthy = {"status": "healthy", "code": 200, "duration_ms": 8, "detail": "HTTP 响应正常"}

    assert agent._health_transition_notification(current_project, None, failed) is None
    title, detail = agent._health_transition_notification(current_project, healthy, failed)
    assert "健康检查失败" in title
    assert "503" in detail
    title, detail = agent._health_transition_notification(current_project, failed, healthy)
    assert "已恢复" in title
    assert "正常" in detail
    assert agent._health_transition_notification(current_project, failed, failed) is None


def test_failed_health_is_exposed_as_an_alert(monkeypatch):
    current_project = replace(project(), health_url="http://127.0.0.1:8000/health")
    monkeypatch.setattr(agent, "PROJECTS", {current_project.key: current_project})
    monkeypatch.setattr(agent, "_health_status", {
        current_project.key: {
            "status": "failed",
            "code": 503,
            "duration_ms": 11,
            "checked_at": "2026-10-01 12:00:00",
            "detail": "HTTP 状态码 503",
        },
    })
    alerts = agent._alerts_payload(
        {"server": {}, "docker": {"available": True, "containers": []}},
        {},
        {},
    )

    health_alerts = [item for item in alerts if item["source"] == "health"]
    assert len(health_alerts) == 1
    assert "503" in health_alerts[0]["detail"]


def test_commit_existence_checks_are_batched(monkeypatch):
    first, second = "a" * 40, "b" * 40
    calls = []

    class Result:
        returncode = 0
        stdout = f"{first} commit 120\n{second} missing\n"

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return Result()

    monkeypatch.setattr(agent.subprocess, "run", run)
    assert agent._commits_exist(project(), [first, second]) == {first}
    assert len(calls) == 1
    assert calls[0][0][1:] == ["cat-file", "--batch-check"]
    assert calls[0][1]["input"] == f"{first}^{{commit}}\n{second}^{{commit}}\n"


def test_project_git_status_uses_a_short_identity_aware_cache(monkeypatch):
    current_project = agent._project_from_config({"key": "git-cache-performance", "repo": "https://example.test/api.git"}, "git-cache-performance")
    calls = []
    monkeypatch.setattr(agent, "_run_git", lambda args, current_project=None: calls.append(args) or (
        "a" * 40 + "\n" + "a" * 7 if args[1] == "rev-parse" else "main"
    ))
    monkeypatch.setattr(agent.time, "monotonic", lambda: 100)

    first = agent._project_git_status(current_project)
    second = agent._project_git_status(current_project)

    assert first == second
    assert len(calls) == 2
    agent._project_git_status(replace(current_project, repo="https://example.test/other.git"))
    assert len(calls) == 4
