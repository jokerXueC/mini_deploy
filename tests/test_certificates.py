from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from dataclasses import replace

import pytest

import agent
import certificates as certs
import nginx_runtime


@pytest.fixture(scope="module")
def pem_pair(tmp_path_factory):
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("OpenSSL required for real certificate validation")
    directory = tmp_path_factory.mktemp("pem")
    key, cert = directory / "key.pem", directory / "cert.pem"
    subprocess.run([
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3",
        "-keyout", str(key), "-out", str(cert), "-subj", "/CN=api.example.test",
        "-addext", "subjectAltName=DNS:api.example.test",
    ], check=True, capture_output=True, timeout=30)
    return {"certificate": cert.read_text(), "private_key": key.read_text(), "label": "Test certificate"}


@pytest.fixture
def setup_store(tmp_path, monkeypatch):
    store = certs.CertificateStore(tmp_path / "data" / "certificates")
    project = agent._project_from_config({"key": "api", "name": "API", "app_domain": "api.example.test",
                                          "service_port": 8000}, "api")
    conf = tmp_path / "nginx" / "api.conf"
    conf.parent.mkdir()
    http = agent._project_nginx_config_text(project)
    commands = []
    real_run = certs.run

    def run(command):
        if command[0] == "openssl":
            return real_run(command)
        commands.append(command)
        return conf.read_text() if command == ["nginx", "-T"] and conf.exists() else ""

    monkeypatch.setattr(certs, "run", run)
    return store, project, conf, http, commands


def test_certificate_lifecycle_and_private_response(setup_store, pem_pair):
    store, project, conf, http, commands = setup_store
    store.operate(project, "upload", pem_pair, conf, http)
    assert not conf.exists()
    first = store.read(project)
    assert not store.describe(project, conf)["certificate"]["active"]
    store.operate(project, "enable", {}, conf, http)
    assert "listen 443 ssl" in conf.read_text()
    assert store.describe(project, conf)["certificate"]["active"]
    assert "PRIVATE KEY" not in json.dumps(store.describe(project, conf))
    assert "private_key" not in store.read(project)
    store.operate(project, "upload", {**pem_pair, "label": "Renewed"}, conf, http)
    assert store.read(project)["revision"] != first["revision"]
    assert store.read(project)["revision"] in conf.read_text()
    assert store.paths(project, first)[1].exists()
    store.operate(project, "rename", {"label": "API TLS"}, conf, http)
    assert store.read(project)["label"] == "API TLS"
    with pytest.raises(certs.CertificateError, match="正在使用"):
        store.operate(project, "delete", {}, conf, http)
    store.operate(project, "disable", {}, conf, http)
    assert conf.read_text() == http
    store.operate(project, "delete", {}, conf, http)
    assert not store.directory(project).exists()
    assert ["nginx", "-T"] in commands


@pytest.mark.parametrize("failure", [["nginx", "-t"], ["systemctl", "reload", "nginx"]])
def test_failed_replacement_restores_active_configuration(setup_store, pem_pair, monkeypatch, failure):
    store, project, conf, http, _ = setup_store
    store.operate(project, "upload", pem_pair, conf, http)
    store.operate(project, "enable", {}, conf, http)
    original, record = conf.read_text(), store.read(project)
    real_run = certs.run
    failed = False

    def fail_once(command):
        nonlocal failed
        if command == failure and not failed:
            failed = True
            raise certs.CertificateError("simulated failure")
        return real_run(command)

    monkeypatch.setattr(certs, "run", fail_once)
    with pytest.raises(certs.CertificateError, match="已恢复原配置"):
        store.operate(project, "upload", pem_pair, conf, http)
    assert conf.read_text() == original
    assert store.read(project) == record
    assert store.paths(project, record)[1].exists()


def test_failed_first_activation_removes_new_configuration(setup_store, pem_pair, monkeypatch):
    store, project, conf, http, _ = setup_store
    store.operate(project, "upload", pem_pair, conf, http)
    real_run = certs.run
    failed = False

    def fail_once(command):
        nonlocal failed
        if command == ["nginx", "-t"] and not failed:
            failed = True
            raise certs.CertificateError("invalid config")
        return real_run(command)

    monkeypatch.setattr(certs, "run", fail_once)
    with pytest.raises(certs.CertificateError):
        store.operate(project, "enable", {}, conf, http)
    assert not conf.exists()


def test_wrong_domain_and_key_rejected_without_replacing_old_certificate(setup_store, pem_pair):
    store, project, conf, http, _ = setup_store
    store.operate(project, "upload", pem_pair, conf, http)
    record = store.read(project)
    with pytest.raises(certs.CertificateError):
        store.operate(replace(project, app_domain="other.example.test"), "upload", pem_pair, conf, http)
    with pytest.raises(certs.CertificateError):
        store.operate(project, "upload", {**pem_pair, "private_key": "invalid"}, conf, http)
    assert store.read(project) == record
    assert len(list(store.directory(project).iterdir())) == 2


