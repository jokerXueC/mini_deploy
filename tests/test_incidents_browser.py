"""Anomaly center contracts with local incident examples and mocked mutations."""
from copy import deepcopy
import re
import time

import pytest

from test_monitoring_browser import dashboard as dashboard_fixture, playwright

dashboard = dashboard_fixture


@pytest.fixture
def incidents(dashboard):
    page, state, origin = dashboard
    now = int(time.time())
    examples = [
        ("docker:api:oom", "api 内存溢出退出", "docker", "critical",
         {"container": "api", "container_id": "api-123", "project": "store", "service": "api",
          "exit_code": 137, "oom_killed": True, "reason": "oom_killed"}),
        ("docker:worker:restart", "worker 反复重启", "docker", "warning",
         {"name": "worker", "id": "worker-123", "compose_project": "store", "compose_service": "worker",
          "restart_count": 12, "restart_delta_5m": 8, "window_seconds": 300, "restart_policy": "always",
          "started_at": "2026-10-11T12:00:00Z", "finished_at": "2026-10-11T11:59:00Z", "reason": "restart_storm"}),
        ("docker:db:health", "db 健康检查失败", "docker", "critical",
         {"container": "db", "project": "<script>bad()</script>", "health_last_check": {"exit_code": 1, "end": "2026-10-11T12:00:00Z"}, "oom_killed": False}),
        ("server:disk", "磁盘占用 93%", "server", "warning", {}),
        ("certificate:shop", "shop 证书剩余 6 天", "certificate", "critical", {}),
    ]
    alerts = [{"id": key, "incident_id": f"incident-{i}", "title": title, "detail": title,
               "source": source, "level": level, "first_seen": now - 180,
               "active": True, "stale": False, "acknowledged_at": None, "metadata": meta}
              for i, (key, title, source, level, meta) in enumerate(examples)]
    recovery = {"id": "event-recovery", "incident_id": "old-healthy", "kind": "recovery",
                "title": "网站已恢复", "detail": "HTTP 200", "source": "website", "level": "success",
                "at_ts": now - 60, "duration_seconds": 125, "metadata": {}}
    missing = {"id": "event-missing", "incident_id": "old-container", "kind": "alert",
               "title": "配置容器消失：queue", "source": "docker", "level": "critical",
               "at_ts": now - 70, "metadata": {"container": "queue", "reason": "configured_container_missing"}}
    data = {"targets": [], "alerts": alerts, "events": [recovery, missing],
            "coverage": {"server": {"status": "healthy", "checked_at": now},
                         "docker": {"status": "healthy", "checked_at": now}},
            "history_retention_days": 30, "history_limit": 1000, "notifications_enabled": False}
    control = {"snapshot": data, "posts": [], "fail_get": False, "fail_post": False, "gets": 0, "summary_gets": 0}

    def monitoring(route):
        if route.request.method == "POST":
            payload = route.request.post_data_json
            control["posts"].append(payload)
            assert route.request.headers.get("x-csrf-token") == "test-token"
            if control["fail_post"]:
                route.fulfill(status=409, json={"detail": "异常已变化，请刷新"})
                return
            if payload["action"] in ("acknowledge", "acknowledge_all"):
                for alert in control["snapshot"]["alerts"]:
                    if alert["acknowledged_at"]:
                        continue
                    if payload["action"] == "acknowledge":
                        if alert["id"] != payload["key"]:
                            continue
                        assert payload["incident_id"] == alert["incident_id"]
                    alert["acknowledged_at"] = now
                    control["snapshot"]["events"].insert(0, {
                        **deepcopy(alert), "id": f"ack-{alert['incident_id']}", "kind": "acknowledged", "at_ts": now})
            elif payload["action"] == "save":
                control["snapshot"]["targets"] = [{"key": "local-probe", "name": "Local",
                    "url": payload["url"], "enabled": True, "result": {}}]
        else:
            summary = 'summary=1' in route.request.url
            control["summary_gets" if summary else "gets"] += 1
            if control["fail_get"]:
                route.fulfill(status=503, json={"detail": "采样服务暂不可用"})
                return
            if summary:
                alerts = [item for item in control['snapshot']['alerts'] if item.get('active') is not False]
                route.fulfill(json={"active_count": len(alerts),
                                    "unacknowledged_count": sum(not item.get('acknowledged_at') for item in alerts)})
                return
        route.fulfill(json=control["snapshot"])

    page.route(re.compile(r'/monitoring(?:\?.*)?$'), monitoring)
    # Old dismissals deliberately match the alert's legacy signature.
    page.add_init_script("try { localStorage.setItem('mini_deploy-dismissed-notices', JSON.stringify(['alert||critical|api 内存溢出退出|docker|', 'event|recovery|success|网站已恢复|website||HTTP 200|'])); } catch (_) {}")
    page.goto(f"{origin}/ui?view=events")
    playwright.expect(page.locator("#alertBadge")).to_have_text("5 项异常")
    yield page, state, control
    assert not state["errors"]


