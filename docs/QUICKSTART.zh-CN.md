# VibePilot Deploy 小白快速上手

这份文档按“复制、粘贴、检查、启用”的顺序写。你不需要写 CI/CD 配置文件，只要准备一台 Linux 服务器和一个代码仓库。

## 你需要提前准备

- 一台 Linux 服务器，建议至少 1 核 1G。
- 服务器 root 权限，或者能执行 `sudo`。
- 服务器已经能访问你的代码仓库。
- 仓库的 SSH Deploy Key 或访问令牌已经配置好。
- 如果项目用 Docker，需要服务器已安装 Docker。
- 如果项目用 Node、Java、Go，需要服务器已安装对应运行环境。

## 1. 安装部署面板

在服务器执行：

```bash
git clone https://github.com/your-org/vibepilot-deploy.git /root/vibepilot-deploy
cd /root/vibepilot-deploy
bash install.sh
systemctl restart vibepilot-deploy-agent
systemctl status vibepilot-deploy-agent
curl http://127.0.0.1:9010/health
```

看到 `{"status":"ok"}` 就说明 Agent 已经启动。

## 2. 配置 Nginx 反向代理

如果你希望用 `https://你的域名/deploy/ui` 访问，把 `docs/nginx.example.conf` 里的配置合并到你的站点 Nginx 配置中，然后执行：

```bash
nginx -t
systemctl reload nginx
```

没有域名时，也可以临时在服务器本机访问：

```text
http://127.0.0.1:9010/ui
```

如果要从外网直接访问，请优先配置 HTTPS 反向代理，不建议直接暴露 `9010` 端口。

## 3. 第一次打开面板

打开：

```text
https://你的域名/deploy/ui
```

第一次会要求设置管理密码。密码会加密写入：

```text
/etc/vibepilot-deploy-agent.env
```

设置完成后可以直接登录。你也可以执行一次重启确认环境文件生效：

```bash
systemctl restart vibepilot-deploy-agent
```

## 4. 添加第一个项目

进入面板后点击“添加仓库”。

建议按这个顺序填写：

1. 项目标识：例如 `my-api`，只能用英文、数字、横线或下划线。
2. 显示名称：例如 `我的 API`。
3. 项目类型：选择 Docker Compose、Node / PM2、Java / systemd、Go / systemd、静态网站或自定义脚本。
4. 仓库地址：例如 `git@gitee.com:org/my-api.git`。
5. 部署分支：例如 `master` 或 `main`。
6. 服务器目录：默认会生成 `/srv/项目标识`。
7. 部署脚本：默认会生成 `/srv/项目标识/deploy/deploy.sh`。
8. 健康检查：例如 `https://api.example.com/health`，没有也可以先留空。
9. 日志文件：默认会生成 `/var/log/vibepilot/项目标识-deploy.log`。

新项目默认不会启用，这是为了避免脚本还没准备好就被 WebHook 触发。

## 5. 复制服务器执行指令

项目表单下方会自动生成“服务器执行指令”。

把整段命令复制到服务器执行。它会做这些事：

- 创建项目目录。
- clone 或更新代码仓库。
- 创建部署脚本。
- 创建日志目录和日志文件。
- 按你选择的项目类型写入一份基础部署流程。

执行完成后，回到面板点击“保存项目”。

## 6. 体检项目

保存项目后点击“检查项目”。

体检会检查：

- 项目目录是否存在。
- `.git` 仓库是否存在。
- 部署脚本是否存在。
- 部署脚本是否可执行。
- Docker、Node、npm、Java、Go 等运行环境是否存在。
- 健康检查地址是否可访问。

如果有红色错误，先按提示修复。黄色警告通常表示可以稍后补充，比如健康检查暂时没启动。

## 7. 配置 WebHook

项目表单会显示：

- WebHook POST 地址。
- WebHook Token。
- Gitee / GitHub / GitLab 填写说明。

在代码平台添加 WebHook：

- URL 填面板里的 POST 地址。
- 请求方法选择 POST。
- 触发事件选择 Push。
- Token 或密码填面板里的 Token。
- 分支选择你配置的部署分支。

## 8. 启用项目

确认体检基本通过后：

1. 勾选“启用项目”。
2. 勾选“允许手动部署/回滚”，如果你需要在网页点按钮部署。
3. 点击“保存项目”。

之后你每次 push 代码，面板就会自动收到 WebHook 并执行部署脚本。

## 常用排查命令

查看 Agent 状态：

```bash
systemctl status vibepilot-deploy-agent
journalctl -u vibepilot-deploy-agent -f
```

查看 Agent 日志：

```bash
tail -f /var/log/vibepilot/vibepilot-deploy-agent.log
```

查看部署日志：

```bash
tail -f /var/log/vibepilot/vibepilot-deploy.log
```

查看项目配置：

```bash
nano /opt/vibepilot-deploy/projects.json
```

修改环境变量：

```bash
nano /etc/vibepilot-deploy-agent.env
systemctl restart vibepilot-deploy-agent
```

## 安全建议

- 不要把 `9010` 端口直接暴露到公网。
- 面板必须放在 HTTPS 后面。
- 管理密码要足够强。
- 每个项目使用不同 WebHook Token。
- 部署脚本有服务器权限，启用前一定要检查内容。
