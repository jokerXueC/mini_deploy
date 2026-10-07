from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import agent


@pytest.fixture(autouse=True)
def isolated_site_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(agent, "SITES_CONFIG_FILE", tmp_path / "sites.json")
    monkeypatch.setattr(agent, "PROJECT_CONFIG_BACKUP_DIR", tmp_path / "backups")


@pytest.mark.parametrize("value", [True, "1", "true", "YES", "on", "enabled"])
def test_bool_config_accepts_explicit_true_values(value: object) -> None:
    assert agent._bool_config(value)


@pytest.mark.parametrize("value", [False, "0", "false", "no", "disabled", "unexpected"])
def test_bool_config_rejects_false_and_unknown_values(value: object) -> None:
    assert not agent._bool_config(value, True)


@pytest.mark.parametrize("value", [None, "", "invalid", 0, -1])
def test_int_config_falls_back_for_non_positive_or_invalid_values(value: object) -> None:
    assert agent._int_config(value, 900) == 900


def test_site_config_normalizes_identifiers_and_preserves_monitoring_fields() -> None:
    site = agent._project_from_config(
        {
            "key": "My API",
            "name": "Example API",
            "health_url": "https://api.example.com/health",
            "service_port": "8080",
            "domain": "api.example.com",
            "https": "yes",
        },
        "fallback",
    )

    assert isinstance(site, agent.Site)
    assert site.key == "my-api"
    assert site.name == "Example API"
    assert site.health_url == "https://api.example.com/health"
    assert site.service_port == 8080
    assert site.app_domain == "api.example.com"
    assert site.app_https is True
    assert agent._normalize_domain("https://API.Example.com:443/health") == "api.example.com"


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

    assert projects == {}
    assert not config_file.parent.exists()
    assert not legacy_file.parent.exists()


