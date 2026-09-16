from __future__ import annotations

import base64
import io
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import agent


def test_password_hash_round_trip_and_wrong_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "PASSWORD_HASH_ITERATIONS", 1_000)
    monkeypatch.setattr(agent, "MIN_PASSWORD_HASH_ITERATIONS", 1_000)
    password_hash = agent._hash_password("correct horse battery staple", salt=b"0123456789abcdef")
    monkeypatch.setattr(agent, "UI_PASSWORD_HASH", password_hash)

    assert password_hash.startswith("pbkdf2_sha256$1000$")
    assert agent._verify_password("correct horse battery staple")
    assert not agent._verify_password("incorrect")


@pytest.mark.parametrize(
    "password_hash",
    [
        "",
        "not-a-password-hash",
        "sha256$260000$c2FsdA==$ZGlnZXN0",
        "pbkdf2_sha256$bad$c2FsdA==$ZGlnZXN0",
        "pbkdf2_sha256$1$c2FsdA==$ZGlnZXN0",
        "pbkdf2_sha256$999999999$c2FsdA==$ZGlnZXN0",
    ],
)
def test_password_verification_rejects_malformed_hashes(
    monkeypatch: pytest.MonkeyPatch,
    password_hash: str,
) -> None:
    monkeypatch.setattr(agent, "UI_PASSWORD_HASH", password_hash)

    assert not agent._verify_password("any password")


def test_session_cookie_round_trip_and_tamper_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "UI_SESSION_SECRET", "session-secret")
    cookie = agent._make_session_cookie()

    assert agent._verify_session_cookie(cookie)

    payload = base64.urlsafe_b64decode(cookie.encode("ascii")).decode("utf-8")
    expires, signature = payload.split(":", 1)
    replacement = "0" if signature[-1] != "0" else "1"
    tampered = base64.urlsafe_b64encode(f"{expires}:{signature[:-1]}{replacement}".encode()).decode("ascii")
    assert not agent._verify_session_cookie(tampered)


def test_session_cookie_rejects_expired_or_unsigned_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "UI_SESSION_SECRET", "session-secret")
    expires = int(time.time()) - 1
    expired_payload = f"{expires}:{agent._session_signature(expires)}"
    expired_cookie = base64.urlsafe_b64encode(expired_payload.encode()).decode("ascii")
    assert not agent._verify_session_cookie(expired_cookie)

    monkeypatch.setattr(agent, "UI_SESSION_SECRET", "")
    assert not agent._verify_session_cookie(expired_cookie)
    assert not agent._verify_session_cookie("not-base64")


def test_csrf_token_is_bound_to_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "UI_SESSION_SECRET", "session-secret")

    first = agent._csrf_token("session-a")
    assert first == agent._csrf_token("session-a")
    assert first != agent._csrf_token("session-b")


@pytest.mark.parametrize(
    ("peer_ip", "forwarded_ip", "trust_proxy", "expected"),
    [
        ("127.0.0.1", "203.0.113.8", True, "203.0.113.8"),
        ("::1", "2001:db8::8", True, "2001:db8::8"),
        ("198.51.100.4", "203.0.113.8", True, "198.51.100.4"),
        ("127.0.0.1", "not-an-ip", True, "127.0.0.1"),
        ("127.0.0.1", "203.0.113.8", False, "127.0.0.1"),
    ],
)
def test_request_client_ip_only_trusts_valid_loopback_proxy_header(
    monkeypatch: pytest.MonkeyPatch,
    peer_ip: str,
    forwarded_ip: str,
    trust_proxy: bool,
    expected: str,
) -> None:
    monkeypatch.setattr(agent, "TRUST_LOOPBACK_PROXY_HEADERS", trust_proxy)
    request = type(
        "Request",
        (),
        {"client_address": (peer_ip, 1234), "headers": {"X-Real-IP": forwarded_ip}},
    )()

    assert agent._request_client_ip(request) == expected


