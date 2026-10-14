(() => {
  'use strict';
  const dialog = document.createElement('dialog'); dialog.id = 'panelAIDialog'; dialog.className = 'ops-dialog panel-ai-dialog';
  dialog.setAttribute('aria-labelledby', 'panelAITitle');
  dialog.innerHTML = `<header class="ops-head"><div><h2 id="panelAITitle">AI 运维诊断</h2><p id="panelAITarget">面板所在服务器 · 只读诊断</p></div><button id="panelAIClose" type="button" class="toolbar-icon" aria-label="关闭 AI 诊断" title="关闭 AI 诊断"><img src="ui/previews/ssh-ai/icons/x.svg" alt=""></button></header>
    <div class="panel-ai-toolbar"><p id="panelAIScope"></p><button id="panelAIHistory" class="ghost-button compact-action" type="button">历史记录</button><button id="panelAISettings" class="ghost-button compact-action" type="button">模型设置</button></div>
    <div id="panelAIHistoryList" class="panel-ai-history" hidden></div>
    <div id="panelAIHttp" class="panel-ai-http" hidden><label><input id="panelAIHttpConsent" type="checkbox"> 我了解 HTTP 明文传输风险，允许本次登录使用 AI</label><button id="panelAIHttpAllow" class="ghost-button compact-action" type="button" disabled>允许本次登录</button></div>
    <div id="panelAIMessages" class="panel-ai-messages" aria-live="polite"></div>
    <form id="panelAIForm" class="panel-ai-compose"><textarea id="panelAIQuestion" aria-label="运维诊断问题" placeholder="继续追问，例如：哪个进程占用最多？" maxlength="2000" required></textarea><div class="panel-ai-bottom"><p id="panelAIFeedback" role="status"></p><button id="panelAIStop" class="ghost-button compact-action" type="button" hidden>停止</button><button id="panelAISend" class="ghost-button compact-action" type="submit">发送</button></div></form>`;
  document.body.append(dialog);
  const $ = id => document.getElementById(id);
  let selection = {kind:'overview'}, settings = {}, token = '', conversation = '', history = '', job = '', busy = false, generation = 0;
  let historyOffset = 0, historyBefore = null, currentHistory = '', historyBusy = false;
  function controls() {
    $('panelAISend').disabled = busy || historyBusy; $('panelAIStop').hidden = !busy;
    $('panelAIHistory').disabled = busy || historyBusy; $('panelAISettings').disabled = busy;
  }
  async function api(path, data) {
    const response = await fetch(`/terminal-ai/${path}`, {method:data ? 'POST' : 'GET', credentials:'same-origin',
      headers:data ? {'Content-Type':'application/json', 'X-CSRF-Token':token} : {}, body:data ? JSON.stringify(data) : undefined});
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || result.error || '诊断请求失败');
    return result;
  }
  async function refreshSettings() {
    settings = await api('settings'); token = settings.csrf_token;
    $('panelAIHttp').hidden = !!(settings.secure || settings.http_enabled);
    $('panelAIScope').textContent = settings.configured ? `只读证据与问题发送至 ${settings.base_url} · ${settings.model}` : '请先配置模型，再发送诊断问题';
  }
  function entry(question) {
    const node = document.createElement('section'); node.className = 'panel-ai-entry';
    const title = document.createElement('p'); title.className = 'panel-ai-question'; title.textContent = question;
    const progress = document.createElement('div');
    const answer = document.createElement('div'); answer.className = 'tai-answer';
    node.append(title, progress, answer); $('panelAIMessages').append(node); return {node, progress, answer};
  }
  function renderProgress(item, result) {
    const opened = new Set([...item.progress.querySelectorAll('details[open]')].map(node => node.dataset.id));
    item.progress.replaceChildren();
    for (const event of result.events || []) {
      const details = document.createElement('details'); details.className = 'panel-ai-evidence'; details.dataset.id = event.id;
      details.open = opened.has(event.id);
      const title = document.createElement('summary'); title.textContent = `${event.title} · ${{done:'已读取', failed:'读取失败', blocked:'不可用', cancelled:'已停止'}[event.status] || '采集中'}`;
      const body = document.createElement('pre'); body.textContent = event.result || event.command || '';
      details.append(title, body); item.progress.append(details);
    }
    if (!['done', 'failed', 'cancelled'].includes(result.status)) {
      const loading = document.createElement('div'); loading.className = 'panel-ai-loading';
      loading.innerHTML = '<img src="ui/previews/ssh-ai/icons/loader-circle.svg" alt=""><span></span>';
      loading.querySelector('span').textContent = result.stage === 'tools' ? '正在读取运维证据…' : '正在分析证据…';
      item.progress.append(loading);
    }
  }
  function renderAnswer(item, text) {
    if (window.TerminalAI?.renderAnswer) window.TerminalAI.renderAnswer(item.answer, text, {insertCommand:false});
    else item.answer.textContent = text;
  }
  async function cancel(id) { if (id) { try { await api('cancel', {id}); } catch (error) { $('panelAIFeedback').textContent = error.message; } } }
  async function ask() {
    const question = $('panelAIQuestion').value.trim(); if (busy || historyBusy || !question) return;
    const run = ++generation; busy = true; controls();
    let item;
    try {
      await refreshSettings(); if (run !== generation || !dialog.open) return;
      if (!settings.configured) { $('panelAIFeedback').textContent = '先点击“模型设置”，保存后再发送。'; return; }
      if (!settings.secure && !settings.http_enabled) { $('panelAIFeedback').textContent = '请确认本次登录的 HTTP 访问，或使用 HTTPS / 本地隧道。'; return; }
      item = entry(question); renderProgress(item, {status:'running'});
      $('panelAIFeedback').textContent = '自动采集所选范围的证据…';
      const submitted = await api('panel', {question, selection, conversation_id:conversation, history_id:history,
        consent:true, permission:'readonly', provider:{base_url:settings.base_url, model:settings.model, protocol:settings.protocol}});
      if (run !== generation || !dialog.open) { await cancel(submitted.id); return; }
      conversation = submitted.conversation_id; history = submitted.history_id; job = submitted.id;
      $('panelAITarget').textContent = `${submitted.target} · 只读诊断`;
      if ($('panelAIQuestion').value.trim() === question) $('panelAIQuestion').value = '';
      while (run === generation && dialog.open) {
        const result = await api(`jobs?id=${encodeURIComponent(job)}`); if (run !== generation) return;
        renderProgress(item, result);
        if (result.partial_text) renderAnswer(item, result.partial_text);
        if (['failed', 'cancelled'].includes(result.status)) throw new Error(result.detail || '诊断未完成');
        if (result.status === 'done') { renderAnswer(item, result.text); $('panelAIFeedback').textContent = result.history_error || '已保存诊断记录，可继续追问'; job = ''; break; }
        await new Promise(resolve => setTimeout(resolve, 700));
      }
    } catch (error) {
      if (run === generation) { $('panelAIFeedback').textContent = error.message; if (item) renderProgress(item, {status:'failed'}); }
    } finally { if (run === generation) { busy = false; controls(); } }
  }
  async function openHistory(id, before = null) {
    if (busy || historyBusy) return; historyBusy = true; controls(); const run = generation;
    try {
      const result = await api(`history?id=${encodeURIComponent(id)}${before ? `&before=${before}` : ''}`);
      if (!dialog.open || generation !== run) return;
      if (!result.conversation.target?.startsWith('面板主机：')) throw new Error('请选择面板诊断记录');
      history = id; currentHistory = id; historyBefore = result.next_before; conversation = ''; selection = {kind:'overview'};
      $('panelAITarget').textContent = `${result.conversation.target} · 只读诊断`;
      $('panelAIMessages').replaceChildren();
      for (const turn of result.turns) { const item = entry(turn.question); renderProgress(item, {status:'done', events:turn.events}); renderAnswer(item, turn.answer || '本轮没有完成'); }
      if (historyBefore !== null) { const older = document.createElement('button'); older.className = 'ghost-button compact-action'; older.textContent = '更早的对话'; older.onclick = () => openHistory(currentHistory, historyBefore); $('panelAIMessages').prepend(older); }
      $('panelAIHistoryList').hidden = true; $('panelAIFeedback').textContent = '历史已加载，追问会重新检查当前主机；历史证据不会补造。';
    } catch (error) { $('panelAIFeedback').textContent = error.message; }
    finally { historyBusy = false; controls(); }
  }
  async function listHistory(offset = 0) {
    if (busy || historyBusy) return; historyBusy = true; controls(); const run = generation;
    try {
      const result = await api(`history?offset=${offset}`); if (run !== generation || !dialog.open) return;
      const list = $('panelAIHistoryList'); list.hidden = false;
      if (!offset) list.replaceChildren(); else list.querySelector('[data-more]')?.remove();
      for (const record of result.conversations.filter(item => item.target?.startsWith('面板主机：'))) {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'tai-history-row';
        const title = document.createElement('strong'); title.textContent = record.title;
        const info = document.createElement('span'); info.textContent = `${record.target} · ${new Date(record.updated * 1000).toLocaleString()}`;
        button.append(title, info); button.onclick = () => openHistory(record.id); list.append(button);
      }
      historyOffset = result.next_offset;
      if (historyOffset !== null) { const more = document.createElement('button'); more.type = 'button'; more.dataset.more = ''; more.className = 'ghost-button compact-action'; more.textContent = '更多历史'; more.onclick = () => listHistory(historyOffset); list.append(more); }
      if (!list.children.length) list.textContent = '暂无面板诊断历史';
    } catch (error) { $('panelAIFeedback').textContent = error.message; }
    finally { historyBusy = false; controls(); }
  }
  function stop() { generation++; cancel(job); job = ''; busy = false; controls(); dialog.querySelectorAll('.panel-ai-loading').forEach(node => node.remove()); }
  $('panelAIForm').addEventListener('submit', event => { event.preventDefault(); ask(); });
  $('panelAIStop').onclick = stop; $('panelAIClose').onclick = () => dialog.close();
  dialog.addEventListener('close', stop);
  $('panelAIHistory').onclick = () => listHistory();
  $('panelAISettings').onclick = async () => { await window.TerminalAI.openSettings(); };
  $('panelAIHttpConsent').onchange = () => { $('panelAIHttpAllow').disabled = !$('panelAIHttpConsent').checked; };
  $('panelAIHttpAllow').onclick = async () => {
    if (!$('panelAIHttpConsent').checked) return;
    $('panelAIHttpAllow').disabled = true;
    try {
      const response = await fetch('/ssh/allow-http', {method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json', 'X-CSRF-Token':token}, body:JSON.stringify({consent:true})});
      const result = await response.json(); if (!response.ok) throw new Error(result.detail || '授权失败');
      await refreshSettings(); $('panelAIFeedback').textContent = '本次登录已允许，请发送诊断问题。';
    } catch (error) { $('panelAIFeedback').textContent = error.message; $('panelAIHttpAllow').disabled = false; }
  };
  window.addEventListener('pagehide', stop);
  window.PanelAI = {open(scope) {
    if (dialog.open) return;
    selection = {...scope}; conversation = ''; history = ''; currentHistory = ''; generation++; historyBusy = false;
    $('panelAIMessages').replaceChildren(); $('panelAIHistoryList').hidden = true;
    $('panelAITarget').textContent = '面板所在服务器 · 只读诊断';
    $('panelAIQuestion').value = scope.kind === 'incident' ? '请诊断这条异常：检查前后资源、进程现场、相关事件和可用日志，给出证据、可能原因及处理建议。'
      : `请分析最近${{ '1h':'1 小时', '24h':'24 小时', '7d':'7 天'}[scope.period] || '24 小时'}的服务器性能，找出异常峰值、主要进程和同时段事件，说明数据覆盖与缺失，给出优化建议。`;
    dialog.showModal(); ask();
  }, async restore(id) {
    if (dialog.open) return;
    generation++; busy = false; historyBusy = false;
    $('panelAIMessages').replaceChildren(); $('panelAIHistoryList').hidden = true; $('panelAIQuestion').value = '';
    dialog.showModal();
    try { await refreshSettings(); await openHistory(id); }
    catch (error) { $('panelAIFeedback').textContent = error.message; }
  }};
})();
