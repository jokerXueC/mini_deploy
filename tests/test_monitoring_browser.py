"""Frontend contract tests; all API requests are mocked, including mutations."""
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

playwright = pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def dashboard():
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(ROOT), **kwargs)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    state = {"sites": [], "calls": [], "errors": [], "refuse_delete": False}
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda error: state["errors"].append(str(error)))

            def api(route):
                path = urlsplit(route.request.url).path
                if path.startswith("/ui/"):
                    route.continue_()
                    return
                if path == "/ui":
                    route.fulfill(path=str(ROOT / "ui/index.html"), content_type="text/html")
                    return
                payload = json.loads(route.request.post_data or "{}")
                state["calls"].append((path, route.request.method, payload))
                result = {}
                if path == "/status":
                    result = {"agent": {"status": "ok"}, "system": {"docker": {"available": True,
                        "containers": [{"name": "web", "id": "123456789abcdef", "state": "running"}]}},
                        "alerts": [], "events": [], "csrf_token": "test-token"}
                elif path == "/system-metrics":
                    result = {"server": {}}
                elif path == "/sites":
                    result = {"sites": state["sites"], "notifications": {}}
                elif path == "/sites/save":
                    site = dict(payload["site"])
                    site["key"] = site.get("key") or "site-test"
                    state["sites"] = [item for item in state["sites"]
                                      if item["key"] != payload.get("original_key", site["key"])] + [site]
                elif path == "/sites/delete":
                    if state["refuse_delete"]:
                        route.fulfill(status=409, json={"error": "Managed Nginx configuration or certificate exists"})
                        return
                    state["sites"] = [item for item in state["sites"] if item["key"] != payload["key"]]
                elif path == "/monitoring":
                    result = {"targets": [], "alerts": [], "events": [], "notifications_enabled": False}
                elif path == "/notifications":
                    result = {"notifications": payload.get("notifications", {})}
                elif path == "/nginx-settings":
                    result = {"configured": True, "profile": {"mode": "local", "container": "", "network": "host"},
                        "upstreams": {}, "projects": [{"key": item["key"], "name": item["name"],
                        "domain": item["app_domain"], "port": item["service_port"]} for item in state["sites"]]}
                elif path == "/certificates":
                    result = {"projects": [{"project": item["key"], "name": item["name"],
                        "domain": item["app_domain"]} for item in state["sites"]]}
                elif path == "/certificate-discovery":
                    result = {**state.get("discovery", {"items": [], "running": False, "last_scan": 1}), "csrf_token": "test-token"}
                    if payload.get("action") == "replace":
                        result = {"message": "证书已替换", "backup": "/data/certificate-backups/example"}
                elif path == "/docker/images":
                    result = {"images": []}
                elif path == "/docker/logs":
                    result = {"container": "web", "lines": ["healthy"]}
                elif path == "/request-gateways":
                    result = {"entries": state.get("gateways", [])}
                elif path == "/gateway-connections/discover":
                    result = {"sources": [item["source"] for item in state.get("connections", [])]}
                elif path == "/gateway-connections":
                    if payload["action"] == "inspect":
                        item = next(item for item in state["connections"] if item["source"] == payload["source"])
                        if item.get("error"):
                            route.fulfill(status=400, json={"detail": item["error"]})
                            return
                        result = item
                    elif payload["action"] == "preview":
                        if state.get("preview_error"):
                            route.fulfill(status=409, json={"detail": state["preview_error"]})
                            return
                        result = {"gateway": {"key": payload["key"]}, "token": "review-token",
                            "site": "api.example.com", "before_rule": "backend:8000", "after_rule": "gateway → backend:8000",
                            "notice": "应用后将重载入口配置"}
                    else:
                        assert payload["action"] == "apply" and payload["confirmed"] and payload["token"] == "review-token"
                elif path == "/request-gateways/networks":
                    result = {"networks": []}
                elif path == "/nginx-requests":
                    result = {"available": True, "enabled": True, "records": [], "containers": []}
                elif path == "/gateway-requests":
                    result = {"records": state.get("records", []), "notice": "测试请求样本"}
                elif path != "/docker/action":
                    state["errors"].append(f"Unexpected API: {path}")
                    route.fulfill(status=404, json={"error": "Removed endpoint"})
                    return
                if route.request.method == "POST":
                    assert route.request.headers.get("x-csrf-token") == "test-token"
                route.fulfill(json=result)

            page.route(f"{origin}/**", api)
            yield page, state, origin
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


