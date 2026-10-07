from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

import agent


LINUX_FLOCK_ONLY = pytest.mark.skipif(agent.fcntl is None, reason="fcntl is available on Linux production hosts")


def _exclusive_lock() -> int:
    descriptor = os.open(agent.MAINTENANCE_LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    os.chmod(agent.MAINTENANCE_LOCK_FILE, 0o600)
    agent.fcntl.flock(descriptor, agent.fcntl.LOCK_EX | agent.fcntl.LOCK_NB)
    return descriptor


def _release_exclusive_lock(descriptor: int) -> None:
    agent.fcntl.flock(descriptor, agent.fcntl.LOCK_UN)
    os.close(descriptor)


@LINUX_FLOCK_ONLY
def test_shared_lock_rejects_active_maintenance_without_leaking_lock() -> None:
    descriptor = _exclusive_lock()
    try:
        with pytest.raises(agent._MaintenanceActiveError):
            agent._acquire_maintenance_shared_lock(blocking=False)
    finally:
        _release_exclusive_lock(descriptor)

    probe = _exclusive_lock()
    _release_exclusive_lock(probe)


@LINUX_FLOCK_ONLY
@pytest.mark.parametrize("parent_state", ["missing", "public"])
def test_shared_operation_fails_closed_for_unsafe_lock_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    parent_state: str,
) -> None:
    lock_directory = tmp_path / parent_state
    if parent_state == "public":
        lock_directory.mkdir(mode=0o755)
        os.chmod(lock_directory, 0o755)
    monkeypatch.setattr(agent, "MAINTENANCE_LOCK_FILE", lock_directory / "maintenance.lock")

    @agent._maintenance_shared_operation
    def mutate() -> None:
        pytest.fail("mutation must not run with an unsafe maintenance lock")

    with pytest.raises(agent._MaintenanceLockError, match="maintenance lock directory"):
        mutate()


@LINUX_FLOCK_ONLY
@pytest.mark.parametrize("file_state", ["public", "symlink"])
def test_shared_operation_fails_closed_for_unsafe_lock_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    file_state: str,
) -> None:
    lock_directory = tmp_path / "private"
    lock_directory.mkdir(mode=0o700)
    os.chmod(lock_directory, 0o700)
    lock_file = lock_directory / "maintenance.lock"
    if file_state == "public":
        lock_file.write_text("", encoding="utf-8")
        os.chmod(lock_file, 0o644)
    else:
        target = tmp_path / "target.lock"
        target.write_text("", encoding="utf-8")
        lock_file.symlink_to(target)
    monkeypatch.setattr(agent, "MAINTENANCE_LOCK_FILE", lock_file)

    @agent._maintenance_shared_operation
    def mutate() -> None:
        pytest.fail("mutation must not run with an unsafe maintenance lock")

    with pytest.raises(agent._MaintenanceLockError, match="maintenance lock file"):
        mutate()


@pytest.mark.parametrize("fail", [False, True])
def test_shared_operation_releases_lock_after_success_or_failure(monkeypatch, fail):
    acquired = []
    released = []
    monkeypatch.setattr(agent, "_acquire_maintenance_shared_lock",
                        lambda *, blocking: acquired.append(blocking) or 73)
    monkeypatch.setattr(agent, "_release_maintenance_lock", released.append)

    @agent._maintenance_shared_operation
    def mutate(value, *, increment):
        assert acquired == [True]
        assert released == []
        if fail:
            raise RuntimeError("mutation failed")
        return value + increment

    if fail:
        with pytest.raises(RuntimeError, match="mutation failed"):
            mutate(4, increment=3)
    else:
        assert mutate(4, increment=3) == 7
    assert released == [73]


def test_log_continues_to_private_file_when_stdout_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    written: list[str] = []

    def fail_print(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise BrokenPipeError("stdout is closed")

    monkeypatch.setattr("builtins.print", fail_print)
    monkeypatch.setattr(agent, "_append_private_text", lambda path, text: written.append(text))

    agent._log("lifecycle diagnostic")

    assert len(written) == 1
    assert written[0].endswith("lifecycle diagnostic\n")


@LINUX_FLOCK_ONLY
def test_blocking_shared_operation_waits_for_maintenance_to_finish() -> None:
    descriptor = _exclusive_lock()
    entered = threading.Event()
    finished = threading.Event()

    @agent._maintenance_shared_operation
    def mutate() -> None:
        entered.set()

    worker = threading.Thread(target=lambda: (mutate(), finished.set()))
    worker.start()
    try:
        assert not entered.wait(timeout=0.1)
    finally:
        _release_exclusive_lock(descriptor)
    worker.join(timeout=2)

    assert entered.is_set()
    assert finished.is_set()
    assert not worker.is_alive()


def test_startup_lock_probe_accepts_active_installer_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []

    def active_probe(*, blocking: bool) -> int | None:
        calls.append(blocking)
        raise agent._MaintenanceActiveError("installer active")

    monkeypatch.setattr(agent, "_acquire_maintenance_shared_lock", active_probe)

    agent._probe_maintenance_lock_for_startup()

    assert calls == [False]


def test_startup_lock_probe_releases_success_and_rejects_unsafe_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released: list[int | None] = []
    monkeypatch.setattr(agent, "_acquire_maintenance_shared_lock", lambda *, blocking: 87)
    monkeypatch.setattr(agent, "_release_maintenance_lock", released.append)

    agent._probe_maintenance_lock_for_startup()

    assert released == [87]

    monkeypatch.setattr(
        agent,
        "_acquire_maintenance_shared_lock",
        lambda *, blocking: (_ for _ in ()).throw(agent._MaintenanceLockError("unsafe")),
    )
    with pytest.raises(agent._MaintenanceLockError, match="unsafe"):
        agent._probe_maintenance_lock_for_startup()
