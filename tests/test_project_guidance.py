from __future__ import annotations

import io
import http.client
import json
import queue
import shlex
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import agent
import project_guidance as guidance


def test_local_discovery_groups_compose_and_preserves_original_arguments(monkeypatch):
    monkeypatch.setattr(Path, "is_dir", lambda path: True)
    monkeypatch.setattr(Path, "is_file", lambda path: True)
    monkeypatch.setattr(Path, "is_absolute", lambda path: True)
    commands = []
    directory = "/srv/my app"
    # On Windows, Path uses backslashes; fake Git accepts the platform representation.
    def run(command, timeout):
        commands.append(command)
        assert timeout <= 5
        if command[:2] == ["docker", "ps"]:
            return 0, "a" * 64 + "\n" + "b" * 64
        if command[:2] == ["docker", "inspect"]:
            assert '.Config.Env' not in command[5]
            return 0, '\n'.join(json.dumps([name, 'running', 'original', directory,
                '/srv/my app/compose.yaml,/srv/my app/production.yaml', '/srv/my app/prod.env'])
                for name in ['/original-api-1', '/original-db-1'])
        if 'rev-parse' in command:
            return 0, directory
        if 'config' in command:
            return 0, 'https://example.test/app.git'
        raise AssertionError(command)
    result = guidance.discover_local_projects(run)
    assert len(result['projects']) == 1
    project = result['projects'][0]
    assert len(project['containers']) == 2
    assert project['repo'] == 'https://example.test/app.git'
    assert project['git_directory'] == directory
    args = shlex.split(project['command'])
    assert args[:2] == ['docker', 'compose']
    assert args[args.index('--project-name') + 1] == 'original'
    assert args.count('--file') == 2
    assert '--env-file' in args
    assert args[-3:] == ['up', '-d', '--build']
    assert not any('up' in command or 'pull' in command or 'checkout' in command for command in commands)


def test_local_discovery_omits_credentials_and_does_not_guess_missing_config(monkeypatch):
    monkeypatch.setattr(Path, 'is_dir', lambda path: True)
    monkeypatch.setattr(Path, 'is_file', lambda path: False)
    def run(command, timeout):
        if command[:2] == ['docker', 'ps']:
            return 0, 'c' * 64
        if command[:2] == ['docker', 'inspect']:
            return 0, json.dumps(['/api', 'running', 'app', '/srv/app', '/missing.yaml', ''])
        if 'rev-parse' in command:
            return 0, '/srv/app'
        return 0, 'https://user:secret@example.test/app.git'
    result = guidance.discover_local_projects(run)
    assert result['projects'][0]['repo'] == ''
    assert result['projects'][0]['command'] == ''
    assert 'secret' not in json.dumps(result)


@pytest.mark.parametrize('code,output', [(1, 'permission denied'), (0, '')])
def test_local_discovery_unavailable_is_actionable(code, output):
    result = guidance.discover_local_projects(lambda *args, **kwargs: (code, output))
    assert result['projects'] == []
    assert '手动' in result['notice']