@pytest.mark.parametrize("view", ["deploy", "server", "requests", "certificates", "unknown"])
def test_routes_and_monitoring_only_requests(dashboard, view):
    page, state, origin = dashboard
    page.goto(f"{origin}/ui?view={view}")
    expected = view if view in ("requests", "certificates") else "server"
    playwright.expect(page.locator(f"#{expected}View")).to_have_class("dashboard-view active")
    assert page.locator("#deployView, #projectModal, #redeployBtn, #deployViewTab, #projectSwitcher").count() == 0
    page.clock.install()
    for tab in ["server", "events", "notify", "certificates", "requests"]:
        page.locator(f"#{tab}ViewTab").click()
        page.wait_for_timeout(100)
        page.clock.run_for(31000)
    assert not state["errors"]


def connection_source(name="proxy", networks=None):
    return {"source": {"kind": "caddy", "mode": "docker", "container": name,
                "config_path": "/etc/caddy/Caddyfile", "label": name},
            "revision": "original-revision", "networks": networks or ["app_default"], "routes": [
                {"id": "static", "site": "example.com", "kind": "static", "label": "静态网站", "supported": True},
                {"id": "api", "site": "api.example.com", "kind": "proxy", "label": "handle /api/*",
                 "upstream": "http://backend:8000", "supported": True}]}


def test_cpu_text_is_stable_while_bars_and_chart_receive_new_samples(dashboard):
    page, state, origin = dashboard
    page.goto(f"{origin}/ui")
    playwright.expect(page.locator("#updatedAt")).to_contain_text("服务器刷新于")
    page.clock.install()
    page.evaluate("activeView = 'requests'; clearServerRefresh()")

    def sample(cpu, offset):
        page.evaluate("""([cpu, offset]) => {
            const ts = Math.floor(Date.now() / 1000) + offset;
            renderSystemStatus({server: {cpu_percent: cpu, sampled_ts: ts},
                realtime_history: [{ts, cpu_percent: cpu}, {ts: ts - 1, cpu_percent: 12}],
                docker: {available: true, containers: [{name: 'api', state: 'running', cpu_percent: cpu}]}});
        }""", [cpu, offset])

    sample(12, 1)
    playwright.expect(page.locator("#cpuValue")).to_have_text("12%")
    page.clock.run_for(1000)
    sample(80, 2)
    playwright.expect(page.locator("#cpuValue")).to_have_text("12%")
    playwright.expect(page.locator(".trend-card.cpu .trend-card-head strong")).to_have_text("12%")
    playwright.expect(page.locator(".meter-line").first.locator("strong")).to_have_text("12%")
    assert page.locator("#cpuBar").get_attribute("aria-valuenow") == "80"
    page.clock.run_for(1100)
    assert float(page.locator("#cpuBar").get_attribute("data-display-value")) > 60
    page.clock.run_for(1000)
    sample(30, 3)
    playwright.expect(page.locator("#cpuValue")).to_have_text("30%")
    playwright.expect(page.locator(".trend-card.cpu .trend-card-head strong")).to_have_text("30%")
    playwright.expect(page.locator(".meter-line").first.locator("strong")).to_have_text("30%")
    assert not state["errors"]


def connection_calls(state, action):
    return [payload for path, _, payload in state["calls"]
            if path == "/gateway-connections" and payload.get("action") == action]


