(() => {
  'use strict';
  const el = id => document.getElementById(id);
  const n = (value, suffix = '') => Number.isFinite(value) ? value.toFixed(1) + suffix : '-';
  const h = escapeHtml;
  const dialog = document.createElement('dialog');
  dialog.className = 'ops-dialog'; dialog.id = 'performanceDialog';
  dialog.setAttribute('aria-labelledby', 'performanceTitle');
  dialog.innerHTML = `<header class="ops-head"><div><h2 id="performanceTitle">容器资源历史</h2><p id="performanceContainer"></p></div><button class="toolbar-icon" type="button" data-performance-close aria-label="关闭资源历史" title="关闭"><img src="ui/previews/ssh-ai/icons/x.svg" alt=""></button></header><div class="ops-tabs" role="group" aria-label="历史范围"><button type="button" data-performance-period="1h">1 小时</button><button type="button" data-performance-period="24h">24 小时</button><button type="button" data-performance-period="7d">7 天</button></div><p id="performanceFeedback" class="ops-feedback" role="status"></p><div class="ops-pane"><div id="performanceCharts" class="performance-charts"></div><p id="performanceNotice" class="ops-caption"></p></div>`;
  document.body.append(dialog);
  let container = '', period = '24h', generation = 0, controller;
  function chart(points, field, title, interval) {
    const valid = points.filter(p => Number.isFinite(p[field]));
    if (valid.length < 2) return `<section class="performance-chart"><h3>${title}</h3><p class="ops-caption">等待连续两次采样，约 1 分钟。历史缺失不会补零。</p></section>`;
    const from = points[0].ts, to = points.at(-1).ts, max = Math.max(100, ...valid.map(p => p[field]));
    let path = '', previous;
    for (const point of points) {
      if (!Number.isFinite(point[field])) { previous = null; continue; }
      const x = 30 + (point.ts - from) / Math.max(1, to - from) * 370;
      const y = 112 - point[field] / max * 90;
      path += `${previous && point.ts - previous.ts <= interval * 2 ? 'L' : 'M'}${x.toFixed(2)},${y.toFixed(2)} `;
      previous = point;
    }
    const time = ts => new Date(ts * 1000).toLocaleString('zh-CN', {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'});
    return `<section class="performance-chart"><h3>${title} · 最近 ${n(valid.at(-1)[field], '%')}</h3><svg viewBox="0 0 420 150" role="img" aria-label="${title} 历史趋势"><line x1="30" y1="112" x2="400" y2="112"/><text x="0" y="24">${max.toFixed(0)}%</text><text x="0" y="115">0</text><path d="${path}"/><text x="30" y="138">${time(from)}</text><text x="400" y="138" text-anchor="end">${time(to)}</text></svg></section>`;
  }
  async function load() {
    const run = ++generation; controller?.abort(); controller = new AbortController();
    el('performanceFeedback').textContent = '读取已保存的采样…';
    dialog.querySelectorAll('[data-performance-period]').forEach(b => b.setAttribute('aria-selected', String(b.dataset.performancePeriod === period)));
    try {
      const response = await fetch(`performance/containers?${new URLSearchParams({id:container, period})}`, {credentials:'same-origin', signal:controller.signal});
      const data = await response.json(); if (!response.ok) throw new Error(data.detail || data.error);
      if (run !== generation) return;
      el('performanceCharts').innerHTML = chart(data.points, 'cpu_percent', 'CPU（100% 为一个逻辑核）', data.interval_seconds) + chart(data.points, 'memory_percent', '内存', data.interval_seconds);
      el('performanceNotice').textContent = data.notice;
      el('performanceFeedback').textContent = `${data.points.length} 个时间点 · 保留最多 ${data.retention_days} 天`;
    } catch (error) { if (run === generation && error.name !== 'AbortError') { el('performanceCharts').replaceChildren(); el('performanceFeedback').textContent = error.message; } }
  }
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-performance-container]');
    if (button) { container = button.dataset.performanceContainer; el('performanceContainer').textContent = button.dataset.performanceName; dialog.showModal(); load(); }
  });
  dialog.addEventListener('click', event => {
    if (event.target.closest('[data-performance-close]')) dialog.close();
    const button = event.target.closest('[data-performance-period]');
    if (button) { period = button.dataset.performancePeriod; load(); }
  });
  dialog.addEventListener('close', () => { generation++; controller?.abort(); });
  let lastAt = 0;
  window.ServerPerformance = {update(server) {
    if (Date.now() - lastAt < 3000) return; lastAt = Date.now();
    const swap = server.memory?.swap, inode = server.disk?.inode || {}, io = server.disk_io || {};
    el('performanceBrief').textContent = `Swap ${swap ? swap.total_mb ? n(swap.percent, '%') : '未启用' : '-'} · inode ${n(inode.percent, '%')} · I/O wait ${n(io.iowait_percent, '%')}`;
    el('performanceValues').innerHTML = `<section><h3>Swap</h3><strong>${swap ? swap.total_mb ? n(swap.percent, '%') : '未启用' : '不可用'}</strong><p>${swap ? `${n(swap.used_mb, ' MiB')} / ${n(swap.total_mb, ' MiB')}` : '系统未提供交换分区数据'}</p></section><section><h3>根分区 inode</h3><strong>${n(inode.percent, '%')}</strong><p>${inode.total ? `${inode.used.toLocaleString()} / ${inode.total.toLocaleString()}` : '文件系统未提供 inode 统计'}</p></section><section><h3>磁盘读写 · I/O wait ${n(io.iowait_percent, '%')}</h3>${io.devices?.length ? io.devices.map(d => `<p><strong>${h(d.device)}</strong><br>读 ${n(d.read_kib_s, ' KiB/s')} · 写 ${n(d.write_kib_s, ' KiB/s')}<br>${n(d.iops)} IOPS · 延迟 ${n(d.await_ms, ' ms')} · 忙碌 ${n(d.busy_percent, '%')}</p>`).join('') : '<p>等待采样，或系统未提供设备统计</p>'}</section>`;
  }};
})();
