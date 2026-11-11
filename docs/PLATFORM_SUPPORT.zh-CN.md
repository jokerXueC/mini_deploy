# Linux 系统兼容与安装准备

标准安装需要 **Linux 主机、root、运行中的 systemd、Python 3.10+（含 SQLite/SSL）**。能运行 Python 不代表全部运维功能已经兼容。

## 支持范围

| 环境 | 当前定位 |
| --- | --- |
| Debian 12/13、Ubuntu 22.04/24.04 | 推荐目标，仍需逐项验收可选功能 |
| Rocky/AlmaLinux/RHEL 9/10、Fedora 等 systemd 发行版 | 条件兼容，整机待验证；需确认 Python、仓库、Docker 和 SELinux |
| 旧 Debian/Ubuntu/CentOS | 不推荐新安装，优先升级系统 |
| Alpine/OpenRC、OpenWrt、无 systemd 的精简系统 | 当前标准安装不支持 |
| 普通 Docker/Podman 容器 | 不支持作为面板主机安装，无法可靠管理宿主机 |
| Windows/macOS | 本地开发与 UI 预览，不支持标准主机安装 |

推荐不等于已完成所有版本和架构的虚拟机验收。单元测试、容器烟雾测试不能替代带 systemd 的整机验收。x86_64 和 ARM64 均须使用对应架构的软件包及容器镜像。

## 安装前只读检查

下载仓库后，在仓库根目录执行：

```bash
bash install.sh --check
```

不取得维护锁、不安装软件、不改配置、不启动或停止服务。通过只代表基本条件满足，不能证明云安全组、域名、业务接口或可选组件可用。

```bash
cat /etc/os-release
ps -p 1 -o comm=
python3 --version
df -h /opt /var
```

PID 1 应为 systemd。安装器检查默认 `python3` 及常见版本命令，不改系统 Python。也可显式选择已有解释器：

```bash
PYTHON_BIN=/usr/bin/python3.11 bash install.sh --check
PYTHON_BIN=/usr/bin/python3.11 SETUP_NGINX=no SETUP_HTTPS=no bash install.sh
```

实际解释器记录在 `/etc/mini-deploy-agent.env` 的 `DEPLOY_AGENT_PYTHON_BIN`，标准升级沿用；更换时显式指定 `PYTHON_BIN`。不要修改 `/usr/bin/python3` 的软链接。

## Debian / Ubuntu

先安装基础工具，再下载和安装面板：

```bash
apt-get update && apt-get install -y bash git curl ca-certificates python3 util-linux coreutils tar
```

推荐版本默认 Python 满足要求。SSH 安装器尝试 `python3-paramiko` 并用实际解释器验证导入。Docker、Nginx 按需安装；已有 Caddy 或业务代理不用替换。

```bash
apt-get update && apt-get install -y python3-paramiko
```

## Rocky / AlmaLinux / RHEL / Fedora

这些环境显示“待验证”。先准备工具：

```bash
dnf install -y bash git curl ca-certificates util-linux coreutils tar
```

RHEL 9 系常见默认 Python 是 3.9。启用的发行版仓库提供对应包时，可以并行安装：

```bash
dnf install -y python3.11 python3.11-pip
/usr/bin/python3.11 -c 'import sqlite3, ssl; print("Python prerequisites OK")'
```

找不到包时核对系统版本、订阅及启用的仓库，不混装其他发行版 RPM。Fedora/RHEL 10 可能已有合适的默认 Python，不需要再次安装 3.11。`python3-paramiko` 可能仅供系统 Python 使用；并行 Python 无法导入时使用下方隔离环境流程。

### Docker Engine

APT 使用 `docker.io`。DNF/YUM 仅从**已经配置的仓库**选择 `docker-ce` 或 `moby-engine`，不再盲目安装 `docker`，不自动添加第三方仓库，也不把 `podman-docker` 当成 Docker Engine。