@pytest.mark.parametrize("width", [390, 1440])
def test_request_tree_ids_refresh_search_and_original_paths(dashboard, tmp_path, width):
    page, state, origin = dashboard
    state["gateways"] = [{"key": "api", "name": "API", "upstream": "http://backend:8000", "state": "running"}]
    first_id = "01a10599-bccc-77d3-81b9-1538746408ce"
    second_id = "01a10599-bccc-77d3-81b9-1538746408cf"
    samples = [("heartbeat", 12, 200), ("commands/claim", 30, 200),
        (f"sessions/{first_id}/mode", 100, 200), (f"sessions/{second_id}/mode", 300, 500),
        (f"sessions/{first_id}/pending-inputs", 40, 200)]
    state["records"] = [{"path": f"/cloud/runtime/{path}", "method": "GET", "host": "aimore.meetpeak.tech",
        "at": "2026-10-07T12:00:00Z", "status": status, "duration_ms": duration, "upstream_ms": duration - 2}
        for path, duration, status in samples]
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(f"{origin}/ui?view=requests")
    root = page.locator("#requestRows > .request-branch")
    playwright.expect(root).to_have_count(1)
    playwright.expect(root.locator(":scope > summary code")).to_have_text("/cloud/runtime/")
    playwright.expect(root.locator(':scope > summary [data-label="次数"]')).to_have_text("5")
    playwright.expect(root.locator(':scope > summary [data-label="平均耗时"]')).to_have_text("96.40 ms")
    assert root.get_attribute("open") is None
    page.screenshot(path=str(tmp_path / f"request-tree-collapsed-{width}.png"), full_page=True)
    root.locator(":scope > summary").click()
    sessions = page.locator('.request-branch').filter(has=page.locator(':scope > summary code[title="/cloud/runtime/sessions/:id"]'))
    assert sessions.get_attribute("open") is None
    sessions.locator(":scope > summary").click()
    mode = page.locator('.request-group').filter(has=page.locator(':scope > summary code[title="/cloud/runtime/sessions/:id/mode"]'))
    playwright.expect(mode.locator(':scope > summary [data-label="次数"]')).to_have_text("2")
    playwright.expect(mode.locator(':scope > summary [data-label="平均耗时"]')).to_have_text("200.00 ms")
    mode.locator(":scope > summary").click()
    playwright.expect(mode.locator(".request-original-path").first).to_contain_text(first_id)
    mode.evaluate("el => el.dataset.retained = 'yes'")
    state["records"].append({**state["records"][2], "duration_ms": 200})
    page.locator("#requestRefresh").click()
    playwright.expect(root.locator(':scope > summary [data-label="次数"]')).to_have_text("6")
    assert mode.get_attribute("data-retained") == "yes" and mode.get_attribute("open") is not None
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / f"request-tree-expanded-{width}.png"), full_page=True)
    page.locator("#requestSearch").fill(second_id)
    playwright.expect(page.locator("#requestSampleSummary")).to_contain_text("1 类请求")
    playwright.expect(page.locator('#requestRows .request-group > summary [data-label="次数"]')).to_have_text("3")
    page.locator("#requestStatus-button").scroll_into_view_if_needed()
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    page.locator("#requestStatus-button").click()
    page.locator("#requestStatus-menu").get_by_role("option", name="含失败请求").click()
    playwright.expect(page.locator('#requestRows .request-group > summary [data-label="次数"]')).to_have_text("3")
    page.locator("#requestLayout-button").click()
    page.locator("#requestLayout-menu").get_by_role("option", name="原始地址").click()
    playwright.expect(page.locator('#requestRows .request-group > summary [data-label="次数"]')).to_have_text("1")
    playwright.expect(page.locator("#requestRows > .request-group > summary code")).to_contain_text(second_id)
    assert not state["errors"]


@pytest.mark.parametrize("width", [390, 1440])
def test_backend_onboarding_uses_detected_defaults_and_requires_confirmation(dashboard, tmp_path, width):
    page, state, origin = dashboard
    state["connections"] = [connection_source()]
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(f"{origin}/ui?view=requests")
    page.locator("#gatewayConnectOpen").click()
    playwright.expect(page.locator("#connectReview")).to_be_visible()
    assert page.locator("#connectService option").count() == 2  # Placeholder and backend; no static site.
    playwright.expect(page.locator("#connectRouteNotice")).to_contain_text("http://backend:8000")
    assert not page.locator("#connectSourceAdvanced").get_attribute("open")
    assert not page.locator("#connectOptionsAdvanced").get_attribute("open")
    playwright.expect(page.locator("#connectNetworkField")).to_be_hidden()
    assert not connection_calls(state, "apply")
    preview = connection_calls(state, "preview")[0]
    assert preview["route_id"] == "api" and preview["network"] == "app_default"
    assert preview["source_revision"] == "original-revision" and preview["probe_path"] == "/api/"
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / f"backend-connect-{width}.png"), full_page=True)
    page.locator("#connectApply").click()
    playwright.expect(page.locator("#gatewayConnectPanel")).to_be_hidden()
    assert len(connection_calls(state, "apply")) == 1
    assert not state["errors"]


