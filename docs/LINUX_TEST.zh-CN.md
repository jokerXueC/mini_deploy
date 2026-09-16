# Linux 实测清单

以下适用于 **Debian 13 系列 / Ubuntu 22.04、24.04，root 用户，专用测试服务器**。这是一份待实机执行的验收流程，不代表这些发行版都已经完成实测。面板不需要域名；后面的 `smoke.example.test` 只是本地测试名称，不需要购买或解析。

按顺序执行：同步代码 → 检查上次安装状态 → 安装和登录 → 测试后端 → Nginx（本机或 Docker 选一种）→ 证书 → 真实部署 → 清理。第 2 节自动化测试可放到最后。任何命令报错，先停止本节，不要跳过错误继续执行后续命令。后续修复统一从仓库更新，不在服务器上手工修改脚本或服务文件。

## 1. 同步代码、重新安装与登录

### 1.1 确认系统并安装基础工具

下面的服务器命令都在 SSH 终端执行。普通用户先执行 `sudo -i`，已经是 root 则直接开始：

```bash
cat /etc/os-release
ps -p 1 -o comm=
apt update
apt install -y git python3 python3-venv curl openssl rsync
python3 --version
```

进程 1 应为 `systemd`，Python 必须为 3.10 或更高版本。

### 1.2 拉取最新代码

你已经有 `/root/mini_deploy`，执行这一组。stash 只保留此前可能手动修正的安装脚本，不恢复旧补丁：

```bash
cd /root/mini_deploy
git status --short
git stash push -m "before-installer-fix-update" -- install.sh
git pull --ff-only
git log -1 --oneline
grep -n '^WorkingDirectory=' install.sh
```

确认 pull 成功；最后一条应显示 `WorkingDirectory=$APP_HOME`，值外面没有双引号，说明已包含 systemd 格式修复。最新版同时包含 chown 修复。如果 pull 冲突或显示其他未处理的本地修改，先停止，不要 `reset --hard`，也不要再 `stash pop` 恢复旧安装脚本。

只有尚未克隆过仓库时，才改用：

```bash
git clone https://gitee.com/XC1960/mini_deploy.git /root/mini_deploy
cd /root/mini_deploy
```

Gitee 不通可把地址换成 `https://github.com/jokerXueC/mini_deploy.git`，不要对已有目录重复 clone。

### 1.3 保留上次首次安装中断的程序目录

**如果已经设置过管理员密码，并已生成 `mini-deploy-agent.service`，只是启动时遇到 `bad-setting`，直接跳到 1.4。** 此时不要移动程序目录或删除配置；新版安装器会备份已有安装、保留密码与项目配置，并重新生成和校验服务文件。

下面针对你遇到的 `chown: unrecognized option '--one-file-system'` 首次安装失败，且使用默认路径的情况。只有不存在安装标记、环境文件和服务文件时，才把半成品程序目录移动到备份位置。**不会删除 `/var/lib/mini-deploy-agent` 中已经创建的项目配置**。正常装好的实例会跳过这段。

```bash
if [ -d /opt/mini_deploy ] \
  && [ ! -e /opt/mini_deploy/.mini-deploy-install ] \
  && [ ! -e /etc/mini-deploy-agent.env ] \
  && [ ! -e /etc/systemd/system/mini-deploy-agent.service ]; then
  failed_backup=$(mktemp -d /opt/mini-deploy-failed.XXXXXX)
  mv -- /opt/mini_deploy "$failed_backup/"
  printf '已保留旧程序目录：%s\n' "$failed_backup"
fi
```

自定义过 `APP_HOME` 等路径的安装不要照搬这段移动命令。若安装器仍提示托管标记或路径异常，先检查提示，不要直接开启旧版接管开关。

### 1.4 运行修复后的安装器

```bash
cd /root/mini_deploy
bash install.sh
```

安装时按下面填写：

| 提示 | 本轮选择 |
| --- | --- |
| 安装语言 | 回车，默认中文 |
| 管理员密码 | 设置你自己的密码，重复输入确认；输入时不显示字符是正常现象 |
| 安装 Docker | 先测本机 Nginx 选“否”；要测第 5 节 Docker 模式则选“是” |
| 面板域名 | 默认不用填写，先使用 IP:6868 |

必须看到“安装完成”且健康状态正常，再继续。选择 Docker 模式还要确认 `docker compose version` 成功。

### 1.5 放行端口并登录

在云厂商安全组放行入站 **TCP 6868**，然后打开：

```text
http://服务器公网IP:6868
```

服务器上检查：

```bash
systemctl status mini-deploy-agent --no-pager
curl -fsS http://127.0.0.1:6868/health
ss -lntp | grep ':6868'
```

应看到服务 `active`、健康接口 `status: ok`、监听 `0.0.0.0:6868`。浏览器用安装时的密码登录，依次点击部署、服务器状态、告警事件、通知配置、Nginx 证书，确认没有卡死或加载失败。