def test_expired_certificate_cannot_be_enabled(setup_store, pem_pair, monkeypatch):
    store, project, conf, http, _ = setup_store
    store.operate(project, "upload", pem_pair, conf, http)
    expires = store.read(project)["expires_at"]
    monkeypatch.setattr(certs.time, "time", lambda: expires + 1)
    with pytest.raises(certs.CertificateError, match="过期"):
        store.operate(project, "enable", {}, conf, http)
    assert not conf.exists()


def test_metadata_failure_after_reload_restores_previous_certificate(setup_store, pem_pair, monkeypatch):
    store, project, conf, http, _ = setup_store
    store.operate(project, "upload", pem_pair, conf, http)
    store.operate(project, "enable", {}, conf, http)
    original, record = conf.read_text(), store.read(project)
    real_write = certs.atomic_write

    def fail_metadata(path, content):
        if path.name == "current.json":
            raise OSError("disk full")
        real_write(path, content)

    monkeypatch.setattr(certs, "atomic_write", fail_metadata)
    with pytest.raises(OSError, match="disk full"):
        store.operate(project, "upload", pem_pair, conf, http)
    assert conf.read_text() == original
    assert store.read(project) == record


def test_refuses_delete_of_unknown_directory_content(setup_store, pem_pair):
    store, project, conf, http, _ = setup_store
    store.operate(project, "upload", pem_pair, conf, http)
    unrelated = store.directory(project) / "business-data"
    unrelated.mkdir()
    with pytest.raises(certs.CertificateError, match="未知内容"):
        store.operate(project, "delete", {}, conf, http)
    assert unrelated.exists()
    assert store.read(project)


def test_nginx_recovery_failure_is_reported_and_preserves_keys(setup_store, pem_pair, monkeypatch):
    store, project, conf, http, _ = setup_store
    store.operate(project, "upload", pem_pair, conf, http)
    store.operate(project, "enable", {}, conf, http)
    old_conf = conf.read_text()
    real_run = certs.run

    def fail_reload(command):
        if command[0] == "systemctl":
            raise certs.CertificateError("service unavailable")
        return real_run(command)

    monkeypatch.setattr(certs, "run", fail_reload)
    with pytest.raises(certs.CertificateError, match="恢复加载失败"):
        store.operate(project, "upload", pem_pair, conf, http)
    assert conf.read_text() == old_conf
    assert len(list(store.directory(project).glob("*/privkey.pem"))) == 2


def test_unmanaged_nginx_config_is_never_overwritten(setup_store, pem_pair):
    store, project, conf, http, _ = setup_store
    conf.write_text("# independently managed site\n")
    store.operate(project, "upload", pem_pair, conf, http)
    with pytest.raises(certs.CertificateError, match="拒绝覆盖"):
        store.operate(project, "enable", {}, conf, http)
    assert conf.read_text() == "# independently managed site\n"


def test_delete_checks_external_references(setup_store, pem_pair, monkeypatch):
    store, project, conf, http, _ = setup_store
    store.operate(project, "upload", pem_pair, conf, http)
    monkeypatch.setattr(certs, "run", lambda command: str(store.directory(project)))
    with pytest.raises(certs.CertificateError, match="仍引用"):
        store.operate(project, "delete", {}, conf, http)
    assert store.read(project)


def test_delete_resolves_normalized_nginx_certificate_paths(setup_store, pem_pair, monkeypatch):
    store, project, conf, http, _ = setup_store
    store.operate(project, "upload", pem_pair, conf, http)
    cert, _ = store.paths(project, store.read(project))
    alias = cert.as_posix().replace('/certificates/api/', '/certificates/unused/../api/')
    monkeypatch.setattr(certs, "run", lambda command: f'ssl_certificate "{alias}";')
    with pytest.raises(certs.CertificateError, match="仍引用"):
        store.operate(project, "delete", {}, conf, http)
    assert store.read(project)


@pytest.mark.parametrize("action", [[], {}, "shell", None])
def test_rejects_invalid_actions(setup_store, action):
    store, project, conf, http, _ = setup_store
    with pytest.raises(certs.CertificateError):
        store.operate(project, action, {}, conf, http)


