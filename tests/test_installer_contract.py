from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = (ROOT / "install.sh").read_text(encoding="utf-8")
BACKUP_SCRIPT = (ROOT / "scripts" / "backup-installation.sh").read_text(encoding="utf-8")
BOOTSTRAP_SCRIPT = (ROOT / "scripts" / "bootstrap_server.sh").read_text(encoding="utf-8")
DOCTOR_SCRIPT = (ROOT / "scripts" / "doctor.sh").read_text(encoding="utf-8")


def test_upgrade_backup_precedes_installer_mutations() -> None:
    backup_call = INSTALLER.index('bash "$SOURCE_DIR/scripts/backup-installation.sh"')
    verify_call = INSTALLER.index('python3 "$SOURCE_DIR/scripts/verify_backup.py" "$UPGRADE_BACKUP_PATH"')
    mutation_boundary = INSTALLER.index("# Only now may the installer mutate the system.")
    first_app_creation = INSTALLER.index('install -d -m 755 "$APP_HOME"', mutation_boundary)
    first_data_marker_write = INSTALLER.index("printf 'data_home=%s\\n'", mutation_boundary)

    assert backup_call < verify_call < mutation_boundary < first_app_creation
    assert backup_call < first_data_marker_write


def test_project_config_migration_follows_verified_original_state_backup() -> None:
    backup_call = INSTALLER.index('bash "$SOURCE_DIR/scripts/backup-installation.sh"')
    verify_call = INSTALLER.index('python3 "$SOURCE_DIR/scripts/verify_backup.py" "$UPGRADE_BACKUP_PATH"')
    migration_call = INSTALLER.index("\nmigrate_projects_config\n", verify_call)
    first_release_mutation = INSTALLER.index("\nsecure_app_release_files\n", migration_call)

    assert backup_call < verify_call < migration_call < first_release_mutation
    assert 'validate_private_projects_file "$APP_HOME/projects.json"' in BACKUP_SCRIPT
    assert 'validate_private_projects_file "$DATA_HOME/projects.json"' in BACKUP_SCRIPT
    assert 'for path in "$APP_HOME" "$ENV_FILE" "$DATA_HOME"' in BACKUP_SCRIPT


def test_project_config_runtime_path_is_data_home() -> None:
    expected = 'DEPLOY_PROJECTS_FILE" "$DATA_HOME/projects.json"'
    assert f'upsert_env_assignment "{expected}' in INSTALLER
    assert 'PROJECTS_FILE="${DEPLOY_PROJECTS_FILE:-}"' in DOCTOR_SCRIPT
    assert 'PROJECTS_FILE="$(env_file_value "DEPLOY_PROJECTS_FILE")"' in DOCTOR_SCRIPT
    assert 'PROJECTS_FILE="${PROJECTS_FILE:-$DATA_HOME/projects.json}"' in DOCTOR_SCRIPT
    assert 'check_file "$PROJECTS_FILE" "projects config"' in DOCTOR_SCRIPT
    assert 'nano $DATA_HOME/projects.json' in INSTALLER


def test_doctor_uses_agent_service_and_state_layout_with_env_precedence() -> None:
    assert 'SERVICE_NAME="${DEPLOY_AGENT_SERVICE_NAME:-}"' in DOCTOR_SCRIPT
    assert 'SERVICE_NAME="${DEPLOY_SERVICE_NAME:-' not in DOCTOR_SCRIPT
    assert 'STATE_FILE="${DEPLOY_AGENT_STATE_FILE:-}"' in DOCTOR_SCRIPT
    assert 'SERVICE_NAME="$(env_file_value "DEPLOY_AGENT_SERVICE_NAME")"' in DOCTOR_SCRIPT
    assert 'STATE_FILE="$(env_file_value "DEPLOY_AGENT_STATE_FILE")"' in DOCTOR_SCRIPT
    assert 'DATA_HOME="${DATA_HOME:-$(dirname -- "$STATE_FILE")}"' in DOCTOR_SCRIPT
    assert DOCTOR_SCRIPT.index('STATE_FILE="$(env_file_value') < DOCTOR_SCRIPT.index(
        'DATA_HOME="${DATA_HOME:-$(dirname -- "$STATE_FILE")}"'
    )


