(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let data = JSON.parse(new TextDecoder().decode(Uint8Array.from(atob($('reportData').textContent), c => c.charCodeAt(0))));
  let meta = data.metadata || {};
  const format = (value, unit = '') => Number.isFinite(value) ? `${value.toLocaleString('zh-CN', {maximumFractionDigits:2})}${unit ? ' ' + unit : ''}` : '未记录';
  const stat = key => data.summary[key] || {};
  const totals = [];
  function renderOverview() {
  meta = data.metadata || {};
  $('server').textContent = `${meta.hostname || '服务器'} · ${meta.logical_cores ? meta.logical_cores + ' 个逻辑核' : '核心数未记录'} · ${format(meta.memory_total_mb == null ? null : meta.memory_total_mb / 1024, 'GiB 内存')}`;
  $('range').textContent = `${data.since} 至 ${data.until}（UTC）`;
  $('coverage').textContent = `覆盖率 ${format(data.coverage_percent, '%')} · ${format(data.coverage_seconds)} 个不同采样秒 · ${format(data.points.length)} 组统计 · ${data.session_count} 个采样批次${data.first_at ? ` · 实际记录 ${data.first_at} 至 ${data.latest_at}` : data.error ? ' · 历史采样保存失败' : data.stale || data.last_saved_at ? ' · 所选时段没有已保存的采样' : ' · 等待首次采样，通常在 30 秒内开始显示'}`;
  $('notice').textContent = data.notice + ' 覆盖率按有有效指标的采样秒计算，各指标完整程度见下方有效采样数。';
  $('warnings').replaceChildren();
  for (const text of [data.error, data.stale ? '最近采样已超过 65 秒，采集可能中断。' : '', data.coverage_percent < 95 ? '所选时间范围的数据尚未完整覆盖；升级前、停机时或采集失败的时间无法补回。' : ''].filter(Boolean)) {
    const p = document.createElement('p'); p.textContent = text; $('warnings').append(p);
  }
  $('downloads').hidden = !data.package;
  for (const [index, [title, value, note]] of [
    ['CPU 平均', format(stat('cpu_percent').average, '%'), '峰值 ' + format(stat('cpu_percent').peak, '%')],
    ['内存平均', format(stat('memory_percent').average, '%'), '峰值 ' + format(stat('memory_percent').peak, '%')],
    ['根分区峰值', format(stat('disk_percent').peak, '%'), '最后 ' + format(stat('disk_percent').latest, '%')],
    ['接收平均', format(stat('network_rx_kib_s').average, 'KiB/s'), '峰值 ' + format(stat('network_rx_kib_s').peak, 'KiB/s')],
    ['发送平均', format(stat('network_tx_kib_s').average, 'KiB/s'), '峰值 ' + format(stat('network_tx_kib_s').peak, 'KiB/s')],
    ['CPU ≥ 85%', format(stat('cpu_percent').high == null ? null : stat('cpu_percent').high / 60, '分钟'), '按实际 1 秒采样累计']].entries()) {
    if (!totals[index]) {
      const metric = document.createElement('div'); metric.className = 'metric';
      totals[index] = ['span', 'strong', 'small'].map(tag => document.createElement(tag));
      metric.append(...totals[index]); $('totals').append(metric);
    }
    [title, value, note].forEach((text, i) => totals[index][i].textContent = text);
  }
  }
  renderOverview();
  const chartSpecs = [
    ['CPU', '%', [['cpu_percent', '平均', '#0d9c7c'], ['cpu_percent_peak', '峰值', '#cf6273']]],
    ['内存', '%', [['memory_percent', '平均', '#428dbe'], ['memory_percent_peak', '峰值', '#b9953b']]],
    ['系统负载', '', [['load_one', '1 分钟', '#0d9c7c'], ['load_five', '5 分钟', '#428dbe'], ['load_fifteen', '15 分钟', '#b9953b']]],
    ['网络吞吐', 'KiB/s', [['network_rx_kib_s', '接收平均', '#428dbe'], ['network_tx_kib_s', '发送平均', '#0d9c7c']]],
    ['磁盘读写', 'KiB/s', [['disk_read_kib_s', '读取平均', '#428dbe'], ['disk_write_kib_s', '写入平均', '#b9953b']]],
    ['根分区与 inode', '%', [['disk_percent', '磁盘占用', '#0d9c7c'], ['inode_percent', 'inode', '#cf6273']]],
    ['Swap 与 I/O wait', '%', [['swap_percent', 'Swap', '#428dbe'], ['iowait_percent', 'I/O wait', '#b9953b']]],
    ['磁盘延迟', 'ms', [['disk_await_ms', '平均', '#428dbe'], ['disk_await_ms_peak', '峰值', '#cf6273']]]];
  function chartPoints(series, maximumPoints) {
    const raw = data.points;
    if (raw.length <= maximumPoints) return raw;
    const width = Math.max(data.interval_seconds, (raw.at(-1).first - raw[0].first) / maximumPoints);
    const groups = []; let current, previous;
    for (const point of raw) {
      const cell = Math.floor((point.first - raw[0].first) / width);
      if (!current || cell !== current.cell || previous && point.first - previous.last > data.interval_seconds * 2) {
        current = {cell, first:point.first, last:point.last, sums:{}, weights:{}}; groups.push(current);
      }
      current.last = point.last;
      for (const [key] of series) {
        if (!Number.isFinite(point[key])) continue;
        if (key.endsWith('_peak')) current[key] = Math.max(current[key] ?? 0, point[key]);
        else {
          const weight = point.weights?.[key] ?? point.observations;
          current.sums[key] = (current.sums[key] || 0) + point[key] * weight;
          current.weights[key] = (current.weights[key] || 0) + weight;
        }
      }
      previous = point;
    }
    return groups.map(group => {
      for (const [key] of series) if (!key.endsWith('_peak')) group[key] = group.weights[key] ? group.sums[key] / group.weights[key] : null;
      return group;
    });
  }
  let animation = 0, animationProgress = 1;
  const mix = (a, b, amount) => a + (b - a) * amount;
  function previousValue(points, at, key) {
    if (!points.length) return null;
    if (at >= points.at(-1).first) return points.at(-1)[key];
    if (at <= points[0].first) return points[0][key];
    let lo = 0, hi = points.length - 1;
    while (lo + 1 < hi) { const mid = (lo + hi) >> 1; if (points[mid].first <= at) lo = mid; else hi = mid; }
    const a = points[lo], b = points[hi];
    if (!Number.isFinite(a[key]) || !Number.isFinite(b[key]) || b.first - a.last > data.interval_seconds * 2) return null;
    return mix(a[key], b[key], (at - a.first) / (b.first - a.first));
  }
  function draw(canvas, empty, unit, series) {
    const rect = canvas.getBoundingClientRect(), ctx = canvas.getContext('2d'), ratio = devicePixelRatio || 1;
    const width = Math.round(rect.width * ratio), height = Math.round(rect.height * ratio);
    if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, rect.width, rect.height);
    if (canvas.plotData !== data || canvas.plotWidth !== rect.width) {
      canvas.plotPoints = chartPoints(series, Math.max(80, Math.floor(rect.width / 3)));
      canvas.plotData = data; canvas.plotWidth = rect.width;
    }
    const points = canvas.plotPoints; let maximum = unit === '%' ? 100 : 1, valid = false;
    for (const p of points) for (const [key] of series) if (Number.isFinite(p[key])) { maximum = Math.max(maximum, p[key]); valid = true; }
    empty.hidden = valid;
    if (!valid || rect.width < 1) { canvas.lastPlot = null; canvas.sceneRange = null; return; }
    const before = animationProgress < 1 ? canvas.motionFrom : null, amount = before ? animationProgress : 1;
    const first = before ? mix(before.first, points[0].first, amount) : points[0].first;
    const last = before ? mix(before.last, points.at(-1).first, amount) : points.at(-1).first;
    const left = 50, right = rect.width - 15, top = 10, bottom = rect.height - 32;
    const x = p => left + (right - left) * (last === first ? .5 : (p.first - first) / (last - first));
    canvas.sceneRange = {first, last, left, right};
    const targetScale = unit === '%' ? maximum : maximum * 1.08;
    const scale = before ? mix(before.scale, targetScale, amount) : targetScale;
    if (amount === 1) canvas.lastPlot = {points, first, last, scale};
    const reading = (point, key) => {
      const previous = before ? previousValue(before.points, point.first, key) : null;
      return Number.isFinite(previous) ? mix(previous, point[key], amount) : point[key];
    };
    const y = value => bottom - (bottom - top) * value / scale;
    const style = getComputedStyle(document.body);
    ctx.font = '11px "PingFang SC", "Microsoft YaHei", sans-serif';
    for (let i = 0; i <= 4; i++) {
      const v = scale * i / 4; ctx.strokeStyle = style.getPropertyValue('--report-line').trim(); ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(left, y(v)); ctx.lineTo(right, y(v)); ctx.stroke();
      ctx.fillStyle = style.getPropertyValue('--report-muted').trim(); ctx.textAlign = 'right'; ctx.fillText(v >= 1000 ? (v / 1000).toFixed(1) + 'k' : v.toFixed(1), left - 7, y(v) + 3);
    }
    for (const [key, , color] of [...series].sort((a, b) => Number(b[0].endsWith('_peak')) - Number(a[0].endsWith('_peak')))) {
      ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 1.7; ctx.beginPath(); let previous = null;
      ctx.setLineDash(key.endsWith('_peak') ? [4, 3] : []); ctx.globalAlpha = key.endsWith('_peak') ? .7 : 1;
      for (const point of points) {
        if (!Number.isFinite(point[key])) { previous = null; continue; }
        const plottedX = Math.max(left, Math.min(right, x(point))), plottedY = y(reading(point, key));
        if (previous && point.first - previous.last <= data.interval_seconds * 2) ctx.lineTo(plottedX, plottedY); else ctx.moveTo(plottedX, plottedY);
        previous = point;
      }
      ctx.stroke();
      if (points.length <= 4) for (const point of points) if (Number.isFinite(point[key])) { ctx.beginPath(); ctx.arc(Math.max(left, Math.min(right, x(point))), y(reading(point, key)), 3, 0, Math.PI * 2); ctx.fill(); }
    }
    ctx.setLineDash([]); ctx.globalAlpha = 1;
    ctx.fillStyle = style.getPropertyValue('--report-muted').trim();
    const labels = Math.min(rect.width < 420 ? 2 : 3, points.length);
    for (let i = 0; i < labels; i++) {
      const index = points.length === 1 ? 0 : Math.round(i * (points.length - 1) / (labels - 1));
      ctx.textAlign = index === 0 ? 'left' : index === points.length - 1 ? 'right' : 'center';
      ctx.fillText(new Date(points[index].first * 1000).toISOString().slice(5, 16).replace('T', ' '), Math.max(left, Math.min(right, x(points[index]))), bottom + 22);
    }
  }
  const plots = [];
  for (const [title, unit, series] of chartSpecs) {
    const section = document.createElement('div'), h2 = document.createElement('h2'), legend = document.createElement('p'), plot = document.createElement('div'), canvas = document.createElement('canvas'), empty = document.createElement('p');
    h2.textContent = title + (unit ? ' · ' + unit : ''); legend.className = 'legend';
    for (const [, label, color] of series) { const span = document.createElement('span'); span.textContent = label; span.style.color = color; legend.append(span); }
    plot.className = 'plot'; canvas.setAttribute('role', 'img'); canvas.setAttribute('aria-label', title + '趋势');
    empty.className = 'plot-empty'; empty.textContent = '此范围没有已记录的有效数据'; plot.append(canvas, empty); section.append(h2, legend, plot); $('charts').append(section); plots.push([canvas, empty, unit, series]);
    canvas.addEventListener('click', event => {
      const range = canvas.sceneRange;
      if (!range || !data.contexts?.length) return;
      const position = Math.max(0, Math.min(1, (event.clientX - canvas.getBoundingClientRect().left - range.left) / (range.right - range.left)));
      const at = range.first + position * (range.last - range.first);
      const closest = data.contexts.reduce((a, b) => Math.abs(a.at - at) <= Math.abs(b.at - at) ? a : b);
      if (Math.abs(closest.at - at) > 35) {
        $('sceneEmpty').textContent = '该时间点附近没有保存进程现场，无法还原当时的进程。';
        $('sceneSection').scrollIntoView({behavior:'smooth', block:'start'}); return;
      }
      $('sceneAbnormal').checked = false;
      scenePage = Math.floor(data.contexts.indexOf(closest) / scenePageSize); renderScenes();
      openScene(closest, sceneTargets.get(sceneKey(closest)));
      $('sceneSection').scrollIntoView({behavior:'smooth', block:'start'});
    });
  }
  const redraw = () => plots.forEach(args => draw(...args));
  new ResizeObserver(() => { cancelAnimationFrame(animation); animationProgress = 1; redraw(); }).observe($('charts')); redraw();
  function renderSummary() {
  $('summary').replaceChildren();
  for (const [key, label, unit] of data.metrics) {
    const s = stat(key), row = document.createElement('tr');
    for (const text of [label, unit || '数值', ...['average', 'minimum', 'peak', 'latest'].map(field => format(s[field])), format(s.count)]) { const td = document.createElement('td'); td.textContent = text; row.append(td); }
    $('summary').append(row);
  }
  $('hardware').textContent = `CPU：${meta.cpu_model || '未记录'}；内存 ${format(meta.memory_total_mb, 'MiB')}；Swap ${format(meta.swap_total_mb, 'MiB')}；磁盘 ${meta.disk_path || '/'} 共 ${format(meta.disk_total_gb, 'GiB')}。`;
  $('generated').textContent = (data.live_detail ? '更新时间：' : '导出时间：') + data.generated_at + '；所有时间为 UTC。';
  }
  renderSummary();
  const sceneKey = scene => `${scene.run}:${scene.ts}`, scenePageSize = 20, sceneTargets = new Map(), waiting = new Map();
  const cachedScenes = new Map((data.context_details || []).map(scene => [sceneKey(scene), scene]));
  let scenePage = 0, sequence = 0;
  $('contextNotice').textContent = data.context_notice || '之前的性能记录未包含进程现场，升级后开始积累。';
  function cell(row, text) { const td = document.createElement('td'); td.textContent = text; row.append(td); return td; }
  function sceneDetail(target, scene) {
    const expanded = new Set([...target.querySelectorAll('details[open]')].map(node => node.dataset.processIdentity));
    target.replaceChildren();
    target.dataset.sceneAt = String(scene.at);
    const description = document.createElement('p');
    description.textContent = `${scene.reasons.join('；')} · CPU ${format(scene.metrics.cpu_percent, '%')} · 内存 ${format(scene.metrics.memory_percent, '%')} · 负载 ${format(scene.metrics.load_one)} · I/O wait ${format(scene.metrics.iowait_percent, '%')} · 磁盘忙碌 ${format(scene.metrics.disk_busy_percent, '%')} · 接收 ${format(scene.metrics.network_rx_kib_s, 'KiB/s')} / 发送 ${format(scene.metrics.network_tx_kib_s, 'KiB/s')}`;
    const coverage = document.createElement('p'); coverage.className = 'caption';
    coverage.textContent = `实际现场 ${new Date(scene.at * 1000).toISOString()}${scene.server_sampled_at ? ' · 整机采样 ' + new Date(scene.server_sampled_at * 1000).toISOString() : ''} · 读取 ${scene.readable ?? 0}/${scene.scanned ?? 0} 个进程 · D 状态 ${scene.blocked_count ?? 0} · 僵尸 ${scene.zombie_count ?? 0} · 首次基线 ${scene.new_baselines ?? 0} 个 · 扫描 ${format(scene.scan_ms, 'ms')}${scene.container_sampled_at ? ' · 容器状态缓存时间 ' + new Date(scene.container_sampled_at * 1000).toISOString() : ' · 容器名称/状态缓存未提供，已识别的容器 ID 仍保留'}`;
    target.append(description, coverage);
    for (const warning of scene.warnings || []) { const p = document.createElement('p'); p.textContent = warning; target.append(p); }
    if (!scene.processes.length) { const p = document.createElement('p'); p.textContent = '该现场未取得有效的进程排名。'; target.append(p); return; }
    const scroll = document.createElement('div'), table = document.createElement('table'), head = document.createElement('thead'), header = document.createElement('tr'), body = document.createElement('tbody'); scroll.className = 'table-scroll';
    for (const title of ['进程 / PID', 'CPU 单核 / 整机', 'RSS / 内存占比', '磁盘读取', '磁盘写入', '服务 / 容器']) { const th = document.createElement('th'); th.textContent = title; header.append(th); }
    head.append(header);
    const ordered = [...scene.processes].sort((a, b) => (b.cpu_percent || 0) - (a.cpu_percent || 0));
    for (const p of ordered) {
      const row = document.createElement('tr'), td = document.createElement('td'), details = document.createElement('details'), summary = document.createElement('summary'), note = document.createElement('p');
      details.dataset.processIdentity = `${p.pid}:${p.start_ticks}`; details.open = expanded.has(details.dataset.processIdentity);
      summary.textContent = `${p.name} · ${p.pid}`;
      note.textContent = `用户 ${p.user} (UID ${p.uid ?? '未知'})；父进程 ${p.ppid}；状态 ${p.state}；线程 ${p.threads}；计数窗口 ${format(p.interval_seconds, 's')}；启动标识 ${p.start_ticks}；可执行文件 ${p.executable || '未读取'}${p.container_id ? '；容器 ID ' + p.container_id : ''}${p.container_state ? '；容器状态 ' + p.container_state : ''}${p.container_health ? '；健康 ' + p.container_health : ''}${p.metadata_unavailable ? '；进程已退出或部分元数据不可读' : ''}`;
      details.append(summary, note); td.append(details); row.append(td);
      cell(row, `${format(p.cpu_percent, '%')} / ${format(p.host_cpu_percent, '%')} · 变化 ${format(p.cpu_change, '百分点')}`);
      cell(row, `${format(p.rss_mib, 'MiB')} / ${format(p.memory_percent, '%')} · 变化 ${format(p.rss_change_mib, 'MiB')}`);
      cell(row, format(p.read_kib_s, 'KiB/s')); cell(row, format(p.write_kib_s, 'KiB/s'));
      cell(row, p.container || p.service || (p.container_id ? '容器 ' + p.container_id.slice(0, 12) : '主机进程'));
      body.append(row);
    }
    table.append(head, body); scroll.append(table); target.append(scroll);
    const note = document.createElement('p'); note.className = 'caption'; note.textContent = '展示 CPU、RSS、读取、写入及 CPU/内存增加各前 5 名的并集；变化与该进程上次有效计数比较。进程名可展开查看用户、状态和路径。R 正在运行，S 休眠，D 不可中断等待，Z 僵尸。尚无上次计数时速率显示未记录。'; target.append(note);
  }
  function contextError(target, text, keep) {
    if (keep && target.cell.childElementCount) {
      let note = target.cell.querySelector('[data-context-feedback]');
      if (!note) { note = document.createElement('p'); note.className = 'caption'; note.dataset.contextFeedback = ''; target.cell.append(note); }
      note.textContent = text;
    } else target.cell.textContent = text;
  }
  function openScene(scene, target, keep = false) {
    if (!target) return;
    target.row.hidden = false; target.button.textContent = '收起'; target.button.setAttribute('aria-expanded', 'true');
    const cached = cachedScenes.get(sceneKey(scene));
    if (cached) { sceneDetail(target.cell, cached); return; }
    if (!data.live_detail || window.parent === window) { target.cell.textContent = '离线报告内嵌最近及高压力现场最多 240 组；该现场的完整进程信息请查看同目录 processes.csv。'; return; }
    const token = 'scene-' + (++sequence); waiting.set(token, {target, key:sceneKey(scene), keep});
    if (!keep || !target.cell.childElementCount) target.cell.textContent = '读取当时保存的进程现场…';
    window.parent.postMessage({type:'performance-context-request', run:scene.run, ts:scene.ts, token}, '*');
    setTimeout(() => { if (waiting.has(token)) { waiting.delete(token); contextError(target, '现场读取超时，收起后可重新展开。', keep); } }, 15000);
  }
  window.addEventListener('message', event => {
    if (event.source !== window.parent || event.data?.type !== 'performance-context-response') return;
    const pending = waiting.get(event.data.token); if (!pending) return; waiting.delete(event.data.token);
    if (event.data.scene && sceneKey(event.data.scene) === pending.key) { cachedScenes.set(pending.key, event.data.scene); sceneDetail(pending.target.cell, event.data.scene); }
    else contextError(pending.target, event.data.error || '该时间点没有保存进程现场。', pending.keep);
  });
  function renderScenes(preserveOpen = false) {
    const scenes = (data.contexts || []).filter(scene => !$('sceneAbnormal').checked || scene.reasons.some(reason => reason !== '定时现场'));
    const pages = Math.max(1, Math.ceil(scenes.length / scenePageSize)); scenePage = Math.min(scenePage, pages - 1);
    const oldTargets = new Map(sceneTargets), nodes = [], focused = document.activeElement;
    sceneTargets.clear();
    $('sceneEmpty').textContent = scenes.length ? `${scenes.length} 个已保留现场` : '当前范围没有进程现场。升级后自动积累；旧的性能采样无法补回当时的进程。';
    $('scenePage').textContent = `${scenePage + 1} / ${pages}`;
    $('scenePrevious').disabled = scenePage === 0; $('sceneNext').disabled = scenePage >= pages - 1;
    for (const scene of scenes.slice(scenePage * scenePageSize, (scenePage + 1) * scenePageSize)) {
      let target = oldTargets.get(sceneKey(scene));
      if (!target) {
        const row = document.createElement('tr'), detailCell = document.createElement('td'), button = document.createElement('button');
        row.className = 'scene-detail'; row.hidden = true; detailCell.colSpan = 6; row.append(detailCell);
        target = {head:document.createElement('tr'), row, cell:detailCell, button};
        button.type = 'button'; button.textContent = '展开'; button.setAttribute('aria-expanded', 'false');
        button.addEventListener('click', () => { if (target.row.hidden) openScene(target.scene, target); else { target.row.hidden = true; button.textContent = '展开'; button.setAttribute('aria-expanded', 'false'); } });
      }
      const row = target.head; row.replaceChildren(); target.scene = scene;
      const time = cell(row, scene.at_text.replace('T', ' ').replace('Z', ''));
      const mobileReason = document.createElement('small'); mobileReason.className = 'scene-mobile-context'; mobileReason.textContent = scene.reasons.join('；'); time.append(mobileReason);
      cell(row, scene.reasons.join('；'));
      for (const [key, unit] of [['cpu', '%'], ['memory', 'MiB'], ['write', 'KiB/s']]) cell(row, scene[key + '_name'] ? `${scene[key + '_name']} (${scene[key + '_pid']}) · ${format(scene[key + '_value'], unit)}` : '未记录');
      const command = document.createElement('td'); command.append(target.button); row.append(command);
      sceneTargets.set(sceneKey(scene), target); nodes.push(row, target.row);
      if (!target.row.hidden && target.cell.dataset.sceneAt !== String(scene.at)) openScene(scene, target, true);
    }
    let held = 0;
    if (preserveOpen) for (const [key, target] of oldTargets) {
      if (target.row.hidden || sceneTargets.has(key)) continue;
      let note = target.head.querySelector('[data-held-scene]');
      if (!note) { note = document.createElement('small'); note.dataset.heldScene = ''; target.head.cells[0].append(note); }
      note.textContent = '保留当前展开的现场';
      sceneTargets.set(key, target); nodes.push(target.head, target.row); held++;
    }
    if (held) $('sceneEmpty').textContent += ` · 另保留 ${held} 个当前展开现场`;
    $('sceneRows').replaceChildren(...nodes);
    if (focused?.isConnected && document.activeElement !== focused) focused.focus({preventScroll:true});
  }
  $('sceneAbnormal').addEventListener('change', () => { scenePage = 0; renderScenes(); });
  $('scenePrevious').addEventListener('click', () => { scenePage--; renderScenes(); });
  $('sceneNext').addEventListener('click', () => { scenePage++; renderScenes(); });
  renderScenes();
  function applyUpdate(next) {
    if (!next || next.period !== data.period || !Array.isArray(next.points) || !Array.isArray(next.metrics)) return;
    const scroll = {x:window.scrollX, y:window.scrollY};
    const viewed = [...sceneTargets.values()].find(target => !target.row.hidden && target.row.getBoundingClientRect().top < innerHeight && target.row.getBoundingClientRect().bottom > 0);
    const anchor = viewed ? {key:sceneKey(viewed.scene), top:viewed.row.getBoundingClientRect().top} : null;
    const oldFirst = [...sceneTargets.keys()][0];
    const index = new Map((next.contexts || []).map(scene => [sceneKey(scene), scene]));
    for (const [key, cached] of cachedScenes) if (!index.has(key) || index.get(key).at !== cached.at) cachedScenes.delete(key);
    data = {...next, live_detail:data.live_detail, package:data.package};
    if (scenePage > 0 && oldFirst) {
      const filtered = (data.contexts || []).filter(scene => !$('sceneAbnormal').checked || scene.reasons.some(reason => reason !== '定时现场'));
      const position = filtered.findIndex(scene => sceneKey(scene) === oldFirst);
      if (position >= 0) scenePage = Math.floor(position / scenePageSize);
    }
    if (anchor) {
      const filtered = (data.contexts || []).filter(scene => !$('sceneAbnormal').checked || scene.reasons.some(reason => reason !== '定时现场'));
      const position = filtered.findIndex(scene => sceneKey(scene) === anchor.key);
      if (position >= 0 && (position < scenePage * scenePageSize || position >= (scenePage + 1) * scenePageSize)) scenePage = Math.floor(position / scenePageSize);
    }
    while (cachedScenes.size > 512) {
      const removable = [...cachedScenes.keys()].find(key => !sceneTargets.has(key));
      if (!removable) break; cachedScenes.delete(removable);
    }
    renderOverview(); renderSummary(); renderScenes(true);
    const currentAnchor = anchor && sceneTargets.get(anchor.key);
    window.scrollTo(scroll.x, currentAnchor ? scroll.y + currentAnchor.row.getBoundingClientRect().top - anchor.top : scroll.y);
    cancelAnimationFrame(animation);
    for (const [canvas] of plots) canvas.motionFrom = canvas.lastPlot;
    if (matchMedia('(prefers-reduced-motion: reduce)').matches || document.hidden) { animationProgress = 1; redraw(); return; }
    const started = performance.now(); animationProgress = 0;
    const step = now => {
      const progress = document.hidden || matchMedia('(prefers-reduced-motion: reduce)').matches ? 1 : Math.min(1, (now - started) / 500);
      animationProgress = progress * progress * (3 - 2 * progress); redraw();
      if (progress < 1) animation = requestAnimationFrame(step);
    };
    animation = requestAnimationFrame(step);
  }
  window.addEventListener('message', event => {
    if (!data.live_detail || event.source !== window.parent || event.data?.type !== 'performance-report-update') return;
    applyUpdate(event.data.data);
  });
  if (data.live_detail && window.parent !== window) window.parent.postMessage({type:'performance-report-ready', period:data.period}, '*');
  $('print').addEventListener('click', () => window.print());
  window.addEventListener('beforeprint', () => { cancelAnimationFrame(animation); animationProgress = 1; redraw(); });
})();
