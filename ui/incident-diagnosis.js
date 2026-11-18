/* Reports are produced by the server; closing the browser never stops diagnosis. */
(() => {
  const el = id => document.getElementById(id);
  const labels = {queued:'等待诊断', running:'正在排查', paused:'已暂停', done:'报告已完成', failed:'诊断失败', incomplete:'报告未完成', pending:'等待发送', sending:'发送中', sent:'渠道已确认', cancelled:'已停止'};
  const channels = {wecom:'企业微信', dingtalk:'钉钉', email:'邮件'};
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const date = value => value ? new Date(value * 1000).toLocaleString() : '尚未尝试';
  let page = 0, loading = false, busy = false, timer, selected = '', detailGeneration = 0, selectedRevision = null, enabled = false;
  const dialog = el('diagnosisDialog');
  function render(data) {
    enabled = data.enabled;
    el('diagnosisEnabled').checked = data.enabled;
    el('diagnosisScope').textContent = data.target || data.target_scope;
    el('diagnosisReadiness').textContent = [data.enabled ? '已开启' : '已关闭', data.model_configured ? '模型已配置' : '请先配置模型', data.notifications_enabled ? '完整报告通知已开启' : '报告保存在面板，通知未开启'].join(' · ');
    el('diagnosisFeedback').textContent = data.error || '';
    el('diagnosisList').innerHTML = (data.items || []).map(row => `<div class="diagnosis-row"><div><strong>${escape(row.title)}</strong><p><span class="diagnosis-state" data-state="${escape(row.state)}">${escape(labels[row.state] || row.state)}</span> · ${escape(date(row.updated))}${row.revision ? ` · v${row.revision}` : ''}<br>${row.delivery.map(d => `${escape(channels[d.channel] || d.channel)} ${d.sent}/${d.total}${d.failed ? ' · 失败重试中' : d.total && d.sent === d.total ? ' · 已确认' : ''}`).join('；') || '未配置通知渠道'}${row.detail ? `<br>${escape(row.detail)}` : ''}</p></div><button type="button" class="ghost-button compact-action" data-diagnosis="${escape(row.id)}">${row.revision ? '查看报告' : '查看进度'}</button></div>`).join('') || '<p class="monitor-feedback">暂无自动诊断记录。开启后，当前有效异常和新确认异常会进入诊断队列。</p>';
    el('diagnosisPageInfo').textContent = `${data.total} 项 · 第 ${data.page + 1} 页`;
    el('diagnosisPrev').disabled = data.page === 0;
    el('diagnosisNext').disabled = !data.has_more;
  }
  function schedule() {
    clearTimeout(timer);
    if (!document.hidden && (activeView === 'events' || dialog.open)) timer = setTimeout(refresh, 5000);
  }
  async function refresh() {
    if (loading || busy) { schedule(); return; }
    loading = true;
    try {
      render(await fetchJson(`incident-diagnosis?page=${page}`));
      if (dialog.open && selected) await report(selected, selectedRevision);
    } catch (error) { el('diagnosisFeedback').textContent = `诊断状态读取失败：${error.message}，稍后重试。`; }
    finally { loading = false; schedule(); }
  }
  async function change(data) {
    if (busy) return;
    busy = true;
    el('diagnosisEnabled').disabled = true;
    el('diagnosisRetry').disabled = true;
    try {
      render(await postJsonBody('incident-diagnosis', data));
      page = 0;
      if (dialog.open && selected) await report(selected, null);
    } catch (error) {
      el('diagnosisFeedback').textContent = `操作失败：${error.message}`;
      if (dialog.open) el('diagnosisReportStatus').textContent = `操作失败：${error.message}`;
      el('diagnosisEnabled').checked = enabled;
    } finally { busy = false; el('diagnosisEnabled').disabled = false; schedule(); }
  }
  async function report(key, revision = null) {
    const generation = ++detailGeneration;
    try {
      const data = await fetchJson(`incident-diagnosis/report?id=${encodeURIComponent(key)}${revision ? `&revision=${revision}` : ''}`);
      if (generation !== detailGeneration || !dialog.open) return;
      const r = data.report;
      el('diagnosisReportTitle').textContent = data.incident.title || '异常诊断报告';
      el('diagnosisReportStatus').textContent = `${labels[data.state] || data.state}${r ? ` · ${r.complete ? '六节报告完整' : '部分结果，诊断未完成'} · ${{confirmed:'证据支持根因判断',suspected:'可能原因，尚待确认',unknown:'根因尚未确认'}[r.confidence]}` : ' · 尚未生成报告'}`;
      const versions = data.versions.map(v => `<option value="${v.revision}">v${v.revision} · ${escape(date(v.created))}</option>`).join('');
      if (el('diagnosisVersion').innerHTML !== versions) el('diagnosisVersion').innerHTML = versions;
      el('diagnosisVersion').value = r?.revision || '';
      el('diagnosisVersion').disabled = !r;
      window.AppSelects?.syncAll();
      el('diagnosisRetry').disabled = !enabled || ['running', 'queued'].includes(data.state);
      const signature = `${key}:${r?.revision || 0}`;
      if (el('diagnosisReportBody').dataset.signature !== signature) {
        el('diagnosisReportBody').innerHTML = r ? DOMPurify.sanitize(marked.parse(r.body), {
          ALLOWED_TAGS:['h1','h2','h3','h4','p','ul','ol','li','a','pre','code','strong','em','table','thead','tbody','tr','th','td','blockquote','hr','br'],
          ALLOWED_ATTR:['href','title']
        }) : '<p>诊断在服务器后台运行。你可以关闭此窗口，稍后查看报告。</p>';
        el('diagnosisReportBody').dataset.signature = signature;
        el('diagnosisTrace').replaceChildren();
        for (const event of r?.trace || []) {
          const details = document.createElement('details'), summary = document.createElement('summary'), pre = document.createElement('pre');
          summary.textContent = `${event.title} · ${event.status}`;
          pre.textContent = [event.command, event.reason, event.result].filter(Boolean).join('\n\n');
          details.append(summary, pre); el('diagnosisTrace').append(details);
        }
      }
      el('diagnosisDownload').removeAttribute('href');
      if (r) el('diagnosisDownload').href = `incident-diagnosis/report?id=${encodeURIComponent(key)}&revision=${r.revision}&download=md`;
      el('diagnosisDelivery').innerHTML = data.delivery.map(d => `<p>${escape(channels[d.channel] || d.channel)} · 第 ${d.part}/${d.total} 条 · ${escape(labels[d.status] || d.status)} · 尝试 ${d.attempts} 次<br>${escape(date(d.attempt_at))}${d.detail ? `<br>${escape(d.detail)}` : ''}</p>`).join('') || '<p>尚无通知送达记录。请核对通知设置；报告生成后会自动投递。</p>';
    } catch (error) {
      if (generation === detailGeneration) { el('diagnosisReportStatus').textContent = `报告读取失败：${error.message}`; el('diagnosisRetry').disabled = true; }
    }
  }
  el('diagnosisEnabled').addEventListener('change', () => change({action:'configure', enabled:el('diagnosisEnabled').checked}));
  el('diagnosisModel').addEventListener('click', () => window.TerminalAI.openSettings());
  el('diagnosisNotify').addEventListener('click', () => setView('notify'));
  el('diagnosisList').addEventListener('click', event => {
    const button = event.target.closest('[data-diagnosis]'); if (!button) return;
    selected = button.dataset.diagnosis; selectedRevision = null;
    el('diagnosisReportBody').textContent = '正在读取报告'; el('diagnosisReportBody').dataset.signature = '';
    el('diagnosisTrace').replaceChildren(); el('diagnosisDelivery').replaceChildren();
    el('diagnosisReportTitle').textContent = '异常诊断报告'; el('diagnosisReportStatus').textContent = '正在读取';
    el('diagnosisRetry').disabled = true; el('diagnosisDownload').removeAttribute('href');
    dialog.showModal(); report(selected); schedule();
  });
  el('diagnosisClose').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => { selected = ''; detailGeneration++; schedule(); });
  el('diagnosisVersion').addEventListener('change', () => { selectedRevision = Number(el('diagnosisVersion').value); report(selected, selectedRevision); });
  el('diagnosisRetry').addEventListener('click', () => { selectedRevision = null; change({action:'retry', id:selected}); });
  el('diagnosisPrev').addEventListener('click', () => { page = Math.max(0, page - 1); refresh(); });
  el('diagnosisNext').addEventListener('click', () => { page++; refresh(); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden && (activeView === 'events' || dialog.open)) refresh(); else schedule(); });
  window.IncidentDiagnosis = {refresh, viewChanged: () => { if (activeView === 'events') refresh(); else schedule(); }};
  if (activeView === 'events') refresh();
})();
