"""Actual local HTTP -> SSH -> xterm workflow and browser security gates."""
import base64
import io
import time

import pytest

import agent
from test_ssh_terminal import http_server as http_server, manager as manager, peer as peer, runtime as runtime

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def page(http_server):
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.add_cookies([{"name": agent.COOKIE_NAME, "value": http_server.cookie, "url": http_server.origin}])
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        # Avoid invoking unrelated discovery/network workers during this workflow.
        page.route("**/monitoring", lambda route: route.fulfill(json={"targets": [], "alerts": [], "events": []}))
        page.route("**/status", lambda route: route.fulfill(json={"csrf_token": agent._csrf_token(http_server.cookie), "system": {}}))
        yield page
        assert not errors
        browser.close()


def fill(page, peer):
    page.locator("#sshHost").fill(peer["spec"]["host"])
    page.locator("#sshPort").fill(str(peer["spec"]["port"]))
    page.locator("#sshUsername").fill("demo")
    page.locator("#sshPassword").fill("safe-password")


def test_real_connection_fingerprint_cancel_then_input_resize_and_disconnect(page, http_server, manager, peer):
    page.goto(http_server.origin + "/ui?view=terminal")
    playwright.expect(page.locator("#sshHost")).to_be_enabled()
    fill(page, peer)
    page.locator("#sshConnect").click()
    playwright.expect(page.locator("#sshFingerprintDialog")).to_be_visible()
    assert peer["auth"] == []
    playwright.expect(page.locator("#sshTrustConfirm")).to_be_disabled()
    page.keyboard.press("Escape")
    playwright.expect(page.locator("#sshFeedback")).to_contain_text("未发送")
    assert not peer["auth"]
    page.locator("#sshPassword").fill("safe-password")
    page.locator("#sshConnect").click()
    playwright.expect(page.locator("#sshFingerprintDialog")).to_be_visible()
    page.locator("#sshTrustCheck").check()
    page.locator("#sshTrustConfirm").click()
    playwright.expect(page.locator("#sshState")).to_have_text("已连接")
    playwright.expect(page.locator("#sshPassword")).to_have_value("")
    page.locator(".xterm-helper-textarea").focus()
    page.keyboard.type("hello")
    page.keyboard.press("Enter")
    page.keyboard.press("Control+c")
    deadline = time.monotonic() + 3
    while b"\x03" not in b"".join(peer["input"]) and time.monotonic() < deadline:
        page.wait_for_timeout(30)
    assert b"hello\r" in b"".join(peer["input"])
    assert b"\x03" in b"".join(peer["input"])
    page.set_viewport_size({"width": 390, "height": 844})
    playwright.expect(page.locator("#sshMobileKeys")).to_be_visible()
    page.wait_for_timeout(300)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert len(peer["sizes"]) >= 2
    page.locator("#sshDisconnect").click()
    playwright.expect(page.locator("#sshState")).to_have_text("已断开")
    assert all(session.closed for session in manager.sessions.values())


def test_private_key_upload_and_untrusted_http_disable_credentials(page, http_server, manager, peer):
    page.goto(http_server.origin + "/ui?view=terminal")
    playwright.expect(page.locator("#sshHost")).to_be_enabled()
    fill(page, peer)
    page.select_option("#sshAuth", "key", force=True)
    private = io.StringIO()
    peer["user_key"].write_private_key(private, password="file-key-secret")
    page.locator("#sshKeyFile").set_input_files({"name": "id_rsa", "mimeType": "text/plain", "buffer": private.getvalue().encode()})
    page.locator("#sshPassphrase").fill("file-key-secret")
    page.locator("#sshConnect").click()
    page.locator("#sshTrustCheck").check()
    page.locator("#sshTrustConfirm").click()
    playwright.expect(page.locator("#sshState")).to_have_text("已连接")
    playwright.expect(page.locator("#sshPrivateKey")).to_have_value("")
    playwright.expect(page.locator("#sshPassphrase")).to_have_value("")
    assert "PRIVATE KEY" not in (manager.directory / "connections.json").read_text()
    page.locator("#sshDisconnect").click()
    page.route("**/ssh", lambda route: route.fulfill(json={"available": True, "secure": False,
        "profiles": [], "sessions": [], "csrf_token": "token"}))
    page.goto(http_server.origin + "/ui?view=terminal")
    playwright.expect(page.locator("#sshSecurityNotice")).to_be_visible()
    playwright.expect(page.locator("#sshHost")).to_be_disabled()


def test_new_connection_closes_current_session_and_allows_reconnect(page, http_server, manager, peer):
    page.goto(http_server.origin + "/ui?view=terminal")
    playwright.expect(page.locator("#sshHost")).to_be_enabled()
    fill(page, peer)
    page.locator("#sshConnect").click()
    page.locator("#sshTrustCheck").check()
    page.locator("#sshTrustConfirm").click()
    playwright.expect(page.locator("#sshState")).to_have_text("已连接")
    old_sessions = list(manager.sessions.values())
    page.locator("#sshNew").click()
    playwright.expect(page.locator("#sshState")).to_have_text("未连接")
    playwright.expect(page.locator("#sshConnect")).to_be_enabled()
    playwright.expect(page.locator("#sshHost")).to_have_value("")
    playwright.expect(page.locator("#sshPort")).to_have_value("22")
    playwright.expect(page.locator("#sshUsername")).to_have_value("root")
    playwright.expect(page.locator("#sshTarget")).to_be_empty()
    playwright.expect(page.locator("#sshEmpty")).to_be_visible()
    assert old_sessions and all(session.closed for session in old_sessions)
    fill(page, peer)
    page.locator("#sshConnect").click()
    playwright.expect(page.locator("#sshState")).to_have_text("已连接")
    playwright.expect(page.locator("#sshTerminal")).to_be_visible()
    page.locator("#sshDisconnect").click()


