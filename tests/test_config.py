from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import agent


@pytest.mark.parametrize("value", [True, "1", "true", "YES", "on", "enabled"])
def test_bool_config_accepts_explicit_true_values(value: object) -> None:
    assert agent._bool_config(value)


@pytest.mark.parametrize("value", [False, "0", "false", "no", "disabled", "unexpected"])
def test_bool_config_rejects_false_and_unknown_values(value: object) -> None:
    assert not agent._bool_config(value, True)


@pytest.mark.parametrize("value", [None, "", "invalid", 0, -1])
def test_int_config_falls_back_for_non_positive_or_invalid_values(value: object) -> None:
    assert agent._int_config(value, 900) == 900


def test_project_form_normalizes_identifiers_and_domain() -> None:
    project = agent._project_from_form(
        {
            "key": "My API",
            "name": " Example API ",
            "repo": "git@github.com:Example/API.git",
            "workdir": "/srv/example-api",
            "service_name": "Example API Service",
            "service_port": "8080",
            "domain": "https://API.Example.com:443/health",
            "https": "yes",
            "webhook_secret": "0123456789abcdef0123456789abcdef",
        }
    )

    assert project.key == "my-api"
    assert project.name == "Example API"
    assert project.workdir == Path("/srv/example-api")
    assert project.service_name == "example-api-service"
    assert project.service_port == 8080
    assert project.app_domain == "api.example.com"
    assert project.app_https is True


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("example.com", True),
        ("api.example.com", True),
        ("localhost", False),
        ("bad_domain.example.com", False),
        ("-api.example.com", False),
    ],
)
def test_domain_validation(domain: str, expected: bool) -> None:
    assert agent._valid_domain(domain) is expected


def test_repository_matching_normalizes_git_and_https_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    project = agent._project_from_config(
        {
            "key": "api",
            "repo": "git@github.com:Example/API.git",
            "webhook_secret": "secret",
        },
        "api",
    )
    monkeypatch.setattr(agent, "PROJECTS", {project.key: project})

    assert agent._project_matches_repo(project, {"https://github.com/example/api.git"})
    assert not agent._project_matches_repo(project, {"https://github.com/example/other.git"})


def test_projects_config_defaults_to_agent_state_directory(tmp_path: Path) -> None:
    state_file = tmp_path / "data" / "state.json"
    app_home = tmp_path / "app"

    config_file, legacy_file = agent._projects_config_paths("  ", state_file, app_home)

    assert config_file == state_file.parent / "projects.json"
    assert legacy_file == app_home / "projects.json"


