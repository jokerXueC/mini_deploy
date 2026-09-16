import json
from types import SimpleNamespace

import pytest

import agent
import certificates
import nginx_runtime as nginx


@pytest.fixture
def wizard(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    conf = tmp_path / "conf.d"
    conf.mkdir()
    settings = nginx.Settings(data)
    monkeypatch.setattr(agent, "STATE_FILE", data / "state.json")
    monkeypatch.setattr(agent, "PROJECTS_CONFIG_FILE", data / "projects.json")
    monkeypatch.setattr(agent, "PROJECT_CONFIG_BACKUP_DIR", data / "backups")
    monkeypatch.setattr(agent, "_nginx_settings", lambda: settings)
    monkeypatch.setattr(agent, "_log", lambda message: None)
    project = agent._project_from_form({"key": "api", "name": "API", "workdir": str(tmp_path),
                                        "enabled": False, "service_port": 8000})
    monkeypatch.setattr(agent, "PROJECTS", {"api": project})
    monkeypatch.setattr(agent, "DEFAULT_PROJECT_KEY", "api")
    monkeypatch.setattr(nginx, "local_setup_plan", lambda: {"installed": False, "active": False, "package_manager": "apt-get"})
    prepared = []
    monkeypatch.setattr(nginx, "prepare_local", prepared.append)
    monkeypatch.setattr(settings, "candidate", lambda mode, name="": {"mode": mode})

    def runtime_init(self, profile, data_home, *, live=True):
        self.profile = profile
        self.mode = profile.get("mode", "local")
        self.data_home = data_home
        self.conf_root = conf
        self.container_id = "a" * 64 if self.mode == "docker" else ""

    monkeypatch.setattr(nginx.Runtime, "__init__", runtime_init)
    monkeypatch.setattr(nginx.Runtime, "probe", lambda *args: None)
    monkeypatch.setattr(nginx, "run", lambda *args, **kwargs: "")
    monkeypatch.setattr(certificates, "run", lambda *args, **kwargs: "")
    request = {"action": "plan-site", "project": "api", "domain": "api.example.com", "port": 8000}
    return settings, conf, request, prepared


def test_plan_does_not_install_or_save_and_apply_creates_http_site(wizard):
    settings, conf, request, prepared = wizard
    plan = agent._nginx_site_operation(request)["plan"]
    assert "apt-get" in plan["steps"][0]
    assert "127.0.0.1:8000" in plan["config"]
    assert not settings.path.exists()
    assert not agent.PROJECTS_CONFIG_FILE.exists()
    assert not prepared
    result = agent._nginx_site_operation({**request, "action": "apply-site", "token": plan["token"]})
    assert result["url"] == "http://api.example.com"
    assert len(prepared) == 1
    assert settings.read()["upstreams"]["api"] == "127.0.0.1"
    assert agent.PROJECTS["api"].app_domain == "api.example.com"
    assert result["settings"]["projects"][0]["site_configured"]
    assert "server_name api.example.com;" in (conf / "mini-deploy-api.conf").read_text()


def test_changed_plan_cannot_install_or_apply(wizard):
    settings, conf, request, prepared = wizard
    token = agent._nginx_site_operation(request)["plan"]["token"]
    with pytest.raises(certificates.CertificateError, match="已变化"):
        agent._nginx_site_operation({**request, "port": 8080, "action": "apply-site", "token": token})
    assert not prepared
    assert not settings.path.exists()
    assert not list(conf.iterdir())


def test_standalone_install_needs_no_project_or_domain(wizard, monkeypatch):
    settings, conf, _, prepared = wizard
    monkeypatch.setattr(agent, "PROJECTS", {})
    plan = agent._nginx_install_operation({"action": "plan-install"})["plan"]
    assert not prepared
    result = agent._nginx_install_operation({"action": "install-local", "token": plan["token"]})
    assert result["ok"]
    assert len(prepared) == 1
    assert result["settings"]["projects"] == []
    assert not settings.path.exists()
    assert not agent.PROJECTS_CONFIG_FILE.exists()
    assert not list(conf.iterdir())


def test_standalone_install_rejects_changed_environment(wizard, monkeypatch):
    _, _, _, prepared = wizard
    plan = agent._nginx_install_operation({"action": "plan-install"})["plan"]
    monkeypatch.setattr(nginx, "local_setup_plan", lambda: {"installed": True, "active": True, "package_manager": ""})
    with pytest.raises(certificates.CertificateError, match="环境已变化"):
        agent._nginx_install_operation({"action": "install-local", "token": plan["token"]})
    assert not prepared


def test_standalone_install_does_not_switch_existing_docker_profile(wizard):
    settings, _, _, _ = wizard
    settings.save({"mode": "docker", "container": "edge", "network": "bridge"}, {"api": "backend"})
    before = settings.path.read_bytes()
    plan = agent._nginx_install_operation({"action": "plan-install"})["plan"]
    agent._nginx_install_operation({"action": "install-local", "token": plan["token"]})
    assert settings.path.read_bytes() == before


def test_failed_reload_rolls_back_site_project_and_settings(wizard, monkeypatch):
    settings, conf, request, prepared = wizard
    original = dict(agent.PROJECTS)
    token = agent._nginx_site_operation(request)["plan"]["token"]

    def failure(command, **kwargs):
        if command[:2] == ["systemctl", "reload"]:
            raise certificates.CertificateError("reload failed")
        return ""

    monkeypatch.setattr(certificates, "run", failure)
    with pytest.raises(certificates.CertificateError):
        agent._nginx_site_operation({**request, "action": "apply-site", "token": token})
    assert agent.PROJECTS == original
    assert not settings.path.exists()
    assert not (conf / "mini-deploy-api.conf").exists()
    assert json.loads(agent.PROJECTS_CONFIG_FILE.read_text())["projects"][0]["app_domain"] == ""


@pytest.mark.parametrize("change", [{"domain": "bad;host"}, {"port": True}, {"port": 65536}, {"project": "missing"}])
def test_invalid_input_is_rejected_before_install(wizard, change):
    _, _, request, prepared = wizard
    with pytest.raises(certificates.CertificateError):
        agent._nginx_site_operation({**request, **change})
    assert not prepared


def test_unreachable_backend_blocks_plan(wizard, monkeypatch):
    _, _, request, prepared = wizard

    def failure(*args):
        raise certificates.CertificateError("业务未启动")

    monkeypatch.setattr(nginx.Runtime, "probe", failure)
    with pytest.raises(certificates.CertificateError, match="业务未启动"):
        agent._nginx_site_operation(request)
    assert not prepared


def test_existing_docker_keeps_runtime_and_requires_reachable_host(wizard):
    settings, _, request, prepared = wizard
    settings.save({"mode": "docker", "network": "bridge", "container": "edge"}, {})
    with pytest.raises(certificates.CertificateError, match="127.0.0.1"):
        agent._nginx_site_operation(request)
    plan = agent._nginx_site_operation({**request, "host": "backend"})["plan"]
    assert plan["mode"] == "docker"
    assert plan["local_setup"] is None
    assert "http://backend:8000" in plan["config"]
    assert not prepared


def test_external_site_and_active_certificate_are_not_overwritten(wizard):
    settings, conf, request, prepared = wizard
    (conf / "mini-deploy-api.conf").write_text("server {}")
    with pytest.raises(certificates.CertificateError):
        agent._nginx_site_operation(request)
    (conf / "mini-deploy-api.conf").unlink()
    (settings.data_home / "certificates" / "api").mkdir(parents=True)
    with pytest.raises(certificates.CertificateError, match="已有上传证书"):
        agent._nginx_site_operation(request)
    assert not prepared


def test_domain_conflict_ignores_own_site_but_rejects_another(wizard, monkeypatch):
    settings, conf, _, _ = wizard
    runtime = settings.runtime()
    own = conf / "mini-deploy-api.conf"
    monkeypatch.setattr(nginx, "run", lambda *args: f"# configuration file {own.as_posix()}:\nserver_name api.example.com;")
    nginx.check_domain_conflict(runtime, "api.example.com", own)
    monkeypatch.setattr(nginx, "run", lambda *args: '# configuration file /etc/nginx/conf.d/other.conf:\nserver_name other.test\n "API.EXAMPLE.COM";')
    with pytest.raises(certificates.CertificateError, match="其他 Nginx"):
        nginx.check_domain_conflict(runtime, "api.example.com", own)


@pytest.mark.parametrize("manager", ["apt-get", "dnf", "yum"])
def test_install_uses_fixed_commands_and_no_shell(monkeypatch, manager):
    plan = {"installed": False, "active": False, "package_manager": manager}
    commands = []
    monkeypatch.setattr(nginx, "local_setup_plan", lambda: plan)
    monkeypatch.setattr(nginx, "run", lambda command, **kwargs: commands.append(command) or "")
    nginx.prepare_local(plan)
    assert any("install" in command and command[-1] == "nginx" for command in commands)
    assert commands[-2:] == [["nginx", "-t"], ["systemctl", "enable", "--now", "nginx"]]
    assert all(isinstance(command, list) and "sh" not in command for command in commands)


def test_existing_active_local_nginx_is_not_reinstalled(monkeypatch):
    plan = {"installed": True, "active": True, "package_manager": ""}
    commands = []
    monkeypatch.setattr(nginx, "local_setup_plan", lambda: plan)
    monkeypatch.setattr(nginx, "run", lambda command, **kwargs: commands.append(command))
    nginx.prepare_local(plan)
    assert commands == [["nginx", "-t"]]


def test_docker_published_port_blocks_local_install(monkeypatch):
    monkeypatch.setattr(nginx, "os", SimpleNamespace(name="posix", geteuid=lambda: 0))
    monkeypatch.setattr(nginx.shutil, "which", lambda name: name if name != "nginx" else None)
    monkeypatch.setattr(nginx, "require_local_docker", lambda: None)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[0] == "systemctl":
            return "inactive"
        return "0.0.0.0:80->80/tcp"

    monkeypatch.setattr(nginx, "run", run)
    with pytest.raises(certificates.CertificateError, match="Docker 容器"):
        nginx.local_setup_plan()
    assert not any("install" in command for command in commands)


def test_occupied_host_port_blocks_local_install(monkeypatch):
    monkeypatch.setattr(nginx, "os", SimpleNamespace(name="posix", geteuid=lambda: 0))
    monkeypatch.setattr(nginx.shutil, "which", lambda name: name if name in {"systemctl", "apt-get"} else None)
    monkeypatch.setattr(nginx, "run", lambda *args, **kwargs: "inactive")

    class BusySocket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def bind(self, address):
            assert address == ("0.0.0.0", 80)
            raise OSError("busy")

    monkeypatch.setattr(nginx.socket, "socket", lambda *args: BusySocket())
    with pytest.raises(certificates.CertificateError, match="其他服务占用"):
        nginx.local_setup_plan()
