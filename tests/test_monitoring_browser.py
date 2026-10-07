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
                elif path == "/docker/images":
                    result = {"images": []}
                elif path == "/docker/logs":
                    result = {"container": "web", "lines": ["healthy"]}
                elif path == "/request-gateways":
                    result = {"entries": []}
                elif path == "/request-gateways/networks":
                    result = {"networks": []}
                elif path == "/nginx-requests":
                    result = {"available": True, "enabled": True, "records": [], "containers": []}
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
    assert not any(any(term in path for term in ("projects-config", "preflight", "redeploy", "rollback", "webhook", "/logs"))
                   for path, _, _ in state["calls"])


def test_site_registration_updates_nginx_and_certificates(dashboard):
    page, state, origin = dashboard
    page.goto(f"{origin}/ui?view=certificates")
    page.locator("#siteAdd").click()
    playwright.expect(page.locator("#siteKey")).to_be_hidden()
    for field, value in {"siteName": "API", "siteDomain": "api.example.test", "sitePort": "8001"}.items():
        page.locator(f"#{field}").fill(value)
    page.locator("#siteSave").click()
    playwright.expect(page.locator('[data-site-edit="site-test"]')).to_be_visible()
    playwright.expect(page.locator("#nginxQuickProject")).to_have_value("site-test")
    playwright.expect(page.locator("#certificateProject")).to_have_value("site-test")
    saved = next(payload for path, _, payload in state["calls"] if path == "/sites/save")
    assert set(saved["site"]) == {"key", "name", "app_domain", "service_port", "health_url"}
    assert saved["site"]["key"] == ""
    page.locator('[data-site-edit="site-test"]').click()
    assert page.locator("#siteKey").evaluate("element => element.readOnly")
    page.locator("#siteName").fill("API service")
    page.locator("#siteSave").click()
    playwright.expect(page.locator("#siteRegistryList")).to_contain_text("API service")
    assert [payload for path, _, payload in state["calls"] if path == "/sites/save"][-1]["original_key"] == "site-test"
    state["refuse_delete"] = True
    page.locator('[data-site-delete="site-test"]').click()
    page.locator("#confirmOkBtn").click()
    playwright.expect(page.locator("#siteFeedback")).to_contain_text("Managed Nginx")
    playwright.expect(page.locator('[data-site-edit="site-test"]')).to_be_visible()
    state["refuse_delete"] = False
    page.locator('[data-site-delete="site-test"]').click()
    page.locator("#confirmOkBtn").click()
    playwright.expect(page.locator("#siteRegistryList")).to_contain_text("暂无站点")
    assert not state["errors"]


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
def test_site_form_and_navigation_layout(dashboard, tmp_path, width):
    page, state, origin = dashboard
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{origin}/ui?view=certificates")
    page.locator("#siteAdd").click()
    page.locator("#siteName").fill("Long site name for layout verification")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    for selector in ["#siteName", "#sitePort", "#siteSave", "#siteCancel"]:
        box = page.locator(selector).bounding_box()
        assert box and box["width"] > 0 and box["x"] >= 0 and box["x"] + box["width"] <= width
    image = tmp_path / f"sites-{width}.png"
    page.screenshot(path=str(image), full_page=True)
    print(f"Screenshot: {image}")
    assert not state["errors"]