def _run_validate_config_cli(config_file: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update({
        "DEPLOY_PROJECTS_FILE": str(config_file),
        "DEPLOY_AGENT_STATE_FILE": str(config_file.parent / "state.json"),
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


def test_validate_config_cli_accepts_legacy_sites_without_deployment_requirements(tmp_path: Path) -> None:
    config_file = tmp_path / "projects.json"
    config_file.write_text(
        json.dumps({
            "projects": [{
                "key": "api",
                "enabled": True,
                "health_url": "http://127.0.0.1:8080/health",
                "webhook_secret": "obsolete-short-secret",
                "script": str(tmp_path / "missing-deploy.sh"),
                "rollback_script": str(tmp_path / "missing-rollback.sh"),
            }]
        }),
        encoding="utf-8",
    )
    original_payload = config_file.read_bytes()

    result = _run_validate_config_cli(config_file)

    assert result.returncode == 0, result.stderr
    assert "sites config OK:" in result.stdout
    assert "sites=1 enabled=1" in result.stdout
    assert config_file.read_bytes() == original_payload
    assert not (tmp_path / "sites.json").exists()


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        ("{not-json", "sites config is invalid"),
        ('{"projects": "invalid"}', "sites must be a list"),
        ('{"sites": [null]}', "site at index 0 must be an object"),
        ('{"sites": [{"key": "api"}, {"key": "api"}]}', "duplicate site key"),
    ],
)
def test_validate_config_cli_rejects_invalid_structure(
    tmp_path: Path,
    payload: str,
    expected_error: str,
) -> None:
    config_file = tmp_path / "projects.json"
    config_file.write_text(payload, encoding="utf-8")

    result = _run_validate_config_cli(config_file)

    assert result.returncode != 0
    assert expected_error in result.stderr
    assert "sites config OK:" not in result.stdout


def test_validate_config_command_does_not_validate_auth_or_probe_maintenance(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = agent._project_from_config({"key": "disabled", "enabled": False}, "disabled")
    monkeypatch.setattr(agent, "_RUNTIME_CONFIG_ERROR", None)
    monkeypatch.setattr(agent, "PROJECTS", {project.key: project})
    monkeypatch.setattr(agent.sys, "argv", ["agent.py", "validate-config"])

    def unexpected_call(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("validate-config must not perform startup-only checks")

    monkeypatch.setattr(agent, "_validate_ui_auth_config", unexpected_call)
    monkeypatch.setattr(agent, "_probe_maintenance_lock_for_startup", unexpected_call)
    monkeypatch.setattr(agent, "_atomic_write_private_text", unexpected_call)
    monkeypatch.setattr(agent, "_append_private_text", unexpected_call)

    agent.main()

    assert "sites config OK:" in capsys.readouterr().out


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

    original_payload = config_file.read_bytes()
    projects = agent._load_projects()

    assert list(projects) == ["worker"]
    project = projects["worker"]
    assert isinstance(project, agent.Site)
    assert project.enabled is True
    assert project.service_port == 8081
    assert set(vars(project)) == {
        "key", "name", "health_url", "enabled", "service_port", "app_domain", "app_https",
    }
    assert config_file.read_bytes() == original_payload


@pytest.mark.parametrize("content", ["{not-json", '{"projects": [null]}'])
def test_load_projects_fails_closed_for_invalid_existing_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    content: str,
) -> None:
    config_file = tmp_path / "projects.json"
    config_file.write_text(content, encoding="utf-8")
    monkeypatch.setattr(agent, "PROJECTS_CONFIG_FILE", config_file)
    monkeypatch.setattr(agent, "LEGACY_PROJECTS_CONFIG_FILE", None)

    with pytest.raises(RuntimeError, match="sites config is invalid"):
        agent._load_projects()


def test_load_projects_accepts_empty_existing_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "projects.json"
    config_file.write_text('{"projects": []}', encoding="utf-8")
    monkeypatch.setattr(agent, "PROJECTS_CONFIG_FILE", config_file)
    monkeypatch.setattr(agent, "LEGACY_PROJECTS_CONFIG_FILE", None)
    assert agent._load_projects() == {}


def test_sites_config_takes_priority_over_conflicting_legacy_files(monkeypatch, tmp_path):
    canonical = tmp_path / "projects.json"
    legacy = tmp_path / "legacy-projects.json"
    canonical.write_text('{"projects": [{"key": "old"}]}', encoding="utf-8")
    legacy.write_text("{broken-legacy", encoding="utf-8")
    monkeypatch.setattr(agent, "PROJECTS_CONFIG_FILE", canonical)
    monkeypatch.setattr(agent, "LEGACY_PROJECTS_CONFIG_FILE", legacy)
    agent.SITES_CONFIG_FILE.write_text('{"sites": [{"key": "current"}]}', encoding="utf-8")

    assert list(agent._load_projects()) == ["current"]
    agent.SITES_CONFIG_FILE.write_text('{"sites": []}', encoding="utf-8")
    assert agent._load_projects() == {}


@pytest.mark.parametrize("payload", ["{broken", '{"sites": "invalid"}'])
def test_invalid_sites_config_does_not_fall_back_to_legacy(monkeypatch, tmp_path, payload):
    canonical = tmp_path / "projects.json"
    canonical.write_text('{"projects": [{"key": "old"}]}', encoding="utf-8")
    monkeypatch.setattr(agent, "PROJECTS_CONFIG_FILE", canonical)
    monkeypatch.setattr(agent, "LEGACY_PROJECTS_CONFIG_FILE", None)
    agent.SITES_CONFIG_FILE.write_text(payload, encoding="utf-8")

    with pytest.raises(RuntimeError, match="sites config is invalid"):
        agent._load_projects()


def test_config_write_migrates_sites_without_changing_legacy_config(monkeypatch, tmp_path):
    canonical = tmp_path / "projects.json"
    original = b'{"projects":[{"key":"api","repo":"old-repo","webhook_secret":"old-secret"}]}\n'
    canonical.write_bytes(original)
    monkeypatch.setattr(agent, "PROJECTS_CONFIG_FILE", canonical)
    monkeypatch.setattr(agent, "LEGACY_PROJECTS_CONFIG_FILE", None)
    projects = agent._load_projects()

    agent._write_projects_config(projects, notifications={})

    assert canonical.read_bytes() == original
    payload = json.loads(agent.SITES_CONFIG_FILE.read_text(encoding="utf-8"))
    assert payload["sites"] == [{
        "key": "api", "name": "api", "health_url": "", "enabled": True,
        "service_port": 8000, "app_domain": "", "app_https": False,
    }]
    assert "projects" not in payload
    assert agent._load_projects() == projects


@pytest.mark.parametrize("existing_monitoring_state", [False, True])
def test_monitoring_state_preserves_only_metrics_and_never_writes_legacy(
    monkeypatch, tmp_path, existing_monitoring_state,
):
    legacy = tmp_path / "state.json"
    monitoring = tmp_path / "monitoring-state.json"
    legacy.write_text(json.dumps({
        "system_metrics": [{"version": "legacy"}],
        "running": True, "deploy_history": [{"secret": "old-secret"}],
    }), encoding="utf-8")
    original = legacy.read_bytes()
    if existing_monitoring_state:
        monitoring.write_text(json.dumps({"system_metrics": [{"version": "current"}]}), encoding="utf-8")
    monkeypatch.setattr(agent, "STATE_FILE", legacy)
    monkeypatch.setattr(agent, "MONITORING_STATE_FILE", monitoring)
    monkeypatch.setattr(agent, "_state", {})

    agent._read_state()

    expected = [{"version": "current" if existing_monitoring_state else "legacy"}]
    assert agent._state == {"system_metrics": expected}
    agent._update_state(running=True, system_metrics=[{"version": "updated"}])
    assert json.loads(monitoring.read_text(encoding="utf-8")) == {
        "system_metrics": [{"version": "updated"}],
    }
    assert legacy.read_bytes() == original


def test_missing_projects_file_starts_with_no_sites(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(agent, "PROJECTS_CONFIG_FILE", tmp_path / "missing.json")
    monkeypatch.setattr(agent, "LEGACY_PROJECTS_CONFIG_FILE", None)

    assert agent._load_projects() == {}


def test_empty_path_environment_value_uses_safe_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TEST_EMPTY_PATH", "  ")

    assert agent._path_from_env("TEST_EMPTY_PATH", tmp_path) == tmp_path


def test_site_config_backups_are_unique_and_private(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_file = tmp_path / "sites.json"
    backup_dir = tmp_path / "state" / "backups"
    config_file.write_text('{"sites": []}\n', encoding="utf-8")
    monkeypatch.setattr(agent, "SITES_CONFIG_FILE", config_file)
    monkeypatch.setattr(agent, "PROJECT_CONFIG_BACKUP_DIR", backup_dir)

    agent._backup_projects_config()
    agent._backup_projects_config()

    backups = list(backup_dir.glob("sites.json.*.bak"))
    assert len(backups) == 2
    for backup in backups:
        assert backup.read_bytes() == config_file.read_bytes()
        if agent.os.name != "nt":
            assert backup.stat().st_mode & 0o777 == 0o600
    if agent.os.name != "nt":
        assert backup_dir.stat().st_mode & 0o777 == 0o700


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


def test_state_writes_cannot_persist_an_older_snapshot_after_a_newer_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent, "_state", {"system_metrics": [{"version": "initial"}]})
    monkeypatch.setattr(agent, "_state_write_lock", threading.Lock())

    first_write_entered = threading.Event()
    allow_first_write = threading.Event()
    persisted_versions: list[str] = []
    call_count = 0
    call_count_lock = threading.Lock()

    def fake_atomic_write(path: Path, text: str) -> None:
        nonlocal call_count
        with call_count_lock:
            call_count += 1
            current_call = call_count
        if current_call == 1:
            first_write_entered.set()
            assert allow_first_write.wait(timeout=2)
        assert path == agent.MONITORING_STATE_FILE
        persisted_versions.append(json.loads(text)["system_metrics"][0]["version"])

    monkeypatch.setattr(agent, "_atomic_write_private_text", fake_atomic_write)

    older = threading.Thread(target=lambda: agent._update_state(system_metrics=[{"version": "old"}]))
    newer = threading.Thread(target=lambda: agent._update_state(system_metrics=[{"version": "new"}]))
    older.start()
    assert first_write_entered.wait(timeout=2)
    newer.start()
    allow_first_write.set()
    older.join(timeout=2)
    newer.join(timeout=2)

    assert not older.is_alive()
    assert not newer.is_alive()
    assert persisted_versions == ["old", "new"]
