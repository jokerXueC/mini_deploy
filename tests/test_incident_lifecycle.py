import pytest

import monitoring


@pytest.fixture
def monitor(tmp_path):
    now = [10000.0]
    item = monitoring.Monitor(tmp_path / "monitor.json", clock=lambda: now[0])
    item.now = now
    return item


def fault(monitor, bad=True):
    monitor.observe("docker:api:oom", bad, "API OOM", "exit 137", "docker",
                    metadata={"container": "api", "exit_code": 137}, immediate=True)


def test_acknowledgment_persists_keeps_fault_and_does_not_hide_history(monitor):
    fault(monitor)
    alert = monitor.snapshot()["alerts"][0]
    monitor.operation({"action": "acknowledge", "key": alert["id"], "incident_id": alert["incident_id"]})
    assert monitor.snapshot()["alerts"][0]["acknowledged_at"] == 10000
    assert monitor.next_notification(True) is None
    monitor.now[0] += 3601
    fault(monitor)
    assert monitor.next_notification(True) is None
    restored = monitoring.Monitor(monitor.path, monitor.clock)
    assert restored.snapshot()["alerts"][0]["acknowledged_at"] == 10000
    assert [e["kind"] for e in restored.snapshot()["events"]] == ["acknowledged", "alert"]
    assert restored.snapshot()["events"][0]["metadata"]["exit_code"] == 137


def test_old_acknowledgment_cannot_acknowledge_a_new_incident(monitor):
    fault(monitor)
    old = monitor.snapshot()["alerts"][0]
    fault(monitor, False)
    fault(monitor)
    new = monitor.snapshot()["alerts"][0]
    assert old["incident_id"] != new["incident_id"]
    with pytest.raises(ValueError, match="故障已变化"):
        monitor.operation({"action": "acknowledge", "key": old["id"], "incident_id": old["incident_id"]})
    assert not new.get("acknowledged_at")
    assert monitor.next_notification(True)


def test_missing_evidence_never_resolves_active_fault_or_sends_stale_notice(monitor):
    fault(monitor)
    fault(monitor, None)
    assert monitor.snapshot()["alerts"][0]["stale"]
    assert monitor.next_notification(True) is None
    assert len(monitor.snapshot()["events"]) == 1
    fault(monitor)
    assert not monitor.snapshot()["alerts"][0]["stale"]
    assert monitor.next_notification(True)


def test_recovery_links_original_incident_and_new_fault_notifies(monitor):
    fault(monitor)
    key, message = monitor.next_notification(True)
    monitor.delivered(key, message, [{"enabled": True, "ok": True}])
    old = monitor.snapshot()["alerts"][0]
    monitor.operation({"action": "acknowledge_all"})
    monitor.now[0] += 180
    fault(monitor, False)
    recovery = monitor.snapshot()["events"][0]
    assert recovery["incident_id"] == old["incident_id"]
    assert recovery["duration_seconds"] == 180
    assert monitor.next_notification(True)[1]["recovery"]
    fault(monitor)
    assert not monitor.snapshot()["alerts"][0].get("acknowledged_at")


def test_ack_write_failure_rolls_back_incident_and_events(monitor, monkeypatch):
    fault(monitor)
    before = monitor.snapshot()
    monkeypatch.setattr(monitor, "flush", lambda: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        monitor.operation({"action": "acknowledge_all"})
    assert monitor.snapshot() == before


def test_history_retention_is_bounded_without_touching_active_incidents(monitor):
    fault(monitor)
    monitor.now[0] += 31 * 86400
    assert not monitor.snapshot()["events"]
    assert monitor.snapshot()["alerts"][0]["active"]
    for i in range(1005):
        monitor.record_event("operation", str(i), "", "docker")
    assert len(monitor.snapshot()["events"]) == 1000
    assert monitor.snapshot()["events"][0]["title"] == "1004"


def test_legacy_active_alert_is_upgraded_without_losing_history(tmp_path):
    import json
    path = tmp_path / "monitor.json"
    path.write_text(json.dumps({"incidents": {"server:cpu": {"active": True, "title": "CPU",
        "source": "server", "level": "critical", "detail": "95%", "first_seen": 1}},
        "events": [{"kind": "alert", "title": "Legacy", "at": "old"}]}))
    item = monitoring.Monitor(path)
    assert item.snapshot()["alerts"][0]["incident_id"]
    assert item.snapshot()["events"][0]["title"] == "Legacy"
