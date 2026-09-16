import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import agent
import certificates
import nginx_install as installer


@pytest.fixture
def docker_env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(installer, "INSTALL_ROOT", tmp_path / "managed")
    monkeypatch.setattr(installer, "os", SimpleNamespace(name="posix", geteuid=lambda: 0))
    monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(installer.nginx_runtime, "require_local_docker", lambda: None)
    checked_ports = []
    monkeypatch.setattr(installer, "_check_ports", lambda ports: checked_ports.append(ports))
    monkeypatch.setattr(installer, "check_http", lambda port, **kwargs: 200)
    state = {"created": False, "name": "", "owner": "", "fail_start": False, "foreign_owner": False}
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        action = command[1]
        if action == "info":
            return "26.0"
        if action == "ps":
            return state["name"] if state["created"] else ""
        if action == "create":
            state.update(created=True, name=command[command.index("--name") + 1],
                         owner=command[command.index("--label") + 1].split("=", 1)[1])
            return "a" * 64
        if action == "start" and state["fail_start"]:
            raise certificates.CertificateError("start failed")
        if action == "inspect":
            if "{{.Id}}" in command:
                return "a" * 64
            return json.dumps({installer.OWNER_LABEL: "foreign" if state["foreign_owner"] else state["owner"]})
        if action == "rm":
            state["created"] = False
        return ""

    monkeypatch.setattr(installer, "run", run)
    return data, state, commands, checked_ports


def test_docker_http_install_needs_no_domain_or_certificate(docker_env):
    data, state, commands, ports = docker_env
    plan = installer.docker_plan(data, "edge", 8080, False)
    assert not (data / "certificates").exists()
    assert not any(command[1] in {"create", "pull"} for command in commands)
    result = installer.install_docker(data, plan)
    assert result["port"] == 8080 and result["http_status"] == 200
    create = next(command for command in commands if command[1] == "create")
    assert "8080:80" in create and "443:443" not in create
    mounts = [create[i + 1] for i, arg in enumerate(create) if arg == "--mount"]
    assert len(mounts) == 2
    assert all(mount.endswith(",readonly") for mount in mounts)
    assert f"src={data / 'certificates'}," in mounts[1]
    assert "--privileged" not in create
    assert state["created"]
    assert all(value == [8080] for value in ports)
    assert list((data / "certificates").iterdir()) == []
    config = (installer.INSTALL_ROOT / "edge/conf.d/00-welcome.conf").read_text()
    assert "listen 80" in config and "ssl_certificate" not in config
    assert "HTTP ready" in config


def test_reserving_https_does_not_enable_tls(docker_env):
    data, _, commands, ports = docker_env
    plan = installer.docker_plan(data, "edge", 8080, True)
    installer.install_docker(data, plan)
    create = next(command for command in commands if command[1] == "create")
    assert "443:443" in create
    assert ports[0] == [8080, 443]
    assert "443" not in installer.HTTP_CONFIG


@pytest.mark.parametrize("args", [("../other", 80, False), ("edge;reboot", 80, False),
                                  ("edge", True, False), ("edge", 0, False), ("edge", 65536, False),
                                  ("edge", 443, True), ("edge", 80, "yes")])
def test_invalid_install_inputs_never_reach_docker(docker_env, args):
    data, _, commands, _ = docker_env
    with pytest.raises(certificates.CertificateError):
        installer.docker_plan(data, *args)
    assert not commands


def test_existing_container_and_unmanaged_directory_are_preserved(docker_env):
    data, state, commands, _ = docker_env
    state.update(created=True, name="edge")
    with pytest.raises(certificates.CertificateError, match="已经存在"):
        installer.docker_plan(data, "edge", 80, False)
    state["created"] = False
    root = installer.INSTALL_ROOT / "edge"
    root.mkdir(parents=True)
    (root / "keep.txt").write_text("keep")
    with pytest.raises(certificates.CertificateError, match="不是本次安装"):
        installer.docker_plan(data, "edge", 80, False)
    assert (root / "keep.txt").read_text() == "keep"
    assert not any(command[1] == "rm" for command in commands)


def test_failure_removes_only_owned_container_and_allows_retry(docker_env):
    data, state, commands, _ = docker_env
    plan = installer.docker_plan(data, "edge", 8080, False)
    state["fail_start"] = True
    with pytest.raises(certificates.CertificateError, match="本次创建的容器已移除"):
        installer.install_docker(data, plan)
    assert ["docker", "rm", "-f", "a" * 64] in commands
    assert not state["created"]
    state["fail_start"] = False
    assert installer.docker_plan(data, "edge", 8080, False) == plan
    assert installer.install_docker(data, plan)["http_status"] == 200


