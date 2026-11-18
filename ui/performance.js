(() => {
  'use strict';
  const el = id => document.getElementById(id);
  const n = (value, suffix = '') => Number.isFinite(value) ? value.toFixed(1) + suffix : '-';
  const h = escapeHtml;
  const dialog = document.createElement('dialog');
  dialog.className = 'ops-dialog'; dialog.id = 'performanceDialog';
  dialog.setAttribute('aria-labelledby', 'performanceTitle');
  dialog.innerHTML = `<header class="ops-head"><div><h2 id="performanceTitle">容器运行观测</h2><p id="performanceContainer"></p></div><button class="toolbar-icon" type="button" data-performance-close aria-label="关闭资源历史" title="关闭"><img src="ui/previews/ssh-ai/icons/x.svg" alt=""></button></header><div class="ops-tabs container-resource-tabs" role="tablist" aria-label="容器观测分类">${[['resources','资源'],['network','网络'],['storage','磁盘与日志'],['processes','进程'],['events','事件']].map(([key,label])=>`<button role="tab" type="button" data-container-pane="${key}" aria-selected="${key==='resources'}">${label}</button>`).join('')}</div><div class="container-history-tools"><div role="group" aria-label="历史范围"><button type="button" data-performance-period="1h">1 小时</button><button type="button" data-performance-period="24h">24 小时</button><button type="button" data-performance-period="7d">7 天</button></div><button id="containerHistoryRefresh" class="toolbar-icon" type="button" title="刷新容器观测" aria-label="刷新容器观测"><img src="ui/previews/ssh-ai/icons/refresh-cw.svg" alt=""></button></div><p id="performanceFeedback" class="ops-feedback" role="status"></p><div class="ops-pane"><div id="containerCurrentMetrics" class="container-current-metrics"></div><p id="containerMetricNotice" class="ops-caption"></p><div id="performanceCharts" class="performance-charts"></div><div id="containerEvidence"></div><p id="performanceNotice" class="ops-caption"></p></div>`;
  document.body.append(dialog);
  let container = '', period = '24h', pane = 'resources', generation = 0, controller, data, timer, loading = false;
  const date = value => value ? new Date(value * 1000).toLocaleString() : '尚无采样';
  const definitions = {
    resources: [['cpu_percent','CPU（100% 为单核）','%'],['cpu_limit_percent','CPU 配额利用率','%'],['cpu_throttled_percent','CPU 限流周期占比','%'],['cpu_pressure_percent','CPU 等待压力','%'],['memory_limit_percent','内存上限利用率','%'],['memory_used_bytes','内存工作集','bytes'],['memory_current_bytes','内存总占用（含缓存）','bytes'],['memory_headroom_bytes','内存余量','bytes'],['memory_anon_bytes','匿名内存','bytes'],['memory_cache_bytes','文件缓存','bytes'],['memory_swap_bytes','已用 Swap','bytes'],['memory_pressure_percent','内存等待压力','%']],
    network: [['network_rx_bytes_s','网络入站','bytes/s'],['network_tx_bytes_s','网络出站','bytes/s']],
    storage: [['block_read_bytes_s','磁盘读取','bytes/s'],['block_write_bytes_s','磁盘写入','bytes/s'],['log_bytes','当前日志段大小','bytes'],['log_bytes_s','当前日志段增长','bytes/s'],['io_pressure_percent','I/O 等待压力','%']],
    processes: [['pids_current','进程与线程','个'],['pids_percent','PID 上限利用率','%'],['main_process_fds','主进程文件描述符','个'],['main_process_fd_percent','主进程 FD 上限利用率','%']],
  };
  function format(value, unit = '') {
    if (!Number.isFinite(value)) return '未知';
    if (unit.startsWith('bytes')) {
      const power = value > 0 ? Math.min(4, Math.floor(Math.log(value) / Math.log(1024))) : 0;
      return `${(value / 1024 ** Math.max(0, power)).toFixed(1)} ${['B','KiB','MiB','GiB','TiB'][Math.max(0,power)]}${unit.endsWith('/s')?'/s':''}`;
    }
    return n(value, unit);
  }
  function chart(points, field, title, interval, unit, summary = {}) {
    const valid = points.filter(p => Number.isFinite(p[field]));
    if (valid.length < 2) return `<section class="performance-chart"><h3>${title}</h3><p class="ops-caption">${valid.length ? '样本不足，等待后续采样。' : '该时段没有可用样本。'}历史缺失不补零。</p></section>`;
    const from = data.since || points[0].ts, to = data.until || points.at(-1).ts;
    const max = Math.max(unit === '%' ? 100 : 1, ...valid.map(p => p[field + '_peak'] ?? p[field]));
    let path = '', peakPath = '', previous;
    for (const point of points) {
      if (!Number.isFinite(point[field])) { previous = null; continue; }
      const x = 60 + (point.ts - from) / Math.max(1, to - from) * 340;
      const y = 112 - point[field] / max * 84;
      path += `${previous && point.ts - previous.ts <= interval * 2 ? 'L' : 'M'}${x.toFixed(2)},${y.toFixed(2)} `;
      if (Number.isFinite(point[field + '_peak'])) peakPath += `${previous && point.ts - previous.ts <= interval * 2 ? 'L' : 'M'}${x.toFixed(2)},${(112-point[field+'_peak']/max*84).toFixed(2)} `;
      previous = point;
    }
    const time = ts => new Date(ts * 1000).toLocaleString('zh-CN', {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'});
    return `<section class="performance-chart"><h3>${h(title)}</h3><p class="container-chart-values">平均 ${h(format(summary.average,unit))} <span>峰值 ${h(format(summary.peak,unit))}</span>${Number.isFinite(summary.coverage_percent) ? `<span>覆盖 ${n(summary.coverage_percent,'%')}</span>` : ''}</p><svg viewBox="0 0 420 150" role="img" aria-label="${h(title)} 历史趋势"><line x1="60" y1="112" x2="400" y2="112"/><text x="0" y="24">${h(format(max,unit))}</text><text x="0" y="115">0</text>${peakPath ? `<path class="container-peak-line" d="${peakPath}"/>` : ''}<path d="${path}"/><text x="60" y="138">${time(from)}</text><text x="400" y="138" text-anchor="end">${time(to)}</text></svg></section>`;
  }
  function render() {
    if (!data) return;
    const current = data.current || {}, resource = {...(current.resources || {}), cpu_percent: current.cpu_percent};
    const evidence = current.inspection || {}, stale = data.current_stale;
    const fields = definitions[pane] || [];
    dialog.querySelectorAll('[data-container-pane]').forEach(button => {
      const selected = button.dataset.containerPane === pane;
      button.setAttribute('aria-selected', String(selected)); button.tabIndex = selected ? 0 : -1;
    });
    el('containerCurrentMetrics').innerHTML = fields.map(([key,title,unit]) => `<div><span>${h(title)}</span><strong>${h(format(stale ? null : resource[key], unit))}</strong></div>`).join('');
    const limits = [`CPU 配额 ${Number.isFinite(resource.cpu_limit_cores)?resource.cpu_limit_cores+' 核':'未获取或未设限'}`, `内存上限 ${format(resource.memory_limit_bytes,'bytes')}`, `PID 上限 ${Number.isFinite(resource.pids_limit)?resource.pids_limit:'未获取或未设限'}`].join(' · ');
    el('containerMetricNotice').textContent = pane === 'resources' ? `${limits}。${stale ? '当前采样已过期；' : ''}${resource.notice || '当前运行指标尚不可读，仍可查看保留历史。'}` : pane === 'network' ? '容器网络接口累计计数的差值；host 网络可能不可归因，0 流量不代表业务正常。' : pane === 'storage' ? `日志驱动：${resource.log_driver || '未知'}。仅统计当前日志段，轮转会重新建立增长基线；块 I/O 不等于应用读写总量或磁盘延迟。` : pane === 'processes' ? `PID 计数包含线程；FD 仅为主进程。${data.process_notice || ''}` : '以下为所选时间内保留的事件；同时发生不等于已确认原因。';
    const chartFields = pane === 'resources' ? fields.filter(([key]) => ['cpu_percent','cpu_throttled_percent','memory_current_bytes','memory_limit_percent'].includes(key)) : fields;
    if (pane === 'resources' && !(data.points || []).some(p => Number.isFinite(p.memory_current_bytes))) {
      chartFields.splice(chartFields.findIndex(([key]) => key === 'memory_current_bytes'), 1, ['memory_percent','内存（Docker 原有统计）','%']);
    }
    el('performanceCharts').innerHTML = chartFields.map(([key,title,unit]) => chart(data.points || [],key,title,data.interval_seconds,unit,(data.summary || {})[key])).join('');
    let html = '';
    if (pane === 'resources') {
      const check = evidence.health_last_check || {};
      html = `<div class="container-health-summary"><span>健康检查：${h(({healthy:'通过',unhealthy:'失败',starting:'启动中',none:'未配置'})[evidence.health || current.health] || '未知')}</span><span>检查耗时：${h(format(check.duration_ms,' ms'))}</span><span>连续失败：${h(String(evidence.health_failing_streak ?? '未知'))}</span><span>重启次数：${h(String(evidence.restart_count ?? '未知'))}</span><span>cgroup OOM 终止累计：${h(format(resource.oom_kill_total,'次'))}</span></div>`;
    } else if (pane === 'processes') {
      html = `<p class="ops-caption">进程现场：${h(date(data.process_sampled_at))}</p><div class="ops-table-scroll"><table class="ops-table"><thead><tr><th>进程 / PID</th><th>CPU（单核）</th><th>常驻内存</th><th>读取 / 写入</th></tr></thead><tbody>${(data.processes || []).map(p => `<tr><td>${h(p.name || '')} / ${h(String(p.pid))}</td><td>${h(format(p.cpu_percent,'%'))}</td><td>${h(format(p.rss_mib,' MiB'))}</td><td>${h(format(p.read_kib_s,' KiB/s'))} / ${h(format(p.write_kib_s,' KiB/s'))}</td></tr>`).join('') || '<tr><td colspan="4">尚无可关联的进程现场。</td></tr>'}</tbody></table></div>`;
    } else if (pane === 'events') {
      html = `<p class="ops-caption">${data.event_count || 0} 条关联事件 · 最多显示最近 100 条</p><div class="container-event-list">${(data.events || []).map(e => `<div><time>${h(date(e.at_ts))}</time><strong>${h(e.title || '')}</strong><p>${h(e.detail || '')}</p></div>`).join('') || '<p class="ops-caption">当前保留范围内没有关联事件，不能据此证明没有发生故障。</p>'}</div>`;
    }
    el('containerEvidence').innerHTML = html;
    el('performanceNotice').textContent = `${data.capacity_limited ? '容量保护曾裁剪较早历史。' : ''}${data.notice || ''}`;
  }
  function schedule() { clearTimeout(timer); if (dialog.open && !document.hidden) timer = setTimeout(load, 15000); }
  async function load() {
    if (!dialog.open) return;
    const run = ++generation; controller?.abort(); controller = new AbortController();
    loading = true;
    el('performanceFeedback').textContent = '读取已保存的采样…';
    dialog.querySelectorAll('[data-performance-period]').forEach(b => b.setAttribute('aria-selected', String(b.dataset.performancePeriod === period)));
    try {
      const response = await fetch(`performance/containers?${new URLSearchParams({id:container, period})}`, {credentials:'same-origin', signal:controller.signal});
      const result = await response.json(); if (!response.ok) throw new Error(result.detail || result.error);
      if (run !== generation) return;
      data = result; render();
      el('performanceFeedback').textContent = `${data.current_stale ? '当前采样已过期 · ' : ''}历史覆盖 ${n(data.coverage_percent,'%')} · 最近历史 ${date(data.latest_at)} · 自动更新`;
    } catch (error) { if (run === generation && error.name !== 'AbortError') el('performanceFeedback').textContent = `读取失败：${error.message}；${data ? '保留上次结果，' : ''}稍后重试。`; }
    finally { if (run === generation) { loading = false; schedule(); } }
  }
  function clearResults() {
    data = null;
    ['performanceCharts','containerCurrentMetrics','containerEvidence'].forEach(id => el(id).replaceChildren());
    el('containerMetricNotice').textContent = ''; el('performanceNotice').textContent = '';
  }
  function open(id, name) {
    container = id; pane = 'resources'; el('performanceContainer').textContent = name;
    clearResults();
    dialog.querySelectorAll('[data-container-pane]').forEach(button => {
      const selected = button.dataset.containerPane === pane;
      button.setAttribute('aria-selected', String(selected)); button.tabIndex = selected ? 0 : -1;
    });
    dialog.showModal(); load();
  }
  window.ContainerPerformance = {open};
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-performance-container]');
    if (button) open(button.dataset.performanceContainer, button.dataset.performanceName);
  });
  dialog.addEventListener('click', event => {
    if (event.target.closest('[data-performance-close]')) dialog.close();
    const button = event.target.closest('[data-performance-period]');
    if (button && period !== button.dataset.performancePeriod) { period = button.dataset.performancePeriod; clearResults(); load(); }
    const next = event.target.closest('[data-container-pane]');
    if (next) { pane = next.dataset.containerPane; render(); }
    if (event.target.closest('#containerHistoryRefresh')) load();
  });
  dialog.addEventListener('close', () => { generation++; controller?.abort(); clearTimeout(timer); loading = false; });
  dialog.querySelector('.container-resource-tabs').addEventListener('keydown', event => {
    if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
    const buttons = [...dialog.querySelectorAll('[data-container-pane]')], index = buttons.indexOf(document.activeElement);
    if (index < 0) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + buttons.length) % buttons.length;
    buttons[next].focus(); buttons[next].click();
  });
  document.addEventListener('visibilitychange', () => { if (dialog.open && !document.hidden && !loading) load(); else if (document.hidden) clearTimeout(timer); });
  el('performanceValues').innerHTML = `<section><div class="swap-heading"><h3>Swap</h3><label class="swap-control"><input id="hostSwapToggle" type="checkbox" role="switch" aria-label="启用 Swap" disabled><span aria-hidden="true"></span></label></div><strong id="hostSwapValue">-</strong><p id="hostSwapUsage"></p><p id="hostSwapFeedback" role="status">展开后读取主机 Swap 配置</p><button id="hostSwapRetry" type="button" class="ghost-button compact-action" hidden>重试保存开机配置</button></section><section id="performanceInode"></section><section id="performanceDiskIO"></section>`;
  let swapState = {}, swapPending = false, swapLoading = false, swapTimer, swapDesired = null;
  const swapToggle = el('hostSwapToggle');
  function renderSwap() {
    swapToggle.checked = swapPending && swapDesired !== null ? swapDesired : Boolean(swapState.owned ? swapState.managed_active : swapState.enabled);
    swapToggle.disabled = swapPending || swapLoading || !swapState.editable;
    swapToggle.setAttribute('aria-busy', String(swapPending));
    el('hostSwapRetry').hidden = !swapState.editable || (swapState.mode === 'external'
      ? !swapState.enabled || !swapState.restore_pending : !swapState.managed_active || swapState.persistent);
    el('hostSwapRetry').textContent = swapState.mode === 'external' ? '恢复其余系统 Swap' : '重试保存开机配置';
    el('hostSwapRetry').disabled = swapPending || swapLoading;
    el('hostSwapFeedback').textContent = (['running', 'failed'].includes(swapState.job?.state) ? swapState.job.detail : swapState.detail) || '正在读取主机配置…';
    swapToggle.title = swapPending ? '正在处理，请稍候' : swapState.detail || '启用 Swap';
  }
  async function loadSwap() {
    if (swapLoading) return;
    clearTimeout(swapTimer); swapLoading = true; renderSwap();
    try {
      swapState = await fetchJson('system/swap');
      swapPending = swapState.job?.state === 'running';
      if (!swapPending) swapDesired = null;
    } catch (error) {
      swapState = {...swapState, editable:false, job:null, detail:`暂时无法读取 Swap：${error.message}`};
    } finally {
      swapLoading = false; renderSwap();
      if (swapPending || el('performancePanel').open) swapTimer = setTimeout(loadSwap, swapPending ? 1500 : 10000);
    }
  }
  el('performancePanel').addEventListener('toggle', () => {
    if (el('performancePanel').open) loadSwap();
    else if (!swapPending) clearTimeout(swapTimer);
  });
  async function changeSwap(enabled) {
    if (swapPending || swapLoading || !swapState.editable) { renderSwap(); return; }
    clearTimeout(swapTimer); swapDesired = enabled; swapPending = true; renderSwap();
    const external = swapState.mode === 'external';
    if (!enabled && !await showConfirmDialog({title:external ? '临时关闭系统 Swap？' : '关闭面板 Swap？',
      message:external ? '关闭会将交换数据移回内存；内存不足时会拒绝操作。保留系统原有交换文件和开机配置，可再次开启；重启后可能自动恢复。'
        : '关闭会将交换数据移回内存；内存不足时会拒绝操作。交换文件保留，系统原有 Swap 不受影响。', confirmText:'关闭 Swap'})) {
      swapDesired = null; swapPending = false; renderSwap();
      if (el('performancePanel').open) swapTimer = setTimeout(loadSwap, 10000);
      return;
    }
    clearTimeout(swapTimer);
    try {
      swapState = await postJsonBody('system/swap', {enabled});
      swapPending = swapState.job?.state === 'running'; renderSwap();
      await loadSwap();
    } catch (error) {
      // A lost response does not prove the operation failed; read actual state before retrying.
      await loadSwap();
      el('hostSwapFeedback').textContent = `提交未确认：${error.message}。请核对当前状态。`;
    }
  }
  swapToggle.addEventListener('change', () => changeSwap(swapToggle.checked));
  el('hostSwapRetry').addEventListener('click', () => changeSwap(true));
  let lastAt = 0;
  window.ServerPerformance = {update(server) {
    if (Date.now() - lastAt < 3000) return; lastAt = Date.now();
    const swap = server.memory?.swap, inode = server.disk?.inode || {}, io = server.disk_io || {};
    el('performanceBrief').textContent = `Swap ${swap ? swap.total_mb ? n(swap.percent, '%') : '未启用' : '-'} · inode ${n(inode.percent, '%')} · I/O wait ${n(io.iowait_percent, '%')}`;
    el('hostSwapValue').textContent = swap ? swap.total_mb ? n(swap.percent, '%') : '未启用' : '不可用';
    el('hostSwapUsage').textContent = swap ? `${n(swap.used_mb, ' MiB')} / ${n(swap.total_mb, ' MiB')}` : '系统未提供交换分区数据';
    el('performanceInode').innerHTML = `<h3>根分区 inode</h3><strong>${n(inode.percent, '%')}</strong><p>${inode.total ? `${inode.used.toLocaleString()} / ${inode.total.toLocaleString()}` : '文件系统未提供 inode 统计'}</p>`;
    el('performanceDiskIO').innerHTML = `<h3>磁盘读写 · I/O wait ${n(io.iowait_percent, '%')}</h3>${io.devices?.length ? io.devices.map(d => `<p><strong>${h(d.device)}</strong><br>读 ${n(d.read_kib_s, ' KiB/s')} · 写 ${n(d.write_kib_s, ' KiB/s')}<br>${n(d.iops)} IOPS · 延迟 ${n(d.await_ms, ' ms')} · 忙碌 ${n(d.busy_percent, '%')}</p>`).join('') : '<p>等待采样，或系统未提供设备统计</p>'}`;
  }};
})();