def test_login_attempt_reservations_are_atomic_and_success_clears_them(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent.time, "time", lambda: 1_000.0)
    with agent._login_failures_lock:
        agent._login_failures.clear()

    with ThreadPoolExecutor(max_workers=10) as executor:
        admitted = list(executor.map(lambda _: agent._reserve_login_attempt("203.0.113.9"), range(10)))

    assert admitted.count(True) == 5
    assert admitted.count(False) == 5
    agent._clear_login_attempts("203.0.113.9")
    assert agent._reserve_login_attempt("203.0.113.9")


def test_login_attempt_reservation_globally_evicts_expired_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    with agent._login_failures_lock:
        agent._login_failures.clear()
    monkeypatch.setattr(agent.time, "time", lambda: 1_000.0)
    assert agent._reserve_login_attempt("203.0.113.1")
    assert agent._reserve_login_attempt("203.0.113.2")

    monkeypatch.setattr(agent.time, "time", lambda: 1_061.0)
    assert agent._reserve_login_attempt("203.0.113.3")

    assert set(agent._login_failures) == {"203.0.113.3"}


def test_login_attempt_reservation_bounds_active_client_table(monkeypatch: pytest.MonkeyPatch) -> None:
    with agent._login_failures_lock:
        agent._login_failures.clear()
    monkeypatch.setattr(agent.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(agent, "LOGIN_FAILURE_MAX_CLIENTS", 2)

    for ip in ("203.0.113.1", "203.0.113.2", "203.0.113.3"):
        assert agent._reserve_login_attempt(ip)

    assert len(agent._login_failures) == 2
    assert "203.0.113.3" in agent._login_failures


def test_force_unlock_refuses_live_deploy_worker_without_child_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running_lock = threading.Lock()
    running_lock.acquire()
    monkeypatch.setattr(agent, "_running_lock", running_lock)
    monkeypatch.setattr(agent, "_deploy_process", None)
    monkeypatch.setattr(agent, "_deploy_worker_thread", threading.current_thread())

    ok, payload = agent._force_unlock_deploy()

    assert not ok
    assert payload["error"] == "deploy_worker_active"
    assert running_lock.locked()


def test_force_unlock_cannot_release_lock_while_deploy_is_starting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = agent._project_from_config({"key": "api", "enabled": False}, "api")
    running_lock = threading.Lock()
    update_entered = threading.Event()
    allow_failure = threading.Event()
    errors: list[BaseException] = []

    def fail_initial_state_write(**changes: object) -> None:
        del changes
        update_entered.set()
        assert allow_failure.wait(timeout=2)
        raise RuntimeError("simulated state initialization failure")

    def run_deploy() -> None:
        try:
            agent._run_deploy({"project_key": "api", "after": "abc123"})
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    monkeypatch.setattr(agent, "PROJECTS", {"api": project})
    monkeypatch.setattr(agent, "DEFAULT_PROJECT_KEY", "api")
    monkeypatch.setattr(agent, "_running_lock", running_lock)
    monkeypatch.setattr(agent, "_deploy_process", None)
    monkeypatch.setattr(agent, "_deploy_worker_thread", None)
    monkeypatch.setattr(agent, "_update_state", fail_initial_state_write)

    worker = threading.Thread(target=run_deploy)
    worker.start()
    assert update_entered.wait(timeout=2)

    ok, payload = agent._force_unlock_deploy()

    assert not ok
    assert payload["error"] == "deploy_worker_active"
    assert running_lock.locked()
    allow_failure.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert errors and isinstance(errors[0], RuntimeError)
    assert not running_lock.locked()
    assert agent._deploy_worker_thread is None


def test_force_unlock_releases_lock_owned_by_dead_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    running_lock = threading.Lock()
    running_lock.acquire()
    stale_worker = threading.Thread(target=lambda: None)
    updates: list[dict[str, object]] = []
    monkeypatch.setattr(agent, "_running_lock", running_lock)
    monkeypatch.setattr(agent, "_deploy_process", None)
    monkeypatch.setattr(agent, "_deploy_worker_thread", stale_worker)
    monkeypatch.setattr(agent, "_update_state", lambda **changes: updates.append(changes))

    ok, payload = agent._force_unlock_deploy()

    assert ok
    assert payload["message"] == "stale deploy lock cleared"
    assert not running_lock.locked()
    assert agent._deploy_worker_thread is None
    assert updates and updates[0]["running"] is False


def test_force_unlock_clears_state_before_releasing_running_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    running_lock = threading.Lock()
    running_lock.acquire()
    lock_observations: list[bool] = []
    monkeypatch.setattr(agent, "_running_lock", running_lock)
    monkeypatch.setattr(agent, "_deploy_process", None)
    monkeypatch.setattr(agent, "_deploy_worker_thread", None)

    def observe_update(**changes: object) -> None:
        assert changes["running"] is False
        lock_observations.append(running_lock.locked())
        assert not running_lock.acquire(blocking=False)

    monkeypatch.setattr(agent, "_update_state", observe_update)

    ok, _payload = agent._force_unlock_deploy()

    assert ok
    assert lock_observations == [True]
    assert not running_lock.locked()


def test_orphaned_child_can_be_force_unlocked_after_it_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    class Process:
        returncode: int | None = None
        pid = 4242

        def poll(self) -> int | None:
            return self.returncode

    process = Process()
    running_lock = threading.Lock()
    running_lock.acquire()
    updates: list[dict[str, object]] = []
    monkeypatch.setattr(agent, "_running_lock", running_lock)
    monkeypatch.setattr(agent, "_deploy_process", process)
    monkeypatch.setattr(agent, "_deploy_worker_thread", threading.current_thread())
    monkeypatch.setattr(agent, "_update_state", lambda **changes: updates.append(changes))

    agent._release_deploy_worker_lifecycle()

    assert agent._deploy_worker_thread is None
    assert running_lock.locked()
    assert agent._deploy_process is process
    blocked, payload = agent._force_unlock_deploy()
    assert not blocked
    assert payload["error"] == "deploy_process_running"

    process.returncode = 137
    ok, payload = agent._force_unlock_deploy()

    assert ok
    assert payload["message"] == "stale deploy lock cleared"
    assert not running_lock.locked()
    assert agent._deploy_process is None
    assert updates and updates[-1]["running"] is False


def test_web_password_setup_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "UI_PASSWORD_HASH", "")
    login_html = agent._render_login()

    assert 'action="setup-password"' not in login_html

    responses: list[tuple[int, str]] = []
    handler = object.__new__(agent.Handler)
    handler._write_html = lambda status, body: responses.append((status, body))

    agent.Handler._handle_setup_password(handler)

    assert responses
    assert responses[0][0] == 403


