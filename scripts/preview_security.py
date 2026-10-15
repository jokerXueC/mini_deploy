"""Read-only synthetic security preview; no login, host discovery or firewall writes."""
import argparse
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import time
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent  # noqa: E402
from security_center import ACTIVITY_PAGE_SIZE, finding  # noqa: E402
from scripts.preview_operations import Handler as OperationsHandler  # noqa: E402


def examples(jail='mini-deploy-panel'):
    now = time.time()
    return {'supported': True, 'read_only': True, 'installed': True, 'running': True,
            'panel_enabled': True, 'ssh_enabled': True, 'existing_ssh': False,
            'jails': ['mini-deploy-panel', 'mini-deploy-sshd'], 'client_ip': '203.0.113.10',
            'notice': '演示数据 · 当前 IP 保留。Docker 发布端口与反向代理入口需分别核实实际拦截。',
            'observation': {'checked_at': now, 'host_readable': True, 'docker_readable': True,
                'ssh': {'ready': True, 'ports': [2222], 'backend': 'systemd', 'detail': '已识别实际监听和日志来源', 'fingerprint': 'a' * 64},
                'checks': [{'name': 'Fail2ban 服务', 'state': 'known', 'detail': '运行中，已读取两条专属规则'},
                           {'name': 'SSH 日志', 'state': 'known', 'detail': 'systemd 日志可读，端口 2222'},
                           {'name': '防火墙对象', 'state': 'unknown', 'detail': '已读取配置，尚未从外部客户端验证拦截'}],
                'warnings': [], 'ports': [{'service': 'PostgreSQL', 'host': '0.0.0.0', 'port': 15432, 'container_port': 5432,
                    'container': 'demo-db', 'protocol': 'tcp', 'kind': 'docker', 'detail': '数据库发布到非回环地址，请核对安全组与访问需求'}],
                'center': {'checked_at': now, 'risks': [
                    finding('docker:socket', '容器挂载了 Docker 控制接口', 'ops-worker', '/var/run/docker.sock → /var/run/docker.sock；只读挂载也可能调用 API。', '确认是受信任的管理组件，不自动重建或移除挂载。', 'critical', 'docker'),
                    finding('port:postgres', 'PostgreSQL 监听于非回环地址', 'demo-db', '0.0.0.0:15432 → 5432/tcp', '核对安全组与访问需求，未执行公网连通探测。', category='exposure'),
                    finding('ssh:password', 'SSH 允许密码登录', 'sshd 默认配置', 'passwordauthentication = yes，不代表发生入侵。', '先验证密钥登录和备用连接，再调整认证配置。', category='login')],
                    'checks': [{'name': 'Docker 权限', 'state': 'known', 'detail': '已只读检查 4 个运行中容器'},
                               {'name': '配置变化', 'state': 'known', 'detail': '监测 6 个常见配置，仅保存摘要基线'}],
                    'notice': '只读检查常见位置，不会自动关闭端口或修改业务配置。'}},
            'activity': {'total': 5, 'page': 0, 'source': 'all', 'login_success_24h': 6, 'login_failure_24h': 83, 'bans_24h': 24, 'config_changes_24h': 1,
                'auth': {'readable': True, 'checked_at': now, 'detail': 'SSH systemd 日志已读取'},
                'items': [
                    {'id':'demo1', 'ts':now - 45, 'source':'ssh', 'kind':'login_failure', 'ip':'198.51.100.24', 'account':'admin', 'target':'SSH', 'detail':'SSH password 认证失败'},
                    {'id':'demo2', 'ts':now - 120, 'source':'fail2ban', 'kind':'ban', 'ip':'198.51.100.24', 'account':'', 'target':'mini-deploy-sshd', 'detail':'Fail2ban 规则封禁（自动或客户端操作）'},
                    {'id':'demo3', 'ts':now - 300, 'source':'ssh', 'kind':'login_success', 'ip':'192.0.2.8', 'account':'deploy', 'target':'SSH', 'detail':'SSH publickey 认证成功'},
                    {'id':'demo4', 'ts':now - 540, 'source':'config', 'kind':'config_change', 'ip':'', 'account':'', 'target':'/etc/ssh/sshd_config', 'detail':'检测到内容摘要变化；这是发现时间，不代表未授权修改。'},
                    {'id':'demo5', 'ts':now - 900, 'source':'fail2ban', 'kind':'unban', 'ip':'2001:db8::42', 'account':'', 'target':'mini-deploy-sshd', 'detail':'Fail2ban 解封'}]},
            'settings': {'maxretry': '5', 'findtime': '600', 'bantime': '3600', 'port': '6868',
                         'ignoreip': '127.0.0.0/8 ::1 203.0.113.10', 'bantime.increment': 'true'},
            'ssh_settings': {'maxretry': '5', 'findtime': '600', 'bantime': '7200', 'port': '2222',
                             'ignoreip': '127.0.0.0/8 ::1 203.0.113.10 192.0.2.8', 'bantime.increment': 'false'},
            'selected': {'name': jail, 'banned': ['198.51.100.24', '2001:db8::42'], 'bantime': '3600',
                         'findtime': '600', 'maxretry': '5', 'ignoreip': ['127.0.0.0/8', '::1', '203.0.113.10'],
                         'ban_details': {'198.51.100.24': {'started_at': now - 300, 'expires_at': now + 3300},
                                         '2001:db8::42': {'started_at': now - 600, 'expires_at': now + 6600}}},
            'history': {'readable': True, 'checked_at': now, 'total': 2, 'page': 0,
                        'detail': '增量采集 Fail2ban 日志；数据为模拟示例', 'notice': '保留已采集的 30 天记录，存在采集空档时明确提示。',
                        'items': [{'ts': now - 300, 'jail': jail, 'kind': 'ban', 'ip': '198.51.100.24',
                                   'reason': 'Fail2ban 规则封禁（自动或客户端操作）', 'observed_failures_10m': 5},
                                  {'ts': now - 600, 'jail': 'mini-deploy-sshd', 'kind': 'ban', 'ip': '2001:db8::42',
                                   'reason': 'SSH 规则封禁', 'observed_failures_10m': 8}]},
            'recent': []}


