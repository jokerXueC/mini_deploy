(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const data = JSON.parse(new TextDecoder().decode(Uint8Array.from(atob($('reportData').textContent), c => c.charCodeAt(0))));
  const count = value => value == null ? '未记录' : value.toLocaleString('zh-CN');
  const ms = value => value == null ? '未记录' : value > 1000 ? `${(value / 1000).toFixed(2)} s` : `${value.toFixed(2)} ms`;
  const percent = value => value == null ? '未记录' : `${value.toFixed(2)}%`;
  $('scope').textContent = `${data.scope} · ${data.kind === 'sample' ? '近期日志样本' : '已保存历史'}`;
  $('range').textContent = data.since ? `${data.since} 至 ${data.until}（UTC）` : '暂无有效请求时间';
  const filter = data.filters || {}, labels = {all: '全部地址', success: '全部成功', error: '含失败请求', slow: '含慢请求'};
  $('notice').textContent = `${data.notice} ${labels[filter.status] || ''}${filter.search ? ' · 查找：' + filter.search : ''}`;
  for (const text of data.warnings || []) { const p = document.createElement('p'); p.textContent = text; $('warnings').append(p); }
  $('downloads').hidden = !data.package;
  $('threshold').textContent = ms(filter.slow_ms || 1000);
  $('generated').textContent = `导出时间：${data.generated_at} · 共 ${count(data.count)} 条原始记录 · ${count(data.groups.length)} 类地址`;
  const totals = data.totals;
  for (const [label, value, note] of [
    ['请求总量', count(totals.count), `${count(data.groups.length)} 类地址`],
    ['成功率', percent(totals.success_rate), `失败 ${count(totals.errors)} 次`],
    ['平均耗时', ms(totals.average), `${count(totals.duration_count)} 条有效耗时`],
    ['P95 / P99', ms(totals.p95), `P99 ${ms(totals.p99)}`],
    ['慢请求', count(totals.slow_count), `阈值 ≥ ${ms(filter.slow_ms || 1000)}`],
    ['来源 IP', count(data.unique_ips), '仅统计已记录 IP']]) {
    const div = document.createElement('div'); div.className = 'metric';
    const span = document.createElement('span'), strong = document.createElement('strong'), small = document.createElement('small');
    span.textContent = label; strong.textContent = value; small.textContent = note; div.append(span, strong, small); $('totals').append(div);
  }
  function bars(target, values, colors = {}) {
    if (!values.length) { target.textContent = '未记录'; return; }
    const max = Math.max(...values.map(([, value]) => value), 1);
    for (const [key, value] of values) {
      const row = document.createElement('div'); row.className = 'bar';
      const label = document.createElement('span'), track = document.createElement('span'), fill = document.createElement('i'), n = document.createElement('span');
      track.className = 'bar-track'; label.textContent = key; n.textContent = count(value); fill.style.width = `${value / max * 100}%`;
      fill.style.background = colors[key] || '#0d8b72'; track.append(fill); row.append(label, track, n); target.append(row);
    }
  }
  bars($('statuses'), Object.entries(data.statuses), Object.fromEntries(Object.keys(data.statuses).map(key => [key, Number(key) >= 400 ? '#cf5060' : Number(key) >= 300 ? '#b68a26' : '#0d8b72'])));
  bars($('methods'), Object.entries(data.methods), {GET: '#397cad', POST: '#0d8b72', DELETE: '#cf5060', PUT: '#b68a26'});
  bars($('ips'), data.ips.map(item => [item.ip, item.count])); $('ipCount').textContent = `${count(data.unique_ips)} 个不同来源 IP`;
  $('bucket').textContent = data.bucket_seconds === 60 ? '每分钟 · UTC' : data.bucket_seconds === 3600 ? '每小时 · UTC' : '每天 · UTC';
  function plot(id, keys, colors) {
    const canvas = $(id), rect = canvas.getBoundingClientRect(), points = data.timeline, ctx = canvas.getContext('2d'), ratio = devicePixelRatio || 1;
    canvas.width = Math.round(rect.width * ratio); canvas.height = Math.round(rect.height * ratio); ctx.scale(ratio, ratio);
    const values = points.flatMap(p => keys.map(key => p[key])).filter(Number.isFinite);
    $(id + 'Empty').hidden = !!values.length;
    if (!values.length || rect.width < 1) return;
    const left = 55, right = rect.width - 20, top = 12, bottom = rect.height - 32;
    const max = Math.max(...values, 1) * 1.12, x = i => left + (right - left) * (points.length === 1 ? .5 : i / (points.length - 1)), y = v => bottom - (bottom - top) * v / max;
    ctx.font = '11px "PingFang SC", "Microsoft YaHei", sans-serif';
    for (let i = 0; i <= 4; i++) {
      const v = max * i / 4; ctx.strokeStyle = '#e3e7eb'; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(left, y(v)); ctx.lineTo(right, y(v)); ctx.stroke();
      ctx.fillStyle = '#687580'; ctx.textAlign = 'right'; ctx.fillText(v >= 1000 ? (v / 1000).toFixed(1) + 'k' : v.toFixed(0), left - 8, y(v) + 4);
    }
    keys.forEach((key, index) => {
      ctx.strokeStyle = colors[index]; ctx.fillStyle = colors[index]; ctx.lineWidth = 2; ctx.beginPath(); let connected = false;
      points.forEach((point, i) => {
        if (!Number.isFinite(point[key])) { connected = false; return; }
        if (connected) ctx.lineTo(x(i), y(point[key])); else ctx.moveTo(x(i), y(point[key])); connected = true;
      }); ctx.stroke();
      if (points.length <= 6) points.forEach((point, i) => { if (Number.isFinite(point[key])) { ctx.beginPath(); ctx.arc(x(i), y(point[key]), 3, 0, Math.PI * 2); ctx.fill(); } });
    });
    const labels = rect.width < 500 ? 3 : 4;
    for (let i = 0; i < Math.min(labels, points.length); i++) {
      const index = points.length === 1 ? 0 : Math.round(i * (points.length - 1) / (Math.min(labels, points.length) - 1));
      ctx.fillStyle = '#687580'; ctx.textAlign = index === 0 ? 'left' : index === points.length - 1 ? 'right' : 'center';
      ctx.fillText(points[index].at.slice(5, 16).replace('T', ' '), x(index), bottom + 22);
    }
  }
  const draw = () => { plot('volume', ['count', 'errors'], ['#0d8b72', '#cf5060']); plot('latency', ['average', 'upstream_average'], ['#397cad', '#b68a26']); };
  new ResizeObserver(draw).observe($('volume').parentElement); draw();
  $('print').addEventListener('click', () => window.print()); window.addEventListener('beforeprint', draw);
  let view = 'routes', page = 0, sort = 'count', descending = true;
  const columns = [['source_name', '来源', String], ['host', '域名', String], ['method', '方法', String], ['path', '请求路径', String],
    ['count', '次数', count], ['success_rate', '成功率', percent], ['errors', '失败', count], ['slow_count', '慢请求', count],
    ['average', '平均耗时', ms], ['p95', 'P95', ms], ['p99', 'P99', ms], ['upstream_average', '平均上游', ms]];
  function render() {
    const selected = view === 'routes' ? columns : [['at', '区间起点 (UTC)', String], ...columns.slice(4)];
    const term = view === 'routes' ? $('search').value.trim().toLowerCase() : '';
    const items = (view === 'routes' ? data.groups : data.timeline).filter(item => !term || `${item.source_name} ${item.host} ${item.path}`.toLowerCase().includes(term))
      .slice().sort((a, b) => {
        if (a[sort] == null) return b[sort] == null ? 0 : 1;
        if (b[sort] == null) return -1;
        const result = typeof a[sort] === 'number' ? a[sort] - b[sort] : String(a[sort]).localeCompare(String(b[sort]));
        return descending ? -result : result;
      });
    const head = document.createElement('tr');
    for (const [key, label] of selected) {
      const th = document.createElement('th'), button = document.createElement('button'); button.type = 'button'; button.textContent = label + (key === sort ? descending ? ' ↓' : ' ↑' : '');
      th.setAttribute('aria-sort', key === sort ? descending ? 'descending' : 'ascending' : 'none');
      button.addEventListener('click', () => { descending = key === sort ? !descending : key !== 'at'; sort = key; page = 0; render(); }); th.append(button); head.append(th);
    }
    $('columns').replaceChildren(head); $('table').replaceChildren();
    for (const item of items.slice(page * 50, (page + 1) * 50)) {
      const tr = document.createElement('tr');
      for (const [key, , format] of selected) {
        const td = document.createElement('td'); td.textContent = format(item[key]);
        if (key === 'path') { const small = document.createElement('small'); small.textContent = `${item.path_count} 个原始路径${item.upstream_addresses.length ? ' · ' + item.upstream_addresses.join(', ') : ''}`; td.append(small); }
        tr.append(td);
      }
      $('table').append(tr);
    }
    if (!items.length) { const tr = document.createElement('tr'), td = document.createElement('td'); td.colSpan = selected.length; td.textContent = '暂无匹配数据'; tr.append(td); $('table').append(tr); }
    $('tableCount').textContent = `${count(items.length)} ${view === 'routes' ? '类地址' : '个区间'}`;
    $('pageCount').textContent = `第 ${page + 1} / ${Math.max(1, Math.ceil(items.length / 50))} 页`;
    $('previous').disabled = !page; $('next').disabled = (page + 1) * 50 >= items.length;
    $('routesTab').setAttribute('aria-selected', String(view === 'routes')); $('timelineTab').setAttribute('aria-selected', String(view === 'timeline')); $('search').hidden = view !== 'routes';
  }
  for (const type of ['routes', 'timeline']) $(type + 'Tab').addEventListener('click', () => { view = type; page = 0; sort = type === 'routes' ? 'count' : 'at'; descending = type === 'routes'; render(); });
  $('search').addEventListener('input', () => { page = 0; render(); }); $('previous').addEventListener('click', () => { page--; render(); }); $('next').addEventListener('click', () => { page++; render(); }); render();
})();