本机 curl 成功但外网打不开时，检查云安全组和系统防火墙；`127.0.0.1` 不是你电脑上访问服务器的地址。服务器没有公网 IP 时，可以从同网络使用其内网 IP，单纯放行端口不会生成公网入口。

## 2. 可选：在 Linux 跑自动化测试

在源代码目录运行，不在 `/opt/mini_deploy` 中安装开发依赖：

```bash
cd /root/mini_deploy
python3 -m venv /root/mini-deploy-test-venv
/root/mini-deploy-test-venv/bin/pip install -r requirements-dev.txt
/root/mini-deploy-test-venv/bin/python -m pytest
/root/mini-deploy-test-venv/bin/python -m ruff check agent.py certificates.py nginx_runtime.py scripts/verify_backup.py tests
```

有 Node.js 18 或更新版本时再运行：

```bash
node --check ui/app.js
node --check ui/nginx.js
node --check ui/certificates.js
node --test tests/ui_navigation.test.cjs
```

这些测试不会代替下面的真实 Nginx 验证。

## 3. 准备一个临时业务服务

下面只用静态 HTTP 服务测试域名和证书，不拉取仓库或自动部署。

```bash
install -d -m 755 /srv/mini-deploy-smoke
printf 'mini_deploy smoke OK\n' > /srv/mini-deploy-smoke/index.html
systemd-run --unit=mini-deploy-smoke --collect \
  /usr/bin/python3 -m http.server 8001 --bind 127.0.0.1 --directory /srv/mini-deploy-smoke
curl -fsS http://127.0.0.1:8001/
```

应返回 `mini_deploy smoke OK`。若名称或端口已占用，先核对现有服务，不要覆盖业务进程。

在面板添加项目，填写下面的测试值：

| 字段 | 值 |
| --- | --- |
| 项目标识 | `smoke` |
| 项目类型 | 自定义 |
| 仓库地址 | `https://gitee.com/XC1960/mini_deploy.git`，仅作为测试字段，不执行克隆 |
| 分支 | `master` |
| 服务器目录 | `/srv/mini-deploy-smoke` |
| 部署脚本 | `/srv/mini-deploy-smoke/deploy.sh`，本项测试不会执行它 |
| 服务端口 | `8001` |
| 业务域名 | `smoke.example.test` |
| 健康检查 | `http://127.0.0.1:8001/` |

取消勾选“启用项目”“允许手动部署/回滚”“业务域名申请 HTTPS”，保存项目。**不要点自动初始化或部署**；这个临时项目只用于 Nginx/证书测试。

## 4. 本机 Nginx 测试

本机模式和下一节 Docker 模式任选一种先测，不能让两套 Nginx 同时占用宿主机 80/443。

```bash
apt install -y nginx
systemctl enable --now nginx
nginx -t
```

网页操作：

1. 打开 **Nginx 证书 → 检测运行环境**。
2. 选择 **服务器本机**，点击 **检查并保存**。
3. 业务项目选 `smoke`，后端地址填 `127.0.0.1`，点击 **检查并保存后端**。
4. 回到项目设置，点击 **配置业务域名**，暂不申请 HTTPS。

在服务器执行：

```bash
curl --noproxy '*' --resolve smoke.example.test:80:127.0.0.1 http://smoke.example.test/
```

应返回 `mini_deploy smoke OK`，且 `/etc/nginx/conf.d/mini-deploy-smoke.conf` 中的后端为 `127.0.0.1:8001`。

## 5. Docker Nginx 测试

如果前面测过本机模式，先在 UI 停用 HTTPS、删除测试证书、移除 `smoke` 的域名入口，再停止测试用的本机 Nginx：`systemctl stop nginx`。已有真实站点的机器不要直接停服务。

在独立目录启动示例，避免与既有 Compose 项目混用：

```bash
install -d -m 755 /srv/mini-deploy-nginx-test/nginx-conf
install -d -m 700 /var/lib/mini-deploy-agent/certificates
cp /root/mini_deploy/examples/nginx.compose.yml /srv/mini-deploy-nginx-test/compose.yml
cd /srv/mini-deploy-nginx-test
docker compose -p mini-deploy-nginx-test up -d
docker exec mini-deploy-nginx nginx -t
```

默认安装目录以外的实例，需要在 Compose 中把证书宿主机目录改成实际数据目录下的 `certificates`。

创建同网络的临时业务容器，容器内部监听 8001，不映射额外宿主机端口：

```bash
docker run -d --name mini-deploy-smoke-backend \
  --network mini-deploy-nginx-test_default \
  --mount type=bind,src=/srv/mini-deploy-smoke,dst=/site,readonly \
  python:3.12-alpine python -m http.server 8001 --bind 0.0.0.0 --directory /site
```

网页操作：

1. 检测运行环境，选择 **Docker 容器**，选择或输入 `mini-deploy-nginx`。
2. 查看配置和证书挂载，点击 **检查并保存**。
3. 选择 `smoke`，后端填 `mini-deploy-smoke-backend`，点击 **检查并保存后端**。
4. 回到项目设置，点击 **配置业务域名**。
5. 再运行上一节的 `curl --resolve`，应看到相同的成功内容。

