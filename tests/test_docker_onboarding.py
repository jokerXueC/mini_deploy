import http.client
import json
import os
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import agent
import docker_onboarding as docker
import project_guidance


def settings(**extra):
    return {"mode": "dockerfile", "file": "Dockerfile", "container_port": 8000,
            "published_port": 8080, "access": "public", "volumes": [], "environment": [], **extra}


def project(tmp_path, monkeypatch, config=None):
    monkeypatch.setattr(agent, "STATE_FILE", tmp_path / "panel" / "state.json")
    workdir = tmp_path / "repo"
    workdir.mkdir(exist_ok=True)
    (workdir / ".git").mkdir(exist_ok=True)
    (workdir / "Dockerfile").write_text("FROM python:3.12\nEXPOSE 8000\nCMD [\"python\", \"main.py\"]\n", encoding="utf-8")
    return agent._project_from_form({"key": "demo", "template": "docker", "workdir": str(workdir),
        "repo": "https://example.test/demo.git", "deploy_log_file": str(tmp_path / "deploy.log"),
        "docker_config": config or settings()})


def test_dockerfile_detection_prioritizes_docker_and_does_not_copy_example_secrets():
    result = project_guidance.detect({"Dockerfile": "FROM python:3\nEXPOSE 8000\nCMD python main.py",
        "requirements.txt": "fastapi", ".env.example": "PASSWORD=do-not-copy\nPORT=8000"})
    assert result["candidates"][0]["template"] == "docker"
    assert result["docker"]["ports"] == [8000]
    assert {"name": "PASSWORD", "required": False} in result["docker"]["environment"]
    assert "do-not-copy" not in json.dumps(result)


def test_final_stage_ports_and_startup_are_used():
    text = 'FROM node AS build\nEXPOSE 3000\nCMD node server\nFROM nginx\nEXPOSE 80\n'
    hint = docker.detect({"Dockerfile": text})
    assert hint["ports"] == [80]
    assert any("启动命令" in warning for warning in hint["warnings"])
    inherited = docker.detect({"Dockerfile": 'FROM python AS base\nEXPOSE 8000\nCMD python main.py\nFROM base\n'})
    assert inherited["ports"] == [8000]
    assert not inherited["warnings"]


def test_compose_variables_skip_defaults_and_escaped_interpolation():
    hint = docker.detect({"compose.yaml": 'services:\n  api:\n    image: "${IMAGE:?required}"\n    environment:\n      PASSWORD: ${PASSWORD}\n      PORT: ${PORT:-8000}\n      LITERAL: $${IGNORED}\n',
                         ".env.example": "PASSWORD=secret\nOPTIONAL=example"})
    assert hint["environment"] == [dict(name='IMAGE', required=True), dict(name='PASSWORD', required=True), dict(name='OPTIONAL', required=False)]


def test_alternative_compose_files_have_separate_variables_and_standard_overrides():
    hint = docker.detect({'compose.yaml': 'services: {api: {image: "${IMAGE}"}}',
                          'compose.override.yml': 'services: {api: {environment: {PASSWORD: "${PASSWORD}"}}}',
                          'docker-compose.yml': 'services: {web: {image: "${OTHER}"}}'})
    assert hint['choices'][0]['environment'] == [dict(name='IMAGE', required=True), dict(name='PASSWORD', required=True)]
    assert hint['choices'][1]['environment'] == [dict(name='OTHER', required=True)]


@pytest.mark.parametrize('extra', [
    {'file': '../Dockerfile'}, {'file': '/etc/passwd'}, {'container_port': 0}, {'published_port': 6868},
    {'published_port': 70000}, {'volumes': ['/']}, {'volumes': ['/etc/ssh']}, {'volumes': ['/app/../data']},
    {'volumes': ['/app', '/app/uploads']}, {'environment': ['PATH']}, {'environment': ['DOCKER_HOST']},
    {'environment': ['COMPOSE_FILE']}, {'environment': ['PASSWORD;id']}, {'access': 'unknown'},
])
def test_bad_configuration_rejected(extra):
    with pytest.raises(ValueError):
        docker.normalize(settings(**extra))


