/* Historical analysis is on demand; live watches reuse the existing resource poll. */
window.ObservationAnalysis = (() => {
  const $ = id => document.getElementById(id);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
  const valid = value => typeof value === 'number' && Number.isFinite(value);
  const fmt = (value, unit = '') => valid(value) ? `${value.toLocaleString('zh-CN', {maximumFractionDigits: 2})}${unit}` : '—';
  const delta = (a, b, unit = '') => valid(a) && valid(b) ? `${a - b > 0 ? '+' : ''}${fmt(a - b, unit)}` : '不可比';
  const when = ts => new Date(ts * 1000).toLocaleString('zh-CN', {hour12: false});
  const inputTime = ts => {
    const date = new Date(ts * 1000);
    return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  };
  const table = (head, rows) => `<div class="evidence-table-scroll"><table><thead><tr>${head.map(h => `<th>${escape(h)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
  const cell = value => `<td>${escape(value)}</td>`;
  const empty = text => `<p class="evidence-empty">${escape(text)}</p>`;
  const names = {cpu_percent:'CPU', memory_percent:'内存', network_total_kbps:'网络'};
  const storageKey = 'mini_deploy-observation-watches-v1';
  let selection = null, controller = null, report = null, growth = 'cpu', system = null;
  let watches = [], watchData = new Map(), watchRequest = null, nextWatchRead = 0, watchError = '';
  let requestContext = null, insightRequest = null, insightKey = '', nextInsightRead = 0;
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey) || '[]');
    if (Array.isArray(saved)) watches = saved.filter(w => w && typeof w.name === 'string' && w.name.length < 2300
      && (w.kind === 'resource' && Object.hasOwn(names, w.metric) && typeof w.target === 'string' && (!w.target || /^[a-f0-9]{64}$/.test(w.target))
      || w.kind === 'endpoint' && Array.isArray(w.identity) && w.identity.length === 4
        && w.identity.every(v => typeof v === 'string' && v.length <= 2048))).slice(0, 8);
  } catch (_) { /* Preferences may be disabled by the browser. */ }
  const watchKey = w => JSON.stringify(w.kind === 'endpoint' ? [w.kind, ...w.identity] : [w.kind, w.target, w.metric]);
  async function get(url, signal) {
    const response = await fetch(url, {credentials:'same-origin', signal});
    if (response.status === 401) throw new Error('登录已过期，请重新登录');
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '历史读取失败');
    return data;
  }
  function save() {
    try { localStorage.setItem(storageKey, JSON.stringify(watches)); return true; }
    catch (_) { $('observationWatchStatus').textContent = '浏览器禁止保存，关注仅在本次页面有效'; return false; }
  }
  function pin(watch) {
    if (!watches.some(w => watchKey(w) === watchKey(watch))) {
      if (watches.length >= 8) { $('observationRangeError').textContent = '最多关注 8 项，请先移除旧项。'; return; }
      watches.push(watch);
    }
    renderWatches();
    const saved = save();
    if (saved) $('observationWatchStatus').textContent = '已关注';
    watchRequest?.abort(); watchRequest = null;
    nextWatchRead = 0; refreshWatches();
  }
  function renderWatches() {
    $('observationWatchPanel').hidden = !watches.length;
    const root = $('observationWatches'), signature = JSON.stringify(watches);
    if (root.dataset.signature !== signature) {
      root.dataset.signature = signature;
      root.innerHTML = watches.map((w,index) => `<article class="observation-watch"><button type="button" data-watch-open="${index}" class="observation-watch-open"><span></span><strong></strong><small></small></button><button type="button" data-watch-remove="${index}" class="toolbar-icon" title="取消关注" aria-label="取消关注 ${escape(w.name)}"><img src="ui/previews/ssh-ai/icons/x.svg" alt=""></button></article>`).join('');
    }
    watches.forEach((w, index) => {
      let value = '—', note = '等待资源采样';
      if (w.kind === 'endpoint') {
        const data = watchData.get(watchKey(w));
        value = data?.result ? `${fmt(data.result.count)} 次` : '—';
        note = data ? `最近 1h · P95 ${fmt(data.result?.p95, ' ms')} · ${when(data.at)}${data.incomplete ? ' · 采集可能不完整' : ''}` : watchError || '正在读取请求统计…';
      } else if (system) {
        const item = w.target ? system.docker?.containers?.find(c => c.id === w.target) : system.server;
        const number = w.target ? item?.[w.metric] : w.metric === 'memory_percent' ? item?.memory?.percent
          : w.metric === 'network_total_kbps' ? valid(item?.network?.rx_kbps) && valid(item?.network?.tx_kbps)
            ? item.network.rx_kbps + item.network.tx_kbps : null : item?.cpu_percent;
        value = fmt(number, w.metric === 'network_total_kbps' ? ' KiB/s' : '%');
        note = !item ? '对象不在当前列表，历史关注已保留' : !valid(number) ? '当前指标不可用' : w.target && w.metric === 'cpu_percent' ? '实时 · 100% = 1 个逻辑核' : '实时';
      }
      const button = root.children[index].querySelector('.observation-watch-open');
      for (const [selector,text] of [['span',w.name],['strong',value],['small',note]]) {
        const element = button.querySelector(selector);
        if (element.textContent !== text) element.textContent = text;
      }
    });
  }
  async function refreshWatches() {
    if (watchRequest || Date.now() < nextWatchRead || document.hidden || !$('serverView').classList.contains('active')) return;
    const watched = watches.filter(w => w.kind === 'endpoint').map(w => w.identity);
    if (!watched.length) return;
    const current = new AbortController(); watchRequest = current;
    const timeout = setTimeout(() => current.abort(), 20000);
    const until = Math.floor(Date.now() / 60000) * 60;
    try {
      const data = await get(`observations/requests?${new URLSearchParams({since:until - 3600, until, watch:JSON.stringify(watched)})}`, current.signal);
      if (watchRequest !== current) return;
      for (const item of data.watched || []) watchData.set(watchKey({kind:'endpoint', identity:item.identity}), {...item, at:until, incomplete:data.incomplete});
      watchError = '';
      $('observationWatchStatus').textContent = '接口统计更新于 ' + when(until);
    } catch (error) {
      if (watchRequest !== current) return;
      watchError = error.name === 'AbortError' ? '请求统计读取超时' : error.message;
      $('observationWatchStatus').textContent = watchError + '，保留上次结果';
    } finally {
      clearTimeout(timeout);
      if (watchRequest === current) { watchRequest = null; nextWatchRead = Date.now() + 60000; renderWatches(); }
    }
  }
  $('observationWatches').addEventListener('click', event => {
    const remove = event.target.closest('[data-watch-remove]');
    if (remove) { watches.splice(Number(remove.dataset.watchRemove), 1); renderWatches(); save(); return; }
    const open = event.target.closest('[data-watch-open]');
    if (!open) return;
    const watch = watches[Number(open.dataset.watchOpen)];
    if (watch.kind === 'endpoint') {
      setView('requests'); window.NginxRequests?.view(watch.identity[0]);
      $('requestSearch').value = watch.identity[3]; $('requestSearch').dispatchEvent(new Event('input'));
    } else {
      $('observationTarget').value = watch.target; $('observationTarget').dispatchEvent(new Event('change'));
      document.querySelector(`[data-observation-metric="${watch.metric}"]`)?.click();
      $('loadMap').scrollIntoView({block:'center'});
    }
  });
  $('observationPin').addEventListener('click', () => {
    const current = window.ServerOverview.selection();
    pin({kind:'resource', ...current, name:`${current.target ? current.targetName : '服务器'} · ${names[current.metric]}`});
  });
  function renderComparison(data) {
    const a = data.resources, b = data.reference;
    const entries = [['cpu_percent','CPU','%','百分点'], ['memory_percent','内存','%','百分点'],
      ['network_rx_kib_s','入站吞吐',' KiB/s',' KiB/s'], ['network_tx_kib_s','出站吞吐',' KiB/s',' KiB/s']];
    let rows = entries.map(([key,label,unit,diff]) => {
      const current = a?.metrics?.[key]?.average, before = b?.metrics?.[key]?.average;
      return `<tr>${cell('整机 · ' + label)}${cell(fmt(current, unit))}${cell(fmt(before, unit))}${cell(delta(current, before, diff))}</tr>`;
    });
    if (selection.target) {
      const item = data.containers?.find(c => c.container === selection.target);
      rows = [['cpu','容器 CPU'],['memory','容器内存']].map(([key,label]) => `<tr>${cell(label)}${cell(fmt(item?.[key], '%'))}${cell(fmt(item?.reference?.[key], '%'))}${cell(delta(item?.[key], item?.reference?.[key], '百分点'))}</tr>`).concat(rows);
    }
    const request = data.requests, baseline = data.reference_requests;
    for (const [key,label,unit,diff] of [['count','已采集请求',' 次',' 次'], ['error_rate','4xx / 5xx 比例','%','百分点'],['average','平均耗时',' ms',' ms'],['p95','P95 估计',' ms',' ms']]) {
      const av = request?.available ? request[key] : null, bv = baseline?.available ? baseline[key] : null;
      rows.push(`<tr>${cell(label)}${cell(fmt(av, unit))}${cell(fmt(bv, unit))}${cell(delta(av, bv, diff))}</tr>`);
    }
    $('observationComparison').innerHTML = table(['指标','所选区间均值 / 统计','对比区间','变化'], rows)
      + `<p class="evidence-caption">对比：${escape(when(data.reference_since))} 至 ${escape(when(data.reference_until))}（不含结束时刻）。整机采样覆盖 ${fmt(a?.coverage_percent, '%')} / ${fmt(b?.coverage_percent, '%')}；缺失不补零。${request?.incomplete || baseline?.incomplete ? '请求采集可能不完整，次数差异仅供参考。' : ''}</p>`;
  }
  function renderGrowth() {
    if (!report) return;
    const metric = growth === 'cpu' ? 'cpu_growth' : 'memory_growth';
    const sort = items => items.slice().sort((a,b) => (valid(b[metric]) ? b[metric] : -Infinity) - (valid(a[metric]) ? a[metric] : -Infinity)).slice(0, 8);
    const containers = sort(report.containers || []), processes = sort(report.processes?.items || []);
    const rows = (items, kind) => items.map(item => {
      const value = growth === 'cpu' ? kind === 'container' ? item.cpu : item.host_cpu_percent : kind === 'container' ? item.memory : item.rss_mib;
      const difference = item[metric];
      const unit = growth === 'memory' && kind === 'process' ? ' MiB' : '%';
      const differenceUnit = growth === 'memory' && kind === 'process' ? ' MiB' : '百分点';
      const identity = kind === 'container' ? item.container : item.container_id;
      return `<tr><td>${escape(item.name || '未知')}<small class="evidence-process-source">${escape(kind === 'container' ? identity.slice(0,12) : `PID ${item.pid} · ${item.container || item.service || '宿主机'}`)}</small></td>${cell(fmt(value, unit))}${cell(valid(difference) ? `${difference > 0 ? '+' : ''}${fmt(difference, differenceUnit)}` : '无法比较')}${cell(!valid(difference) ? '现场或起始样本不足' : difference > 0 ? '增长' : difference === 0 ? '持平' : '下降')}</tr>`;
    });
    $('observationGrowth').innerHTML = `<section><h5>容器 · 首尾窗口变化</h5>${containers.length ? table(['对象','区间均值','首尾变化','趋势'],rows(containers,'container')) : empty(report.errors.containers || '该时段没有容器采样。')}<p class="evidence-caption">首尾窗口各取区间 1/4，最长 5 分钟；CPU 100% = 1 核，内存百分比基于各容器上限，不可相加。重建按新 ID 单独展示。</p></section><section><h5>进程 · 首末保留现场</h5>${processes.length ? table(['进程','末次现场','首末变化','趋势'],rows(processes,'process')) : empty(report.errors.processes || report.processes?.notice || '没有可比较的进程现场。')}<p class="evidence-caption">${escape(report.processes?.notice || '')}${report.processes?.first_at ? ` 现场：${escape(when(report.processes.first_at))} 至 ${escape(when(report.processes.last_at))}。` : ''}${escape((report.processes?.warnings || []).join('；'))}</p></section>`;
  }
  function latencyHTML(data) {
    if (!data) return empty('请求历史暂不可读。');
    if (!data.available || !data.sources?.length) return empty('当前来源尚未接入请求采集或已不可用，无法还原网站请求。');
    const count = data.duration_count || 0;
    const bars = (data.distribution || []).map(bin => `<div class="latency-bin"><span>${escape(bin.label)}</span><progress max="${Math.max(1,count)}" value="${bin.count}" aria-label="${escape(bin.label)} ${bin.count} 次"></progress><strong>${fmt(bin.count)}</strong><small>${count ? fmt(bin.count / count * 100, '%') : '—'}</small></div>`).join('');
    const endpoints = data.slow_endpoints || [];
    return `<div class="latency-summary"><span>已采集 <strong>${fmt(data.count)} 次</strong></span><span>有效耗时 <strong>${fmt(count)} 次</strong></span><span>P95 <strong>${fmt(data.p95, ' ms')}</strong></span><span>P99 <strong>${fmt(data.p99, ' ms')}</strong></span></div>${count ? `<div class="latency-distribution">${bars}</div>` : empty('没有有效耗时样本，暂不能生成分布。')}${endpoints.length ? table(['慢接口（P95 降序）','请求数','P95 估计','均值',''],endpoints.map(item => `<tr><td class="endpoint-path">${escape(item.method)} ${escape(item.host)}${escape(item.path)}<small class="evidence-process-source">${escape(item.source)}</small></td>${cell(fmt(item.count))}${cell(fmt(item.p95,' ms'))}${cell(fmt(item.average,' ms'))}<td><button type="button" class="toolbar-icon" data-endpoint-pin="${escape(JSON.stringify(item.identity))}" title="关注接口" aria-label="关注接口 ${escape(item.path)}"><img src="ui/previews/ssh-ai/icons/plus.svg" alt=""></button></td></tr>`)) : ''}<p class="evidence-caption">${escape(data.notice)}${data.incomplete ? ' 采集覆盖不完整或有延迟，请检查请求来源。' : ''}</p>`;
  }
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-endpoint-pin]');
    if (!button) return;
    const identity = JSON.parse(button.dataset.endpointPin);
    pin({kind:'endpoint', identity, name:`${identity[2]} ${identity[1]}${identity[3]}`});
    button.title = '已关注'; button.setAttribute('aria-label','已关注接口');
  });
  async function analyze() {
    controller?.abort(); controller = null; report = null;
    if (!selection) return;
    const current = new AbortController(); controller = current;
    const timeout = setTimeout(() => current.abort(), 25000);
    $('observationAnalysis').setAttribute('aria-busy','true');
    $('observationAnalysisStatus').textContent = '正在读取所选区间与对比区间…';
    for (const id of ['observationComparison','observationGrowth','observationLatency']) $(id).innerHTML = '';
    const params = new URLSearchParams({...selection, compare:$('observationCompare').value, source:$('observationRequestSource').value});
    if ($('observationCompare').value === 'custom') {
      const reference = Date.parse($('observationReference').value) / 1000;
      if (!valid(reference)) {
        clearTimeout(timeout); controller = null;
        $('observationAnalysis').setAttribute('aria-busy','false');
        $('observationAnalysisStatus').textContent = '请选择对比区间结束时间'; return;
      }
      params.set('reference',reference);
    }
    try {
      const data = await get(`observations/analyze?${params}`, current.signal);
      if (controller !== current) return;
      report = data; renderComparison(data); renderGrowth();
      $('observationLatency').innerHTML = latencyHTML(data.requests);
      $('observationTrafficScope').textContent = selection.target ? '已选来源的请求，未按容器归因' : '所选区间 · 已采集请求';
      const source = $('observationRequestSource'), value = source.value;
      const options = (data.requests?.sources || []).map(s => new Option(s.name || s.key, s.key));
      if (value && !options.some(o => o.value === value)) options.push(new Option(`${value}（来源当前不可用）`,value));
      source.replaceChildren(new Option('全部已采集来源',''), ...options);
      source.value = value; window.AppSelects?.syncAll();
      const errors = Object.entries(data.errors || {}).map(([key,value]) => `${({resources:'整机统计',reference:'对比统计',containers:'容器',processes:'进程',requests:'请求',reference_requests:'请求对比',events:'事件'}[key] || key)}：${value}`);
      if (data.resources?.error) errors.push('性能历史写入失败：' + data.resources.error);
      $('observationAnalysisStatus').textContent = errors.length ? errors.join('；') : `已读取 · ${when(data.since)} 至 ${when(data.until)}（不含结束时刻）`;
    } catch (error) {
      if (controller === current) $('observationAnalysisStatus').textContent = (error.name === 'AbortError' ? '区间分析读取超时' : error.message) + '，点击刷新可重试。';
    } finally {
      clearTimeout(timeout);
      if (controller === current) { controller = null; $('observationAnalysis').setAttribute('aria-busy','false'); }
    }
  }
  document.addEventListener('observation-range-selected', event => {
    selection = event.detail;
    $('observationFrom').value = inputTime(selection.since); $('observationTo').value = inputTime(selection.until);
    analyze();
  });
  function cancel(message) {
    const pending = Boolean(controller); controller?.abort(); controller = null;
    $('observationAnalysis').setAttribute('aria-busy','false');
    if (pending) $('observationAnalysisStatus').textContent = message;
  }
  document.addEventListener('observation-range-closed', () => { cancel(''); selection = null; report = null; });
  document.addEventListener('observation-analysis-pause', () => cancel('读取已暂停，点击刷新可重试。'));
  $('observationRangeForm').addEventListener('submit', event => {
    event.preventDefault();
    const since = Date.parse($('observationFrom').value) / 1000, until = Date.parse($('observationTo').value) / 1000;
    if (!valid(since) || !valid(until) || until <= since || until - since > 7 * 86400 || until > Date.now() / 1000 + 60) {
      $('observationRangeError').textContent = '请选择结束晚于开始、且不超过 7 天的历史区间。'; return;
    }
    $('observationRangeError').textContent = '';
    window.ServerOverview.selectRange(since,until);
  });
  $('observationCompare').addEventListener('change', () => {
    $('observationReferenceField').hidden = $('observationCompare').value !== 'custom'; analyze();
  });
  $('observationReference').addEventListener('change', analyze);
  $('observationRequestSource').addEventListener('change', analyze);
  $('observationAnalysisRefresh').addEventListener('click', analyze);
  document.querySelectorAll('[data-growth-metric]').forEach(button => button.addEventListener('click', () => {
    growth = button.dataset.growthMetric;
    document.querySelectorAll('[data-growth-metric]').forEach(item => item.setAttribute('aria-pressed',String(item === button)));
    renderGrowth();
  }));
  async function refreshInsights(force = false) {
    if (!$('requestInsights').open || !requestContext || document.hidden || !$('requestsView').classList.contains('active')) return;
    const key = JSON.stringify(requestContext);
    if (!force && key === insightKey && Date.now() < nextInsightRead) return;
    insightRequest?.abort();
    const current = new AbortController(); insightRequest = current; insightKey = key; nextInsightRead = Date.now() + 60000;
    if (requestContext.backend !== 'gateway' || !requestContext.source) {
      insightRequest = null;
      $('requestInsightsBody').innerHTML = empty('请先选择已接入采集的服务，完整耗时分布使用持久化请求统计。'); return;
    }
    $('requestInsightsBody').textContent = '正在读取耗时分布…';
    const until = Math.floor(Date.now() / 60000) * 60;
    const seconds = {'1h':3600,'24h':86400,'7d':604800}[requestContext.period] || 86400;
    const timeout = setTimeout(() => current.abort(),20000);
    try {
      const data = await get(`observations/requests?${new URLSearchParams({since:until-seconds,until,source:requestContext.source})}`,current.signal);
      if (insightRequest === current) $('requestInsightsBody').innerHTML = `<p class="evidence-caption">${escape(when(data.since))} 至 ${escape(when(data.until))} · 当前服务全部接口（不受下方搜索与状态筛选影响）</p>` + latencyHTML(data);
    } catch (error) {
      if (insightRequest === current) $('requestInsightsBody').textContent = error.name === 'AbortError' ? '耗时分布读取超时，请重新展开重试。' : error.message;
      nextInsightRead = 0;
    } finally { clearTimeout(timeout); if (insightRequest === current) insightRequest = null; }
  }
  $('requestInsights').addEventListener('toggle', () => {
    if ($('requestInsights').open) refreshInsights(true); else { insightRequest?.abort(); insightRequest = null; }
  });
  const now = Math.floor(Date.now() / 60000) * 60;
  $('observationFrom').value = inputTime(now - 900); $('observationTo').value = inputTime(now);
  $('observationReference').value = inputTime(now - 900);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { watchRequest?.abort(); watchRequest = null; insightRequest?.abort(); insightRequest = null; }
    else { nextWatchRead = 0; refreshWatches(); refreshInsights(true); }
  });
  renderWatches();
  return {system: payload => { system = payload; if (watches.length) { renderWatches(); refreshWatches(); } },
    requestContext: context => { requestContext = context; refreshInsights(); }};
})();
