from __future__ import annotations

import itertools
import queue
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

import pytest

import agent


class FakeProcess:
    def __init__(
        self,
        *,
        returncode: int | None = None,
        on_first_poll: Callable[[], None] | None = None,
        wait_times_out_until_killed: bool = False,
    ) -> None:
        self.pid = 4242
        self.stdout: tuple[str, ...] = ()
        self.returncode = returncode
        self.on_first_poll = on_first_poll
        self.wait_times_out_until_killed = wait_times_out_until_killed
        self.poll_calls = 0
        self.wait_timeouts: list[float | None] = []
        self.killed = False

    def poll(self) -> int | None:
        self.poll_calls += 1
        if self.poll_calls == 1 and self.on_first_poll is not None:
            self.on_first_poll()
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if self.wait_times_out_until_killed and not self.killed:
            raise subprocess.TimeoutExpired(cmd=["deploy.sh"], timeout=timeout)
        return self.returncode if self.returncode is not None else 0


@pytest.fixture
def deploy_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[agent.DeployProject, list[int | None]]:
    project = agent._project_from_config(
        {
            "key": "api",
            "name": "API",
            "workdir": str(tmp_path),
            "script": "deploy.sh",
            "timeout_seconds": 5,
            "enabled": True,
        },
        "api",
    )
    state: dict[str, Any] = {
        "running": False,
        "queue_size": 0,
        "current_deploy": None,
        "last_deploy": None,
        "history": [],
    }
    released_locks: list[int | None] = []

    monkeypatch.setattr(agent, "PROJECTS", {project.key: project})
    monkeypatch.setattr(agent, "DEFAULT_PROJECT_KEY", project.key)
    monkeypatch.setattr(agent, "_jobs", queue.Queue(maxsize=4))
    monkeypatch.setattr(agent, "_running_lock", threading.Lock())
    monkeypatch.setattr(agent, "_deploy_process_lock", threading.Lock())
    monkeypatch.setattr(agent, "_deploy_process", None)
    monkeypatch.setattr(agent, "_deploy_worker_thread", None)
    monkeypatch.setattr(agent, "_cancel_requested", None)
    monkeypatch.setattr(agent, "_state", state)
    monkeypatch.setattr(agent, "_write_state", lambda: None)
    monkeypatch.setattr(agent, "_read_process_output", lambda proc, stop_event: None)
    monkeypatch.setattr(agent, "_notify_deploy_finished", lambda entry: None)
    monkeypatch.setattr(agent, "_log", lambda message: None)
    monkeypatch.setattr(agent, "_acquire_maintenance_shared_lock", lambda *, blocking: 91)
    monkeypatch.setattr(agent, "_release_maintenance_lock", released_locks.append)

    return project, released_locks


def test_successful_deploy_records_history_and_cleans_runtime_state(
    monkeypatch: pytest.MonkeyPatch,
    deploy_runtime: tuple[agent.DeployProject, list[int | None]],
) -> None:
    project, released_locks = deploy_runtime
    process = FakeProcess(returncode=0)
    monkeypatch.setattr(agent.subprocess, "Popen", lambda *args, **kwargs: process)
    job = {
        "project_key": project.key,
        "after": "abc123",
        "source": "manual",
        "actor": "maintainer",
    }

    agent._run_deploy(job)

    history = agent._state["history"]
    assert len(history) == 1
    assert history[0]["status"] == "success"
    assert history[0]["exit_code"] == 0
    assert history[0]["after"] == "abc123"
    assert agent._state["last_deploy"] == history[0]
    assert agent._state["running"] is False
    assert agent._state["current_deploy"] is None
    assert not agent._running_lock.locked()
    assert agent._deploy_process is None
    assert agent._deploy_worker_thread is None
    assert agent._JOB_MAINTENANCE_LOCK_KEY not in job
    assert released_locks == [91]


