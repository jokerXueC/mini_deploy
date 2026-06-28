# mini_deploy 快速上手

这是一套轻量部署面板。它负责接收 WebHook、执行你的部署脚本、展示日志和部署状态。

它不会自动理解所有业务代码。第一次接入项目时，你需要确认项目怎么构建、怎么重启、健康检查地址是什么。

## 你需要准备

- 一台 Linux 服务器，能用 root 或 sudo。
- 服务器已安装 `git`。这个需要你自己先装，因为第一步 clone 项目就要用。
- 服务器能访问你的代码仓库，通常需要配置 SSH Deploy Key。
- 一个业务项目仓库，例如 FastAPI、Go API、Spring Boot。

如果没有 `git`：

```bash
# Ubuntu / Debian
apt update
apt install -y git

# CentOS / Rocky
dnf install -y git
```

## 安装面板

```bash
git clone https://gitee.com/XC1960/mini_deploy.git /root/mini_deploy
cd /root/mini_deploy
bash install.sh
```

安装脚本会先问语言：

```text
请选择安装向导语言 / Select installer language (zh/en，默认 zh):
```

直接回车就是中文；输入 `en` 使用英文。

然后会问面板域名：

```text
请输入部署面板域名，例如 deploy.example.com；直接回车则跳过 Nginx 自动配置:
```

填了域名后，脚本会尝试自动配置 Nginx，并可选安装 certbot 申请 HTTPS。
如果你的项目使用 Docker Compose，且服务器未安装 Docker，安装脚本也会询问是否自动安装 Docker；默认不安装。

没有 HTTPS 也能访问：

```text
http://你的域名/deploy/ui
```

公网长期使用建议开启 HTTPS。

## 第一次打开

打开面板后，按页面提示设置管理员密码。

设置后建议重启一次：

```bash
systemctl restart mini-deploy-agent
```

检查状态：

```bash
systemctl status mini-deploy-agent
curl http://127.0.0.1:9010/health
```

## 添加第一个项目

在面板点击“添加仓库”，填写：

- 项目标识，例如 `fastapi-demo`
- 仓库地址，例如 `git@gitee.com:org/fastapi-demo.git`
- 部署分支，例如 `main`
- 服务器目录，例如 `/srv/fastapi-demo`
- 部署脚本，例如 `/srv/fastapi-demo/deploy/deploy.sh`
- systemd 服务名，例如 `fastapi-demo`
- 服务端口，例如 `8001`
- 启动命令，不懂可以先留空
- 业务域名，例如 `api.example.com`，没有可以先留空
- 健康检查，例如 `http://127.0.0.1:8001/health`

然后点击“自动初始化”。面板会直接在服务器上准备项目，不需要你复制大段命令。

自动初始化会：

- 检查服务器是否能访问仓库
- clone 或更新代码
- 生成一份初版 `deploy.sh`
- 对 Python / Go / Java 项目生成一份初版 systemd service
- 创建日志目录
- 设置脚本可执行权限

如果仓库权限不通，它会提示你配置 SSH Key。

“服务器执行指令”仍然保留，作为自动初始化失败时的备用方案。

如果你填写了业务域名，可以点击“配置业务域名”。它会自动生成该项目的 Nginx 反向代理配置：

```text
api.example.com -> http://127.0.0.1:8001
```

如果勾选“业务域名申请 HTTPS”，会在服务器已安装 certbot 时尝试自动申请证书。没有 HTTPS 时，HTTP 访问仍然可用。

## 重要边界

`deploy.sh` 是你的业务部署流程。面板可以生成初版，但你需要检查。

它通常做这些事：

```text
拉最新代码
安装依赖或构建
重启服务
健康检查
```

例如 FastAPI 如果用 systemd 管理，`deploy.sh` 里通常是：

```bash
git pull --ff-only origin main
pip install -r requirements.txt
systemctl restart fastapi-demo
curl -fsS http://127.0.0.1:8001/health
```

但是 `fastapi-demo.service` 这种服务文件仍然属于你的业务项目配置。mini_deploy 可以给模板和检查，但不能保证自动生成适合所有项目的 service。

## 配置 WebHook

项目保存后，面板会显示：

- WebHook URL
- Token

把它们填到 Gitee / GitHub / GitLab 的 WebHook 设置里，触发事件选择 Push。

之后你每次 push 到配置分支，mini_deploy 就会执行该项目的 `deploy.sh`。

## 常用排查

Agent 日志：

```bash
journalctl -u mini-deploy-agent -f
tail -n 100 /var/log/mini_deploy/mini-deploy-agent.log
```

项目部署日志在面板里可以直接看，也可以看你配置的日志文件。

如果部署失败，优先检查：

- 服务器能不能 `git pull`
- `deploy.sh` 是否可执行
- 服务重启命令是否正确
- 健康检查 URL 是否真实可访问

更完整的参考示例见：[部署流程参考](BEGINNER_DEPLOY_FLOW.zh-CN.md)。
