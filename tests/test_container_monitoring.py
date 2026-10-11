from __future__ import annotations

import copy
import json

import pytest

import container_monitoring as cm
from monitoring import Monitor


ID = "a" * 64
REPLACEMENT = "b" * 64


@pytest.fixture
def monitor(tmp_path):
    instance = Monitor(tmp_path / "monitor.json", clock=lambda: instance.now)
    instance.now = 10000.0
    return instance


def inspected(*, container_id=ID, name="web", state="running", exit_code=0, oom=False,
              error="", restarts=0, policy="no", health="healthy", project="", service="", replica="", oneoff=False):
    result = {
        "Id": container_id, "Name": "/" + name, "RestartCount": restarts,
        "State": {"Status": state, "Running": state == "running", "ExitCode": exit_code,
                  "OOMKilled": oom, "Error": error, "StartedAt": "1970-01-01T01:00:00Z",
                  "FinishedAt": "1970-01-01T01:01:00Z"},
        "HostConfig": {"RestartPolicy": {"Name": policy, "MaximumRetryCount": 0}},
        "Config": {"Labels": {"com.docker.compose.project": project, "com.docker.compose.service": service,
                              "com.docker.compose.container-number": replica,
                              "com.docker.compose.oneoff": str(oneoff)}},
    }
    if health is not None:
        result["State"]["Health"] = {"Status": health, "Log": [
            {"ExitCode": 1, "End": "old", "Output": "password=old-secret"},
            {"ExitCode": 1, "End": "2026-10-11T01:02:00Z", "Output": "token=secret-value " * 10000},
        ]}
    return result


def snapshot(*items, available=True, payload=None):
    raw = {"available": available, "containers": [{"id": item["Id"], "name": item["Name"].lstrip("/")}
                                                   for item in items]}
    return cm.attach_inspection(raw, list(items) if payload is None else payload)


def evaluate(monitor, *items, **kwargs):
    cm.evaluate_containers(monitor, snapshot(*items, **kwargs))


def prefix(item):
    return cm.container_identity(cm.parse_inspect([item])[item["Id"]])


def alerts(monitor):
    return monitor.snapshot()["alerts"]


def activate(monitor, item, reason):
    evaluate(monitor, item)
    monitor.now += 61
    evaluate(monitor, item)
    key = prefix(item) + ":" + reason
    assert monitor.incidents[key]["active"]
    return key


@pytest.mark.parametrize("encoded", [False, True])
def test_parse_batched_inspect_extracts_only_bounded_safe_evidence(encoded):
    item = inspected(project="shop", service="web", replica="2", policy="on-failure", restarts=9,
                     exit_code=1, oom=True, error="password=engine-secret " * 10000)
    item["Config"]["Env"] = ["TOKEN=config-secret"]
    second = inspected(container_id=REPLACEMENT, health=None)
    payload = [item, second]
    result = cm.parse_inspect(json.dumps(payload) if encoded else payload)
    evidence = result[ID]
    assert set(result) == {ID, REPLACEMENT}
    assert evidence["exit_code"] == 1 and evidence["oom_killed"] is True
    assert evidence["engine_error"] is True and "error" not in evidence
    assert evidence["started_at"] == item["State"]["StartedAt"]
    assert evidence["finished_at"] == item["State"]["FinishedAt"]
    assert evidence["health_last_check"] == {"exit_code": 1, "end": "2026-10-11T01:02:00Z"}
    assert evidence["restart_count"] == 9 and evidence["restart_policy"] == "on-failure"
    assert evidence["restart_maximum_retry_count"] == 0
    assert evidence["compose_project"] == "shop" and evidence["compose_service"] == "web"
    assert evidence["compose_replica"] == "2"
    assert result[REPLACEMENT]["health"] == "none"
    assert "secret" not in json.dumps(result) and len(json.dumps(result)) < 2500


@pytest.mark.parametrize("payload", [None, "not json", {}, [None, 3, {}], b"\xff", {"State": {}}])
def test_invalid_batch_is_unknown(payload):
    assert cm.parse_inspect(payload) == {}


@pytest.mark.parametrize("field,value", [("Status", []), ("Status", "unknown"), ("Status", None)])
def test_invalid_state_records_do_not_mask_valid_records(field, value):
    invalid = inspected()
    invalid["State"][field] = value
    assert set(cm.parse_inspect([invalid, inspected(container_id=REPLACEMENT)])) == {REPLACEMENT}


