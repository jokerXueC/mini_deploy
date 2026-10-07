"""Discovery reads actual configurations; replacements preserve business resources."""
import copy
import http.client
import json
import time
from pathlib import Path

import pytest

import agent
import certificate_discovery as discovery
from test_certificates import pem_pair as pem_pair
from test_deployment_removal import http_server as http_server, runtime as runtime


def test_nginx_includes_inheritance_custom_config_and_multiple_keys():
    dump = '''# configuration file /custom/nginx.conf:
events {} http { ssl_certificate certs/one.crt; ssl_certificate_key certs/one.key;
include sites/*.conf; }
stream { server { listen 9000; } }
# configuration file /custom/sites/a.conf:
server { listen 443 ssl; server_name a.example.test www.a.example.test; }
server { listen 8443 ssl; server_name b.example.test;
ssl_certificate /etc/two.crt; ssl_certificate_key /etc/two.key;
ssl_certificate /etc/three.crt; ssl_certificate_key /etc/three.key; }
'''
    rows = discovery.nginx_sites(dump)
    assert len(rows) == 3
    assert rows[0]["domains"] == ["a.example.test", "www.a.example.test"]
    assert rows[0]["certificate_path"] == "/custom/certs/one.crt"
    assert rows[0]["config_path"] == "/custom/sites/a.conf"
    assert rows[2]["key_path"] == "/etc/three.key"


def test_nginx_cycles_and_unresolved_variables():
    with pytest.raises(ValueError, match="循环"):
        discovery.nginx_sites('# configuration file /etc/nginx/nginx.conf:\nhttp { include nginx.conf; }')
    rows = discovery.nginx_sites('# configuration file /etc/nginx/nginx.conf:\nhttp { server { server_name a.test; ssl_certificate /certs/$host.crt; } }')
    with pytest.raises(ValueError, match="变量"):
        discovery.Discovery(".").read_certificate({}, rows[0]["certificate_path"])


def test_caddy_json_hosts_from_nested_imports_and_http():
    config = {"apps": {"http": {"servers": {
        "secure": {"listen": [":443"], "routes": [{"match": [{"host": ["a.test", "b.test"]}],
                    "handle": [{"routes": [{"match": [{"host": ["c.test"]}]}]}]}]},
        "plain": {"listen": [":80"], "automatic_https": {"disable": True},
                  "routes": [{"match": [{"host": ["plain.test"]}]}]}}}}}
    rows, loaded = discovery.caddy_sites(config, "/etc/caddy/Caddyfile")
    assert [r["domains"] for r in rows] == [["a.test"], ["b.test"], ["c.test"], ["plain.test"]]
    assert not rows[-1]["tls"] and all(r["automatic"] for r in rows[:3])
    assert loaded == []


def test_hostname_match_does_not_match_nested_domains():
    assert discovery.matches_host("api.example.test", "*.example.test")
    assert not discovery.matches_host("child.api.example.test", "*.example.test")
    assert not discovery.matches_host("example.test", "*.example.test")


def test_public_metadata_never_returns_private_material(tmp_path, pem_pair):
    path = tmp_path / "fullchain.pem"
    path.write_text(pem_pair["certificate"] + pem_pair["private_key"])
    pem = discovery.public_pem(path)
    assert "PRIVATE" not in pem
    meta = discovery.certificate_metadata(pem)
    assert meta["domains"] == ["api.example.test"]
    assert len(meta["fingerprint"]) == 64
    assert meta["expires_at"] > time.time()
    assert list(discovery.bounded_files(tmp_path)) == [path]
    path.write_text(pem_pair["private_key"])
    with pytest.raises(ValueError, match="私钥"):
        discovery.public_pem(path)


@pytest.fixture
def discovered(tmp_path, monkeypatch, pem_pair):
    cert, key = tmp_path / "live.crt", tmp_path / "live.key"
    cert.write_text(pem_pair["certificate"])
    key.write_text(pem_pair["private_key"])
    source = {"kind": "nginx", "location": "Docker · web", "container_id": "a" * 64,
              "arguments": ["nginx", "-g", "daemon off;"], "active": True,
              "mounts": [{"Destination": "/certs", "Source": str(tmp_path), "RW": True, "Type": "bind"}]}
    site = {"domains": ["api.example.test"], "certificate_path": "/certs/live.crt", "key_path": "/certs/live.key",
            "config_path": "/etc/nginx/nginx.conf", "tls": True, "listens": [["443", "ssl"]], "notes": []}
    manager = discovery.Discovery(tmp_path / "data")
    monkeypatch.setattr(manager, "targets", lambda issues: iter([copy.deepcopy(source)]))
    monkeypatch.setattr(manager, "source_sites", lambda target: [copy.deepcopy(site)])
    monkeypatch.setattr(manager, "read_certificate", lambda target, path: discovery.certificate_metadata(discovery.public_pem(cert)))
    monkeypatch.setattr(discovery, "COMMON_ROOTS", ())
    manager.scan()
    return manager, cert, key, source, site


