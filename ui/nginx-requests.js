window.NginxRequests = (() => {
  let records = [], pending = false, generation = 0, sourceChosen = false, pendingRefresh = false;
  const source = $('requestSource'), notice = $('requestNotice'), rows = $('requestRows');
  const rendered = new WeakMap();
  const sampleData = new WeakMap();
  let historyResult = null, historyPage = 0, searchTimer = null;
  let visitors = null;

  function updateHtml(element, html) {
    if (rendered.get(element) === html) return;
    element.innerHTML = html;
    rendered.set(element, html);
  }

  function formatDuration(value, empty = '未记录') {
    if (!Number.isFinite(value) || value < 0) return empty;
    return value > 1000 ? `${(value / 1000).toFixed(2)} s` : `${value.toFixed(2)} ms`;
  }

  function normalizeRequestPath(path) {
    return path.split('/').map(part => {
      if (/^[0-9]+$/.test(part)) return ':number';
      if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(part) ||
          /^(?:[0-9a-f]{24}|[0-9a-f]{32})$/i.test(part)) return ':id';
      return part;
    }).join('/');
  }

  function summarizeRequests(items, slowMs = 1000) {
    let successes = 0, errors = 0, slowCount = 0, durationSum = 0, durationCount = 0, upstreamSum = 0, upstreamCount = 0;
    for (const item of items) {
      if (item.status >= 200 && item.status < 400) successes++;
      if (item.status >= 400) errors++;
      if (Number.isFinite(item.duration_ms) && item.duration_ms >= slowMs) slowCount++;
      if (Number.isFinite(item.duration_ms) && item.duration_ms >= 0) { durationSum += item.duration_ms; durationCount++; }
      if (Number.isFinite(item.upstream_ms) && item.upstream_ms >= 0) { upstreamSum += item.upstream_ms; upstreamCount++; }
    }
    const latencies = items.map(item => item.duration_ms).filter(value => Number.isFinite(value) && value >= 0).sort((a, b) => a - b);
    const percentile = ratio => latencies.length ? latencies[Math.ceil(latencies.length * ratio) - 1] : null;
    return {items, count: items.length, successes, errors, slowCount, durationSum, durationCount, upstreamSum, upstreamCount,
      p95: percentile(.95), p99: percentile(.99),
      average: durationCount ? durationSum / durationCount : null,
      upstreamAverage: upstreamCount ? upstreamSum / upstreamCount : null,
      successRate: items.length ? successes / items.length * 100 : 0};
  }

  function groupRequests(items, normalize = false, slowMs = 1000) {
    const groups = new Map();
    const ordered = items.slice().sort((a, b) => (Date.parse(b.at) || 0) - (Date.parse(a.at) || 0));
    for (const item of ordered) {
      const path = normalize ? normalizeRequestPath(item.path || '') : item.path || '';
      const key = JSON.stringify([item.host || '', item.method || '', path]);
      if (!groups.has(key)) groups.set(key, {key, host: item.host || '', method: item.method || '', path, items: []});
      groups.get(key).items.push(item);
    }
    return [...groups.values()].map(group => ({...group, ...summarizeRequests(group.items, slowMs)}));
  }

  function requestTree(groups) {
    const hosts = new Map();
    const node = (host, path) => ({host, path, children: new Map(), groups: []});
    for (const group of groups) {
      if (!hosts.has(group.host)) hosts.set(group.host, node(group.host, ''));
      let parent = hosts.get(group.host);
      const parts = group.path.split('/');
      // Bound tree depth even for adversarial or machine-generated paths.
      if (parts.length > 16) parts.splice(15, parts.length - 15, parts.slice(15).join('/'));
      for (let i = 0; i < parts.length; i++) {
        const path = parts.slice(0, i + 1).join('/');
        if (!parent.children.has(parts[i])) parent.children.set(parts[i], node(group.host, path));
        parent = parent.children.get(parts[i]);
      }
      parent.groups.push(group);
    }
    function compact(current) {
      while (!current.groups.length && current.children.size === 1) current = current.children.values().next().value;
      const children = [...current.children.values()].sort((a, b) => a.path.localeCompare(b.path)).map(compact);
      const leaves = current.groups.slice().sort((a, b) => a.method.localeCompare(b.method))
        .map(group => ({...group, key: `leaf:${group.key}`, leaves: 1}));
      if (!children.length && leaves.length === 1) return leaves[0];
      const all = [...leaves, ...children];
      const items = all.flatMap(child => child.items).sort((a, b) => (Date.parse(b.at) || 0) - (Date.parse(a.at) || 0));
      const totals = Object.fromEntries(['count', 'successes', 'errors', 'slowCount', 'durationSum', 'durationCount', 'upstreamSum', 'upstreamCount']
        .map(key => [key, all.reduce((sum, child) => sum + (child[key] || 0), 0)]));
      return {key: `branch:${JSON.stringify([current.host, current.path])}`, host: current.host, path: current.path,
        children: all, leaves: all.reduce((total, child) => total + child.leaves, 0), ...totals, items,
        average: totals.durationCount ? totals.durationSum / totals.durationCount : null,
        upstreamAverage: totals.upstreamCount ? totals.upstreamSum / totals.upstreamCount : null,
        successRate: totals.count ? totals.successes / totals.count * 100 : 0};
    }
    return [...hosts.values()].sort((a, b) => a.host.localeCompare(b.host)).map(compact);
  }

  function render() {
    const term = $('requestSearch').value.trim().toLowerCase();
    const status = $('requestStatus').value;
    $('requestSlowField').hidden = status !== 'slow';
    const slowMs = slowThreshold();
    const tree = $('requestLayout').value === 'tree';
    window.RequestMap?.render(visitors, {scope:`${$('requestBackend').value}:${$('requestGatewayKey').value}:${$('requestContainer').value}:${$('requestPeriod').value}`,
      ...(historyResult ? {} : {records:records.filter(item => matchesSearch(item, term) &&
        (status === 'all' || status === 'success' && item.status >= 200 && item.status < 400 ||
        status === 'error' && item.status >= 400 || status === 'slow' && item.duration_ms >= slowMs))})});
    const groups = historyResult?.groups || groupRequests(records, tree, slowMs).filter(group => {
      if (status === 'success' && group.successes !== group.count) return false;
      if (status === 'error' && !group.errors) return false;
      if (status === 'slow' && !group.slowCount) return false;
      return !term || `${group.path} ${group.host}`.toLowerCase().includes(term) ||
        group.items.some(item => matchesSearch(item, term));
    });
    $('requestSampleSummary').textContent = historyResult
      ? `时段内${historyResult.aggregate_mode ? '累计' : '已保存'} ${historyResult.total_requests} 条 · ${historyResult.total_groups} 类请求 · 本页 ${groups.length} 类 · 明细取最近 30 条 · 2xx / 3xx 计为成功`
      : records.length
      ? `本次读取 ${records.length} 条 · 显示 ${groups.length} ${tree ? '类请求（疑似 ID 已合并）' : '个地址'} · 2xx / 3xx 计为成功` : '';
    if (status === 'slow') $('requestSampleSummary').textContent += ` · 慢请求 ≥ ${formatDuration(slowMs)}，汇总为整类，明细仅显示慢请求`;
    if (term) $('requestSampleSummary').textContent += ' · 明细仅显示匹配记录';
    $('requestHistoryPages').hidden = !historyResult || (!historyPage && !historyResult.has_more);
    $('requestHistoryPrevious').disabled = historyPage === 0;
    $('requestHistoryNext').disabled = !historyResult?.has_more;
    $('requestHistoryPage').textContent = `第 ${historyPage + 1} 页`;
    if (!groups.length) {
      rows.innerHTML = `<p class="request-empty">${historyResult?.error ? '采集需要检查，当前没有匹配的已保存请求；请查看上方提示。' : '暂无匹配的请求'}</p>`;
      return;
    }
    let header = rows.querySelector('.request-summary-head');
    if (!header) {
      rows.replaceChildren();
      header = document.createElement('div');
      header.className = 'request-summary request-summary-head';
      header.setAttribute('aria-hidden', 'true');
      header.innerHTML = '<span>请求地址</span><span>次数</span><span>平均耗时</span><span>平均上游</span><span>最近请求 / 成功率</span>';
      rows.append(header);
    }
    const searchChanged = rows.dataset.search !== term;
    rows.dataset.search = term;
    drawRequests(rows, tree ? requestTree(groups) : groups, 0, '', term, searchChanged);
  }

  function drawRequests(parent, entries, depth, prefix, term, searchChanged) {
    const existing = new Map([...parent.children].filter(element => element.classList.contains('request-group')).map(element => [element.dataset.key, element]));
    entries.forEach((group, index) => {
      let element = existing.get(group.key);
      const created = !element;
      if (!element) {
        element = document.createElement('details');
        element.className = 'request-group';
        element.dataset.key = group.key;
        element.innerHTML = `<summary class="request-summary"></summary><div class="${group.children ? 'request-children' : 'request-detail-list'}"></div>`;
      }
      existing.delete(group.key);
      element.classList.toggle('request-branch', !!group.children);
      element.style.setProperty('--request-indent', `${Math.min(depth, 3) * 12}px`);
      if (term && group.children && (created || searchChanged)) element.open = true;
      else if (created && group.children && depth === 0) element.open = true;
      const samples = group.items.slice(0, 30).reverse();
      const sampleKind = $('requestStatus').value === 'slow' ? '慢请求' : '请求';
      const bars = samples.map((item, position) => `<span class="request-tick ${item.status >= 200 && item.status < 400 ? 'is-ok' : item.status >= 400 ? 'is-error' : 'is-unknown'}" ${position === 0 ? `style="grid-column-start:${31 - samples.length}"` : ''} title="${escapeHtml(item.at || '-')} · ${escapeHtml(item.status)}"></span>`).join('');
      const label = prefix && group.path.startsWith(prefix + '/') ? group.path.slice(prefix.length + 1) : group.path;
      const pathCount = group.pathCount ?? new Set(group.items.map(item => item.path)).size;
      const meta = [!depth ? group.host || '-' : '', group.children ? `${group.leaves} 类请求` : pathCount > 1 ? `${pathCount} 个原始地址` : '', group.slowCount ? `慢 ${group.slowCount} 次` : ''].filter(Boolean).join(' · ');
      const summary = `<span class="request-route"><span class="request-route-top"><i class="request-expand" aria-hidden="true"></i>${group.children ? '' : `<b>${escapeHtml(group.method)}</b>`}<code title="${escapeHtml(group.path)}">${escapeHtml(label || '/')}${group.children && label && !label.endsWith('/') ? '/' : ''}</code></span>${meta ? `<span class="request-route-host">${escapeHtml(meta)}</span>` : ''}</span>
        <span class="request-metric" data-label="次数">${group.count}</span>
        <span class="request-metric" data-label="平均耗时" title="${group.durationCount} 条有效耗时样本${group.children ? '' : ` · P95 ${formatDuration(group.p95)} · P99 ${formatDuration(group.p99)}`}">${formatDuration(group.average)}</span>
        <span class="request-metric" data-label="平均上游" title="${group.upstreamCount} 条有效上游样本">${formatDuration(group.upstreamAverage, '-')}</span>
        <span class="request-reliability"><span class="request-spark" role="img" aria-label="最近 ${samples.length} 次${sampleKind}，由旧到新">${bars}</span><strong class="${group.errors ? 'has-errors' : ''}" title="${group.successes} / ${group.count} 次成功">${group.successRate.toFixed(1)}%</strong></span>`;
      const summaryNode = element.querySelector('summary');
      updateHtml(summaryNode, summary);
      if (group.children) {
        drawRequests(element.querySelector('.request-children'), group.children, depth + 1, group.path, term, searchChanged);
      } else {
        const showOriginal = group.items.some(item => (item.path || '') !== group.path);
        const detailNode = element.querySelector('.request-detail-list');
        const samples = group.items.filter(item => matchesSearch(item, term) && ($('requestStatus').value !== 'slow' || Number.isFinite(item.duration_ms) && item.duration_ms >= slowThreshold()));
        drawSamples(detailNode, samples, showOriginal);
        let latency = detailNode.querySelector('.request-latency-stats');
        if (!latency) { latency = document.createElement('p'); latency.className = 'ops-caption request-latency-stats'; detailNode.append(latency); }
        latency.textContent = `P95 ${formatDuration(group.p95)} · P99 ${formatDuration(group.p99)} · ${group.durationCount} 条有效耗时 · ${historyResult ? '所选时段内已保存请求' : '当前采集样本'}，不是仅按下方明细计算`;
      }
      const position = index + (parent === rows ? 1 : 0);
      if (parent.children[position] !== element) parent.insertBefore(element, parent.children[position] || null);
    });
    for (const element of existing.values()) element.remove();
  }

  function drawSamples(parent, items, showOriginal) {
    if (!parent.querySelector('.request-detail-head')) {
      parent.innerHTML = '<div class="request-detail-head"><span>时间 / 请求详情</span><span>状态</span><span>耗时</span><span>上游</span></div>';
    }
    const existing = new Map([...parent.querySelectorAll('.request-inspect')].map(node => [node.dataset.key, node]));
    const occurrences = new Map();
    items.forEach((item, index) => {
      const signature = String(item.record_id ?? JSON.stringify(item));
      const ordinal = occurrences.get(signature) || 0;
      occurrences.set(signature, ordinal + 1);
      const key = `${signature}:${ordinal}`;
      let node = existing.get(key);
      if (!node) {
        node = document.createElement('details');
        node.className = 'request-inspect';
        node.dataset.key = key;
        node.innerHTML = '<summary class="request-detail-row"></summary><div class="request-inspect-body"></div>';
        node.addEventListener('toggle', () => { if (node.open) renderSampleDetails(node); });
      }
      existing.delete(key);
      sampleData.set(node, item);
      updateHtml(node.querySelector('summary'), `<time>${escapeHtml(item.at || '-')}${Number.isFinite(item.duration_ms) && item.duration_ms >= slowThreshold() ? '<span class="request-slow-label">慢</span>' : ''}</time><strong class="${item.status >= 400 ? 'failed' : ''}">${escapeHtml(item.status)}</strong><span>${formatDuration(item.duration_ms)}</span><span>${formatDuration(item.upstream_ms, '-')}</span>${showOriginal ? `<code class="request-original-path">${escapeHtml(item.path || '/')}</code>` : ''}`);
      if (node.open) renderSampleDetails(node);
      if (parent.children[index + 1] !== node) parent.insertBefore(node, parent.children[index + 1] || null);
    });
    for (const node of existing.values()) node.remove();
  }

  function renderSampleDetails(node) {
    const item = sampleData.get(node);
    if (!item) return;
    const metadata = [
      ['请求', `${item.method || '-'} ${item.host || ''}${item.path || '/'}`],
      ['请求 ID', item.request_id ? `${item.request_id}${item.request_id_source === 'generated' ? '（自动生成）' : ''}` : '未采集'],
      ['来源 IP', item.client_ip || '未采集'],
      ...(item.peer_ip && item.peer_ip !== item.client_ip ? [['连接 IP', item.peer_ip]] : []),
      ['协议', item.protocol || '未采集'],
      ['上游地址', item.upstream_address || '未采集'],
      ['上游状态', (item.upstream_attempts || []).map(attempt => attempt.status ?? '未采集').join(' → ') || '未采集'],
      ['响应大小', Number.isFinite(item.response_bytes) ? `${item.response_bytes.toLocaleString()} B` : '未采集'],
    ];
    const headers = Object.entries(item.request_headers || {});
    const attempts = item.upstream_attempts || [];
    const phases = attempts.length ? attempts.map((attempt, index) => `<div class="request-phase-row"><span>${attempts.length > 1 ? `第 ${index + 1} 次上游` : '上游请求'}</span><span><small>连接</small>${formatDuration(attempt.connect_ms)}</span><span><small>等待响应头</small>${formatDuration(attempt.wait_ms)}</span><span><small>传输响应</small>${formatDuration(attempt.transfer_ms)}</span></div>`).join('') : '<p>此来源未采集分阶段耗时。</p>';
    const copyId = item.request_id ? `<button class="request-copy-id" data-copy-request-id="${escapeHtml(item.request_id)}" type="button" aria-label="复制请求 ID" title="复制请求 ID"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg></button>` : '';
    updateHtml(node.querySelector('.request-inspect-body'), `<dl class="request-inspect-meta">${metadata.map(([name, value]) => `<dt>${escapeHtml(name)}</dt><dd>${escapeHtml(value)}${name === '请求 ID' ? copyId : ''}</dd>`).join('')}</dl><h4>分阶段耗时</h4>${phases}<p>等待与传输为上游累计耗时之差；长连接结束后才完整。上游状态是代理记录的尝试结果，连接失败时不代表后端实际响应。</p><h4>请求头${headers.length ? ` · ${headers.length}` : ''}</h4>${headers.length ? `<table class="request-header-table" aria-label="请求头"><tbody>${headers.map(([name, value]) => `<tr><th scope="row">${escapeHtml(name)}</th><td>${escapeHtml(value)}</td></tr>`).join('')}</tbody></table>` : '<p>此记录没有采集到请求头。</p>'}<p>仅保留常用请求头，值最多 256 字符；不保存 Cookie、Authorization 或请求正文。来源 IP 是采集点获取的地址，X-Forwarded-For / X-Real-IP 是代理声明值。</p>`);
  }

  function slowThreshold() {
    const value = Number($('requestSlowMs').value);
    return Number.isInteger(value) && value >= 1 && value <= 3600000 ? value : 1000;
  }

  function matchesSearch(item, term) {
    return !term || `${item.path || ''} ${normalizeRequestPath(item.path || '')} ${item.host || ''} ${item.client_ip || ''} ${item.request_id || ''}`.toLowerCase().includes(term);
  }

  rows.addEventListener('click', async event => {
    const button = event.target.closest('[data-copy-request-id]');
    if (!button) return;
    try {
      await copyText(button.dataset.copyRequestId);
      button.title = '已复制'; button.setAttribute('aria-label', '请求 ID 已复制');
    } catch (_) {
      button.title = '复制失败，请选中请求 ID 复制';
    }
  });

  async function refresh() {
    if (pending || document.hidden || !$('requestsView').classList.contains('active')) return;
    pending = true;
    $('requestRefresh').disabled = true;
    const version = ++generation;
    try {
      let backend = $('requestBackend').value;
      let result;
      $('requestGatewayField').hidden = backend !== 'gateway';
      $('requestPeriodField').hidden = backend !== 'gateway';
      $('requestLimitField').hidden = backend === 'gateway';
      $('requestContainerField').hidden = backend !== 'caddy';
      if (backend === 'gateway') {
        await window.RequestGateway.ensure();
        if (version !== generation) return;
        const key = $('requestGatewayKey').value;
        const params = new URLSearchParams({key, period: $('requestPeriod').value, layout: $('requestLayout').value,
          search: $('requestSearch').value.trim(), status: $('requestStatus').value, slow_ms: String(slowThreshold()), page: String(historyPage)});
        result = key ? await fetchJson(`request-history?${params}`)
          : {mode: 'gateway', records: [], notice: '尚未接入后端服务。'};
      } else {
        result = await fetchJson(`${backend}-requests?limit=${$('requestLimit').value}${backend === 'caddy' && $('requestContainer').value.trim() ? `&container=${encodeURIComponent($('requestContainer').value.trim())}` : ''}`);
      }
      if (backend === 'nginx' && result.mode === 'none' && !sourceChosen) {
        try {
          const caddy = await fetchJson(`caddy-requests?limit=${$('requestLimit').value}`);
          if (caddy.container || caddy.candidates?.length) {
            backend = 'caddy';
            $('requestBackend').value = backend;
            result = caddy;
          }
        } catch (_) {
          // Keep the existing Nginx empty state when Docker is unavailable.
        }
      }
      if (version !== generation || !$('requestsView').classList.contains('active')) return;
      records = result.records || [];
      visitors = result.visitors || null;
      historyResult = result.history ? result : null;
      window.AppSelects?.syncAll();
      $('requestContainerField').hidden = backend !== 'caddy';
      if (backend === 'caddy') {
        const names = result.candidates || [];
        $('requestContainerOptions').replaceChildren(...names.map(name => {
          const option = document.createElement('option');
          option.value = name;
          return option;
        }));
        if (result.container && !$('requestContainer').value.trim()) $('requestContainer').value = result.container;
      }
      const entry = backend === 'gateway' ? window.RequestGateway.list().find(item => item.key === $('requestGatewayKey').value) : null;
      const collectionError = entry?.connection?.error || result.error;
      const attached = entry?.connection?.state === 'connected' && !collectionError;
      const needsRecovery = entry?.connection && !['connected', 'not_connected'].includes(entry.connection.state);
      source.textContent = backend === 'gateway'
        ? attached ? `${entry.connection.site} · ${entry.state === 'running' ? '已开启' : '采集服务未运行'} · ${entry.connection.scope || '已接入的转发规则'}`
          : entry?.connection && collectionError ? `${entry.connection.site} · 采集需检查`
            : needsRecovery ? `${entry.connection.site} · 接入操作待恢复`
            : entry ? `自定义入口 · ${entry.name} · 仅记录经过该入口的请求` : '尚未开启后端请求记录'
        : `正在查看已有${backend === 'caddy' ? ' Caddy' : ' Nginx'}日志${result.container ? ` · ${result.container}` : ''}`;
      $('requestStopMonitoring').hidden = !entry?.connection || entry.connection.state === 'not_connected';
      const repairable = ['changed', 'unavailable'].includes(entry?.connection?.config_state);
      $('requestStopMonitoring').textContent = repairable ? '自动修复' : attached ? '停止记录' : '恢复原入口';
      $('requestStopMonitoring').title = repairable ? '自动定位当前配置并恢复原规则，必要时重载或重启前置代理' : '';
      notice.textContent = result.notice || '';
      if (historyResult) {
        const checked = result.checked_at ? new Date(result.checked_at * 1000).toLocaleTimeString('zh-CN') : '';
        notice.textContent = `${checked ? `最近检查 ${checked}` : '等待首次后台采集'} · 最多保留 7 天（受容量限制）。`;
        notice.textContent += result.latest_at ? ` 最后一条请求：${new Date(result.latest_at * 1000).toLocaleString('zh-CN')}。` : ' 尚未保存请求。';
        if (collectionError) notice.textContent += ` ${collectionError}。`;
        if (result.checked_at && Date.now() / 1000 - result.checked_at > 60) notice.textContent += ' 采集延迟，当前统计可能不完整。';
        if (result.limited_at) notice.textContent += ' 曾达到单批采集上限，可能存在缺口。';
        if (result.pending) notice.textContent += ' 正在分批补采，统计会继续更新。';
        if (result.aggregate_mode) notice.textContent += ` ${result.notice}`;
        if (result.rollups_limited_at) notice.textContent += ' 统计汇总曾达到容量上限，较早部分已过期。';
        if (result.earliest_at) notice.textContent += ` 当前最早记录：${new Date(result.earliest_at * 1000).toLocaleString('zh-CN')}。`;
        if (!result.total_requests && result.retained_requests) notice.textContent += ` 当前时段无匹配记录，此入口仍保留 ${result.retained_requests} 条历史，可切换时段或筛选查看。`;
        if (!entry && $('requestGatewayKey').value) source.textContent = '已保存的历史记录 · 原采集入口不可用';
      }
      $('requestEnable').hidden = backend !== 'nginx' || (result.enabled && result.details_enabled !== false) || result.mode === 'none';
      $('requestEnable').textContent = result.enabled ? '升级日志详情' : '开启 Nginx 日志';
      $('requestDisable').hidden = backend !== 'nginx' || !result.enabled;
      render();
    } catch (err) {
      if (version === generation) { notice.textContent = `请求读取失败：${err.message}`; window.RequestMap?.error(err.message); }
    } finally {
      pending = false;
      $('requestRefresh').disabled = false;
      if (pendingRefresh) {
        pendingRefresh = false;
        refresh();
      }
    }
  }

  function changeSource() {
    sourceChosen = true;
    generation++;
    records = []; historyResult = null; historyPage = 0; render();
    source.textContent = '检查中';
    $('requestEnable').hidden = true;
    $('requestDisable').hidden = true;
    $('requestStopMonitoring').hidden = true;
    if (pending) pendingRefresh = true;
    else refresh();
  }

  $('requestRefresh').addEventListener('click', refresh);
  $('requestStopMonitoring').addEventListener('click', () => {
    const entry = window.RequestGateway.list().find(item => item.key === $('requestGatewayKey').value);
    if (entry?.connection && entry.connection.state !== 'not_connected') {
      if (['changed', 'unavailable'].includes(entry.connection.config_state)) window.GatewayConnect.repair(entry);
      else window.GatewayConnect.disconnect(entry);
    }
  });
  $('requestBackend').addEventListener('change', changeSource);
  $('requestContainer').addEventListener('change', changeSource);
  $('requestGatewayKey').addEventListener('change', changeSource);
  $('requestLimit').addEventListener('change', changeSource);
  function changeHistory(resetPage = true) {
    if ($('requestBackend').value !== 'gateway') { render(); return; }
    if (resetPage) historyPage = 0;
    generation++;
    records = []; historyResult = null; render();
    if (pending) pendingRefresh = true;
    else refresh();
  }
  $('requestSearch').addEventListener('input', () => {
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(changeHistory, 250);
  });
  $('requestStatus').addEventListener('change', () => { $('requestSlowField').hidden = $('requestStatus').value !== 'slow'; changeHistory(); });
  $('requestSlowMs').addEventListener('change', () => { if ($('requestSlowMs').reportValidity()) changeHistory(); });
  $('requestLayout').addEventListener('change', () => changeHistory());
  $('requestPeriod').addEventListener('change', () => changeHistory());
  $('requestHistoryPrevious').addEventListener('click', () => { if (historyPage) { historyPage--; changeHistory(false); } });
  $('requestHistoryNext').addEventListener('click', () => { if (historyResult?.has_more) { historyPage++; changeHistory(false); } });
  $('requestEnable').addEventListener('click', async () => {
    const confirmed = await showConfirmDialog({title: '开启 Nginx 请求记录',
      message: '将在当前选中的 Nginx 中新增或升级面板专用日志配置，先检查语法再重载。新请求将记录来源 IP 和常用请求头；站点自行覆盖 access_log 时可能不在列表中。',
      confirmText: '开启记录', danger: false});
    if (!confirmed) return;
    $('requestEnable').disabled = true;
    try {
      await postJsonBody('nginx-settings', {action: 'enable-request-logging'});
      await refresh();
    } catch (err) {
      notice.textContent = `开启失败：${err.message}`;
    } finally {
      $('requestEnable').disabled = false;
    }
  });
  $('requestDisable').addEventListener('click', async () => {
    const confirmed = await showConfirmDialog({title: '关闭 Nginx 请求记录',
      message: '将删除面板托管的请求日志配置并重新加载当前 Nginx；已写入的日志不会自动删除。',
      confirmText: '关闭记录', danger: false});
    if (!confirmed) return;
    $('requestDisable').disabled = true;
    try {
      await postJsonBody('nginx-settings', {action: 'disable-request-logging'});
      await refresh();
    } catch (err) {
      notice.textContent = `关闭失败：${err.message}`;
    } finally {
      $('requestDisable').disabled = false;
    }
  });
  window.setInterval(refresh, 10000);
  return {refresh, view: key => {
    $('requestBackend').value = 'gateway';
    $('requestGatewayKey').value = key;
    changeSource();
  }};
})();
