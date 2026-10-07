from __future__ import annotations

import http.client
import json
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import agent
import monitoring
from test_deployment_removal import http_server as http_server, runtime as runtime


@pytest.fixture
def monitor(tmp_path):
    clock = [10000.0]
    instance = monitoring.Monitor(tmp_path / "monitor.json", clock=lambda: clock[0])
    instance.test_clock = clock
    return instance


def advance(monitor, seconds):
    monitor.test_clock[0] += seconds


def observe(monitor, bad=True):
    monitor.observe("server:cpu", bad, "CPU high", "90%", "server")


@pytest.mark.parametrize("url,expected", [
    ("Example.com", "https://example.com/"), ("http://127.0.0.1:6868/health", "http://127.0.0.1:6868/health"),
    ("https://example.com:443", "https://example.com/"), ("http://[::1]:8080", "http://[::1]:8080/"),
])
def test_normalize(url, expected):
    assert monitoring.normalize_url(url) == expected


@pytest.mark.parametrize("url", ["", "ftp://example.com", "https://u:p@a.com", "http://a.com/#x",
                                      "http://a.com/?token=secret", "http://a.com:99999", "http://a.com\n", "http://a\\b"])
def test_reject_unsafe_or_sensitive_urls(url):
    if url.endswith("\n"):
        url = "http://a.com/\nsecret"
    with pytest.raises(ValueError):
        monitoring.normalize_url(url)


def test_sustained_initial_failure_repeat_recovery_and_restart(monitor):
    observe(monitor)
    assert not monitor.snapshot()["alerts"]
    advance(monitor, 60)
    observe(monitor)
    assert len(monitor.snapshot()["alerts"]) == 1
    key, message = monitor.next_notification(True)
    monitor.delivered(key, message, [{"enabled": True, "ok": True}])
    restored = monitoring.Monitor(monitor.path, monitor.clock)
    assert restored.next_notification(True) is None
    assert len(restored.snapshot()["alerts"]) == 1
    advance(monitor, 3600)
    observe(monitor)
    assert monitor.next_notification(True)
    observe(monitor, False)
    assert not monitor.snapshot()["alerts"]
    key, message = monitor.next_notification(True)
    assert message["recovery"]
    monitor.delivered(key, message, [{"enabled": True, "ok": True}])
    assert monitor.next_notification(True) is None
    assert monitor.snapshot()["events"][0]["kind"] == "recovery"


def test_flapping_missing_samples_and_notification_failures(monitor):
    observe(monitor)
    advance(monitor, 30)
    observe(monitor, False)
    observe(monitor)
    advance(monitor, 30)
    observe(monitor, None)
    observe(monitor)
    assert not monitor.snapshot()["alerts"]
    advance(monitor, 60)
    observe(monitor)
    assert monitor.next_notification(False) is None
    key, message = monitor.next_notification(True)
    monitor.delivered(key, message, [{"enabled": True, "ok": False}])
    assert not monitor.snapshot()["delivery"]["ok"]
    assert monitor.next_notification(True) is None
    advance(monitor, 300)
    assert monitor.next_notification(True) is None  # old sample must not trigger reminders
    observe(monitor)
    assert monitor.next_notification(True)
    observe(monitor, None)
    assert len(monitor.snapshot()["alerts"]) == 1


def test_mute_persists_without_hiding_incident(monitor):
    observe(monitor)
    advance(monitor, 60)
    observe(monitor)
    monitor.operation({"action": "mute", "seconds": 3600})
    assert monitor.snapshot()["alerts"][0]["muted"]
    assert monitor.next_notification(True) is None
    restored = monitoring.Monitor(monitor.path, monitor.clock)
    assert restored.snapshot()["muted_until"] == monitor.clock() + 3600
    monitor.operation({"action": "mute", "seconds": 0})
    assert monitor.next_notification(True)


def test_no_recovery_message_when_initial_alert_was_never_sent(monitor):
    observe(monitor)
    advance(monitor, 60)
    observe(monitor)
    assert monitor.next_notification(False) is None
    observe(monitor, False)
    assert monitor.next_notification(True) is None
    assert monitor.snapshot()["events"][0]["kind"] == "recovery"


