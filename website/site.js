(() => {
  'use strict';
  const motion = matchMedia('(prefers-reduced-motion: reduce)');
  const previews = {
    server:['server.webp','mini_deploy 服务器状态面板，显示资源观测与入站、出站累计流量','资源观测关联 CPU、内存、网络与容器。回看历史时间点，再展开进程现场。'],
    requests:['requests.webp','mini_deploy 请求记录与来源地图，显示地区、IP、访问次数和错误次数','看看谁在请求、从哪里访问。按时间和状态筛选，再从地图进入 IP 明细。'],
    ai:['ai.webp','mini_deploy AI 运维诊断，展示已采集证据与分析结果','从异常进入对话，读取同一台主机的日志、资源和代码线索，保存每一次诊断。'],
    security:['security.webp','mini_deploy 安全中心，展示登录地区地图、IP 记录与基础防护','查看登录活动，手动封禁 IP，或按认证失败策略自动处理。防护范围取决于实际入口。']
  };
  const tabs = [...document.querySelectorAll('[data-preview]')], image = document.getElementById('preview-image');
  let imageMotion;
  function select(tab) {
    const [file, alt, description] = previews[tab.dataset.preview];
    tabs.forEach(button => { button.setAttribute('aria-selected', String(button === tab)); button.tabIndex = button === tab ? 0 : -1; });
    imageMotion?.cancel();
    image.onload = () => {
      if (!motion.matches) imageMotion = image.animate([{opacity:.35, transform:'translateY(8px)'},
        {opacity:1, transform:'translateY(0)'}], {duration:360, easing:'cubic-bezier(.2,.7,.3,1)'});
    };
    image.src = `assets/${file}`; image.alt = alt;
    document.getElementById('preview-panel').setAttribute('aria-labelledby', tab.id);
    document.getElementById('preview-description').textContent = description;
    document.getElementById('preview-number').textContent = `${String(tabs.indexOf(tab) + 1).padStart(2,'0')} / 04`;
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
    const branch = button.dataset.repository === 'github' ? 'main' : 'master';
    document.getElementById('install-code').textContent = `mini_deploy_source="$(mktemp -d /tmp/mini-deploy-source.XXXXXX)" && git clone --depth 1 --branch ${branch} ${remote} "$mini_deploy_source" && SETUP_NGINX=no SETUP_HTTPS=no bash "$mini_deploy_source/install.sh"`;
    document.getElementById('copy-status').textContent = '在 root 终端执行。Git 需预先安装；详细准备步骤见下方指南。';
  }));
  document.getElementById('copy-command').onclick = async () => {
    const text = document.getElementById('install-code').textContent;
    try { await navigator.clipboard.writeText(text); document.getElementById('copy-status').textContent = '安装命令已复制。'; }
    catch { const range = document.createRange(); range.selectNodeContents(document.getElementById('install-code')); const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range); document.getElementById('copy-status').textContent = '命令已选中，请使用系统复制。'; }
  };
  const dialog = document.getElementById('screenshot-dialog');
  document.getElementById('preview-enlarge').onclick = () => { const enlarged = document.getElementById('enlarged-image'); enlarged.src = image.src; enlarged.alt = image.alt; dialog.showModal(); };
  document.getElementById('close-screenshot').onclick = () => dialog.close();
  dialog.addEventListener('click', event => {
    const bounds = dialog.getBoundingClientRect();
    if (event.target === dialog && (event.clientX < bounds.left || event.clientX > bounds.right ||
        event.clientY < bounds.top || event.clientY > bounds.bottom)) dialog.close();
  });

  const header = document.querySelector('.site-header');
  let scrollFrame = 0;
  addEventListener('scroll', () => {
    if (!scrollFrame) scrollFrame = requestAnimationFrame(() => {
      header.dataset.scrolled = String(scrollY > 30); scrollFrame = 0;
    });
  }, {passive:true});
  const arrival = new IntersectionObserver(entries => {
    for (const entry of entries) if (entry.isIntersecting) {
      entry.target.dataset.entered = 'true'; arrival.unobserve(entry.target);
    }
  }, {threshold:.12});
  arrival.observe(document.querySelector('.product-section'));

  const canvas = document.getElementById('signal-canvas'), ctx = canvas.getContext('2d');
  const position = document.getElementById('signal-position'), pause = document.getElementById('signal-pause');
  const metrics = {
    cpu: {label:'CPU 使用率', unit:'%', color:'#2548ed', max:100, value:t =>
      31 + Math.sin(t / 7) * 8 + Math.sin(t / 2.8) * 4 + 49 * Math.exp(-Math.pow((t % 180 - 72) / 9, 2))},
    memory: {label:'内存使用率', unit:'%', color:'#128675', max:100, value:t =>
      56 + Math.sin(t / 20) * 9 + Math.sin(t / 6) * 2 + 13 * Math.exp(-Math.pow((t % 180 - 84) / 18, 2))},
    network: {label:'网络吞吐', unit:'KiB/s', color:'#a57517', max:1000, value:t =>
      220 + Math.sin(t / 9) * 70 + Math.sin(t / 3) * 27 + 560 * Math.exp(-Math.pow((t % 180 - 75) / 11, 2))}
  };
  let metric = 'cpu', step = 0, fraction = 0, selected = 119, paused = motion.matches;
  let visible = false, frame = 0, previous = 0, width = 0, height = 0;

  function reading() {
    const spec = metrics[metric], value = spec.value(step + selected);
    document.getElementById('signal-metric').textContent = spec.label;
    document.getElementById('signal-value').replaceChildren(document.createTextNode(value.toFixed(1)),
      Object.assign(document.createElement('span'), {textContent:spec.unit}));
    document.getElementById('signal-time').textContent = selected === 119 ? '最近采样 · 演示数据' :
      `${((119 - selected) / 2).toFixed(1)} 秒前 · 演示数据`;
    canvas.setAttribute('aria-label', `${spec.label}回放曲线，演示数据，所选值 ${value.toFixed(1)} ${spec.unit}`);
    canvas.dataset.step = String(step);
  }
  function draw() {
    if (!ctx || !width || !height) return;
    ctx.clearRect(0, 0, width, height);
    const spec = metrics[metric], left = 2, right = width - 3, top = 12, bottom = height - 12;
    const x = i => left + i / 119 * (right - left);
    const y = value => bottom - value / spec.max * (bottom - top);
    ctx.strokeStyle = '#e6ebec'; ctx.lineWidth = 1;
    for (const ratio of [.25, .5, .75]) { const at = y(spec.max * ratio); ctx.beginPath(); ctx.moveTo(left, at); ctx.lineTo(right, at); ctx.stroke(); }
    const values = Array.from({length:121}, (_, i) => spec.value(step + i));
    const points = values.slice(0,120).map((value,i) => value + (values[i + 1] - value) * fraction);
    ctx.beginPath(); ctx.moveTo(left, bottom);
    points.forEach((value,i) => ctx.lineTo(x(i), y(value)));
    ctx.lineTo(right, bottom); ctx.closePath(); ctx.fillStyle = spec.color + '0b'; ctx.fill();
    ctx.beginPath(); points.forEach((value,i) => i ? ctx.lineTo(x(i), y(value)) : ctx.moveTo(x(i), y(value)));
    ctx.strokeStyle = spec.color; ctx.lineWidth = 2; ctx.lineJoin = 'round'; ctx.stroke();
    const peak = points.indexOf(Math.max(...points));
    ctx.fillStyle = '#b77b22'; ctx.beginPath(); ctx.arc(x(peak), y(points[peak]), 3, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = '#9faeb2'; ctx.setLineDash([3,4]); ctx.beginPath(); ctx.moveTo(x(selected), top); ctx.lineTo(x(selected), bottom); ctx.stroke(); ctx.setLineDash([]);
    ctx.beginPath(); ctx.arc(x(selected), y(points[selected]), 4, 0, Math.PI * 2); ctx.fillStyle = spec.color; ctx.fill();
    canvas.dataset.frame = String(Number(canvas.dataset.frame || 0) + 1);
  }
  function stop() { cancelAnimationFrame(frame); frame = 0; previous = 0; }
  function animate(now) {
    frame = 0;
    if (paused || !visible || document.hidden) return;
    if (previous) fraction += Math.min(100, now - previous) / 800;
    previous = now;
    if (fraction >= 1) { step++; fraction %= 1; position.value = String(selected = 119); reading(); }
    draw(); frame = requestAnimationFrame(animate);
  }
  function schedule() {
    stop();
    if (!paused && visible && !document.hidden) frame = requestAnimationFrame(animate);
  }
  function setPaused(value) {
    paused = value;
    pause.setAttribute('aria-pressed', String(value));
    pause.title = pause.ariaLabel = value ? '播放回放' : '暂停回放';
    pause.querySelector('img').src = value ? 'assets/play.svg' : 'assets/pause.svg';
    if (value) { fraction = 0; draw(); reading(); }
    else { selected = 119; position.value = '119'; reading(); }
    schedule();
  }
  document.querySelectorAll('[data-metric]').forEach(button => button.addEventListener('click', () => {
    metric = button.dataset.metric;
    document.querySelectorAll('[data-metric]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
    draw(); reading();
  }));
  pause.onclick = () => setPaused(!paused);
  position.oninput = () => { selected = Number(position.value); setPaused(true); };
  canvas.addEventListener('pointerdown', event => {
    const bounds = canvas.getBoundingClientRect();
    position.value = String(Math.round(Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width)) * 119));
    position.dispatchEvent(new Event('input'));
  });
  const resize = new ResizeObserver(() => {
    const rect = canvas.getBoundingClientRect(), scale = Math.min(devicePixelRatio || 1, 2);
    width = rect.width; height = rect.height;
    canvas.width = Math.round(width * scale); canvas.height = Math.round(height * scale);
    ctx?.setTransform(scale, 0, 0, scale, 0, 0); draw();
  });
  resize.observe(canvas);
  const viewport = new IntersectionObserver(entries => { visible = entries[0].isIntersecting; schedule(); }, {threshold:0});
  viewport.observe(canvas);
  document.addEventListener('visibilitychange', schedule);
  motion.addEventListener('change', () => { if (motion.matches) { imageMotion?.cancel(); setPaused(true); } });
  setPaused(paused); reading();
})();