@pytest.fixture
def empty_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "PROJECTS", {})
    monkeypatch.setattr(agent, "DEFAULT_PROJECT_KEY", "")
    monkeypatch.setattr(agent, "PROJECTS_CONFIG_FILE", tmp_path / "projects.json")
    monkeypatch.setattr(agent, "PROJECT_CONFIG_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(agent, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(agent, "_state", {"history": [], "current_deploy": None})
    monkeypatch.setattr(agent, "_jobs", queue.Queue())
    monkeypatch.setattr(agent, "_system_status_payload", lambda: {})
    monkeypatch.setattr(agent, "_nginx_project_has_site", lambda project: False)
    monkeypatch.setattr(agent, "_log", lambda message: None)


def test_empty_install_add_first_delete_last_and_reload(empty_runtime, tmp_path):
    payload = agent._status_payload()
    assert payload["projects"] == []
    assert payload["default_project"] == ""
    assert payload["agent"]["project_count"] == 0
    assert payload["git"] == {"head": "", "short_head": "", "branch": ""}
    assert agent._project_for_manual({}) is None
    assert agent._project_for_webhook({}, {}) is None
    assert agent._run_git(["rev-parse", "HEAD"]) == ""
    assert agent._validate_projects_runtime_config() == []
    project = agent._save_project_transaction({"key": "first", "enabled": False, "workdir": str(tmp_path)})
    assert agent._status_payload()["agent"]["project_count"] == 1
    assert agent._project_for_manual({}) == project
    agent._delete_project_transaction("first")
    assert agent._status_payload()["projects"] == []
    assert json.loads(agent.PROJECTS_CONFIG_FILE.read_text(encoding="utf-8"))["projects"] == []
    agent._replace_projects({})
    assert agent.DEFAULT_PROJECT_KEY == ""
    assert list((tmp_path / "backups").glob("*.bak"))


@pytest.mark.parametrize("state", ["running", "queued", "certificate", "nginx"])
def test_last_project_still_protects_active_resources(empty_runtime, tmp_path, monkeypatch, state):
    agent._save_project_transaction({"key": "first", "enabled": False, "workdir": str(tmp_path)})
    if state == "running":
        agent._state["current_deploy"] = {"project_key": "first", "status": "running"}
    elif state == "queued":
        agent._jobs.put({"project_key": "first"})
    elif state == "certificate":
        (tmp_path / "certificates" / "first").mkdir(parents=True)
    else:
        monkeypatch.setattr(agent, "_nginx_project_has_site", lambda project: True)
    with pytest.raises((agent._ProjectRunningError, agent.certificates.CertificateError)):
        agent._delete_project_transaction("first")
    assert "first" in agent.PROJECTS


@pytest.mark.parametrize(("files", "expected", "entry"), [
    ({"requirements.txt": "fastapi", "main.py": "from fastapi import FastAPI\napp = FastAPI()"}, "python", "main:app"),
    ({"pyproject.toml": "", "app/main.py": "from fastapi import FastAPI\napi: FastAPI = FastAPI()"}, "python", "app.main:api"),
    ({"go.mod": "module example.test/api"}, "go", ""),
    ({"pom.xml": ""}, "java", ""),
    ({"build.gradle.kts": ""}, "java", ""),
    ({"compose.yaml": ""}, "docker", ""),
])
def test_manifest_detection(files, expected, entry):
    result = guidance.detect(files)
    assert result["candidates"][0]["template"] == expected
    assert result["candidates"][0]["entry"] == entry


def test_ambiguous_python_does_not_guess_or_execute(tmp_path):
    marker = tmp_path / "executed"
    source = f"from pathlib import Path\nPath({str(marker)!r}).touch()\nfrom fastapi import FastAPI\napp = FastAPI()"
    result = guidance.detect({"requirements.txt": "", "main.py": source, "app.py": "other = FastAPI()"})
    assert result["candidates"][0]["entry"] == ""
    assert result["warnings"]
    assert not marker.exists()
    mixed = guidance.detect({"compose.yml": "", "go.mod": "", "deploy.sh": ""})
    assert len(mixed["candidates"]) == 2
    assert mixed["existing_script"] == "deploy.sh"
    assert guidance.detect({})["candidates"] == []


@pytest.mark.parametrize("repo", ["file:///etc", "/srv/repo", "--upload-pack=evil", "ext::sh -c evil",
                                  "https://token@example.test/a.git", "https://u:p@example.test/a.git",
                                  "https://example.test/a.git?token=secret", "git@host:a.git\ncommand"])
def test_repository_rejects_unsafe_transports_and_embedded_credentials(repo):
    with pytest.raises(ValueError):
        guidance.validate_repository(repo, "main")


@pytest.mark.parametrize("repo", ["https://github.com/team/api.git", "git@gitee.com:team/api.git", "ssh://git@host.test:2222/team/api.git"])
def test_repository_accepts_supported_transports(repo):
    guidance.validate_repository(repo, "release/v1")


@pytest.mark.parametrize(("output", "code"), [
    ("Permission denied (publickey)", "repo_auth"),
    ("Host key verification failed", "host_key"),
    ("fatal: couldn't find remote ref release", "branch"),
    ("fatal: Not possible to fast-forward, aborting", "git_conflict"),
    ("Could not resolve host: git.example.test", "dns"),
    ("Failed to connect to server", "network"),
    ("OSError: Address already in use", "port"),
    ("No space left on device", "disk"),
    ("ModuleNotFoundError: No module named 'fastapi'", "dependency"),
    ("Cannot connect to the Docker daemon", "docker"),
    ("uvicorn: command not found", "command"),
    ("curl: (22) The requested URL returned error: 404", "health"),
    ("mysterious problem", "unknown"),
])
def test_failure_advice(output, code):
    assert guidance.diagnose(output)[0]["code"] == code
    assert guidance.diagnose(output, exit_code=0) == []


def test_advice_does_not_copy_secrets_and_timeout_is_explicit():
    result = guidance.diagnose("authentication failed https://u:super-secret@example.test/r", exit_code=124)
    assert result[0]["code"] == "timeout"
    assert "super-secret" not in json.dumps(result)


def test_preview_preserves_existing_script_and_bootstrap_does_not_overwrite(tmp_path, monkeypatch):
    script = tmp_path / "deploy.sh"
    script.write_text("#!/bin/sh\necho keep-user-script\n", encoding="utf-8")
    project = agent._project_from_form({"key": "api", "template": "docker", "workdir": str(tmp_path),
                                        "repo": "https://example.test/api.git", "script": str(script),
                                        "deploy_log_file": str(tmp_path / "deploy.log")})
    preview = agent._project_preview(project)
    assert preview["files"][0]["exists"]
    assert "keep-user-script" in preview["files"][0]["content"]
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(agent.shutil, "which", lambda command: command)
    monkeypatch.setattr(agent, "_run_bootstrap_command", lambda command, **kwargs: agent._bootstrap_result(kwargs["step"], True, "ok"))
    monkeypatch.setattr(agent, "_project_doctor", lambda project: {})
    monkeypatch.setattr(agent, "_preflight_payload", lambda project: {})
    assert agent._bootstrap_project(project)["ok"]
    assert script.read_text(encoding="utf-8") == "#!/bin/sh\necho keep-user-script\n"


def test_output_capture_is_bounded_and_attached_to_this_process(monkeypatch):
    from collections import deque
    from types import SimpleNamespace

    monkeypatch.setattr(agent, "_log", lambda message: None)
    proc = SimpleNamespace(stdout=io.StringIO(("x" * 2000 + "\n") * 100 + "Address already in use\n"),
                           _mini_deploy_diagnostic_tail=deque(maxlen=64))
    agent._read_process_output(proc, threading.Event())
    captured = proc._mini_deploy_diagnostic_tail
    assert len(captured) == 64
    assert all(len(line) <= 1024 for line in captured)
    assert guidance.diagnose("\n".join(captured))[0]["code"] == "port"


def test_java_preview_uses_gradle_artifacts_and_maven_wrapper(tmp_path):
    project = agent._project_from_form({"key": "java", "template": "java", "workdir": str(tmp_path)})
    text = agent._deploy_script_text(project)
    assert "jar_dir=build/libs" in text
    assert "bash ./mvnw" in text
    assert "${#jars[@]}" in text


def test_repository_inspection_reads_real_git_objects_without_checkout(tmp_path, monkeypatch):
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    (repo / "requirements.txt").write_text("fastapi\nuvicorn\n", encoding="utf-8")
    (repo / "main.py").write_text("raise RuntimeError('must not execute')\napp = FastAPI()", encoding="utf-8")
    (repo / "Dockerfile").write_text("FROM python:3.12\nEXPOSE 8000\nCMD python main.py\n", encoding="utf-8")
    (repo / ".env.example").write_text("DATABASE_URL=must-not-copy-secret\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.test",
                    "-c", "commit.gpgsign=false", "commit", "-m", "test"], check=True, capture_output=True)
    real_popen = subprocess.Popen
    clones = []

    def local_transport(command, **kwargs):
        if "ls-remote" in command:
            command = ["protocol.file.allow=always" if arg == "protocol.file.allow=never" else arg for arg in command]
            command[command.index("https://example.test/test.git")] = repo.as_uri()
        elif "clone" in command:
            assert "--no-checkout" in command
            assert "protocol.ext.allow=never" in command
            assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
            clones.append(Path(command[-1]))
            command = ["protocol.file.allow=always" if arg == "protocol.file.allow=never" else arg for arg in command]
            command[-2] = repo.as_uri()
        else:
            assert not (clones[-1] / "main.py").exists()
        return real_popen(command, **kwargs)

    monkeypatch.setattr(guidance.subprocess, "Popen", local_transport)
    result = guidance.inspect_repository("https://example.test/test.git", "main")
    assert result["ok"]
    assert next(item for item in result["candidates"] if item["template"] == "python")["entry"] == "main:app"
    assert result["docker"]["ports"] == [8000]
    assert result["docker"]["environment"][0]["name"] == "DATABASE_URL"
    assert "must-not-copy-secret" not in json.dumps(result)
    assert not clones[-1].exists()
    automatic = guidance.inspect_repository("https://example.test/test.git", "")
    assert automatic["ok"]
    assert automatic["branch"] == "main"
    assert "main" in automatic["branches"]
    missing = guidance.inspect_repository("https://example.test/test.git", "missing-branch")
    assert not missing["ok"]
    assert missing["diagnosis"][0]["code"] == "branch"