def test_invalid_fault_fields_remain_unknown_and_duplicate_ids_are_rejected():
    item = inspected(restarts=True)
    item["State"].update(ExitCode=True, OOMKilled="false", Error=None)
    item["State"]["Health"].update(Status=[], Log=[{"ExitCode": -1, "End": "x"}])
    evidence = cm.parse_inspect([item])[ID]
    assert evidence["exit_code"] is None and evidence["oom_killed"] is None
    assert evidence["engine_error"] is None and evidence["restart_count"] is None
    assert evidence["health"] is None and evidence["health_last_check"]["exit_code"] is None
    assert cm.parse_inspect([item, copy.deepcopy(item)]) == {}


def test_attach_inspection_is_nonmutating_exact_id_and_clears_old_evidence():
    original = {"available": True, "containers": [{"id": ID, "inspection": {"stale": True}}, {"id": ID[:12]}]}
    before = copy.deepcopy(original)
    attached = cm.attach_inspection(original, json.dumps([inspected()]))
    assert attached["containers"][0]["inspection"]["id"] == ID
    assert attached["containers"][1]["inspection"] is None
    assert cm.attach_inspection(original, "invalid")["containers"][0]["inspection"] is None
    assert original == before


def test_restart_baseline_is_not_a_storm_and_window_uses_exact_deltas(monitor):
    item = inspected(restarts=900)
    evaluate(monitor, item)
    key = prefix(item) + ":restarts"
    assert not alerts(monitor)
    monitor.now += 0.25
    item["RestartCount"] = 901
    evaluate(monitor, item)
    first_delta_at = monitor.now
    monitor.now += 59.75
    item["RestartCount"] = 903
    evaluate(monitor, item)
    assert monitor.incidents[key]["active"]
    assert monitor.incidents[key]["metadata"]["restart_delta_5m"] == 3
    incident_id = monitor.incidents[key]["incident_id"]
    evaluate(monitor, item)
    assert monitor.incidents[key]["metadata"]["restart_delta_5m"] == 3
    monitor.now = first_delta_at + 299.999
    evaluate(monitor, item)
    assert monitor.incidents[key]["incident_id"] == incident_id
    monitor.now = first_delta_at + 300
    evaluate(monitor, item)
    assert key not in monitor.incidents
    assert monitor.container_state[prefix(item)]["restart_deltas"] == [[10060.0, 2]]
    assert sum(event["kind"] == "recovery" for event in monitor.events) == 1


def test_restart_counter_reset_and_same_time_polls_do_not_invent_restarts(monitor):
    item = inspected(restarts=8)
    evaluate(monitor, item)
    item["RestartCount"] = 0
    evaluate(monitor, item)
    item["RestartCount"] = 1
    evaluate(monitor, item)
    item["RestartCount"] = 2
    evaluate(monitor, item)
    assert not alerts(monitor)
    assert monitor.container_state[prefix(item)]["restart_deltas"] == [[monitor.now, 2]]
    item["RestartCount"] = 3
    evaluate(monitor, item)
    assert alerts(monitor)[0]["metadata"]["restart_delta_5m"] == 3


def test_restart_long_gap_does_not_localize_count_increases_or_recover_existing_storm(monitor):
    item = inspected()
    evaluate(monitor, item)
    monitor.now += 1
    item["RestartCount"] = 3
    evaluate(monitor, item)
    key = prefix(item) + ":restarts"
    monitor.now += 301
    item["RestartCount"] = 100
    evaluate(monitor, item)
    assert monitor.incidents[key]["sample_unknown"]
    assert monitor.incidents[key]["metadata"]["restart_delta_5m"] == 3  # Last valid fault evidence retained.
    monitor.now += 299
    evaluate(monitor, item)
    assert monitor.incidents[key]["active"]
    monitor.now += 1
    evaluate(monitor, item)
    assert key not in monitor.incidents


def test_compose_replacement_preserves_identity_history_and_rebases_counter(monitor):
    item = inspected(project="shop", service="web", replica="1", restarts=10)
    evaluate(monitor, item)
    monitor.now += 1
    item["RestartCount"] = 12
    evaluate(monitor, item)
    replacement = inspected(container_id=REPLACEMENT, name="new-web", project="shop", service="web", replica="1",
                            restarts=800)
    assert prefix(replacement) == prefix(item)
    monitor.now += 1
    evaluate(monitor, replacement)
    assert not alerts(monitor)
    monitor.now += 1
    replacement["RestartCount"] = 801
    evaluate(monitor, replacement)
    assert monitor.incidents[prefix(item) + ":restarts"]["metadata"]["restart_delta_5m"] == 3


