(() => {
  'use strict';
  const $ = id => document.getElementById(id), dialog = document.createElement('dialog');
  dialog.className = 'ops-dialog server-performance-dialog'; dialog.id = 'serverPerformanceDialog'; dialog.setAttribute('aria-labelledby', 'serverPerformanceTitle');
  dialog.innerHTML = `<header class="ops-head"><div><h2 id="serverPerformanceTitle">服务器性能历史</h2><p>持续采集 · 关闭网页也会保存</p></div><button id="serverPerformanceClose" class="toolbar-icon" type="button" title="关闭性能历史" aria-label="关闭性能历史"><img src="ui/previews/ssh-ai/icons/x.svg" alt=""></button></header><div class="server-performance-toolbar"><div class="ops-tabs" role="group" aria-label="服务器性能范围"><button type="button" data-server-period="1h">1 小时</button><button type="button" data-server-period="24h">24 小时</button><button type="button" data-server-period="7d">7 天</button></div><div class="server-performance-actions"><button id="serverPerformanceRefresh" class="toolbar-icon" type="button" title="刷新性能历史" aria-label="刷新性能历史"><img src="ui/previews/ssh-ai/icons/refresh-cw.svg" alt=""></button><button id="serverPerformanceCsv" class="ghost-button compact-action" type="button">采样 CSV</button><button id="serverPerformanceExport" class="ghost-button compact-action" type="button"><img src="ui/previews/ssh-ai/icons/download.svg" alt="">导出报告</button></div></div><p id="serverPerformanceFeedback" class="ops-feedback" role="status"></p><iframe id="serverPerformanceFrame" title="服务器性能报告" sandbox="allow-scripts allow-modals" hidden></iframe>`;
  document.body.append(dialog);
  let period = '24h', controller, revision = 0, url = '', busy = false;
  function controls() {
    ['serverPerformanceRefresh', 'serverPerformanceCsv', 'serverPerformanceExport'].forEach(id => $(id).disabled = busy);
    dialog.querySelectorAll('[data-server-period]').forEach(button => { button.disabled = busy; button.setAttribute('aria-pressed', String(button.dataset.serverPeriod === period)); });
  }
  function clearFrame() { if (url) URL.revokeObjectURL(url); url = ''; $('serverPerformanceFrame').removeAttribute('src'); $('serverPerformanceFrame').hidden = true; }
  async function load(format = 'html') {
    if (busy && format !== 'html') return;
    const run = ++revision; controller?.abort(); controller = new AbortController(); busy = true; controls();
    if (format === 'html') clearFrame();
    $('serverPerformanceFeedback').textContent = format === 'html' ? '读取已保存的性能采样…' : '生成性能报告…';
    try {
      const parameters = new URLSearchParams({period, format, theme: format === 'html' ? document.documentElement.dataset.theme || 'light' : 'light'});
      if (format === 'html') parameters.set('preview', '1');
      const response = await fetch(`performance/server/export?${parameters}`, {credentials:'same-origin', signal:controller.signal});
      if (!response.ok) { const data = await response.json(); throw new Error(data.detail || '性能报告读取失败'); }
      const blob = await response.blob(); if (run !== revision || !dialog.open) return;
      const target = URL.createObjectURL(blob), count = response.headers.get('X-Performance-Samples') || '0';
      if (format === 'html') { url = target; $('serverPerformanceFrame').src = target; $('serverPerformanceFrame').hidden = false; }
      else {
        const link = document.createElement('a'); link.href = target;
        link.download = /filename="([^"]+)"/.exec(response.headers.get('Content-Disposition') || '')?.[1] || `mini-deploy-performance.${format}`;
        document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(target), 60000);
      }
      $('serverPerformanceFeedback').textContent = `${format === 'html' ? '已读取' : '已下载'} · ${count} 次有效采样${count === '0' ? ' · 采集通常在 30 秒内开始，过去的数据无法补回' : ' · 每 30 秒保存统计'}`;
    } catch (error) { if (run === revision && error.name !== 'AbortError') $('serverPerformanceFeedback').textContent = error.message; }
    finally { if (run === revision) { busy = false; controls(); } }
  }
  $('serverPerformanceOpen').addEventListener('click', () => { period = '24h'; dialog.showModal(); load(); });
  $('serverPerformanceClose').addEventListener('click', () => dialog.close());
  $('serverPerformanceRefresh').addEventListener('click', () => load());
  $('serverPerformanceExport').addEventListener('click', () => load('zip'));
  $('serverPerformanceCsv').addEventListener('click', () => load('csv'));
  window.addEventListener('message', async event => {
    const message = event.data, frame = $('serverPerformanceFrame');
    if (!dialog.open || event.source !== frame.contentWindow || message?.type !== 'performance-context-request') return;
    if (!/^[a-f0-9]{32}$/.test(message.run) || !Number.isSafeInteger(message.ts) || typeof message.token !== 'string' || message.token.length > 80) return;
    const version = revision, source = event.source;
    try {
      const response = await fetch(`performance/server/context?${new URLSearchParams({run:message.run, ts:message.ts})}`, {credentials:'same-origin', signal:controller?.signal});
      const body = await response.json();
      if (dialog.open && version === revision) source.postMessage({type:'performance-context-response', token:message.token, scene:response.ok ? body : null, error:response.ok ? '' : body.detail || '现场读取失败'}, '*');
    } catch (error) {
      if (dialog.open && version === revision && error.name !== 'AbortError') source.postMessage({type:'performance-context-response', token:message.token, error:'现场读取失败，请重试。'}, '*');
    }
  });
  dialog.addEventListener('click', event => { const button = event.target.closest('[data-server-period]'); if (button) { period = button.dataset.serverPeriod; load(); } });
  dialog.addEventListener('close', () => { revision++; controller?.abort(); busy = false; clearFrame(); });
})();
