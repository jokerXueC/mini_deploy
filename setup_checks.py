"""Read-only installation evidence; never probe or change a business service."""
from contextlib import closing, contextmanager
import math
from pathlib import Path, PurePosixPath
import shlex
import sqlite3
import time

from certificates import trusted_path


def installation_commands(app, home, env, service, runtime, *, linux=True):
    """Copyable commands for the actual installation; never expose env contents."""
    if not linux:
        return {'available': False, 'detail': '本地开发环境不生成 Linux 服务操作命令，请按指南在目标服务器执行。', 'commands': []}
    def q(value):
        return shlex.quote(str(value))
    service, app, home, env, runtime = str(service), PurePosixPath(app), PurePosixPath(home), PurePosixPath(env), str(runtime)
    commands = [
        ('status', '服务状态', 'maintenance', f'systemctl status {q(service)} --no-pager -l'),
        ('logs', '最近运行日志', 'troubleshooting', f'journalctl -u {q(service)} -n 100 --no-pager -l'),
        ('health', '本机健康检查', 'access', 'curl -fsS http://127.0.0.1:6868/health'),
        ('doctor', '安装与配置诊断', 'troubleshooting', f'DEPLOY_AGENT_ENV_FILE={q(env)} bash {q(app / "scripts/doctor.sh")}'),
        ('storage', '数据磁盘与 inode', 'history', f'df -h {q(home)} && df -i {q(home)}'),
        ('ssh', '检查实际 Python 的 SSH 依赖', 'ssh', f'{q(runtime)} -c {q("import paramiko; print(paramiko.__version__)")}'),
        ('password', '交互修改面板密码并重启面板', 'login',
         f'DEPLOY_AGENT_ENV_FILE={q(env)} DEPLOY_AGENT_SERVICE_NAME={q(service)} {q(runtime)} {q(app / "agent.py")} admin set-password && systemctl restart {q(service)}'),
    ]
    return {'available': True, 'app': str(app), 'data': str(home), 'service': service, 'runtime': runtime,
            'commands': [{'key': key, 'title': title, 'topic': topic, 'command': command,
                          'changes_server': key == 'password'} for key, title, topic, command in commands]}

@contextmanager
def database(path):
    path = Path(path).absolute()
    for suffix in ('', '-journal', '-wal', '-shm'):
        trusted_path(Path(str(path) + suffix))
    if not path.is_file():
        raise FileNotFoundError(path)
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.2)) as db:
        deadline = time.monotonic() + .2
        db.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
        db.execute('PRAGMA query_only=ON')
        yield db


def fresh(at, now, seconds):
    return type(at) in (int, float) and math.isfinite(at) and 0 <= now - at <= seconds


