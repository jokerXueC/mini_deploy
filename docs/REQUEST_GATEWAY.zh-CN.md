# 统一请求网关

让请求经过 mini_deploy 网关，即可查看请求时间、路径、状态码和耗时。业务代码不用改，现有 Caddy/Nginx 继续管理域名和 HTTPS。

```mermaid
flowchart LR
    A[浏览器] --> B[现有网站入口]
    B --> C[mini_deploy 网关]
    C --> D[业务服务或原静态站点]
    C -. 请求完成后 .-> E[面板请求记录]
```

## 推荐：在网页启用

需要 Linux、本机 Docker，以及以 root 运行的 Agent。

1. 打开「请求记录 → 添加网站」。发现唯一服务时，自动读取网站；多个服务时先选择网站所在服务。
2. 选择网站，核对显示的监控范围，点击「下一步」。
3. 阅读配置改动和重启提示，点击「开启记录」。完成后自动返回该网站的请求列表。

访问网站后即可看到经过网关的请求。首页只保留网站、状态和路径筛选。停止时点击「停止记录」，系统恢复原网站访问路径。

容器、配置路径、网络、镜像等参数默认收起。只有检测失败或网络无法唯一确定时，才展开需要处理的选项。已有代理日志和网关维护位于「高级设置」，无需同时配置。

系统会启动网关、备份入口配置、校验并应用，失败时尝试恢复。Caddy 使用重启，已有连接可能短暂中断；Nginx 使用重载。无需再复制一份接入脚本到服务器执行。

**页面会显示当前网站与采集范围。只有实际经过网关的请求才有记录。** 检测请求本身也会产生记录；请再用真实网址验证。只接入某个转发规则时，其余路径不采集。旧版手动创建的入口显示为“自定义入口”，不假定它已接入任何网站。

### 你的 Caddy 示例

系统会检测 Docker 容器 `aimore-caddy-1` 并尝试读取 `/etc/caddy/Caddyfile`。检测失败时，在「手动配置服务」中核对这两个值；配置路径填写容器内路径。

- API：选择 `aimore.meetpeak.tech` 的 `/cloud/*` 规则，网络选择与业务共有的 `aimore_default`，检测路径填 `/cloud/health`。已有 `aimore-api` 网关配置一致时可以复用。
- 主站：另选 `meetpeak.tech` 静态站点，新建不同标识和监听端口的网关。向导保留原静态文件规则，增加内部 HTTP 入口，再接入网关。无需新增 Docker 宿主机端口映射。

只接入 API，不会记录主站访问。网络名以检测结果为准；Docker 服务名不能在“服务器本机”网络中解析。

## 不知道怎么填

网页有「不知道怎么填？查看查询指令」，可直接复制。先在服务器终端运行，再按提示填写：

| 查询内容 | 指令 | 填写方式 |
| --- | --- | --- |
| 代理容器 | `docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'` | 将代理对应的 NAMES 填入容器名称 |
| 配置挂载 | `docker inspect 容器名称 --format '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}'` | 配置路径填写箭头右侧；挂载目录需补文件名 |
| Caddy 启动参数 | `docker inspect 容器名称 --format '{{json .Config.Cmd}}'` | 填写 `--config` 后面的路径 |
| Docker 网络 | `docker inspect 容器名称 --format '{{json .NetworkSettings.Networks}}'` | 代理和业务各查一次，选择共同网络 |
| 本机代理 | `systemctl is-active caddy nginx` | 对应项为 active 才表示本机服务正在运行 |
| 本机启动参数 | `systemctl show caddy nginx --property=ExecStart --value` | Caddy 的 `--config` 后面是配置路径 |
| 应用错误 | `docker logs --tail 80 容器名称` | 查看原始错误，不用填进表单 |

Nginx 不知道选哪个文件时，网页另有查询指令，按域名列出它所在的生效配置文件。查询失败会保留已填内容；对外提供日志前先遮盖密码和令牌。

## 自动接入的范围

- 本机或 Docker Caddy：常见单个 HTTP 后端，以及简单静态网站。
- 本机或 Docker Nginx：单个 HTTP 后端，规则需明确设置 Host、X-Forwarded-Proto 和 X-Forwarded-For。
- Docker 配置必须以 bind 方式挂载到宿主机；使用 host 或自定义 bridge 网络。
- 复杂配置会给出原因并拒绝自动改写，例如 Caddy import、多上游、带高级选项的转发、依赖客户端 IP 的静态规则、Nginx 静态站点。
- 本机 Caddy 的服务环境变量配置、Caddy --resume，以及 Nginx 自定义主配置/前缀暂不自动接入。

其他代理可在「高级设置 → 网关维护 → 手动新建」创建网关，由维护者把对应转发目标改为页面给出的网关地址：本机代理用 `127.0.0.1:监听端口`，同网络容器用 `mini-gateway-标识:10000`。采集仍统一在网关完成，不依赖代理日志格式。

## 撤销与维护

点击「停止记录」恢复原网站规则，网关仍保留运行。需要释放容器时，再到「高级设置 → 网关维护」中停止或删除。多个站点可以独立撤销，不覆盖其他站点的后续修改。

配置备份在数据目录的 `gateway-connections/入口标识/`：`before.conf` 是接入前完整配置，`disconnect-before.conf` 是最近一次撤销前配置。备份不包含 Docker 容器或日志。

如规则被外部修改、Compose 重建了代理容器，或自动恢复失败，系统会停止自动覆盖并保留网关。先由维护者核对当前配置、容器挂载和备份再恢复，不能直接用旧文件覆盖整个站点配置。Agent 文件备份不会替你恢复外部代理的配置。

## 记录范围

- 页面每 10 秒刷新，最多展示最近 300 条；采集不依赖网页保持打开。
- 请求按域名、方法和路径合并，显示次数、平均耗时、平均上游耗时及成功率（2xx/3xx）；统计仅覆盖本次读取的样本，缺失耗时不计为 0。状态格展示该地址最近最多 30 次请求，点击可展开单次记录；“含失败请求”筛选保留该地址全部样本的统计。
- 记录网关观察到的时间、方法、域名、路径、状态、总耗时和上游耗时，不代表浏览器完整加载时间。SSE/WebSocket 在连接结束后形成完整记录。
- 支持 HTTP 后端、SSE/WebSocket；不支持 HTTPS 上游、gRPC、TCP/UDP。外层 HTTPS 保留。
- 上传上限 64 MiB，上游读写空闲超时 1 小时。外层代理和业务仍可能有更小限制。
- 不记录查询参数、认证头、Cookie 和请求体。不要把密码或令牌放进 URL 路径。
- 日志由 Docker 轮转，每个容器约 3 份、每份 10 MiB；页面读取最近 1000 行中的有效记录，不做永久归档。删除容器会删除日志。
- 向导默认只映射本机端口，并信任前置代理的转发头。只应允许可信代理和同网络容器连接，不要把网关端口直接暴露到公网。

## 开发验收

原生 Caddy/Nginx 联动测试：

```bash
MINI_DEPLOY_TEST_CADDY=/usr/bin/caddy \
MINI_DEPLOY_TEST_NGINX=/usr/sbin/nginx \
python3 -m pytest tests/test_gateway_connections_integration.py -q
```

专用 Linux Docker 测试机以 root 运行（会创建并清理临时容器和网络）：

```bash
MINI_DEPLOY_GATEWAY_DOCKER_TESTS=1 \
python3 -m pytest tests/test_request_gateway_integration.py tests/test_gateway_connections_integration.py -q
```

CI 已配置 Docker 验收。本地 Windows 原生代理通过不代表 Linux Docker 验收已通过。