def test_doctor_delegates_full_config_validation_to_agent_cli() -> None:
    fixed_path = 'PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"'
    validation = (
        'MINI_DEPLOY_HOME="$APP_HOME" \\\n'
        '    DEPLOY_AGENT_STATE_FILE="$STATE_FILE" \\\n'
        '    DEPLOY_PROJECTS_FILE="$PROJECTS_FILE" \\\n'
        '    "$PYTHON_BIN" "$APP_HOME/agent.py" validate-config'
    )

    assert fixed_path in DOCTOR_SCRIPT
    assert 'PYTHON_BIN="$(readlink -f -- "$PYTHON_BIN")"' in DOCTOR_SCRIPT
    assert validation in DOCTOR_SCRIPT


def test_installer_prints_parameterized_admin_commands_for_custom_layouts() -> None:
    command_prefix = (
        "DEPLOY_AGENT_ENV_FILE=$ENV_FILE DEPLOY_AGENT_SERVICE_NAME=$SERVICE_NAME "
        "$PYTHON_BIN $APP_HOME/agent.py admin "
    )

    assert f"{command_prefix}set-password" in INSTALLER
    assert f"{command_prefix}reset-session" in INSTALLER
    assert 'DEPLOY_AGENT_ENV_FILE=$ENV_FILE bash $APP_HOME/scripts/doctor.sh' in INSTALLER
    assert "Agent validates projects config" in DOCTOR_SCRIPT
    assert "json.loads" not in DOCTOR_SCRIPT


def test_project_config_env_path_rejects_unmanaged_custom_locations() -> None:
    layout_check = INSTALLER[INSTALLER.index("assert_projects_env_layout() {") :]
    layout_check = layout_check[: layout_check.index("\n}\n")]

    assert 'canonical="$(readlink -m -- "$current")"' in layout_check
    assert '"$canonical" == "$legacy_projects_file"' in layout_check
    assert '"$canonical" == "$data_projects_file"' in layout_check
    assert "unsupported custom path" in layout_check
    assert "unmanaged custom path that this backup would omit" in BACKUP_SCRIPT


def test_first_project_config_migration_fails_closed_on_divergence() -> None:
    plan = INSTALLER[INSTALLER.index("validate_projects_migration_plan() {") :]
    plan = plan[: plan.index("\n}\n")]

    assert '"$PROJECTS_LAYOUT_STATE" != "data"' in plan
    assert 'projects_files_are_identical "$legacy_projects_file" "$data_projects_file"' in plan
    assert "risk losing project Tokens" in plan
    assert '"$PROJECTS_LAYOUT_STATE" == "data"' in plan
    assert "possibly stale legacy copy" in plan


def test_existing_install_never_replaces_missing_projects_with_example() -> None:
    plan = INSTALLER[INSTALLER.index("validate_projects_migration_plan() {") :]
    plan = plan[: plan.index("\n}\n")]

    assert '"$EXISTING_INSTALLATION" == "true"' in plan
    assert '"$PROJECTS_LAYOUT_STATE" == "data"' in plan
    assert "refusing to create an example" in plan
    assert "refusing to silently replace it with an example" in plan
    assert '"$EXISTING_INSTALLATION" != "true"' in plan
    assert 'validate_projects_json_structure "$SOURCE_DIR/examples/projects.example.json" "release"' in plan


