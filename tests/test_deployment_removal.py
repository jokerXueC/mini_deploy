"""Retiring deployment must preserve business resources and existing monitoring."""
import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

import agent


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(agent, "MONITORING_STATE_FILE", tmp_path / "monitoring-state.json")
    monkeypatch.setattr(agent, "PROJECTS_CONFIG_FILE", tmp_path / "projects.json")
    monkeypatch.setattr(agent, "LEGACY_PROJECTS_CONFIG_FILE", None)
    monkeypatch.setattr(agent, "SITES_CONFIG_FILE", tmp_path / "sites.json")
    monkeypatch.setattr(agent, "PROJECT_CONFIG_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(agent, "PROJECTS", {})
    monkeypatch.setattr(agent, "NOTIFICATIONS", agent._default_notification_config())
    monkeypatch.setattr(agent, "_RUNTIME_CONFIG_ERROR", None)
    monkeypatch.setattr(agent, "_state", {"system_metrics": []})
    monkeypatch.setattr(agent, "_audit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent, "_log", lambda *args: None)
    monkeypatch.setattr(agent, "_system_status_payload", lambda: {"server": {}, "docker": {"available": True}})
    return tmp_path


@pytest.fixture
def http_server(runtime, monkeypatch):
    monkeypatch.setattr(agent, "UI_PASSWORD_HASH", agent._hash_password("test-password-123"))
    monkeypatch.setattr(agent, "UI_SESSION_SECRET", "0123456789abcdef" * 4)
    cookie = agent._make_session_cookie()
    headers = {"Cookie": f"{agent.COOKIE_NAME}={cookie}", "X-CSRF-Token": agent._csrf_token(cookie)}
    server = ThreadingHTTPServer(("127.0.0.1", 0), agent.Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def request(method, path, data=None, *, authenticated=True):
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        connection.request(method, path, json.dumps(data or {}), headers=headers if authenticated else {})
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, json.loads(body)

    request.origin = f"http://127.0.0.1:{server.server_port}"
    request.cookie = cookie
    try:
        yield request
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


@pytest.mark.parametrize("path", [
    "/webhook", "/deploy/webhook", "/redeploy", "/rollback", "/cancel", "/force-unlock",
    "/preflight", "/projects-config", "/projects-config/inspect", "/projects-config/bootstrap",
    "/projects-config/save", "/projects-config/delete", "/projects-config/secret",
])
def test_retired_routes_reject_without_executing_or_changing_files(http_server, runtime, monkeypatch, path):
    marker = runtime / "business-file"
    marker.write_text("untouched")

    def forbidden(*args, **kwargs):
        pytest.fail("retired route attempted to start a process")

    monkeypatch.setattr(agent.subprocess, "Popen", forbidden)
    for method in ("GET", "POST"):
        code, payload = http_server(method, path, {"project": {"script": str(marker)}}, authenticated=False)
        assert code == 410
        assert payload["error"] == "deployment_removed"
    assert marker.read_text() == "untouched"
    assert not agent.SITES_CONFIG_FILE.exists()


def test_new_site_endpoints_require_auth_and_csrf(http_server):
    for method, path in (("GET", "/sites"), ("GET", "/notifications"), ("POST", "/sites/save"), ("POST", "/sites/delete")):
        assert http_server(method, path, authenticated=False)[0] == 401


def test_legacy_config_is_read_only_and_missing_scripts_do_not_block(runtime):
    legacy = {"projects": [{"key": "api", "name": "API", "app_domain": "api.example.test",
                             "service_port": 8000, "enabled": True, "webhook_secret": "weak",
                             "script": "/does-not-exist/deploy.sh", "repo": "https://example.test/private.git",
                             "deployment_plan": {"method": "invalid-old-plan"}, "docker_config": "legacy"}],
              "notifications": {"wecom": {"enabled": True, "webhook_url": "https://example.test/notice"}}}
    original = json.dumps(legacy).encode()
    agent.PROJECTS_CONFIG_FILE.write_bytes(original)
    agent._replace_projects(agent._load_projects())
    agent._replace_notifications(agent._load_notifications())
    assert len(agent._validate_projects_runtime_config()) == 1
    assert not agent.SITES_CONFIG_FILE.exists()
    agent._save_site({"original_key": "api", "site": {"key": "api", "name": "API renamed",
                     "app_domain": "api.example.test", "service_port": 8000}})
    assert agent.PROJECTS_CONFIG_FILE.read_bytes() == original
    stored = json.loads(agent.SITES_CONFIG_FILE.read_text())
    assert stored["sites"][0]["name"] == "API renamed"
    assert "script" not in stored["sites"][0]
    assert "webhook_secret" not in stored["sites"][0]
    assert stored["notifications"]["wecom"]["enabled"]
    assert agent._load_projects()["api"].name == "API renamed"


def test_deleting_site_preserves_business_files_and_legacy_config(runtime):
    business = runtime / "business"
    business.mkdir()
    for name in ("deploy.sh", "app.service", "compose.yaml", "database.sqlite", "deployment.log"):
        (business / name).write_text(f"original {name}")
    original = json.dumps({"projects": [{"key": "api", "name": "API", "workdir": str(business)}]}).encode()
    agent.PROJECTS_CONFIG_FILE.write_bytes(original)
    agent._replace_projects(agent._load_projects())
    snapshot = {file.name: file.read_bytes() for file in business.iterdir()}
    agent._delete_site({"key": "api"})
    assert agent.PROJECTS == {}
    assert agent._load_projects() == {}
    assert agent.PROJECTS_CONFIG_FILE.read_bytes() == original
    assert {file.name: file.read_bytes() for file in business.iterdir()} == snapshot


def test_old_queue_and_history_never_enter_monitoring_state(runtime):
    original = json.dumps({"queued_jobs": [{"project_key": "api", "script": "/tmp/must-not-run"}],
                           "current_deploy": {"status": "running"}, "history": [{"repo": "private"}],
                           "system_metrics": [{"at": "sample"}]}).encode()
    agent.STATE_FILE.write_bytes(original)
    agent._read_state()
    assert agent._state == {"system_metrics": [{"at": "sample"}]}
    agent._write_state()
    assert agent.STATE_FILE.read_bytes() == original
    assert json.loads(agent.MONITORING_STATE_FILE.read_text()) == agent._state
    assert not hasattr(agent, "_worker")
    assert not hasattr(agent, "_run_deploy")
    assert not hasattr(agent, "_restore_queued_jobs")


def test_startup_starts_only_monitoring_tasks(runtime, monkeypatch):
    original = json.dumps({"queued_jobs": [{"project_key": "api"}], "system_metrics": []}).encode()
    agent.STATE_FILE.write_bytes(original)
    started = []

    class Thread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            started.append(self.target.__name__)

    class Server:
        def __init__(self, address, handler):
            assert address == ("0.0.0.0", 6868)

        def serve_forever(self):
            return

    monkeypatch.setattr(agent.threading, "Thread", Thread)
    monkeypatch.setattr(agent, "ThreadingHTTPServer", Server)
    monkeypatch.setattr(agent, "_validate_ui_auth_config", lambda: None)
    monkeypatch.setattr(agent.sys, "argv", ["agent.py"])
    agent.main()
    assert started == ["_realtime_metric_sampler", "_system_metric_sampler",
                       "_docker_log_metric_sampler", "_health_check_sampler"]
    assert agent.STATE_FILE.read_bytes() == original


def test_status_and_site_crud_do_not_require_repository(http_server, runtime):
    code, result = http_server("POST", "/sites/save", {"site": {"name": "API", "app_domain": "api.example.test", "service_port": 8766}})
    assert code == 200
    site = result["sites"][0]
    assert site["name"] == "API"
    assert "repo" not in site and "script" not in site
    code, result = http_server("GET", "/status")
    assert code == 200
    assert not ({"git", "projects", "state", "lock"} & result.keys())
    assert result["agent"]["site_count"] == 1
    assert http_server("POST", "/sites/delete", {"key": site["key"]})[0] == 200
    assert http_server("GET", "/sites")[1]["sites"] == []


def test_site_key_cannot_take_over_retained_certificate(runtime):
    directory = runtime / "certificates" / "api"
    directory.mkdir(parents=True)
    marker = directory / "private.key"
    marker.write_text("do not touch")
    with pytest.raises(ValueError, match="已有站点配置或证书"):
        agent._save_site({"site": {"key": "api", "name": "API"}})
    assert marker.read_text() == "do not touch"
    assert agent.PROJECTS == {}


def test_site_ui_uses_actual_site_api_without_deployment(http_server, runtime):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as browser_runtime:
        browser = browser_runtime.chromium.launch()
        try:
            context = browser.new_context()
            context.add_cookies([{"name": agent.COOKIE_NAME, "value": http_server.cookie,
                                  "url": http_server.origin}])
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(http_server.origin + "/ui?view=certificates")
            page.locator("#siteAdd").click()
            page.locator("#siteName").fill("API")
            page.locator("#siteDomain").fill("api.example.test")
            page.locator("#sitePort").fill("8766")
            page.locator("#siteSave").click()
            playwright.expect(page.locator("#siteRegistryList")).to_contain_text("api.example.test")
            key = next(iter(agent.PROJECTS))
            assert agent.PROJECTS[key].service_port == 8766
            page.locator(f'[data-site-edit="{key}"]').click()
            page.locator("#siteName").fill("API renamed")
            page.locator("#siteSave").click()
            playwright.expect(page.locator("#siteRegistryList")).to_contain_text("API renamed")
            page.locator(f'[data-site-delete="{key}"]').click()
            page.locator("#confirmOkBtn").click()
            playwright.expect(page.locator("#siteRegistryList")).to_contain_text("暂无站点")
            assert agent.PROJECTS == {}
            assert not errors
        finally:
            browser.close()
