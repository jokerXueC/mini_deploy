# mini_deploy 官网

直接用浏览器打开 `index.html`。HTML、样式、脚本和截图均为本地静态文件，无需 Node.js 或构建。

发布时将本目录上传到任意静态站点托管或已有 Web 服务。它不需要访问面板 API，也不要把私密面板配置或数据目录一起上传。

截图为真实面板界面配合模拟数据。维护者可运行 `python scripts/capture_website.py` 重新生成图片，需要 Playwright、Chromium 与 Pillow；这些不是面板运行依赖。

首屏动态演示直接复用真实面板资源区的 HTML 与 CSS：CPU、内存、磁盘、网络数值与分段资源条同步更新，吞吐积分为累计流量，资源时间轴持续更新。逐核视图、指标切换、时间范围与时间点均可操作。`panel-demo.html` 和 `assets/panel.css` 由 `scripts/capture_website.py` 从真实 UI 生成，请勿另行设计这些模块；修改面板 UI 后运行该脚本同步。官网演示脚本独立于生产面板，不加载 API 客户端。

下方资源回放可切换 CPU / 内存 / 网络，支持暂停与时间点选择。两处均明确标注模拟数据，不连接服务器或面板 API；不可见或切入后台时停止动画，遵循系统减少动态效果设置。安装命令默认使用 GitHub。
