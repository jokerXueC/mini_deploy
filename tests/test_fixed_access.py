from __future__ import annotations

import http.client
import os
import shutil
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

import pytest

import agent


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = (ROOT / "install.sh").read_text(encoding="utf-8")


@pytest.mark.parametrize("host,port", [("127.0.0.1", "9010"), ("127.0.0.1", "12345"), ("", "invalid")])
def test_old_environment_cannot_override_fixed_listener(host, port):
    env = {**os.environ, "DEPLOY_AGENT_HOST": host, "DEPLOY_AGENT_PORT": port}
    result = subprocess.run([sys.executable, "-c", "import agent; print(agent.HOST, agent.PORT)"],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=10, check=True)
    assert result.stdout.strip() == "0.0.0.0 6868"


def test_root_http_login_and_assets_without_nginx(monkeypatch):
    monkeypatch.setattr(agent, "UI_PASSWORD_HASH", agent._hash_password("Test-only-password-6868"))
    monkeypatch.setattr(agent, "UI_SESSION_SECRET", "test-session-secret-0123456789abcdef")
    monkeypatch.setattr(agent, "COOKIE_SECURE_MODE", "auto")
    monkeypatch.setattr(agent, "_audit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent, "_log", lambda *args: None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), agent.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        assert response.status == 200
        assert b'password' in response.read()
        connection.request("GET", "/system-metrics")
        response = connection.getresponse()
        assert response.status == 401
        response.read()
        connection.request("POST", "/login", body=urlencode({"password": "Test-only-password-6868"}),
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
        response = connection.getresponse()
        assert response.status in {302, 303}
        cookie = response.getheader("Set-Cookie")
        assert "Secure" not in cookie
        assert "HttpOnly" in cookie
        response.read()
        connection.request("GET", "/", headers={"Cookie": cookie.split(";", 1)[0]})
        response = connection.getresponse()
        assert response.status == 200
        assert b'certificatesViewTab' in response.read()
        connection.request("GET", "/system-metrics", headers={"Cookie": cookie.split(";", 1)[0]})
        response = connection.getresponse()
        assert response.status == 200
        assert b'realtime_history' in response.read()
        for asset in ("app.js", "certificates.js", "selects.js", "style.css"):
            connection.request("GET", f"/ui/{asset}")
            response = connection.getresponse()
            assert response.status == 200
            assert response.read()
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def shell_function(name):
    start = INSTALLER.index(name + "() {\n")
    end = INSTALLER.index("\n}\n", start) + 3
    return INSTALLER[start:end]


def run_helper(name, body, **env):
    bash = shutil.which("bash")
    if os.name == "nt":
        bash = "C:/Program Files/Git/bin/bash.exe"
    if not bash or not Path(bash).is_file():
        pytest.skip("Bash required for installer helper tests")
    preamble = 'set -euo pipefail\npython3() { "$TEST_PYTHON" "$@"; }\nis_en() { return 0; }\n'
    return subprocess.run([bash, "-c", preamble + shell_function(name) + "\n" + body], cwd=ROOT,
                          env={**os.environ, "TEST_PYTHON": sys.executable, "PYTHONIOENCODING": "utf-8", **env},
                          capture_output=True, text=True, encoding="utf-8", timeout=10)


@pytest.mark.parametrize("address,expected", [("8.8.8.8", "8.8.8.8"), ("192.168.1.2", "<server-public-ip>"),
                                              ("invalid", "<server-public-ip>")])
def test_public_address_validates_display_override(address, expected):
    result = run_helper("dashboard_public_address", "dashboard_public_address", DEPLOY_PUBLIC_IP=address)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_public_ip_detection_failure_is_not_an_install_failure():
    result = run_helper("dashboard_public_address", "curl() { return 7; }; dashboard_public_address", DEPLOY_PUBLIC_IP="")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "<server-public-ip>"


@pytest.mark.parametrize("fail_reload", [False, True])
def test_existing_nginx_port_migration_preserves_tls(tmp_path, fail_reload):
    config = tmp_path / "mini-deploy.conf"
    original = 'server {\nlisten 443 ssl;\nssl_certificate /etc/letsencrypt/live/example/fullchain.pem;\nproxy_pass http://127.0.0.1:9010/;\n}\n'
    config.write_text(original, encoding="utf-8")
    body = 'nginx() { return 0; }; systemctl() { return 0; }; migrate_nginx_dashboard_port'
    if fail_reload:
        body = 'nginx() { return 0; }; systemctl() { [[ "$1" != reload ]]; }; migrate_nginx_dashboard_port'
    result = run_helper("migrate_nginx_dashboard_port", body, NGINX_CONF_FILE=str(config))
    assert result.returncode == (1 if fail_reload else 0), result.stderr
    expected = original if fail_reload else original.replace(":9010/", ":6868/")
    assert config.read_text(encoding="utf-8") == expected
    assert list(tmp_path.glob("mini-deploy.conf*")) == [config]


def test_firewall_changes_only_add_fixed_tcp_port(tmp_path):
    log = tmp_path / "firewall.log"
    body = '''
ufw() { if [[ "$1" == status ]]; then echo 'Status: active'; else echo "ufw $*" >> "$TEST_LOG"; fi; }
firewall-cmd() { if [[ "$1" == --state ]]; then return 0; else echo "firewalld $*" >> "$TEST_LOG"; fi; }
open_dashboard_firewall
'''
    result = run_helper("open_dashboard_firewall", body, TEST_LOG=str(log))
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == ["ufw allow 6868/tcp", "firewalld --permanent --add-port=6868/tcp",
                                          "firewalld --add-port=6868/tcp"]


def test_installer_normalizes_port_before_start_and_opens_firewall_after_auth():
    assert 'if [[ -z "$DEPLOY_DOMAIN" ]] && nginx_install_is_preapproved;' in INSTALLER
    assert INSTALLER.index('upsert_env_assignment "DEPLOY_AGENT_PORT" "6868"') < INSTALLER.index('\ninitialize_admin_credentials\n')
    assert INSTALLER.index('\ninitialize_admin_credentials\n') < INSTALLER.index('\nopen_dashboard_firewall\n')
    assert 'DASHBOARD_URL="http://$PUBLIC_ADDRESS:6868"' in INSTALLER
    assert 'upsert_env_assignment "DEPLOY_COOKIE_SECURE" "auto"' in INSTALLER
