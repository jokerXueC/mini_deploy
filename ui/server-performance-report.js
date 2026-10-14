(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const data = JSON.parse(new TextDecoder().decode(Uint8Array.from(atob($('reportData').textContent), c => c.charCodeAt(0))));
  const meta = data.metadata || {}, format = (value, unit = '') => Number.isFinite(value) ? `${value.toLocaleString('zh-CN', {maximumFractionDigits:2})}${unit ? ' ' + unit : ''}` : '未记录';
  const stat = key => data.summary[key] || {};
  $('server').textContent = `${meta.hostname || '服务器'} · ${meta.logical_cores ? meta.logical_cores + ' 个逻辑核' : '核心数未记录'} · ${format(meta.memory_total_mb == null ? null : meta.memory_total_mb / 1024, 'GiB 内存')}`;
  $('range').textContent = `${data.since} 至 ${data.until}（UTC）`;
  $('coverage').textContent = `覆盖率 ${format(data.coverage_percent, '%')} · ${format(data.coverage_seconds)} 个不同采样秒 · ${format(data.points.length)} 组统计 · ${data.session_count} 个采样批次${data.first_at ? ` · 实际记录 ${data.first_at} 至 ${data.latest_at}` : ' · 等待首次采样，通常在 30 秒内开始显示'}`;
  $('notice').textContent = data.notice + ' 覆盖率按有有效指标的采样秒计算，各指标完整程度见下方有效采样数。';
  for (const text of [data.error, data.stale ? '最近采样已超过 65 秒，采集可能中断。' : '', data.coverage_percent < 95 ? '所选时间范围的数据尚未完整覆盖；升级前、停机时或采集失败的时间无法补回。' : ''].filter(Boolean)) {
    const p = document.createElement('p'); p.textContent = text; $('warnings').append(p);
  }
  $('downloads').hidden = !data.package;
  for (const [title, value, note] of [
    ['CPU 平均', format(stat('cpu_percent').average, '%'), '峰值 ' + format(stat('cpu_percent').peak, '%')],
    ['内存平均', format(stat('memory_percent').average, '%'), '峰值 ' + format(stat('memory_percent').peak, '%')],
    ['根分区峰值', format(stat('disk_percent').peak, '%'), '最后 ' + format(stat('disk_percent').latest, '%')],
    ['接收平均', format(stat('network_rx_kib_s').average, 'KiB/s'), '峰值 ' + format(stat('network_rx_kib_s').peak, 'KiB/s')],
    ['发送平均', format(stat('network_tx_kib_s').average, 'KiB/s'), '峰值 ' + format(stat('network_tx_kib_s').peak, 'KiB/s')],
    ['CPU ≥ 85%', format(stat('cpu_percent').high == null ? null : stat('cpu_percent').high / 60, '分钟'), '按实际 1 秒采样累计']]) {
    const metric = document.createElement('div'); metric.className = 'metric';
    for (const [tag, text] of [['span', title], ['strong', value], ['small', note]]) { const element = document.createElement(tag); element.textContent = text; metric.append(element); }
    $('totals').append(metric);
  }
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
  function draw(canvas, empty, unit, series) {
    const rect = canvas.getBoundingClientRect(), ctx = canvas.getContext('2d'), ratio = devicePixelRatio || 1;
    canvas.width = Math.round(rect.width * ratio); canvas.height = Math.round(rect.height * ratio); ctx.scale(ratio, ratio);
    const points = chartPoints(series, Math.max(80, Math.floor(rect.width / 3))); let maximum = unit === '%' ? 100 : 1, valid = false;
    for (const p of points) for (const [key] of series) if (Number.isFinite(p[key])) { maximum = Math.max(maximum, p[key]); valid = true; }
    empty.hidden = valid;
    if (!valid || rect.width < 1) return;
    const first = points[0].first, last = points.at(-1).first, left = 50, right = rect.width - 15, top = 10, bottom = rect.height - 32;
    const x = p => left + (right - left) * (last === first ? .5 : (p.first - first) / (last - first));
    const scale = unit === '%' ? maximum : maximum * 1.08;
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
        if (previous && point.first - previous.last <= data.interval_seconds * 2) ctx.lineTo(x(point), y(point[key])); else ctx.moveTo(x(point), y(point[key]));
        previous = point;
      }
      ctx.stroke();
      if (points.length <= 4) for (const point of points) if (Number.isFinite(point[key])) { ctx.beginPath(); ctx.arc(x(point), y(point[key]), 3, 0, Math.PI * 2); ctx.fill(); }
    }
    ctx.setLineDash([]); ctx.globalAlpha = 1;
    ctx.fillStyle = style.getPropertyValue('--report-muted').trim();
    const labels = Math.min(rect.width < 420 ? 2 : 3, points.length);
    for (let i = 0; i < labels; i++) {
      const index = points.length === 1 ? 0 : Math.round(i * (points.length - 1) / (labels - 1));
      ctx.textAlign = index === 0 ? 'left' : index === points.length - 1 ? 'right' : 'center';
      ctx.fillText(new Date(points[index].first * 1000).toISOString().slice(5, 16).replace('T', ' '), x(points[index]), bottom + 22);
    }
  }
  const plots = [];
  for (const [title, unit, series] of chartSpecs) {
    const section = document.createElement('div'), h2 = document.createElement('h2'), legend = document.createElement('p'), plot = document.createElement('div'), canvas = document.createElement('canvas'), empty = document.createElement('p');
    h2.textContent = title + (unit ? ' · ' + unit : ''); legend.className = 'legend';
    for (const [, label, color] of series) { const span = document.createElement('span'); span.textContent = label; span.style.color = color; legend.append(span); }
    plot.className = 'plot'; canvas.setAttribute('role', 'img'); canvas.setAttribute('aria-label', title + '趋势');
    empty.className = 'plot-empty'; empty.textContent = '此范围没有已记录的有效数据'; plot.append(canvas, empty); section.append(h2, legend, plot); $('charts').append(section); plots.push([canvas, empty, unit, series]);
  }
  const redraw = () => plots.forEach(args => draw(...args)); new ResizeObserver(redraw).observe($('charts')); redraw();
  for (const [key, label, unit] of data.metrics) {
    const s = stat(key), row = document.createElement('tr');
    for (const text of [label, unit || '数值', ...['average', 'minimum', 'peak', 'latest'].map(field => format(s[field])), format(s.count)]) { const td = document.createElement('td'); td.textContent = text; row.append(td); }
    $('summary').append(row);
  }
  $('hardware').textContent = `CPU：${meta.cpu_model || '未记录'}；内存 ${format(meta.memory_total_mb, 'MiB')}；Swap ${format(meta.swap_total_mb, 'MiB')}；磁盘 ${meta.disk_path || '/'} 共 ${format(meta.disk_total_gb, 'GiB')}。`;
  $('generated').textContent = '导出时间：' + data.generated_at + '；所有时间为 UTC。';
  $('print').addEventListener('click', () => window.print()); window.addEventListener('beforeprint', redraw);
})();