def test_scan_snapshot_is_readonly_and_hides_internal_data(discovered, monkeypatch):
    manager, cert, key, _, _ = discovered
    before = (cert.read_bytes(), key.read_bytes())
    monkeypatch.setattr(manager, "execute", lambda *args: pytest.fail("GET must not run subprocesses"))
    public = manager.snapshot()
    row = public["items"][0]
    assert row["can_replace"]
    assert not {"target", "key_path", "replacement_paths"} & row.keys()
    assert "PRIVATE KEY" not in json.dumps(public)
    assert before == (cert.read_bytes(), key.read_bytes())


def test_failed_rescan_retains_previous_rows_as_stale(discovered, monkeypatch):
    manager, *_ = discovered
    monkeypatch.setattr(manager, "source_sites", lambda target: (_ for _ in ()).throw(ValueError("secret config")))
    manager.scan()
    snapshot = manager.snapshot()
    assert len(snapshot["items"]) == 1 and snapshot["items"][0]["stale"]
    assert snapshot["issues"] and "secret config" not in json.dumps(snapshot)


def test_budget_stops_inside_container_enumeration(tmp_path, monkeypatch):
    manager = discovery.Discovery(tmp_path)
    monkeypatch.setattr(discovery.shutil, "which", lambda name: name if name == "docker" else None)
    monkeypatch.setattr(discovery.nginx_runtime, "require_local_docker", lambda: None)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        manager.deadline = time.monotonic() - 1
        return json.dumps({"ID": "a" * 64})

    manager.run = run
    manager.scan()
    assert len(calls) == 1 and "60 秒" in " ".join(manager.issues)


def test_caddy_mount_certificates_associate_without_claiming_live_use(tmp_path, monkeypatch, pem_pair):
    directory = tmp_path / "caddy" / "certificates" / "issuer" / "api.example.test"
    directory.mkdir(parents=True)
    (directory / "api.example.test.crt").write_text(pem_pair["certificate"])
    source = {"kind": "caddy", "location": "Docker · proxy", "container_id": "a" * 64,
              "arguments": [], "active": True, "mounts": [{"Type": "volume", "Destination": "/data", "Source": str(tmp_path)}]}
    site = {"domains": ["api.example.test"], "config_path": "/etc/caddy/Caddyfile", "certificate_path": "",
            "key_path": "", "tls": True, "automatic": True, "notes": []}
    manager = discovery.Discovery(tmp_path)
    monkeypatch.setattr(manager, "targets", lambda issues: iter([source]))
    monkeypatch.setattr(manager, "source_sites", lambda target: [site])
    monkeypatch.setattr(discovery, "COMMON_ROOTS", ())
    manager.scan()
    rows = manager.snapshot()["items"]
    assert len(rows) == 1 and rows[0]["certificate_candidate"]
    assert not rows[0]["can_replace"] and rows[0]["certificate"]["domains"] == ["api.example.test"]


def test_replacement_checks_backups_and_preserves_permissions(discovered, monkeypatch, pem_pair):
    manager, cert, key, _, _ = discovered
    commands = []
    monkeypatch.setattr(manager, "execute", lambda target, args: commands.append(args) or "")
    row = manager.snapshot()["items"][0]
    old = cert.read_bytes()
    result = manager.replace(row["id"], row["certificate"]["fingerprint"], pem_pair["certificate"] + "\n", pem_pair["private_key"])
    assert "重载" in result["message"]
    assert cert.read_text() == pem_pair["certificate"] + "\n"
    assert list((manager.home / "certificate-backups").glob("*/previous.crt"))[0].read_bytes() == old
    extra = ["-g", "daemon off;"]
    assert commands == [["nginx", "-t", *extra], ["nginx", "-t", *extra], ["nginx", "-s", "reload", *extra]]
    assert key.exists()


def test_reload_failure_restores_both_original_files(discovered, monkeypatch, pem_pair):
    manager, cert, key, _, _ = discovered
    before = cert.read_bytes(), key.read_bytes()
    calls = []

    def execute(target, args):
        calls.append(args)
        if len(calls) == 3:
            raise ValueError("reload failed")
        return ""

    monkeypatch.setattr(manager, "execute", execute)
    row = manager.rows[0]
    with pytest.raises(ValueError, match="已恢复"):
        manager.replace(row["id"], row["certificate"]["fingerprint"], pem_pair["certificate"] + "\n", pem_pair["private_key"])
    assert (cert.read_bytes(), key.read_bytes()) == before
    assert len(calls) == 5