def refresh(page):
    page.evaluate("window.WebsiteMonitoring.refresh()")


def choose(page, control_id, value):
    label = page.locator(f"#{control_id}").evaluate(
        "(node, value) => [...node.options].find(option => option.value === value).textContent", value)
    page.locator(f"#{control_id}").select_option(value, force=True)
    playwright.expect(page.locator(f"#{control_id}-button .form-select-value")).to_have_text(label)


@pytest.mark.parametrize('width,theme', [(390, 'dark'), (1440, 'light')])
def test_incident_tab_badge_across_views_filters_recovery_and_failed_refresh(incidents, width, theme, tmp_path):
    page, _, control = incidents
    page.set_viewport_size({'width': width, 'height': 960})
    page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
    badge = page.locator('#eventsTabCount')
    playwright.expect(badge).to_have_text('5')
    choose(page, 'incidentSource', 'docker')
    playwright.expect(badge).to_have_text('5')
    page.locator('#acknowledgeAllBtn').click()
    playwright.expect(page.locator('#incidentFeedback')).to_have_text('当前异常已标为已知晓；恢复前仍保留显示')
    playwright.expect(page.locator('#acknowledgeAllBtn')).to_be_disabled()
    playwright.expect(badge).to_be_hidden()
    page.clock.install()
    page.locator('#serverViewTab').click()
    playwright.expect(page.locator('#eventsViewTab')).to_have_attribute('aria-label', '异常中心，暂无待知晓异常')
    page.clock.run_for(31000)
    playwright.expect(badge).to_be_hidden()
    control['snapshot']['alerts'] = control['snapshot']['alerts'][:2]
    for item in control['snapshot']['alerts']:
        item['acknowledged_at'] = None
    page.clock.run_for(31000)
    playwright.expect(badge).to_have_text('2')
    assert control['summary_gets'] > 0
    tab_box, badge_box = page.locator('#eventsViewTab').bounding_box(), badge.bounding_box()
    assert badge_box['x'] > tab_box['x'] + tab_box['width'] / 2
    assert badge_box['y'] < tab_box['y'] + tab_box['height'] / 2
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path / f'incident-badge-{width}-{theme}.png'))
    control['fail_get'] = True
    page.clock.run_for(31000)
    playwright.expect(badge).to_have_text('2')
    playwright.expect(page.locator('#eventsViewTab')).to_have_attribute('title', '异常数量暂未更新，等待重试；已保留上次结果')
    control['fail_get'] = False
    control['snapshot']['alerts'] = []
    page.clock.run_for(31000)
    playwright.expect(badge).to_be_hidden()
    playwright.expect(page.locator('#eventsViewTab')).to_have_attribute('aria-label', '异常中心，暂无待知晓异常')


def test_acknowledgment_keeps_faults_and_history_and_survives_reload(incidents):
    page, _, control = incidents
    playwright.expect(page.locator("#panelTitle")).to_have_text("异常中心")
    page.locator('[data-incident-ack="docker:api:oom"]').click()
    playwright.expect(page.locator("#alertList")).to_contain_text("已知晓 · 仍异常")
    assert control["posts"][-1] == {"action": "acknowledge", "key": "docker:api:oom", "incident_id": "incident-0"}
    playwright.expect(page.locator("#alertBadge")).to_have_text("5 项异常")
    playwright.expect(page.locator('#eventsTabCount')).to_have_text('4')
    playwright.expect(page.locator("#eventTimeline")).to_contain_text("网站已恢复")
    page.reload()
    playwright.expect(page.locator("#alertList")).to_contain_text("已知晓 · 仍异常")
    playwright.expect(page.locator('#eventsTabCount')).to_have_text('4')
    choose(page, "incidentStatus", "unacknowledged")
    assert page.locator("#alertList .incident-row").count() == 4
    page.locator("#acknowledgeAllBtn").click()
    playwright.expect(page.locator("#acknowledgeAllBtn")).to_be_disabled()
    playwright.expect(page.locator("#alertList")).to_contain_text("没有符合筛选")
    playwright.expect(page.locator("#alertBadge")).to_have_text("5 项异常")
    playwright.expect(page.locator('#eventsTabCount')).to_be_hidden()
    assert control["posts"][-1] == {"action": "acknowledge_all"}
    page.locator("#resetIncidentFilters").click()
    assert page.locator("#alertList .incident-row").count() == 5
    assert page.locator("#eventTimeline .incident-row").count() == 2
    playwright.expect(page.locator("#eventTimeline")).not_to_contain_text("已知晓：")
    choose(page, "historyStatus", "recovery")
    playwright.expect(page.locator("#eventTimeline")).to_contain_text("持续 2 分钟")
    choose(page, "historyStatus", "disappeared")
    playwright.expect(page.locator("#eventTimeline")).to_contain_text("配置容器消失")
    page.reload()
    playwright.expect(page.locator('#alertBadge')).to_have_text('5 项异常')
    playwright.expect(page.locator('#eventsTabCount')).to_be_hidden()


