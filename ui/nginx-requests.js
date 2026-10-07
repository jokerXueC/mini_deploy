window.NginxRequests = (() => {
  let records = [], pending = false, generation = 0, sourceChosen = false, pendingRefresh = false;
  const source = $('requestSource'), notice = $('requestNotice'), rows = $('requestRows');

  function render() {
    const term = $('requestSearch').value.trim().toLowerCase();
    const status = $('requestStatus').value;
    const visible = records.filter(item => {
      if (status === 'success' && (item.status < 200 || item.status >= 400)) return false;
      if (status === 'error' && item.status < 400) return false;
      return !term || `${item.path} ${item.host}`.toLowerCase().includes(term);
    });
    if (!visible.length) {
      rows.innerHTML = '<p class="request-empty">暂无匹配的请求</p>';
      return;
    }
    rows.innerHTML = `<div class="request-row request-columns" aria-hidden="true"><span>时间</span><span>状态</span><span>方法</span><span>请求路径</span><span>域名</span><span>总耗时</span><span>上游</span></div>` + visible.map(item =>
      `<div class="request-row"><time>${escapeHtml(item.at || '-')}</time><strong class="request-status ${item.status >= 400 ? 'failed' : ''}">${escapeHtml(item.status)}</strong><span>${escapeHtml(item.method)}</span><code title="${escapeHtml(item.path)}">${escapeHtml(item.path)}</code><span class="request-host">${escapeHtml(item.host)}</span><span>${item.duration_ms == null ? '未记录' : `${escapeHtml(item.duration_ms)} ms`}</span><span>${item.upstream_ms == null ? '-' : `${escapeHtml(item.upstream_ms)} ms`}</span></div>`).join('');
  }

  async function refresh() {
    if (pending || document.hidden || !$('requestsView').classList.contains('active')) return;
    pending = true;
    $('requestRefresh').disabled = true;
    const version = ++generation;
    try {
      let backend = $('requestBackend').value;
      let result = await fetchJson(`${backend}-requests?limit=${$('requestLimit').value}${backend === 'caddy' && $('requestContainer').value.trim() ? `&container=${encodeURIComponent($('requestContainer').value.trim())}` : ''}`);
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
      source.textContent = backend === 'caddy' ? `Docker Caddy${result.container ? ` · ${result.container}` : ''}` : result.mode === 'docker' ? `Docker Nginx · ${result.container}` : result.mode === 'local' ? '本机 Nginx' : '尚未接入 Nginx';
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
    if (pending) pendingRefresh = true;
    else refresh();
  }

  $('requestRefresh').addEventListener('click', refresh);
  $('requestBackend').addEventListener('change', changeSource);
  $('requestContainer').addEventListener('change', changeSource);
  $('requestLimit').addEventListener('change', refresh);
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
  return {refresh};
})();