@pytest.mark.parametrize(
    ("password_hash", "session_secret"),
    [("configured-hash", ""), ("", "configured-session")],
)
def test_ui_auth_configuration_requires_independent_complete_credentials(
    monkeypatch: pytest.MonkeyPatch,
    password_hash: str,
    session_secret: str,
) -> None:
    monkeypatch.setattr(agent, "UI_PASSWORD_HASH", password_hash)
    monkeypatch.setattr(agent, "UI_SESSION_SECRET", session_secret)

    with pytest.raises(SystemExit, match="必须同时设置"):
        agent._validate_ui_auth_config()


def test_ui_auth_configuration_accepts_both_empty_or_both_strong(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "PASSWORD_HASH_ITERATIONS", 1_000)
    monkeypatch.setattr(agent, "MIN_PASSWORD_HASH_ITERATIONS", 1_000)
    configured_hash = agent._hash_password("test-password", salt=b"0123456789abcdef")
    for password_hash, session_secret in (("", ""), (configured_hash, "0123456789abcdef" * 4)):
        monkeypatch.setattr(agent, "UI_PASSWORD_HASH", password_hash)
        monkeypatch.setattr(agent, "UI_SESSION_SECRET", session_secret)
        agent._validate_ui_auth_config()


def test_ui_auth_configuration_rejects_malformed_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "UI_PASSWORD_HASH", "configured-but-invalid")
    monkeypatch.setattr(agent, "UI_SESSION_SECRET", "0123456789abcdef" * 4)

    with pytest.raises(SystemExit, match="PASSWORD_HASH"):
        agent._validate_ui_auth_config()