def test_stream_reconnect_retains_output_and_ansi_palette_across_themes(page, http_server, manager, peer, monkeypatch):
    import ssh_terminal
    original = ssh_terminal.Session.stream
    monkeypatch.setattr(ssh_terminal.Session, "stream", lambda self, cursor: original(self, cursor, duration=.2))
    calls = []
    page.on("request", lambda request: calls.append(request.url))
    page.goto(http_server.origin + "/ui?view=terminal")
    fill(page, peer)
    page.locator("#sshConnect").click()
    page.locator("#sshTrustCheck").check()
    page.locator("#sshTrustConfirm").click()
    playwright.expect(page.locator("#sshState")).to_have_text("已连接")
    playwright.expect(page.locator(".ssh-target-user")).to_have_text("demo")
    for label, code in (("folder", 34), ("success", 32), ("warning", 33), ("error", 31)):
        peer["channels"][-1].sendall(f"\r\n\x1b[{code}m{label}\x1b[0m".encode())
    for label in ("folder", "success", "warning", "error"):
        playwright.expect(page.locator(".xterm-rows")).to_contain_text(label)
    for theme in ("light", "dark"):
        page.evaluate("theme => { document.documentElement.dataset.theme = theme; }", theme)
        page.wait_for_timeout(100)
        colors = page.locator(".xterm-rows span").evaluate_all("""nodes => Object.fromEntries(
            nodes.filter(node => ['folder', 'success', 'warning', 'error'].includes(node.textContent))
                .map(node => [node.textContent, getComputedStyle(node).color]))""")
        assert len(colors) == 4 and len(set(colors.values())) == 4
    page.wait_for_timeout(500)
    streams = [url for url in calls if "/ssh/stream?" in url]
    assert len(streams) >= 2 and not any("/ssh/output?" in url for url in calls)
    page.locator(".xterm-helper-textarea").focus()
    page.keyboard.type("still-connected")
    playwright.expect(page.locator(".xterm-rows")).to_contain_text("still-connected")
    page.locator("#sshDisconnect").click()


def test_saved_profile_escapes_markup_and_never_keeps_secret(page, http_server, manager, peer):
    page.goto(http_server.origin + "/ui?view=terminal")
    playwright.expect(page.locator("#sshHost")).to_be_enabled()
    fill(page, peer)
    page.locator(".ssh-save summary").last.click()
    page.locator("#sshName").fill('<img src=x onerror="alert(1)">')
    page.locator("#sshSave").click()
    playwright.expect(page.locator(".ssh-profile-select strong")).to_have_text('<img src=x onerror="alert(1)">')
    assert page.locator("#sshProfiles img").count() == 0
    assert "safe-password" not in (manager.directory / "connections.json").read_text()
    page.locator(".ssh-profile-select").click()
    playwright.expect(page.locator("#sshPassword")).to_have_value("")
    page.get_by_role("button", name='移除 <img src=x onerror="alert(1)"> 的地址').click()
    playwright.expect(page.locator("#sshProfiles")).to_contain_text("暂无保存")


def test_legacy_login_requires_reauthentication_for_ssh(page, http_server, manager):
    expires = int(time.time()) + 300
    legacy = base64.urlsafe_b64encode(f"{expires}:{agent._session_signature(expires)}".encode()).decode()
    page.context.add_cookies([{"name": agent.COOKIE_NAME, "value": legacy, "url": http_server.origin}])
    page.goto(http_server.origin + "/ui?view=terminal")
    playwright.expect(page.locator("#sshFeedback")).to_contain_text("重新登录")
    playwright.expect(page.locator("#sshHost")).to_be_disabled()


def test_mobile_connect_collapses_form_and_escape_cannot_reuse_previous_trust(page, http_server, manager, peer):
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(http_server.origin + "/ui?view=terminal")
    playwright.expect(page.locator("#sshHost")).to_be_enabled()
    fill(page, peer)
    page.locator("#sshConnect").click()
    page.locator("#sshTrustCheck").check()
    page.locator("#sshTrustConfirm").click()
    playwright.expect(page.locator("#sshState")).to_have_text("已连接")
    playwright.expect(page.locator("#sshForm")).to_be_hidden()
    assert page.evaluate("document.querySelector('#terminalViewTab').getBoundingClientRect().right <= innerWidth")
    page.locator("#sshDisconnect").click()
    page.locator("#sshConnectionToggle").click()
    playwright.expect(page.locator("#sshForm")).to_be_visible()
    peer["key"] = __import__("paramiko").RSAKey.generate(2048)
    page.locator("#sshPassword").fill("safe-password")
    auth_count = len(peer["auth"])
    page.locator("#sshConnect").click()
    playwright.expect(page.locator("#sshFingerprintTitle")).to_have_text("服务器指纹发生变化")
    page.locator("#sshTrustCheck").check()
    page.keyboard.press("Escape")
    playwright.expect(page.locator("#sshFeedback")).to_contain_text("未发送")
    assert len(peer["auth"]) == auth_count
    assert all(session.closed for session in manager.sessions.values())
