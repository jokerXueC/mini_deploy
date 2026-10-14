import time
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts.preview_operations import examples
from test_monitoring_browser import dashboard as dashboard, playwright


@pytest.fixture
def operations(dashboard):
    page, state, origin = dashboard
    inventory, evidence = examples()
    control = {"calls": [], "logs": ['2026-10-12T10:00:00Z ERROR <script>window.bad=1</script> crash',
                                      '2026-10-12T10:00:01Z INFO recovered'], "fail": False}

    def api(route):
        parsed = urlsplit(route.request.url)
        query = parse_qs(parsed.query)
        control["calls"].append((parsed.path, query))
        if parsed.path == "/operations/inventory":
            result = inventory
        elif parsed.path == "/operations/incident":
            result = evidence
        else:
            if control["fail"]:
                route.fulfill(status=503, json={"detail": "日志读取超时"})
                return
            lines = control["logs"]
            if query.get("level") == ["error"]:
                lines = lines[:1]
            result = {"lines": lines, "name": "api", "checked_at": time.time(), "notice": "测试读取范围"}
        route.fulfill(json=result)

    page.route("**/operations/*", api)
    page.route("**/ssh", lambda r: r.fulfill(json={"enabled": False, "profiles": [], "sessions": []}))
    page.route("**/monitoring", lambda r: r.fulfill(json={"targets": [], "alerts": [evidence["incident"]],
                                                        "events": evidence["timeline"], "coverage": {}}))
    page.goto(f"{origin}/ui?view=events")
    yield page, state, control
    assert not state["errors"]


@pytest.mark.parametrize("width", [1440, 390])
def test_incident_drilldown_keeps_modal_open_and_logs_safe(operations, tmp_path, width):
    page, _, control = operations
    page.set_viewport_size({"width": width, "height": 900})
    page.locator("#alertList summary").click()
    page.locator("#alertList [data-incident-diagnose]").click()
    playwright.expect(page.locator("#operationsDialog")).to_be_visible()
    playwright.expect(page.locator("#opsIncident")).to_contain_text("137")
    playwright.expect(page.locator("#opsIncident")).to_contain_text("当前状态")
    playwright.expect(page.locator("#opsAnalyze")).to_be_enabled()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    bounds = page.locator("#operationsDialog").bounding_box()
    assert 0 <= bounds["y"] and bounds["y"] + bounds["height"] <= 901
    page.screenshot(path=str(tmp_path / f"diagnostics-incident-{width}.png"), full_page=True)
    page.locator("#opsIncidentLogs").click()
    playwright.expect(page.locator("#opsLogLines")).to_contain_text("<script>")
    assert page.evaluate("window.bad") is None
    assert control["calls"][-1][1]["source"] == ["container:" + "a" * 64]
    assert "since" in control["calls"][-1][1] and "until" in control["calls"][-1][1]
    page.screenshot(path=str(tmp_path / f"diagnostics-logs-{width}.png"), full_page=True)
    page.mouse.click(2, 2)
    playwright.expect(page.locator("#operationsDialog")).to_be_visible()
    page.keyboard.press("Escape")
    playwright.expect(page.locator("#operationsDialog")).not_to_be_visible()


def test_process_port_links_and_keyboard_tabs(operations):
    page, _, control = operations
    page.locator("#serverViewTab").click()
    page.locator('[data-operations-open="processes"]').click()
    playwright.expect(page.locator("#opsProcesses")).to_contain_text("python")
    page.locator('[data-ops-pid-ports="1826"]').click()
    playwright.expect(page.locator("#opsPorts")).to_contain_text("8766")
    page.locator('[data-ops-owner="1826"]').click()
    playwright.expect(page.locator("#opsProcessSearch")).to_have_value("1826")
    page.locator("#opsTabProcesses").focus()
    page.keyboard.press("ArrowRight")
    playwright.expect(page.locator("#opsTabPorts")).to_have_attribute("aria-selected", "true")
    page.locator("#opsTabProcesses").click()
    page.locator('[data-ops-log-source="container:' + "a" * 64 + '"]').click()
    playwright.expect(page.locator("#opsLogLines")).to_contain_text("ERROR")
    assert all(path.startswith("/operations/") for path, _ in control["calls"])


def test_log_filters_pause_download_and_read_errors(operations):
    page, _, control = operations
    page.locator("#serverViewTab").click()
    page.locator('[data-operations-open="logs"]').click()
    page.locator("#opsLogSource").select_option("container:" + "a" * 64, force=True)
    playwright.expect(page.locator("#opsLogLines")).to_contain_text("INFO")
    page.locator("#opsLogLevel").select_option("error", force=True)
    playwright.expect(page.locator("#opsLogLines")).not_to_contain_text("INFO")
    with page.expect_download() as download:
        page.locator("#opsLogDownload").click()
    assert download.value.suggested_filename.endswith(".log")
    page.clock.install()
    page.locator("#opsLogPause").click()
    page.clock.run_for(5200)
    playwright.expect(page.locator("#opsLogNotice")).to_contain_text("测试读取范围")
    page.locator("#opsLogPause").click()
    calls = len(control["calls"])
    page.clock.run_for(10000)
    assert len(control["calls"]) == calls
    control["fail"] = True
    page.locator("#opsRefresh").click()
    playwright.expect(page.locator("#opsLogNotice")).to_contain_text("日志读取超时")
    playwright.expect(page.locator("#opsLogDownload")).to_be_disabled()


def test_ai_entry_opens_panel_scope_without_ssh_or_missing_model_submission(operations):
    page, _, _ = operations
    page.locator("#alertList summary").click()
    page.locator("#alertList [data-incident-diagnose]").click()
    playwright.expect(page.locator("#opsAnalyze")).to_be_enabled()
    received = []
    page.expose_function("captureScope", lambda scope: received.append(scope))
    page.evaluate("window.PanelAI = {open: captureScope}")
    page.locator("#opsAnalyze").click()
    playwright.expect(page.locator("#eventsView")).to_have_class("dashboard-view active")
    page.wait_for_timeout(100)
    assert received[0]['kind'] == 'incident' and received[0]['id']
