# Linux 实测清单

以下以 **Ubuntu 22.04 / 24.04、root 用户、专用测试服务器** 为例。项目仍在公开测试版准备阶段，先用没有生产业务的服务器。面板不需要域名；后面的 `smoke.example.test` 只是本地测试名称，不需要购买或解析。

## 1. 安装与登录

普通用户先执行 `sudo -i`，然后：

```bash
apt update
apt install -y git python3 python3-venv curl openssl rsync
python3 --version
git clone https://gitee.com/XC1960/mini_deploy.git /root/mini_deploy
cd /root/mini_deploy
bash install.sh
```

Python 必须为 3.10 或更高版本。Gitee 不通可换 GitHub：`https://github.com/jokerXueC/mini_deploy.git`，两条 clone 命令选一条即可。

已有代码目录时，用 `cd /root/mini_deploy && git pull --ff-only` 更新，再运行安装器；有本地改动应先核对，不能强制覆盖。旧安装如果提示托管标记或路径不符合要求，不要直接开启兼容开关，先备份并检查提示。

安装时选择中文、设置管理员密码。仅测试本机 Nginx 可不安装 Docker；要测试容器模式时，在 Docker 安装询问中选择“是”，并检查 `docker compose version` 是否正常。

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

Nginx/证书测试完成后，再接入你自己的测试仓库，确认构建命令、启动命令、端口和健康检查。自动初始化后检查生成的脚本，运行项目体检，手动部署成功后再配置 Git 平台 WebHook 并 push 一个测试提交。

依次验证手动部署、Webhook 部署、部署日志、取消，以及你的项目实际支持的回滚。临时 `smoke` 项目没有部署脚本，不能用它验收自动部署。

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
