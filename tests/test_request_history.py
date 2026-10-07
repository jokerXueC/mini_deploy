import json
import sqlite3
import subprocess
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import request_history as history

NOW = 1_800_000_000


def line(path="/api/health", *, ts=NOW - 10, status=200, duration="0.010"):
    stamp = datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")
    return stamp + ' mini_deploy_req ' + json.dumps({"at": stamp, "host": "api.test", "method": "GET",
        "path": path, "status": str(status), "duration": duration, "upstream": "0.005"})


def ingest(store, lines, **kwargs):
    store.ingest("api", "API", "container-a", lines, NOW - 1, now=NOW, **kwargs)


def test_restart_overlap_and_identical_requests_are_preserved_once(tmp_path):
    store = history.History(tmp_path)
    lines = [line(), line(), line("/api/users/12")]
    ingest(store, lines)
    ingest(history.History(tmp_path), lines)
    result = store.query("api", now=NOW)
    assert result["total_requests"] == 3
    assert result["total_groups"] == 2
    assert result["groups"][0]["count"] == 2
    assert store.source("api")["cursor"] == NOW - 1
    store.ingest("api", "API", "container-b", [line()], NOW - 1, now=NOW)
    assert store.query("api", now=NOW)["total_requests"] == 4


def test_low_frequency_routes_survive_many_heartbeats_and_statistics_are_full(tmp_path):
    store = history.History(tmp_path)
    lines = [line("/api/rare", ts=NOW - 1000, duration="0.300")]
    lines += [line("/api/heartbeat") for _ in range(1001)]
    ingest(store, lines)
    result = store.query("api", now=NOW)
    assert result["total_requests"] == 1002 and result["total_groups"] == 2
    busy, rare = result["groups"]
    assert busy["count"] == 1001 and len(busy["items"]) == 30 and busy["average"] == 10
    assert rare["count"] == 1 and rare["average"] == 300
    assert result["earliest_at"] == NOW - 1000


def test_search_selects_whole_normalized_group_and_raw_view_keeps_ids(tmp_path):
    store = history.History(tmp_path)
    ingest(store, [line("/users/1", duration="0.100"), line("/users/2", status=500, duration="0.300")])
    result = store.query("api", search="/users/2", status="error", now=NOW)
    assert result["total_requests"] == 2 and result["groups"][0]["average"] == 200
    assert result["groups"][0]["pathCount"] == 2
    assert store.query("api", status="success", now=NOW)["total_groups"] == 0
    assert store.query("api", layout="flat", search="/users/2", now=NOW)["total_requests"] == 1
    assert store.query("api", search="%' OR 1=1 --", now=NOW)["total_groups"] == 0


def test_retention_row_and_byte_limits_prune_only_history(tmp_path):
    business = tmp_path / "business.txt"
    business.write_text("keep")
    store = history.History(tmp_path, max_rows=2, retention=100)
    ingest(store, [line("/expired", ts=NOW - 101), line("/old", ts=NOW - 90),
                   line("/new", ts=NOW - 50), line("/newest", ts=NOW - 20)])
    assert {g["path"] for g in store.query("api", now=NOW)["groups"]} == {"/new", "/newest"}
    tiny = history.History(tmp_path, max_bytes=1)
    ingest(tiny, [line()])
    assert tiny.query("api", now=NOW)["total_requests"] == 0
    assert business.read_text() == "keep"


def test_pagination_and_time_range_do_not_limit_aggregation_to_recent_records(tmp_path):
    store = history.History(tmp_path)
    ingest(store, [line(f"/route-{i}") for i in range(110)] + [line("/older", ts=NOW - 7200)])
    first = store.query("api", now=NOW)
    second = store.query("api", page=1, now=NOW)
    assert first["total_groups"] == 111 and first["has_more"]
    assert len(first["groups"]) == 100 and len(second["groups"]) == 11 and not second["has_more"]
    assert store.query("api", period="1h", now=NOW)["total_groups"] == 110


def test_failed_transaction_does_not_advance_cursor(tmp_path, monkeypatch):
    store = history.History(tmp_path)
    ingest(store, [line()])
    before = store.source("api")
    def fail(_db, _now):
        raise sqlite3.OperationalError("disk full")
    monkeypatch.setattr(store, "_prune", fail)
    with pytest.raises(sqlite3.OperationalError):
        store.ingest("api", "API", "container-b", [line("/new")], NOW + 10, now=NOW)
    assert store.source("api") == before
    assert store.query("api", now=NOW)["total_requests"] == 1


def test_collector_checkpoints_without_browser_and_timeout_keeps_previous_history(tmp_path, monkeypatch):
    store = history.History(tmp_path)
    collector = history.Collector(store)
    entry = {"spec": {"key": "api", "name": "API"}}
    gateway = SimpleNamespace(inspect=lambda _: {"Id": "container-a"})
    commands = []
    def logs(command, **kwargs):
        commands.append(command)
        kwargs["stdout"].write((line() + '\n').encode())
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(history.subprocess, "run", logs)
    collector.collect(gateway, entry, now=NOW)
    collector.collect(gateway, entry, now=NOW + 5)
    assert store.query("api", now=NOW)["total_requests"] == 1
    assert "--timestamps" in commands[0]
    assert commands[1][commands[1].index("--since") + 1] == str(NOW - 2)
    before = store.source("api")["cursor"]
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("docker", 8)
    monkeypatch.setattr(history.subprocess, "run", timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        collector.collect(gateway, entry, now=NOW + 10)
    assert store.source("api")["cursor"] == before
    assert store.query("api", now=NOW)["total_requests"] == 1


def test_limits_errors_and_removed_container_history_are_visible(tmp_path):
    store = history.History(tmp_path)
    ingest(store, [line()], limited=True)
    history.Collector(store).collect(SimpleNamespace(inspect=lambda _: None), {"spec": {"key": "api", "name": "API"}}, now=NOW)
    result = store.query("api", now=NOW)
    assert result["limited_at"] == NOW and "容器不存在" in result["error"]
    assert result["total_requests"] == 1 and store.sources()[0]["key"] == "api"


def test_stopped_collector_does_not_touch_docker_or_database(tmp_path):
    stop = threading.Event()
    stop.set()
    store = history.History(tmp_path)
    history.Collector(store).run(stop, print)
    assert not store.path.exists()


@pytest.mark.parametrize("params", [{"period": "forever"}, {"layout": "sql"}, {"page": -1}, {"search": "x" * 2049}])
def test_invalid_queries(tmp_path, params):
    with pytest.raises(ValueError):
        history.History(tmp_path).query("api", **params)
