# mini_deploy 快速上手

这是一套轻量部署面板。它负责接收 WebHook、执行你的部署脚本、展示日志和部署状态。

它不会自动理解所有业务代码。第一次接入项目时，你需要确认项目怎么构建、怎么重启、健康检查地址是什么。

> [!WARNING]
> 当前默认安装器以 `root` 运行 Agent，权限拆分、自动失败回滚和恢复命令尚未完成。现阶段只应部署到受控的专用测试服务器，不要把它当作已经达到生产安全基线的面板。

## 你需要准备

- 一台 Linux 服务器，能用 root 或 sudo。
- Agent 运行所需的 Python 3.10 或更高版本，当前 CI 覆盖 3.10-3.13。这个要求与业务项目是否使用 Python 无关；安装器会拒绝低于 3.10 的解释器，但不会代替你升级或选择解释器。
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

安装过程中还会要求输入两次管理员密码。脚本先把密码哈希和独立的 Session Secret 写入 `/etc/mini-deploy-agent.env`，然后才启动 Agent 和配置公网入口。网页不提供管理员密码初始化，避免第一个公网访问者抢先注册。

没有 HTTPS 也能访问：

```text
http://你的域名/deploy/ui
```

测试环境确需经过公网访问时必须开启 HTTPS，并限制来源；HTTPS 不能消除当前 root 权限边界。

## 第一次登录

打开面板后，使用安装时设置的管理员密码登录。

需要修改密码或让所有旧登录失效时，在服务器执行。以下命令假设使用默认 `APP_HOME`、`ENV_FILE` 和 `SERVICE_NAME`；自定义安装必须复制安装器结束时输出的参数化管理员命令，其中会显式传入实际环境文件和服务名，避免把凭据写进默认配置文件：

```bash
python3 /opt/mini_deploy/agent.py admin set-password
systemctl restart mini-deploy-agent

# 只让所有旧 Session 失效，不修改密码
python3 /opt/mini_deploy/agent.py admin reset-session
systemctl restart mini-deploy-agent
```

非交互安装必须通过 `DEPLOY_UI_PASSWORD_FILE` 提供权限为 `0600` 的密码文件；未提供凭据时安装器会安全终止，不会先暴露未初始化的面板。安装成功后应立即删除明文密码文件。非交互环境还应把 `SETUP_NGINX` 明确设为 `yes` 或 `false`；默认的 `auto` 在没有 TTY 且服务器缺少 Nginx 时会安全跳过安装。

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
- Token / Secret

URL 中只保留 `project` 参数，不要拼接 `token` 或 `secret`。把 Token 分别填入 Gitee 的密码/Token、GitHub 的 Secret 或 GitLab 的 Secret token 字段，触发事件选择 Push。GitHub 会使用 HMAC-SHA256 签名，Gitee/GitLab 会使用平台原生 Token 请求头。

之后你每次 push 到配置分支，mini_deploy 就会执行该项目的 `deploy.sh`。

## 常用排查

项目配置默认位于 `/var/lib/mini-deploy-agent/projects.json`，不再把活动配置写入程序目录。需要直接检查时使用：

```bash
nano /var/lib/mini-deploy-agent/projects.json
chown root:root /var/lib/mini-deploy-agent/projects.json
chmod 600 /var/lib/mini-deploy-agent/projects.json
DEPLOY_PROJECTS_FILE=/var/lib/mini-deploy-agent/projects.json \
  python3 /opt/mini_deploy/agent.py validate-config
systemctl restart mini-deploy-agent
```

优先在面板中保存项目。直接编辑时必须先校验再重启；Agent 不会自动监视外部文件变化，未重启前仍使用内存中的旧配置。

升级会在原始状态备份通过校验后，把旧默认路径 `/opt/mini_deploy/projects.json` 原子复制到数据目录。首次迁移时，若新旧两份内容不同，或环境文件使用了安装器无法纳入备份的外置自定义路径，安装器会停止并要求人工核对，不会猜测哪一份 Token 更新。环境文件已经明确指向数据目录后，该文件成为唯一权威配置；程序目录中保留的 root 私有旧副本可以过期，不再参与内容冲突判断。

