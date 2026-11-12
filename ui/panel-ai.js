(() => {
  'use strict';
  const dialog = document.createElement('dialog'); dialog.id = 'panelAIDialog'; dialog.className = 'ops-dialog panel-ai-dialog';
  dialog.setAttribute('aria-labelledby', 'panelAITitle');
  dialog.innerHTML = `<header class="ops-head"><div><h2 id="panelAITitle">AI 运维诊断</h2><p id="panelAITarget">面板所在服务器 · 只读诊断</p></div><div class="panel-ai-window-actions"><button id="panelAIMinimize" type="button" class="toolbar-icon" aria-label="最小化 AI 诊断" title="最小化，诊断继续运行"><img src="ui/previews/ssh-ai/icons/minimize-2.svg" alt=""></button><button id="panelAIClose" type="button" class="toolbar-icon" aria-label="关闭 AI 诊断" title="关闭并停止当前诊断"><img src="ui/previews/ssh-ai/icons/x.svg" alt=""></button></div></header>
    <div class="panel-ai-toolbar"><p id="panelAIScope"></p><button id="panelAIHistory" class="ghost-button compact-action" type="button">历史记录</button><button id="panelAISettings" class="ghost-button compact-action" type="button">模型设置</button></div>
    <div class="panel-ai-code"><label for="panelAICodeProject">代码范围</label><select id="panelAICodeProject"><option value="">服务器文件（只读）</option></select><button id="panelAICodeRefresh" class="toolbar-icon" type="button" title="重新扫描代码项目" aria-label="重新扫描代码项目"><img src="ui/previews/ssh-ai/icons/refresh-cw.svg" alt=""></button><span id="panelAICodeStatus" role="status"></span></div>
    <div id="panelAIHistoryList" class="panel-ai-history" hidden></div>
    <div id="panelAIHttp" class="panel-ai-http" hidden><label><input id="panelAIHttpConsent" type="checkbox"> 我了解 HTTP 明文传输风险，允许本次登录使用 AI</label><button id="panelAIHttpAllow" class="ghost-button compact-action" type="button" disabled>允许本次登录</button></div>
    <div id="panelAIMessages" class="panel-ai-messages" aria-live="polite"></div>
    <form id="panelAIForm" class="panel-ai-compose"><textarea id="panelAIQuestion" aria-label="运维诊断问题" placeholder="继续追问，例如：哪个进程占用最多？" maxlength="2000" required></textarea><div class="panel-ai-bottom"><p id="panelAIFeedback" role="status"></p><button id="panelAIStop" class="ghost-button compact-action" type="button" hidden>停止</button><button id="panelAISend" class="ghost-button compact-action" type="submit">发送</button></div></form>`;
  document.body.append(dialog);
  const globalTools = document.createElement('div'); globalTools.className = 'panel-ai-global-tools'; globalTools.hidden = true;
  globalTools.innerHTML = '<label for="panelAIPermission">操作方式</label><select id="panelAIPermission"><option value="approve">操作前确认</option><option value="auto">自动操作</option></select><button id="panelAINew" type="button" class="toolbar-icon" title="新建对话" aria-label="新建全局对话"><img src="ui/previews/ssh-ai/icons/plus.svg" alt=""></button>';
  dialog.querySelector('.panel-ai-toolbar').after(globalTools);
  const grant = document.createElement('dialog'); grant.className = 'ops-dialog panel-ai-grant'; grant.id = 'panelAIGrant';
  grant.innerHTML = '<header class="ops-head"><h2>授权自动操作</h2></header><p>授权覆盖本次登录当前已连接的 SSH 服务器，有效 30 分钟。AI 可自动检查、备份后编辑文件、启停服务和容器，并执行常见通用查询；变更可能中断业务。使用对应 SSH 账号权限，面板本机工具保持只读。</p><p>明确的删除、清空、格式化和卸载会被阻止；未知命令、新连接的服务器仍需逐条确认。命令检查不能保证识别所有间接删除行为；通用命令没有自动备份或回滚，停止或超时不保证远端进程终止。</p><label><input id="panelAIGrantConsent" type="checkbox">我了解范围，允许自动操作这些已连接的服务器。</label><p id="panelAIGrantFeedback" role="status"></p><footer><button id="panelAIGrantCancel" type="button" class="ghost-button compact-action">取消</button><button id="panelAIGrantConfirm" type="button" class="ghost-button compact-action" disabled>确认授权</button></footer>';
  document.body.append(grant);
  const dock = document.createElement('button');
  dock.id = 'panelAIDock'; dock.className = 'panel-ai-dock'; dock.type = 'button'; dock.hidden = true;
  dock.setAttribute('aria-controls', 'panelAIDialog'); dock.setAttribute('aria-expanded', 'false');
  dock.innerHTML = '<span class="panel-ai-dock-indicator" aria-hidden="true"></span><span class="panel-ai-dock-copy"><strong>AI 诊断</strong><span id="panelAIDockStatus" role="status" aria-live="polite"></span></span><img src="ui/previews/ssh-ai/icons/maximize-2.svg" alt="">';
  document.body.append(dock);
  const $ = id => document.getElementById(id);
  let sessionActive = false, minimized = false, phase = 'idle', restoreFocus = null;
  const phaseLabels = {idle:'等待提问', preparing:'准备中', tools:'正在执行工具', running:'正在分析', awaiting_approval:'等待确认', done:'已完成', failed:'分析失败', cancelled:'已停止'};
  function setPhase(value) {
    phase = value; dock.dataset.state = value;
    $('globalAIOpen').dataset.state = value;
    $('panelAIDockStatus').textContent = phaseLabels[value];
    const name = globalMode ? 'AI 助手' : 'AI 诊断';
    dock.setAttribute('aria-label', `恢复 ${name}，${phaseLabels[value]}`);
    dock.title = `恢复 ${name} · ${phaseLabels[value]}`;
  }
  function minimize() {
    if (!dialog.open) return;
    restoreFocus = dialog.contains(document.activeElement) ? document.activeElement : null;
    minimized = true; dock.hidden = false; dialog.close(); dock.focus({preventScroll:true});
    $('globalAIOpen').setAttribute('aria-expanded', 'false');
  }
  function resume() {
    if (!minimized || !sessionActive) return;
    minimized = false; dock.hidden = true; dialog.showModal();
    $('globalAIOpen').setAttribute('aria-expanded', 'true');
    const target = restoreFocus?.isConnected && !restoreFocus.disabled ? restoreFocus : $('panelAIQuestion');
    target.focus({preventScroll:true});
  }
  let selection = {kind:'overview'}, settings = {}, token = '', conversation = '', history = '', job = '', busy = false, generation = 0;
  let historyOffset = 0, historyBefore = null, currentHistory = '', historyBusy = false;
  let projects = [], projectsBusy = false, scopeUnavailable = false;
  let globalMode = false, permission = 'approve', accessBusy = false;
  function controls() {
    $('panelAISend').disabled = busy || historyBusy || projectsBusy || scopeUnavailable || accessBusy; $('panelAIStop').hidden = !busy;
    $('panelAIHistory').disabled = busy || historyBusy || projectsBusy; $('panelAISettings').disabled = busy;
    $('panelAICodeProject').disabled = busy || historyBusy || projectsBusy;
    $('panelAICodeRefresh').disabled = busy || historyBusy || projectsBusy;
    $('panelAIPermission').disabled = busy || historyBusy || accessBusy || !settings.configured;
    $('panelAINew').disabled = busy || historyBusy;
    window.AppSelects?.syncAll();
  }
  async function api(path, data) {
    const response = await fetch(`/terminal-ai/${path}`, {method:data ? 'POST' : 'GET', credentials:'same-origin',
      headers:data ? {'Content-Type':'application/json', 'X-CSRF-Token':token} : {}, body:data ? JSON.stringify(data) : undefined});
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || result.error || '诊断请求失败');
    return result;
  }
  async function refreshSettings() {
    const run = generation, result = await api('settings');
    if (run !== generation || !sessionActive) return;
    settings = result; token = settings.csrf_token;
    if (globalMode && (settings.secure || settings.http_enabled)) {
      const access = await api('access?session_id=global-assistant');
      if (run !== generation || !sessionActive) return;
      permission = access.mode || 'approve'; $('panelAIPermission').value = permission;
    }
    $('panelAIHttp').hidden = !!(settings.secure || settings.http_enabled);
    $('panelAIScope').textContent = settings.configured ? `问题及读取的服务器文件、日志和配置发送至 ${settings.base_url} · ${settings.model}` : '请先配置模型，再发送诊断问题';
  }
  function codeStatus() {
    const project = projects.find(p => p.id === $('panelAICodeProject').value);
    $('panelAICodeStatus').textContent = scopeUnavailable ? '历史代码项目已不可用'
      : project ? `${project.path} · 只读` : '服务器文件 · 只读';
  }
  function changeProject() {
    conversation = ''; history = ''; currentHistory = ''; scopeUnavailable = false;
    $('panelAIMessages').replaceChildren(); $('panelAIHistoryList').hidden = true;
    $('panelAITarget').textContent = '面板所在服务器 · 只读诊断';
    $('panelAIFeedback').textContent = '已切换代码范围，开始新的对话；原对话保留在历史中。';
    codeStatus(); controls();
    setPhase('idle');
  }
  async function loadProjects(refresh = false) {
    if (globalMode) return;
    if (!settings.secure && !settings.http_enabled) return;
    const run = generation, previous = $('panelAICodeProject').value;
    projectsBusy = true; controls();
    try {
      const query = new URLSearchParams();
      if (selection.kind === 'incident') query.set('incident_id', selection.id);
      if (refresh) query.set('refresh', '1');
      const result = await api(`code-projects?${query}`);
      if (run !== generation || !sessionActive) return;
      projects = result.projects || [];
      $('panelAICodeProject').replaceChildren(new Option('服务器文件（只读）', ''),
        ...projects.map(p => new Option(`${p.name} · ${p.path}`, p.id)));
      $('panelAICodeProject').value = projects.some(p => p.id === previous) ? previous : '';
      if (previous && previous !== $('panelAICodeProject').value) changeProject();
      codeStatus();
      if (!projects.length) $('panelAICodeStatus').textContent = result.supported
        ? '服务器文件 · 未发现可推荐的项目' : '本机文件读取需要 Linux';
    } catch (error) {
      if (run === generation) $('panelAICodeStatus').textContent = `项目扫描失败：${error.message}`;
    } finally { if (run === generation) { projectsBusy = false; controls(); } }
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
      const title = document.createElement('summary'); title.textContent = `${event.title} · ${{done:event.write ? '已执行' : '已读取', failed:'执行失败', blocked:'未执行', cancelled:'已停止', awaiting_approval:'等待确认'}[event.status] || '执行中'}`;
      const body = document.createElement('pre'); body.textContent = event.result || event.command || '';
      details.append(title, body); item.progress.append(details);
    }
    if (!['done', 'failed', 'cancelled', 'awaiting_approval'].includes(result.status)) {
      const loading = document.createElement('div'); loading.className = 'panel-ai-loading';
      loading.innerHTML = '<img src="ui/previews/ssh-ai/icons/loader-circle.svg" alt=""><span></span>';
      loading.querySelector('span').textContent = result.stage === 'tools' ? '正在执行工具…' : '正在分析证据…';
      item.progress.append(loading);
    }
    if (result.status === 'awaiting_approval' && result.approval) {
      const approval = result.approval, key = job;
      const box = document.createElement('section'); box.className = 'panel-ai-approval';
      const target = document.createElement('strong'); target.textContent = approval.target;
      const impact = document.createElement('p'); impact.textContent = approval.impact;
      const command = document.createElement('pre'); command.textContent = approval.preview?.diff || approval.command;
      const actions = document.createElement('div');
      for (const [label, decision] of [['拒绝', false], ['同意执行', true]]) {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'ghost-button compact-action'; button.textContent = label;
        button.onclick = async () => {
          actions.querySelectorAll('button').forEach(node => { node.disabled = true; });
          try { await api('approve', {id:key, approval_id:approval.id, approve:decision}); box.remove(); }
          catch (error) { $('panelAIFeedback').textContent = error.message; }
        };
        actions.append(button);
      }
      box.append(target, impact, command, actions); item.progress.append(box);
    }
  }
  function renderAnswer(item, text) {
    if (window.TerminalAI?.renderAnswer) window.TerminalAI.renderAnswer(item.answer, text, {insertCommand:false});
    else item.answer.textContent = text;
  }
  async function cancel(id) { if (id) { try { await api('cancel', {id}); } catch (error) { $('panelAIFeedback').textContent = error.message; } } }
  async function ask() {
    const question = $('panelAIQuestion').value.trim(); if (busy || historyBusy || projectsBusy || scopeUnavailable || accessBusy || !question) return;
    const run = ++generation; busy = true; setPhase('preparing'); controls();
    let item;
    try {
      await refreshSettings(); if (run !== generation || !sessionActive) return;
      if (!settings.configured) { setPhase('idle'); $('panelAIFeedback').textContent = '先点击“模型设置”，保存后再发送。'; return; }
      if (!settings.secure && !settings.http_enabled) { setPhase('idle'); $('panelAIFeedback').textContent = '请确认本次登录的 HTTP 访问，或使用 HTTPS / 本地隧道。'; return; }
      item = entry(question); renderProgress(item, {status:'running'});
      $('panelAIFeedback').textContent = globalMode ? '正在处理…' : '自动采集所选范围的证据…';
      const submitted = await api(globalMode ? 'global' : 'panel', {question, conversation_id:conversation, history_id:history,
        ...(globalMode ? {} : {selection, code_project:$('panelAICodeProject').value}),
        consent:true, permission:globalMode ? permission : 'readonly', provider:{base_url:settings.base_url, model:settings.model, protocol:settings.protocol}});
      if (run !== generation || !sessionActive) { await cancel(submitted.id); return; }
      conversation = submitted.conversation_id; history = submitted.history_id; job = submitted.id;
      $('panelAITarget').textContent = submitted.target + (globalMode ? '' : ' · 只读诊断');
      if ($('panelAIQuestion').value.trim() === question) $('panelAIQuestion').value = '';
      while (run === generation && sessionActive) {
        const result = await api(`jobs?id=${encodeURIComponent(job)}`); if (run !== generation) return;
        setPhase(['done', 'failed', 'cancelled', 'awaiting_approval'].includes(result.status) ? result.status : result.stage === 'tools' ? 'tools' : 'running');
        renderProgress(item, result);
        if (result.status === 'awaiting_approval') $('panelAIFeedback').textContent = '等待确认后执行';
        if (result.partial_text) renderAnswer(item, result.partial_text);
        if (['failed', 'cancelled'].includes(result.status)) throw new Error(result.detail || '诊断未完成');
        if (result.status === 'done') { renderAnswer(item, result.text); $('panelAIFeedback').textContent = result.history_error || '已保存诊断记录，可继续追问'; job = ''; break; }
        await new Promise(resolve => setTimeout(resolve, 700));
      }
    } catch (error) {
      if (run === generation) { if (phase !== 'cancelled') setPhase('failed'); $('panelAIFeedback').textContent = error.message; if (item) renderProgress(item, {status:phase}); }
    } finally { if (run === generation) { busy = false; controls(); } }
  }
  async function openHistory(id, before = null) {
    if (busy || historyBusy || projectsBusy) return; historyBusy = true; controls(); const run = generation;
    try {
      const result = await api(`history?id=${encodeURIComponent(id)}${before ? `&before=${before}` : ''}`);
      if (!sessionActive || generation !== run) return;
      if (!globalMode && !result.conversation.target?.startsWith('面板主机：')) throw new Error('请选择面板诊断记录');
      history = id; currentHistory = id; historyBefore = result.next_before; conversation = ''; selection = {kind:'overview'};
      const root = result.conversation.target.split(' · 代码范围：')[1] || '';
      const project = projects.find(p => p.path === root);
      $('panelAICodeProject').querySelector('[data-unavailable]')?.remove();
      $('panelAICodeProject').value = project?.id || '';
      scopeUnavailable = Boolean(root && !project);
      if (globalMode) scopeUnavailable = result.conversation.target !== settings.global_target;
      if (scopeUnavailable && !globalMode) {
        const missing = new Option(`原项目不可用：${root}`, 'unavailable', false, true);
        missing.disabled = true; missing.dataset.unavailable = ''; $('panelAICodeProject').append(missing);
      }
      codeStatus();
      $('panelAITarget').textContent = result.conversation.target + (globalMode ? '' : ' · 只读诊断');
      $('panelAIMessages').replaceChildren();
      for (const turn of result.turns) { const item = entry(turn.question); renderProgress(item, {status:'done', events:turn.events}); renderAnswer(item, turn.answer || '本轮没有完成'); }
      if (historyBefore !== null) { const older = document.createElement('button'); older.className = 'ghost-button compact-action'; older.textContent = '更早的对话'; older.onclick = () => openHistory(currentHistory, historyBefore); $('panelAIMessages').prepend(older); }
      $('panelAIHistoryList').hidden = true; $('panelAIFeedback').textContent = '历史已加载，追问会重新检查当前主机；历史证据不会补造。';
      if (scopeUnavailable) $('panelAIFeedback').textContent = '历史代码目录已不可用，可选择其他项目或“服务器文件（只读）”开始新对话。';
      if (globalMode && scopeUnavailable) $('panelAIFeedback').textContent = '这是旧入口或其他主机的历史记录，可查看；新建全局对话后继续提问。';
      setPhase('idle');
    } catch (error) { if (run === generation) $('panelAIFeedback').textContent = error.message; }
    finally { if (run === generation) { historyBusy = false; controls(); } }
  }
  async function listHistory(offset = 0) {
    if (busy || historyBusy || projectsBusy) return; historyBusy = true; controls(); const run = generation;
    try {
      const result = await api(`history?offset=${offset}`); if (run !== generation || !sessionActive) return;
      const list = $('panelAIHistoryList'); list.hidden = false;
      if (!offset) list.replaceChildren(); else list.querySelector('[data-more]')?.remove();
      for (const record of result.conversations.filter(item => globalMode || item.target?.startsWith('面板主机：'))) {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'tai-history-row';
        const title = document.createElement('strong'); title.textContent = record.title;
        const info = document.createElement('span'); info.textContent = `${record.target} · ${new Date(record.updated * 1000).toLocaleString()}`;
        button.append(title, info); button.onclick = () => openHistory(record.id); list.append(button);
      }
      historyOffset = result.next_offset;
      if (historyOffset !== null) { const more = document.createElement('button'); more.type = 'button'; more.dataset.more = ''; more.className = 'ghost-button compact-action'; more.textContent = '更多历史'; more.onclick = () => listHistory(historyOffset); list.append(more); }
      if (!list.children.length) list.textContent = '暂无对话历史';
    } catch (error) { if (run === generation) $('panelAIFeedback').textContent = error.message; }
    finally { if (run === generation) { historyBusy = false; controls(); } }
  }
  function stop() { const running = busy; generation++; cancel(job); job = ''; busy = false; controls(); dialog.querySelectorAll('.panel-ai-loading').forEach(node => node.remove()); if (running) { setPhase('cancelled'); $('panelAIFeedback').textContent = '诊断已停止，可继续提问。'; } }
  $('panelAIForm').addEventListener('submit', event => { event.preventDefault(); ask(); });
  $('panelAIStop').onclick = stop; $('panelAIClose').onclick = () => globalMode ? minimize() : dialog.close();
  dialog.addEventListener('cancel', event => { if (globalMode) { event.preventDefault(); minimize(); } });
  $('panelAIMinimize').onclick = minimize; dock.onclick = resume;
  dialog.addEventListener('close', () => { if (!dialog.open && !minimized) { sessionActive = false; dock.hidden = true; stop(); } });
  $('panelAIHistory').onclick = () => listHistory();
  $('panelAISettings').onclick = async () => { await window.TerminalAI.openSettings(); };
  $('aiSettingsDialog').addEventListener('close', async () => {
    if (!sessionActive) return;
    try { await refreshSettings(); controls(); }
    catch (error) { $('panelAIFeedback').textContent = error.message; }
  });
  $('panelAICodeProject').onchange = changeProject;
  $('panelAICodeRefresh').onclick = () => loadProjects(true);
  $('panelAIHttpConsent').onchange = () => { $('panelAIHttpAllow').disabled = !$('panelAIHttpConsent').checked; };
  $('panelAIHttpAllow').onclick = async () => {
    if (!$('panelAIHttpConsent').checked) return;
    $('panelAIHttpAllow').disabled = true;
    try {
      const response = await fetch('/ssh/allow-http', {method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json', 'X-CSRF-Token':token}, body:JSON.stringify({consent:true})});
      const result = await response.json(); if (!response.ok) throw new Error(result.detail || '授权失败');
      await refreshSettings(); await loadProjects(); $('panelAIFeedback').textContent = '本次登录已允许，请发送诊断问题。';
    } catch (error) { $('panelAIFeedback').textContent = error.message; $('panelAIHttpAllow').disabled = false; }
  };
  window.addEventListener('pagehide', () => { sessionActive = false; minimized = false; dock.hidden = true; stop(); });
  function newGlobal() {
    globalMode = true; scopeUnavailable = false; projectsBusy = false;
    selection = {kind:'overview'}; conversation = ''; history = ''; currentHistory = '';
    $('panelAITitle').textContent = 'AI 助手'; $('panelAITarget').textContent = settings.global_target || '全局会话';
    $('panelAIMinimize').setAttribute('aria-label', '最小化 AI 助手');
    $('panelAIMinimize').title = '最小化，任务继续运行';
    $('panelAIClose').setAttribute('aria-label', '收起 AI 助手'); $('panelAIClose').title = '收起对话，任务继续运行';
    dock.querySelector('strong').textContent = 'AI 助手';
    $('panelAICodeProject').value = ''; dialog.querySelector('.panel-ai-code').hidden = true; globalTools.hidden = false;
    $('panelAIMessages').replaceChildren(); $('panelAIHistoryList').hidden = true;
    $('panelAIQuestion').value = ''; $('panelAIQuestion').placeholder = '例如：检查 aimore 的接口报错，只读取代码和日志';
    $('panelAIFeedback').textContent = '等待提问'; setPhase('idle'); controls();
  }
  async function openGlobal() {
    if (minimized) { resume(); return; }
    if (dialog.open && globalMode) { $('panelAIQuestion').focus(); return; }
    if (busy) return;
    newGlobal(); sessionActive = true; minimized = false; dock.hidden = true;
    if (!dialog.open) dialog.showModal();
    $('globalAIOpen').setAttribute('aria-expanded', 'true');
    try { await refreshSettings(); controls(); } catch (error) { $('panelAIFeedback').textContent = error.message; }
    $('panelAIQuestion').focus();
  }
  $('globalAIOpen').onclick = openGlobal;
  $('panelAINew').onclick = newGlobal;
  async function grantAccess(mode, consent = false) {
    accessBusy = true; controls();
    try {
      const result = await api('access', {session_id:'global-assistant', mode, consent, ...(mode === 'auto' ? {policy_version:2} : {})});
      permission = result.mode; $('panelAIPermission').value = permission;
    } finally { accessBusy = false; controls(); }
  }
  $('panelAIPermission').onchange = async () => {
    if ($('panelAIPermission').value === 'auto') {
      $('panelAIGrantConsent').checked = false; $('panelAIGrantConfirm').disabled = true;
      $('panelAIGrantFeedback').textContent = ''; grant.showModal();
    } else { try { await grantAccess('approve'); } catch (error) { $('panelAIFeedback').textContent = error.message; } }
  };
  $('panelAIGrantConsent').onchange = () => { $('panelAIGrantConfirm').disabled = !$('panelAIGrantConsent').checked; };
  $('panelAIGrantCancel').onclick = () => grant.close();
  grant.addEventListener('close', () => { $('panelAIPermission').value = permission; controls(); });
  $('panelAIGrantConfirm').onclick = async () => {
    if (!$('panelAIGrantConsent').checked) return;
    $('panelAIGrantConfirm').disabled = true;
    try { await grantAccess('auto', true); grant.close(); }
    catch (error) { $('panelAIGrantFeedback').textContent = error.message; $('panelAIGrantConfirm').disabled = false; }
  };
  window.PanelAI = {async open(scope, initialQuestion = '') {
    if (dialog.open) return;
    if (minimized) { resume(); return; }
    stop();
    globalMode = false; globalTools.hidden = true; dialog.querySelector('.panel-ai-code').hidden = false;
    sessionActive = true; setPhase('preparing');
    selection = {...scope}; conversation = ''; history = ''; currentHistory = ''; generation++; historyBusy = false;
    projectsBusy = false; scopeUnavailable = false; $('panelAICodeProject').value = ''; codeStatus();
    $('panelAIMessages').replaceChildren(); $('panelAIHistoryList').hidden = true;
    $('panelAITarget').textContent = '面板所在服务器 · 只读诊断';
    $('panelAIQuestion').value = initialQuestion || (scope.kind === 'incident' ? '请诊断这条异常：检查前后资源、进程现场、相关事件和可用日志，给出证据、可能原因及处理建议。'
      : `请分析最近${{ '1h':'1 小时', '24h':'24 小时', '7d':'7 天'}[scope.period] || '24 小时'}的服务器性能，找出异常峰值、主要进程和同时段事件，说明数据覆盖与缺失，给出优化建议。`);
    dialog.showModal(); const run = generation;
    try { await refreshSettings(); if (run !== generation) return; await loadProjects(); if (run === generation && sessionActive) ask(); }
    catch (error) { if (run === generation) { setPhase('failed'); $('panelAIFeedback').textContent = error.message; } }
  }, openGlobal, async restore(id) {
    if (dialog.open) return;
    if (minimized) { resume(); return; }
    stop();
    sessionActive = true; setPhase('idle');
    generation++; busy = false; historyBusy = false; projectsBusy = false; scopeUnavailable = false;
    $('panelAIMessages').replaceChildren(); $('panelAIHistoryList').hidden = true; $('panelAIQuestion').value = '';
    dialog.showModal(); const run = generation;
    try {
      await refreshSettings(); if (run !== generation) return;
      await loadProjects(); if (run !== generation) return;
      await openHistory(id);
    } catch (error) { if (run === generation) $('panelAIFeedback').textContent = error.message; }
  }};
})();