def test_explicit_projects_config_has_priority_and_disables_legacy_comparison(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_file = tmp_path / "data" / "state.json"
    app_home = tmp_path / "app"
    explicit_file = tmp_path / "custom" / "projects.json"
    legacy_file = app_home / "projects.json"
    explicit_file.parent.mkdir(parents=True)
    legacy_file.parent.mkdir(parents=True)
    explicit_file.write_text(
        json.dumps({"projects": [{"key": "explicit", "enabled": False}]}),
        encoding="utf-8",
    )
    legacy_file.write_text(
        json.dumps({"projects": [{"key": "stale-legacy", "enabled": False}]}),
        encoding="utf-8",
    )

    config_file, legacy_check = agent._projects_config_paths(
        f"  {explicit_file}  ",
        state_file,
        app_home,
    )
    monkeypatch.setattr(agent, "PROJECTS_CONFIG_FILE", config_file)
    monkeypatch.setattr(agent, "LEGACY_PROJECTS_CONFIG_FILE", legacy_check)

    assert config_file == explicit_file
    assert legacy_check is None
    assert list(agent._load_projects()) == ["explicit"]


def test_only_legacy_projects_config_requires_installer_migration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "data" / "projects.json"
    legacy_file = tmp_path / "app" / "projects.json"
    legacy_file.parent.mkdir(parents=True)
    legacy_file.write_text(
        json.dumps({"projects": [{"key": "legacy", "enabled": False}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(agent, "PROJECTS_CONFIG_FILE", config_file)
    monkeypatch.setattr(agent, "LEGACY_PROJECTS_CONFIG_FILE", legacy_file)

    with pytest.raises(RuntimeError, match="legacy projects config requires migration") as exc_info:
        agent._load_projects()

    assert str(config_file) in str(exc_info.value)
    assert str(legacy_file) in str(exc_info.value)
    assert not config_file.exists()


def test_different_canonical_and_legacy_projects_configs_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "data" / "projects.json"
    legacy_file = tmp_path / "app" / "projects.json"
    config_file.parent.mkdir(parents=True)
    legacy_file.parent.mkdir(parents=True)
    config_file.write_text(
        json.dumps({"projects": [{"key": "canonical", "enabled": False}]}),
        encoding="utf-8",
    )
    legacy_file.write_text(
        json.dumps({"projects": [{"key": "legacy", "enabled": False}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(agent, "PROJECTS_CONFIG_FILE", config_file)
    monkeypatch.setattr(agent, "LEGACY_PROJECTS_CONFIG_FILE", legacy_file)

    with pytest.raises(RuntimeError, match="projects config conflict") as exc_info:
        agent._load_projects()

    assert str(config_file) in str(exc_info.value)
    assert str(legacy_file) in str(exc_info.value)


def test_identical_canonical_and_legacy_projects_configs_use_canonical_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "data" / "projects.json"
    legacy_file = tmp_path / "app" / "projects.json"
    payload = json.dumps({"projects": [{"key": "canonical", "enabled": False}]})
    config_file.parent.mkdir(parents=True)
    legacy_file.parent.mkdir(parents=True)
    config_file.write_text(payload, encoding="utf-8")
    legacy_file.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(agent, "PROJECTS_CONFIG_FILE", config_file)
    monkeypatch.setattr(agent, "LEGACY_PROJECTS_CONFIG_FILE", legacy_file)

    projects = agent._load_projects()

    assert list(projects) == ["canonical"]


def test_missing_projects_configs_do_not_create_state_or_app_directories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "missing-data" / "projects.json"
    legacy_file = tmp_path / "missing-app" / "projects.json"
    monkeypatch.setattr(agent, "PROJECTS_CONFIG_FILE", config_file)
    monkeypatch.setattr(agent, "LEGACY_PROJECTS_CONFIG_FILE", legacy_file)
    monkeypatch.delenv("DEPLOY_PROJECT_ENABLED", raising=False)

    projects = agent._load_projects()

    assert list(projects) == ["default"]
    assert not config_file.parent.exists()
    assert not legacy_file.parent.exists()


def _run_validate_config_cli(config_file: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update({
        "DEPLOY_PROJECTS_FILE": str(config_file),
        "DEPLOY_UI_PASSWORD_HASH": "intentionally-invalid-for-this-command",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    environment.pop("DEPLOY_UI_SESSION_SECRET", None)
    return subprocess.run(
        [sys.executable, str(Path(agent.__file__).resolve()), "validate-config"],
        cwd=str(Path(agent.__file__).resolve().parent),
        env=environment,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


def test_validate_config_cli_accepts_valid_config_without_ui_credentials(tmp_path: Path) -> None:
    config_file = tmp_path / "projects.json"
    deploy_script = tmp_path / "deploy.sh"
    deploy_script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    deploy_script.chmod(0o700)
    config_file.write_text(
        json.dumps({
            "projects": [{
                "key": "api",
                "enabled": True,
                "webhook_secret": "0123456789abcdef0123456789abcdef",
                "script": str(deploy_script),
            }]
        }),
        encoding="utf-8",
    )
    original_payload = config_file.read_bytes()

    result = _run_validate_config_cli(config_file)

    assert result.returncode == 0, result.stderr
    assert "projects config OK:" in result.stdout
    assert "projects=1 enabled=1" in result.stdout
    assert config_file.read_bytes() == original_payload


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        ("{not-json", "projects config is invalid"),
        ('{"projects": []}', "projects must be a non-empty list"),
        (
            json.dumps({
                "projects": [{
                    "key": "api",
                    "enabled": True,
                    "webhook_secret": "too-short",
                }]
            }),
            "webhook secret missing, too short, or still a placeholder for projects: api",
        ),
    ],
)
def test_validate_config_cli_rejects_invalid_structure_and_weak_enabled_secrets(
    tmp_path: Path,
    payload: str,
    expected_error: str,
) -> None:
    config_file = tmp_path / "projects.json"
    config_file.write_text(payload, encoding="utf-8")

    result = _run_validate_config_cli(config_file)

    assert result.returncode != 0
    assert expected_error in result.stderr
    assert "projects config OK:" not in result.stdout


@pytest.mark.parametrize(
    ("missing_action", "expected_error"),
    [
        ("deploy", "deploy script not found"),
        ("rollback", "rollback script not found"),
    ],
)
def test_validate_config_cli_rejects_missing_enabled_project_scripts(
    tmp_path: Path,
    missing_action: str,
    expected_error: str,
) -> None:
    deploy_script = tmp_path / "deploy.sh"
    deploy_script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    deploy_script.chmod(0o700)
    missing_script = tmp_path / f"missing-{missing_action}.sh"
    project = {
        "key": "api",
        "enabled": True,
        "webhook_secret": "0123456789abcdef0123456789abcdef",
        "script": str(missing_script if missing_action == "deploy" else deploy_script),
    }
    if missing_action == "rollback":
        project["rollback_script"] = str(missing_script)
    config_file = tmp_path / "projects.json"
    config_file.write_text(json.dumps({"projects": [project]}), encoding="utf-8")

    result = _run_validate_config_cli(config_file)

    assert result.returncode != 0
    assert expected_error in result.stderr
    assert str(missing_script) in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="executable mode validation targets Linux servers")
def test_validate_config_cli_rejects_non_executable_deploy_script(tmp_path: Path) -> None:
    deploy_script = tmp_path / "deploy.sh"
    deploy_script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    deploy_script.chmod(0o600)
    config_file = tmp_path / "projects.json"
    config_file.write_text(
        json.dumps({
            "projects": [{
                "key": "api",
                "enabled": True,
                "webhook_secret": "0123456789abcdef0123456789abcdef",
                "script": str(deploy_script),
            }]
        }),
        encoding="utf-8",
    )

    result = _run_validate_config_cli(config_file)

    assert result.returncode != 0
    assert "deploy script is not executable" in result.stderr
    assert str(deploy_script) in result.stderr


@pytest.mark.parametrize(
    ("non_executable_action", "expected_error"),
    [
        ("deploy", "deploy script is not executable"),
        ("rollback", "rollback script is not executable"),
    ],
)
def test_project_runtime_validation_rejects_non_executable_scripts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    non_executable_action: str,
    expected_error: str,
) -> None:
    deploy_script = tmp_path / "deploy.sh"
    rollback_script = tmp_path / "rollback.sh"
    deploy_script.write_text("deploy\n", encoding="utf-8")
    rollback_script.write_text("rollback\n", encoding="utf-8")
    project = agent._project_from_config(
        {
            "key": "api",
            "enabled": True,
            "webhook_secret": "0123456789abcdef0123456789abcdef",
            "script": str(deploy_script),
            "rollback_script": str(rollback_script) if non_executable_action == "rollback" else "",
        },
        "api",
    )
    non_executable_path = rollback_script if non_executable_action == "rollback" else deploy_script
    monkeypatch.setattr(agent, "_RUNTIME_CONFIG_ERROR", None)
    monkeypatch.setattr(agent, "PROJECTS", {project.key: project})
    monkeypatch.setattr(agent, "DEFAULT_PROJECT_KEY", project.key)
    monkeypatch.setattr(agent, "_script_is_executable", lambda path: path != non_executable_path)

    with pytest.raises(SystemExit, match=expected_error):
        agent._validate_projects_runtime_config()


def test_validate_config_command_does_not_validate_auth_or_probe_maintenance(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = agent._project_from_config({"key": "disabled", "enabled": False}, "disabled")
    monkeypatch.setattr(agent, "_RUNTIME_CONFIG_ERROR", None)
    monkeypatch.setattr(agent, "PROJECTS", {project.key: project})
    monkeypatch.setattr(agent, "DEFAULT_PROJECT_KEY", project.key)
    monkeypatch.setattr(agent.sys, "argv", ["agent.py", "validate-config"])

    def unexpected_call(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("validate-config must not perform startup-only checks")

    monkeypatch.setattr(agent, "_validate_ui_auth_config", unexpected_call)
    monkeypatch.setattr(agent, "_probe_maintenance_lock_for_startup", unexpected_call)
    monkeypatch.setattr(agent, "_atomic_write_private_text", unexpected_call)
    monkeypatch.setattr(agent, "_append_private_text", unexpected_call)

    agent.main()

    assert "projects config OK:" in capsys.readouterr().out


def test_projects_config_reader_rejects_path_replacement_after_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "projects.json"
    config_file.write_text('{"projects": []}\n', encoding="utf-8")
    original_lstat = Path.lstat
    target_lstat_calls = 0

    def changed_inode_on_final_lstat(path: Path):
        nonlocal target_lstat_calls
        result = original_lstat(path)
        if path == config_file:
            target_lstat_calls += 1
            if target_lstat_calls == 2:
                values = list(result)
                values[1] += 1
                return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, "lstat", changed_inode_on_final_lstat)

    with pytest.raises(RuntimeError, match="path changed while it was being read"):
        agent._read_regular_projects_config(config_file, "canonical")

    assert target_lstat_calls == 2


def test_load_projects_parses_supported_aliases(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_file = tmp_path / "projects.json"
    config_file.write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "key": "worker",
                        "type": "python",
                        "repository": "https://github.com/example/worker.git",
                        "project_dir": "/srv/worker",
                        "log_file": "/var/log/worker.log",
                        "secret": "worker-secret",
                        "enabled": "yes",
                        "manual_deploy_enabled": "no",
                        "timeout_seconds": "120",
                        "port": "8081",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(agent, "PROJECTS_CONFIG_FILE", config_file)
    monkeypatch.setattr(agent, "LEGACY_PROJECTS_CONFIG_FILE", None)

    projects = agent._load_projects()

    assert list(projects) == ["worker"]
    project = projects["worker"]
    assert project.template == "python"
    assert project.repo == "https://github.com/example/worker.git"
    assert project.workdir == Path("/srv/worker")
    assert project.deploy_log_file == Path("/var/log/worker.log")
    assert project.webhook_secret == "worker-secret"
    assert project.enabled is True
    assert project.manual_deploy_enabled is False
    assert project.timeout_seconds == 120
    assert project.service_port == 8081


@pytest.mark.parametrize("content", ["{not-json", '{"projects": []}', '{"projects": [null]}'])
def test_load_projects_fails_closed_for_invalid_or_empty_existing_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    content: str,
) -> None:
    config_file = tmp_path / "projects.json"
    config_file.write_text(content, encoding="utf-8")
    monkeypatch.setattr(agent, "PROJECTS_CONFIG_FILE", config_file)
    monkeypatch.setattr(agent, "LEGACY_PROJECTS_CONFIG_FILE", None)

    with pytest.raises(RuntimeError, match="projects config is invalid"):
        agent._load_projects()


def test_project_config_does_not_fall_back_to_global_webhook_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "WEBHOOK_SECRET", "global-secret-0123456789abcdef")

    project = agent._project_from_config({"key": "api", "enabled": False}, "api")

    assert project.webhook_secret == ""


def test_project_form_generates_for_empty_or_placeholder_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent.secrets, "token_hex", lambda _: "ab" * 32)

    empty = agent._project_from_form({"key": "empty", "webhook_secret": ""})
    placeholder = agent._project_from_form({"key": "placeholder", "webhook_secret": "replace-with-token"})

    assert empty.webhook_secret == "ab" * 32
    assert placeholder.webhook_secret == "ab" * 32


def test_project_form_rejects_short_non_placeholder_secret() -> None:
    with pytest.raises(ValueError, match="至少需要 32 位"):
        agent._project_from_form({"key": "api", "webhook_secret": "short-custom-secret"})


@pytest.mark.parametrize("secret", ["", "x" * 31, "replace-with-a-long-random-token"])
def test_agent_startup_rejects_enabled_projects_with_weak_secrets(
    monkeypatch: pytest.MonkeyPatch,
    secret: str,
) -> None:
    project = agent._project_from_config(
        {"key": "api", "webhook_secret": secret, "enabled": True},
        "api",
    )
    monkeypatch.setattr(agent, "PROJECTS", {"api": project})
    monkeypatch.setattr(agent, "DEFAULT_PROJECT_KEY", "api")
    monkeypatch.setattr(agent.sys, "argv", ["agent.py"])
    monkeypatch.setattr(agent, "UI_PASSWORD_HASH", "")
    monkeypatch.setattr(agent, "UI_SESSION_SECRET", "")

    with pytest.raises(SystemExit, match="webhook secret missing"):
        agent.main()


def test_missing_projects_file_creates_disabled_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(agent, "PROJECTS_CONFIG_FILE", tmp_path / "missing.json")
    monkeypatch.setattr(agent, "LEGACY_PROJECTS_CONFIG_FILE", None)
    monkeypatch.delenv("DEPLOY_PROJECT_ENABLED", raising=False)

    projects = agent._load_projects()

    assert list(projects) == ["default"]
    assert projects["default"].enabled is False


def test_empty_path_environment_value_uses_safe_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TEST_EMPTY_PATH", "  ")

    assert agent._path_from_env("TEST_EMPTY_PATH", tmp_path) == tmp_path


def test_project_config_backups_are_unique_and_private(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_file = tmp_path / "projects.json"
    backup_dir = tmp_path / "state" / "backups"
    config_file.write_text('{"projects": []}\n', encoding="utf-8")
    monkeypatch.setattr(agent, "PROJECTS_CONFIG_FILE", config_file)
    monkeypatch.setattr(agent, "PROJECT_CONFIG_BACKUP_DIR", backup_dir)
    monkeypatch.setattr(agent, "PROJECT_CONFIG_BACKUP_LIMIT", 5)

    first = Path(agent._backup_projects_config())
    second = Path(agent._backup_projects_config())

    assert first != second
    assert first.read_text(encoding="utf-8") == config_file.read_text(encoding="utf-8")
    assert second.read_text(encoding="utf-8") == config_file.read_text(encoding="utf-8")
    if agent.os.name != "nt":
        assert backup_dir.stat().st_mode & 0o777 == 0o700
        assert first.stat().st_mode & 0o777 == 0o600
        assert second.stat().st_mode & 0o777 == 0o600


def test_private_atomic_write_leaves_no_fixed_or_stale_temp_file(tmp_path: Path) -> None:
    destination = tmp_path / "mini-deploy-agent.env"

    agent._atomic_write_private_text(destination, "DEPLOY_UI_SESSION_SECRET=test-only\n")

    assert destination.read_text(encoding="utf-8") == "DEPLOY_UI_SESSION_SECRET=test-only\n"
    assert list(tmp_path.glob(f".{destination.name}.*.tmp")) == []
    assert not destination.with_suffix(".tmp").exists()
    if agent.os.name != "nt":
        assert destination.stat().st_mode & 0o777 == 0o600


def test_private_atomic_write_failure_preserves_target_and_cleans_temp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "mini-deploy-agent.env"
    destination.write_text("old\n", encoding="utf-8")

    def fail_replace(source: object, target: object) -> None:
        del source, target
        raise OSError("replace failed")

    monkeypatch.setattr(agent.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        agent._atomic_write_private_text(destination, "new\n")

    assert destination.read_text(encoding="utf-8") == "old\n"
    assert list(tmp_path.glob(f".{destination.name}.*.tmp")) == []


def test_private_append_tightens_existing_file_permissions(tmp_path: Path) -> None:
    destination = tmp_path / "audit.jsonl"
    destination.write_text("first\n", encoding="utf-8")
    agent.os.chmod(destination, 0o644)

    agent._append_private_text(destination, "second\n")

    assert destination.read_text(encoding="utf-8") == "first\nsecond\n"
    if agent.os.name != "nt":
        assert destination.stat().st_mode & 0o777 == 0o600


def test_deploy_subprocess_environment_excludes_control_plane_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name in agent._DEPLOY_CONTROL_SECRET_ENV_NAMES:
        monkeypatch.setenv(name, f"secret-{name.lower()}")
    monkeypatch.setenv("BUSINESS_DEPLOY_SETTING", "keep-this-value")
    project = agent._project_from_config(
        {
            "key": "api",
            "repo": "git@example.test:team/api.git",
            "workdir": str(tmp_path),
            "script": str(tmp_path / "deploy.sh"),
            "webhook_secret": "project-secret",
            "service_port": 8080,
        },
        "api",
    )

    environment = agent._deploy_subprocess_environment(
        project,
        "deploy",
        tmp_path / "deploy.sh",
        {"after": "abcdef", "source": "webhook"},
    )

    assert not (agent._DEPLOY_CONTROL_SECRET_ENV_NAMES & environment.keys())
    assert environment["BUSINESS_DEPLOY_SETTING"] == "keep-this-value"
    assert environment["DEPLOY_PROJECT_KEY"] == "api"
    assert environment["DEPLOY_AFTER"] == "abcdef"


def test_project_config_transactions_prevent_concurrent_secret_reversion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    initial = agent._project_from_config(
        {
            "key": "api",
            "name": "Initial",
            "workdir": str(tmp_path),
            "script": str(tmp_path / "deploy.sh"),
            "webhook_secret": "01" * 32,
            "enabled": False,
        },
        "api",
    )
    monkeypatch.setattr(agent, "PROJECTS", {"api": initial})
    monkeypatch.setattr(agent, "DEFAULT_PROJECT_KEY", "api")
    monkeypatch.setattr(agent.secrets, "token_hex", lambda _: "cd" * 32)

    first_write_entered = threading.Event()
    allow_first_write = threading.Event()
    second_write_entered = threading.Event()
    write_count = 0
    write_count_lock = threading.Lock()

    def fake_write(projects, notifications=None) -> None:
        del projects, notifications
        nonlocal write_count
        with write_count_lock:
            write_count += 1
            current_write = write_count
        if current_write == 1:
            first_write_entered.set()
            assert allow_first_write.wait(timeout=2)
        elif current_write == 2:
            second_write_entered.set()

    monkeypatch.setattr(agent, "_write_projects_config", fake_write)
    errors: list[BaseException] = []

    def save_name() -> None:
        try:
            agent._save_project_transaction({"key": "api", "name": "Updated"}, "api")
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)

    def rotate_secret() -> None:
        try:
            agent._reset_project_secret_transaction("api")
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)

    first = threading.Thread(target=save_name)
    second = threading.Thread(target=rotate_secret)
    first.start()
    assert first_write_entered.wait(timeout=2)
    second.start()
    assert not second_write_entered.wait(timeout=0.1)
    allow_first_write.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not errors
    assert not first.is_alive()
    assert not second.is_alive()
    assert second_write_entered.is_set()
    assert agent.PROJECTS["api"].name == "Updated"
    assert agent.PROJECTS["api"].webhook_secret == "cd" * 32


def test_state_writes_cannot_persist_an_older_snapshot_after_a_newer_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent, "_state", {"version": "initial"})
    monkeypatch.setattr(agent, "_state_write_lock", threading.Lock())

    first_write_entered = threading.Event()
    allow_first_write = threading.Event()
    persisted_versions: list[str] = []
    call_count = 0
    call_count_lock = threading.Lock()

    def fake_atomic_write(path: Path, text: str) -> None:
        del path
        nonlocal call_count
        with call_count_lock:
            call_count += 1
            current_call = call_count
        if current_call == 1:
            first_write_entered.set()
            assert allow_first_write.wait(timeout=2)
        persisted_versions.append(json.loads(text)["version"])

    monkeypatch.setattr(agent, "_atomic_write_private_text", fake_atomic_write)

    older = threading.Thread(target=lambda: agent._update_state(version="old"))
    newer = threading.Thread(target=lambda: agent._update_state(version="new"))
    older.start()
    assert first_write_entered.wait(timeout=2)
    newer.start()
    allow_first_write.set()
    older.join(timeout=2)
    newer.join(timeout=2)

    assert not older.is_alive()
    assert not newer.is_alive()
    assert persisted_versions == ["old", "new"]