def test_foreign_container_cannot_be_removed_during_cleanup(docker_env):
    data, state, commands, _ = docker_env
    plan = installer.docker_plan(data, "edge", 8080, False)
    state.update(fail_start=True, foreign_owner=True)
    with pytest.raises(certificates.CertificateError, match="不会删除无法确认归属"):
        installer.install_docker(data, plan)
    assert not any(command[1] == "rm" for command in commands)


def test_http_failure_is_not_reported_as_install_success(docker_env, monkeypatch):
    data, _, commands, _ = docker_env
    plan = installer.docker_plan(data, "edge", 8080, False)

    def fail(port, **kwargs):
        raise certificates.CertificateError("HTTP failed")

    monkeypatch.setattr(installer, "check_http", fail)
    with pytest.raises(certificates.CertificateError, match="HTTP failed"):
        installer.install_docker(data, plan)
    assert any(command[1] == "rm" for command in commands)


def test_modified_config_is_not_overwritten_on_retry(docker_env):
    data, state, _, _ = docker_env
    plan = installer.docker_plan(data, "edge", 8080, False)
    state["fail_start"] = True
    with pytest.raises(certificates.CertificateError):
        installer.install_docker(data, plan)
    file = installer.INSTALL_ROOT / "edge/conf.d/00-welcome.conf"
    file.write_text("user modified config")
    with pytest.raises(certificates.CertificateError, match="配置已经修改"):
        installer.docker_plan(data, "edge", 8080, False)
    assert file.read_text() == "user modified config"


def test_missing_docker_and_remote_docker_fail_before_install(docker_env, monkeypatch):
    data, _, commands, _ = docker_env
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)
    with pytest.raises(certificates.CertificateError, match="未安装 Docker"):
        installer.docker_plan(data, "edge", 80, False)
    monkeypatch.setattr(installer.shutil, "which", lambda name: "docker")

    def reject():
        raise certificates.CertificateError("remote Docker rejected")

    monkeypatch.setattr(installer.nginx_runtime, "require_local_docker", reject)
    with pytest.raises(certificates.CertificateError, match="remote"):
        installer.docker_plan(data, "edge", 80, False)
    assert not commands


def test_published_and_host_ports_are_checked(monkeypatch):
    monkeypatch.setattr(installer, "run", lambda *args, **kwargs: "0.0.0.0:8080->80/tcp")
    with pytest.raises(certificates.CertificateError, match="其他容器"):
        installer._check_ports([8080])
    monkeypatch.setattr(installer, "run", lambda *args, **kwargs: "")
    with installer.socket.socket() as occupied:
        occupied.bind(("0.0.0.0", 0))
        occupied.listen()
        with pytest.raises(certificates.CertificateError, match="被占用"):
            installer._check_ports([occupied.getsockname()[1]])


def test_http_probe_checks_real_response_and_welcome_marker(monkeypatch):
    class Response(BaseHTTPRequestHandler):
        body = b"mini_deploy Nginx HTTP ready\n"

        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(self.body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Response)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(installer.time, "sleep", lambda delay: None)
    try:
        assert installer.check_http(server.server_port, welcome=True) == 200
        Response.body = b"different application"
        with pytest.raises(certificates.CertificateError, match="HTTP"):
            installer.check_http(server.server_port, welcome=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_agent_docker_install_plan_and_apply_without_projects(docker_env, monkeypatch):
    data, _, _, _ = docker_env
    monkeypatch.setattr(agent, "STATE_FILE", data / "state.json")
    monkeypatch.setattr(agent, "PROJECTS", {})
    monkeypatch.setattr(agent, "_nginx_settings", lambda: installer.nginx_runtime.Settings(data))
    monkeypatch.setattr(agent, "_nginx_payload", lambda: {"projects": []})
    saved = []
    monkeypatch.setattr(agent, "_save_nginx_settings", saved.append)
    request = {"mode": "docker", "container": "edge", "port": 8080, "reserve_https": False}
    plan = agent._nginx_install_operation({**request, "action": "plan-install"})["plan"]
    with pytest.raises(certificates.CertificateError, match="已变化"):
        agent._nginx_install_operation({**request, "action": "install-nginx", "port": 8081, "token": plan["token"]})
    result = agent._nginx_install_operation({**request, "action": "install-nginx", "token": plan["token"]})
    assert result["access_port"] == 8080 and result["http_status"] == 200
    assert saved == [{"mode": "docker", "container": "edge"}]
