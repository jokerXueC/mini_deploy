# 贡献指南

感谢你参与 mini_deploy。项目面向个人开发者、小团队和低配置 Linux 服务器，目标是保持部署流程可理解、可审计，并尽量维持 Python 标准库运行时和无数据库架构。

## 开始之前

- Bug 和功能建议请先搜索现有 Issue，避免重复讨论。
- 较大的功能、配置格式变更或架构调整，建议先创建 Issue 说明用户问题、范围和兼容性影响。
- 安全漏洞不要提交公开 Issue 或 Pull Request。当前仓库尚未配置并验证私密报告入口；请查看 [SECURITY.md](SECURITY.md) 的渠道状态，必要时只创建不含漏洞细节和敏感信息的联系请求。
- 参与讨论和代码评审时，请遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。

## 支持目标

mini_deploy 的部署目标是 Linux 服务器，主要运行环境为：

- Python 3.10-3.13。
- systemd。
- Bash、Git。
- 可选的 Nginx、Certbot 和 Docker Compose。

Windows 兼容性不是功能验收条件。无论使用什么设备编辑代码，涉及安装、服务管理、权限、路径、Shell 或容器的行为都必须在一次性的 Linux 测试环境中验证。

## 准备开发环境

运行时目前不需要安装 Python 第三方包。用于完整检查的开发工具包括 Python 3.10-3.13、Bash、Node.js 和 ShellCheck。

```bash
git clone <repository-url> mini_deploy
cd mini_deploy
python3 -m pip install -r requirements-dev.txt
```

不要直接在工作站或生产服务器上试运行 `install.sh`。安装脚本会操作 `/opt`、`/etc`、systemd，并可能配置 Nginx 或 Docker；请使用可丢弃的 Linux 虚拟机或测试服务器。

## 修改原则

- 保持改动集中，一个 Pull Request 解决一个明确问题。
- 优先沿用现有标准库、原生 JavaScript/CSS 和本地辅助函数，不为小功能引入重型框架或常驻服务。
- 所有来自 HTTP、WebHook、项目配置和环境变量的输入都应视为不可信数据。
- 不通过字符串拼接接受任意 Shell、文件路径、systemd 单元名、容器名或 Nginx 配置片段。
- 配置和状态写入应保持原子性；包含 Secret 的文件应使用最小权限。
- 安装、升级、回滚和卸载默认不得删除业务代码、数据库、Docker Volume、用户配置或备份。
- 不要提交本地 `projects.json`、旧版 `.backups/` 或 `projects.json.*.bak`；这些文件可能包含 WebHook 和通知 Secret。
- 面板不得提供任意 Shell 终端，也不得自动执行无法安全回滚的数据库迁移。
- 新增配置项需要安全默认值、环境变量示例、兼容说明和文档。
- 用户可见的变化应添加到 `CHANGELOG.md` 的 `Unreleased` 部分。

## 本地检查

提交前至少运行与改动相关的检查：

```bash
python3 -m py_compile agent.py certificates.py nginx_runtime.py scripts/verify_backup.py
python3 -m pytest
python3 -m ruff check agent.py certificates.py nginx_runtime.py scripts/verify_backup.py tests
node --check ui/app.js
node --check ui/certificates.js
node --check ui/nginx.js
node --test tests/ui_navigation.test.cjs
bash -n install.sh
for file in scripts/*.sh; do bash -n "$file"; done
git diff --check
```

如果安装了 ShellCheck，再运行：

```bash
shellcheck install.sh scripts/*.sh
```

修改认证、WebHook、配置写入、部署队列、取消、回滚、备份校验、维护锁、路径校验或安装升级逻辑时，应增加能够覆盖成功、失败和恶意输入的自动化测试。备份测试只能使用临时 fixture；`scripts/verify_backup.py` 是只读校验器，不应在测试说明中把它表述为恢复工具。GitHub Actions 会在 Python 3.10-3.13 上运行测试和 Ruff，并单独执行 JavaScript 语法检查与 ShellCheck。请在 Pull Request 中清楚记录无法自动验证的部分和手动验证步骤。

手动验证只能使用测试凭据、测试域名和非生产仓库。测试完成后应撤销临时 Token，并删除日志或截图中的敏感信息。

## 提交 Issue

Bug 报告应包含：

- mini_deploy 版本或提交号。
- Linux 发行版和版本、Python 版本。
- 安装方式，以及是否使用 systemd、Nginx 和 Docker Compose。
- 最小复现步骤、预期结果和实际结果。
- 已脱敏的配置片段、状态和相关日志。

请勿粘贴真实的 WebHook Secret、密码、Session Cookie、SMTP 凭据、仓库访问令牌、带凭据的仓库 URL、服务器 IP 或客户数据。不能确认是否敏感时，请先删除或替换为占位符。

功能建议应先描述要解决的实际问题，再说明建议方案、替代方案、安全影响和对现有配置的兼容性影响。项目不会因为功能通用而自动接受依赖数据库、Redis、消息队列、前端重型框架或任意 Shell 执行能力的方案。

## 提交 Pull Request

Pull Request 应：

- 关联对应 Issue，或解释为什么不需要 Issue。
- 说明变更目标、实现方式、风险和回滚方法。
- 列出实际执行的自动化检查和 Linux 手动验证环境。
- 为行为变化补充测试；不能补测试时说明原因和剩余风险。
- 同步更新相关示例、文档和 `CHANGELOG.md`。
- UI 变更提供桌面和移动端截图，并确认无明显遮挡、溢出或不可操作控件。
- 不混入格式化全仓库、重命名或与目标无关的重构。

维护者可能要求缩小范围、补充测试、安全说明或迁移方案。被接受的贡献将按仓库的 MIT License 发布；提交代码表示你有权以该许可证提供这项贡献。

## 文档与语言

面向用户的说明应简洁、可直接执行。命令示例必须区分占位符和真实值，并说明是否会修改系统状态。新增中文文档使用 UTF-8；公共配置键、环境变量和代码标识保持英文。
