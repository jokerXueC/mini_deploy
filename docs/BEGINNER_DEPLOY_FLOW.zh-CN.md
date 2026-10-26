# mini_deploy 首次使用与升级说明

mini_deploy 是服务器运维与请求监控面板。完整的新手指引在[安装与功能使用指南](../ui/user-guide.md)，包含从首次安装到各功能启用、命令和验收的完整步骤；安装后也可从面板顶部文档图标离线阅读。

## 按问题直达

- [首次安装](../ui/user-guide.md#install) · [打不开网页与 HTTPS](../ui/user-guide.md#access) · [密码与路径](../ui/user-guide.md#login)
- [资源监控](../ui/user-guide.md#resources) · [性能历史与报告](../ui/user-guide.md#history) · [Docker 与日志](../ui/user-guide.md#docker)
- [请求记录、地图与手动接入](../ui/user-guide.md#requests) · [域名证书](../ui/user-guide.md#certificates)
- [SSH 与文件管理](../ui/user-guide.md#ssh) · [AI 运维与代码分析](../ui/user-guide.md#ai)
- [登录与基础防护](../ui/user-guide.md#security) · [Swap](../ui/user-guide.md#swap) · [异常监测](../ui/user-guide.md#monitoring) · [通知](../ui/user-guide.md#notifications)
- [更新与备份](../ui/user-guide.md#maintenance) · [排查命令](../ui/user-guide.md#troubleshooting) · [逐项验收](../ui/user-guide.md#acceptance)

## 从哪里开始

1. 按[快速上手](QUICKSTART.zh-CN.md)安装面板，用管理员密码登录。
2. 查看服务器监控及 Docker 容器、镜像；无需接入业务 Git 仓库。
3. 业务网站已运行时，选择代理日志或[独立请求网关](REQUEST_GATEWAY.zh-CN.md)采集请求。
4. 打开「域名与证书」，查看自动发现的网站、证书和到期时间。

无需重新登记已配置的网站。内部入口和默认站点自动收起；需要替换证书时，直接在对应网站上传证书链和私钥。原有托管证书使用同一入口，自动续签的证书继续由原服务管理。

## 哪些功能不再提供

不再接入 Git 部署，不填写部署脚本，不配置 Push WebHook，不生成业务运行环境，也不通过面板构建、发布或回滚业务。业务代码更新、运行环境和数据库维护由原有维护流程负责。

## 升级后会发生什么

- 部署页面、队列、脚本执行及仓库识别适配器已移除；旧部署 API 返回 HTTP `410 Gone`。
- 站点保存到 `sites.json`，旧 `projects.json` 只读提取站点。监控使用 `monitoring-state.json`，原 `state.json` 保留，不加载队列；启动或重启均不恢复旧部署任务。
- 旧服务器业务文件、配置、脚本、服务、容器和持久数据保留，不因移除部署功能而删除、停止或重建。
- 已有网站继续由原服务和代理承载；后续主动修改站点、容器或网关仍需核对影响并确认。

升级前保留快照和安装备份，沿用原路径及环境文件，不清空旧配置或状态。自动化及真实后端站点 UI 测试已通过，Linux 实机验收尚未执行，详见[验收清单](LINUX_TEST.zh-CN.md)。

更多边界见[安全策略](../SECURITY.md)和[路线图](ROADMAP.zh-CN.md)。
