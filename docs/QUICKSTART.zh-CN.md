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

默认不需要填写域名，也不需要 Nginx 或证书。安装后直接访问 **`http://服务器公网IP:6868`**，端口固定为 `6868`。
脚本会尝试识别公网 IP；识别失败时，用云控制台显示的公网 IP 替换地址中的占位符。也可用 `DEPLOY_PUBLIC_IP` 指定安装完成时显示的 IP。

如需额外配置域名入口，可以执行 `DEPLOY_DOMAIN=deploy.example.com bash install.sh`，脚本会配置 Nginx，并可选安装 certbot 申请 HTTPS。
如果你的项目使用 Docker Compose，且服务器未安装 Docker，安装脚本也会询问是否自动安装 Docker；默认不安装。

安装过程中还会要求输入两次管理员密码。脚本先把密码哈希和独立的 Session Secret 写入 `/etc/mini-deploy-agent.env`，然后才启动 Agent 和配置公网入口。网页不提供管理员密码初始化，避免第一个公网访问者抢先注册。

没有 HTTPS 也能访问：

```text
http://服务器公网IP:6868
```

安装器会尝试在已启用的 UFW/firewalld 中放行 TCP 6868；**云服务器还需在安全组中放行入站 TCP 6868**。默认 HTTP 不加密，可以后续配置域名和 HTTPS。