def test_project_config_structure_is_checked_before_backup_and_after_copy() -> None:
    validator = INSTALLER[INSTALLER.index("validate_projects_json_structure() {") :]
    validator = validator[: validator.index("\n}\n")]
    preflight = INSTALLER.index("\nvalidate_projects_migration_plan\n")
    backup = INSTALLER.index('bash "$SOURCE_DIR/scripts/backup-installation.sh"')
    migration = INSTALLER.index("\nmigrate_projects_config\n", backup)

    assert "json.loads(payload.decode" in validator
    assert "not isinstance(projects, list) or not projects" in validator
    assert "any(not isinstance(project, dict)" in validator
    assert preflight < backup < migration
    assert 'validate_projects_json_structure "$data_projects_file" "private"' in INSTALLER
    assert 'projects_files_are_identical "$legacy_projects_file" "$data_projects_file" "private"' in INSTALLER


def test_authoritative_data_layout_does_not_parse_stale_legacy_json() -> None:
    plan = INSTALLER[INSTALLER.index("validate_projects_migration_plan() {") :]
    plan = plan[: plan.index("\n}\n")]

    assert 'validate_private_projects_file "$legacy_projects_file"' in plan
    assert '"$PROJECTS_LAYOUT_STATE" != "data" && -f "$legacy_projects_file"' in plan
    assert 'validate_projects_json_structure "$legacy_projects_file" "private"' in plan
    assert 'validate_projects_json_structure "$data_projects_file" "private"' in plan


def test_active_legacy_agent_stops_only_after_verified_backup() -> None:
    backup = INSTALLER.index('bash "$SOURCE_DIR/scripts/backup-installation.sh"')
    verify = INSTALLER.index(
        'python3 "$SOURCE_DIR/scripts/verify_backup.py" "$UPGRADE_BACKUP_PATH"'
    )
    stop = INSTALLER.index("\nstop_existing_agent_for_upgrade\n", verify)
    migration = INSTALLER.index("\nmigrate_projects_config\n", stop)

    assert backup < verify < stop < migration
    stop_function = INSTALLER[INSTALLER.index("stop_existing_agent_for_upgrade() {") :]
    stop_function = stop_function[: stop_function.index("\n}\n")]
    assert 'systemctl is-active "$SERVICE_NAME"' in stop_function
    assert 'trap restart_previous_agent_on_failure EXIT' in stop_function
    assert 'systemctl stop "$SERVICE_NAME"' in stop_function
    assert "Could not confirm the old Agent service stopped" in stop_function


def test_failed_upgrade_restarts_previously_active_agent_and_preserves_status() -> None:
    failsafe = INSTALLER[INSTALLER.index("restart_previous_agent_on_failure() {") :]
    failsafe = failsafe[: failsafe.index("\n}\n")]
    main_service = INSTALLER.index("\nwrite_systemd_service\n")
    restart = INSTALLER.index('systemctl restart "$SERVICE_NAME"', main_service)
    active_check = INSTALLER.index('systemctl is-active --quiet "$SERVICE_NAME"', restart)
    disarm = INSTALLER.index("\ndisarm_upgrade_service_failsafe\n", active_check)

    assert "local original_status=$?" in failsafe
    assert "trap - EXIT" in failsafe
    assert 'systemctl start "$SERVICE_NAME"' in failsafe
    assert 'exit "$original_status"' in failsafe
    assert restart < active_check < disarm


def test_project_config_copy_is_private_atomic_and_durable() -> None:
    copier = INSTALLER[INSTALLER.index("atomic_copy_projects_file() {") :]
    copier = copier[: copier.index("\n}\n")]

    assert 'getattr(os, "O_NOFOLLOW", 0)' in copier
    assert "source_after.st_nlink != 1" in copier
    assert "os.fchmod(temporary_descriptor, 0o600)" in copier
    assert "os.fchown(temporary_descriptor, 0, 0)" in copier
    assert "os.fsync(target_handle.fileno())" in copier
    assert "os.replace(temporary_path, target_path)" in copier
    assert "os.fsync(directory_descriptor)" in copier


def test_data_home_is_recognized_by_project_config_alone() -> None:
    assert '&& ! -f "$DATA_HOME/projects.json"' in INSTALLER
    assert '&& ! -f "$DATA_HOME/projects.json"' in BACKUP_SCRIPT