def test_backend_discovery_preserves_partial_results_and_network_choice(dashboard):
    page, state, origin = dashboard
    failed = {**connection_source("unreadable"), "error": "配置无法读取"}
    state["connections"] = [connection_source(networks=["app_default", "other"]), failed]
    page.goto(f"{origin}/ui?view=requests")
    page.locator("#gatewayConnectOpen").click()
    playwright.expect(page.locator("#gatewayConnectFeedback")).to_contain_text("无法唯一确定网络")
    playwright.expect(page.locator("#connectDiscoveryNotice")).to_contain_text("unreadable")
    assert not connection_calls(state, "preview")
    page.locator("#connectNetwork-button").click()
    page.locator("#connectNetwork-menu").get_by_role("option", name="app_default", exact=True).click()
    playwright.expect(page.locator("#connectReview")).to_be_visible()
    assert connection_calls(state, "preview")[-1]["network"] == "app_default"
    page.locator("#connectBack").click()
    page.locator("#connectOptionsAdvanced > summary").click()
    page.locator("#connectPort").fill("18090")
    playwright.expect(page.locator("#connectReview")).to_be_hidden()
    assert not connection_calls(state, "apply")
    page.locator("#connectPreview").click()
    playwright.expect(page.locator("#connectReview")).to_be_visible()
    assert connection_calls(state, "preview")[-1]["port"] == 18090
    assert not state["errors"]


def test_multiple_backends_and_preview_failure_do_not_apply_stale_plan(dashboard):
    page, state, origin = dashboard
    second = connection_source("another-proxy")
    second["routes"][1]["upstream"] = "http://java-service:8080"
    state["connections"] = [connection_source(), second]
    page.goto(f"{origin}/ui?view=requests")
    page.locator("#gatewayConnectOpen").click()
    playwright.expect(page.locator("#connectService-button")).to_be_enabled()
    page.locator("#connectService-button").click()
    page.locator("#connectService-menu").get_by_role("option", name="http://backend:8000", exact=False).click()
    playwright.expect(page.locator("#connectReview")).to_be_visible()
    state["preview_error"] = "入口配置已修改，请重新检测"
    page.locator("#connectService-button").click()
    page.locator("#connectService-menu").get_by_role("option", name="http://java-service:8080", exact=False).click()
    playwright.expect(page.locator("#gatewayConnectFeedback")).to_contain_text("入口配置已修改")
    playwright.expect(page.locator("#connectReview")).to_be_hidden()
    assert not connection_calls(state, "apply")
    assert not state["errors"]


def test_no_backend_does_not_silently_select_static_site(dashboard):
    page, state, origin = dashboard
    item = connection_source()
    item["routes"] = item["routes"][:1]
    state["connections"] = [item]
    page.goto(f"{origin}/ui?view=requests")
    page.locator("#gatewayConnectOpen").click()
    playwright.expect(page.locator("#gatewayConnectFeedback")).to_contain_text("未找到可自动接入的后端")
    assert not connection_calls(state, "preview")
    page.locator("#connectSourceAdvanced > summary").click()
    page.locator("#connectIncludeStatic").check()
    assert page.locator("#connectService option").count() == 2
    assert not connection_calls(state, "apply")
    assert not state["errors"]


@pytest.mark.parametrize("width", [390, 1440])
def test_default_sites_collapsed_and_certificate_upload_has_one_entry(dashboard, tmp_path, width):
    page, state, origin = dashboard
    base = {"referenced": True, "active": True, "source": "Docker · proxy", "kind": "nginx", "renewal": "原服务管理"}
    state["discovery"] = {"running": False, "last_scan": 1791356400, "items": [
        {**base, "id": "site", "domains": ["api.example.test"], "can_replace": True, "tls": True,
         "certificate": {"days_remaining": 30, "expires_at": 1793962000, "fingerprint": "abc"}},
        {**base, "id": "gateway", "domains": ["_"], "tls": False},
        {**base, "id": "default", "domains": [], "kind": "caddy", "tls": True},
        {**base, "id": "dash", "domains": ["-"], "tls": False},
        {**base, "id": "wildcard", "domains": ["*.example.test"], "tls": True}]}
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{origin}/ui?view=certificates")
    playwright.expect(page.locator("#discoverySites")).to_contain_text("api.example.test")
    playwright.expect(page.locator("#discoveryInternalCount")).to_have_text("(3)")
    playwright.expect(page.locator('[data-discovery-id="gateway"]')).to_be_hidden()
    assert page.locator('#certificateAdvanced, #siteAdd, #nginxSettingsForm, #certificateProject').count() == 0
    assert page.locator('[data-discovery-id="wildcard"] [data-discovery-action="check"]').count() == 0
    page.screenshot(path=str(tmp_path / f"certificates-folded-{width}.png"), full_page=True)
    page.locator("#discoveryInternal > summary").click()
    playwright.expect(page.locator('[data-discovery-id="gateway"]')).to_be_visible()
    assert page.locator('#discoveryInternal [data-discovery-action="check"]').count() == 0
    page.locator("#discoveryScan").click()
    page.wait_for_timeout(100)
    assert page.locator("#discoveryInternal").get_attribute("open") is not None
    page.locator('[data-discovery-action="replace"]').click()
    page.locator("#discoveryCertFile").set_input_files({"name": "cert.pem", "mimeType": "text/plain", "buffer": b"certificate"})
    page.locator("#discoveryKeyFile").set_input_files({"name": "private.key", "mimeType": "text/plain", "buffer": b"private-key"})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    for selector in ("#discoveryCertFile", "#discoveryKeyFile", "#discoveryReplaceSave"):
        box = page.locator(selector).bounding_box()
        assert box and box["x"] >= 0 and box["x"] + box["width"] <= width
    page.screenshot(path=str(tmp_path / f"certificates-upload-{width}.png"), full_page=True)
    page.locator("#discoveryReplaceSave").click()
    page.locator("#confirmOkBtn").click()
    playwright.expect(page.locator("#discoveryFeedback")).to_contain_text("证书已替换")
    request = next(payload for path, _, payload in state["calls"] if path == "/certificate-discovery" and payload.get("action") == "replace")
    assert request == {"action": "replace", "id": "site", "fingerprint": "abc", "certificate": "certificate", "private_key": "private-key"}
    assert not any(path in ("/nginx-settings", "/certificates", "/sites", "/sites/save") for path, _, _ in state["calls"])
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert not state["errors"]
    assert not any(any(term in path for term in ("projects-config", "preflight", "redeploy", "rollback", "webhook", "/logs"))
                   for path, _, _ in state["calls"])




