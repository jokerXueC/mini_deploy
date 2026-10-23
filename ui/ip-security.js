(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const text = value => escapeHtml(String(value ?? ''));
  const date = value => new Date(value * 1000).toLocaleString('zh-CN', {hour12:false});
  let status = null, token = '', timer, request, version = 0, view = false, busy = false;
  let page = 0, banPage = 0, error = '', draft = false;
  let mapGroups = new Map(), selectedRegion = null;
  let protection = null, protectionPending = false, protectionError = '', protectionToken = '';
  const feedback = value => { $('ipSecurityFeedback').textContent = value; };
  const minutes = value => value >= 3600 ? (value / 3600) + ' 小时' : (value / 60) + ' 分钟';

  function controls() {
    $('ipSecurityRefresh').disabled = !!request || busy;
    $('securityGuardToggle').disabled = !status || busy || !!request || !!error;
    $('securityPolicyForm').querySelector('button').disabled = !status || busy || !!request || !!error;
    $('securityActivityPrev').disabled = busy || !!request || page === 0;
    $('securityActivityNext').disabled = busy || !!request || !status || (page + 1) * 10 >= status.total;
    $('securityBanPrev').disabled = busy || !!request || banPage === 0;
    $('securityBanNext').disabled = busy || !!request || !status || (banPage + 1) * 10 >= status.ban_total;
    $('securityBans').querySelectorAll('button').forEach(button => { button.disabled = busy || !!error; });
    $('securityBasicToggle').disabled = !protection || protectionPending || protection.busy || !!protectionError;
    $('securityBasicRetry').disabled = !protection || protectionPending || protection.busy || !!protectionError;
  }

  function renderProtection() {
    if (!protection) return;
    const active = protection.enabled;
    $('securityBasicToggle').setAttribute('aria-checked', String(active));
    const names = {active:'已生效',partial:'部分生效',unavailable:'未接入',changed:'需核对',off:'未启用',pending:'检测中'};
    const rows = [];
    const add = (label, item) => rows.push('<div class="security-basic-row" data-state="' + text(item.state) +
      '"><strong>' + text(label) + '</strong><p>' + text(item.detail) + '</p><span>' +
      text(names[item.state] || '待确认') + '</span></div>');
    add('登录防护', {state: status?.settings.enabled ? (status.ssh.ready ? 'active' : 'partial') : 'off', detail: status?.settings.enabled ?
      '面板认证失败自动封禁；SSH 状态：' + status.ssh.detail : '登录继续记录，自动封禁已暂停'});
    add('异常连接', protection.connections || {state:'off',detail:'开启后自动检测 80/443 的 TCP 新连接防护'});
    for (const entry of protection.entries || []) add(entry.label, entry);
    add('下载限速', protection.downloads || {state:'off',detail:'不默认限制正常下载和流式响应'});
    $('securityBasicCoverage').innerHTML = rows.join('');
    const count = (protection.entries || []).filter(item => item.state === 'active').length;
    const incomplete = !status?.settings.enabled || !status?.ssh.ready || protection.connections?.state !== 'active' ||
      (protection.entries || []).some(item => item.state !== 'active') || protection.errors?.length;
    $('securityBasicSummary').textContent = protectionPending || protection.busy ? '正在检测并应用，请稍候…' :
      protection.phase === 'interrupted' ? '上次操作中断，请展开详情重新核对' : active ?
      (incomplete ? '已开启，部分能力未接入' : '基础防护已开启') + ' · ' + count + ' 个网站入口已限流' :
      '开启后自动检测登录、网站入口与主机连接防护';
    $('securityBasicFeedback').textContent = protectionError || (protection.errors || []).join('；');
    $('securityBasicRetry').hidden = !active && !['interrupted','partial'].includes(protection.phase);
    controls();
  }

  async function refreshProtection(signal, current) {
    try {
      const response = await fetch('security/protection', {credentials:'same-origin',cache:'no-store',signal});
      const body = await response.json();
      if (current !== version) return;
      if (!response.ok) throw new Error(body.detail || '基础防护状态读取失败');
      if (!Array.isArray(body.entries) || typeof body.enabled !== 'boolean') throw new Error('基础防护响应不完整，请更新服务端');
      protection = body; protectionToken = body.csrf_token || protectionToken; protectionError = '';
    } catch (err) {
      if (current !== version) return;
      protectionError = err.name === 'AbortError' ? '基础防护状态读取超时，请刷新确认' : err.message;
      $('securityBasicFeedback').textContent = protectionError;
    }
    renderProtection();
    controls();
  }

  async function changeProtection(enabled) {
    if (protectionPending || protection?.busy) return;
    protectionPending = true; protectionError = ''; renderProtection(); controls();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch('security/protection', {method:'POST', credentials:'same-origin',
        headers:{'Content-Type':'application/json','X-CSRF-Token':protectionToken},
        body:JSON.stringify({enabled}), signal:controller.signal});
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || '操作未能提交，请刷新确认');
      protection = body;
    } catch (err) {
      protectionError = (err.name === 'AbortError' ? '提交超时' : err.message) + '；请刷新确认实际状态，不要重复提交。';
    } finally {
      clearTimeout(timeout); protectionPending = false; renderProtection(); controls(); schedule();
    }
  }

  function renderMapDetail() {
    const group = mapGroups.get(selectedRegion);
    $('securityMapDetail').hidden = !group;
    $('securityRegions').hidden = !!group;
    if (!group) return;
    $('securityMapDetailTitle').textContent = group.label || '登录地区';
    const content = '<p class="security-map-detail-total">最近 24 小时 · ' + group.members.length +
      ' 个 IP · ' + group.count + ' 次登录</p><div class="security-map-ip-list">' +
      [...group.members].sort((a,b) => b.count - a.count).map(item =>
        '<div class="security-map-ip"><div><strong>' + text(item.ip) + '</strong><b>' + item.count +
        '<small> 次</small></b></div><p><span>成功 ' + (item.count - item.failures) +
        '</span><span data-failed="true">失败 ' + item.failures + '</span></p>' +
        (item.last_at ? '<time>最近 ' + text(date(item.last_at)) + '</time>' : '') + '</div>').join('') + '</div>';
    if ($('securityMapDetailBody').innerHTML !== content) $('securityMapDetailBody').innerHTML = content;
  }

  function selectRegion(key) {
    selectedRegion = key;
    for (const point of $('securityMapDots').children) {
      point.setAttribute('aria-pressed', String(point.dataset.region === key));
    }
    renderMapDetail();
  }

  function closeRegion() {
    const point = [...$('securityMapDots').children].find(item => item.dataset.region === selectedRegion);
    selectRegion(null);
    point?.focus();
  }

  function renderMap() {
    const locations = status.locations || [];
    const positioned = locations.filter(item => Number.isFinite(item.latitude) && Number.isFinite(item.longitude));
    const grouped = new Map();
    for (const item of positioned) {
      const key = item.latitude + ':' + item.longitude;
      const group = grouped.get(key) || { ...item, count:0, failures:0, members:[] };
      group.count += item.count; group.failures += item.failures; group.members.push(item);
      grouped.set(key, group);
    }
    // Keep point nodes stable so refreshes preserve keyboard focus and selection.
    mapGroups = grouped;
    if (!grouped.has(selectedRegion)) selectedRegion = null;
    const points = new Map([...$('securityMapDots').children].map(node => [node.dataset.region,node]));
    for (const [key,item] of grouped) {
      let point = points.get(key);
      if (!point) {
        point = document.createElementNS('http://www.w3.org/2000/svg', 'g');
        point.dataset.region = key;
        point.setAttribute('role', 'button');
        point.setAttribute('tabindex', '0');
        point.setAttribute('aria-controls', 'securityMapDetail');
        point.classList.add('security-map-point');
        point.innerHTML = '<title></title><rect x="-16" y="-16" width="32" height="32" fill="transparent"/><circle class="security-map-dot"/>';
        $('securityMapDots').append(point);
      }
      point.setAttribute('transform', 'translate(' + ((item.longitude + 180) / 360 * 1000).toFixed(2) +
        ',' + ((90 - item.latitude) / 180 * 500).toFixed(2) + ')');
      const label = (item.label || '登录地区') + ' · ' + item.members.length + ' 个 IP · ' + item.count +
        ' 次登录 · ' + item.failures + ' 次失败';
      point.setAttribute('aria-label', label);
      point.setAttribute('aria-pressed', String(key === selectedRegion));
      point.querySelector('title').textContent = label;
      point.querySelector('circle').setAttribute('r', Math.min(8, 3 + Math.log2(item.count + 1)).toFixed(1));
      point.querySelector('circle').dataset.failed = String(item.failures > 0);
    }
    for (const [key,point] of points) if (!grouped.has(key)) point.remove();
    renderMapDetail();
    $('securityMapEmpty').hidden = positioned.length > 0;
    $('securityMapEmpty').textContent = locations.length ? '暂无可定位的公网 IP' : '最近 24 小时暂无已采集登录';
    $('securityMapNote').textContent = '最近 24 小时 · ' + positioned.length + '/' + locations.length +
      ' 个来源已定位 · IP 近似位置，非设备实时位置';
    const regions = new Map();
    for (const item of locations) {
      const label = item.label || '待定位';
      const group = regions.get(label) || {count:0, ips:0, failures:0};
      group.count += item.count; group.ips++; group.failures += item.failures;
      regions.set(label, group);
    }
    $('securityRegions').innerHTML = [...regions].sort((a,b) => b[1].count - a[1].count).slice(0,5).map(([label,item]) =>
      '<div><span>' + text(label) + '<small>' + item.ips + ' 个 IP · ' + item.failures +
      ' 次失败</small></span><strong>' + item.count + '</strong></div>').join('') || '<p class="security-empty">等待登录来源</p>';
  }

  function render() {
    if (!status) return;
    const settings = status.settings;
    $('securitySummaryNote').textContent = error ? '读取失败：' + error + '。以下保留上次结果。' :
      (status.demo ? '本地模拟预览 · ' : '') + '面板与 SSH 登录 · 最近更新 ' + date(status.checked_at);
    $('securitySuccessCount').textContent = status.success_24h;
    $('securityLoginCount').textContent = status.failure_24h;
    $('securityBanCount').textContent = status.ban_total;
    $('securityBanHint').textContent = status.bans.some(item => !item.enforced) ? '部分 SSH 规则未能执行' : '到期自动解封';
    $('securityLocationCount').textContent = status.location_total;
    $('securityGuardToggle').setAttribute('aria-checked', String(settings.enabled));
    $('securityPolicySummary').textContent = minutes(settings.findtime) + '内认证失败 ' +
      settings.maxretry + ' 次，临时封禁 ' + minutes(settings.bantime);
    $('securityEnforcement').textContent = !settings.enabled ? '自动封禁已暂停 · 登录继续记录' :
      '面板登录防护已启用 · ' + status.ssh.detail;
    $('securityEnforcement').dataset.state = settings.enabled && !status.ssh.ready ? 'partial' : 'ready';
    if (!draft) {
      $('securityMaxretry').value = settings.maxretry;
      $('securityFindtime').value = settings.findtime / 60;
      $('securityBantime').value = settings.bantime / 60;
      $('securityIgnore').value = settings.ignoreip.join('\n');
    }
    $('securityClientNote').textContent = '当前访问 IP：' + status.client_ip + '；保存时自动加入白名单。';
    renderMap();
    const open = new Set([...$('securityActivityList').querySelectorAll('details[open]')].map(item => item.dataset.event));
    $('securityActivityList').innerHTML = status.items.map(item => '<details class="security-login-row" data-event="' +
      text(item.id) + '" data-kind="' + text(item.kind) + '" ' + (open.has(item.id) ? 'open' : '') + '><summary><span><time>' +
      text(date(item.ts)) + '</time><small>' + (item.source === 'panel' ? '面板登录' : 'SSH 登录') +
      '</small></span><strong>' + text(item.ip) + '</strong><span>' + text(item.location?.label || '待定位') +
      '<small>' + text(item.account || (item.source === 'panel' ? '面板管理员' : '—')) + '</small></span><span class="security-login-result">' +
      (item.kind === 'login_success' ? '成功' : '失败') + '</span></summary><p>' + text(item.detail) +
      '</p></details>').join('') || '<p class="security-empty">此筛选下暂无已采集登录记录。</p>';
    const pages = Math.max(1, Math.ceil(status.total / 10));
    $('securityActivityPage').textContent = '第 ' + (page + 1) + ' / ' + pages + ' 页 · 共 ' + status.total + ' 条';
    $('securityActivityRetention').textContent = (status.auth.readable ? '' : (status.auth.detail || 'SSH 日志尚不可读') + '。') +
      '登录记录保留 30 天，最多 20000 条；停机或日志轮转可能存在空档。';
    $('securityBans').innerHTML = status.bans.map(item => '<div class="security-ban-row"><div><strong>' +
      text(item.ip) + '</strong><small>' + (item.source === 'panel' ? '面板登录' : 'SSH 登录') +
      (item.enforced ? ' · 已执行' : ' · 未生效') + '</small></div><span>' + text(item.reason) +
      '<small>到期 ' + text(date(item.expires)) + '</small></span><button class="ghost-button compact-action" type="button" data-unban="' +
      text(item.ip) + '" data-source="' + text(item.source) + '">解封</button></div>').join('') ||
      '<p class="security-empty">当前没有临时封禁。</p>';
    $('securityBanPage').textContent = '第 ' + (banPage + 1) + ' / ' + Math.max(1, Math.ceil(status.ban_total / 10)) + ' 页 · 共 ' + status.ban_total + ' 项';
    window.AppSelects?.syncAll();
    controls();
  }

  function schedule() {
    clearTimeout(timer);
    if (view && !document.hidden) timer = setTimeout(refresh, 5000);
  }

  async function refresh() {
    clearTimeout(timer);
    if (request || busy || !view || document.hidden) return;
    const current = ++version;
    const controller = new AbortController(); request = controller; controls();
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const query = new URLSearchParams({page,ban_page:banPage,source:$('securityActivitySource').value,outcome:$('securityActivityOutcome').value});
      const response = await fetch('security/logins?' + query, {credentials:'same-origin',cache:'no-store',signal:controller.signal});
      if (response.status === 401) { location.reload(); return; }
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || '登录记录暂不可读');
      if (!body.settings || !body.ssh || !body.auth || !Array.isArray(body.items) || !Array.isArray(body.bans) || !Array.isArray(body.locations)) {
        throw new Error('安全中心响应不完整，请确认服务端已更新');
      }
      if (current !== version) return;
      status = body; token = body.csrf_token || token; error = '';
      if (page && page * 10 >= status.total || banPage && banPage * 10 >= status.ban_total) {
        page = Math.max(0, Math.min(page, Math.ceil(status.total / 10) - 1));
        banPage = Math.max(0, Math.min(banPage, Math.ceil(status.ban_total / 10) - 1));
        clearTimeout(timeout); request = null; return refresh();
      }
      render();
      await refreshProtection(controller.signal, current);
    } catch (err) {
      if (current !== version) return;
      error = err.name === 'AbortError' ? '请求超时' : err.message;
      $('securitySummaryNote').textContent = '读取失败：' + error + '；稍后自动重试，保留上次结果。';
      if (status) render();
    } finally {
      clearTimeout(timeout);
      if (current === version) { request = null; controls(); schedule(); }
    }
  }

  async function change(data) {
    if (busy || !status || error) return;
    version++; request?.abort(); request = null;
    busy = true; clearTimeout(timer); controls(); feedback('正在保存…');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 35000);
    try {
      const response = await fetch('security/logins', {method:'POST',credentials:'same-origin',
        headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify(data),signal:controller.signal});
      if (response.status === 401) { location.reload(); return; }
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || '保存失败，请刷新确认实际状态');
      draft = false; feedback('已保存。'); busy = false; await refresh();
    } catch (err) {
      feedback(err.message + '；请刷新确认实际状态。');
    } finally { clearTimeout(timeout); busy = false; controls(); schedule(); }
  }

  function policy(enabled) {
    return {action:'settings',enabled,maxretry:Number($('securityMaxretry').value),
      findtime:Number($('securityFindtime').value) * 60,bantime:Number($('securityBantime').value) * 60,
      ignoreip:$('securityIgnore').value.split(/[\s,]+/).filter(Boolean)};
  }
  function filter() {
    version++; request?.abort(); request = null; page = 0; refresh();
  }
  $('ipSecurityRefresh').onclick = refresh;
  $('securityBasicToggle').onclick = async () => {
    const enabled = !protection.enabled;
    if (!enabled && !await showConfirmDialog({title:'关闭基础防护',message:'撤销面板添加的连接与网站限流规则，登录防护恢复开启前的设置；业务文件和历史记录保留。',confirmText:'关闭防护'})) return;
    changeProtection(enabled);
  };
  $('securityBasicRetry').onclick = () => changeProtection(true);
  $('securityMapDots').onclick = event => {
    const point = event.target.closest('[data-region]');
    if (point) selectRegion(point.dataset.region);
  };
  $('securityMapDots').onkeydown = event => {
    const point = event.target.closest('[data-region]');
    if (point && (event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault(); selectRegion(point.dataset.region);
    }
  };
  $('securityMapDetailClose').onclick = closeRegion;
  $('securityView').addEventListener('keydown', event => {
    if (event.key === 'Escape' && selectedRegion !== null) { event.preventDefault(); closeRegion(); }
  });
  $('securityActivitySource').onchange = filter;
  $('securityActivityOutcome').onchange = filter;
  $('securityActivityPrev').onclick = () => { page--; refresh(); };
  $('securityActivityNext').onclick = () => { page++; refresh(); };
  $('securityBanPrev').onclick = () => { banPage--; refresh(); };
  $('securityBanNext').onclick = () => { banPage++; refresh(); };
  $('securityPolicyOpen').onclick = () => { $('securityPolicyForm').hidden = !$('securityPolicyForm').hidden; };
  $('securityPolicyForm').oninput = () => { draft = true; };
  $('securityPolicyForm').onsubmit = event => { event.preventDefault(); change(policy(status.settings.enabled)); };
  $('securityGuardToggle').onclick = async () => {
    const enabled = !status.settings.enabled;
    if (!enabled && !await showConfirmDialog({title:'暂停自动封禁',message:'暂停后会解除本面板创建的临时封禁，登录记录继续保存。其他防火墙和 Fail2ban 规则保持不变。',confirmText:'暂停防护'})) return;
    change({...status.settings,action:'settings',enabled});
  };
  $('securityBans').onclick = async event => {
    const button = event.target.closest('[data-unban]');
    if (button && !busy && await showConfirmDialog({title:'解除临时封禁',message:'解除 ' + button.dataset.unban + ' 的' + (button.dataset.source === 'ssh' ? ' SSH ' : '面板') + '临时封禁？',confirmText:'解封'})) {
      change({action:'unban',ip:button.dataset.unban,source:button.dataset.source});
    }
  };

  let mapLoaded = false;
  async function loadMap() {
    if (mapLoaded) return;
    mapLoaded = true;
    try {
      const response = await fetch('ui/world-land.geojson', {credentials:'same-origin'});
      if (!response.ok) throw new Error('地图底图加载失败');
      const data = await response.json();
      const paths = [];
      for (const feature of data.features) {
        const polygons = feature.geometry.type === 'MultiPolygon' ? feature.geometry.coordinates : [feature.geometry.coordinates];
        for (const polygon of polygons) {
          paths.push('<path d="' + polygon.map(ring => ring.map(([lon,lat],index) =>
            (index ? 'L' : 'M') + ((lon + 180) / 360 * 1000).toFixed(2) + ',' +
            ((90 - lat) / 180 * 500).toFixed(2)).join('') + 'Z').join('') + '"/>');
        }
      }
      $('securityMapLand').innerHTML = paths.join('');
    } catch (_) {
      mapLoaded = false;
      $('securityMapNote').textContent += ' · 地图底图暂不可用';
    }
  }
  window.IPSecurity = { viewChanged(name) {
    view = name === 'security'; clearTimeout(timer);
    if (view) { loadMap(); refresh(); }
    else { version++; request?.abort(); request = null; }
  }};
  document.addEventListener('visibilitychange', () => {
    clearTimeout(timer);
    if (document.hidden) { version++; request?.abort(); request = null; }
    else if (view) refresh();
  });
  if (activeView === 'security') window.IPSecurity.viewChanged('security');
})();