def test_inspection_limits_parallel_work():
    with guidance._inspection_lock:
        result = guidance.inspect_repository("https://example.test/test.git", "main")
    assert result["diagnosis"][0]["code"] == "busy"


def test_guidance_http_auth_csrf_and_empty_status(empty_runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "UI_PASSWORD_HASH", agent._hash_password("test-password-123"))
    monkeypatch.setattr(agent, "UI_SESSION_SECRET", "test-secret-0123456789abcdef0123456789abcdef")
    monkeypatch.setattr(agent, "_audit_event", lambda *args, **kwargs: None)
    calls = []

    def inspect(repo, branch, **kwargs):
        calls.append((repo, branch))
        assert "DEPLOY_UI_SESSION_SECRET" not in kwargs["env"]
        return {"ok": True, **guidance.detect({"go.mod": ""})}

    monkeypatch.setenv("DEPLOY_UI_SESSION_SECRET", "must-not-reach-git")
    monkeypatch.setattr(guidance, "inspect_repository", inspect)
    cookie_value = agent._make_session_cookie()
    cookie_headers = {"Cookie": f"{agent.COOKIE_NAME}={cookie_value}"}
    auth_headers = {**cookie_headers, "X-CSRF-Token": agent._csrf_token(cookie_value)}
    server = ThreadingHTTPServer(("127.0.0.1", 0), agent.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection(*server.server_address, timeout=5)

    def request(path, body, headers):
        connection.request("POST", path, body=json.dumps(body), headers={"Content-Type": "application/json", **headers})
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    try:
        discovery_calls = []
        monkeypatch.setattr(guidance, 'discover_local_projects', lambda run: discovery_calls.append(True) or {'projects': []})
        connection.request('GET', '/projects-config/discover')
        response = connection.getresponse()
        assert response.status == 401
        response.read()
        assert not discovery_calls
        connection.request('GET', '/projects-config/discover', headers=cookie_headers)
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())['projects'] == []
        assert discovery_calls == [True]
        body = {"project": {"repo": "https://example.test/api.git", "branch": "main"}}
        for path in ("/projects-config/inspect", "/projects-config/preview"):
            assert request(path, body, {})[0] == 401
            assert request(path, body, cookie_headers)[0] == 403
        assert calls == []
        status, result = request("/projects-config/inspect", body, auth_headers)
        assert status == 200 and result["candidates"][0]["template"] == "go"
        assert len(calls) == 1
        preview_body = {"project": {"key": "api", "workdir": str(tmp_path / "api"), "template": "docker", "script": "deploy.sh"}}
        status, result = request("/projects-config/preview", preview_body, auth_headers)
        assert status == 200 and "docker compose" in result["files"][0]["content"]
        assert not (tmp_path / "api").exists()
        assert not agent.PROJECTS_CONFIG_FILE.exists()
        assert request("/projects-config/preview", {"project": []}, auth_headers)[0] == 400
        connection.request("GET", "/status", headers=cookie_headers)
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["projects"] == []
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
