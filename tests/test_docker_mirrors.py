from __future__ import annotations

import hashlib
import http.client
import json
import threading

import pytest

import agent
import docker_mirrors as mirrors
from certificates import CertificateError
from test_deployment_removal import http_server as http_server, runtime as runtime


@pytest.fixture
def engine(tmp_path, monkeypatch):
    path = tmp_path / "docker" / "daemon.json"
    path.parent.mkdir()
    original = '{\n  "log-driver": "json-file", "log-opts": {"max-size": "10m"},\n  "registry-mirrors": ["https://old.example"]\n}\n'
    path.write_bytes(original.encode())
    state = {"effective": ["https://old.example"], "commands": [], "reload": True,
             "restart": True, "validation": True, "live_restore": False, "running": "", "external_edit": False}
    monkeypatch.setattr(mirrors.time, "sleep", lambda _: None)

    def inspect():
        raw, config, mode = mirrors.read_config(path)
        return {"editable": True, "path": str(path), "mirrors": mirrors.normalize(config.get("registry-mirrors", [])),
                "effective_mirrors": list(state["effective"]), "revision": hashlib.sha256(raw.encode()).hexdigest(),
                "endpoint": "unix:///var/run/docker.sock", "executable": "/usr/bin/dockerd", "daemon_id": "local-id",
                "raw": raw, "config": config, "mode": mode, "live_restore": state["live_restore"]}

    def run(command, *, timeout):
        state["commands"].append(command)
        if "--validate" in command:
            if not state["validation"]:
                raise CertificateError("validation failed")
            return "configuration OK"
        if "kill" in command or "restart" in command:
            candidate = json.loads(path.read_text())
            desired = candidate.get("registry-mirrors", [])
            rollback = desired == ["https://old.example"]
            if state["external_edit"] and not rollback:
                path.write_text('{"log-driver": "external-edit"}')
                raise CertificateError("reload failed")
            if ("kill" in command and state["reload"]) or ("restart" in command and state["restart"]) or rollback:
                state["effective"] = desired
            elif "restart" in command:
                raise CertificateError("restart failed")
            return ""
        if "info" in command:
            return json.dumps({"ID": "local-id", "RegistryConfig": {"Mirrors": state["effective"]}})
        if "ps" in command:
            return state["running"]
        pytest.fail(f"Unexpected command {command}")

    manager = mirrors.Manager(tmp_path / "data", inspect=inspect, runner=run)
    return manager, path, state, original


@pytest.mark.parametrize("bad", [None, {}, [""], ["example.com"], ["ftp://example.com"], ["https://u:p@host"],
                                     ["https://host/path"], ["https://host?key=secret"], ["http://host:0"],
                                     ["https://host:65536"], ["https://host\nmore"], ["https://host\\other"]])
def test_address_validation(bad):
    with pytest.raises(ValueError):
        mirrors.normalize(bad)


def test_normalization_handles_ports_ipv6_and_deduplication():
    assert mirrors.normalize([" https://Example.COM/ ", "https://example.com:443", "http://[::1]:5000/"]) == [
        "https://example.com", "http://[::1]:5000"]
    assert mirrors.normalize([]) == []


def test_config_read_rejects_duplicate_or_invalid_json(tmp_path):
    path = tmp_path / "daemon.json"
    for raw in ('{"registry-mirrors": [], "registry-mirrors": []}', "[]", "", '{"registry-mirrors": "bad"}'):
        path.write_text(raw)
        with pytest.raises(ValueError):
            mirrors.read_config(path)
        assert path.read_text() == raw


def test_read_only_status_exposes_only_mirror_fields(engine):
    manager, path, state, original = engine
    public = manager.status()
    assert public["mirrors"] == ["https://old.example"]
    assert not {"raw", "config", "executable", "endpoint"} & public.keys()
    assert path.read_text() == original and not state["commands"]
    assert not manager.home.exists()


def test_apply_preserves_other_config_and_backup_then_hot_reloads(engine):
    manager, path, state, original = engine
    mode = path.stat().st_mode & 0o777
    manager.apply(["https://new.example"], manager.status()["revision"])
    updated = json.loads(path.read_text())
    assert updated["log-driver"] == "json-file" and updated["log-opts"] == {"max-size": "10m"}
    assert updated["registry-mirrors"] == ["https://new.example"]
    assert path.stat().st_mode & 0o777 == mode
    assert next(manager.home.glob("backups/docker-mirrors/*/before.json")).read_text() == original
    assert manager.job["state"] == "succeeded" and manager.job["activation"] == "热加载"
    assert not any("restart" in command for command in state["commands"])