def test_crud_rejects_late_probe_and_never_touches_business_files(monitor, tmp_path):
    business = tmp_path / "Caddyfile"
    business.write_text("business settings")
    target = monitor.operation({"action": "save", "url": "example.com"})["targets"][0]
    target.pop("result")
    with pytest.raises(ValueError, match="已在"):
        monitor.operation({"action": "save", "url": "https://example.com/"})
    monitor.operation({"action": "toggle", "key": target["key"]})
    monitor.accept(target, {"checked_at": monitor.clock(), "status": "failed"})
    assert monitor.results == {}
    monitor.operation({"action": "remove", "key": target["key"]})
    monitor.accept(target, {"checked_at": monitor.clock(), "status": "healthy"})
    assert not monitor.snapshot()["targets"]
    assert business.read_text() == "business settings"


def test_failed_save_rolls_back_memory(monitor, monkeypatch):
    def fail():
        raise OSError("disk full")
    monkeypatch.setattr(monitor, "flush", fail)
    with pytest.raises(OSError):
        monitor.operation({"action": "save", "url": "example.com"})
    assert monitor.targets == {}


def test_one_failed_sample_never_confirms_sustained_outage(monitor):
    target = monitor.operation({"action": "save", "url": "example.com"})["targets"][0]
    target.pop("result")
    monitor.accept(target, {"checked_at": monitor.clock(), "status": "failed", "duration_ms": 5})
    monitor.evaluate_websites()
    advance(monitor, 61)
    monitor.evaluate_websites()
    assert monitor.snapshot()["alerts"] == []
    monitor.accept(target, {"checked_at": monitor.clock(), "status": "failed", "duration_ms": 5})
    monitor.evaluate_websites()
    assert len(monitor.snapshot()["alerts"]) == 1


def test_certificate_alert_and_unknown_is_not_recovery(monitor):
    target = monitor.operation({"action": "save", "url": "example.com"})["targets"][0]
    target.pop("result")
    result = {"checked_at": monitor.clock(), "status": "healthy", "duration_ms": 50,
              "certificate": {"status": "valid", "checked_at": monitor.clock(), "expires_at": monitor.clock() + 86400}}
    monitor.accept(target, result)
    monitor.evaluate_websites()
    advance(monitor, 60)
    monitor.evaluate_websites()
    assert monitor.snapshot()["alerts"][0]["source"] == "certificate"
    monitor.accept(target, {**result, "certificate": {"status": "unknown", "checked_at": monitor.clock()}})
    monitor.evaluate_websites()
    assert len(monitor.snapshot()["alerts"]) == 1


class ProbeHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        code = 302 if self.path == "/redirect" else 503 if self.path == "/fail" else 200
        self.send_response(code)
        if code == 302:
            self.send_header("Location", "/fail")
        self.end_headers()

    def log_message(self, *_args):
        pass


def test_actual_http_probe_reports_redirect_and_errors():
    server = ThreadingHTTPServer(("127.0.0.1", 0), ProbeHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        healthy = monitoring.probe({"url": base})
        assert healthy["status"] == "healthy"
        assert healthy["certificate"]["status"] == "not_applicable"
        assert monitoring.probe({"url": base + "/fail"})["status"] == "failed"
        assert monitoring.probe({"url": base + "/redirect"})["code"] == 302
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)


def test_actual_tls_valid_untrusted_and_wrong_hostname(tmp_path, monkeypatch):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True, timeout=20)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server = ThreadingHTTPServer(("127.0.0.1", 0), ProbeHandler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        url = f"https://localhost:{server.server_port}"
        assert monitoring.certificate_probe(url)["status"] == "invalid"
        factory = ssl.create_default_context
        monkeypatch.setattr(monitoring.ssl, "create_default_context", lambda: factory(cafile=str(cert)))
        good = monitoring.certificate_probe(url)
        assert good["status"] == "valid" and good["verified"]
        assert good["expires_at"] > time.time()
        assert len(good["sha256"]) == 64
        mismatch = monitoring.certificate_probe(f"https://127.0.0.1:{server.server_port}")
        assert mismatch["status"] == "invalid" and not mismatch["verified"]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)