Agent 日志：

```bash
journalctl -u mini-deploy-agent -f
tail -n 100 /var/log/mini_deploy/mini-deploy-agent.log
```

没有托管标记的旧版 `APP_HOME`、systemd unit 或 Nginx 配置默认会被拒绝。只有在人工核对路径并额外保留服务器快照后，才可为一次迁移临时设置 `DEPLOY_ALLOW_LEGACY_INSTALL_ADOPTION=true`；迁移成功后立即恢复为 `false`。

安装器识别到已有安装时，会在配置的备份目录（默认 `/var/backups/mini-deploy-agent`）原子发布一个 root 私有备份 bundle。每个 `mini-deploy-<时间>-<随机后缀>/` 目录包含权限受限的 `installation.tar.gz`、带 SHA-256 的 `format=2` 清单 `manifest` 和 `complete`；只有三个文件同时存在才算成功备份。安装器会在修改现有安装前自动用只读校验器检查新 bundle，校验失败就停止。下面是默认路径的手动备份命令；自定义安装应复制安装器结束时输出的完整参数化命令：

```bash
bash /opt/mini_deploy/scripts/backup-installation.sh
```

命令成功时会输出 `<bundle>/installation.tar.gz`；同目录的 `manifest` 和 `complete` 属于同一份备份，复制或保存时三者不能拆开。

也可以 root 身份把 bundle 目录或其中的 `installation.tar.gz` 传给校验器。例如把下面的示例路径替换为实际 bundle：

```bash
python3 /opt/mini_deploy/scripts/verify_backup.py \
  /var/backups/mini-deploy-agent/mini-deploy-YYYYmmddTHHMMSSZ-suffix
```

校验器会检查 root 私有控制文件、`format=2` 清单、归档摘要、受管归档根、硬链接解析以及 Agent 文件指纹，不会解压、恢复或修改 bundle。它会统计但不会跟随符号链接，并拒绝归档成员继续穿过链接写入；它不会认可符号链接将来的恢复目标。校验成功只说明当前 bundle 的结构和完整性符合要求，不等于已经验证恢复流程。

归档包含程序、项目配置、状态、环境文件，以及实际存在时的 systemd unit 和 Nginx 配置。安装器管理的独立日志目录（默认 `/var/log/mini_deploy`）会原地保留，不会作为单独目录加入归档；但脚本会整体归档 `APP_HOME` 和 `DATA_HOME`，旧布局或手工放在这两个目录内的日志仍可能进入归档，因此归档必须按敏感数据保存。

Agent、安装器和独立备份通过 `/run/mini-deploy-agent/maintenance.lock` 协调。部署或项目回滚任务从进入队列起到结束都持有共享锁；安装或备份持有独占维护锁时，新 WebHook、手动部署和项目回滚触发会返回 HTTP `503`，配置、状态和审计写入会等待维护结束。systemd 使用 `RuntimeDirectoryPreserve=yes` 在服务重启时保留这个 root 私有运行目录，并在服务器重启后重新创建。`/run/mini-deploy-agent` 仍是临时运行目录，不应存放持久数据。

当前版本尚未完成版本目录切换、失败自动回滚、恢复、版本回滚和卸载命令，生产升级前仍需保留可用的服务器快照并查看[开源开发路线图](ROADMAP.zh-CN.md)。

项目部署日志在面板里可以直接看，也可以看你配置的日志文件。

如果部署失败，优先检查：

- 服务器能不能 `git pull`
- `deploy.sh` 是否可执行
- 服务重启命令是否正确
- 健康检查 URL 是否真实可访问

更完整的参考示例见：[部署流程参考](BEGINNER_DEPLOY_FLOW.zh-CN.md)。
