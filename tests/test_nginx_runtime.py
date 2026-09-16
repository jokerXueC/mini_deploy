from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import agent
import certificates
import nginx_runtime as nginx


@pytest.fixture
def docker_setup(tmp_path, monkeypatch):
    data = tmp_path / "data"
    cert_root = data / "certificates"
    conf_root = tmp_path / "nginx-conf"
    cert_root.mkdir(parents=True, mode=0o700)
    conf_root.mkdir(mode=0o700)
    item = {"id": "a" * 64, "name": "/edge", "running": True, "network": "project_default",
            "ports": {"80/tcp": [{"HostPort": "80"}]}, "mounts": [
                {"Type": "bind", "Source": str(conf_root), "Destination": nginx.CONF_DEST, "RW": False},
                {"Type": "bind", "Source": str(cert_root), "Destination": nginx.CERT_DEST, "RW": False},
            ]}
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[1] == "context":
            return json.dumps("unix:///var/run/docker.sock")
        if command[1] == "inspect":
            return json.dumps(item)
        if command[1] == "ps":
            return "edge\n"
        if command[-1] == "-T":
            return "include /etc/nginx/conf.d/*.conf;"
        return ""

    monkeypatch.setattr(nginx, "run", run)
    monkeypatch.setattr(nginx.shutil, "which", lambda name: f"/usr/bin/{name}")
    settings = nginx.Settings(data)
    return settings, item, commands


def test_detects_local_and_docker_and_reports_only_selected_fields(docker_setup):
    settings, item, _ = docker_setup
    item["Env"] = ["PASSWORD=secret"]
    detected = settings.discover()
    assert detected["local"]
    assert detected["containers"][0]["name"] == "edge"
    assert "secret" not in json.dumps(detected)
    assert detected["certificate_mount"].endswith(nginx.CERT_DEST + ":ro")


def test_candidate_persists_then_uses_verified_container_id_and_paths(docker_setup):
    settings, item, commands = docker_setup
    profile = settings.candidate("docker", "edge")
    settings.save(profile, {"api": "api"})
    runtime = settings.runtime()
    assert runtime.command("test") == ["docker", "exec", item["id"], "nginx", "-t"]
    assert runtime.command("reload") == ["docker", "exec", item["id"], "nginx", "-s", "reload"]
    assert runtime.visible_path(settings.data_home / "certificates/api/rev/key.pem") == nginx.CERT_DEST + "/api/rev/key.pem"
    runtime.probe("api", 8000)
    assert commands[-1][-1] == "http://api:8000/"
    assert commands[-1][2] == item["id"]
    assert settings.read()["upstreams"] == {"api": "api"}


@pytest.mark.parametrize("change", ["volume", "single-file", "wrong-cert-root", "nested", "stopped", "network-none"])
def test_invalid_container_layout_is_rejected(docker_setup, change):
    settings, item, _ = docker_setup
    if change == "volume":
        item["mounts"][0]["Type"] = "volume"
    elif change == "single-file":
        item["mounts"][0]["Destination"] += "/default.conf"
    elif change == "wrong-cert-root":
        item["mounts"][1]["Source"] = str(settings.data_home)
    elif change == "nested":
        item["mounts"].append({"Type": "bind", "Destination": nginx.CERT_DEST + "/hidden"})
    elif change == "stopped":
        item["running"] = False
    else:
        item["network"] = "none"
    with pytest.raises(certificates.CertificateError):
        settings.candidate("docker", "edge")
    assert not settings.path.exists()


def test_runtime_rejects_mount_or_network_drift(docker_setup):
    settings, item, _ = docker_setup
    profile = settings.candidate("docker", "edge")
    settings.save(profile, {})
    item["network"] = "host"
    with pytest.raises(certificates.CertificateError, match="已变化"):
        settings.runtime()


@pytest.mark.parametrize("host", [None, "127.0.0.1", "127.0.1.2", "localhost", "api;reboot", "api\nserver", "http://api", "0.0.0.0", "169.254.169.254"])
def test_bridge_upstreams_reject_loopback_and_injection(docker_setup, host):
    settings, _, _ = docker_setup
    settings.save(settings.candidate("docker", "edge"), {})
    with pytest.raises(certificates.CertificateError):
        settings.runtime().upstream(host)


def test_host_network_uses_local_backend_address(docker_setup):
    settings, item, _ = docker_setup
    item["network"] = "host"
    settings.save(settings.candidate("docker", "edge"), {})
    assert settings.runtime().upstream(None) == "127.0.0.1"


def test_failed_probe_does_not_save_upstream(docker_setup, monkeypatch):
    settings, _, _ = docker_setup
    settings.save(settings.candidate("docker", "edge"), {})
    project = agent._project_from_config({"key": "api", "service_port": 8000}, "api")
    monkeypatch.setattr(agent, "_nginx_settings", lambda: settings)
    monkeypatch.setattr(agent, "PROJECTS", {"api": project})

    def fail(*args):
        raise certificates.CertificateError("unreachable")

    monkeypatch.setattr(nginx.Runtime, "probe", fail)
    with pytest.raises(certificates.CertificateError, match="unreachable"):
        agent._nginx_upstream_operation({"action": "save-upstream", "project": "api", "host": "api"})
    assert settings.read()["upstreams"] == {}