def test_filters_expansion_and_polling_stay_stable_on_recovery(incidents):
    page, state, control = incidents
    choose(page, "incidentSource", "docker")
    choose(page, "incidentSeverity", "critical")
    assert page.locator("#alertList .incident-row").count() == 2
    page.locator("#alertList summary").first.click()
    choose(page, "historySource", "website")
    page.clock.install()
    page.clock.run_for(16000)
    playwright.expect(page.locator("#alertList details").first).to_have_attribute("open", "")
    assert page.locator("#incidentSource").input_value() == "docker"
    assert page.locator("#incidentSeverity").input_value() == "critical"
    assert page.locator("#historySource").input_value() == "website"
    control["snapshot"]["alerts"] = [item for item in control["snapshot"]["alerts"] if item["source"] != "docker"]
    refresh(page)
    playwright.expect(page.locator("#alertBadge")).to_have_text("2 项异常")
    playwright.expect(page.locator("#alertList")).to_contain_text("没有符合筛选")
    assert page.locator("#incidentSource").input_value() == "docker"
    assert len([call for call in state["calls"] if call[0] == "/status"]) == 1
    page.locator("#requestsViewTab").click()
    gets = control["gets"]
    page.clock.run_for(31000)
    assert control["gets"] == gets


@pytest.mark.parametrize("width", [390, 1440])
def test_evidence_links_collapsed_probes_and_layout(incidents, width, tmp_path):
    page, state, _ = incidents
    page.set_viewport_size({"width": width, "height": 960})
    assert not page.locator("#monitorWebsiteSection").evaluate("node => node.open")
    assert not page.locator(".monitor-settings").evaluate("node => node.open")
    api = page.locator("#alertList .incident-row").filter(has_text="api 内存溢出退出")
    api.locator("summary").click()
    playwright.expect(api).to_contain_text("137")
    playwright.expect(api).to_contain_text("内存溢出")
    playwright.expect(api).not_to_contain_text("oom_killed")
    api.locator("[data-incident-log]").click()
    playwright.expect(page.locator("#containerLogsModal")).to_be_visible()
    assert any(call[0] == "/docker/logs" for call in state["calls"])
    page.locator("#closeContainerLogsBtn").click()
    db = page.locator("#alertList .incident-row").filter(has_text="db 健康检查失败")
    db.locator("summary").click()
    playwright.expect(db).to_contain_text("<script>bad()</script>")
    playwright.expect(db).to_contain_text("健康检查退出码")
    playwright.expect(db).to_contain_text("2026-10-11T12:00:00Z")
    assert page.locator("#alertList script").count() == 0
    page.locator("#monitorWebsiteSection > summary").click()
    page.locator("#monitorUrl").fill("http://127.0.0.1:8080/health")
    page.locator("#monitorSave").click()
    playwright.expect(page.locator("#monitorTargets")).to_contain_text("http://127.0.0.1:8080/health")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / f"incidents-{width}.png"), full_page=True)
    cert = page.locator("#alertList .incident-row").filter(has_text="shop 证书剩余 6 天")
    cert.locator("summary").click()
    cert.locator("[data-incident-certificate]").click()
    playwright.expect(page.locator("#certificatesView")).to_have_class("dashboard-view active")
    page.locator("#certificatesView .discovery-secondary > summary").filter(has_text="线上持续监测").click()
    page.locator("#monitorCertificateAdd").click()
    playwright.expect(page.locator("#monitorUrl")).to_be_visible()
    playwright.expect(page.locator("#monitorUrl")).to_be_focused()


