import copy
import io
import json
import subprocess
import threading
import time

import pytest

import container_monitoring
import docker_events
import monitoring


CONTAINER_ID = "a" * 64
STAMP = 9_999_123_456_789


def payload(action="start", stamp=STAMP, **attributes):
    return {"Type": "container", "Action": action, "time": stamp // 1_000_000_000, "timeNano": stamp,
            "Actor": {"ID": CONTAINER_ID, "Attributes": {"name": "api-1", **attributes}}}


def line(event):
    return json.dumps(event).encode() + b"\n"


@pytest.fixture
def monitor(tmp_path):
    return monitoring.Monitor(tmp_path / "monitor.json", clock=lambda: 10_000.0)


def test_history_dedup_persists_original_time_without_per_event_flush(monitor, monkeypatch):
    event = payload("die", exitCode="137")
    with monkeypatch.context() as patch:
        patch.setattr(monitor, "flush", lambda: pytest.fail("observer must leave flushing to Monitor"))
        assert docker_events.consume(monitor, event)
        assert not docker_events.consume(monitor, event)
    history = monitor.events[0]
    assert history["kind"] == "lifecycle"
    assert history["source"] == "docker"
    assert history["at_ts"] == STAMP / 1_000_000_000
    assert history["at"] == time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(STAMP / 1_000_000_000))
    assert "退出" in history["title"]
    assert "退出码 137" in history["detail"]
    assert "OOM" not in history["title"] + history["detail"]
    monitor.flush()
    restored = monitoring.Monitor(monitor.path, monitor.clock)
    assert not docker_events.consume(restored, event)
    assert len(restored.events) == 1
    assert restored.container_state[docker_events.STATE_KEY]["cursor"] == STAMP
    assert not restored.coverage


@pytest.mark.parametrize("action", sorted(docker_events.ACTIONS) + ["health_status: healthy", "health_status: unhealthy"])
def test_lifecycle_never_raises_or_resolves_faults(monitor, action):
    monitor.observe("existing", True, "fault", "evidence", "docker", immediate=True)
    before_incidents, before_pending = copy.deepcopy(monitor.incidents), copy.deepcopy(monitor.pending)
    assert docker_events.consume(monitor, payload(action, exitCode="137"))
    assert monitor.incidents == before_incidents
    assert monitor.pending == before_pending
    assert [entry["kind"] for entry in monitor.events] == ["alert", "lifecycle"]


def test_selected_compose_labels_use_shared_identity_and_oneoff_full_id(monitor):
    labels = {"com.docker.compose.project": "demo", "com.docker.compose.service": "api",
              "com.docker.compose.container-number": "2", "com.docker.compose.oneoff": "False"}
    assert docker_events.consume(monitor, payload(**labels))
    metadata = monitor.events[-1]["metadata"]
    evidence = {"id": CONTAINER_ID, "compose_project": "demo", "compose_service": "api",
                "compose_replica": "2", "compose_oneoff": False}
    assert metadata["identity"] == container_monitoring.container_identity(evidence)
    assert metadata["compose_replica"] == "2"
    assert metadata["compose_oneoff"] is False
    labels["com.docker.compose.oneoff"] = "True"
    docker_events.consume(monitor, payload("restart", **labels))
    assert monitor.events[-1]["metadata"]["identity"] == container_monitoring.container_identity({"id": CONTAINER_ID})


def test_secrets_and_invalid_selected_attribute_values_never_persist(monitor):
    secret = "DO_NOT_PERSIST_PASSWORD"
    event = payload("oom", **{"password": secret, "Env": [secret], "health_status": secret,
                            "name": {"secret": secret}, "exitCode": secret,
                            "com.docker.compose.project": secret + "\n",
                            "com.docker.compose.service": [secret],
                            "com.docker.compose.container-number": secret,
                            "com.docker.compose.oneoff": secret})
    event.update(Env=[secret], status=secret, fromImage=secret)
    assert docker_events.consume(monitor, event)
    assert set(monitor.events[-1]["metadata"]) == {"action", "container_id", "time_nano", "identity"}
    monitor.flush()
    assert secret not in monitor.path.read_text(encoding="utf-8")
    assert "内存不足" in monitor.events[-1]["title"]


