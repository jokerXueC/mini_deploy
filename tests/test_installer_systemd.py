"""Render the installer unit and verify publication and systemd parsing."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = (ROOT / "install.sh").read_text(encoding="utf-8")
START = INSTALLER.index("write_systemd_service() {\n")
GENERATOR = INSTALLER[START:INSTALLER.index("\n}\n", START) + 3]


def generate(tmp_path, verifier):
    bash = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")
    if not bash or not Path(bash).is_file():
        pytest.skip("Bash required")
    unit = tmp_path / "mini-deploy-agent.service"
    unit.write_text("# previous service\n", encoding="utf-8")
    app = tmp_path / "app"
    app.mkdir()
    env_file = tmp_path / "agent.env"
    env_file.write_text("", encoding="utf-8")
    # Only the privileged ownership change is adapted to the current test user.
    generator = GENERATOR.replace('chown root:root', 'chown "$(id -u):$(id -g)"')
    script = 'set -euo pipefail\n' + verifier + '\n' + generator + '\nwrite_systemd_service\n'
    environment = {**os.environ, "SERVICE_FILE": str(unit), "SERVICE_NAME": "mini-deploy-agent",
                   "APP_HOME": str(app), "ENV_FILE": str(env_file), "PYTHON_BIN": "/bin/true",
                   "MANAGED_SERVICE_MARKER": "# mini_deploy-managed: agent-systemd-v1"}
    result = subprocess.run([bash, "-c", script], env=environment, capture_output=True,
                            text=True, encoding="utf-8", timeout=20)
    return result, unit, app, env_file


def test_generated_paths_have_no_literal_quotes_and_exec_arguments_remain_quoted(tmp_path):
    verifier = '''systemd-analyze() {
      [[ "$1" == verify && "$2" == *.service ]] || return 1
      grep -Fxq '# previous service' "$SERVICE_FILE" || return 1
      [[ -s "$2" ]]
    }'''
    result, unit, app, env_file = generate(tmp_path, verifier)
    assert result.returncode == 0, result.stderr
    text = unit.read_text(encoding="utf-8")
    assert f'WorkingDirectory={app}\n' in text
    assert f'EnvironmentFile={env_file}\n' in text
    assert f'ExecStart="/bin/true" "{app}/agent.py"' in text
    assert list(tmp_path.glob("*.service")) == [unit]


def test_failed_systemd_validation_preserves_existing_service(tmp_path):
    result, unit, *_ = generate(tmp_path, 'systemd-analyze() { return 1; }')
    assert result.returncode != 0
    assert unit.read_text(encoding="utf-8") == "# previous service\n"
    assert list(tmp_path.glob("*.service")) == [unit]
    backups = list(tmp_path.glob("*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "# previous service\n"


@pytest.mark.skipif(os.name != "posix" or not shutil.which("systemd-analyze"), reason="Linux systemd-analyze required")
def test_real_systemd_accepts_generated_unit(tmp_path):
    result, unit, *_ = generate(tmp_path, "")
    assert result.returncode == 0, result.stderr
    assert "WorkingDirectory=" in unit.read_text()


def test_verifier_is_required_before_installer_mutations():
    prerequisites = INSTALLER[INSTALLER.index("for command in "):INSTALLER.index('PYTHON_BIN=')]
    assert "systemd-analyze" in prerequisites