def test_unknown_stale_and_failed_mutation_never_imply_recovery(incidents):
    page, _, control = incidents
    control["snapshot"]["coverage"]["docker"] = {"status": "healthy", "checked_at": time.time() - 180}
    refresh(page)
    playwright.expect(page.locator("#incidentCoverage")).to_contain_text("容器 · 采样未知")
    assert page.locator("#incidentCoverage .available").count() == 1
    control["snapshot"]["coverage"] = {"server": {"status": "unknown", "detail": "等待采样"},
        "docker": {"status": "not_installed"}}
    control["snapshot"]["alerts"][0]["stale"] = True
    refresh(page)
    playwright.expect(page.locator("#incidentCoverage")).to_contain_text("采样未知")
    playwright.expect(page.locator("#incidentCoverage")).to_contain_text("未安装")
    choose(page, "incidentStatus", "stale")
    assert page.locator("#alertList .incident-row").count() == 1
    playwright.expect(page.locator("#alertBadge")).to_have_text("5 项异常")
    control["fail_post"] = True
    page.locator("[data-incident-ack]").click()
    playwright.expect(page.locator("#incidentFeedback")).to_contain_text("异常已变化")
    playwright.expect(page.locator("[data-incident-ack]")).to_be_enabled()
    playwright.expect(page.locator("#alertList")).to_contain_text("待处理")
    control["snapshot"]["alerts"] = []
    refresh(page)
    playwright.expect(page.locator("#alertBadge")).to_have_text("暂无当前异常")
    playwright.expect(page.locator("#alertList")).to_contain_text("尚不能确认已恢复")
    control["fail_get"] = True
    refresh(page)
    playwright.expect(page.locator("#incidentFeedback")).to_contain_text("正在重试")
    playwright.expect(page.locator("#alertList")).to_contain_text("监测暂不可用")


def test_missing_incident_id_disables_ack_and_non_incidents_are_hidden(incidents):
    page, state, control = incidents
    control["snapshot"]["alerts"][0].pop("incident_id")
    control["snapshot"]["events"].append({"id": "operation", "kind": "monitoring_save", "source": "monitoring",
        "title": "保存监测地址", "detail": "Local probe", "at_ts": int(time.time())})
    control["snapshot"]["events"].append({"id": "lifecycle", "kind": "lifecycle", "source": "docker_events",
        "title": "容器 api 已启动", "level": "info", "metadata": {"container": "api", "action": "start"}, "at_ts": int(time.time())})
    control["snapshot"]["events"].append({"id": "audit", "kind": "audit", "source": "docker",
        "title": "操作失败审计", "level": "critical", "at_ts": int(time.time())})
    control["snapshot"]["events"].append({"id": "api-failure", "kind": "alert", "source": "website",
        "title": "接口 /health 无法访问", "detail": "HTTP 503", "level": "critical", "at_ts": int(time.time())})
    refresh(page)
    playwright.expect(page.locator('[data-incident-ack="docker:api:oom"]')).to_be_disabled()
    playwright.expect(page.locator("#eventTimeline")).not_to_contain_text("保存监测地址")
    playwright.expect(page.locator("#eventTimeline")).not_to_contain_text("容器 api 已启动")
    playwright.expect(page.locator("#eventTimeline")).not_to_contain_text("操作失败审计")
    playwright.expect(page.locator("#eventTimeline")).to_contain_text("接口 /health 无法访问")
    playwright.expect(page.locator("#incidentHistoryHint")).to_contain_text("3 条异常与恢复记录")
    playwright.expect(page.locator("#alertBadge")).to_have_text("5 项异常")
    assert page.locator("#incidentOperations, #prometheusPanel, script[src*='prometheus.js']").count() == 0
    assert not any(call[0] == '/metrics/access' for call in state['calls'])
    assert len(control["snapshot"]["events"]) == 6


def test_history_pagination_filter_reset_excludes_operations(incidents):
    page, _, control = incidents
    now = int(time.time())
    control["snapshot"]["events"] = [
        {"id": f"history-{i}", "kind": "recovery", "source": "website" if i < 5 else "docker",
         "title": f"恢复记录 {i}", "level": "success", "at_ts": now - i, "duration_seconds": 120}
        for i in range(65)] + [
        {"id": f"operation-{i}", "kind": "operation", "source": "docker",
         "title": f"面板操作 {i}", "at_ts": now - i} for i in range(65)]
    control["snapshot"]["coverage"]["docker_events"] = {"status": "unknown", "detail": "等待首次事件", "checked_at": now - 5}
    refresh(page)
    assert page.locator("#eventTimeline .incident-row").count() == 5
    assert page.locator("#incidentOperations").count() == 0
    playwright.expect(page.locator("#historyPageInfo")).to_have_text("筛选 65 / 共 65 条 · 第 1 / 13 页")
    assert page.locator("#incidentCoverage small").count() == 1
    assert page.locator("#incidentCoverage .incident-coverage-item").count() == 2
    page.get_by_role("button", name="下一页历史", exact=True).click()
    assert page.locator("#eventTimeline .incident-row").count() == 5
    playwright.expect(page.locator("#historyNext")).to_be_enabled()
    refresh(page)
    playwright.expect(page.locator("#historyPageInfo")).to_contain_text("第 2 / 13 页")
    choose(page, "historySource", "website")
    assert page.locator("#eventTimeline .incident-row").count() == 5
    playwright.expect(page.locator("#historyPageInfo")).to_have_text("筛选 5 / 共 65 条 · 第 1 / 1 页")
    playwright.expect(page.locator("#historyPrev")).to_be_disabled()
    playwright.expect(page.locator("#historyNext")).to_be_disabled()