def test_changed_cert_and_readonly_or_file_mounts_cannot_replace(discovered, pem_pair):
    manager, cert, key, _, _ = discovered
    row = manager.rows[0]
    with pytest.raises(ValueError, match="变化"):
        manager.replace(row["id"], "bad", pem_pair["certificate"], pem_pair["private_key"])
    source = row["target"]
    source["mounts"][0]["RW"] = False
    with pytest.raises(ValueError, match="可写目录"):
        manager.replacement_paths(row)
    source["mounts"] = [{"Destination": "/certs/live.crt", "Source": str(cert), "Type": "bind", "RW": True}]
    with pytest.raises(ValueError, match="单文件"):
        manager.replacement_paths(row)
    assert key.exists()


def test_discovery_api_auth_csrf_and_no_scan_on_get(http_server, monkeypatch):
    manager = agent._certificate_discovery()
    calls = []
    monkeypatch.setattr(manager, "request_scan", lambda: calls.append("scan"))
    for method in ("GET", "POST"):
        assert http_server(method, "/certificate-discovery", {"action": "scan"}, authenticated=False)[0] == 401
    assert http_server("GET", "/certificate-discovery")[0] == 200 and not calls
    assert http_server("POST", "/certificate-discovery", {"action": "scan"})[0] == 200
    assert calls == ["scan"]
    assert http_server("POST", "/certificate-discovery", {"action": []})[0] == 400
    assert http_server("POST", "/certificate-discovery", {"action": "delete"})[0] == 400
    connection = http.client.HTTPConnection(http_server.origin.removeprefix("http://"), timeout=5)
    connection.request("POST", "/certificate-discovery", headers={"Cookie": f"{agent.COOKIE_NAME}={http_server.cookie}"})
    response = connection.getresponse()
    assert response.status == 403
    response.read()
    connection.close()
    assert calls == ["scan"]


def test_online_check_is_bounded_and_reports_disk_mismatch(discovered, monkeypatch):
    manager, *_ = discovered
    monkeypatch.setattr(discovery.monitoring, "probe", lambda target: {"status": "healthy", "checked_at": time.time(), "certificate": {"sha256": "different"}})
    class Thread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            self.target()
    monkeypatch.setattr(discovery.threading, "Thread", Thread)
    identity = manager.rows[0]["id"]
    manager.check(identity)
    assert manager.online[identity]["matches_disk"] is False
    with pytest.raises(ValueError, match="稍候"):
        manager.check(identity)


def test_nginx_process_title_keeps_global_pid_directive():
    args = discovery.process_arguments('nginx: master process nginx -g daemon off; pid /tmp/site.pid; -c /custom/nginx.conf\0')
    assert discovery.nginx_flags(args) == ["-c", "/custom/nginx.conf", "-g", "daemon off; pid /tmp/site.pid;"]
    assert discovery.process_arguments('caddy\0run\0--config\0/etc/site name/Caddyfile\0') == ["caddy", "run", "--config", "/etc/site name/Caddyfile"]


def test_expiry_alerts_do_not_treat_stale_or_candidate_files_as_live(discovered):
    manager, *_ = discovered
    clock = [time.time()]
    monitor = discovery.monitoring.Monitor(manager.home / "monitor.json", clock=lambda: clock[0])
    manager.observe(monitor)
    clock[0] += 61
    manager.observe(monitor)
    assert next(iter(monitor.incidents.values()))["active"]
    manager.rows[0]["stale"] = True
    manager.issues = ["source unavailable"]
    manager.rows[0]["certificate"]["expires_at"] += 86400 * 365
    manager.observe(monitor)
    assert next(iter(monitor.incidents.values()))["active"]
    assert not any(event["kind"] == "recovery" for event in monitor.events)
    manager.rows[0]["stale"] = False
    manager.issues = []
    manager.rows[0]["certificate_candidate"] = True
    manager.observe(monitor)
    assert not monitor.incidents


