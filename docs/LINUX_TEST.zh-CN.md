# Linux 实测清单

实现已完成：全量自动化测试 476 通过、19 跳过（缺少 Linux/Docker 环境），另有真实后端站点 UI 增改删测试通过。Linux 实机验收尚未执行；以下是待执行清单，不是实测报告。

使用专用、可丢弃的 Linux 测试机，记录发行版、Python/Docker/Nginx 版本和待验收提交号。

仅测试面板安装、运维和请求监控。不创建真实业务部署任务、不 push、不删除服务器业务数据。所有变更使用隔离测试站点、容器和端口。

## 1. 安装与登录

按[快速上手](QUICKSTART.zh-CN.md)准备 Linux、systemd、Python 3.10+ 和 Git，并运行已核对版本的安装器。已有安装先备份，沿用原路径及环境文件；遇到路径、标记或备份错误就停止，不删除目录或强制接管。

默认安装的只读检查：

```bash
git log -1 --oneline
systemctl status mini-deploy-agent --no-pager
curl -fsS http://127.0.0.1:6868/health
ss -lntp
```

确认 Agent 为 active、健康状态正常、监听 `0.0.0.0:6868`。在云安全组按需允许 TCP 6868 后登录；验证服务器、Docker、域名证书和请求记录页面可访问。没有 Git 项目时也应正常使用。

## 2. 升级与部署停用

仅在隔离副本中使用带旧项目、队列、Nginx 元数据和证书的测试数据。启动任何旧 Agent 前确认不会执行真实脚本，不在真实业务上制造待执行任务。

- 升级前记录业务文件/配置/脚本摘要、服务状态、容器 ID、挂载和站点访问结果，保存快照与完整安装备份。
- 确认旧 Agent 的活动部署进程已结束，再升级到待测版本。升级失败时保留原数据和诊断信息。
- UI 不再提供添加 Git 仓库、部署脚本、WebHook、初始化业务环境、部署或部署回滚入口。
- 对旧手动部署、回滚、WebHook 及初始化接口做负向测试：使用测试身份和测试数据，确认返回 HTTP `410 Gone`，且没有入队、脚本执行、Git 更新或业务文件生成。
- 确认站点保存到 `sites.json`，旧 `projects.json` 仅只读提取站点；监控写入 `monitoring-state.json`，原 `state.json` 保留且不加载队列。重启 Agent 后重复检查，用无副作用的执行标记和进程记录佐证。
- 对比升级前后：旧业务文件、配置、脚本、服务、容器及持久数据保留，服务未被停止，容器未被重建；旧配置和状态不因停用部署而被清空。
- 已有 Nginx 入口、证书、请求来源和网关继续可用；脱敏记录差异。

若出现部署触发、队列恢复或填写脚本要求，记为回归缺陷，不修改旧数据来让测试通过，也不向真实仓库 push。

## 3. 服务器与 Docker

- 未安装 Docker 时，验证默认 `SETUP_DOCKER=ask` 在终端询问、默认回答“否”，无终端时跳过；仅明确同意后安装 Docker。已有 Docker 保留，不要求 Compose 或 PyYAML。
- 连续观察 CPU、内存、磁盘和网络，确认采样更新；后台切换及无数据状态不报错。
- 对照 Docker 状态核对容器资源、端口和日志。
- 用专用测试镜像验证搜索、拉取和列表刷新；拉取不应自动更新业务容器。
- 容器/镜像删除能力只在可丢弃 fixture 上测试，确认运行容器和被使用镜像受保护，数据卷及宿主机挂载目录保留。无隔离 fixture 时跳过删除验证并注明原因。

## 4. Nginx 独立站点与证书

使用预先准备的测试 HTTP 上游和自有测试域名；不要创建部署项目或占位脚本。本机与 Docker Nginx 分别记录结果：

1. 新增、编辑并读取站点名称、域名和端口；验证健康检查可选，高级 key 留空自动生成。选择 Nginx 实例，在入口设置后端地址。
2. 检查并预览配置，应用后验证 HTTP 与上游一致。
3. 验证重复域名、端口冲突、不可达上游和外部配置不会被静默覆盖。
4. 上传测试 PEM 链和私钥，验证有效期、密钥匹配、HTTPS、替换和停用；私钥不能回显。
5. 验证错误配置或重载失败会保留/恢复原配置，其他站点不受影响。
6. 重启 Agent，确认站点、上游和证书仍有效，且不会触发旧部署队列。
7. 删除无关联的测试站点仅移除登记；有关联 Nginx 配置或证书时阻止删除，服务器业务文件保留。

Docker 模式额外核对配置/证书 bind 挂载及网络可达性；bridge 容器不能通过自身 `127.0.0.1` 访问宿主机业务。已有容器不得为接入而自动重建。没有证书条件时，标注 HTTPS 未验证，不能用 HTTP 结果代替。

## 5. 请求采集与独立网关

- 代理日志：分别验证受支持的本机/Docker Nginx 与 Docker Caddy JSON 日志。无日志、无权限和格式不匹配应有明确状态。
- 独立网关：按[网关说明](REQUEST_GATEWAY.zh-CN.md)选择测试网站、预览、确认接入；检查外层域名和 HTTPS 保持有效。
- 发送成功和失败请求，核对方法、域名、路径、状态、耗时及合并统计；缺失耗时不按零统计。
- 测试请求包含虚构的查询参数、Cookie 和请求体，确认它们没有进入采集记录。
- 验证 SSE/WebSocket 转发及结束后的记录；只采集实际经过所选入口的流量。
- 停止记录后验证原访问路径恢复，网关保留；代理被外部修改时应拒绝覆盖并给出诊断。
- 重新登录和重启 Agent 后，检查请求采集仍工作且没有旧部署任务执行。

## 6. 自动化与记录

在源代码目录的独立开发环境安装 `requirements-dev.txt` 后运行：

```bash
python3 -m pytest tests/test_docker_management.py tests/test_nginx_runtime.py tests/test_nginx_wizard.py tests/test_certificates.py tests/test_nginx_requests.py tests/test_caddy_requests.py tests/test_request_gateway.py tests/test_gateway_connections.py -q
python3 -m pytest tests/test_deployment_removal.py tests/test_installer_docker.py tests/test_monitoring_browser.py -q
node --test tests/ui_docker.test.cjs tests/ui_nginx.test.cjs tests/ui_requests.test.cjs
git diff --check
```

这些命令用于复验运维、部署停用、站点及 Docker 安装行为；浏览器用例需要 Playwright。真实网关联动测试命令见[网关开发验收](REQUEST_GATEWAY.zh-CN.md#开发验收)。

失败时保留脱敏日志：

```bash
journalctl -u mini-deploy-agent -n 100 --no-pager
bash /opt/mini_deploy/scripts/doctor.sh
```

验收记录逐项写明通过、失败或未测及原因，附目标提交、系统版本、Nginx 模式和脱敏证据。本文不提供业务数据清理命令；升级验证必须保留旧资源。
