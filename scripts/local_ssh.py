"""Run the authenticated SSH panel on loopback without Linux monitoring workers."""
import argparse
import json
import os
import secrets
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent  # noqa: E402
import ssh_terminal  # noqa: E402


class Handler(agent.Handler):
    def send_header(self, keyword, value):
        if keyword.lower() == "location" and value == "ui":
            value = "/ui?view=terminal"
        super().send_header(keyword, value)

    def do_GET(self):  # noqa: N802
        path = agent._normal_path(urlparse(self.path).path)
        if path in {"/", "/ui", "/ui/"}:
            if not self._authenticated():
                self._write_html(200, agent._render_login().replace(
                    "输入面板密码查看服务器状态。", "输入本地测试面板密码。"))
            else:
                page = agent._render_ui().replace('<body>', '<body><div role="status" '
                    'style="padding:8px 24px;font-size:12px;color:#059669">'
                    '本地 SSH 实测 · 连接真实服务器；本入口仅提供远程终端。</div>')
                self._write_html(200, page)
            return
        if path.startswith("/ui/") or path in {"/ssh", "/ssh/output", "/ssh/stream", "/logout", "/health"}:
            super().do_GET()
            return
        if not self._require_auth_json():
            return
        if path == "/status":
            self._write_json(200, {"system": {"server": {}, "docker": {"available": False}},
                "agent": {"status": "ok"}, "csrf_token": self._csrf_token_for_request()})
        elif path == "/monitoring":
            self._write_json(200, {"targets": [], "alerts": [], "events": [], "coverage": {}})
        else:
            self._write_json(404, {"detail": "本地实测入口仅提供远程终端。"})

    def do_POST(self):  # noqa: N802
        path = agent._normal_path(urlparse(self.path).path)
        if path == "/login" or path.startswith("/ssh/"):
            super().do_POST()
        else:
            self.close_connection = True
            self._write_json(403, {"detail": "本地实测入口仅提供远程终端。"})

    def log_message(self, *_):
        pass


def configure(directory, port=60678):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_path = directory / "panel-auth.json"
    password_path = directory / "panel-password.txt"
    if not config_path.exists():
        password = secrets.token_urlsafe(18)
        config = {"password_hash": agent._hash_password(password), "secret": secrets.token_hex(32)}
        agent._atomic_write_private_text(password_path, password + "\n")
        agent._atomic_write_private_text(config_path, json.dumps(config))
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if agent._parse_password_hash(config["password_hash"]) is None or len(config["secret"]) < 32:
        raise ValueError("本地登录配置无效；请检查 panel-auth.json，原文件已保留。")
    agent.UI_PASSWORD_HASH = config["password_hash"]
    agent.UI_SESSION_SECRET = config["secret"]
    agent.COOKIE_NAME = f"mini_deploy_local_ssh_{port}_session"
    agent.COOKIE_SECURE_MODE = "auto"
    agent.TRUST_LOOPBACK_PROXY_HEADERS = False
    agent.STATE_FILE = directory / "state.json"
    agent.AUDIT_LOG_FILE = directory / "audit.jsonl"
    agent.LOG_FILE = directory / "agent.log"
    # Use the normal manager: no test peer restriction or automatic session cookie.
    agent._ssh_manager_instance = None
    return password_path


def main():
    default_directory = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local/share"))) / "mini_deploy/local-ssh"
    parser = argparse.ArgumentParser(description="启动本地真实 SSH 测试面板，仅监听 127.0.0.1。")
    parser.add_argument("--port", type=int, default=60678)
    parser.add_argument("--data-dir", type=Path, default=default_directory)
    args = parser.parse_args()
    if not ssh_terminal.paramiko:
        parser.exit(1, "缺少 SSH 组件，请先执行 python -m pip install -r requirements-ssh.txt\n")
    password_path = configure(args.data_dir.resolve(), args.port)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        parser.exit(1, f"无法监听本地端口 {args.port}，可用 --port 指定其他端口：{exc}\n")
    print(f"http://127.0.0.1:{args.port}/ui?view=terminal", flush=True)
    print(f"本地面板登录密码文件：{password_path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if agent._ssh_manager_instance is not None:
            agent._ssh_manager_instance.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
