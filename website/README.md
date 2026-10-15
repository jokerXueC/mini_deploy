# mini_deploy 官网

直接用浏览器打开 `index.html`。HTML、样式、脚本和截图均为本地静态文件，无需 Node.js 或构建。

发布时将本目录上传到任意静态站点托管或已有 Web 服务。它不需要访问面板 API，也不要把私密面板配置或数据目录一起上传。

截图为真实面板界面配合模拟数据。维护者可运行 `python scripts/capture_website.py` 重新生成图片，需要 Playwright、Chromium 与 Pillow；这些不是面板运行依赖。