def test_identity_separates_projects_replicas_and_oneoffs():
    base = inspected(project="shop", service="web", replica="1")
    assert prefix(base) != prefix(inspected(project="other", service="web", replica="1"))
    assert prefix(base) != prefix(inspected(project="shop", service="web", replica="2"))
    assert prefix(base) != prefix(inspected(project="shop", service="web", replica="1", oneoff=True))
    assert prefix(inspected(project="shop", service="web")) == prefix(
        inspected(container_id=REPLACEMENT, name="renamed", project="shop", service="web"))


def test_valid_compose_replacement_recovers_old_fault_without_phantom_alert(monitor):
    item = inspected(project="shop", service="web", health="unhealthy")
    key = activate(monitor, item, "health")
    evaluate(monitor, inspected(container_id=REPLACEMENT, project="shop", service="web"))
    assert key not in monitor.incidents and not alerts(monitor)
    assert sum(event["kind"] == "recovery" for event in monitor.events) == 1


def test_oom_is_immediate_but_stale_oom_on_running_container_is_not_active(monitor):
    item = inspected(state="exited", exit_code=137, oom=True)
    evaluate(monitor, item)
    key = prefix(item) + ":oom"
    assert monitor.incidents[key]["active"]
    item["State"].update(Status="running", Running=True)
    evaluate(monitor, item)
    assert key not in monitor.incidents and not alerts(monitor)
    fresh = Monitor(monitor.path, clock=lambda: monitor.now)
    evaluate(fresh, item)
    assert not fresh.incidents


@pytest.mark.parametrize("state,exit_code,policy,oneoff", [
    ("exited", 0, "no", False), ("exited", 0, "unless-stopped", False),
    ("exited", 0, "on-failure", False), ("exited", 0, "no", True),
    ("exited", 137, "always", False), ("exited", 143, "always", False),
    ("paused", 0, "always", False), ("created", 0, "always", False),
])
def test_manual_external_stops_and_normal_oneoff_exits_do_not_alarm(monitor, state, exit_code, policy, oneoff):
    item = inspected(policy=policy, oneoff=oneoff)
    evaluate(monitor, item)
    item["State"].update(Status=state, ExitCode=exit_code, Running=False)
    evaluate(monitor, item)
    monitor.now += 61
    evaluate(monitor, item)
    assert not monitor.incidents


@pytest.mark.parametrize("policy", ["always", "unless-stopped", "no"])
def test_expected_running_policy_and_prior_running_observation(monitor, policy):
    item = inspected(state="created", policy=policy)
    evaluate(monitor, item)
    entry = monitor.container_state[prefix(item)]
    assert entry["expected_running"] is (policy != "no")
    item["State"]["Status"] = "running"
    evaluate(monitor, item)
    assert entry["expected_running"] is True


@pytest.mark.parametrize("policy,explicit", [("always", False), ("no", True)])
def test_clean_expected_exit_uses_monitor_sustain(monitor, policy, explicit):
    item = inspected(state="exited", policy=policy)
    if explicit:
        monitor.container_state[prefix(item)] = {"expected_running": True, "watch_explicit": True}
    key = activate(monitor, item, "exit")
    assert monitor.incidents[key]["metadata"]["expected_running"] is True


def test_explicit_false_overrides_automatic_expectation(monitor):
    item = inspected(state="exited", policy="always")
    monitor.container_state[prefix(item)] = {"expected_running": False, "watch_explicit": True}
    evaluate(monitor, item)
    assert not monitor.incidents
    assert monitor.container_state[prefix(item)]["expected_running"] is False


@pytest.mark.parametrize("oneoff", [True, False])
def test_abnormal_standalone_exit_still_alarms(monitor, oneoff):
    item = inspected(state="exited", exit_code=1, oneoff=oneoff)
    key = activate(monitor, item, "exit")
    assert monitor.incidents[key]["metadata"]["exit_code"] == 1


@pytest.mark.parametrize("sample", ["unavailable", "empty", "inspect_missing", "missing_state", "ambiguous", "manual_exit"])
def test_unknown_samples_never_resolve_active_faults(monitor, sample):
    item = inspected(project="shop", service="web", health="unhealthy")
    key = activate(monitor, item, "health")
    before = len(monitor.events)
    if sample == "unavailable":
        evaluate(monitor, item, available=False)
    elif sample == "empty":
        evaluate(monitor)
    elif sample == "inspect_missing":
        evaluate(monitor, item, payload=[])
    elif sample == "missing_state":
        del item["State"]
        evaluate(monitor, item)
    elif sample == "ambiguous":
        evaluate(monitor, item, inspected(container_id=REPLACEMENT, project="shop", service="web"))
    else:
        item["State"].update(Status="exited", ExitCode=0)
        evaluate(monitor, item)
    assert monitor.incidents[key]["active"] and monitor.incidents[key]["sample_unknown"]
    assert alerts(monitor)[0]["stale"] is True
    assert len(monitor.events) == before