def test_release_symlinks_are_rejected_before_backup_and_before_chmod() -> None:
    source_validation = INSTALLER.index("\nvalidate_release_source_tree\n")
    backup_call = INSTALLER.index('bash "$SOURCE_DIR/scripts/backup-installation.sh"')
    copy_block = INSTALLER.index('rsync -a -x --delete')
    copied_tree_validation = INSTALLER.index("\nsecure_app_release_files\n", copy_block)
    executable_chmod = INSTALLER.index('chmod +x "$APP_HOME/agent.py"', copy_block)

    assert source_validation < backup_call
    assert copy_block < copied_tree_validation < executable_chmod


def test_installer_never_recursively_changes_persistent_app_tree() -> None:
    assert 'chown -R root:root "$APP_HOME"' not in INSTALLER
    assert 'chmod -R go-w "$APP_HOME"' not in INSTALLER
    for persistent_name in ("workspace", "projects.json", ".backups", "state.json", "audit.jsonl"):
        assert persistent_name in INSTALLER


def test_control_files_require_exact_managed_markers() -> None:
    assert '# mini_deploy-managed: agent-systemd-v1' in INSTALLER
    assert '# mini_deploy-managed: nginx-v1' in INSTALLER
    assert "grep -Fxq -- \"$MANAGED_SERVICE_MARKER\"" in INSTALLER
    assert "grep -Fxq -- \"$MANAGED_NGINX_MARKER\"" in INSTALLER
    assert "grep -Eq 'mini[_-]deploy|agent\\.py'" not in INSTALLER
    assert "grep -Eq 'mini[_-]deploy|/deploy/'" not in INSTALLER


def test_install_marker_is_bound_to_canonical_app_home() -> None:
    for script in (INSTALLER, BACKUP_SCRIPT):
        assert 'marker_unique_field "$marker" "format"' in script
        assert 'marker_unique_field "$marker" "app_home"' in script
        assert '[[ "$marker_app_home" == "$APP_HOME" ]]' in script
        assert "stat -c '%h' -- \"$marker\"" in script


def test_backup_bundle_is_published_atomically_with_completion_marker() -> None:
    manifest_write = BACKUP_SCRIPT.index("printf 'archive=installation.tar.gz\\n'")
    completion_write = BACKUP_SCRIPT.index("printf 'complete\\n'")
    publish = BACKUP_SCRIPT.index('mv -T -- "$staging" "$bundle"')

    assert manifest_write < completion_write < publish
    assert 'sync -f "$archive" "$manifest" "$complete_marker" "$staging"' in BACKUP_SCRIPT
    assert 'printf \'%s\\n\' "$published_archive"' in BACKUP_SCRIPT


def test_backup_manifest_records_portable_layout_and_release_identity() -> None:
    for field in (
        "format=2",
        "app_home=%s",
        "env_file=%s",
        "data_home=%s",
        "log_home=%s",
        "service_name=%s",
        "service_file=%s",
        "nginx_conf_file=%s",
        "legacy_installation=%s",
        "release_fingerprint=sha256:%s",
    ):
        assert field in BACKUP_SCRIPT


def test_legacy_adoption_is_explicitly_opt_in() -> None:
    assert 'DEPLOY_ALLOW_LEGACY_INSTALL_ADOPTION="${DEPLOY_ALLOW_LEGACY_INSTALL_ADOPTION:-false}"' in INSTALLER
    assert 'BACKUP_ALLOW_LEGACY_INSTALLATION="${BACKUP_ALLOW_LEGACY_INSTALLATION:-false}"' in BACKUP_SCRIPT


