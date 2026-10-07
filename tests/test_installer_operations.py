"""Upgrade preservation and site-only installer compatibility checks."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import verify_backup as verifier
from test_verify_backup import ArchiveEntry, DATA_HOME, _build_bundle


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = (ROOT / "install.sh").read_text(encoding="utf-8")
MANIFEST = INSTALLER[INSTALLER.index("RELEASE_ENTRIES=("):INSTALLER.index("\n)") + 3]


def function(name):
    start = INSTALLER.index(f"{name}() {{\n")
    return INSTALLER[start:INSTALLER.index("\n}\n", start) + 3]


def bash_run(script, **environment):
    bash = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")
    if not bash or not Path(bash).is_file():
        pytest.skip("Bash and coreutils required")
    return subprocess.run(
        [bash, "-c", "set -euo pipefail\n" + script],
        env={**os.environ, **{key: str(value) for key, value in environment.items()}},
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )


def test_release_manifest_excludes_deployment_adapters_and_templates():
    entries = MANIFEST.split("(", 1)[1].rsplit(")", 1)[0].split()
    for entry in entries:
        assert (ROOT / entry).exists(), entry
    assert "agent.py" in entries
    assert "monitoring.py" in entries
    assert "docker_mirrors.py" in entries
    assert "scripts/verify_backup.py" in entries
    assert "scripts/doctor.sh" in entries
    assert "examples/nginx.compose.yml" in entries
    assert not {"scripts", "examples", "project_guidance.py", "docker_onboarding.py",
                "entrypoint_checks.py", "scripts/deploy.sample.sh", "scripts/rollback.sample.sh"} & set(entries)
    assert "--delete" not in INSTALLER
    assert "rm -rf" not in INSTALLER
    assert '"$APP_HOME/scripts/"*.sh' not in INSTALLER
    assert "docker-compose" not in INSTALLER
    assert "import yaml" not in INSTALLER
    assert "DEPLOY_LOG_FILE" not in INSTALLER


@pytest.mark.parametrize("copier", ["cp", "rsync"])
def test_release_copy_preserves_business_files_and_skips_unshipped_sources(tmp_path, copier):
    source, target = tmp_path / "source", tmp_path / "installed"
    entries = MANIFEST.split("(", 1)[1].rsplit(")", 1)[0].split()
    for entry in entries:
        path = source / entry
        if entry == "ui":
            path.mkdir(parents=True)
            (path / "index.html").write_text("new UI", encoding="utf-8")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("new release", encoding="utf-8")
    for entry in ("examples/projects.example.json", "project_guidance.py", "scripts/deploy.sample.sh"):
        path = source / entry
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("must not ship", encoding="utf-8")
    retained = {
        "scripts/deploy.sample.sh": "custom deploy script",
        "scripts/rollback.sample.sh": "custom rollback script",
        "scripts/customer.sh": "business script",
        "systemd/customer.service": "business service",
        "examples/customer.json": "business configuration",
        "workspace/app.py": "business application",
        "ui/local.txt": "local file",
        "project_guidance.py": "retired adapter",
        "projects.json": "legacy config",
        "sites.json": "site config",
        "state.json": "legacy deployment state",
        "monitoring-state.json": "monitoring state",
    }
    for entry, content in retained.items():
        path = target / entry
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (target / "agent.py").write_text("old release", encoding="utf-8")
    if copier == "rsync":
        if bash_run("command -v rsync").returncode:
            pytest.skip("rsync required")
        start = INSTALLER.index('  (\n    cd "$SOURCE_DIR"')
        copy = INSTALLER[start:INSTALLER.index("\n  )", start) + 4]
        copy = copy.replace("--chown=root:root", '--chown="$(id -u):$(id -g)"')
    else:
        # Root/path validation is covered separately; exercise the actual copier.
        copy = "\n".join([
            "validate_release_source_tree() { :; }",
            "validate_app_release_tree_safety() { :; }",
            "assert_no_nested_mounts() { :; }",
            function("first_hardlinked_regular_file"),
            function("copy_without_rsync").replace("root:root", '"$(id -u):$(id -g)"'),
            "copy_without_rsync",
        ])
    result = bash_run(MANIFEST + copy, SOURCE_DIR=source.as_posix(), APP_HOME=target.as_posix())
    assert result.returncode == 0, result.stderr
    assert (target / "agent.py").read_text() == "new release"
    assert (target / "ui/index.html").read_text() == "new UI"
    for entry, content in retained.items():
        assert (target / entry).read_text() == content, entry
    assert not (target / "examples/projects.example.json").exists()


@pytest.mark.parametrize("authoritative_sites", [False, True])
def test_validate_config_accepts_legacy_missing_scripts_and_weak_webhook(tmp_path, authoritative_sites):
    legacy = tmp_path / "projects.json"
    legacy.write_text(json.dumps({"projects": [{
        "key": "legacy", "name": "Legacy site", "enabled": True,
        "script": str(tmp_path / "missing-deploy.sh"),
        "rollback_script": str(tmp_path / "missing-rollback.sh"),
        "webhook_secret": "weak", "workdir": str(tmp_path / "missing-repo"),
    }]}), encoding="utf-8")
    if authoritative_sites:
        (tmp_path / "sites.json").write_text('{"sites": []}', encoding="utf-8")
        legacy.write_text("stale malformed legacy config", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(ROOT / "agent.py"), "validate-config"],
        env={**os.environ, "MINI_DEPLOY_HOME": str(tmp_path),
             "DEPLOY_AGENT_ENV_FILE": str(tmp_path / "agent.env"),
             "DEPLOY_AGENT_STATE_FILE": str(tmp_path / "state.json"),
             "DEPLOY_PROJECTS_FILE": str(legacy)},
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "sites config OK:" in result.stdout
    assert f"sites={0 if authoritative_sites else 1}" in result.stdout


@pytest.mark.parametrize("payload, valid", [
    ({"projects": [{"script": "/missing.sh", "webhook_secret": "weak"}]}, True),
    ({"sites": []}, True),
    ({"sites": {}, "projects": []}, False),
    ({"sites": ["invalid entry"]}, False),
])
def test_install_preflight_checks_structure_without_deployment_requirements(tmp_path, payload, valid):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = bash_run(
        function("validate_projects_json_structure") + '\nvalidate_projects_json_structure "$CONFIG" release\n',
        PYTHON_BIN=Path(sys.executable).as_posix(), CONFIG=path.as_posix(),
    )
    assert (result.returncode == 0) is valid, result.stderr


def test_backup_preserves_both_site_and_legacy_configuration():
    backup = (ROOT / "scripts/backup-installation.sh").read_text(encoding="utf-8")
    assert 'validate_private_projects_file "$DATA_HOME/sites.json"' in backup
    assert 'validate_private_projects_file "$DATA_HOME/projects.json"' in backup
    assert 'validate_private_projects_file "$DATA_HOME/monitoring-state.json"' in backup
    assert '&& ! -f "$DATA_HOME/sites.json"' in backup
    assert not re.search(r"(?:docker|systemctl)\s+(?:rm|prune|disable)", INSTALLER)


def test_site_only_backup_with_monitoring_state_is_accepted(tmp_path):
    data_root = DATA_HOME.lstrip("/")
    bundle = _build_bundle(
        tmp_path, omit_entries=frozenset({f"{data_root}/projects.json"}),
        extra_entries=(
            ArchiveEntry(f"{data_root}/sites.json", "file", b'{"sites": []}\n'),
            ArchiveEntry(f"{data_root}/monitoring-state.json", "file", b'{}\n'),
        ),
    )
    result = verifier.verify_backup(bundle, require_root_owner=False)
    assert result.member_count == 10


@pytest.mark.parametrize("name", ["sites.json", "monitoring-state.json"])
@pytest.mark.parametrize("kind, mode, uid", [
    ("file", 0o644, 0), ("file", 0o600, 1000), ("directory", 0o700, 0),
    ("symlink", 0o600, 0), ("hardlink", 0o600, 0),
])
def test_backup_rejects_unsafe_site_or_monitoring_data(tmp_path, name, kind, mode, uid):
    bundle = _build_bundle(
        tmp_path,
        extra_entries=(ArchiveEntry(f"{DATA_HOME.lstrip('/')}/{name}", kind, b'{}', mode=mode, uid=uid,
                                    linkname=f"{DATA_HOME.lstrip('/')}/projects.json"),),
    )
    with pytest.raises(verifier.VerificationError, match=f"archived {re.escape(name)} is not"):
        verifier.verify_backup(bundle, require_root_owner=False)


@pytest.mark.parametrize("sites, legacy, valid", [
    ('{"sites": []}', 'malformed stale config', True),
    ('{"sites": {}}', '{"projects": []}', False),
    (None, '{"projects": [{"script": "/missing.sh", "webhook_secret": "weak"}]}', True),
    (None, None, False),
])
def test_upgrade_preflight_selects_sites_before_legacy_without_changing_files(tmp_path, sites, legacy, valid):
    app, data = tmp_path / "app", tmp_path / "data"
    app.mkdir()
    data.mkdir()
    if sites is not None:
        (data / "sites.json").write_text(sites, encoding="utf-8")
    if legacy is not None:
        (data / "projects.json").write_text(legacy, encoding="utf-8")
    original = {p.name: p.read_bytes() for p in data.iterdir()}
    # This test exercises selection and JSON validation, not root-only metadata.
    validator = function("validate_projects_json_structure").replace(
        "validate_projects_json_structure()", "check_structure()")
    script = "\n".join([
        "validate_private_projects_file() { :; }", validator,
        'validate_projects_json_structure() { check_structure "$1" release; }',
        function("validate_projects_migration_plan"),
        "validate_projects_migration_plan",
    ])
    result = bash_run(script, APP_HOME=app.as_posix(), DATA_HOME=data.as_posix(),
                      PYTHON_BIN=Path(sys.executable).as_posix(),
                      PROJECTS_LAYOUT_STATE="data", EXISTING_INSTALLATION="true")
    assert (result.returncode == 0) is valid, result.stderr
    assert {p.name: p.read_bytes() for p in data.iterdir()} == original
