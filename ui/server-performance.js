(() => {
  'use strict';
  const $ = id => document.getElementById(id), dialog = document.createElement('dialog');
  dialog.className = 'ops-dialog server-performance-dialog'; dialog.id = 'serverPerformanceDialog'; dialog.setAttribute('aria-labelledby', 'serverPerformanceTitle');
  dialog.innerHTML = `<header class="ops-head"><div><h2 id="serverPerformanceTitle">服务器性能历史</h2><p>持续采集 · 关闭网页也会保存</p></div><button id="serverPerformanceClose" class="toolbar-icon" type="button" title="关闭性能历史" aria-label="关闭性能历史"><img src="ui/previews/ssh-ai/icons/x.svg" alt=""></button></header><div class="server-performance-toolbar"><div class="ops-tabs" role="group" aria-label="服务器性能范围"><button type="button" data-server-period="1h">1 小时</button><button type="button" data-server-period="24h">24 小时</button><button type="button" data-server-period="7d">7 天</button></div><div class="server-performance-actions"><button id="serverPerformanceRefresh" class="toolbar-icon" type="button" title="刷新性能历史" aria-label="刷新性能历史"><img src="ui/previews/ssh-ai/icons/refresh-cw.svg" alt=""></button><button id="serverPerformanceCsv" class="ghost-button compact-action" type="button">采样 CSV</button><button id="serverPerformanceExport" class="ghost-button compact-action" type="button"><img src="ui/previews/ssh-ai/icons/download.svg" alt="">导出报告</button></div></div><p id="serverPerformanceFeedback" class="ops-feedback" role="status"></p><iframe id="serverPerformanceFrame" title="服务器性能报告" sandbox="allow-scripts allow-modals" hidden></iframe>`;
  const automatic = document.createElement('label'); automatic.className = 'server-performance-auto';
  automatic.innerHTML = '<input id="serverPerformanceAuto" type="checkbox" checked> 自动更新';
  dialog.querySelector('.server-performance-actions').prepend(automatic);
  document.body.append(dialog);
  const aiAnalyze = document.createElement('button'); aiAnalyze.type = 'button'; aiAnalyze.id = 'serverPerformanceAI';
  aiAnalyze.className = 'ghost-button compact-action'; aiAnalyze.textContent = 'AI 分析';
  dialog.querySelector('.server-performance-actions').append(aiAnalyze);
  aiAnalyze.addEventListener('click', () => { const selected = period; dialog.close(); window.PanelAI.open({kind:'performance', period:selected}); });
  let period = '24h', controller, revision = 0, requestVersion = 0, url = '', busy = false;
  let ready = false, refreshing = false, timer, liveController, contextController, etag = '';
  let retryInitial = false, failures = 0;
  async function requestHistory(path, options = {}, format = 'json') {
    const abort = new AbortController();
    const cancel = () => abort.abort();
    options.signal?.addEventListener('abort', cancel, {once:true});
    if (options.signal?.aborted) cancel();
    let timedOut = false;
    const timeout = setTimeout(() => { timedOut = true; abort.abort(); }, format === 'blob' ? 60000 : 30000);
    try {
      const response = await fetch(path, {...options, credentials:'same-origin', cache:'no-store', signal:abort.signal});
      if (response.status === 304) return {response};
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        const error = new Error(response.status === 401 ? '登录已过期，请刷新页面重新登录。' : body.detail || `性能历史请求失败（HTTP ${response.status}）。`);
        error.retry = response.status === 409 || response.status === 429 || response.status >= 500;
        throw error;
      }
      return {response, data:format === 'blob' ? await response.blob() : await response.json()};
    } catch (error) {
      if (timedOut) throw Object.assign(new Error('性能历史读取超时，请检查服务器连接或稍后重试。'), {retry:true});
      if (error.name === 'AbortError' || typeof error.retry === 'boolean') throw error;
      const message = error instanceof SyntaxError ? '服务器返回的数据不完整，请稍后重试。'
        : navigator.onLine === false ? '网络已断开，恢复连接后会自动重试。'
        : '性能历史传输失败，连接可能中断或响应未接收完整。';
      throw Object.assign(new Error(message), {retry:true});
    } finally {
      clearTimeout(timeout); options.signal?.removeEventListener('abort', cancel);
    }
  }
  function controls() {
    ['serverPerformanceCsv', 'serverPerformanceExport'].forEach(id => $(id).disabled = busy);
    $('serverPerformanceRefresh').disabled = busy || refreshing;
    dialog.querySelectorAll('[data-server-period]').forEach(button => { button.disabled = busy; button.setAttribute('aria-pressed', String(button.dataset.serverPeriod === period)); });
  }
  function stopAutomatic() { clearTimeout(timer); liveController?.abort(); }
  function schedule() {
    clearTimeout(timer);
    if (dialog.open && (ready || retryInitial) && !busy && !refreshing && !document.hidden && navigator.onLine !== false && $('serverPerformanceAuto').checked) {
      const delay = retryInitial ? Math.min(30000, 3000 * 2 ** Math.min(4, failures - 1)) : 15000;
      timer = setTimeout(() => retryInitial ? load() : refreshData(), delay);
    }
  }
  function clearFrame() {
    stopAutomatic(); contextController?.abort(); contextController = new AbortController(); ready = false; etag = ''; revision++;
    if (url) URL.revokeObjectURL(url); url = ''; $('serverPerformanceFrame').removeAttribute('src'); $('serverPerformanceFrame').hidden = true;
  }
  async function refreshData(force = false) {
    clearTimeout(timer);
    if (!dialog.open || busy || refreshing) return;
    if (!ready) { if (force || retryInitial) load(); return; }
    if (!force && (document.hidden || !$('serverPerformanceAuto').checked)) return;
    const version = revision, requestedPeriod = period;
    liveController?.abort(); const request = liveController = new AbortController();
    refreshing = true; controls();
    try {
      const {response, data} = await requestHistory(`performance/server?${new URLSearchParams({period:requestedPeriod, live:'1'})}`, {signal:request.signal, headers:etag ? {'If-None-Match':etag} : {}});
      if (!dialog.open || version !== revision || request.signal.aborted || busy) return;
      if (response.status === 304) {
        if (force) $('serverPerformanceFeedback').textContent = '已检查，暂无新增的已保存采样。';
        return;
      }
      if (!dialog.open || version !== revision || request.signal.aborted || busy) return;
      if (data.period !== requestedPeriod || !Array.isArray(data.points) || !Array.isArray(data.metrics) || !data.summary || typeof data.summary !== 'object' || !Number.isFinite(data.sample_seconds)) throw new Error('性能历史数据格式异常');
      etag = response.headers.get('ETag') || '';
      $('serverPerformanceFrame').contentWindow.postMessage({type:'performance-report-update', data}, '*');
      $('serverPerformanceFeedback').textContent = `已更新 ${new Date().toLocaleTimeString('zh-CN', {hour12:false})} · ${data.sample_seconds} 次有效采样 · 每 30 秒保存统计`;
    } catch (error) {
      if (version === revision && error.name !== 'AbortError') $('serverPerformanceFeedback').textContent = `${error.message.replace(/[。；]$/, '')}；保留当前图表，稍后重试。`;
    } finally { if (liveController === request) { refreshing = false; controls(); schedule(); } }
  }
  async function load(format = 'html') {
    if (busy && format !== 'html') return;
    const run = ++requestVersion; stopAutomatic(); controller?.abort(); controller = new AbortController(); busy = true; controls();
    if (format === 'html') { retryInitial = false; clearFrame(); }
    $('serverPerformanceFeedback').textContent = format === 'html' ? '读取已保存的性能采样…' : '生成性能报告…';
    try {
      const parameters = new URLSearchParams({period, format, theme: format === 'html' ? document.documentElement.dataset.theme || 'light' : 'light'});
      if (format === 'html') parameters.set('preview', '1');
      const {response, data:blob} = await requestHistory(`performance/server/export?${parameters}`, {signal:controller.signal}, 'blob');
      if (run !== requestVersion || !dialog.open) return;
      const target = URL.createObjectURL(blob), count = response.headers.get('X-Performance-Samples') || '0';
      if (format === 'html') { failures = 0; url = target; $('serverPerformanceFrame').src = target; $('serverPerformanceFrame').hidden = false; }
      else {
        const link = document.createElement('a'); link.href = target;
        link.download = /filename="([^"]+)"/.exec(response.headers.get('Content-Disposition') || '')?.[1] || `mini-deploy-performance.${format}`;
        document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(target), 60000);
      }
      $('serverPerformanceFeedback').textContent = `${format === 'html' ? '已读取' : '已下载'} · ${count} 次有效采样${count === '0' ? ' · 采集通常在 30 秒内开始，过去的数据无法补回' : ' · 每 30 秒保存统计'}`;
    } catch (error) {
      if (run === requestVersion && error.name !== 'AbortError') {
        retryInitial = format === 'html' && error.retry !== false;
        if (retryInitial) failures++;
        $('serverPerformanceFeedback').textContent = error.message + (retryInitial ? ' 历史数据保留，自动更新开启时会重试，也可点击刷新。' : '');
      }
    }
    finally { if (run === requestVersion) { busy = false; controls(); schedule(); } }
  }
  $('serverPerformanceOpen').addEventListener('click', () => { period = '24h'; window.ServerOverview?.pause(); dialog.showModal(); load(); });
  $('serverPerformanceClose').addEventListener('click', () => dialog.close());
  $('serverPerformanceRefresh').addEventListener('click', () => refreshData(true));
  $('serverPerformanceExport').addEventListener('click', () => load('zip'));
  $('serverPerformanceCsv').addEventListener('click', () => load('csv'));
  window.addEventListener('message', async event => {
    const message = event.data, frame = $('serverPerformanceFrame');
    if (!dialog.open || event.source !== frame.contentWindow) return;
    if (message?.type === 'performance-report-ready' && message.period === period) { ready = true; schedule(); return; }
    if (message?.type !== 'performance-context-request') return;
    if (!/^[a-f0-9]{32}$/.test(message.run) || !Number.isSafeInteger(message.ts) || typeof message.token !== 'string' || message.token.length > 80) return;
    const version = revision, source = event.source;
    try {
      const {data:body} = await requestHistory(`performance/server/context?${new URLSearchParams({run:message.run, ts:message.ts})}`, {signal:contextController?.signal});
      if (dialog.open && version === revision) source.postMessage({type:'performance-context-response', token:message.token, scene:body, error:''}, '*');
    } catch (error) {
      if (dialog.open && version === revision && error.name !== 'AbortError') source.postMessage({type:'performance-context-response', token:message.token, error:error.message}, '*');
    }
  });
  dialog.addEventListener('click', event => { const button = event.target.closest('[data-server-period]'); if (button) { period = button.dataset.serverPeriod; load(); } });
  $('serverPerformanceAuto').addEventListener('change', () => { if ($('serverPerformanceAuto').checked) refreshData(); else { stopAutomatic(); $('serverPerformanceFeedback').textContent = '自动更新已暂停，服务器仍持续采集。'; } });
  document.addEventListener('visibilitychange', () => { if (document.hidden) stopAutomatic(); else if (dialog.open && $('serverPerformanceAuto').checked) refreshData(); });
  window.addEventListener('offline', stopAutomatic);
  window.addEventListener('online', () => { if (dialog.open && $('serverPerformanceAuto').checked) refreshData(true); });
  dialog.addEventListener('close', () => {
    requestVersion++; controller?.abort(); busy = false; retryInitial = false; clearFrame();
    document.dispatchEvent(new Event('server-observation-change'));
  });
})();
