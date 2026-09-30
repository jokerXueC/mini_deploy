from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import agent
import project_guidance as guidance


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "STATE_FILE", tmp_path / "data" / "state.json")
    monkeypatch.setattr(agent.shutil, "which", lambda command: command)
    monkeypatch.setattr(agent, "_project_doctor", lambda project: {})
    monkeypatch.setattr(agent, "_preflight_payload", lambda project: {})


def project_form(tmp_path, method="commands", situation="existing"):
    return dict(key="api", repo="https://github.com/team/api.git", branch="main", workdir=str(tmp_path / "api"),
                deploy_log_file=str(tmp_path / "logs" / "deploy.log"), trigger_mode="manual",
                deployment_plan=dict(situation=situation, method=method, build="echo build", restart="echo restart"))


@pytest.mark.parametrize(("repo", "provider", "expected"), [
    ("https://github.com/team/api.git", "auto", "github"),
    ("git@gitee.com:team/api.git", "auto", "gitee"),
    ("ssh://git@gitlab.com/team/api.git", "auto", "gitlab"),
    ("https://git.example.test/team/api.git", "auto", "generic"),
    ("https://git.example.test/team/api.git", "gitea", "gitea"),
    ("https://git.example.test/team/api.git", "gitlab", "gitlab"),
])
def test_provider_guides_without_host_guessing(repo, provider, expected):
    info = guidance.provider_info(repo, provider)
    assert info["type"] == expected
    assert info["keys"] and info["hooks"]


def test_repository_identity_normalizes_transports_not_nonstandard_ports():
    assert guidance.repository_identity("git@github.com:team/api.git") == guidance.repository_identity("https://github.com/team/api/")
    assert guidance.repository_identity("ssh://git@host:2222/team/api.git") != guidance.repository_identity("ssh://git@host:2223/team/api.git")


@pytest.mark.parametrize("plan", ["bad", {"situation": "other", "method": "commands"},
    {"situation": [], "method": "commands"}, {"situation": "new", "method": []},
    {"situation": "new", "method": "poll"}, {"situation": "new", "method": "commands"},
    {"situation": "new", "method": "commands", "restart": " \n "},
    {"situation": "new", "method": "commands", "restart": "x\0y"},
    {"situation": "new", "method": "commands", "restart": "x" * 4097}])
def test_plan_requires_explicit_supported_steps(plan):
    with pytest.raises(ValueError):
        guidance.deployment_plan(plan)


def test_commands_accept_crlf_as_normalized_newlines():
    plan = guidance.deployment_plan(dict(situation="new", method="commands", restart="echo one\r\necho two"))
    assert plan["restart"] == "echo one\necho two"


def test_unknown_template_cannot_generate_fake_deployment(runtime, tmp_path):
    with pytest.raises(ValueError, match="占位"):
        agent._project_from_form({**project_form(tmp_path, method="template", situation="new"), "template": "custom"})


def test_plan_roundtrip_and_hidden_entries_do_not_override_commands(runtime, tmp_path):
    raw = project_form(tmp_path)
    raw.update(template="python", entry_kind="fastapi", entry_path="../invalid.py", docker_config={"mode": "invalid"}, repository_provider="gitea")
    project = agent._project_from_form(raw)
    restored = agent._project_from_config(agent._project_to_config(project), "api")
    assert restored.deployment_plan == project.deployment_plan
    assert restored.trigger_mode == "manual"
    assert restored.repository_provider == "gitea"
    assert restored.template == "custom"
    assert not restored.entry_kind and not restored.docker_config
    with pytest.raises(ValueError, match="触发"):
        agent._project_from_form({**raw, "trigger_mode": "poll"})


def make_repo(path):
    path.mkdir()
    for args in (["init", "-b", "main"], ["remote", "add", "origin", "git@github.com:team/api.git"]):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    (path / "main.py").write_text("print('unchanged')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=Test", "-c", "user.email=test@example.test",
                    "-c", "commit.gpgsign=false", "commit", "-m", "initial"], check=True, capture_output=True)


