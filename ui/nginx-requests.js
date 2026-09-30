window.NginxRequests = (() => {
  let records = [], pending = false, generation = 0, timer = null;
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
      const result = await fetchJson(`nginx-requests?limit=${$('requestLimit').value}`);
      if (version !== generation || !$('requestsView').classList.contains('active')) return;
      records = result.records || [];
      source.textContent = result.mode === 'docker' ? `Docker · ${result.container}` : result.mode === 'local' ? '服务器本机' : '尚未接入';
      notice.textContent = result.notice || '';
      $('requestEnable').hidden = result.enabled || result.mode === 'none';
      $('requestDisable').hidden = !result.enabled;
      render();
    } catch (err) {
      if (version === generation) notice.textContent = `请求读取失败：${err.message}`;
    } finally {
      pending = false;
      $('requestRefresh').disabled = false;
    }
  }

  $('requestRefresh').addEventListener('click', refresh);
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
  timer = window.setInterval(refresh, 10000);
  return {refresh};
})();