@pytest.mark.parametrize("invalid", [None, [], "secret", {},
    {"Type": "image"}, {"Action": "exec_create: secret"}, {"Action": {"secret": True}},
    {"Action": "health_status: secret"}, {"Actor": []},
    {"Actor": {"ID": "secret"}}, {"Actor": {"ID": [CONTAINER_ID]}},
    {"timeNano": True, "time": False}, {"timeNano": -1, "time": -1},
    {"timeNano": 1.5, "time": float("nan")}, {"timeNano": "secret", "time": "secret"},
    {"timeNano": 2**63, "time": 2**63}, {"timeNano": "9" * 100, "time": "9" * 100}])
def test_invalid_events_do_not_change_state(monitor, invalid):
    event = {**payload(), **invalid} if isinstance(invalid, dict) and invalid else invalid
    assert not docker_events.consume(monitor, event)
    assert not monitor.events
    assert not monitor.container_state


@pytest.mark.parametrize("exit_code", [True, False, -1, 256, "256", 137.0, {}, "secret"])
def test_invalid_exit_codes_are_omitted(monitor, exit_code):
    assert docker_events.consume(monitor, payload("die", exitCode=exit_code))
    assert "exit_code" not in monitor.events[-1]["metadata"]


def test_seconds_fallback_and_distinct_events_same_time(monitor):
    event = payload()
    event.pop("timeNano")
    event["time"] = "9999"
    assert docker_events.consume(monitor, event)
    assert monitor.events[-1]["at_ts"] == 9999
    for action in ("oom", "die", "health_status: healthy", "health_status: unhealthy"):
        event["Action"] = action
        assert docker_events.consume(monitor, event)
    assert len(monitor.events) == 5


def test_dedup_ledger_bounded_without_touching_container_worker_state(monitor):
    foreign = {"docker:container:other": {"last_seen": 0, "expected_running": True},
               "event:other": {"owned_elsewhere": True}}
    monitor.container_state.update(copy.deepcopy(foreign))
    for offset in range(2005):
        assert docker_events.consume(monitor, payload(stamp=STAMP + offset))
    ledger = monitor.container_state[docker_events.STATE_KEY]
    assert len(ledger["ids"]) == 2000
    assert min(ledger["ids"].values()) == STAMP + 5
    assert ledger["cursor"] == STAMP + 2004
    assert all(monitor.container_state[key] == value for key, value in foreign.items())
    assert all(isinstance(value, dict) for value in monitor.container_state.values())
    assert not docker_events.consume(monitor, payload(stamp=STAMP + 2004))


def test_replayed_history_obeys_monitor_retention(monitor):
    monitor.clock = lambda: 40 * 86400.0
    assert docker_events.consume(monitor, payload())
    assert not monitor.events
    assert len(monitor.container_state[docker_events.STATE_KEY]["ids"]) == 1


class FakePipe(io.BytesIO):
    def fileno(self):
        return id(self)


class FakeProcess:
    def __init__(self, data, *, idle=False, stubborn=False):
        self.stdout = FakePipe(data)
        self.idle, self.stubborn = idle, stubborn
        self.ended = self.entered = self.exited = self.terminated = self.killed = False
        self.waits = []

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *_):
        self.exited = True
        self.stdout.close()

    def poll(self):
        return 0 if self.ended else None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout):
        self.waits.append(timeout)
        if self.stubborn and not self.killed:
            raise subprocess.TimeoutExpired("fake", timeout)
        self.ended = True


@pytest.fixture
def stream(monkeypatch):
    processes, calls, queue, read_sizes, modes = [], [], [], [], []

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        assert queue, "unexpected reconnect"
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        processes.append(item)
        return item

    def read(descriptor, size):
        read_sizes.append(size)
        process = next(item for item in processes if item.stdout.fileno() == descriptor)
        result = process.stdout.read(size)
        if not result and process.idle:
            raise BlockingIOError()
        if not result:
            process.ended = True
        return result

    monkeypatch.setattr(docker_events.os, "read", read)
    monkeypatch.setattr(docker_events.os, "set_blocking", lambda fd, mode: modes.append(mode))
    return {"popen": popen, "queue": queue, "calls": calls, "processes": processes,
            "read_sizes": read_sizes, "modes": modes}