升级也会切换到固定的 `0.0.0.0:6868`，旧 `DEPLOY_AGENT_HOST` / `DEPLOY_AGENT_PORT` 不再控制监听地址；安装器管理的旧 Nginx 配置会同步修改上游端口。若 6868 已被其他服务占用，安装器会停止并提示释放端口。

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
curl http://127.0.0.1:6868/health
```

## 添加第一个项目

服务器页默认显示每秒采样的实时趋势，正常启动后约 2～3 秒出现曲线，最多保留最近 60 个样本。实时样本仅保存在内存，面板重启后重新积累；24h / 7d 等历史视图仍使用低频归档。

新安装默认没有项目。`examples/` 目录里的 Node、Go、Java 配置只是参考，不会自动接入面板。

1. 点击“添加仓库”，填写项目标识、仓库地址和分支。
2. 点击“识别仓库”，选择检测到的部署方式并采用配置。支持根目录的 Compose、Python、Go、Java 和 Node 常见项目；特殊项目可直接选择自定义脚本。
3. 核对服务器目录、业务端口和启动命令。识别到唯一 FastAPI 入口时会推荐启动命令；其余情况需要按项目确认。域名和健康检查可先留空，不会填入虚构地址。
4. 点击“预览部署文件”，核对后勾选确认，再点击“自动初始化”。已有 `deploy.sh` 和服务文件会保留，不会覆盖。
5. 点击“检查项目”，补齐缺少的运行环境；确认后保存并启用项目，先手动部署一次，再配置 WebHook。

识别只在临时目录读取代码，不执行项目程序。初始化会拉取代码、补齐初版脚本和 Python / Go / Java 服务文件、创建日志目录；不会替你安装业务依赖或启动业务，实际部署才会执行脚本。仓库中新拉取的同名文件会保留，初始化后可再次预览核对。

失败时页面会给出可能原因和排查建议，例如仓库权限、网络、端口占用或依赖缺失。部署详情会保存本次失败诊断；无法识别时仍需查看日志。“手动接入指令”保留为可选入口。

没有项目时，也可在 **域名与证书** 点击“安装 Nginx”：

1. 选择“服务器本机”或“Docker 容器”。本机使用 80 端口；Docker 可填写其他 HTTP 端口，如 8080。
2. Docker 模式确认新容器名称，配置和空证书目录由面板自动挂载，不需要手写 Compose。服务器需已安装并启动 Docker；缺少时可运行安装向导选择安装 Docker。
3. 点击“检查安装计划”，确认后执行。无需项目、域名或证书，先启动 HTTP。Docker 默认不发布 443；勾选“预留 HTTPS 443 端口”只准备映射，不启用 TLS。
4. HTTP 检查通过后，打开页面提供的测试地址；外部访问需放行对应安全组端口。没有预留 443 的容器，以后启用 HTTPS 前需调整映射并重建，面板不会自动重建已有容器。

同名容器、被占用的端口及非托管目录会阻止创建；失败时仅清理确认属于本次创建的容器，镜像和托管配置目录保留供重试。新容器使用 `nginx:stable-alpine`，配置目录位于 `/srv/mini-deploy-nginx/<容器名>/conf.d`。已有 Nginx 接入实例会保留；新建 Docker Nginx 在尚未接入实例时尝试自动接入。

需要域名访问时，打开 **域名与证书 → 访问入口**，选择项目、填写域名和业务端口，点击“检查并预览”，核对后“确认并配置”。没有 Nginx 时，可自动通过 apt-get / dnf / yum 安装本机 Nginx；已有接入实例会继续使用。业务需先启动，域名解析和云安全组 80 端口仍需在云平台设置。生成的反向代理关系如下：

```text
api.example.com -> http://127.0.0.1:8001
```

默认先使用 HTTP，无需证书。上传和更换证书在 **HTTPS 证书** 标签中完成。已有 Docker Nginx 可展开“高级接入”选择；端口冲突会阻止安装，缺少容器挂载时不会自动重建容器。配置或重载失败会尝试恢复站点和项目配置，已安装的 Nginx 软件会保留。

如果勾选“业务域名申请 HTTPS”，本机模式会在服务器已安装 certbot 时尝试申请证书；Docker 模式请在证书页上传证书。没有 HTTPS 时，HTTP 访问仍然可用。

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

### 选择 Nginx 运行环境

面板 `公网IP:6868` 不依赖 Nginx。只有配置业务域名或证书时，才需要完成以下步骤：

1. 先保存业务项目；打开 **Nginx 证书**，点击 **检测运行环境**。
2. 选择“服务器本机”或“Docker 容器”。两者都存在时，请选择实际承接域名流量的实例；也可以“暂不配置”。
3. 点击 **检查并保存**。后续项目和证书操作都会复用此实例。
4. 选择业务项目，填写 **Nginx 访问的后端地址**，点击 **检查并保存后端**。再回项目配置业务域名。

本机或 Docker `host` 网络通常使用 `127.0.0.1`；Docker 桥接网络应填写同网络的业务服务名，例如 `api`，或容器可达的宿主机地址。业务端口来自项目设置。宿主机业务如果只监听 `127.0.0.1`，桥接容器通常无法连接它，需要调整业务监听或网络。

Docker 接入要求本机 Docker Unix Socket、运行中的 Nginx 容器，以及两个**目录 bind mount**：

- 独立的宿主机配置目录 → `/etc/nginx/conf.d`。
- Agent 数据目录下的 `certificates` → `/etc/mini-deploy/certificates`，可只读挂载。

网页会显示检测到的网络、端口和挂载，并给出所需挂载示例。修改 Compose 挂载后需要重建容器，再次检测。暂不支持远程 Docker、命名 Volume 或单文件配置挂载；不会自动重建已有容器。容器应发布实际使用的 80/443 端口；Nginx 主配置需包含 `include /etc/nginx/conf.d/*.conf;`。

没有 Nginx 时可以先选择“暂不配置”，或安装本机 Nginx；新建 Docker Nginx 可参考 [Compose 示例](../examples/nginx.compose.yml)。本机未安装并不代表 Docker 中没有 Nginx。

修改运行实例前，需要先停用 HTTPS、删除该实例的托管证书，并对已有项目点击 **移除域名入口**。移除入口不会删除业务代码或停止业务服务，但域名访问会暂时中断。

### 网页管理 HTTPS 证书

先完成上面的 Nginx 接入，Agent 所在服务器还需安装 OpenSSL。已有证书时，不用每次登录服务器替换文件：

1. 在项目设置里填写业务域名和服务端口。
2. 打开 **Nginx 证书**，选择项目，填写证书名称。
3. 选择或粘贴 PEM 完整证书链（`fullchain.pem`）和未加密私钥（`privkey.pem`），点击保存。
4. 点击 **启用 HTTPS**。面板校验证书和 Nginx 配置后重新加载服务，HTTP 会跳转至 HTTPS。

续期后上传新证书和私钥，点击 **替换并应用证书** 即可；也可以单独修改证书名称。删除前必须先停用 HTTPS，停用后站点仅保留 HTTP。443 端口需要在防火墙和安全组中放行。

此入口管理已选择的本机/Docker Nginx 上、面板已登记的**业务项目域名**，不覆盖面板自身域名、手工维护的站点或 Certbot 改写的配置。已有 Certbot 站点应继续使用 Certbot 续期；本入口不自动申请或续签证书。没有域名时无法启用此功能。

上传证书保存在数据目录的 `certificates/` 下，私钥不回显。Nginx 实例及项目后端设置保存于同目录的 `nginx.json`。替换时保留旧证书用于恢复，删除会一并删除保留版本；安装备份包含这两项数据，但不自动归档外部 Docker Compose 文件或业务 Nginx 挂载目录，后两项需另行备份。

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

### Docker 容器与镜像

在「服务器状态」中，停止后的容器可以确认删除。删除会丢失容器自身写入的文件，但保留数据卷和宿主机挂载目录；Compose 下次部署可能重建容器。

「Docker 镜像」支持查看、搜索、拉取和删除。拉取不会自动更新运行中的容器；删除不使用强制参数，有容器引用（包括已停止容器）或多个标签时会提示原因。私有仓库沿用服务器已有 Docker 登录凭据。

### 项目已经有 Nginx？

Nginx 不是接入项目的必装项。在「域名与证书 → 项目访问入口」选择本次配置方式：

- **保留项目自带入口**：继续使用业务 Compose，无需安装第二个 Nginx。
- **使用面板统一入口**：由面板 Nginx 转发到业务服务。
- **统一入口转发到项目 Nginx**：保留业务 Nginx，使用其他端口（如 8080）或内部网络接收转发。

两个服务不能同时绑定服务器同一地址的 80/443。识别仓库会提示 Compose 入口风险；拉取代码后点击「检查项目」可查看当前端口占用及服务名称。提示不会改 Compose、停止容器或接管已有 Nginx。变量、覆盖文件、profiles 和自定义启动脚本仍需核对，检查不是部署阻断器。

更完整的参考示例见：[部署流程参考](BEGINNER_DEPLOY_FLOW.zh-CN.md)。