def test_docker_actions_and_notification_save_do_not_fetch_deployment(dashboard):
    page, state, origin = dashboard
    page.goto(f"{origin}/ui?view=deploy")
    page.locator('[data-container-action="restart"]').click()
    page.locator("#confirmOkBtn").click()
    playwright.expect(page.locator("#dockerFeedback")).to_contain_text("已重启")
    page.locator("#notifyViewTab").click()
    page.locator("#saveNotificationBtn").click()
    playwright.expect(page.locator("#notificationResult")).to_contain_text("已保存")
    assert any(path == "/notifications" and method == "POST" for path, method, _ in state["calls"])
    assert not state["errors"]




@pytest.mark.parametrize("width", [390, 1440])
def test_discovery_default_view_and_actions(dashboard, tmp_path, width):
    page, state, origin = dashboard
    state["discovery"] = {"running": False, "last_scan": 1791356400, "progress": "已发现 3 项", "issues": [], "items": [
        {"id": "a", "domains": ["meetpeak.tech"], "source": "Docker · aimore-caddy-1", "kind": "caddy",
         "referenced": True, "active": True, "tls": True, "renewal": "自动管理", "certificate_candidate": True,
         "certificate": {"days_remaining": 62, "expires_at": 1796713200, "issuer": "Let's Encrypt"}, "can_replace": False},
        {"id": "b", "domains": ["api.example.test"], "source": "服务器", "kind": "nginx", "referenced": True,
         "active": True, "tls": True, "renewal": "原服务管理", "can_replace": True,
         "certificate": {"days_remaining": 7, "expires_at": 1791961200, "issuer": "Example CA", "fingerprint": "abc"}},
        {"id": "c", "domains": ["old.example.test"], "source": "常见证书目录", "kind": "file", "referenced": False,
         "active": False, "certificate_path": "/etc/nginx/ssl/old.crt", "certificate": {"days_remaining": -3, "expires_at": 1791097200}}]}
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{origin}/ui?view=certificates")
    playwright.expect(page.locator("#discoverySites")).to_contain_text("meetpeak.tech")
    assert page.locator("#siteForm, #certificateAdvanced").count() == 0
    assert not any(path in ("/nginx-settings", "/certificates", "/sites") for path, _, _ in state["calls"])
    playwright.expect(page.locator("#discoveryFiles")).to_be_hidden()
    page.locator('[data-discovery-action="check"][data-id="a"]').click()
    page.wait_for_timeout(100)
    assert any(path == "/certificate-discovery" and payload == {"action": "check", "id": "a"} for path, _, payload in state["calls"])
    page.locator('[data-discovery-action="replace"]').click()
    playwright.expect(page.locator("#discoveryReplace")).to_be_visible()
    page.locator("#discoveryReplaceCancel").click()
    playwright.expect(page.locator("#discoveryReplace")).to_be_hidden()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(tmp_path / f"discovery-{width}.png"), full_page=True)
    assert not state["errors"]
