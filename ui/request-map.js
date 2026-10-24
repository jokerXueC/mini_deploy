window.RequestMap = (() => {
  'use strict';
  const el = id => document.getElementById(id);
  const text = value => escapeHtml(String(value ?? ''));
  const date = value => value ? new Date(value * 1000).toLocaleString('zh-CN', {hour12:false}) : '时间未采集';
  let groups = new Map(), selected = null, data = null, land = null, scope = '';

  async function loadLand() {
    if (land) return land;
    land = (async () => {
      try {
        const response = await fetch('ui/world-land.geojson', {credentials:'same-origin'});
        if (!response.ok) throw new Error('底图加载失败');
        const source = await response.json(), paths = [];
        for (const feature of source.features) {
          const polygons = feature.geometry.type === 'MultiPolygon' ? feature.geometry.coordinates : [feature.geometry.coordinates];
          for (const polygon of polygons) paths.push('<path d="' + polygon.map(ring => ring.map(([lon,lat],i) =>
            (i ? 'L' : 'M') + ((lon + 180) / 360 * 1000).toFixed(2) + ',' + ((90 - lat) / 180 * 500).toFixed(2)).join('') + 'Z').join('') + '"/>');
        }
        el('requestMapLand').innerHTML = paths.join('');
      } catch (_) {
        land = null;
        el('requestMapNote').textContent += ' · 地图底图暂不可用，来源明细仍可查看';
      }
    })();
    return land;
  }

  function detail() {
    const group = selected === 'all' ? {label:'全部来源', members:data?.items || []} : groups.get(selected);
    el('requestMapDetail').hidden = !group;
    el('requestMapRegions').hidden = !!group;
    for (const point of el('requestMapDots').children) point.setAttribute('aria-pressed', String(point.dataset.region === selected));
    if (!group) return;
    el('requestMapDetailTitle').textContent = group.label;
    const body = '<p class="security-map-detail-total">' + group.members.length + ' 个 IP · ' +
      group.members.reduce((sum, item) => sum + item.count, 0) + ' 次请求</p><div class="security-map-ip-list">' +
      group.members.map(item => '<div class="security-map-ip"><div><strong>' + text(item.ip) + '</strong><b>' + item.count +
        '<small> 次</small></b></div><p><span>成功 ' + item.successes + '</span><span data-failed="true">错误 ' + item.errors +
        '</span></p><code>' + text(`${item.last_method || ''} ${item.last_host || ''}${item.last_path || ''}`) +
        '</code><footer><time>最近 ' + text(date(item.last_at)) + '</time><button type="button" class="ghost-button compact-action" data-request-ip="' +
        text(item.ip) + '" aria-label="查看 ' + text(item.ip) + ' 的请求">查看请求</button></footer></div>').join('') + '</div>';
    if (el('requestMapDetailBody').innerHTML !== body) el('requestMapDetailBody').innerHTML = body;
  }

  function select(key) { selected = key; detail(); }
  el('requestMapDots').addEventListener('click', event => {
    const point = event.target.closest('[data-region]'); if (point) select(point.dataset.region);
  });
  el('requestMapDots').addEventListener('keydown', event => {
    const point = event.target.closest('[data-region]');
    if (point && ['Enter', ' '].includes(event.key)) { event.preventDefault(); select(point.dataset.region); }
  });
  el('requestMapRegions').addEventListener('click', event => {
    const button = event.target.closest('[data-region]'); if (button) select(button.dataset.region);
  });
  el('requestMapDetailClose').onclick = () => {
    const previous = selected;
    select(null);
    ([...el('requestMapDots').children].find(node => node.dataset.region === previous) ||
      el('requestMapRegions').querySelector('button'))?.focus({preventScroll:true});
  };
  el('requestMapDetailBody').addEventListener('click', event => {
    const button = event.target.closest('[data-request-ip]');
    if (button) { el('requestSearch').value = button.dataset.requestIp; el('requestSearch').dispatchEvent(new Event('input', {bubbles:true})); }
  });

  function sample(records, known) {
    const origins = new Map();
    for (const item of known?.items || []) for (const ip of [item.ip, ...(item.aliases || [])]) origins.set(ip, item);
    const items = new Map();
    let missing = 0;
    for (const record of records) {
      const origin = origins.get(record.client_ip);
      if (!origin) { missing++; continue; }
      const item = items.get(origin.ip) || {...origin, count:0, successes:0, errors:0, last_at:0};
      item.count++; item.successes += Number(record.status >= 200 && record.status < 400); item.errors += Number(record.status >= 400);
      const at = record.ts ?? Date.parse(record.at) / 1000;
      if (at >= item.last_at) Object.assign(item, {last_at:at, last_host:record.host, last_path:record.path, last_method:record.method});
      items.set(item.ip, item);
    }
    return {...known, items:[...items.values()].sort((a,b) => b.count - a.count), total_requests:records.length,
      total_ips:items.size, missing_ip_requests:missing, mode:'sample'};
  }

  function render(value, options = {}) {
    if (options.scope !== scope) { selected = null; scope = options.scope; }
    data = options.records ? sample(options.records, value) : value;
    const items = data?.items || [];
    groups = new Map();
    let positioned = 0;
    for (const item of items) {
      const location = item.location || {label:'待定位'};
      const located = Number.isFinite(location.latitude) && Number.isFinite(location.longitude);
      positioned += Number(located);
      const key = located ? `${location.latitude}:${location.longitude}` : `label:${location.label}`;
      const group = groups.get(key) || {...location, count:0, errors:0, members:[]};
      group.count += item.count; group.errors += item.errors; group.members.push(item); groups.set(key, group);
    }
    if (selected !== 'all' && !groups.has(selected)) selected = null;
    const points = new Map([...el('requestMapDots').children].map(node => [node.dataset.region,node]));
    for (const [key,group] of groups) {
      if (!Number.isFinite(group.latitude) || !Number.isFinite(group.longitude)) continue;
      let point = points.get(key);
      if (!point) {
        point = document.createElementNS('http://www.w3.org/2000/svg', 'g');
        point.dataset.region = key; point.classList.add('security-map-point');
        point.setAttribute('role', 'button'); point.setAttribute('tabindex', '0'); point.setAttribute('aria-controls', 'requestMapDetail');
        point.innerHTML = '<title></title><rect x="-16" y="-16" width="32" height="32" fill="transparent"/><circle class="security-map-dot"/>';
        el('requestMapDots').append(point);
      }
      const label = `${group.label} · ${group.members.length} 个 IP · ${group.count} 次请求 · ${group.errors} 次错误`;
      point.setAttribute('aria-label', label); point.querySelector('title').textContent = label;
      point.setAttribute('transform', `translate(${((group.longitude + 180) / 360 * 1000).toFixed(2)},${((90 - group.latitude) / 180 * 500).toFixed(2)})`);
      point.querySelector('circle').setAttribute('r', Math.min(8, 3 + Math.log2(group.count + 1)).toFixed(1));
      point.querySelector('circle').dataset.failed = String(group.errors > 0);
    }
    for (const [key,point] of points) if (!groups.has(key)) point.remove();
    el('requestMapRegions').innerHTML = [...groups].sort((a,b) => b[1].count - a[1].count).slice(0,5).map(([key,group]) =>
      '<button type="button" data-region="' + text(key) + '"><span>' + text(group.label) + '<small>' +
      group.members.length + ' 个 IP · 错误 ' + group.errors + '</small></span><strong>' + group.count + '</strong></button>').join('') +
      (items.length ? '<button type="button" data-region="all"><span>全部来源<small>' + data.total_ips + ' 个 IP</small></span><span>查看</span></button>' : '');
    el('requestMapEmpty').hidden = positioned > 0;
    el('requestMapEmpty').textContent = items.length ? '暂无可定位的公网 IP，来源明细仍可查看' : '暂无来源 IP';
    const period = data?.mode === 'history' ? ({'1h':'最近 1 小时','24h':'最近 24 小时','7d':'最近 7 天'}[el('requestPeriod').value] || '所选时段') : '当前日志样本';
    el('requestMapNote').textContent = data?.error || (!data ? '等待地区统计数据' :
      `${period} · ${data.total_requests} 条明细 · ${data.total_ips} 个 IP · ${positioned}/${items.length} 个已定位` +
      (data.missing_ip_requests ? ` · ${data.missing_ip_requests} 条未采集 IP` : '') +
      (data.limited ? ' · 显示请求最多的 500 个 IP' : '') + (data.mode === 'history' ? ' · 按单条明细筛选，过期记录不补造' : ''));
    detail();
    if (el('requestsView').classList.contains('active')) loadLand();
  }
  return {render, error(message) { el('requestMapNote').textContent = `来源统计未更新：${message}；保留上次结果。`; }};
})();