@pytest.mark.parametrize("selector", [ID, "web", "/web"])
def test_intentional_stop_preserves_fault_until_start_or_replacement(monitor, selector):
    item = inspected(health="unhealthy", policy="always")
    key = activate(monitor, item, "health")
    cm.mark_intentional_stop(monitor, selector)
    evaluate(monitor, inspected())  # A cached running sample must not clear the intent.
    evaluate(monitor, inspected(state="exited", policy="always"))
    assert monitor.incidents[key]["active"] and monitor.incidents[key]["sample_unknown"]
    assert not any(event["kind"] == "recovery" for event in monitor.events)
    cm.mark_intentional_stop(monitor, selector, stopped=False)
    evaluate(monitor, inspected())
    assert key not in monitor.incidents


def test_stop_marker_before_first_inspection_and_new_replacement_resets_intent(monitor):
    cm.mark_intentional_stop(monitor, "web")
    item = inspected(state="exited", policy="always", project="shop", service="web")
    evaluate(monitor, item)
    assert not monitor.incidents
    assert monitor.container_state[prefix(item)]["intentional_stop"] is True
    assert not any(key.startswith("docker:intent:") for key in monitor.container_state)
    replacement = inspected(container_id=REPLACEMENT, project="shop", service="web", policy="always", state="exited")
    activate(monitor, replacement, "exit")
    assert monitor.container_state[prefix(item)]["intentional_stop"] is False


@pytest.mark.parametrize("started_at", [
    "1970-01-01T02:47:42Z", "1970-01-01T02:47:41.000000001Z", "1970-01-01T10:47:42+08:00",
])
def test_external_start_clears_persisted_intent_only_after_new_start(monitor, started_at):
    item = inspected(health="unhealthy")
    key = activate(monitor, item, "health")
    cm.mark_intentional_stop(monitor, ID)
    intentional_at = monitor.now
    evaluate(monitor, inspected())
    assert monitor.incidents[key]["sample_unknown"]
    assert monitor.container_state[prefix(item)]["intentional_at"] == intentional_at
    monitor.flush()
    restored = Monitor(monitor.path, clock=lambda: monitor.now)
    started = inspected()
    started["State"]["StartedAt"] = started_at
    monitor.now += 1
    evaluate(restored, started)
    assert not restored.container_state[prefix(item)]["intentional_stop"]
    assert key not in restored.incidents


@pytest.mark.parametrize("started_at", ["", "not a date", "0001-01-01T00:00:00Z", "1970-01-01T02:47:41Z"])
def test_unknown_or_not_later_start_does_not_clear_stop_intent(monitor, started_at):
    monitor.now = 10061.0
    item = inspected()
    evaluate(monitor, item)
    cm.mark_intentional_stop(monitor, ID)
    item["State"]["StartedAt"] = started_at
    evaluate(monitor, item)
    assert monitor.container_state[prefix(item)]["intentional_stop"] is True


def test_nanosecond_strict_comparison_does_not_round_new_start_to_stop_time(monitor):
    monitor.now = 10061.25
    item = inspected()
    evaluate(monitor, item)
    cm.mark_intentional_stop(monitor, ID)
    item["State"]["StartedAt"] = "1970-01-01T02:47:41.250000000Z"
    evaluate(monitor, item)
    assert monitor.container_state[prefix(item)]["intentional_stop"] is True
    item["State"]["StartedAt"] = "1970-01-01T02:47:41.250000001Z"
    evaluate(monitor, item)
    assert monitor.container_state[prefix(item)]["intentional_stop"] is False


def test_persistence_preserves_restart_window_explicit_watch_and_intent(monitor):
    item = inspected(project="shop", service="web", policy="always")
    evaluate(monitor, item)
    monitor.now += 1
    item["RestartCount"] = 2
    evaluate(monitor, item)
    entry = monitor.container_state[prefix(item)]
    entry.update(expected_running=True, watch_explicit=True)
    cm.mark_intentional_stop(monitor, ID)
    monitor.flush()
    restored = Monitor(monitor.path, clock=lambda: monitor.now)
    assert restored.container_state == monitor.container_state
    cm.mark_intentional_stop(restored, ID, stopped=False)
    monitor.now += 1
    item["RestartCount"] = 3
    evaluate(restored, item)
    assert restored.incidents[prefix(item) + ":restarts"]["active"]