def test_rejects_traversal_project(setup_store):
    store, project, *_ = setup_store
    with pytest.raises(certs.CertificateError):
        store.read(replace(project, key="../outside"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and symlink checks")
def test_rejects_symlink_and_public_private_key(setup_store, pem_pair, tmp_path):
    store, project, conf, http, _ = setup_store
    store.operate(project, "upload", pem_pair, conf, http)
    cert, key = store.paths(project, store.read(project))
    assert key.stat().st_mode & 0o777 == 0o600
    key.chmod(0o644)
    with pytest.raises(certs.CertificateError, match="0600"):
        store.paths(project, store.read(project))
    key.chmod(0o600)
    outside = tmp_path / "outside.pem"
    outside.write_text("do not touch")
    cert.unlink()
    cert.symlink_to(outside)
    with pytest.raises(certs.CertificateError, match="符号链接"):
        store.operate(project, "delete", {}, conf, http)
    assert outside.read_text() == "do not touch"


@pytest.mark.parametrize("method,authenticated,csrf,expected", [
    ("GET", False, "", 401), ("POST", False, "", 401), ("POST", True, "", 403),
])
@pytest.mark.parametrize("path", ["/certificates", "/nginx-settings"])
def test_routes_require_authentication_and_csrf(monkeypatch, method, authenticated, csrf, expected, path):
    handler = object.__new__(agent.Handler)
    handler.path = path
    handler.command = method
    handler.headers = {"X-CSRF-Token": csrf}
    handler.client_address = ("127.0.0.1", 1234)
    handler._authenticated = lambda: authenticated
    handler._csrf_token_for_request = lambda: "expected"
    responses = []
    handler._write_json = lambda status, payload: responses.append(status)
    monkeypatch.setattr(agent, "_audit_event", lambda *args, **kwargs: None)
    getattr(handler, f"do_{method}")()
    assert responses == [expected]


def test_malformed_operation_does_not_log_or_return_private_key(monkeypatch):
    handler = object.__new__(agent.Handler)
    body = json.dumps({"project": "missing", "action": [], "private_key": "PRIVATE-SECRET"}).encode()
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(body)
    handler.client_address = ("127.0.0.1", 1234)
    responses, audits = [], []
    handler._write_json = lambda status, payload: responses.append((status, payload))
    monkeypatch.setattr(agent, "_audit_event", lambda *args, **kwargs: audits.append(kwargs))
    handler._handle_certificates()
    assert responses[0][0] == 400
    assert "PRIVATE-SECRET" not in json.dumps([responses, audits])


def test_new_ui_asset_is_served():
    assert "certificates.js" in agent.UI_ASSET_TYPES


def test_corrupt_metadata_is_reported_without_crashing_list(setup_store, pem_pair, monkeypatch):
    store, project, conf, http, _ = setup_store
    store.operate(project, "upload", pem_pair, conf, http)
    path = store.directory(project) / "current.json"
    record = store.read(project)
    record.pop("expires_at")
    certs.atomic_write(path, json.dumps(record))
    monkeypatch.setattr(agent, "PROJECTS", {project.key: project})
    monkeypatch.setattr(agent, "_certificate_store", lambda: store)
    monkeypatch.setattr(agent, "_project_nginx_conf_path", lambda _: conf)
    assert agent._certificates_payload()["projects"][0]["error"]


def test_uploaded_certificate_prevents_old_domain_action_from_overwriting_tls(setup_store, pem_pair, monkeypatch):
    store, project, conf, http, _ = setup_store
    store.operate(project, "upload", pem_pair, conf, http)
    store.operate(project, "enable", {}, conf, http)
    original = conf.read_text()
    monkeypatch.setattr(agent, "_certificate_store", lambda: store)
    monkeypatch.setattr(agent, "_project_nginx_conf_path", lambda _: conf)
    assert not agent._configure_project_nginx(project)["ok"]
    assert conf.read_text() == original


def test_docker_certificate_lifecycle_uses_container_paths_and_commands(setup_store, pem_pair, monkeypatch):
    store, project, conf, http, commands = setup_store
    data_home = store.root.parent
    runtime = nginx_runtime.Runtime({"mode": "docker", "container": "edge", "network": "bridge",
                                     "conf_root": str(conf.parent)}, data_home, live=False)
    runtime.container_id = "b" * 64
    store.runtime = runtime
    real_run = certs.run

    def docker_run(command):
        if command[0] == "docker":
            commands.append(command)
            return conf.read_text() if command[-1] == "-T" and conf.exists() else ""
        return real_run(command)

    monkeypatch.setattr(certs, "run", docker_run)
    store.operate(project, "upload", pem_pair, conf, http)
    store.operate(project, "enable", {}, conf, http)
    assert 'ssl_certificate "/etc/mini-deploy/certificates/api/' in conf.read_text()
    assert str(data_home) not in conf.read_text()
    assert store.describe(project, conf)["certificate"]["active"]
    store.operate(project, "upload", pem_pair, conf, http)
    store.operate(project, "disable", {}, conf, http)
    store.operate(project, "delete", {}, conf, http)
    assert not store.directory(project).exists()
    assert ["docker", "exec", runtime.container_id, "nginx", "-s", "reload"] in commands
    assert not any(command[0] == "systemctl" for command in commands)


def test_docker_failed_reload_restores_old_certificate_config(setup_store, pem_pair, monkeypatch):
    store, project, conf, http, commands = setup_store
    runtime = nginx_runtime.Runtime({"mode": "docker", "network": "bridge", "conf_root": str(conf.parent)}, store.root.parent, live=False)
    runtime.container_id = "b" * 64
    store.runtime = runtime
    real_run = certs.run
    failed = False

    def docker_run(command):
        nonlocal failed
        if command[0] == "docker":
            if command[-1] == "reload" and not failed:
                failed = True
                raise certs.CertificateError("reload failed")
            commands.append(command)
            return ""
        return real_run(command)

    monkeypatch.setattr(certs, "run", docker_run)
    store.operate(project, "upload", pem_pair, conf, http)
    conf.write_text(http)
    with pytest.raises(certs.CertificateError, match="已恢复原配置"):
        store.operate(project, "enable", {}, conf, http)
    assert conf.read_text() == http
    assert store.paths(project, store.read(project))[1].exists()