class Handler(OperationsHandler):
    def _authenticated(self):
        return True

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if agent._normal_path(parsed.path) in {'/security/center', '/security/fail2ban'}:
            query = parse_qs(parsed.query)
            jail = query.get('jail', [''])[0]
            data = examples(jail)
            if not jail:
                data.pop('selected')
            source = query.get('activity_source', ['all'])[0]
            if source != 'all':
                data['activity']['items'] = [item for item in data['activity']['items'] if item['source'] == source]
                data['activity']['total'] = len(data['activity']['items'])
                data['activity']['source'] = source
            activity = data['activity']
            page = max(0, int(query.get('activity_page', ['0'])[0]))
            activity.update(page=page, page_size=ACTIVITY_PAGE_SIZE)
            activity['items'] = activity['items'][page * ACTIVITY_PAGE_SIZE:(page + 1) * ACTIVITY_PAGE_SIZE]
            data['csrf_token'] = self._csrf_token_for_request()
            self._write_json(200, data)
        else:
            super().do_GET()

    def do_POST(self):  # noqa: N802
        self.close_connection = True
        self._write_json(403, {'detail': '本地界面演示，不执行任何服务器操作'})


def main():
    parser = argparse.ArgumentParser(description='安全中心只读演示')
    parser.add_argument('--port', type=int, default=60691)
    args = parser.parse_args()
    with ThreadingHTTPServer(('127.0.0.1', args.port), Handler) as server:
        print(f'http://127.0.0.1:{args.port}/ui?view=security', flush=True)
        server.serve_forever()


if __name__ == '__main__':
    main()