def test_state_and_restart_history_bounds_preserve_event_worker_ledger(monitor):
    ledger = {"event:cursor": {"last_seen": -100, "value": "keep"}, "other": {"value": "keep"}}
    monitor.container_state.update(copy.deepcopy(ledger))
    for index in range(cm.MAX_CONTAINER_STATE + 10):
        cm.mark_intentional_stop(monitor, f"container-{index}")
    assert len([key for key in monitor.container_state if key.startswith("docker:")]) == cm.MAX_CONTAINER_STATE
    assert all(monitor.container_state[key] == value for key, value in ledger.items())
    item = inspected()
    evaluate(monitor, item)
    for index in range(1, 330):
        monitor.now += 0.1
        item["RestartCount"] = index
        evaluate(monitor, item)
    entry = monitor.container_state[prefix(item)]
    assert len(entry["restart_deltas"]) <= cm.RESTART_WINDOW_SECONDS + 1
    assert monitor.incidents[prefix(item) + ":restarts"]["active"]
    assert all(monitor.container_state[key] == value for key, value in ledger.items())


def test_coverage_ui_metadata_and_secrets_are_safe(monitor):
    item = inspected(state="exited", oom=True, exit_code=137, error="token=engine-secret", project="shop", service="web")
    evaluate(monitor, item)
    alert = alerts(monitor)[0]
    assert alert["title"].startswith("容器")
    assert alert["metadata"]["container"] == "web" and alert["metadata"]["container_id"] == ID
    assert alert["metadata"]["project"] == "shop" and alert["metadata"]["service"] == "web"
    assert monitor.coverage["docker_containers"]["status"] == "healthy"
    evaluate(monitor, item, payload=[])
    assert monitor.coverage["docker_containers"]["status"] == "unknown"
    cm.evaluate_containers(monitor, {"available": False, "error": "docker command not found", "containers": []})
    assert monitor.coverage["docker_containers"]["status"] == "not_installed"
    monitor.flush()
    assert "secret" not in monitor.path.read_text(encoding="utf-8")


def legacy_incident(monitor, key):
    monitor.observe(key, True, "旧容器异常", "旧详情", "docker", immediate=True)
    monitor.incidents[key]["acknowledged_at"] = monitor.now
    monitor.pending[key]["custom"] = "preserved"
    return monitor.incidents[key]["incident_id"], copy.deepcopy(monitor.pending[key])


@pytest.mark.parametrize("reason", ["exit", "oom", "health", "restarts"])
@pytest.mark.parametrize("legacy_name", [False, True])
def test_legacy_migration_preserves_incident_id_ack_and_pending(monitor, reason, legacy_name):
    item = inspected()
    if reason == "exit":
        item["State"].update(Status="exited", ExitCode=1)
    elif reason == "oom":
        item["State"].update(Status="exited", ExitCode=137, OOMKilled=True)
    elif reason == "health":
        item["State"]["Health"]["Status"] = "unhealthy"
    else:
        evaluate(monitor, item)
        monitor.now += 1
        item["RestartCount"] = 3
    legacy = "docker:container:" + ("web" if legacy_name else ID)
    incident_id, pending = legacy_incident(monitor, legacy)
    events_before = len(monitor.events)
    evaluate(monitor, item)
    key = prefix(item) + ":" + reason
    assert legacy not in monitor.incidents and legacy not in monitor.pending
    assert monitor.incidents[key]["incident_id"] == incident_id
    assert monitor.incidents[key]["acknowledged_at"] == monitor.now
    assert monitor.pending[key] == pending
    assert len(alerts(monitor)) == 1 and len(monitor.events) == events_before


def test_healthy_inspect_resolves_legacy_fault_but_missing_and_manual_samples_do_not(monitor):
    legacy = "docker:container:" + ID
    incident_id, _ = legacy_incident(monitor, legacy)
    evaluate(monitor, inspected(), payload=[])
    assert monitor.incidents[legacy]["sample_unknown"]
    evaluate(monitor, inspected(state="exited"))
    assert monitor.incidents[legacy]["active"]
    assert not any(event["kind"] == "recovery" for event in monitor.events)
    evaluate(monitor, inspected())
    assert legacy not in monitor.incidents and not alerts(monitor)
    recovered = [event for event in monitor.events if event["kind"] == "recovery"]
    assert len(recovered) == 1 and recovered[0]["incident_id"] == incident_id


def test_unobserved_legacy_fault_remains_stale(monitor):
    legacy = "docker:container:" + ID
    legacy_incident(monitor, legacy)
    evaluate(monitor, inspected(container_id=REPLACEMENT))
    assert monitor.incidents[legacy]["active"] and monitor.incidents[legacy]["sample_unknown"]
    assert len(alerts(monitor)) == 1