def test_managed_compose_uses_structured_yaml_and_no_secret_values(tmp_path, monkeypatch):
    p = project(tmp_path, monkeypatch, settings(environment=['PASSWORD'], volumes=['/app/uploads']))
    directory = agent._docker_project_directory(p.key)
    data = yaml.safe_load(docker.compose_text(p.docker_config, p.workdir, directory))['services']['app']
    assert data['ports'] == ['0.0.0.0:8080:8000']
    assert data['build']['context'] == p.workdir.as_posix()
    assert data['environment']['PASSWORD'].startswith('${PASSWORD:?')
    assert data['volumes'][0]['target'] == '/app/uploads'
    assert data['volumes'][0]['type'] == 'volume'
    assert p.script == str(directory / 'deploy.sh')
    assert p.service_port == 8080
    assert agent._project_from_config(agent._project_to_config(p), p.key).docker_config == p.docker_config
    preview = agent._project_preview(p)
    assert [file['kind'] for file in preview['files']] == ['deploy.sh', 'compose']
    assert not directory.exists()


def test_bootstrap_preserves_repo_and_variables_are_separate(tmp_path, monkeypatch):
    p = project(tmp_path, monkeypatch, settings(environment=['PASSWORD'], volumes=['/app/uploads']))
    original = p.workdir / 'deploy.sh'
    original.write_text('keep original', encoding='utf-8')
    monkeypatch.setattr(agent.shutil, 'which', lambda command: command)
    monkeypatch.setattr(agent, '_run_bootstrap_command', lambda args, **kw: agent._bootstrap_result(kw['step'], True, 'ok'))
    secret = "p@ss'word$VAR\n中文"
    result = agent._bootstrap_project(p, environment={'PASSWORD': secret})
    assert result['ok']
    assert original.read_text(encoding='utf-8') == 'keep original'
    assert not (p.workdir / 'compose.yaml').exists()
    directory = agent._docker_project_directory(p.key)
    compose = yaml.safe_load((directory / 'compose.yaml').read_text(encoding='utf-8'))
    assert compose['volumes']['data_0']['name'] == 'mini-demo-data-0'
    assert json.loads((directory / 'environment.json').read_text(encoding='utf-8')) == {'PASSWORD': secret}
    if os.name != 'nt':
        assert directory.stat().st_mode & 0o777 == 0o700
        assert (directory / 'environment.json').stat().st_mode & 0o777 == 0o600
    assert secret not in json.dumps(agent._project_to_config(p))
    assert secret not in json.dumps(agent._project_preview(p))
    assert secret not in json.dumps(result)
    assert secret not in Path(p.script).read_text(encoding='utf-8')
    assert agent._bootstrap_project(p)['ok']


def test_plan_changes_cannot_repoint_existing_data(tmp_path, monkeypatch):
    p = project(tmp_path, monkeypatch)
    agent._prepare_docker_files(p, {})
    changed = agent._project_from_form({'docker_config': settings(volumes=['/app'])}, p)
    with pytest.raises(ValueError, match='已有容器运行计划'):
        agent._prepare_docker_files(changed, {})


def test_retry_can_change_access_port_and_variables_without_moving_data(tmp_path, monkeypatch):
    p = project(tmp_path, monkeypatch, settings(volumes=['/app/uploads']))
    agent._prepare_docker_files(p, {})
    before = yaml.safe_load((agent._docker_project_directory(p.key) / 'compose.yaml').read_text(encoding='utf-8'))
    changed = agent._project_from_form({'docker_config': settings(volumes=['/app/uploads'], published_port=8081, environment=['PASSWORD'])}, p)
    agent._prepare_docker_files(changed, {'PASSWORD': 'new-secret'})
    preview = agent._project_preview(changed)
    assert '8081:8000' in preview['files'][1]['content']
    assert preview['files'][1]['managed']
    after = yaml.safe_load(preview['files'][1]['content'])
    assert after['volumes'] == before['volumes']
    assert docker.execution_environment(agent._docker_project_directory(p.key) / 'environment.json', ['PASSWORD'])['PASSWORD'] == 'new-secret'


def test_source_must_exist_before_preparation(tmp_path, monkeypatch):
    p = project(tmp_path, monkeypatch)
    (p.workdir / 'Dockerfile').unlink()
    with pytest.raises(ValueError, match='不存在'):
        agent._prepare_docker_files(p, {})
    assert not agent._docker_project_directory(p.key).exists()