def test_stream_reconnects_with_exact_cursor_dedup_and_minimal_template(monitor, stream):
    first, second = payload(), payload("die", STAMP + 1, exitCode="137")
    stream["queue"] += [FakeProcess(line(first)), FakeProcess(line(first) + line(second))]
    stop = threading.Event()
    pauses, statuses = [], []

    def pause(seconds):
        pauses.append(seconds)
        statuses.append(copy.deepcopy(monitor.coverage["docker_events"]))
        if len(pauses) == 2:
            stop.set()

    docker_events.run(lambda: monitor, which=lambda name: "/usr/bin/docker", popen=stream["popen"],
                      pause=pause, stop_event=stop)
    assert pauses == [5, 5]
    assert [entry["metadata"]["action"] for entry in monitor.events] == ["start", "die"]
    first_command, kwargs = stream["calls"][0]
    second_command, _ = stream["calls"][1]
    assert first_command[:5] == ["/usr/bin/docker", "events", "--filter", "type=container", "--format"]
    assert first_command[-2:] == ["--since", "10000.000000000"]
    # The cursor cannot move backwards if an earlier buffered event is delivered.
    assert second_command[-2:] == ["--since", "10000.000000000"]
    assert kwargs == {"stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL, "bufsize": 0}
    template = first_command[5]
    assert "{{json .Actor.Attributes}}" not in template
    assert "Env" not in template
    for attribute in (*docker_events.ATTRIBUTE_FIELDS, "exitCode", "com.docker.compose.container-number",
                      "com.docker.compose.oneoff"):
        assert f'index .Actor.Attributes "{attribute}"' in template
    assert all(item["status"] == "unknown" and "checked_at" in item and "detail" in item for item in statuses)
    assert all(item.entered and item.exited for item in stream["processes"])
    assert stream["modes"] == [False, False]


def test_stream_loads_persisted_cursor_and_advances_fractional_nanoseconds(monitor, stream):
    docker_events.consume(monitor, payload())
    monitor.flush()
    monitor = monitoring.Monitor(monitor.path, monitor.clock)
    stream["queue"] += [FakeProcess(line(payload("oom", STAMP + 7))), FakeProcess(b"")]
    stop = threading.Event()

    def pause(_):
        if len(stream["calls"]) == 2:
            stop.set()

    docker_events.run(lambda: monitor, which=lambda _: "docker", popen=stream["popen"], pause=pause, stop_event=stop)
    assert stream["calls"][0][0][-1] == "9999.123456789"
    assert stream["calls"][1][0][-1] == "9999.123456796"
    assert monitor.container_state[docker_events.STATE_KEY]["cursor"] == STAMP + 7


def test_stream_skips_oversized_lines_until_newline_and_invalid_json(monitor, stream):
    oversized = line(payload(**{"secret": "x" * 50_000}))
    stream["queue"].append(FakeProcess(oversized + b'\xff\n{broken}\n[]\n' + line(payload("pause"))))
    stop = threading.Event()
    docker_events.run(lambda: monitor, which=lambda _: "docker", popen=stream["popen"],
                      pause=lambda _: stop.set(), stop_event=stop)
    assert [item["metadata"]["action"] for item in monitor.events] == ["pause"]
    assert max(stream["read_sizes"]) == docker_events.MAX_LINE_BYTES + 1


@pytest.mark.parametrize("stubborn", [False, True])
def test_idle_stream_stop_cleans_up_without_blocked_reader(monitor, stream, stubborn):
    process = FakeProcess(b"", idle=True, stubborn=stubborn)
    stream["queue"].append(process)
    stop = threading.Event()
    pauses = []

    def pause(seconds):
        pauses.append(seconds)
        coverage = monitor.coverage["docker_events"]
        assert coverage["status"] == "healthy"
        assert "历史回放" in coverage["detail"]
        stop.set()

    docker_events.run(lambda: monitor, which=lambda _: "docker", popen=stream["popen"], pause=pause, stop_event=stop)
    assert pauses == [0.25]
    assert process.entered and process.exited and process.terminated
    assert process.killed == stubborn
    assert process.waits == ([2, 2] if stubborn else [2])
    assert monitor.coverage["docker_events"]["status"] == "unknown"


def test_missing_docker_skips_spawn_and_retries_after_15_seconds(monitor):
    stop = threading.Event()
    pauses = []

    def pause(seconds):
        pauses.append(seconds)
        assert monitor.coverage["docker_events"]["status"] == "not_installed"
        stop.set()

    docker_events.run(lambda: monitor, which=lambda _: None,
                      popen=lambda *_a, **_k: pytest.fail("must not spawn Docker"), pause=pause, stop_event=stop)
    assert pauses == [15]
    assert not monitor.events


def test_spawn_failure_retries_without_exposing_exception_text(monitor, stream):
    stream["queue"].append(OSError("SECRET_STDERR"))
    stop = threading.Event()

    def pause(seconds):
        assert seconds == 5
        assert monitor.coverage["docker_events"]["status"] == "unknown"
        assert "SECRET_STDERR" not in json.dumps(monitor.coverage)
        stop.set()

    docker_events.run(lambda: monitor, which=lambda _: "docker", popen=stream["popen"], pause=pause, stop_event=stop)


def test_stopped_or_unavailable_monitor_does_not_spawn():
    stop = threading.Event()
    stop.set()
    def spawn(*_args, **_kwargs):
        pytest.fail("must not spawn Docker")

    docker_events.run(lambda: pytest.fail("already stopped"), popen=spawn, stop_event=stop)
    stop.clear()
    docker_events.run(lambda: None, popen=spawn, pause=lambda _: stop.set(), stop_event=stop)


def test_idle_stream_refreshes_coverage_every_15_seconds(monitor, stream):
    now = [10_000.0]
    monitor.clock = lambda: now[0]
    stream["queue"].append(FakeProcess(b"", idle=True))
    stop = threading.Event()
    timestamps = []

    def pause(seconds):
        assert seconds == 0.25
        timestamps.append(monitor.coverage["docker_events"]["checked_at"])
        now[0] += 15
        if len(timestamps) == 10:
            stop.set()

    docker_events.run(lambda: monitor, which=lambda _: "docker", popen=stream["popen"], pause=pause, stop_event=stop)
    assert timestamps == [10_000.0 + offset * 15 for offset in range(10)]
    assert not monitor.events


def test_corrupt_monitor_getter_retries_instead_of_ending_observer(monitor, stream):
    stream["queue"].append(FakeProcess(line(payload())))
    stop = threading.Event()
    calls, pauses = [], []

    def getter():
        calls.append(True)
        if len(calls) == 1:
            raise ValueError("SECRET_INVALID_STATE")
        return monitor

    def pause(seconds):
        pauses.append(seconds)
        if len(pauses) == 2:
            stop.set()

    docker_events.run(getter, which=lambda _: "docker", popen=stream["popen"], pause=pause, stop_event=stop)
    assert pauses == [5, 5]
    assert len(monitor.events) == 1
    assert "SECRET_INVALID_STATE" not in json.dumps(monitor.snapshot())


def test_normalization_error_cleans_up_then_retries_without_sensitive_text(monitor, stream, monkeypatch):
    first, second = FakeProcess(line(payload())), FakeProcess(line(payload()))
    stream["queue"] += [first, second]
    original = docker_events._normalize
    calls = []
    stop = threading.Event()

    def normalize(event):
        calls.append(True)
        if len(calls) == 1:
            raise ValueError("SECRET_EVENT_ERROR")
        return original(event)

    def pause(seconds):
        assert seconds == 5
        assert "SECRET_EVENT_ERROR" not in json.dumps(monitor.snapshot())
        if len(stream["calls"]) == 2:
            stop.set()

    monkeypatch.setattr(docker_events, "_normalize", normalize)
    docker_events.run(lambda: monitor, which=lambda _: "docker", popen=stream["popen"], pause=pause, stop_event=stop)
    assert first.terminated and first.exited
    assert second.exited
    assert len(monitor.events) == 1


def test_keyboard_interrupt_propagates_after_process_cleanup(monitor, stream):
    process = FakeProcess(b"", idle=True)
    stream["queue"].append(process)

    def pause(_):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        docker_events.run(lambda: monitor, which=lambda _: "docker", popen=stream["popen"], pause=pause)
    assert process.terminated and process.exited
    assert len(stream["calls"]) == 1


def test_future_event_rejected_without_advancing_stream_cursor(monitor, stream):
    future = payload(stamp=10_006_000_000_000)
    assert not docker_events.consume(monitor, future)
    assert not monitor.container_state
    stream["queue"].append(FakeProcess(line(future) + line(payload())))
    stop = threading.Event()
    docker_events.run(lambda: monitor, which=lambda _: "docker", popen=stream["popen"],
                      pause=lambda _: stop.set(), stop_event=stop)
    assert len(monitor.events) == 1
    assert monitor.events[0]["metadata"]["time_nano"] == STAMP
    ledger = monitor.container_state[docker_events.STATE_KEY]
    assert ledger["cursor"] == 10_000_000_000_000
    assert list(ledger["ids"].values()) == [STAMP]


def test_timestamp_within_five_second_clock_skew_is_accepted(monitor):
    assert docker_events.consume(monitor, payload(stamp=10_005_000_000_000))
