from __future__ import annotations

import io
import json
import os
import queue
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
def test_enqueue_rejects_active_maintenance_without_leaking_job_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "_jobs", queue.Queue(maxsize=2))
    descriptor = _exclusive_lock()
    job = {"project_key": "api"}
    try:
        with pytest.raises(agent._MaintenanceActiveError):
            agent._enqueue_job(job)
    finally:
        _release_exclusive_lock(descriptor)

    assert agent._jobs.empty()
    assert agent._JOB_MAINTENANCE_LOCK_KEY not in job


@LINUX_FLOCK_ONLY
@pytest.mark.parametrize("parent_state", ["missing", "public"])
def test_enqueue_fails_closed_for_unsafe_lock_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    parent_state: str,
) -> None:
    lock_directory = tmp_path / parent_state
    if parent_state == "public":
        lock_directory.mkdir(mode=0o755)
        os.chmod(lock_directory, 0o755)
    monkeypatch.setattr(agent, "MAINTENANCE_LOCK_FILE", lock_directory / "maintenance.lock")
    monkeypatch.setattr(agent, "_jobs", queue.Queue(maxsize=2))
    job = {"project_key": "api"}

    with pytest.raises(agent._MaintenanceLockError, match="maintenance lock directory"):
        agent._enqueue_job(job)

    assert agent._jobs.empty()
    assert agent._JOB_MAINTENANCE_LOCK_KEY not in job