def test_existing_compose_and_override_are_kept(tmp_path, monkeypatch):
    config = {'mode': 'compose', 'file': 'compose.yaml', 'environment': ['PASSWORD']}
    p = project(tmp_path, monkeypatch, config)
    text = 'services:\n  db:\n    image: postgres\n'
    (p.workdir / 'compose.yaml').write_text(text, encoding='utf-8')
    (p.workdir / 'compose.override.yml').write_text('services: {}', encoding='utf-8')
    agent._prepare_docker_files(p, {'PASSWORD': 'abc$def'})
    assert (p.workdir / 'compose.yaml').read_text(encoding='utf-8') == text
    cmd = docker.command(p.workdir, p.docker_config, agent._docker_project_directory(p.key), p.key)
    assert str(p.workdir / 'compose.override.yml') in cmd
    assert '--project-name' not in cmd
    assert 'PASSWORD' not in ' '.join(cmd)


def test_runner_provides_literal_variables_without_shell_or_secret_arguments(tmp_path, monkeypatch):
    p = project(tmp_path, monkeypatch, settings(environment=['PASSWORD']))
    secret = 'abc${UNEXPANDED}; touch not-a-command'
    agent._prepare_docker_files(p, {'PASSWORD': secret})
    monkeypatch.setattr('sys.argv', ['docker_onboarding.py', '--workdir', str(p.workdir), '--directory', str(agent._docker_project_directory(p.key)), '--key', p.key])
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(docker.subprocess, 'run', run)
    assert docker.main() == 0
    assert len(calls) == 3
    assert calls[0][0][-2:] == ['config', '--quiet']
    assert calls[1][0][-3:] == ['up', '-d', '--build']
    for command, kwargs in calls:
        assert kwargs['env']['PASSWORD'] == secret
        assert secret not in ' '.join(command)
        assert not kwargs.get('shell')


def test_missing_environment_is_blocking_and_not_logged(tmp_path, monkeypatch):
    p = project(tmp_path, monkeypatch, settings(environment=['PASSWORD']))
    checks = agent._docker_readiness(p)
    assert any(check['key'] == 'docker_environment' and not check['ok'] for check in checks)


def test_http_keeps_environment_out_of_saved_config_and_audit(tmp_path, monkeypatch):
    p = project(tmp_path, monkeypatch, settings(environment=['PASSWORD']))
    monkeypatch.setattr(agent, 'PROJECTS', {})
    monkeypatch.setattr(agent, 'PROJECTS_CONFIG_FILE', tmp_path / 'projects.json')
    monkeypatch.setattr(agent, 'PROJECT_CONFIG_BACKUP_DIR', tmp_path / 'backups')
    monkeypatch.setattr(agent, 'UI_SESSION_SECRET', '0123456789abcdef' * 4)
    monkeypatch.setattr(agent, '_log', lambda *args: None)
    audit, calls = [], []
    monkeypatch.setattr(agent, '_audit_event', lambda *args, **kw: audit.append(kw))
    def bootstrap(project, **kwargs):
        calls.append(kwargs)
        return {'ok': True, 'results': []}
    monkeypatch.setattr(agent, '_bootstrap_project', bootstrap)
    cookie = agent._make_session_cookie()
    headers = {'Cookie': f'{agent.COOKIE_NAME}={cookie}', 'X-CSRF-Token': agent._csrf_token(cookie), 'Content-Type': 'application/json'}
    server = ThreadingHTTPServer(('127.0.0.1', 0), agent.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        body = {'project': agent._project_to_config(p), 'options': {'environment': {'PASSWORD': 'secret-should-not-leak'}}}
        client.request('POST', '/projects-config/bootstrap', json.dumps(body), headers)
        response = client.getresponse()
        text = response.read().decode()
        assert response.status == 200, text
        assert calls[0]['environment'] == {'PASSWORD': 'secret-should-not-leak'}
        assert 'secret-should-not-leak' not in text
        assert 'secret-should-not-leak' not in agent.PROJECTS_CONFIG_FILE.read_text(encoding='utf-8')
        assert 'secret-should-not-leak' not in json.dumps(audit)
        body['options']['environment'] = {'PASSWORD': ''}
        client.request('POST', '/projects-config/bootstrap', json.dumps(body), headers)
        response = client.getresponse()
        response.read()
        assert response.status == 400
        assert len(calls) == 1
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