def test_remove_all_only_removes_mirror_key(engine):
    manager, path, state, _ = engine
    manager.apply([], manager.status()["revision"])
    assert "registry-mirrors" not in json.loads(path.read_text())
    assert json.loads(path.read_text())["log-driver"] == "json-file"
    assert state["effective"] == []


def test_no_change_never_restarts(engine):
    manager, _, state, _ = engine
    manager.apply(["https://old.example"], manager.status()["revision"])
    assert state["commands"] == []
    assert manager.job["state"] == "succeeded"


def test_stale_revision_does_not_overwrite_external_edit(engine):
    manager, path, state, _ = engine
    revision = manager.status()["revision"]
    path.write_text('{"debug": true}')
    with pytest.raises(ValueError, match="变化"):
        manager.apply([], revision)
    assert path.read_text() == '{"debug": true}' and state["commands"] == []


def test_failed_validation_does_not_write_or_restart(engine):
    manager, path, state, original = engine
    state["validation"] = False
    with pytest.raises(CertificateError):
        manager.apply([], manager.status()["revision"])
    assert path.read_text() == original
    assert not any("systemctl" in command for command in state["commands"])


def test_reload_fallback_restarts_when_no_containers(engine):
    manager, _, state, _ = engine
    state["reload"] = False
    manager.apply(["https://new.example"], manager.status()["revision"])
    assert manager.job["activation"] == "重启 Docker"


def test_running_containers_without_live_restore_prevent_restart(engine):
    manager, path, state, original = engine
    state.update(reload=False, running="a" * 64)
    with pytest.raises(ValueError, match="未自动重启"):
        manager.apply(["https://new.example"], manager.status()["revision"])
    assert path.read_text() == original
    assert not any("restart" in command for command in state["commands"])
    assert manager.job["rollback"] == "succeeded"


def test_restart_failure_rolls_back_and_reports_failure(engine):
    manager, path, state, original = engine
    state.update(reload=False, restart=False)
    with pytest.raises(ValueError, match="已恢复"):
        manager.apply(["https://new.example"], manager.status()["revision"])
    assert path.read_text() == original
    assert manager.job["state"] == "failed"
    assert state["effective"] == ["https://old.example"]


def test_external_edit_during_apply_is_not_overwritten_on_rollback(engine):
    manager, path, state, _ = engine
    state.update(external_edit=True, reload=False, restart=False)
    with pytest.raises(ValueError, match="未覆盖"):
        manager.apply(["https://new.example"], manager.status()["revision"])
    assert json.loads(path.read_text())["log-driver"] == "external-edit"


def test_busy_job_cannot_be_submitted_twice(engine):
    manager, _, _, _ = engine
    gate = threading.Event()
    audit = []

    def operation(callback):
        assert gate.wait(5)
        callback()

    revision = manager.status()["revision"]
    manager.start([], revision, operation=operation, audit=audit.append)
    try:
        assert manager.status(job_only=True)["job"]["state"] == "running"
        with pytest.raises(ValueError, match="等待"):
            manager.start([], revision, operation=operation, audit=audit.append)
    finally:
        gate.set()
        manager.worker.join(5)
    assert not manager.worker.is_alive()
    assert audit == [True] and manager.job["state"] == "succeeded"


def test_interrupted_operation_is_visible_after_restart(engine):
    manager, _, _, _ = engine
    manager._progress("running", "applying", backup="test-backup")
    restored = mirrors.Manager(manager.home, inspect=manager.inspect, runner=manager.run)
    assert restored.status()["job"]["state"] == "interrupted"
    assert restored.status()["job"]["backup"] == "test-backup"