def test_existing_adoption_does_not_mutate_git_or_execute_steps(runtime, tmp_path, monkeypatch):
    raw = project_form(tmp_path)
    path = Path(raw["workdir"])
    make_repo(path)
    marker = tmp_path / "never-executed"
    raw["deployment_plan"]["restart"] = f"touch '{marker.as_posix()}'"
    project = agent._project_from_form(raw)
    before = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"])
    monkeypatch.setattr(agent, "_run_bootstrap_command", lambda *args, **kwargs: pytest.fail("No remote access or service changes during adoption"))
    preview = agent._project_preview(project)
    assert "不拉取" in preview["plan_steps"][0]
    assert not Path(project.script).exists()
    assert agent._bootstrap_project(project, write_service=True)["ok"]
    assert subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"]) == before
    assert (path / "main.py").read_text(encoding="utf-8") == "print('unchanged')\n"
    assert not marker.exists()
    assert "mini-deploy-managed: command-plan-v1" in Path(project.script).read_text(encoding="utf-8")
    assert "git pull --ff-only" in Path(project.script).read_text(encoding="utf-8")
    assert not (path / "deploy.sh").exists()


def test_existing_wrong_origin_and_missing_directory_block(runtime, tmp_path):
    project = agent._project_from_form(project_form(tmp_path))
    assert not agent._bootstrap_project(project)["ok"]
    assert not project.workdir.exists()
    make_repo(project.workdir)
    wrong = agent._project_from_form({**project_form(tmp_path), "repo": "https://github.com/other/api.git"})
    result = agent._bootstrap_project(wrong)
    assert not result["ok"]
    assert "不一致" in result["results"][0]["detail"]
    assert not Path(wrong.script).exists()
    subdirectory = project.workdir / "src"
    subdirectory.mkdir()
    nested = agent._project_from_form({**project_form(tmp_path), "workdir": str(subdirectory)})
    assert not agent._bootstrap_project(nested)["ok"]


def test_new_preparation_wont_pull_from_different_existing_origin(runtime, tmp_path, monkeypatch):
    raw = project_form(tmp_path, situation="new")
    make_repo(Path(raw["workdir"]))
    raw["repo"] = "https://github.com/other/api.git"
    monkeypatch.setattr(agent, "_run_bootstrap_command", lambda *args, **kwargs: pytest.fail("Wrong origin must stop before mutating Git"))
    project = agent._project_from_form(raw)
    assert not agent._bootstrap_project(project)["ok"]
    assert not Path(project.script).exists()


def test_existing_script_is_preserved_and_missing_script_is_not_generated(runtime, tmp_path):
    raw = project_form(tmp_path, method="script")
    make_repo(Path(raw["workdir"]))
    raw["script"] = "custom-deploy.sh"
    project = agent._project_from_form(raw)
    path = agent._project_script_path(project)
    assert "占位" in agent._project_preview(project)["files"][0]["content"]
    assert not agent._bootstrap_project(project)["ok"]
    assert not path.exists()
    path.write_text("#!/bin/sh\necho preserved\n", encoding="utf-8")
    before = path.stat().st_mode
    assert agent._bootstrap_project(project)["ok"]
    assert path.read_text(encoding="utf-8") == "#!/bin/sh\necho preserved\n"
    assert path.stat().st_mode == before


def test_existing_templates_require_compose_and_unsure_cannot_prepare(runtime, tmp_path):
    raw = project_form(tmp_path, method="template")
    for template, docker in [("python", {}), ("docker", {"mode": "dockerfile", "file": "Dockerfile", "container_port": 8000, "published_port": 8080})]:
        with pytest.raises(ValueError, match="已有服务"):
            agent._project_from_form({**raw, "template": template, "docker_config": docker})
    project = agent._project_from_form({**raw, "template": "docker", "docker_config": {"mode": "compose", "file": "compose.yaml"}})
    assert project.docker_config["mode"] == "compose"
    unsure = agent._project_from_form(project_form(tmp_path, situation="unsure"))
    result = agent._bootstrap_project(unsure)
    assert not result["ok"]
    assert "不确定" in result["results"][0]["detail"]


def test_managed_steps_refresh_without_overwriting_user_script(runtime, tmp_path):
    raw = project_form(tmp_path)
    project = agent._project_from_form(raw)
    agent._prepare_command_script(project)
    updated = agent._project_from_form({**raw, "deployment_plan": {**raw["deployment_plan"], "restart": "echo updated"}})
    agent._prepare_command_script(updated)
    assert "echo updated" in Path(updated.script).read_text(encoding="utf-8")
    Path(updated.script).write_text("#!/bin/sh\necho user-owned\n", encoding="utf-8")
    with pytest.raises(ValueError, match="不会覆盖"):
        agent._prepare_command_script(updated)
    assert "user-owned" in Path(updated.script).read_text(encoding="utf-8")