def snapshot(home, *, system, authenticated, ssh_available, ai, notifications,
             protection, history_error='', started_at=0, now=None, linux=True,
             discovered_sites=0, website_targets=0):
    now = time.time() if now is None else now
    home, items = Path(home), []

    def add(key, title, state, detail, view, steps, *, optional=True, at=None, action='查看设置'):
        items.append(dict(key=key, title=title, state=state, detail=detail, view=view,
                          steps=steps, optional=optional, evidence_at=at, action=action))

    add('login', '面板登录', 'verified' if authenticated else 'error',
        '管理员口令与会话签名已配置。' if authenticated else '管理员口令或会话签名缺失。', 'server',
        ['安装时设置管理员密码；不要向他人发送环境配置文件。',
         '公网访问推荐配置 HTTPS；HTTP 下远程终端需单独确认。'], optional=False, action='查看服务器')
    server, docker = system.get('server') or {}, system.get('docker') or {}
    at = server.get('sampled_ts')
    measured = any(type(server.get(k)) in (int, float) and math.isfinite(server[k]) for k in ('cpu_percent',))
    add('resources', '实时资源', 'verified' if measured and fresh(at, now, 15) else 'pending' if now - started_at < 60 else 'error',
        '最近 15 秒内收到主机资源采样。' if measured and fresh(at, now, 15) else '尚未收到有效的新采样；检查 Agent 服务日志。',
        'server', ['进入服务器状态，确认 CPU、内存、网络数值持续更新。',
                   '生产采样依赖 Linux /proc；本地预览不代表生产验收。'], optional=False, at=at, action='查看实时资源')

    state, detail, saved = 'pending', '等待首次历史写入，正常情况下约 30 秒；本页会自动复查。', None
    try:
        with database(home / 'performance/server.sqlite3') as db:
            row = db.execute('SELECT last FROM samples ORDER BY ts DESC LIMIT 1').fetchone()
        saved = row[0] if row else None
        if history_error:
            state, detail = 'error', '后台报告性能历史写入失败；请在性能历史查看存储错误并检查磁盘和目录权限。'
        elif fresh(saved, now, 90):
            state, detail = 'verified', '已核验最近 90 秒内的磁盘记录；重启后可继续查看历史。'
        elif saved is not None or now - started_at >= 90:
            state, detail = 'error', '历史库没有新的持久采样；实时图正常也不代表历史写入正常。'
    except FileNotFoundError:
        if history_error or now - started_at >= 90:
            state, detail = 'error', '历史库尚未建立或写入失败；检查 Agent 日志及数据目录可用空间。'
    except (OSError, ValueError, sqlite3.Error):
        state, detail = 'error', '无法只读核验性能历史库；检查数据目录权限、数据库状态或稍后重试。'
    add('history', '性能历史与报告', state, detail, 'server',
        ['进入服务器状态 → 性能历史，确认最新采样时间推进。',
         '切换最近 1 小时并导出报告；持续采样由后台完成，无需保持网页打开。'],
        optional=False, at=saved, action='查看性能历史')

    at = docker.get('sampled_at')
    if docker.get('available') and not docker.get('stale') and fresh(at, now, 65):
        state, detail = 'verified', f"后台已读取 {len(docker.get('containers') or [])} 个容器；零容器也属于有效结果。"
    elif docker.get('available'):
        state, detail = 'pending', '已有 Docker 数据，但采样已过期；等待后台刷新。'
    else:
        state, detail = 'setup', 'Docker 未安装、未启动或尚未采样；已有原生网站不需要为了监控主机安装 Docker。'
    add('docker', 'Docker 容器', state, detail, 'server',
        ['需要容器管理时，在安装向导选择安装 Docker，或由管理员安装并启动 Docker Engine。',
         '进入服务器状态确认容器列表；存在容器却无法读取时，检查 Docker 服务和 Agent 访问权限。'], at=at, action='查看容器')

    state, detail, checked = 'setup', '尚未发现持久采集来源；原生 Nginx 或 Docker Nginx / Caddy 均可从请求接入向导开始。', None
    try:
        with database(home / 'request-history/history.sqlite3') as db:
            rows = db.execute('SELECT checked,error FROM sources LIMIT 257').fetchall()
        if rows:
            checked = max((r[0] for r in rows if type(r[0]) in (int, float)), default=None)
            if any(r[1] for r in rows):
                state, detail = 'error', '部分采集来源报告错误；进入请求记录查看对应来源的修复提示。'
            elif any(not fresh(r[0], now, 120) for r in rows):
                state, detail = 'pending', '部分来源尚未完成近期采集检查；进入请求记录核对每个来源。'
            else:
                state, detail = 'pending', f'已检查 {len(rows)} 个持久采集来源；仍需核对入口配置及真实流量，不能仅凭游标推进判定接入成功。'
            if len(rows) > 256:
                state, detail = 'pending', '来源较多，本页只核验前 256 个；请到请求记录逐项确认。'
    except FileNotFoundError:
        pass
    except (OSError, ValueError, sqlite3.Error):
        state, detail = 'error', '无法只读核验请求历史；请进入请求记录查看采集错误。'
    add('requests', '请求记录与地图', state, detail, 'requests',
        ['点击接入请求记录，检测现有入口；支持的 Nginx / Caddy 规则可先预览再接入。',
         '持久采集需要本机 Docker；自定义或无法可靠识别的配置需按向导手动接入。',
         '用原域名发起一次真实请求，确认对应来源显示采集中，且新请求已写入历史。'], at=checked, action='接入与验证')
    add('certificates', '域名与证书', 'pending' if discovered_sites else 'setup',
        f'后台已发现 {discovered_sites} 个网站；发现配置不代表 DNS、公网入口或证书续期已经验证。' if discovered_sites else '尚未发现网站配置；可检测现有入口或手动添加站点。',
        'certificates', ['进入域名与证书，检测网站或添加站点，确认使用实际运行的 Nginx / Caddy 配置。',
                         '域名 DNS 解析和云安全组需在服务商处配置；HTTP 验证需要公网入口，DNS 验证需要相应凭据。',
                         '核对线上状态、证书域名与有效期，并测试续期；已有证书不等于自动续期已就绪。'], action='检查网站与证书')
    add('monitoring', '网站与接口监测', 'pending' if website_targets else 'setup',
        f'已配置 {website_targets} 个监测目标；请到异常中心核对检测结果与预期状态。' if website_targets else '尚未添加业务监测目标；主机与容器采样不会自动覆盖所有网站和接口。',
        'events', ['在异常中心添加网站或接口地址，设置预期状态码和需要的响应校验。',
                   '先验证一次正常结果，按需绑定通知；认证接口应提供专用健康检查地址。'], action='配置业务监测')
    add('ssh', 'SSH 与文件管理', 'available' if ssh_available else 'setup',
        'SSH 依赖可用；尚未在本页验证任何远程连接。' if ssh_available else '当前 Python 缺少 Paramiko 依赖；其他功能可继续使用。',
        'terminal', ['缺少依赖时重新运行安装脚本；网络或包源安装失败需按安装输出补装 Paramiko。',
                     '添加目标主机、端口及账户，核对主机指纹后认证；已保存的连接不等于登录成功。',
                     '远程主机须允许面板服务器访问 SSH 端口；文件权限沿用 SSH 登录用户。'], action='添加或测试连接')
    add('ai', 'AI 运维助手', 'error' if ai.get('error') else 'pending' if ai.get('configured') else 'setup',
        '模型配置无法读取；原文件已保留。' if ai.get('error') else '模型配置已保存；需要在模型设置测试连接，并确认本轮操作权限。' if ai.get('configured') else '尚未配置模型服务，支持 OpenAI 兼容与 Claude 原生接口。',
        'terminal', ['进入模型设置，填写服务地址、模型名称和 API Key（本地免密服务可留空）。',
                     '先测试连接再保存；代码分析与服务器操作还需选定目标及权限，模型接入不会自动授予操作权限。'], action='配置模型')
    phase = protection.get('phase')
    state = 'unsupported' if not linux else 'error' if phase in {'partial', 'interrupted'} else 'pending' if phase in {'on', 'applying'} else 'setup'
    detail = ('生产防护需要 Linux 主机权限。' if not linux else
              '防护存在未完成的项目；进入安全中心查看实际覆盖范围。' if state == 'error' else
              '已开启或正在应用；仍需核对每个入口的实际覆盖范围。' if state == 'pending' else
              '可按需开启基础防护；不会因安装面板自动接管现有网站或防火墙。')
    add('security', '登录与基础防护', state, detail, 'security',
        ['在安全中心开启所需防护，检查 SSH 日志是否可读、主机封禁是否生效及网站入口覆盖范围。',
         'SSH 主机封禁需要 nftables 与相应权限；不依赖安装 Fail2ban。',
         '本机限流无法抵挡超过服务器带宽的大规模攻击，需要云厂商或上游防护。'], action='查看防护范围')
    add('notifications', '异常通知', 'pending' if notifications else 'setup',
        '已启用通知渠道；需要发送测试确认投递，不会在检查时发送消息。' if notifications else '通知渠道尚未启用；不影响异常中心记录。',
        'notify', ['填写钉钉或邮件渠道，发送一次测试通知确认投递。',
                   '网站与接口异常还需添加相应监测目标；不要把主机在线等同于业务正常。'], action='配置并测试通知')
    return {'generated_at': now, 'items': items, 'environment': 'Linux 主机' if linux else '本地开发环境',
            'summary': {s: sum(item['state'] == s for item in items) for s in
                        ('verified', 'available', 'pending', 'setup', 'error', 'unsupported')}}
