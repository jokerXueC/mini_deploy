# mini_deploy 安装与功能使用指南

<h2 id="start">先看这里</h2>

这份文档随面板安装，不依赖 GitHub 或外部文档服务。面板顶部的文档图标、功能检查中的「完整指引与命令」都能打开它。右上角可下载原文。

mini_deploy 管理已经运行的 Linux 服务器和业务。它不会替你购买服务器、配置云账号、发布业务代码或创建业务数据库。

**命令在哪里执行：**除明确标注「自己的电脑」外，均在面板所在服务器的 SSH 终端执行，使用 root 或先执行 `sudo -i`。不要把 `root@服务器:~#` 提示符复制进去。带「替换」说明的示例先改成自己的值，再执行；多条命令逐条执行，代码块内的 `&&` 表示前一步成功后才继续。

**路径说明：**文档示例采用标准服务 `mini-deploy-agent`、程序目录 `/opt/mini_deploy`、数据目录 `/var/lib/mini-deploy-agent`。自定义安装优先使用指南「本机排查命令」，那里根据正在运行的服务生成路径。业务目录（如 `/opt/zhunkeda`、`/opt/aimore`）与面板目录是不同的。

| 功能 | 安装后状态 | 你需要做的事 |
| --- | --- | --- |
| CPU、内存、磁盘、网络、性能历史 | Linux 后台自动采集 | 等待约 30 秒产生历史，检查磁盘可写 |
| 容器和镜像 | 检测本机 Docker | 没有 Docker 时按需安装 |
| 容器与服务器异常 | 自动观察可读取的资源 | 按业务需求设置阈值和通知 |
| 网站、接口监测 | 需要指定业务地址 | 添加 URL、预期响应，验证一次 |
| 请求历史、地图和导出 | 需要接入实际流量 | 选择 Nginx/Caddy 转发规则，确认接入 |
| 域名、证书 | 自动扫描已有配置 | DNS、云安全组、证书申请与续期需验证 |
| SSH、远程文件 | 依赖 Paramiko 与目标 SSH | 提供账号并核对指纹 |
| AI 运维 | 需要自己的模型服务 | 配置模型、测试连接、授权本轮操作 |
| 登录防护 | 默认启用内置失败阈值策略 | SSH 拦截还需 nftables、有效日志和识别出的 SSH 端口 |
| 基础防护 | 默认关闭 | 主动开启并逐项核对生效范围 |
| 外部通知 | 默认未配置 | 填写渠道并发送测试 |

「依赖就绪」「已保存配置」「待验证」不等于故障，也不等于端到端成功。没有业务网站就无需把网站相关项全部变绿。

<h2 id="install">安装与首次登录</h2>

### 1. 准备服务器

推荐先使用 Debian 12/13 或 Ubuntu 22.04/24.04 的普通云服务器。运行环境必须有 systemd、root 权限和 Python 3.10 以上；不要直接在不运行 systemd 的普通 Docker 容器里安装面板。其他发行版先确认相同前提，尚未完成所有发行版实机验收。

在 Debian/Ubuntu 执行基础依赖安装：

```bash
apt-get update && apt-get install -y git curl ca-certificates python3 util-linux
```

确认版本和系统环境：

```bash
python3 --version
ps -p 1 -o comm=
df -h /opt /var
```

Python 应至少为 3.10，PID 1 应为 systemd，程序和数据磁盘都应有剩余空间。旧发行版优先升级操作系统；不要随意替换系统 Python 或给系统 Python 强制执行 pip 安装。

### 2. 下载并安装

以下一整行同时适用于首次安装和标准旧安装升级：下载到新的临时目录，安装器读取原环境文件并保留既有数据，不要求程序目录有 `.git`：

```bash
mini_deploy_source="$(mktemp -d /tmp/mini-deploy-source.XXXXXX)" && git clone --depth 1 --branch main https://github.com/jokerXueC/mini_deploy.git "$mini_deploy_source" && SETUP_NGINX=no SETUP_HTTPS=no bash "$mini_deploy_source/install.sh"
```

GitHub 无法访问时使用 Gitee 这一行，二选一：

```bash
mini_deploy_source="$(mktemp -d /tmp/mini-deploy-source.XXXXXX)" && git clone --depth 1 --branch master https://gitee.com/XC1960/mini_deploy.git "$mini_deploy_source" && SETUP_NGINX=no SETUP_HTTPS=no bash "$mini_deploy_source/install.sh"
```

