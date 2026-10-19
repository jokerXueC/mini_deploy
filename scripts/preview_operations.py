"""Read-only local UI preview using clearly labelled synthetic server evidence."""
import argparse
import math
import sys
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent  # noqa: E402
from scripts.local_ssh import configure  # noqa: E402


def examples():
    now = time.time()
    container = "a" * 64
    sources = [{"id": "container:" + container, "kind": "container", "name": "api"},
               {"id": "service:mini-deploy-agent.service", "kind": "service", "name": "mini-deploy-agent.service"},
               {"id": "service:postgresql.service", "kind": "service", "name": "postgresql.service"}]
    inventory = {"hostname": "debian-demo（演示）", "checked_at": now, "process_count": 126,
        "cpu_semantics": "CPU 为进程存活期间的平均占用；100% 表示一个逻辑核", "warnings": [], "sources": sources,
        "processes": [{"pid": 1826, "ppid": 1, "uid": 1000, "name": "python", "cpu": 86.2,
                       "memory": 17.5, "rss_mib": 1433.6, "log_source": sources[0]["id"]},
                      {"pid": 920, "ppid": 1, "uid": 999, "name": "postgres", "cpu": 12.4,
                       "memory": 9.2, "rss_mib": 753.7, "log_source": sources[2]["id"]},
                      {"pid": 1105, "ppid": 1, "uid": 0, "name": "caddy", "cpu": 2.8, "memory": 1.1, "rss_mib": 90.1}],
        "ports": [{"protocol": "tcp", "state": "LISTEN", "address": "127.0.0.1:8766", "owners": [{"pid": 1826, "name": "python"}]},
                  {"protocol": "tcp", "state": "LISTEN", "address": "0.0.0.0:443", "owners": [{"pid": 1105, "name": "caddy"}]},
                  {"protocol": "tcp", "state": "LISTEN", "address": "127.0.0.1:5432", "owners": [{"pid": 920, "name": "postgres"}]}]}
    incident = {"id": "docker:api:oom", "incident_id": "demo-oom", "title": "api 内存溢出退出", "detail": "退出码 137；最近 5 分钟重启 3 次",
        "source": "docker", "level": "critical", "first_seen": now - 120, "active": True,
        "metadata": {"container": "api", "container_id": container, "exit_code": 137, "oom_killed": True,
                     "restart_count": 7, "restart_delta_5m": 3, "reason": "oom_killed"}}
    events = [{"id": "demo-event", "incident_id": "demo-oom", "at_ts": now - 120, "kind": "alert", "level": "critical",
               "title": "检测到 api 内存溢出", "source": "docker", "detail": "容器已自动重启，但异常尚未确认恢复"}]
    server = {"sampled_ts": now, "sampled_at": time.strftime("%H:%M:%S"), "cpu_percent": 38.2,
        "cpu": {"model": "Demo CPU", "physical_cores": 4, "logical_count": 8}, "load": {"one": 2.1, "five": 1.7, "fifteen": 1.2},
        "memory": {"percent": 62.4, "total_mb": 8192, "used_mb": 5111, "available_mb": 3081},
        "disk": {"percent": 41, "total_gb": 80, "used_gb": 32.8, "path": "/"},
        "network": {"rx_kbps": 126.4, "tx_kbps": 43.1, "total_kbps": 169.5, "percent": 12}}
    server['memory']['swap'] = {'total_mb': 2048, 'used_mb': 256, 'percent': 12.5}
    server['disk']['inode'] = {'total': 5242880, 'used': 891289, 'percent': 17}
    server['disk_io'] = {'available': True, 'iowait_percent': 2.1, 'devices': [
        {'device': 'vda', 'read_kib_s': 428.5, 'write_kib_s': 1638.4, 'iops': 86.2,
         'await_ms': 3.2, 'busy_percent': 18.4}]}
    samples = [{"ts": now - 210 + n * 30, "cpu_percent": 72 + n * 3, "memory_percent": 84 + n * 4,
                "network_total_kbps": 120 + n * 40} for n in range(4)]
    return inventory, {"incident": incident, "timeline": events, "inventory": inventory, "server_now": server,
                       "resource_samples": samples, "window": {"since": now - 420, "until": now},
                       "window_available": True, "log_source": sources[0]["id"], "checked_at": now}