@pytest.mark.parametrize(
    "session_secret",
    ["short", "x" * 64, "replace-with-a-long-random-session-secret-1234567890"],
)
def test_ui_auth_configuration_rejects_weak_session_secret(
    monkeypatch: pytest.MonkeyPatch,
    session_secret: str,
) -> None:
    monkeypatch.setattr(agent, "PASSWORD_HASH_ITERATIONS", 1_000)
    monkeypatch.setattr(agent, "MIN_PASSWORD_HASH_ITERATIONS", 1_000)
    monkeypatch.setattr(
        agent,
        "UI_PASSWORD_HASH",
        agent._hash_password("test-password", salt=b"0123456789abcdef"),
    )
    monkeypatch.setattr(agent, "UI_SESSION_SECRET", session_secret)

    with pytest.raises(SystemExit, match="SESSION_SECRET"):
        agent._validate_ui_auth_config()


def test_write_admin_password_rotates_session_and_preserves_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_file = tmp_path / "mini-deploy-agent.env"
    env_file.write_text(
        "# existing configuration\n"
        "DEPLOY_AGENT_PORT=9010\n"
        "DEPLOY_UI_PASSWORD_HASH=old-hash\n"
        "DEPLOY_UI_SESSION_SECRET=old-session-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(agent, "AGENT_ENV_FILE", env_file)
    monkeypatch.setattr(agent, "PASSWORD_HASH_ITERATIONS", 1_000)
    monkeypatch.setattr(agent, "MIN_PASSWORD_HASH_ITERATIONS", 1_000)
    monkeypatch.setattr(agent, "UI_SESSION_SECRET", "old-session-secret")
    monkeypatch.setattr(agent.secrets, "token_hex", lambda _: "ab" * 32)
    old_cookie = agent._make_session_cookie()

    agent._write_admin_password("new-password")

    values = dict(
        line.split("=", 1)
        for line in env_file.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )
    assert values["DEPLOY_AGENT_PORT"] == "9010"
    assert values["DEPLOY_UI_PASSWORD_HASH"] != "old-hash"
    assert values["DEPLOY_UI_SESSION_SECRET"] == "ab" * 32
    assert str(env_file) in capsys.readouterr().out

    monkeypatch.setattr(agent, "UI_PASSWORD_HASH", values["DEPLOY_UI_PASSWORD_HASH"])
    monkeypatch.setattr(agent, "UI_SESSION_SECRET", values["DEPLOY_UI_SESSION_SECRET"])
    assert agent._verify_password("new-password")
    assert not agent._verify_session_cookie(old_cookie)


def test_set_admin_password_from_stdin_delegates_without_newline(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []
    monkeypatch.setattr(agent.sys, "stdin", io.StringIO("password-from-stdin\n"))
    monkeypatch.setattr(agent, "_write_admin_password", captured.append)

    agent._set_admin_password_from_stdin()

    assert captured == ["password-from-stdin"]


def test_reset_admin_sessions_rotates_only_session_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "mini-deploy-agent.env"
    env_file.write_text(
        "DEPLOY_UI_PASSWORD_HASH=keep-this-hash\n"
        "DEPLOY_UI_SESSION_SECRET=old-session\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(agent, "AGENT_ENV_FILE", env_file)
    monkeypatch.setattr(agent.secrets, "token_hex", lambda _: "cd" * 32)

    agent._reset_admin_sessions()

    values = dict(line.split("=", 1) for line in env_file.read_text(encoding="utf-8").splitlines())
    assert values["DEPLOY_UI_PASSWORD_HASH"] == "keep-this-hash"
    assert values["DEPLOY_UI_SESSION_SECRET"] == "cd" * 32


def test_write_admin_password_rejects_short_password(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / "mini-deploy-agent.env"
    monkeypatch.setattr(agent, "AGENT_ENV_FILE", env_file)

    with pytest.raises(SystemExit):
        agent._write_admin_password("short")

    assert not env_file.exists()


@pytest.mark.skipif(os.name == "nt", reason="Linux deployment security behavior")
def test_upsert_env_file_rejects_symbolic_link(tmp_path: Path) -> None:
    target = tmp_path / "real.env"
    target.write_text("KEEP=original\n", encoding="utf-8")
    link = tmp_path / "agent.env"
    link.symlink_to(target)

    with pytest.raises(OSError, match="symbolic-link"):
        agent._upsert_env_file(link, {"DEPLOY_UI_SESSION_SECRET": "ab" * 32})

    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == "KEEP=original\n"


@pytest.mark.parametrize(
    ("arguments", "function_name"),
    [
        (["set-password"], "_set_admin_password"),
        (["reset-session"], "_reset_admin_sessions"),
    ],
)
def test_break_glass_admin_commands_run_before_runtime_config_validation(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    function_name: str,
) -> None:
    called: list[bool] = []
    monkeypatch.setattr(agent, "_RUNTIME_CONFIG_ERROR", RuntimeError("broken projects config"))
    monkeypatch.setattr(agent.sys, "argv", ["agent.py", *arguments])
    monkeypatch.setattr(agent, function_name, lambda: called.append(True))

    agent.main()

    assert called == [True]


def test_validate_auth_command_runs_before_runtime_config_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[bool] = []
    monkeypatch.setattr(agent, "_RUNTIME_CONFIG_ERROR", RuntimeError("broken projects config"))
    monkeypatch.setattr(agent.sys, "argv", ["agent.py", "validate-auth"])
    monkeypatch.setattr(agent, "_validate_ui_auth_config", lambda: called.append(True))

    agent.main()

    assert called == [True]


def test_help_imports_when_projects_config_is_damaged(tmp_path: Path) -> None:
    broken_config = tmp_path / "projects.json"
    broken_config.write_text("{not-json", encoding="utf-8")
    environment = os.environ.copy()
    environment.update({
        "DEPLOY_PROJECTS_FILE": str(broken_config),
        "PYTHONDONTWRITEBYTECODE": "1",
    })

    result = subprocess.run(
        [sys.executable, str(Path(agent.__file__).resolve()), "--help"],
        cwd=str(Path(agent.__file__).resolve().parent),
        env=environment,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "set-password" in result.stdout


def test_set_password_stdin_works_when_projects_config_is_damaged(tmp_path: Path) -> None:
    broken_config = tmp_path / "projects.json"
    broken_config.write_text("{not-json", encoding="utf-8")
    env_file = tmp_path / "agent.env"
    environment = os.environ.copy()
    environment.update({
        "DEPLOY_PROJECTS_FILE": str(broken_config),
        "DEPLOY_AGENT_ENV_FILE": str(env_file),
        "PYTHONDONTWRITEBYTECODE": "1",
    })

    result = subprocess.run(
        [sys.executable, str(Path(agent.__file__).resolve()), "set-password-stdin"],
        cwd=str(Path(agent.__file__).resolve().parent),
        env=environment,
        input="strong-test-password\n",
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    content = env_file.read_text(encoding="utf-8")
    assert "DEPLOY_UI_PASSWORD_HASH=pbkdf2_sha256$" in content
    assert "DEPLOY_UI_SESSION_SECRET=" in content


def test_set_password_stdin_works_when_legacy_projects_config_needs_migration(
    tmp_path: Path,
) -> None:
    app_home = tmp_path / "app"
    app_home.mkdir()
    (app_home / "projects.json").write_text(
        '{"projects":[{"key":"legacy","enabled":false}]}\n',
        encoding="utf-8",
    )
    env_file = tmp_path / "agent.env"
    environment = os.environ.copy()
    environment.pop("DEPLOY_PROJECTS_FILE", None)
    environment.update({
        "MINI_DEPLOY_HOME": str(app_home),
        "DEPLOY_AGENT_STATE_FILE": str(tmp_path / "data" / "state.json"),
        "DEPLOY_AGENT_ENV_FILE": str(env_file),
        "PYTHONDONTWRITEBYTECODE": "1",
    })

    result = subprocess.run(
        [sys.executable, str(Path(agent.__file__).resolve()), "set-password-stdin"],
        cwd=str(Path(agent.__file__).resolve().parent),
        env=environment,
        input="strong-test-password\n",
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    content = env_file.read_text(encoding="utf-8")
    assert "DEPLOY_UI_PASSWORD_HASH=pbkdf2_sha256$" in content
    assert "DEPLOY_UI_SESSION_SECRET=" in content
    assert not (tmp_path / "data" / "projects.json").exists()