def test_install_and_backup_share_private_maintenance_lock() -> None:
    lock_path = '/run/mini-deploy-agent/maintenance.lock'
    assert f'MAINTENANCE_LOCK_FILE="{lock_path}"' in INSTALLER
    assert f'MAINTENANCE_LOCK_FILE="{lock_path}"' in BACKUP_SCRIPT
    assert 'BACKUP_MAINTENANCE_LOCK_HELD="true"' in INSTALLER
    assert 'BACKUP_MAINTENANCE_LOCK_FD="$MAINTENANCE_LOCK_FD"' in INSTALLER
    assert 'flock -n "$MAINTENANCE_LOCK_FD"' in INSTALLER
    assert 'flock -n "$MAINTENANCE_LOCK_FD"' in BACKUP_SCRIPT
    for script in (INSTALLER, BACKUP_SCRIPT):
        assert "stat -Lc '%d:%i' -- \"/proc/self/fd/$MAINTENANCE_LOCK_FD\"" in script
        assert "stat -Lc '%h' -- \"/proc/self/fd/$MAINTENANCE_LOCK_FD\"" in script


def test_systemd_recreates_private_runtime_lock_directory_after_reboot() -> None:
    service_template = (ROOT / "systemd" / "mini-deploy-agent.service").read_text(encoding="utf-8")
    for unit_text in (INSTALLER, service_template):
        assert "RuntimeDirectory=mini-deploy-agent" in unit_text
        assert "RuntimeDirectoryMode=0700" in unit_text
        assert "RuntimeDirectoryPreserve=yes" in unit_text


def test_python_310_is_required_before_installer_state_mutation() -> None:
    version_check = INSTALLER.index("sys.version_info >= (3, 10)")
    lock_call = INSTALLER.index("\nacquire_maintenance_lock\n")

    assert version_check < lock_call
    assert "Python 3.10 or newer is required" in INSTALLER
    assert "sys.version_info >= (3, 10)" in BACKUP_SCRIPT


def test_nested_mounts_are_rejected_before_recursive_operations() -> None:
    assert 'assert_no_nested_mounts "$SOURCE_DIR" "SOURCE_DIR"' in INSTALLER
    assert 'assert_no_nested_mounts "$APP_HOME" "APP_HOME"' in INSTALLER
    assert 'assert_no_nested_mounts "$APP_HOME" "APP_HOME"' in BACKUP_SCRIPT
    assert 'assert_no_nested_mounts "$DATA_HOME" "DATA_HOME"' in BACKUP_SCRIPT
    assert "--one-file-system -czf" in BACKUP_SCRIPT
    assert "rsync -a -x --delete" in INSTALLER
    assert "chown -R --one-file-system" in INSTALLER

    backup_mount_check = BACKUP_SCRIPT.rindex('assert_no_nested_mounts "$APP_HOME" "APP_HOME"')
    tar_call = BACKUP_SCRIPT.index('tar -C / --one-file-system -czf')
    assert backup_mount_check < tar_call


def test_control_and_credential_files_reject_hard_links() -> None:
    assert "Control file must have exactly one hard link" in INSTALLER
    assert "Backup control file must have exactly one hard link" in BACKUP_SCRIPT
    assert "projects.json must have exactly one hard link" in INSTALLER
    assert "projects.json must have exactly one hard link" in BACKUP_SCRIPT
    assert "Password file changed while opening or has multiple hard links" in INSTALLER
    assert "server.env changed while it was being opened or has multiple hard links" in BOOTSTRAP_SCRIPT


def test_bootstrap_disables_xtrace_before_loading_credentials() -> None:
    assert BOOTSTRAP_SCRIPT.index("set +x") < BOOTSTRAP_SCRIPT.index('if [[ -e "$ROOT_DIR/server.env"')
    assert BOOTSTRAP_SCRIPT.index("set +x") < BOOTSTRAP_SCRIPT.index("source ")


def test_root_shell_entrypoints_use_a_fixed_system_path() -> None:
    fixed_path = 'PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"'
    for script in (INSTALLER, BACKUP_SCRIPT, BOOTSTRAP_SCRIPT):
        assert fixed_path in script
        assert script.index(fixed_path) < script.index("command -v") if "command -v" in script else True