@LINUX_FLOCK_ONLY
@pytest.mark.parametrize("file_state", ["public", "symlink"])
def test_enqueue_fails_closed_for_unsafe_lock_file(
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
    monkeypatch.setattr(agent, "_jobs", queue.Queue(maxsize=2))

    with pytest.raises(agent._MaintenanceLockError, match="maintenance lock file"):
        agent._enqueue_job({"project_key": "api"})

    assert agent._jobs.empty()


@LINUX_FLOCK_ONLY
def test_cancel_releases_canceled_job_locks_but_preserves_kept_job_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    jobs: queue.Queue[dict[str, object]] = queue.Queue(maxsize=2)
    monkeypatch.setattr(agent, "_jobs", jobs)
    monkeypatch.setattr(agent, "_update_state", lambda **changes: None)
    first = {"project_key": "first"}
    second = {"project_key": "second"}
    agent._enqueue_job(first)
    agent._enqueue_job(second)
    first_project = agent._project_from_config(
        {"key": "first", "workdir": str(tmp_path), "enabled": False},
        "first",
    )

    canceled = agent._cancel_queued_jobs(first_project)

    assert canceled == [{"project_key": "first"}]
    assert agent._JOB_MAINTENANCE_LOCK_KEY not in first
    probe = os.open(agent.MAINTENANCE_LOCK_FILE, os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            agent.fcntl.flock(probe, agent.fcntl.LOCK_EX | agent.fcntl.LOCK_NB)
        agent._cancel_queued_jobs()
        agent.fcntl.flock(probe, agent.fcntl.LOCK_EX | agent.fcntl.LOCK_NB)
    finally:
        _release_exclusive_lock(probe)

    assert agent._JOB_MAINTENANCE_LOCK_KEY not in second


def test_queue_full_releases_only_the_rejected_job_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
    descriptors = iter([41, 42])
    released: list[int | None] = []
    monkeypatch.setattr(agent, "_jobs", jobs)
    monkeypatch.setattr(agent, "_update_state", lambda **changes: None)
    monkeypatch.setattr(agent, "_acquire_maintenance_shared_lock", lambda *, blocking: next(descriptors))
    monkeypatch.setattr(agent, "_release_maintenance_lock", released.append)
    accepted = {"project_key": "accepted"}
    rejected = {"project_key": "rejected"}

    agent._enqueue_job(accepted)
    with pytest.raises(queue.Full):
        agent._enqueue_job(rejected)

    assert released == [None, 42]
    assert accepted[agent._JOB_MAINTENANCE_LOCK_KEY] == 41
    assert agent._JOB_MAINTENANCE_LOCK_KEY not in rejected
    agent._cancel_queued_jobs()
    assert released == [None, 42, 41]


def test_queued_job_snapshot_never_exposes_internal_lock_descriptor(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
    jobs.put_nowait({
        "project_key": "api",
        agent._JOB_MAINTENANCE_LOCK_KEY: 99,
        agent._JOB_DEPLOY_LIFECYCLE_OWNER_KEY: True,
    })
    monkeypatch.setattr(agent, "_jobs", jobs)

    assert agent._queued_jobs_snapshot() == [{"project_key": "api"}]


def test_orphan_process_reaper_owns_job_lock_until_process_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    class Process:
        pid = 4242
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float) -> int:
            assert timeout == agent._ORPHAN_REAPER_WAIT_SECONDS
            self.returncode = 137
            return self.returncode

    class ImmediateThread:
        def __init__(self, *, target, args, name: str, daemon: bool) -> None:
            del name
            assert daemon
            self.target = target
            self.args = args

        def start(self) -> None:
            self.target(*self.args)

    process = Process()
    released: list[int | None] = []
    messages: list[str] = []
    job = {
        "project_key": "api",
        agent._JOB_MAINTENANCE_LOCK_KEY: 101,
        agent._JOB_DEPLOY_LIFECYCLE_OWNER_KEY: True,
    }
    monkeypatch.setattr(agent, "fcntl", object())
    monkeypatch.setattr(agent, "_deploy_process", process)
    monkeypatch.setattr(agent.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(agent, "_release_maintenance_lock", released.append)
    monkeypatch.setattr(agent, "_update_state", lambda **changes: None)
    monkeypatch.setattr(agent, "_log", messages.append)

    agent._retain_job_lock_for_orphan_process(job)

    assert released == [101]
    assert agent._JOB_MAINTENANCE_LOCK_KEY not in job
    assert agent._JOB_DEPLOY_LIFECYCLE_OWNER_KEY not in job
    assert any("maintenance remains blocked" in message for message in messages)


def test_orphan_reaper_start_failure_waits_synchronously_and_releases_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        pid = 4343
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float) -> int:
            del timeout
            self.returncode = 0
            return self.returncode

    class FailingThread:
        def __init__(self, **kwargs: object) -> None:
            del kwargs
            raise RuntimeError("thread unavailable")

    released: list[int | None] = []
    job = {
        agent._JOB_MAINTENANCE_LOCK_KEY: 102,
        agent._JOB_DEPLOY_LIFECYCLE_OWNER_KEY: True,
    }
    monkeypatch.setattr(agent, "fcntl", object())
    monkeypatch.setattr(agent, "_deploy_process", Process())
    monkeypatch.setattr(agent.threading, "Thread", FailingThread)
    monkeypatch.setattr(agent, "_release_maintenance_lock", released.append)
    monkeypatch.setattr(agent, "_update_state", lambda **changes: None)
    monkeypatch.setattr(agent, "_log", lambda message: None)

    agent._retain_job_lock_for_orphan_process(job)

    assert released == [102]
    assert agent._JOB_MAINTENANCE_LOCK_KEY not in job


def test_waiting_job_is_not_lost_when_completed_orphan_releases_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        returncode = 137

        def poll(self) -> int:
            return self.returncode

    process = Process()
    running_lock = threading.Lock()
    running_lock.acquire()
    process_lock = threading.Lock()
    waiting_job: dict[str, object] = {"project_key": "next"}
    acquired = threading.Event()
    finish = threading.Event()
    state_updates: list[dict[str, object]] = []

    monkeypatch.setattr(agent, "_running_lock", running_lock)
    monkeypatch.setattr(agent, "_deploy_process_lock", process_lock)
    monkeypatch.setattr(agent, "_deploy_process", process)
    monkeypatch.setattr(agent, "_deploy_worker_thread", None)
    monkeypatch.setattr(agent, "_cancel_requested", {"actor": "old"})
    monkeypatch.setattr(agent, "_jobs", queue.Queue())
    monkeypatch.setattr(agent, "_update_state", lambda **changes: state_updates.append(changes))
    monkeypatch.setattr(agent, "_log", lambda message: None)

    def wait_for_lifecycle() -> None:
        agent._acquire_deploy_worker_lifecycle(waiting_job)
        acquired.set()
        finish.wait(timeout=2)
        agent._release_deploy_worker_lifecycle()

    waiter = threading.Thread(target=wait_for_lifecycle)
    waiter.start()
    assert not acquired.wait(timeout=0.1)

    agent._clear_completed_orphan_lifecycle(process)
    assert acquired.wait(timeout=2)
    assert waiting_job[agent._JOB_DEPLOY_LIFECYCLE_OWNER_KEY] is True
    assert state_updates == [{"running": False, "current_deploy": None, "queue_size": 0}]
    assert agent._deploy_process is None
    assert agent._cancel_requested is None

    finish.set()
    waiter.join(timeout=2)
    assert not waiter.is_alive()
    assert not running_lock.locked()


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


def test_orphan_reaper_releases_lock_when_logging_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    class Process:
        pid = 4444
        returncode: int | None = None
        wait_calls = 0

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float) -> int:
            assert timeout == agent._ORPHAN_REAPER_WAIT_SECONDS
            self.wait_calls += 1
            self.returncode = 0
            return self.returncode

    process = Process()
    released: list[int | None] = []
    cleared: list[object] = []
    monkeypatch.setattr(
        agent,
        "_log",
        lambda message: (_ for _ in ()).throw(BrokenPipeError("stdout is closed")),
    )
    monkeypatch.setattr(agent, "_clear_completed_orphan_lifecycle", cleared.append)
    monkeypatch.setattr(agent, "_release_maintenance_lock", released.append)

    agent._reap_orphan_process_maintenance_lock(process, 103)

    assert process.wait_calls == 1
    assert cleared == [process]
    assert released == [103]


def test_worker_releases_job_lock_after_unexpected_deploy_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
    job = {"project_key": "api", agent._JOB_MAINTENANCE_LOCK_KEY: 55}
    jobs.put_nowait(job)
    released: list[int | None] = []
    calls = 0

    def dequeue_once() -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return jobs.get_nowait()
        raise RuntimeError("stop worker test")

    def fail_deploy(queued_job: dict[str, object]) -> None:
        assert queued_job is job
        raise RuntimeError("simulated deploy failure")

    monkeypatch.setattr(agent, "_jobs", jobs)
    monkeypatch.setattr(agent, "_dequeue_job", dequeue_once)
    monkeypatch.setattr(agent, "_run_deploy", fail_deploy)
    monkeypatch.setattr(agent, "_release_deploy_worker_lifecycle", lambda: None)
    monkeypatch.setattr(agent, "_release_maintenance_lock", released.append)

    with pytest.raises(RuntimeError, match="stop worker test"):
        agent._worker()

    assert released == [55]
    assert agent._JOB_MAINTENANCE_LOCK_KEY not in job
    assert jobs.unfinished_tasks == 0


def test_worker_releases_job_lock_when_lifecycle_cleanup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
    job = {"project_key": "api", agent._JOB_MAINTENANCE_LOCK_KEY: 61}
    jobs.put_nowait(job)
    released: list[int | None] = []
    monkeypatch.setattr(agent, "_jobs", jobs)
    monkeypatch.setattr(agent, "_dequeue_job", jobs.get_nowait)
    monkeypatch.setattr(agent, "_run_deploy", lambda queued_job: None)
    monkeypatch.setattr(
        agent,
        "_release_deploy_worker_lifecycle",
        lambda: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
    )
    monkeypatch.setattr(agent, "_release_maintenance_lock", released.append)

    with pytest.raises(RuntimeError, match="cleanup failed"):
        agent._worker()

    assert released == [61]
    assert agent._JOB_MAINTENANCE_LOCK_KEY not in job
    assert jobs.unfinished_tasks == 0


def test_direct_run_deploy_owns_and_releases_its_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    job = {"project_key": "api"}
    released: list[int | None] = []
    monkeypatch.setattr(agent, "_acquire_maintenance_shared_lock", lambda *, blocking: 73)
    monkeypatch.setattr(agent, "_release_maintenance_lock", released.append)
    monkeypatch.setattr(agent, "_run_deploy_locked", lambda queued_job: (_ for _ in ()).throw(RuntimeError("failed")))

    with pytest.raises(RuntimeError, match="failed"):
        agent._run_deploy(job)

    assert released == [73]
    assert agent._JOB_MAINTENANCE_LOCK_KEY not in job


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


def test_manual_deploy_returns_503_when_maintenance_is_active(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = agent._project_from_config(
        {
            "key": "api",
            "workdir": str(tmp_path),
            "webhook_secret": "01" * 32,
            "enabled": True,
            "manual_deploy_enabled": True,
        },
        "api",
    )
    responses: list[tuple[int, dict[str, object]]] = []
    handler = object.__new__(agent.Handler)
    handler.client_address = ("198.51.100.10", 1234)
    handler._write_json = lambda status, payload: responses.append((status, payload))
    monkeypatch.setattr(agent, "PROJECTS", {"api": project})
    monkeypatch.setattr(agent, "DEFAULT_PROJECT_KEY", "api")
    monkeypatch.setattr(agent, "_jobs", queue.Queue(maxsize=2))
    monkeypatch.setattr(agent, "_manual_deploy_job", lambda actor, selected: {"project_key": selected.key})
    monkeypatch.setattr(
        agent,
        "_enqueue_job",
        lambda job: (_ for _ in ()).throw(agent._MaintenanceActiveError("maintenance")),
    )

    agent.Handler._handle_redeploy(handler, {"project": ["api"]})

    assert responses == [(503, {"error": "maintenance_in_progress"})]


def test_manual_deploy_returns_503_when_maintenance_lock_is_unsafe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = agent._project_from_config(
        {
            "key": "api",
            "workdir": str(tmp_path),
            "webhook_secret": "01" * 32,
            "enabled": True,
            "manual_deploy_enabled": True,
        },
        "api",
    )
    responses: list[tuple[int, dict[str, object]]] = []
    handler = object.__new__(agent.Handler)
    handler.client_address = ("198.51.100.10", 1234)
    handler._write_json = lambda status, payload: responses.append((status, payload))
    monkeypatch.setattr(agent, "PROJECTS", {"api": project})
    monkeypatch.setattr(agent, "DEFAULT_PROJECT_KEY", "api")
    monkeypatch.setattr(agent, "_jobs", queue.Queue(maxsize=2))
    monkeypatch.setattr(agent, "_manual_deploy_job", lambda actor, selected: {"project_key": selected.key})
    monkeypatch.setattr(agent, "_log", lambda message: None)
    monkeypatch.setattr(
        agent,
        "_enqueue_job",
        lambda job: (_ for _ in ()).throw(agent._MaintenanceLockError("unsafe")),
    )

    agent.Handler._handle_redeploy(handler, {"project": ["api"]})

    assert responses == [(503, {"error": "maintenance_lock_unavailable"})]


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


def test_webhook_returns_503_when_maintenance_is_active(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret = "test-webhook-secret-0123456789abcdef"
    project = agent._project_from_config(
        {
            "key": "api",
            "branch": "main",
            "workdir": str(tmp_path),
            "webhook_secret": secret,
            "enabled": True,
        },
        "api",
    )
    body = json.dumps({"ref": "refs/heads/main", "after": "abc123"}).encode()
    responses: list[tuple[int, dict[str, object]]] = []
    handler = object.__new__(agent.Handler)
    handler.headers = {"Content-Length": str(len(body)), "X-Gitee-Token": secret}
    handler.rfile = io.BytesIO(body)
    handler.client_address = ("198.51.100.10", 1234)
    handler._write_json = lambda status, payload: responses.append((status, payload))
    monkeypatch.setattr(agent, "PROJECTS", {"api": project})
    monkeypatch.setattr(agent, "DEFAULT_PROJECT_KEY", "api")
    monkeypatch.setattr(agent, "_jobs", queue.Queue(maxsize=2))
    monkeypatch.setattr(
        agent,
        "_enqueue_job",
        lambda job: (_ for _ in ()).throw(agent._MaintenanceActiveError("maintenance")),
    )

    agent.Handler._handle_webhook(handler, agent.urlparse("/webhook?project=api"))

    assert responses == [(503, {"error": "maintenance_in_progress"})]
