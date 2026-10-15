(() => {
  const previews = {
    server:['server.webp','mini_deploy 服务器状态面板，显示 CPU、内存和核心资源趋势','资源趋势持续更新。需要回看时，再打开性能历史与进程现场。'],
    requests:['requests.webp','mini_deploy 请求记录，按路径归类，显示次数、耗时和上游','把动态 ID 归类到同一种路径，再逐层展开，查看具体请求与来源。'],
    ai:['ai.webp','mini_deploy AI 运维诊断，展示采集证据和分析结果','诊断从所选告警或性能范围开始，附带主机身份与时间，支持继续追问。'],
    security:['security.webp','mini_deploy 安全中心，展示主机风险、登录活动与采集状态','先查看风险依据与安全事件，再按需要处理。日志读取失败和未覆盖的检查会明确标记。']
  };
  const tabs = [...document.querySelectorAll('[data-preview]')], image = document.getElementById('preview-image');
  function select(tab) {
    const [file, alt, description] = previews[tab.dataset.preview];
    tabs.forEach(button => { button.setAttribute('aria-selected', String(button === tab)); button.tabIndex = button === tab ? 0 : -1; });
    image.src = `assets/${file}`; image.alt = alt;
    document.getElementById('preview-panel').setAttribute('aria-labelledby', tab.id);
    document.getElementById('preview-description').textContent = description;
  }
  tabs.forEach(tab => {
    tab.addEventListener('click', () => select(tab));
    tab.addEventListener('keydown', event => {
      if (!['ArrowRight','ArrowLeft','Home','End'].includes(event.key)) return;
      event.preventDefault();
      const index = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (tabs.indexOf(tab) + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
      select(tabs[index]); tabs[index].focus();
    });
  });
  document.querySelectorAll('[data-repository]').forEach(button => button.addEventListener('click', () => {
    document.querySelectorAll('[data-repository]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
    const remote = button.dataset.repository === 'github' ? 'https://github.com/jokerXueC/mini_deploy.git' : 'https://gitee.com/XC1960/mini_deploy.git';
    document.getElementById('install-code').textContent = `git clone ${remote} mini-deploy-source\ncd mini-deploy-source\nbash install.sh`;
    document.getElementById('copy-status').textContent = '在 root 终端执行。Git 需预先安装。';
  }));
  document.getElementById('copy-command').onclick = async () => {
    const text = document.getElementById('install-code').textContent;
    try { await navigator.clipboard.writeText(text); document.getElementById('copy-status').textContent = '安装命令已复制。'; }
    catch { const range = document.createRange(); range.selectNodeContents(document.getElementById('install-code')); const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range); document.getElementById('copy-status').textContent = '命令已选中，请使用系统复制。'; }
  };
  const dialog = document.getElementById('screenshot-dialog');
  document.getElementById('preview-enlarge').onclick = () => { const enlarged = document.getElementById('enlarged-image'); enlarged.src = image.src; enlarged.alt = image.alt; dialog.showModal(); };
  document.getElementById('close-screenshot').onclick = () => dialog.close();
  dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
})();