def test_switching_instance_with_existing_site_is_blocked(docker_setup, monkeypatch):
    settings, item, _ = docker_setup
    profile = settings.candidate("docker", "edge")
    settings.save(profile, {})
    (Path(profile["conf_root"]) / "mini-deploy-api.conf").write_text("server {}")
    monkeypatch.setattr(agent, "_nginx_settings", lambda: settings)
    monkeypatch.setattr(agent, "STATE_FILE", settings.data_home / "state.json")
    before = settings.path.read_bytes()
    with pytest.raises(certificates.CertificateError, match="站点配置"):
        agent._save_nginx_settings({"mode": "local"})
    assert settings.path.read_bytes() == before


def test_docker_domain_config_uses_saved_upstream_and_no_host_certbot(docker_setup, monkeypatch):
    settings, item, commands = docker_setup
    profile = settings.candidate("docker", "edge")
    settings.save(profile, {"api": "backend"})
    project = agent._project_from_config({"key": "api", "app_domain": "api.example.test", "service_port": 8000}, "api")
    monkeypatch.setattr(agent, "_nginx_settings", lambda: settings)
    monkeypatch.setattr(agent, "STATE_FILE", settings.data_home / "state.json")
    monkeypatch.setattr(certificates, "run", nginx.run)
    result = agent._configure_project_nginx(project, issue_https=True)
    assert result["http_ready"]
    assert not result["https_ok"]
    assert "proxy_pass http://backend:8000;" in agent._project_nginx_conf_path(project).read_text()
    assert not any(command[0] in {"systemctl", "certbot"} for command in commands)
    monkeypatch.setattr(agent, "PROJECTS", {"api": project})
    assert agent._remove_nginx_site({"project": "api"})["ok"]
    assert not agent._project_nginx_conf_path(project).exists()


def test_none_and_invalid_modes_are_explicit(docker_setup):
    settings, _, _ = docker_setup
    settings.save(settings.candidate("none"), {})
    with pytest.raises(certificates.CertificateError, match="尚未接入"):
        settings.runtime()
    with pytest.raises(certificates.CertificateError):
        settings.candidate("shell")


def test_upstream_input_never_becomes_shell_source(docker_setup):
    settings, _, commands = docker_setup
    settings.save(settings.candidate("docker", "edge"), {})
    settings.runtime().probe("backend-service", 8000)
    command = commands[-1]
    assert "backend-service" not in command[-3]
    assert command[-2:] == ["probe", "http://backend-service:8000/"]


def test_remote_docker_is_rejected_before_mount_access(docker_setup, monkeypatch):
    settings, _, commands = docker_setup
    monkeypatch.setenv("DOCKER_HOST", "tcp://example.test:2375")
    with pytest.raises(certificates.CertificateError, match="远程"):
        settings.candidate("docker", "edge")
    assert not commands


def test_removing_project_cannot_orphan_nginx_site(docker_setup, monkeypatch):
    settings, _, _ = docker_setup
    settings.save(settings.candidate("docker", "edge"), {})
    project = agent._project_from_config({"key": "api"}, "api")
    other = agent._project_from_config({"key": "other"}, "other")
    monkeypatch.setattr(agent, "_nginx_settings", lambda: settings)
    monkeypatch.setattr(agent, "STATE_FILE", settings.data_home / "state.json")
    monkeypatch.setattr(agent, "PROJECTS", {"api": project, "other": other})
    agent._project_nginx_conf_path(project).write_text("server {}")
    with pytest.raises(certificates.CertificateError, match="移除"):
        agent._delete_project_transaction("api")
    assert "api" in agent.PROJECTS


def test_changing_project_cannot_leave_stale_nginx_domain(docker_setup, monkeypatch):
    settings, _, _ = docker_setup
    settings.save(settings.candidate("docker", "edge"), {})
    project = agent._project_from_config({"key": "api", "app_domain": "old.example.test"}, "api")
    monkeypatch.setattr(agent, "_nginx_settings", lambda: settings)
    monkeypatch.setattr(agent, "STATE_FILE", settings.data_home / "state.json")
    monkeypatch.setattr(agent, "PROJECTS", {"api": project})
    agent._project_nginx_conf_path(project).write_text("server {}")
    with pytest.raises(ValueError, match="移除域名入口"):
        agent._save_project_transaction({"key": "api", "app_domain": "new.example.test", "enabled": False}, "api")


def test_unincluded_conf_directory_is_not_accepted(docker_setup, monkeypatch):
    settings, _, _ = docker_setup
    real_run = nginx.run

    def run(command, **kwargs):
        return "http {}" if command[-1] == "-T" else real_run(command, **kwargs)

    monkeypatch.setattr(nginx, "run", run)
    with pytest.raises(certificates.CertificateError, match="include"):
        settings.candidate("docker", "edge")


@pytest.mark.skipif(os.name != "posix", reason="Linux directory permissions")
def test_unsafe_mount_path_is_rejected(docker_setup):
    settings, item, _ = docker_setup
    Path(item["mounts"][0]["Source"]).chmod(0o777)
    with pytest.raises(certificates.CertificateError, match="权限不安全"):
        settings.candidate("docker", "edge")
