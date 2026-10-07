"""Optional Docker engine setup must not restore business deployment behavior."""

import pytest

from test_installer_operations import INSTALLER, ROOT, bash_run, function


def run_docker_setup(setting="", package_manager="apt-get", present=False, approve=False, fail=False, language="en"):
    mocks = r'''
command() {
  if [[ "$1" == "-v" ]]; then
    case "$2" in
      docker) [[ "$TEST_DOCKER_PRESENT" == true ]]; return ;;
      apt-get|dnf|yum) [[ "$2" == "$TEST_PACKAGE_MANAGER" ]]; return ;;
    esac
  fi
  builtin command "$@"
}
apt-get() { printf 'PACKAGE apt-get %s\n' "$*"; [[ "$TEST_FAIL" != true ]]; }
dnf() { printf 'PACKAGE dnf %s\n' "$*"; [[ "$TEST_FAIL" != true ]]; }
yum() { printf 'PACKAGE yum %s\n' "$*"; [[ "$TEST_FAIL" != true ]]; }
systemctl() { printf 'SERVICE %s\n' "$*"; }
'''
    if approve:
        mocks += '\nask_yes_no() { printf "PROMPT %s\\n" "$1"; return 0; }\n'
    script = "\n".join([
        next(line for line in INSTALLER.splitlines() if line.startswith("SETUP_DOCKER=")),
        function("is_en"), function("ask_yes_no"), mocks,
        function("install_docker_if_requested"),
        "install_docker_if_requested </dev/null",
    ])
    return bash_run(script, SETUP_DOCKER=setting, TEST_PACKAGE_MANAGER=package_manager,
                    TEST_DOCKER_PRESENT=str(present).lower(), TEST_FAIL=str(fail).lower(), INSTALL_LANG=language)


@pytest.mark.parametrize("setting", ["", "ask", "false", "no", "0", "off"])
def test_noninteractive_default_or_disabled_does_not_install_or_start_docker(setting):
    result = run_docker_setup(setting)
    assert result.returncode == 0, result.stderr
    assert "PACKAGE " not in result.stdout
    assert "SERVICE " not in result.stdout


@pytest.mark.parametrize("package_manager, expected", [
    ("apt-get", ["PACKAGE apt-get update", "PACKAGE apt-get install -y docker.io"]),
    ("dnf", ["PACKAGE dnf install -y docker"]),
    ("yum", ["PACKAGE yum install -y docker"]),
])
def test_preapproved_install_adds_only_engine_and_starts_service(package_manager, expected):
    result = run_docker_setup("yes", package_manager=package_manager)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == expected + ["SERVICE enable docker", "SERVICE start docker"]


def test_existing_docker_is_not_reinstalled_or_restarted():
    result = run_docker_setup("yes", present=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "Docker is already installed."


@pytest.mark.parametrize("language, prompt", [
    ("en", "container management, Docker Nginx and the request gateway"),
    ("zh", "容器管理、Docker Nginx 和请求网关"),
])
def test_prompt_approval_installs_engine_with_operations_wording(language, prompt):
    result = run_docker_setup("ask", approve=True, language=language)
    assert result.returncode == 0, result.stderr
    assert prompt in result.stdout
    assert "SERVICE start docker" in result.stdout
    assert "Compose" not in result.stdout


def test_failed_package_install_does_not_start_docker():
    result = run_docker_setup("yes", fail=True)
    assert result.returncode != 0
    assert "SERVICE " not in result.stdout


def test_unsupported_package_manager_skips_service_changes():
    result = run_docker_setup("yes", package_manager="none")
    assert result.returncode == 0, result.stderr
    assert "No supported package manager" in result.stdout
    assert "SERVICE " not in result.stdout


def test_docker_setup_remains_optional_and_after_upgrade_backup():
    call = INSTALLER.index("\ninstall_docker_if_requested\n")
    backup = INSTALLER.index('bash "$SOURCE_DIR/scripts/backup-installation.sh"')
    assert call > backup
    assert 'SETUP_DOCKER="${SETUP_DOCKER:-ask}"' in INSTALLER
    assert "SETUP_DOCKER=ask" in (ROOT / "server.env.example").read_text(encoding="utf-8")
    assert "docker-compose" not in INSTALLER
    assert "import yaml" not in INSTALLER