def test_resource_rules_ignore_absent_docker_and_dont_alert_on_manual_stop(monitor):
    system = {"server": {"cpu_percent": 95}, "docker": {"available": False, "error": "docker command not found"}}
    agent._evaluate_resource_alerts(monitor, system)
    advance(monitor, 60)
    agent._evaluate_resource_alerts(monitor, system)
    assert [a["id"] for a in monitor.snapshot()["alerts"]] == ["server:cpu"]
    system["docker"] = {"available": True, "containers": [{"name": "manual", "state": "exited"}]}
    agent._evaluate_resource_alerts(monitor, system)
    assert not any(a["source"] == "docker" for a in monitor.snapshot()["alerts"])


def test_monitor_api_auth_csrf_and_read_only_status(http_server, runtime, monkeypatch):
    assert http_server("GET", "/monitoring", authenticated=False)[0] == 401
    assert http_server("POST", "/monitoring", {"action": "save", "url": "example.com"}, authenticated=False)[0] == 401
    connection = http.client.HTTPConnection(http_server.origin.removeprefix("http://"))
    connection.request("POST", "/monitoring", json.dumps({"action": "save", "url": "example.com"}),
                       {"Cookie": f"{agent.COOKIE_NAME}={http_server.cookie}"})
    response = connection.getresponse()
    assert response.status == 403
    response.read()
    connection.close()
    monkeypatch.setattr(monitoring, "probe", lambda *_args: pytest.fail("API must not perform network probes"))
    code, result = http_server("POST", "/monitoring", {"action": "save", "url": "example.com"})
    assert code == 200 and len(result["targets"]) == 1
    code, result = http_server("GET", "/monitoring")
    assert code == 200 and not result["notifications_enabled"]
    assert result["targets"][0]["url"] == "https://example.com/"
    assert http_server("POST", "/monitoring", {"action": "remove", "key": result["targets"][0]["key"]})[0] == 200


def test_corrupt_monitor_file_is_preserved_and_dashboard_still_loads(http_server, runtime, monkeypatch):
    path = runtime / "website-monitoring.json"
    path.write_text("{invalid", encoding="utf-8")
    monkeypatch.setattr(agent, "_monitor_instance", None)
    monkeypatch.setattr(agent, "_system_status_payload", lambda: {})
    assert http_server("GET", "/monitoring")[0] == 503
    code, result = http_server("GET", "/status")
    assert code == 200 and result["alerts"][0]["source"] == "monitoring"
    assert path.read_text() == "{invalid"


def test_webhook_application_error_is_delivery_failure(monkeypatch):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return b'{"errcode": 40001, "errmsg": "invalid token"}'
    monkeypatch.setattr(agent, "urlopen", lambda *args, **kwargs: Response())
    with pytest.raises(RuntimeError, match="拒绝"):
        agent._http_post_json("https://example.test/webhook", {})


def test_smtp_partial_refusal_is_reported(monkeypatch):
    class SMTP:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def send_message(self, _message):
            return {"rejected@example.test": (550, b"refused")}
    monkeypatch.setattr(agent.smtplib, "SMTP_SSL", lambda *a, **kw: SMTP())
    with pytest.raises(RuntimeError, match="收件人"):
        agent._send_email_notification({"smtp_host": "smtp.example.test", "from_addr": "sender@example.test",
                                        "to_addrs": "rejected@example.test"}, "test", "test")


