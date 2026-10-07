window.NginxRequests = (() => {
  let records = [], pending = false, generation = 0, sourceChosen = false, pendingRefresh = false;
  const source = $('requestSource'), notice = $('requestNotice'), rows = $('requestRows');
  const rendered = new WeakMap();

  function updateHtml(element, html) {
    if (rendered.get(element) === html) return;
    element.innerHTML = html;
    rendered.set(element, html);
  }

  function formatDuration(value, empty = '未记录') {
    if (!Number.isFinite(value) || value < 0) return empty;
    return value > 1000 ? `${(value / 1000).toFixed(2)} s` : `${value.toFixed(2)} ms`;
  }

  function groupRequests(items) {
    const groups = new Map();
    const ordered = items.slice().sort((a, b) => (Date.parse(b.at) || 0) - (Date.parse(a.at) || 0));
    for (const item of ordered) {
      const key = JSON.stringify([item.host || '', item.method || '', item.path || '']);
      if (!groups.has(key)) groups.set(key, {key, host: item.host || '', method: item.method || '', path: item.path || '',
        items: [], successes: 0, errors: 0, durationSum: 0, durationCount: 0, upstreamSum: 0, upstreamCount: 0});
      const group = groups.get(key);
      group.items.push(item);
      if (item.status >= 200 && item.status < 400) group.successes++;
      if (item.status >= 400) group.errors++;
      if (Number.isFinite(item.duration_ms) && item.duration_ms >= 0) {
        group.durationSum += item.duration_ms; group.durationCount++;
      }
      if (Number.isFinite(item.upstream_ms) && item.upstream_ms >= 0) {
        group.upstreamSum += item.upstream_ms; group.upstreamCount++;
      }
    }
    return [...groups.values()].map(group => ({...group, count: group.items.length,
      average: group.durationCount ? group.durationSum / group.durationCount : null,
      upstreamAverage: group.upstreamCount ? group.upstreamSum / group.upstreamCount : null,
      successRate: group.successes / group.items.length * 100}));
  }

  function render() {
    const term = $('requestSearch').value.trim().toLowerCase();
    const status = $('requestStatus').value;
    const groups = groupRequests(records).filter(group => {
      if (status === 'success' && group.successes !== group.count) return false;
      if (status === 'error' && !group.errors) return false;
      return !term || `${group.path} ${group.host}`.toLowerCase().includes(term);
    });
    $('requestSampleSummary').textContent = records.length
      ? `本次读取 ${records.length} 条 · 显示 ${groups.length} 个地址 · 2xx / 3xx 计为成功` : '';
    if (!groups.length) {
      rows.innerHTML = '<p class="request-empty">暂无匹配的请求</p>';
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
    const existing = new Map([...rows.querySelectorAll('.request-group')].map(element => [element.dataset.key, element]));
    groups.forEach((group, index) => {
      let element = existing.get(group.key);
      if (!element) {
        element = document.createElement('details');
        element.className = 'request-group';
        element.dataset.key = group.key;
        element.innerHTML = '<summary class="request-summary"></summary><div class="request-detail-list"></div>';
      }
      existing.delete(group.key);
      const samples = group.items.slice(0, 30).reverse();
      const bars = samples.map((item, position) => `<span class="request-tick ${item.status >= 200 && item.status < 400 ? 'is-ok' : item.status >= 400 ? 'is-error' : 'is-unknown'}" ${position === 0 ? `style="grid-column-start:${31 - samples.length}"` : ''} title="${escapeHtml(item.at || '-')} · ${escapeHtml(item.status)}"></span>`).join('');
      const summary = `<span class="request-route"><span class="request-route-top"><i class="request-expand" aria-hidden="true"></i><b>${escapeHtml(group.method)}</b><code>${escapeHtml(group.path)}</code></span><span class="request-route-host">${escapeHtml(group.host || '-')}</span></span>
        <span class="request-metric" data-label="次数">${group.count}</span>
        <span class="request-metric" data-label="平均耗时" title="${group.durationCount} 条有效耗时样本">${formatDuration(group.average)}</span>
        <span class="request-metric" data-label="平均上游" title="${group.upstreamCount} 条有效上游样本">${formatDuration(group.upstreamAverage, '-')}</span>
        <span class="request-reliability"><span class="request-spark" role="img" aria-label="最近 ${samples.length} 次请求，由旧到新">${bars}</span><strong class="${group.errors ? 'has-errors' : ''}" title="${group.successes} / ${group.count} 次成功">${group.successRate.toFixed(1)}%</strong></span>`;
      const summaryNode = element.querySelector('summary');
      updateHtml(summaryNode, summary);
      const detail = '<div class="request-detail-head"><span>时间</span><span>状态</span><span>耗时</span><span>上游</span></div>' + group.items.map(item =>
        `<div class="request-detail-row"><time>${escapeHtml(item.at || '-')}</time><strong class="${item.status >= 400 ? 'failed' : ''}">${escapeHtml(item.status)}</strong><span>${formatDuration(item.duration_ms)}</span><span>${formatDuration(item.upstream_ms, '-')}</span></div>`).join('');
      const detailNode = element.querySelector('.request-detail-list');
      updateHtml(detailNode, detail);
      if (rows.children[index + 1] !== element) rows.insertBefore(element, rows.children[index + 1] || null);
    });
    for (const element of existing.values()) element.remove();
  }

  async function refresh() {
    if (pending || document.hidden || !$('requestsView').classList.contains('active')) return;
    pending = true;
    $('requestRefresh').disabled = true;
    const version = ++generation;
    try {
      let backend = $('requestBackend').value;
      let result;
      $('requestGatewayField').hidden = backend !== 'gateway';
      $('requestContainerField').hidden = backend !== 'caddy';
      if (backend === 'gateway') {
        await window.RequestGateway.ensure();
        if (version !== generation) return;
        const key = $('requestGatewayKey').value;
        result = key ? await fetchJson(`gateway-requests?key=${encodeURIComponent(key)}&limit=${$('requestLimit').value}`)
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
      const attached = entry?.connection?.state === 'connected';
      const needsRecovery = entry?.connection && !['connected', 'not_connected'].includes(entry.connection.state);
      source.textContent = backend === 'gateway'
        ? attached ? `${entry.connection.site} · ${entry.state === 'running' ? '已开启' : '采集服务未运行'} · ${entry.connection.scope || '已接入的转发规则'}`
          : needsRecovery ? `${entry.connection.site} · 接入操作待恢复`
            : entry ? `自定义入口 · ${entry.name} · 仅记录经过该入口的请求` : '尚未开启后端请求记录'
        : `正在查看已有${backend === 'caddy' ? ' Caddy' : ' Nginx'}日志${result.container ? ` · ${result.container}` : ''}`;
      $('requestStopMonitoring').hidden = !entry?.connection || entry.connection.state === 'not_connected';
      $('requestStopMonitoring').textContent = attached ? '停止记录' : '恢复原入口';
      notice.textContent = result.notice || '';
      $('requestEnable').hidden = backend !== 'nginx' || result.enabled || result.mode === 'none';
      $('requestDisable').hidden = backend !== 'nginx' || !result.enabled;
      render();
    } catch (err) {
      if (version === generation) notice.textContent = `请求读取失败：${err.message}`;
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
    records = []; render();
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
    if (entry?.connection && entry.connection.state !== 'not_connected') window.GatewayConnect.disconnect(entry);
  });
  $('requestBackend').addEventListener('change', changeSource);
  $('requestContainer').addEventListener('change', changeSource);
  $('requestGatewayKey').addEventListener('change', changeSource);
  $('requestLimit').addEventListener('change', changeSource);
  $('requestSearch').addEventListener('input', render);
  $('requestStatus').addEventListener('change', render);
  $('requestEnable').addEventListener('click', async () => {
    const confirmed = await showConfirmDialog({title: '开启 Nginx 请求记录',
      message: '将在当前选中的 Nginx 中新增面板专用日志配置，先检查语法再重载。仅采集新请求；站点自行覆盖 access_log 时可能不在列表中。',
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
