from collections import deque

import agent


def test_realtime_samples_are_bounded_independent_and_do_not_write_disk(monkeypatch):
    monkeypatch.setattr(agent, "_realtime_metrics", deque(maxlen=60))
    monkeypatch.setattr(agent, "_realtime_server", {})
    monkeypatch.setattr(agent, "_cpu_percent", lambda: 12)
    monkeypatch.setattr(agent, "_load_average", lambda: {})
    monkeypatch.setattr(agent, "_memory_status", lambda: {"percent": 23})
    monkeypatch.setattr(agent, "_disk_status", lambda path: {})
    monkeypatch.setattr(agent, "_network_status", lambda: {"total_kbps": 7, "rx_kbps": 4, "tx_kbps": 3})
    clock = [1000]
    monkeypatch.setattr(agent.time, "time", lambda: clock[0])

    def forbidden(*args, **kwargs):
        raise AssertionError("realtime sampling must not run Docker or write persistent state")

    monkeypatch.setattr(agent, "_docker_status", forbidden)
    monkeypatch.setattr(agent, "_write_state", forbidden)
    for _ in range(65):
        agent._sample_realtime_metrics()
        clock[0] += 1
    result = agent._realtime_metrics_payload()
    assert len(result["realtime_history"]) == 60
    assert result["realtime_interval_seconds"] == 1
    assert result["realtime_history"][0]["ts"] == 1064
    assert result["realtime_history"][-1]["ts"] == 1005
    assert result["server"]["memory"]["percent"] == 23
    result["server"]["memory"]["percent"] = 0
    assert agent._realtime_metrics_payload()["server"]["memory"]["percent"] == 23


def test_sampler_accounts_for_work_duration_and_recovers(monkeypatch):
    times = iter([10, 10.2, 11.1, 13.2])
    monkeypatch.setattr(agent.time, "monotonic", lambda: next(times))
    calls, sleeps = [], []

    def sample():
        calls.append(1)
        if len(calls) == 2:
            raise OSError("transient")

    class Finished(Exception):
        pass

    def sleep(delay):
        sleeps.append(round(delay, 1))
        if len(sleeps) == 3:
            raise Finished

    monkeypatch.setattr(agent, "_sample_realtime_metrics", sample)
    monkeypatch.setattr(agent, "_log", lambda message: None)
    monkeypatch.setattr(agent.time, "sleep", sleep)
    try:
        agent._realtime_metric_sampler()
    except Finished:
        pass
    assert len(calls) == 3
    assert sleeps == [0.8, 0.9, 1]


def test_status_reads_sampler_snapshot_without_resampling_counters(monkeypatch):
    monkeypatch.setattr(agent, "_system_status_cache", {})
    monkeypatch.setattr(agent, "_realtime_server", {"cpu_percent": 12})
    monkeypatch.setattr(agent, "_realtime_metrics", deque([{"ts": 1000, "cpu_percent": 12}], maxlen=60))
    monkeypatch.setattr(agent, "_docker_status", lambda: {})
    monkeypatch.setattr(agent, "_record_system_metric", lambda server, now: [])

    def forbidden():
        raise AssertionError("request must not consume counter baselines")

    monkeypatch.setattr(agent, "_cpu_percent", forbidden)
    monkeypatch.setattr(agent, "_network_status", forbidden)
    result = agent._system_status_payload()
    assert result["server"]["cpu_percent"] == 12
    assert result["realtime_history"][0]["ts"] == 1000