def test_container_discovery_reads_running_arguments_for_custom_images(tmp_path, monkeypatch):
    identity = "b" * 64
    calls = []
    monkeypatch.setattr(discovery.shutil, "which", lambda name: name if name == "docker" else None)
    monkeypatch.setattr(discovery.nginx_runtime, "require_local_docker", lambda: None)

    def run(args, **kwargs):
        calls.append(args)
        if args[1] == "ps":
            return json.dumps({"ID": identity})
        if args[1] == "inspect":
            return json.dumps({"name": "/frontend", "image": "custom:1", "args": [], "path": "/entrypoint.sh", "mounts": [], "running": True})
        if args[1] == "top":
            return "COMMAND\nnginx: master process nginx -c /custom/nginx.conf -g daemon off; pid /tmp/custom.pid;\n"
        raise AssertionError(args)

    manager = discovery.Discovery(tmp_path, run=run)
    targets = list(manager.targets([]))
    assert len(targets) == 1 and targets[0]["kind"] == "nginx"
    assert discovery.nginx_flags(targets[0]["arguments"]) == ["-c", "/custom/nginx.conf", "-g", "daemon off; pid /tmp/custom.pid;"]
    assert all(".Config.Env" not in str(call) for call in calls)


def test_replacement_rejects_new_uncovered_shared_domain(discovered, monkeypatch, pem_pair):
    manager, cert, key, _, site = discovered
    before = cert.read_bytes(), key.read_bytes()
    changed = {**site, "domains": ["api.example.test", "other.example.test"]}
    monkeypatch.setattr(manager, "source_sites", lambda source: [changed])
    monkeypatch.setattr(manager, "execute", lambda *args: "")
    row = manager.rows[0]
    with pytest.raises(ValueError, match="全部域名"):
        manager.replace(row["id"], row["certificate"]["fingerprint"], pem_pair["certificate"], pem_pair["private_key"])
    assert before == (cert.read_bytes(), key.read_bytes())


@pytest.fixture
def managed_discovered(discovered, pem_pair):
    manager, _, _, source, site = discovered
    directory = manager.home / "certificates" / "api" / ("a" * 24)
    directory.mkdir(parents=True)
    cert, key = directory / "fullchain.pem", directory / "privkey.pem"
    discovery.certificates.atomic_write(cert, pem_pair["certificate"])
    discovery.certificates.atomic_write(key, pem_pair["private_key"])
    record = {**discovery.certificates.inspect_pair(cert, key, "api.example.test"),
              "revision": directory.name, "label": "Original user label", "issuer": "stale record"}
    metadata = directory.parent / "current.json"
    discovery.certificates.atomic_write(metadata, json.dumps(record))
    source["mounts"][0].update(Source=str(manager.home / "certificates"), RW=False)
    site.update(certificate_path=f"/certs/api/{directory.name}/fullchain.pem",
                key_path=f"/certs/api/{directory.name}/privkey.pem")
    manager.scan()
    assert manager.rows[0]["can_replace"], manager.snapshot()
    return manager, cert, key, metadata


def test_managed_certificate_replaces_from_same_entry_with_metadata_and_backup(managed_discovered, monkeypatch, pem_pair):
    manager, cert, key, metadata = managed_discovered
    original = metadata.read_bytes()
    modes = [path.stat().st_mode & 0o777 for path in (cert, key)]
    monkeypatch.setattr(manager, "execute", lambda *args: "")
    row = manager.rows[0]
    result = manager.replace(row["id"], row["certificate"]["fingerprint"], pem_pair["certificate"] + "\n", pem_pair["private_key"])
    record = json.loads(metadata.read_text())
    assert record["label"] == "Original user label" and record["revision"] == "a" * 24
    assert record["issuer"] != "stale record"
    assert (Path(result["backup"]) / "previous.json").read_bytes() == original
    assert cert.read_text() == pem_pair["certificate"] + "\n"
    assert [path.stat().st_mode & 0o777 for path in (cert, key)] == modes


def test_managed_replace_reload_failure_restores_metadata_and_files(managed_discovered, monkeypatch, pem_pair):
    manager, cert, key, metadata = managed_discovered
    original = [path.read_bytes() for path in (cert, key, metadata)]
    calls = []

    def execute(source, args):
        calls.append(args)
        if len(calls) == 3:
            raise ValueError("reload failed")

    monkeypatch.setattr(manager, "execute", execute)
    row = manager.rows[0]
    with pytest.raises(discovery.DiscoveryError, match="已恢复"):
        manager.replace(row["id"], row["certificate"]["fingerprint"], pem_pair["certificate"] + "\n", pem_pair["private_key"])
    assert [path.read_bytes() for path in (cert, key, metadata)] == original


def test_managed_historical_revision_is_not_replaced(managed_discovered):
    manager, cert, key, metadata = managed_discovered
    record = json.loads(metadata.read_text())
    record["revision"] = "b" * 24
    metadata.write_text(json.dumps(record))
    with pytest.raises(discovery.DiscoveryError, match="当前托管版本"):
        manager.replacement_paths(manager.rows[0])
    assert cert.exists() and key.exists()