这两条命令先提供 IP 访问的面板，不改现有 Nginx/Caddy 网站入口。安装过程中选择语言并设置管理员密码，输入密码时终端不显示字符。旧安装保留原密码。需要容器管理和持久请求采集时，在 Docker 安装询问中选「是」；只看主机资源可选「否」，以后再安装。

安装器尝试补装 SSH 所需的 Paramiko；包源失败会提示依赖不可用，按 [SSH 指南](#ssh)处理。云安全组、DNS 和模型密钥无法由安装器代填。

### 3. 验收安装

```bash
systemctl status mini-deploy-agent --no-pager -l
curl -fsS http://127.0.0.1:6868/health
```

服务应为 `active (running)`，健康接口返回 JSON 且 `status` 为 `ok`。浏览器访问 `http://服务器公网IP:6868`，使用安装时的面板密码登录，再进入「功能检查」。主机健康不代表 SSH、模型、网站和通知都已配置。

<h2 id="access">打不开网页与 HTTPS</h2>

### 本机能访问，公网打不开

在云厂商控制台进入这台实例的安全组，添加入方向 TCP 6868，来源优先填写自己的公网 IP `/32`；IPv6 使用对应 `/128`。SSH 端口通常为 22，保留已有规则。云安全组不能通过面板里的开关自动修改。

服务器检查监听与健康：

```bash
ss -lntp 'sport = :6868'
curl -fsS http://127.0.0.1:6868/health
```

安装器会尝试适配已启用的 UFW/firewalld。仍被主机防火墙拦截时，只在实际使用的防火墙中添加规则，二选一，不要关闭整个防火墙。UFW 示例先把 `203.0.113.10` 换成自己的公网 IP：

```bash
ufw status verbose
ufw allow from 203.0.113.10 to any port 6868 proto tcp
```

firewalld 示例同样先替换来源 IP，在当前活动 zone 中使用：

```bash
firewall-cmd --get-active-zones
firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="203.0.113.10/32" port port="6868" protocol="tcp" accept' && firewall-cmd --reload
```

若网卡使用的不是默认 zone，在添加命令中补上 `--zone=实际zone`。不使用这两种防火墙时不要为了面板启动另一套规则。

### 没有域名也能安全使用 SSH 和 AI

在**自己的电脑**执行，替换账号和服务器 IP，端口不是 22 时修改 `-p`：

```bash
ssh -p 22 -N -L 16868:127.0.0.1:6868 root@服务器IP
```

保持该窗口运行，浏览器打开 `http://127.0.0.1:16868/ui`。本机端口被占用可把两处 `16868` 换成其他空闲端口。

### 使用已有 Nginx/Caddy 提供 HTTPS

先在 DNS 服务商添加面板域名的 A 记录指向服务器 IPv4。仅在服务器确实有可用 IPv6 时添加 AAAA。云安全组允许 80/443。已有代理继续使用，不要为了面板再启动另一个占用 80/443 的代理。

同机 Nginx 的 HTTPS server 内可使用以下转发片段，证书与 server_name 沿用该域名自己的配置：

```nginx
location / {
    proxy_pass http://127.0.0.1:6868;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_buffering off;
    proxy_read_timeout 300s;
}
```

同机 Caddy 示例，将域名换成自己的面板域名：

```caddyfile
panel.example.com {
    reverse_proxy 127.0.0.1:6868 {
        header_up X-Real-IP {remote_host}
        flush_interval -1
    }
}
```

修改前备份实际配置，校验成功后再重载。常见本机命令如下，路径不同先替换：

```bash
nginx -t && systemctl reload nginx
```

```bash
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && systemctl reload caddy
```

同机回环代理需要在 `/etc/mini-deploy-agent.env` 中设置 `DEPLOY_TRUST_LOOPBACK_PROXY_HEADERS=true`，编辑已有键而非重复追加，随后 `systemctl restart mini-deploy-agent`。面板读取的是 `X-Real-IP`，上例必须保留覆盖该请求头的指令，不能直接转发客户端自行填写的值。示例适用于客户端直达代理；前面还有 CDN 时，需先核对整条可信代理链。Docker 代理中的 `127.0.0.1` 指的是代理容器自身，不能直接套用上面的宿主机地址，也不能通过任意信任转发头解决；不确定网络时先用 SSH 隧道。

没有现成代理且想让安装器创建面板域名入口：先完成 DNS 和端口设置，从上节临时源码目录执行 `DEPLOY_DOMAIN=panel.example.com SETUP_NGINX=yes SETUP_HTTPS=yes bash "$mini_deploy_source/install.sh"`，替换域名，并按证书申请提示输入邮箱。仅在 80/443 没有被其他代理占用时使用。

<h2 id="login">登录密码与安装路径</h2>

面板密码、服务器 SSH 密码和模型 API Key 是三种不同的凭据。面板没有通用默认密码。

忘记面板密码时，在服务器用「本机排查命令」生成的修改密码命令；标准安装为：

```bash
python3 /opt/mini_deploy/agent.py admin set-password && systemctl restart mini-deploy-agent
```

按提示交互输入，不要把密码写进命令行。需要注销所有现有面板会话时执行：

```bash
python3 /opt/mini_deploy/agent.py admin reset-session && systemctl restart mini-deploy-agent
```

确认实际安装路径与运行程序：

```bash
systemctl show mini-deploy-agent --property=WorkingDirectory --property=ExecStart --no-pager
```

不要在业务目录里执行 `git pull` 来更新面板。出现 `not a git repository` 只说明那里没有 Git 元数据，按 [升级说明](#maintenance)从新临时目录安装即可。

<h2 id="resources">实时资源与容器观测</h2>

登录后打开「服务器状态」，CPU、内存、网络与磁盘自动采样；无需安装 Prometheus。CPU 显示核数，内存显示容量，逻辑处理器视图按需展开。资源观测可切换整体或已采集的容器，实时与历史是不同的数据窗口。

验收：连续观察 30 秒，确认采样时间推进。数值不变不一定故障，空闲主机的数值本来就可能稳定；核对时间戳。手机切到后台暂停页面刷新，返回后应恢复，服务器后台采样不依赖网页是否打开。

需要和系统对照时执行只读命令：

```bash
uptime
free -h
df -h
```

Docker 看不到容器先查 [Docker](#docker)；实时正常、历史为空查 [性能历史](#history)。

<h2 id="history">性能历史、进程现场与报告</h2>

主机每秒采样，约每 30 秒持久化平均值与峰值。安装后需要等待第一批数据，不会凭空补出安装前的 24 小时。进入「服务器状态 → 性能历史」，可查看 1 小时、24 小时和 7 天，开启自动更新并观察最新采样时间。

点击曲线或资源观测时间格，查看保留的进程现场与关联事件。进程现场是采样时的数据；缺失的过去进程不能用当前进程代替，也不能单凭同时出现判定故障根因。

导出 HTML 用于离线图表查看，CSV/ZIP 用于表格分析。下载内容是导出时的快照。历史有保留时间和容量预算，不是无限期归档。

实时正常但没有历史，执行：

```bash
journalctl -u mini-deploy-agent --since '1 hour ago' --no-pager -l | grep -Ei 'performance|persistence|database|disk|error' | tail -n 60
df -h /var/lib/mini-deploy-agent
df -i /var/lib/mini-deploy-agent
```

自定义数据目录使用「本机排查命令」。`database or disk is full` 要结合实际磁盘空间与数据库上限判断；已修复的旧版 128 MiB 历史库上限问题通过升级恢复轮换，不需要删除数据库。磁盘真的已满则先核对占用并释放已确认可清理的空间。

<h2 id="docker">Docker、镜像与日志</h2>

面板管理的是本机 Docker。仅存在远程 SSH 连接不会自动把远程容器接入首页。

先检查现有环境：

```bash
docker version
docker info
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
```

命令不存在且确认需要安装 Docker 时，Debian/Ubuntu 执行：

```bash
apt-get update && apt-get install -y docker.io && systemctl enable --now docker
```

已有 Docker 时不要重复安装另一套发行版包。`Cannot connect to the Docker daemon` 时查看 `systemctl status docker --no-pager -l` 和 `journalctl -u docker -n 80 --no-pager -l`；确认服务本应运行再执行 `systemctl start docker`，无需先重启所有容器。其他发行版按 [Docker 官方安装说明](https://docs.docker.com/engine/install/)选择对应系统。

回到服务器状态刷新，确认显示实际容器名称和状态。日志面板选择容器、开启跟随，容器产生日志后应出现新行；旧日志没有变化不代表刷新失败。原命令核对时把容器名替换为实际名称：

```bash
docker logs --timestamps --tail 80 容器名称
```

镜像拉取失败先检查服务器到镜像仓库的网络和账号权限。镜像加速填写你实际可用的服务地址，保存后按页面提示验证；有运行容器且不能安全重启 Docker 时，页面会要求进一步处理。拉取镜像不等于业务容器已经升级。

<h2 id="requests">请求记录、地图与导出</h2>

### 推荐接入流程

需要本机 Docker、已经运行的 HTTP 业务，以及实际承载流量的 Nginx/Caddy。Python、Go、Java 等业务使用同一接入方式，不需要语言 SDK。

1. 打开「请求记录 → 接入后端」，检测现有入口。
2. 选择实际业务域名下的转发规则。一个域名有 API、Web 多条规则时分别接入，记录范围以所选规则为准。
3. 核对原后端、网关地址、网络、检测路径和预期状态，再点击「开启记录」。Nginx 会校验后重载；Caddy 当前实现可能重启，长连接可能短暂断开，操作前阅读方案提示。
4. 在浏览器访问原业务域名的对应路径。等待请求结束及后台采集，页面应显示「已收到真实请求并写入历史」。只访问面板、网关本机健康地址，不能验收整个公网链路。
5. 展开单条记录查看来源 IP、状态、耗时、上游与保留的请求头。点击地图地区查看 IP 与次数；内网、代理或无法定位的 IP 不会被编造为地理坐标。
6. 使用「导出」选择全部保留历史、来源或当前筛选，下载 CSV、HTML/ZIP。所谓全部是仍在保留预算内的已采集数据，不包括接入前或绕过网关的流量。

长轮询、SSE、WebSocket 往往在连接结束后才形成完整日志。401/404 可以证明服务有响应，但不等于业务健康；检测路径优先使用真正的 `/health` 并设置预期 200/204。

### 自动检测不到：先找实际配置

Docker 入口执行以下查询，替换容器名称：

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
docker inspect 容器名称 --format '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}'
docker inspect 容器名称 --format '{{json .Config.Cmd}}'
docker inspect 容器名称 --format '{{json .NetworkSettings.Networks}}'
```

在「未找到服务？调整检测来源」填写代理类型、容器名和容器内配置路径。挂载箭头右侧是容器内路径，左侧是宿主机路径。代理和业务各查一次网络，选择能到达后端的网络；容器内 `127.0.0.1` 不代表宿主机或其他容器。

本机代理执行：

```bash
systemctl show nginx caddy --property=ExecStart --no-pager
nginx -T 2>&1 | grep -E '^# configuration file|server_name|proxy_pass'
```

Nginx 要选实际站点规则所在文件，不只填写最外层 `nginx.conf`。自定义 `-c/-p` 参数也要核对；诊断输出可能含内部域名、上游地址，分享前遮盖敏感信息。

### 确实不支持自动接入：手动入口

多上游、动态地址、复杂继承或只读挂载可能无法安全自动改写。此时不要反复点开启或删旧记录。

1. 在「高级设置 → 网关维护 → 手动新建」填写原 HTTP 后端与可达网络，预览并创建。记下页面生成的网关地址。本机代理通常使用 `127.0.0.1:监听端口`；同网络 Docker 代理使用 `mini-gateway-入口标识:10000`。
2. 用 SSH 文件管理打开查询到的真实配置，先备份再编辑。下例假定是宿主机 `/etc/nginx/conf.d/app.conf`，使用前替换成实际路径：

```bash
cp -a /etc/nginx/conf.d/app.conf "/etc/nginx/conf.d/app.conf.before-mini-deploy.$(date +%Y%m%d-%H%M%S)"
```

3. 保持域名、匹配范围、证书与原超时不变，仅将所选规则的唯一转发目标改为页面提供的网关地址。Nginx 例子如下，`18080` 必须换成实际端口，不要把整份配置替换成这一行：

```nginx
proxy_pass http://127.0.0.1:18080;
```

Caddy 对应上游示例：

```caddyfile
reverse_proxy 127.0.0.1:18080
```

4. 网关的后端仍必须是原业务地址，不能填回这个网关自己或公网域名造成转发循环。带路径改写、HTTPS 上游、gRPC、复杂多上游不应套用这个简单示例，需要按现有架构单独处理。
5. 本机 Nginx 执行 `nginx -t && systemctl reload nginx`；Docker Nginx 执行下列命令，替换代理容器名及自定义启动参数：

```bash
docker exec 代理容器名 nginx -t && docker exec 代理容器名 nginx -s reload
```

Caddy 先校验实际文件。本机标准服务可使用前述 validate/reload 命令；Docker Caddy 按实际启动方式应用配置，常见标准 Caddyfile 实例可执行：

```bash
docker exec 代理容器名 caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && docker exec 代理容器名 caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
```

管理 API 被禁用或配置不支持重载时，需要原业务维护者安排重启；不要为了记录流量强行重建业务。校验失败不执行重载，先从备份核对并恢复所改规则。

### 昨天有记录，今天没有

```bash
docker ps -a --filter name=mini-gateway- --format 'table {{.Names}}\t{{.Status}}'
docker logs --timestamps --tail 5 实际网关容器名
```

网关运行但最新日志停在过去，通常要核对业务是否绕过网关。进入请求记录看对应来源的「自动修复」和接入范围，支持的原规则可以重新应用。业务发布模板覆盖代理配置时，确认是否启用了「自动恢复同一规则」，同时把采集路径纳入原业务配置管理。未经过网关的流量无法补回。

已有代理日志预览只是近期日志窗口，不等于持久网关历史。CDN/代理后的真实 IP 必须建立正确的可信代理链；地图显示代理 IP 时不要把任意来源的 X-Forwarded-For 都设为可信。

<h2 id="certificates">域名、证书与续期</h2>

「域名与证书」会扫描本机及 Docker 的受支持配置。点击重新扫描并核对域名、代理、挂载路径和有效期；发现配置不证明公网已经到达该入口。

需要自己配置的内容：在 DNS 服务商创建正确 A/AAAA 记录；在云安全组允许 80/443；证书签发时完成 HTTP 或 DNS 验证。先在服务器查询 DNS：

```bash
getent ahosts 你的域名
curl -I --max-time 15 https://你的域名
```

已有 Caddy 自动 HTTPS 或 Certbot 自动续期继续由原服务管理。不要上传一份手工证书覆盖正在正常续期的证书链。面板允许替换的手工 Nginx 证书会显示对应入口，上传 PEM 完整证书链和匹配私钥后，由面板备份、校验并应用。

使用 Certbot 的服务器，可核对证书与续期：

```bash
certbot certificates
systemctl list-timers --all --no-pager | grep -i certbot
certbot renew --dry-run
```

`--dry-run` 会联系测试签发服务并可能运行已有续期钩子，安排在可验证的时段执行。HTTP 验证需外网到达 80；DNS 验证需要对应插件与凭据。不同 DNS 服务商配置不同，按 Certbot 插件说明设置，凭据文件不要发到 AI 或聊天里。

符号链接、只读挂载、单文件挂载、共享关系不明确或不支持的配置，面板可能只展示而不能直接替换，需在原证书管理位置处理，再回面板重新扫描。

<h2 id="monitoring">网站、接口与异常中心</h2>

在「异常中心 → 网站与接口监测」添加实际 URL，例如 `https://业务域名/health`。默认接受 200–399；业务健康接口建议在高级检查中明确填写 200 或 204。按需设置 GET/HEAD、认证请求头、JSON 字段或文本校验。POST 只用于明确无业务副作用的健康接口，不能拿下单、支付等接口做周期探测。

点击开始监测，等待一轮结果，确认状态与响应内容符合预期。服务器指标正常不等于业务正常；也不要拿必须登录的首页返回 401 作为成功标准。

异常中心显示持续异常、恢复与关联现场。「已知晓」清除待处理提示，但不会把仍在失败的业务改成健康。误报先检查预期状态和业务特征，再调整阈值，不要直接把所有错误都纳入成功。

可使用服务器命令核对同一路径，先替换 URL：

```bash
curl -sS --max-time 15 -o /dev/null -w 'HTTP=%{http_code} total=%{time_total}s\n' https://业务域名/health
```

<h2 id="notifications">异常通知</h2>

进入「通知配置」，选择实际可用的钉钉或邮件渠道。钉钉填写机器人 Webhook，启用加签时填写相应密钥；邮件填写 SMTP 地址、端口、加密方式、用户名、授权码和收件人。邮箱授权码不一定是网页登录密码。

保存后点击对应测试按钮，确认消息真正到达。测试会发送消息，可能受渠道频率和费用规则影响。保存成功但收不到消息不算验收通过。

检查机器人所在群是否正确、加签与关键词规则是否匹配，邮箱是否允许 SMTP、是否进垃圾箱，服务器是否能访问 SMTP 服务。许多云厂商限制 25 端口，优先使用服务商支持的 465/587，不要任意关闭 TLS。

仍失败时查看面板日志中的投递错误，分享时隐去 Webhook、令牌、邮件授权码。网站异常通知还需要先添加监测目标；主机在线不能替代业务探测。

<h2 id="ssh">SSH 与远程文件</h2>

### 补齐依赖

面板服务器需要 Paramiko；目标服务器需要 SSH/SFTP，目标不需要安装 mini_deploy。标准 Debian/Ubuntu 安装缺少依赖时：

```bash
apt-get update && apt-get install -y python3-paramiko && systemctl restart mini-deploy-agent
```

使用「本机排查命令」检查实际运行 Python 的导入结果。若使用自定义 Python/虚拟环境，发行版包可能不在它的搜索路径中；在已有专用虚拟环境中执行其 `bin/python -m pip install -r /实际面板目录/requirements-ssh.txt`，再重启面板。不要对受系统管理的 Python 使用 `--break-system-packages`。

### 建立连接

1. 打开「远程终端」。公网推荐 HTTPS 或 [SSH 隧道](#access)；HTTP 下需阅读风险并确认本次登录允许使用，确认后控件才启用。
2. 新建连接，填写目标服务器 IP、SSH 端口、用户名和密码/私钥。保存地址不会保存密码或私钥。
3. 首次连接核对主机指纹。在目标服务器的可信控制台执行与页面算法对应的指令，例如 Ed25519：

```bash
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256
```

4. 指纹一致后认证，终端执行 `pwd` 验证当前登录目录。指纹变化先核对是否重装或更换服务器。
5. 点击文件入口浏览目录；读写权限是 SSH 登录用户的权限。文本保存提供差异与备份，不提供任意删除功能。

连接路径是浏览器 → 面板服务器 → 目标 SSH。目标安全组要允许**面板服务器**访问 SSH 端口，不只是允许你的电脑。检查目标是否监听：

```bash
ss -lntp | grep sshd
```

从面板服务器测试到目标（会尝试认证，填写真实账号与 IP）：

```bash
ssh -p 22 -o ConnectTimeout=10 用户名@目标IP
```

不要为连接方便随意开启 root 密码登录。目标要求跳板机、硬件密钥或交互式 MFA 时，当前面板连接方式可能不支持，应继续使用原管理入口。

<h2 id="ai">AI 运维与代码分析</h2>

模型配置不需要先登录 SSH。在「远程终端 → 模型设置」选择 OpenAI 兼容或 Anthropic/Claude 原生，填写服务地址、模型标识和自己的 API Key。常见地址示例：

| 服务 | Base URL |
| --- | --- |
| OpenAI 官方 | `https://api.openai.com/v1` |
| Claude 官方原生协议 | `https://api.anthropic.com/v1` |
| 面板服务器本机 Ollama 兼容接口 | `http://127.0.0.1:11434/v1` |

第三方平台使用它给出的 Base URL 与实际支持的模型名，不要把聊天网页地址粘进来。先测试连接，再保存。测试会调用模型，可能消耗额度。普通聊天成功也不保证该模型支持工具调用，运维分析需要支持工具的模型。

**本机诊断：**从异常或性能历史打开 AI，目标是面板所在服务器，使用面板已授权的本机诊断工具，因此无需额外输入 SSH 密码。代码分析时选择或提供项目根目录，例如 `/opt/aimore`，明确只读范围；项目目录必须实际存在且可读。

**远程操作：**先连接目标 SSH，再从终端 AI 提问，确认标题中的服务器、账号与本轮权限。默认「操作前确认」；「自动操作」需要单独授权，连接、模型或登录变化后需重新授权。显式删除等危险命令会被阻止，不能把自动操作理解成任意 Shell 无限制执行。

推荐第一次用只读任务验收：「只读检查当前服务器的 CPU、内存、磁盘和 Docker 状态，引用实际工具结果」；代码分析可以问：「仅查看 /opt/aimore 中与某接口相关的代码，不修改文件，给出文件与行号」。核对执行记录真的出现工具调用，不要把模型口头建议当成已经执行。

若回答没有工具，先确认进入的是本机诊断还是已连接的远程会话、目标和权限是否正确、模型是否支持工具协议、面板是否已更新。无需为了只读分析授予完全修改权限。

对话和工具记录保存在面板数据目录，可从历史入口查看。发送给模型的代码、日志与工具结果会做常见凭据脱敏，但业务自定义敏感信息仍需控制读取范围。模型额度、网络、服务商超时或工具拒绝应查看实际错误，不要反复放大权限。

<h2 id="security">登录防护、IP 封禁与基础防护</h2>

### 登录记录与自动封禁

安全中心展示面板与主机 SSH 登录记录。面板登录立即记录，SSH 日志约每 10 秒采集；来源筛选要选对。默认策略是同一来源中，同一 IP 在 10 分钟内失败 5 次，首次封禁 1 小时。解封后重新达到阈值，自动封禁逐次翻倍为 2、4、8、16 小时，最长 7 天；解封后连续 30 天未再触发则恢复首次时长。累计次数独立保存，重启不会清空。手动封禁使用设置的基础时长，不增加自动违规次数。业务接口的 4xx/5xx 不计为 SSH 登录失败。

连续失败策略由面板内置实现，不要求安装 Fail2ban。已有 Fail2ban 保持独立。面板入口可在应用内拦截，主机 SSH 拦截还需 nftables、有效日志及可识别的端口。

如果界面提示缺少 nftables，Debian/Ubuntu 按需补装工具：

```bash
apt-get update && apt-get install -y nftables
nft --version
```

安装后回到安全中心重新检测或重新应用。无需为了面板执行 `nft flush ruleset`，也不要覆盖系统防火墙配置文件；面板只管理自己的规则表。

核对 SSH 日志来源：

```bash
journalctl -u ssh -u sshd --since '1 hour ago' --no-pager -n 80
```

未看到事件时再核对发行版认证日志的位置与采集提示，不能因此制造假数据。Docker 容器内 SSH 不属于宿主机 SSH 防护。

手动封禁：在 IP 记录对应操作或手动封禁入口填写实际 IP、来源和时长，确认后核对当前封禁列表。当前管理地址和白名单地址受保护。测试请用另一可控地址，避免把唯一管理入口封掉。

地图显示公网 IP 的近似归属地，点击地区可看 IP 和次数。底图随面板提供；归属地服务只查询公网 IP，网络受限时会显示待定位或未知，不影响日志本身。

### 基础防护开关

打开「基础防护」，等待任务结束，再展开「生效范围与详情」，逐项查看登录、连接和网站规则。

| 层级 | 当前能力 | 前提与边界 |
| --- | --- | --- |
| 登录防护 | 失败统计、临时封禁、到期解封 | 面板与可识别的宿主机 SSH |
| 连接防护 | 受支持的 80/443 TCP 新建连接限速 | 主机 nftables；不等于 UDP/QUIC 防护 |
| HTTP 防护 | 支持的 Nginx 入口请求率与并发限制 | 配置可可靠修改；Caddy 不是自动套用 Nginx 指令 |
| 大规模 DDoS、刷带宽 | 需要上游/CDN/云防护 | 本机规则无法把已经占满公网链路的流量消掉 |

部分启用要按具体入口提示处理，不能把总开关开启当作所有站点受保护。更复杂 WAF、验证码、蜜罐或云清洗不属于当前一键开关已实现的能力。

<h2 id="swap">Swap 开关</h2>

在「服务器状态 → 更多性能指标 → Swap」切换。面板新建 2 GiB Swap 需要 root、受支持的 ext4/XFS 文件系统及足够空间；创建后至少剩余 1 GiB。内存不足时关闭可能被拒绝，应先减轻负载。

只读核对命令：

```bash
swapon --show
free -h
```

面板创建的 Swap 支持开机启用，关闭后保留文件。已有系统 Swap 的开关只临时控制，不改原有开机配置，重启可能恢复。Swap 不能替代真实内存，也不保证高负载性能更好。

<h2 id="maintenance">更新、备份与恢复</h2>

兼容更新优先通过「服务器状态 → 面板维护」检查 GitHub/Gitee 版本、确认更新。更新时面板可能短暂断开，独立维护进程会检查新版本健康；失败时尝试恢复原程序，回到页面看最终状态。

遇到旧更新器误判文件清单、依赖安装变化或数据兼容变化时，使用下面一行从临时目录升级；它沿用标准环境文件中的安装路径，不要求程序目录是 Git 仓库：

```bash
mini_deploy_update="$(mktemp -d /tmp/mini-deploy-update.XXXXXX)" && git clone --depth 1 --branch main https://github.com/jokerXueC/mini_deploy.git "$mini_deploy_update" && SETUP_NGINX=no SETUP_HTTPS=no SETUP_DOCKER=no bash "$mini_deploy_update/install.sh" && systemctl status mini-deploy-agent --no-pager -l
```

Gitee 对应分支为 `master`，地址为 `https://gitee.com/XC1960/mini_deploy.git`，替换命令中的 branch 和仓库地址。不要在仓库 URL 中填写密码。`terminal prompts disabled` 可能是匿名访问或网络限制，不是服务器 root 密码错误。

安装器遇到后台写入占锁会等待最多 60 秒；超时仍不执行升级。只读查占锁进程：

```bash
lslocks --notruncate -o PID,COMMAND,TYPE,MODE,PATH | grep -F /run/mini-deploy-agent/maintenance.lock
systemctl list-units --all --no-pager 'mini-deploy-maintenance-*'
```

`READ` 可来自正常后台操作，`WRITE` 表示独占锁。等待任务结束后再试；持续不释放时保留进程和服务日志给维护者，不删除锁文件绕过互斥。

完整备份优先使用「面板维护 → 备份并校验」。标准布局也可执行：

```bash
bash /opt/mini_deploy/scripts/backup-installation.sh
```

记下输出的备份目录，验证时替换最后的示例路径：

```bash
python3 /opt/mini_deploy/scripts/verify_backup.py /var/backups/mini-deploy-agent/实际备份目录
```

自定义路径使用安装器打印的参数化备份命令。验证成功不等于已经恢复；网页「恢复程序」只回退程序，保留当前数据配置。完整数据库/配置恢复需要停写并按备份记录核对原路径，不提供一个会盲目覆盖生产数据的通用解压命令。

面板备份不能替代业务数据库、Docker Volume、外部代理配置及 Swap 的独立备份。安装器已有备份和重启保护，但整目录原子切换、断电恢复及完整自动回滚仍有待完善。

<h2 id="troubleshooting">问题定位与反馈</h2>

按顺序区分「面板未启动」「入口不通」「某项功能未开启」，不要一上来重装或清空数据。

```bash
systemctl status mini-deploy-agent --no-pager -l
curl -fsS http://127.0.0.1:6868/health
journalctl -u mini-deploy-agent -n 100 --no-pager -l
bash /opt/mini_deploy/scripts/doctor.sh
```

自定义安装使用本页的本机命令。诊断脚本读取环境、服务、路径和健康状态，不会代替你修复业务配置。不要为了反馈直接贴出整个 `.env`、私钥、模型配置文件或数据库。

| 现象 | 下一步 |
| --- | --- |
| 本机 health 正常，公网打不开 | 检查安全组、主机防火墙、代理地址和 DNS |
| SSH/模型控件灰色 | 核对依赖、HTTP 确认及登录会话；无需先连接 SSH 才能配置模型 |
| Docker 为空 | 用 docker ps 区分没有容器与引擎不可用 |
| 请求记录为空 | 核对入口范围、最近网关日志、是否真的经过采集路径 |
| 资源实时有值，历史无值 | 查看持久化错误、空间、inode 与数据库状态 |
| 地图无点 | 核对是否有公网 IP 记录、归属地查询是否完成 |
| 安全开关开启但部分未生效 | 查看逐项覆盖范围、nftables 和具体代理兼容性 |
| 更新等待维护锁超时 | 保留 lslocks、维护任务列表与服务日志，不删锁 |
| 已保存通知但收不到 | 发送测试，核对渠道规则与实际投递错误 |

反馈时提供：操作时间及时区、系统发行版、面板版本、失败按钮/页面、原样错误提示和相关日志片段。先隐去密码、令牌和业务敏感信息。

<h2 id="acceptance">安装后逐项验收</h2>

按自己需要的功能逐项完成即可。选择不用的能力不算安装失败。

1. 面板：服务 active，本机健康接口 ok，自己的浏览器能登录。
2. 资源：实时采样时间推进；等待约 30 秒后历史开始出现，导出报告能打开。
3. Docker：容器列表与 docker ps 对得上；有新日志时面板能显示。
4. 请求：通过原域名访问选定规则，新的真实请求写入历史，导出能找到它。
5. 网站：健康接口检测结果正确，线上 HTTPS 域名和证书匹配，续期方式明确。
6. SSH：目标指纹核对成功，能执行 pwd，文件列表与登录账号权限一致。
7. AI：模型测试成功，只读问题有真实工具执行记录，目标服务器正确。
8. 安全：登录事件能出现；每层防护实际状态可确认，未覆盖部分有明确原因。
9. 通知：测试消息确实到达，业务监测目标已配置。
10. 维护：备份已验证，知道面板与业务数据的不同备份范围。

目前安装和功能已有自动化与浏览器回归，但 Linux 多发行版、新机、不同代理及云网络的完整实机矩阵尚未全部完成。使用本清单记录实际结果，不把「安装完成」当作所有外部依赖都已验证。