def test_failure_advice_persists_with_its_deployment(monkeypatch, deploy_runtime):
    project, _ = deploy_runtime
    process = FakeProcess(returncode=1)
    monkeypatch.setattr(agent.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(agent, "_read_process_output", lambda proc, stop: proc._mini_deploy_diagnostic_tail.append("Address already in use"))
    agent._run_deploy({"project_key": project.key, "source": "manual", "after": "failed-commit"})
    assert agent._state["last_deploy"]["diagnosis"][0]["code"] == "port"
    assert agent._state["history"][0]["after"] == "failed-commit"
    process.returncode = 0
    agent._run_deploy({"project_key": project.key, "source": "manual", "after": "next-commit"})
    assert "diagnosis" not in agent._state["last_deploy"]
    assert not agent._running_lock.locked()


def test_canceled_deploy_returns_130_and_terminates_process(
    monkeypatch: pytest.MonkeyPatch,
    deploy_runtime: tuple[agent.DeployProject, list[int | None]],
) -> None:
    project, _released_locks = deploy_runtime

    def request_cancel() -> None:
        agent._cancel_requested = {"actor": "operator", "project_key": project.key}

    process = FakeProcess(on_first_poll=request_cancel)
    terminated: list[FakeProcess] = []

    def terminate(candidate: FakeProcess) -> None:
        terminated.append(candidate)
        candidate.returncode = -15

    monkeypatch.setattr(agent.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(agent, "_terminate_process", terminate)
    monkeypatch.setattr(agent.time, "sleep", lambda seconds: None)

    agent._run_deploy({"project_key": project.key, "after": "cancel-me"})

    entry = agent._state["last_deploy"]
    assert entry["status"] == "canceled"
    assert entry["phase"] == "canceled"
    assert entry["exit_code"] == 130
    assert terminated == [process]
    assert process.wait_timeouts == [10]
    assert agent._state["running"] is False
    assert agent._state["current_deploy"] is None
    assert agent._deploy_process is None
    assert not agent._running_lock.locked()


def test_timed_out_deploy_returns_124_and_escalates_to_kill(
    monkeypatch: pytest.MonkeyPatch,
    deploy_runtime: tuple[agent.DeployProject, list[int | None]],
) -> None:
    project, _released_locks = deploy_runtime
    process = FakeProcess(wait_times_out_until_killed=True)
    terminated: list[FakeProcess] = []
    killed: list[FakeProcess] = []
    clock = itertools.count(start=100.0, step=2.0)

    def terminate(candidate: FakeProcess) -> None:
        terminated.append(candidate)

    def kill(candidate: FakeProcess) -> None:
        killed.append(candidate)
        candidate.killed = True
        candidate.returncode = -9

    monkeypatch.setattr(agent.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(agent, "_terminate_process", terminate)
    monkeypatch.setattr(agent, "_kill_process", kill)
    monkeypatch.setattr(agent.time, "time", lambda: next(clock))
    monkeypatch.setattr(agent.time, "sleep", lambda seconds: None)

    agent._run_deploy({"project_key": project.key, "after": "too-slow"})

    entry = agent._state["last_deploy"]
    assert entry["status"] == "failed"
    assert entry["phase"] == "timeout"
    assert entry["exit_code"] == 124
    assert terminated == [process]
    assert killed == [process]
    assert process.wait_timeouts == [10, 10]
    assert agent._state["running"] is False
    assert agent._state["current_deploy"] is None
    assert agent._deploy_process is None
    assert not agent._running_lock.locked()


def test_worker_continues_after_job_exception_and_releases_each_job_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StopWorker(RuntimeError):
        pass

    jobs: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=2)
    first = {"project_key": "first", agent._JOB_MAINTENANCE_LOCK_KEY: 51}
    second = {"project_key": "second", agent._JOB_MAINTENANCE_LOCK_KEY: 52}
    jobs.put_nowait(first)
    jobs.put_nowait(second)
    attempted: list[str] = []
    released: list[int | None] = []

    def dequeue() -> dict[str, Any]:
        if jobs.empty():
            raise StopWorker("worker observed both jobs")
        return jobs.get_nowait()

    def run(job: dict[str, Any]) -> None:
        attempted.append(str(job["project_key"]))
        if job is first:
            raise RuntimeError("simulated job failure")

    monkeypatch.setattr(agent, "_jobs", jobs)
    monkeypatch.setattr(agent, "_dequeue_job", dequeue)
    monkeypatch.setattr(agent, "_run_deploy", run)
    monkeypatch.setattr(agent, "_release_deploy_worker_lifecycle", lambda: None)
    monkeypatch.setattr(agent, "_release_maintenance_lock", released.append)
    monkeypatch.setattr(agent, "_log", lambda message: None)

    with pytest.raises(StopWorker, match="observed both jobs"):
        agent._worker()

    assert attempted == ["first", "second"]
    assert released == [51, 52]
    assert agent._JOB_MAINTENANCE_LOCK_KEY not in first
    assert agent._JOB_MAINTENANCE_LOCK_KEY not in second
    assert jobs.unfinished_tasks == 0


def test_canceling_one_projects_queue_preserves_other_jobs_and_locks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    jobs: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=4)
    descriptors = iter([61, 62, 63])
    released: list[int] = []
    state_updates: list[dict[str, Any]] = []
    api = agent._project_from_config({"key": "api", "workdir": str(tmp_path)}, "api")
    worker = agent._project_from_config({"key": "worker", "workdir": str(tmp_path)}, "worker")
    api_first = {"project_key": api.key, "after": "api-1"}
    worker_job = {"project_key": worker.key, "after": "worker-1"}
    api_second = {"project_key": api.key, "after": "api-2"}

    monkeypatch.setattr(agent, "_jobs", jobs)
    monkeypatch.setattr(agent, "_jobs_admin_lock", threading.Lock())
    monkeypatch.setattr(agent, "_acquire_maintenance_shared_lock", lambda *, blocking: next(descriptors))
    monkeypatch.setattr(
        agent,
        "_release_maintenance_lock",
        lambda descriptor: released.append(descriptor) if descriptor is not None else None,
    )
    monkeypatch.setattr(agent, "_update_state", lambda **changes: state_updates.append(changes))

    for job in (api_first, worker_job, api_second):
        agent._enqueue_job(job)

    canceled = agent._cancel_queued_jobs(api)

    assert [job["after"] for job in canceled] == ["api-1", "api-2"]
    assert released == [61, 63]
    assert agent._JOB_MAINTENANCE_LOCK_KEY not in api_first
    assert agent._JOB_MAINTENANCE_LOCK_KEY not in api_second
    remaining = agent._queued_jobs_snapshot()
    assert remaining == [{"project_key": "worker", "after": "worker-1"}]
    assert worker_job[agent._JOB_MAINTENANCE_LOCK_KEY] == 62
    assert state_updates == [{"queue_size": 1}]

    agent._cancel_queued_jobs()
    assert released == [61, 63, 62]
    assert jobs.unfinished_tasks == 0
