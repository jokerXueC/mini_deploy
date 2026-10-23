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


def test_metadata_survives_restart_and_keeps_stable_record_identity(tmp_path):
    store = history.History(tmp_path)
    stamp, payload = line().split(' mini_deploy_req ', 1)
    data = {**json.loads(payload), "client_ip": "203.0.113.8", "response_bytes": "1024",
        "request_headers": {"User-Agent": "browser", "Authorization": "secret"}}
    ingest(store, [line("/old"), stamp + ' mini_deploy_req ' + json.dumps(data)])
    record = store.query("api", layout="flat", now=NOW)["groups"][0]["items"][0]
    assert record["client_ip"] == "203.0.113.8" and record["request_headers"] == {"User-Agent": "browser"}
    restored = history.History(tmp_path).query("api", layout="flat", now=NOW)["groups"][0]["items"][0]
    assert restored == record and isinstance(record["record_id"], int)
    assert "secret" not in store.path.read_bytes().decode("utf-8", errors="ignore")


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


def test_slow_filter_keeps_full_statistics_but_finds_older_matching_details(tmp_path):
    store = history.History(tmp_path)
    ingest(store, [line("/busy", ts=NOW - 1000, duration="1.500")] +
           [line("/busy", ts=NOW - 50 + index, duration="0.010") for index in range(40)] + [line("/normal")])
    result = store.query("api", status="slow", slow_ms=1000, now=NOW)
    group = result["groups"][0]
    assert result["total_groups"] == 1 and group["count"] == 41 and group["slowCount"] == 1
    assert group["average"] == pytest.approx((1500 + 40 * 10) / 41)
    assert len(group["items"]) == 1 and group["items"][0]["duration_ms"] == 1500
    assert store.query("api", status="slow", slow_ms=2000, now=NOW)["total_groups"] == 0
    assert store.query("api", status="slow", slow_ms=10, now=NOW)["total_groups"] == 2


def test_request_id_and_ip_search_select_whole_group(tmp_path):
    store = history.History(tmp_path)
    stamp, payload = line("/api/items").split(' mini_deploy_req ', 1)
    data = {**json.loads(payload), "request_id": "trace-123", "client_ip": "203.0.113.9"}
    ingest(store, [line("/api/items"), stamp + ' mini_deploy_req ' + json.dumps(data)])
    for term in ("trace-123", "203.0.113.9"):
        result = store.query("api", search=term, now=NOW)
        assert result["total_requests"] == 2
        assert len(result["groups"][0]["items"]) == 1
        assert result["groups"][0]["items"][0]["request_id"] == "trace-123"


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
    assert {g["path"] for g in store.query("api", now=NOW)["groups"]} == {"/old", "/new", "/newest"}
    assert store.query('api', now=NOW)['retained_requests'] == 2
    tiny = history.History(tmp_path, max_bytes=1)
    ingest(tiny, [line()])
    assert tiny.query("api", now=NOW)["total_requests"] == 4
    assert tiny.query('api', now=NOW)['retained_requests'] == 0
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
    gateway = SimpleNamespace(read=lambda _: entry, inspect=lambda _: {"Id": "container-a"}, upgrade_logging=lambda *_: None)
    commands = []
    def logs(stream, since, until, checkpoint):
        commands.append((since, until))
        return ([line()], ['first'], {}) if len(commands) == 1 else ([], [], {})
    monkeypatch.setattr(history, "read_log_batch", logs)
    collector.collect(gateway, entry, now=NOW)
    collector.collect(gateway, entry, now=NOW + 5)
    assert store.query("api", now=NOW)["total_requests"] == 1
    assert commands[1][0] == NOW - 1
    before = store.source("api")["cursor"]
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("docker", 8)
    monkeypatch.setattr(history, "read_log_batch", timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        collector.collect(gateway, entry, now=NOW + 10)
    assert store.source("api")["cursor"] == before
    assert store.query("api", now=NOW)["total_requests"] == 1


def test_limits_errors_and_removed_container_history_are_visible(tmp_path):
    store = history.History(tmp_path)
    ingest(store, [line()], limited=True)
    entry = {"spec": {"key": "api", "name": "API"}}
    history.Collector(store).collect(SimpleNamespace(read=lambda _: entry, inspect=lambda _: None), entry, now=NOW)
    result = store.query("api", now=NOW)
    assert result["limited_at"] == NOW and "容器不存在" in result["error"]
    assert result["total_requests"] == 1 and store.sources()[0]["key"] == "api"


def test_empty_period_exposes_retained_history_and_last_request_after_restart(tmp_path):
    store = history.History(tmp_path)
    ingest(store, [line('/older', ts=NOW - 2 * 86400)])
    store.ingest('api', 'API', 'container-a', [], NOW - 1, now=NOW,
                 error='代理配置已变化，请求可能绕过采集网关')
    restored = history.History(tmp_path)
    result = restored.query('api', now=NOW)
    assert result['total_requests'] == 0 and result['retained_requests'] == 1
    assert result['latest_at'] == NOW - 2 * 86400 and result['checked_at'] == NOW
    assert '绕过' in result['error']
    assert restored.query('api', period='7d', now=NOW)['total_requests'] == 1


def test_collector_keeps_route_warning_while_persisting_inflight_requests(tmp_path, monkeypatch):
    from gateway_connections import Connections

    store = history.History(tmp_path)
    entry = {'spec': {'key': 'api', 'name': 'API'}}
    gateway = SimpleNamespace(read=lambda _: entry, inspect=lambda _: {'Id': 'container-a'}, upgrade_logging=lambda *_: None)
    monkeypatch.setattr(Connections, 'status', lambda *_a, **_kw: {'error': '代理配置已变化'})

    def logs(*args):
        return [line()], ['first'], {}

    monkeypatch.setattr(history, 'read_log_batch', logs)
    history.Collector(store).collect(gateway, entry, now=NOW)
    result = store.query('api', now=NOW)
    assert result['total_requests'] == 1 and result['error'] == '代理配置已变化'


def test_stopped_collector_does_not_touch_docker_or_database(tmp_path):
    stop = threading.Event()
    stop.set()
    store = history.History(tmp_path)
    history.Collector(store).run(stop, print)
    assert not store.path.exists()


@pytest.mark.parametrize("params", [{"period": "forever"}, {"layout": "sql"}, {"page": -1}, {"search": "x" * 2049},
    {"slow_ms": 0}, {"slow_ms": True}, {"slow_ms": "1000"}, {"slow_ms": 3_600_001}])
def test_invalid_queries(tmp_path, params):
    with pytest.raises(ValueError):
        history.History(tmp_path).query("api", **params)
