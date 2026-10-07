from __future__ import annotations

from dataclasses import replace

import agent


def project():
    return agent._project_from_config({"key": "api"}, "api")


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


def test_existing_health_check_uses_sustained_alerts(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "STATE_FILE", tmp_path / "state.json")
    monitor = agent._monitor()
    now = [10000]
    monitor.clock = lambda: now[0]
    monitor.observe("health:api", True, "健康检查失败：API", "HTTP 503", "health")
    assert agent._alerts_payload({}) == []
    now[0] += 60
    monitor.observe("health:api", True, "健康检查失败：API", "HTTP 503", "health")
    alerts = agent._alerts_payload({})
    assert len(alerts) == 1
    assert alerts[0]["source"] == "health" and "503" in alerts[0]["detail"]
