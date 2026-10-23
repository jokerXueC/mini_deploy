(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const dialog = document.createElement('dialog'); dialog.id = 'requestExportDialog'; dialog.className = 'request-export-dialog'; dialog.setAttribute('aria-labelledby', 'requestExportTitle');
  dialog.innerHTML = `<header><div><h2 id="requestExportTitle">导出请求记录</h2><p id="requestExportSource"></p></div><button id="requestExportClose" class="toolbar-icon" type="button" title="关闭导出" aria-label="关闭导出"><img src="ui/previews/ssh-ai/icons/x.svg" alt=""></button></header>
    <form id="requestExportForm"><label>导出范围<select id="requestExportScope"><option value="all">全部入口 · 全部已保留历史</option><option value="source">当前入口 · 全部已保留历史</option><option value="filtered">当前入口 · 当前时间和筛选</option></select></label><p id="requestExportRange" class="muted"></p>
    <fieldset><legend>文件内容</legend><label><input type="radio" name="requestExportFormat" value="zip" checked><span><strong>分析包 ZIP</strong><small>完整请求明细、地址统计、时间趋势与图表报告</small></span></label><label><input type="radio" name="requestExportFormat" value="csv"><span><strong>明细 CSV</strong><small>可用 Excel 打开，每条请求一行</small></span></label><label><input type="radio" name="requestExportFormat" value="html"><span><strong>分析报告 HTML</strong><small>离线图表与地址汇总，支持筛选和排序</small></span></label></fieldset>
    <p id="requestExportFeedback" role="status" class="muted"></p><div class="request-export-actions"><button id="requestExportPreview" class="ghost-button" type="button">预览报告</button><button id="requestExportDownload" class="ghost-button ssh-primary" type="submit"><img src="ui/previews/ssh-ai/icons/download.svg" alt="">下载</button></div></form>
    <iframe id="requestExportFrame" title="请求分析报告预览" sandbox="allow-scripts allow-modals" hidden></iframe>`;
  document.body.append(dialog);
  let controller, busy = false, snapshot = null, previewUrl = '', revision = 0;
  const options = () => ({backend: $('requestBackend').value, key: $('requestGatewayKey').value,
    period: $('requestPeriod').value, layout: $('requestLayout').value, status: $('requestStatus').value,
    search: $('requestSearch').value.trim(), slow_ms: $('requestSlowMs').value || '1000',
    limit: $('requestLimit').value, container: $('requestContainer').value.trim()});
  function clearPreview() {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = ''; $('requestExportFrame').removeAttribute('src'); $('requestExportFrame').hidden = true; dialog.classList.remove('with-preview');
  }
  function scopeHint() {
    clearPreview();
    const scope = $('requestExportScope').value, historic = snapshot.backend === 'gateway';
    const range = historic ? scope === 'filtered' ? $('requestPeriod').selectedOptions[0].textContent : '全部仍保留的数据，最多 7 天' : `近期日志样本，最多 ${snapshot.limit} 条`;
    const filter = scope === 'filtered' ? ` · ${$('requestStatus').selectedOptions[0].textContent}${snapshot.search ? ' · 查找：' + snapshot.search : ''}` : '';
    $('requestExportRange').textContent = `${range}${filter}。${historic ? '导出覆盖所有分页和全部明细。' : '已有代理日志不保存完整历史。'}${scope === 'filtered' ? '筛选按整类地址导出，包含该类的全部明细。' : ''}`;
    $('requestExportFeedback').textContent = '时间使用 UTC；不包含认证头、Cookie、查询参数或请求体。未筛选的报告与 ZIP 另含独立保留统计。';
  }
  function controls() {
    ['requestExportPreview', 'requestExportDownload', 'requestExportScope'].forEach(id => $(id).disabled = busy);
    dialog.querySelectorAll('[name=requestExportFormat]').forEach(input => { input.disabled = busy; }); window.AppSelects?.syncAll();
  }
  $('requestExportOpen').addEventListener('click', () => {
    snapshot = options(); const historic = snapshot.backend === 'gateway', scope = $('requestExportScope');
    scope.options[0].hidden = !historic;
    scope.options[1].textContent = historic ? '当前入口 · 全部已保留历史' : '当前来源 · 全部近期样本';
    scope.options[2].textContent = historic ? '当前入口 · 当前时间和筛选' : '当前来源 · 当前筛选样本';
    scope.value = historic ? 'all' : 'source'; scope.options[1].disabled = scope.options[2].disabled = historic && !snapshot.key;
    $('requestExportSource').textContent = historic ? '后端请求 · 持久历史' : `${snapshot.backend.toUpperCase()} · 日志样本`;
    dialog.showModal(); scopeHint(); controls();
  });
  $('requestExportScope').addEventListener('change', scopeHint);
  async function generate(preview = false) {
    if (busy || !snapshot) return;
    const run = ++revision; controller = new AbortController(); busy = true; controls(); clearPreview();
    const format = preview ? 'html' : dialog.querySelector('[name=requestExportFormat]:checked').value;
    const parameters = new URLSearchParams({...snapshot, scope: $('requestExportScope').value, format});
    $('requestExportFeedback').textContent = '正在读取完整记录并生成文件…';
    try {
      const response = await fetch(`request-export?${parameters}`, {credentials:'same-origin', signal:controller.signal});
      if (!response.ok) { const error = await response.json(); throw new Error(error.detail || `导出失败（${response.status}）`); }
      const blob = await response.blob(); if (run !== revision || !dialog.open) return;
      const count = response.headers.get('X-Request-Export-Count') || '0', url = URL.createObjectURL(blob);
      if (preview) { previewUrl = url; $('requestExportFrame').src = url; $('requestExportFrame').hidden = false; dialog.classList.add('with-preview'); }
      else {
        const link = document.createElement('a'); link.href = url;
        link.download = /filename="([^"]+)"/.exec(response.headers.get('Content-Disposition') || '')?.[1] || `mini-deploy-requests.${format}`;
        document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 60000);
      }
      $('requestExportFeedback').textContent = `${preview ? '已生成报告' : '已下载'} · ${count} 条请求${snapshot.backend !== 'gateway' ? '（近期样本）' : ''}${count === '0' ? ' · 此范围没有已保存记录' : ''}`;
    } catch (error) { if (run === revision && error.name !== 'AbortError') $('requestExportFeedback').textContent = error.message; }
    finally { if (run === revision) { busy = false; controls(); } }
  }
  $('requestExportPreview').addEventListener('click', () => generate(true));
  $('requestExportForm').addEventListener('submit', event => { event.preventDefault(); generate(); });
  $('requestExportClose').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => { revision++; controller?.abort(); busy = false; clearPreview(); });
})();