```bash
docker --version
systemctl status docker --no-pager -l
docker --host unix:///var/run/docker.sock info
```

缺少引擎包时，基础面板继续安装，结果显示 Docker 未就绪。按官方文档选择匹配当前系统的仓库：

- [Debian](https://docs.docker.com/engine/install/debian/)
- [Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
- [Fedora](https://docs.docker.com/engine/install/fedora/)
- [RHEL](https://docs.docker.com/engine/install/rhel/)
- 衍生发行版先查其 Docker Engine 安装说明，不直接套用其他系统的仓库。

已有 Docker CLI 时保留安装，不自动重装或重启引擎，在「功能检查」核对本机引擎是否可用。

## 非默认 Python 的 SSH 依赖

发行版 Paramiko 不能供所选 Python 导入时，使用专用虚拟环境，不给系统 Python 强制执行 pip。以下示例要求已有 `/usr/bin/python3.11`。专用目录 `/opt/mini-deploy-runtime` 应尚不存在，或是你明确管理的该面板运行环境，不能覆盖其他项目。

Debian/Ubuntu 需要对应版本的 venv 包（推荐版本使用 `python3-venv`）；RPM 系使用发行版对应的 Python/pip 包。以下在下载的仓库目录执行：

```bash
test ! -e /opt/mini-deploy-runtime && /usr/bin/python3.11 -m venv --copies /opt/mini-deploy-runtime
/opt/mini-deploy-runtime/bin/python -m pip install -r requirements-ssh.txt
/opt/mini-deploy-runtime/bin/python -c 'import sqlite3, ssl, paramiko; print(paramiko.__version__)'
PYTHON_BIN=/opt/mini-deploy-runtime/bin/python SETUP_NGINX=no SETUP_HTTPS=no bash install.sh
```

每步成功后再执行下一步。目录已存在时先确认归属，不覆盖其他项目。`--copies` 保留专用解释器路径，避免解析软链接后脱离虚拟环境。下载失败先检查网络和依赖源，不使用 `--break-system-packages`；此处不自动配置加速源。

独立 Python 环境和系统软件包不在面板的程序/数据备份内。迁移到新主机时先安装匹配的 Python，并按上述步骤重建同路径运行环境和依赖，再恢复并验证服务。

## SELinux、防火墙和 Nginx

预检显示 SELinux enforcing 状态，不会执行 `setenforce 0` 或关闭策略。Nginx 502、Permission denied 或自定义路径无法读取时，先核对 AVC 拒绝：

```bash
getenforce
ausearch -m AVC,USER_AVC -ts recent
systemctl status nginx --no-pager -l
journalctl -u nginx -n 80 --no-pager -l
```

缺少 `ausearch` 时安装本发行版 audit 工具，不直接应用自动生成的宽泛允许规则。仅确认 Nginx 因 SELinux 拒绝联网，且接受布尔值影响所有 httpd 域服务时，由管理员选择：

```bash
setsebool -P httpd_can_network_connect on
```

这不是默认安装动作，自定义目录/证书文件上下文仍需核对。安装器尝试在已启用的 UFW/firewalld 中放行面板 TCP 6868，不关闭防火墙；云安全组仍由用户在云厂商处设置。

SSH 自动封禁还需要 nftables 和可读认证日志。Debian/Ubuntu 使用 `apt-get install -y nftables`，RPM 系使用 `dnf install -y nftables`；不主动替换现有防火墙服务。日志兼容 systemd journal、`/var/log/auth.log` 与 `/var/log/secure`，以安全中心实际采集状态为准。

## 验收

按 [Linux 验收清单](LINUX_TEST.zh-CN.md)记录发行版、版本、架构、Python、Docker、SELinux。每个目标至少验证首次安装、保留配置升级、重启、SSH、历史写入、网站接入、封禁/解封及备份校验。失败、跳过和未具备条件的项目必须保留记录，不能标为全部支持。