class Handler(agent.Handler):
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = agent._normal_path(parsed.path)
        if path in {"/", "/ui", "/ui/"}:
            page = agent._render_ui() if self._authenticated() else agent._render_login()
            self._write_html(200, page.replace('<body>', '<body><div role="status" style="padding:10px 24px;color:#059669;font-size:12px">本地演示 · 数据为模拟示例，不连接或修改服务器</div>'))
            return
        if path.startswith("/ui/") or path in {"/health", "/logout"}:
            super().do_GET()
            return
        if not self._require_auth_json():
            return
        inventory, evidence = examples()
        query = parse_qs(parsed.query)
        if path == "/operations/inventory":
            inventory["processes"].sort(key=lambda p: p["rss_mib"] if query.get("sort") == ["memory"] else p["cpu"], reverse=True)
            payload = inventory
        elif path == "/operations/incident":
            payload = evidence
        elif path == "/operations/logs":
            source = query.get("source", [""])[0]
            name = next((s["name"] for s in inventory["sources"] if s["id"] == source), "演示日志")
            lines = [f'{time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 180 + n * 2))} '
                     + ("ERROR worker exceeded memory limit; exit_code=137" if n in {31, 32, 33}
                        else "WARNING request latency above threshold; duration=1842ms" if n in {28, 29}
                        else f"INFO GET /cloud/health status=200 duration={12 + n % 8}ms") for n in range(80)]
            keyword = query.get("keyword", [""])[0].lower()
            if keyword:
                lines = [line for line in lines if keyword in line.lower()]
            level = query.get("level", ["all"])[0]
            if level != "all":
                lines = [line for line in lines if ("ERROR" if level == "error" else "WARNING") in line]
            payload = {"source": source, "name": name, "lines": lines, "checked_at": time.time(), "notice": "本地演示日志，不代表真实服务器。"}
        elif path == "/status":
            history = [{"ts": time.time() - 59 + n, "cpu_percent": 38 + math.sin(n / 8) * 12,
                        "memory_percent": 62 + math.sin(n / 14) * 3, "network_total_kbps": 169 + math.sin(n / 6) * 40} for n in range(60)]
            payload = {"agent": {"status": "ok"}, "csrf_token": self._csrf_token_for_request(),
                       "system": {"server": evidence["server_now"], "realtime_history": history,
                                  "docker": {"available": True, "containers": [
                                      {'id': 'a' * 64, 'name': 'api（演示）', 'image': 'python:3.12-slim', 'state': 'running',
                                       'cpu_percent': 138.5, 'memory_percent': 62.4, 'memory_usage': '1.2 GiB / 2 GiB',
                                       'ports': '127.0.0.1:8766 → 8766/tcp'}]}}}
        elif path == "/system-metrics":
            payload = {"server": evidence["server_now"]}
        elif path == '/system/swap':
            payload = {'supported': False, 'editable': False, 'enabled': True, 'owned': False,
                       'detail': '本地演示不操作 Swap；正式环境可一键开启 2 GiB Swap。', 'job': {}}
        elif path == '/performance/containers':
            payload = {'container': 'a' * 64, 'period': query.get('period', ['24h'])[0],
                       'interval_seconds': 30, 'retention_days': 7,
                       'notice': '本地模拟历史。正式环境每 30 秒持久采样；容器重建按新 ID 单独记录。',
                       'points': [{'ts': time.time() - 1800 + n * 30, 'cpu_percent': 80 + math.sin(n / 5) * 58,
                                   'memory_percent': 62 + math.sin(n / 12) * 12} for n in range(61)]}
        elif path == '/panel-maintenance':
            payload = {'supported': False, 'warning': '本地演示：维护操作已禁用，不连接或修改服务器。',
                       'version': {'revision': 'beb526c' + '0' * 33}, 'jobs': [],
                       'csrf_token': self._csrf_token_for_request(), 'backup_directory': '/var/backups/mini-deploy-agent',
                       'notice': '更新前保存程序快照，通过本地健康和运行版本检查后完成；失败时尝试恢复旧程序。配置、数据和业务文件保留。',
                       'backups': [{'id': 'program-20261012-demo', 'at': time.time() - 3600, 'kind': 'program'},
                                   {'id': 'installation-20261011-demo', 'at': time.time() - 86400, 'kind': 'full'}]}
        elif path == "/monitoring":
            payload = {"targets": [], "alerts": [evidence["incident"]], "events": evidence["timeline"],
                       "coverage": {key: {"status": "healthy", "checked_at": time.time()} for key in ("server", "docker")}}
        elif path in {"/terminal-ai/settings", "/terminal-ai/history", "/ssh"}:
            payload = {"enabled": False, "profiles": [], "sessions": [], "conversations": [], "csrf_token": self._csrf_token_for_request()}
        else:
            payload = {"notifications": {}, "images": [], "entries": [], "history_sources": [], "items": []}
        self._write_json(200, payload)

    def do_POST(self):  # noqa: N802
        if agent._normal_path(urlparse(self.path).path) == "/login":
            super().do_POST()
        else:
            self.close_connection = True
            self._write_json(403, {"detail": "本地演示为只读模式，不执行服务器操作。"})

    def log_message(self, *_):
        pass


def main():
    parser = argparse.ArgumentParser(description="只读运维 UI 演示")
    parser.add_argument("--port", type=int, default=60683)
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    password_path = configure(args.data_dir.resolve(), args.port)
    print(f"http://127.0.0.1:{args.port}/ui?view=events", flush=True)
    print(f"登录密码文件：{password_path}", flush=True)
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