def test_remote_context_is_rejected(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote.example:2375")
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    with pytest.raises(ValueError, match="不是主机"):
        mirrors.local_endpoint()


def test_daemon_arguments_detect_custom_path_and_conflicts():
    assert str(mirrors.daemon_arguments(["dockerd", "--config-file=/etc/docker/custom.json"])).replace("\\", "/").endswith("/etc/docker/custom.json")
    for args in (["--registry-mirror=https://example.com"], ["--rootless"], ["--config-file=relative.json"], ["-H", "tcp://0.0.0.0:2375"], ["-Hunix:///custom.sock"]):
        with pytest.raises(ValueError):
            mirrors.daemon_arguments(["dockerd", *args])


def test_validation_uses_existing_daemon_flags_without_old_config():
    command = mirrors.validation_command({"executable": "/usr/bin/dockerd", "arguments": [
        "-H", "fd://", "--containerd=/run/containerd/containerd.sock", "--config-file", "/etc/docker/custom.json",
    ]}, "/backup/proposed.json")
    assert command == ["/usr/bin/dockerd", "-H", "fd://", "--containerd=/run/containerd/containerd.sock",
                       "--config-file", "/backup/proposed.json", "--validate"]


def test_failed_progress_write_allows_retry(engine, monkeypatch):
    manager, _, _, _ = engine
    original = mirrors.atomic_write

    def fail_record(path, content):
        if path == manager.record:
            raise OSError("disk full")
        return original(path, content)

    monkeypatch.setattr(mirrors, "atomic_write", fail_record)
    with pytest.raises(OSError):
        manager.start([], manager.status()["revision"], operation=lambda callback: callback(), audit=lambda _: None)
    assert manager.job["state"] == "idle"
    assert manager.worker is None


def test_api_auth_csrf_async_and_real_manager(http_server, runtime, engine, monkeypatch):
    manager, path, _, _ = engine
    monkeypatch.setattr(agent, "_docker_mirrors_manager", lambda: manager)
    assert http_server("GET", "/docker/mirrors", authenticated=False)[0] == 401
    assert http_server("POST", "/docker/mirrors", {}, authenticated=False)[0] == 401
    connection = http.client.HTTPConnection(http_server.origin.removeprefix("http://"))
    connection.request("POST", "/docker/mirrors", '{}', {"Cookie": f"{agent.COOKIE_NAME}={http_server.cookie}"})
    response = connection.getresponse()
    assert response.status == 403
    response.read()
    connection.close()
    code, result = http_server("GET", "/docker/mirrors")
    assert code == 200 and result["editable"]
    code, result = http_server("POST", "/docker/mirrors", {"mirrors": [], "revision": result["revision"]})
    assert code == 202
    manager.worker.join(5)
    assert http_server("GET", "/docker/mirrors?job=1")[1]["job"]["state"] == "succeeded"
    assert "registry-mirrors" not in json.loads(path.read_text())


@pytest.mark.parametrize("width", [390, 1440])
def test_browser_edits_existing_mirrors_and_applies_without_terminal(http_server, runtime, engine, monkeypatch, width):
    playwright = pytest.importorskip("playwright.sync_api")
    manager, path, _, _ = engine
    monkeypatch.setattr(agent, "_docker_mirrors_manager", lambda: manager)
    monkeypatch.setattr(agent, "_docker_images", lambda: [])
    with playwright.sync_playwright() as browser_runtime:
        browser = browser_runtime.chromium.launch()
        try:
            context = browser.new_context(viewport={"width": width, "height": 960})
            context.add_cookies([{"name": agent.COOKIE_NAME, "value": http_server.cookie, "url": http_server.origin}])
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(http_server.origin + "/ui")
            page.locator("#dockerMirrorsOpen").click()
            inputs = page.locator("#dockerMirrorsAddresses input")
            playwright.expect(inputs).to_have_value("https://old.example")
            inputs.fill("https://updated.example")
            page.locator("#dockerMirrorsAdd").click()
            inputs.nth(1).fill("https://second.example")
            page.locator("#dockerMirrorsSave").click()
            playwright.expect(page.locator("#dockerMirrorsFeedback")).to_contain_text("通过热加载生效")
            playwright.expect(page.locator("#dockerMirrorsEffective")).to_contain_text("https://second.example")
            assert json.loads(path.read_text())["registry-mirrors"] == ["https://updated.example", "https://second.example"]
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.locator("#dockerMirrorsPanel").scroll_into_view_if_needed()
            page.screenshot(path=str(runtime / f"docker-mirrors-{width}.png"))
            while page.locator("[data-mirror-remove]").count():
                page.locator("[data-mirror-remove]").first.click()
            page.locator("#dockerMirrorsSave").click()
            playwright.expect(page.locator("#dockerMirrorsEffective")).to_contain_text("Docker 默认拉取方式")
            assert "registry-mirrors" not in json.loads(path.read_text())
            assert not errors
        finally:
            if manager.worker:
                manager.worker.join(5)
            browser.close()