额外验证：把检查框中的后端临时改成 `127.0.0.1`，点击“检查连通”，应明确提示桥接网络不能这样连接；改回容器名。不要保存错误值。

## 6. 证书新增、替换和删除

不需要真实域名，使用仅供测试的自签证书。在服务器生成：

```bash
install -d -m 700 /root/mini-deploy-test-certs
umask 077
openssl req -x509 -newkey rsa:2048 -nodes -days 3 \
  -keyout /root/mini-deploy-test-certs/privkey.pem \
  -out /root/mini-deploy-test-certs/fullchain.pem \
  -subj '/CN=smoke.example.test' \
  -addext 'subjectAltName=DNS:smoke.example.test'
```

通过自己的 SFTP 工具下载这两个测试文件，在网页 **Nginx 证书** 中选择 `smoke`，填写名称，上传证书和私钥，保存后点击 **启用 HTTPS**。

服务器上验证：

```bash
curl --noproxy '*' --resolve smoke.example.test:443:127.0.0.1 \
  --cacert /root/mini-deploy-test-certs/fullchain.pem https://smoke.example.test/
curl --noproxy '*' -I --resolve smoke.example.test:80:127.0.0.1 http://smoke.example.test/
```

第一条应返回测试内容，第二条应返回跳转到 HTTPS 的 `301`。自签证书仅用于验证功能，普通浏览器不会自动信任它。

依次检查：

- **修改名称**：刷新网页后新名称保留。
- **替换**：另行生成新证书和私钥，上传并应用，网页指纹应变化。
- **错误私钥/错误域名**：使用不匹配的测试文件应被拒绝，原 HTTPS 应仍可访问。
- **删除保护**：正在使用的证书不能直接删除。
- **停用和删除**：停用 HTTPS 后通过 HTTP 仍能访问，再删除证书。
- **服务重启**：`systemctl restart mini-deploy-agent` 后，Nginx 选择和后端地址仍保留。

公网验证业务域名还需真实 DNS，以及安全组放行 80/443；上面的服务器本机测试不需要。

## 7. 真正的部署链路

Nginx/证书测试完成后，再接入你自己的测试仓库。临时 `smoke` 项目没有部署脚本，不能用它验收自动部署。

1. 在网页新建业务项目，填自己的仓库、分支、独立的服务器目录、端口、启动命令和健康检查地址。先不要启用自动部署。
2. 点击“自动初始化”，确认仓库能拉取；私有仓库按错误提示配置 Deploy Key。检查生成的部署脚本和服务文件是否符合你的业务，不要直接假定模板能运行。
3. 运行项目体检，解决阻塞项；启用项目和手动部署，保存。
4. 点击“重新部署”。验收：出现部署记录、日志更新、最终成功，健康检查返回预期结果。失败时先查看该次部署日志，不急着配置 WebHook。
5. 从网页复制此项目 WebHook URL。在 GitHub/Gitee/GitLab 仓库设置中选择 Push 事件；Token 填平台对应的 Secret/密码字段，不拼到 URL 上。
6. 在你的业务仓库修改一个可观察的小内容，提交并 push 到配置分支。验收：代码平台投递成功、面板出现该提交的部署记录、业务内容更新。仅“投递成功”不代表部署成功，还要看面板最终状态。
7. 在测试项目上验证失败和取消：使用可控的失败步骤或耗时步骤，确认失败不会显示成功、取消后进程停止，下一次部署仍可正常执行。不要用数据库操作测试失败。
8. 只有已提供并验证回滚脚本的业务项目才测试回滚；确认回滚到预期提交并通过健康检查，不支持回滚的项目跳过。
9. 重启 `mini-deploy-agent`，重新登录，确认项目、Nginx 实例、后端地址和证书记录仍存在。当前队列重启恢复尚未实现，不要在部署进行中用重启模拟无损恢复。

验收记录至少保留：服务器系统版本、代码提交号、Nginx 模式、手动部署结果、Webhook 投递和部署结果、证书增删改结果、失败时的脱敏日志。

## 8. 排查和清理

失败时先保留报错和日志：

```bash
journalctl -u mini-deploy-agent -n 100 --no-pager
bash /opt/mini_deploy/scripts/doctor.sh
```

本机模式再看 `nginx -t`；Docker 模式看 `docker exec mini-deploy-nginx nginx -t` 和 `docker logs --tail 100 mini-deploy-nginx`。对外分享前隐藏 Token、Cookie 和私钥。

测试结束，在 UI 中依次停用 HTTPS、删除测试证书、移除测试域名入口，然后停用测试服务：

```bash
systemctl stop mini-deploy-smoke
```

仅当使用了本文创建的 Docker 测试实例时，再执行：

```bash
docker stop mini-deploy-smoke-backend
docker rm mini-deploy-smoke-backend
cd /srv/mini-deploy-nginx-test
docker compose -p mini-deploy-nginx-test down
```

这些清理命令只针对本文创建的服务名称；不要替换成生产容器。测试文件目录保留，确认不再需要后自行清理。