def test_sampler_runs_bounded_probes_and_keeps_legacy_health_checks(monitor, monkeypatch):
    for i in range(6):
        monitor.operation({"action": "save", "url": f"https://example{i}.test"})
    lock = threading.Lock()
    running = [0, 0]
    sleep = time.sleep

    def probe(target, check_cert):
        with lock:
            running[0] += 1
            running[1] = max(running[1], running[0])
        sleep(0.02)
        with lock:
            running[0] -= 1
        return {"checked_at": monitor.clock(), "status": "healthy", "duration_ms": 20}

    ticks = [0]

    def tick(_seconds):
        ticks[0] += 1
        if ticks[0] >= 30:
            raise StopIteration
        sleep(0.01)

    monkeypatch.setattr(agent, "_monitor", lambda: monitor)
    monkeypatch.setattr(agent, "_system_status_payload", lambda: {})
    monkeypatch.setattr(agent, "_notification_config_payload", lambda **kw: {})
    monkeypatch.setattr(agent, "_health_status", {})
    monkeypatch.setattr(agent, "PROJECTS", {"legacy": agent.Site(key="legacy", name="Legacy", health_url="http://example.test/health")})
    monkeypatch.setattr(agent, "_probe_health_url", lambda site: {"status": "healthy", "detail": "HTTP 200"})
    monkeypatch.setattr(monitoring, "probe", probe)
    monkeypatch.setattr(agent.time, "sleep", tick)
    with pytest.raises(StopIteration):
        agent._health_check_sampler()
    assert len(monitor.results) == 6
    assert 1 <= running[1] <= 4
    assert agent._health_status["legacy"]["status"] == "healthy"


@pytest.mark.parametrize("width", [390, 1440])
def test_actual_monitor_ui_add_edit_pause_remove_and_certificates(http_server, runtime, width):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as browser_runtime:
        browser = browser_runtime.chromium.launch()
        try:
            context = browser.new_context(viewport={"width": width, "height": 960})
            context.add_cookies([{"name": agent.COOKIE_NAME, "value": http_server.cookie, "url": http_server.origin}])
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(http_server.origin + "/ui?view=events")
            page.locator("#monitorUrl").fill("example.com/health")
            page.locator("#monitorSave").click()
            playwright.expect(page.locator("#monitorTargets")).to_contain_text("https://example.com/health")
            monitor = agent._monitor()
            target = next(iter(monitor.targets.values()))
            monitor.accept(dict(target), {"checked_at": time.time(), "status": "healthy", "code": 200,
                                         "detail": "HTTP 200", "duration_ms": 1234.56789,
                                         "certificate": {"status": "valid", "expires_at": time.time() + 86400 * 9,
                                                         "checked_at": time.time(), "issuer": "Test CA", "detail": "证书校验通过"}})
            page.evaluate("window.WebsiteMonitoring.refresh()")
            playwright.expect(page.locator("#monitorTargets")).to_contain_text("1.23 s")
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(runtime / f"website-monitor-{width}.png"), full_page=True)
            page.locator("#monitorMute").click()
            playwright.expect(page.locator("#monitorMute")).to_have_text("恢复通知")
            assert monitor.muted_until > time.time()
            page.locator("#certificatesViewTab").click()
            playwright.expect(page.locator("#monitorCertificates")).to_contain_text("Test CA")
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(runtime / f"website-certificates-{width}.png"), full_page=True)
            page.locator("#eventsViewTab").click()
            page.locator(".monitor-actions summary").click()
            page.locator('[data-monitor-action="toggle"]').click()
            playwright.expect(page.locator("#monitorTargets")).to_contain_text("已暂停")
            page.locator(".monitor-actions summary").click()
            page.locator('[data-monitor-action="edit"]').click()
            page.locator("#monitorUrl").fill("http://127.0.0.1:6868/health")
            page.locator("#monitorSave").click()
            playwright.expect(page.locator("#monitorTargets")).to_contain_text("http://127.0.0.1:6868/health")
            page.locator(".monitor-actions summary").click()
            page.locator('[data-monitor-action="remove"]').click()
            playwright.expect(page.locator("#confirmMessage")).to_contain_text("服务器文件")
            page.locator("#confirmOkBtn").click()
            playwright.expect(page.locator("#monitorTargets")).to_contain_text("暂无监测网站")
            assert not monitor.targets
            assert not errors
        finally:
            browser.close()
