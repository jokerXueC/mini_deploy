(() => {
  const el = id => document.getElementById(id);
  let token = '', settings = {}, allowed = false, loading = false, working = false;
  let conversationId = '', job = '', generation = 0, autoTimer, autoSession = '', autoCursor = -1, controller;
  let dialogTest = false, saving = false;
  let pendingEntry;
  let savedSession = '';
  let permission = 'readonly', accessLoading = false, accessDraft = '', accessSession = '', submitting = 0;
  const context = () => window.SSHTerminal?.context() || {session_id: '', selection: '', cursor: 0, target: ''};
  const message = (text, error = false, id = 'aiFeedback') => {
    el(id).textContent = text; el(id).classList.toggle('tai-error', error);
  };
  function icons(root = document) {
    root.querySelectorAll('[data-ai-icon]').forEach(icon => {
      icon.style.setProperty('--tai-icon', `url("/ui/previews/ssh-ai/icons/${icon.dataset.aiIcon}.svg")`);
      icon.setAttribute('aria-hidden', 'true');
    });
  }
  async function api(path, data) {
    const abort = new AbortController(); const timer = setTimeout(() => abort.abort(), 15000);
    controller = abort;
    try {
      const response = await fetch(path, {method: data ? 'POST' : 'GET', credentials: 'same-origin',
        signal: abort.signal, headers: data ? {'Content-Type': 'application/json', 'X-CSRF-Token': token} : {},
        body: data ? JSON.stringify(data) : undefined});
      const result = await response.json().catch(() => ({}));
      if (!response.ok) {
        const error = new Error(result.detail || (response.status === 401 ? '登录已过期，请重新登录。' : '模型操作失败，请刷新重试。'));
        error.status = response.status; throw error;
      }
      return result;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('请求已停止或超时，请检查连接。');
      throw error;
    } finally { clearTimeout(timer); if (controller === abort) controller = undefined; }
  }
  function panel(open) {
    el('aiPanel').hidden = !open; el('sshLayout').classList.toggle('ai-open', open);
    el('aiChatOpen').setAttribute('aria-expanded', String(open));
    el('aiBackdrop').hidden = !open || !matchMedia('(max-width:1000px)').matches;
  }
  function controls() {
    const connected = !!context().session_id;
    el('aiAskSend').disabled = working || accessLoading || saving;
    el('aiNewChat').disabled = working;
    el('aiShareOutput').disabled = !connected || working;
    el('aiPermission').disabled = !connected || !allowed || accessLoading || saving;
    el('aiPermission').value = connected ? permission : 'chat';
    el('aiAuto').disabled = !connected || !el('aiShareOutput').checked || working && !autoSession;
    el('aiStop').hidden = !working;
    el('aiSave').disabled = el('aiTest').disabled = !allowed || working || saving;
    el('aiTargetLabel').textContent = connected ? context().target : '普通对话';
    let scope = connected && el('aiShareOutput').checked ? '问题、对话记录与最近输出（常见凭据会脱敏）' : '问题与对话记录';
    if (connected && permission !== 'chat') scope += '及工具检查结果';
    el('aiSendContext').textContent = settings.configured ? `发送${scope}至 ${settings.base_url}` : '先配置模型';
    window.AppSelects?.syncAll();
  }
  async function refreshAccess() {
    const sessionId = context().session_id;
    if (!sessionId || !allowed) { permission = 'readonly'; controls(); return; }
    accessLoading = true; controls();
    try {
      const result = await api(`/terminal-ai/access?session_id=${encodeURIComponent(sessionId)}`);
      if (sessionId === context().session_id) permission = result.mode;
    } catch (error) { if (sessionId === context().session_id) message(error.message, true); }
    finally { accessLoading = false; controls(); }
  }
  function renderSettings() {
    const name = settings.configured ? `${settings.protocol === 'anthropic' ? 'Claude' : '兼容接口'} · ${settings.model}` : 'AI 尚未配置';
    el('aiModelSummary').textContent = name;
    el('aiPanelModel').textContent = settings.configured ? settings.model : '配置模型';
  }
  async function refresh() {
    if (loading) return;
    loading = true;
    try {
      settings = await api('/terminal-ai/settings'); token = settings.csrf_token;
      allowed = !!((settings.secure && window.isSecureContext) || settings.http_enabled === true);
      window.SSHTerminal?.updateAccess(settings);
      if (el('aiSettingsDialog').open) settingsFeedback();
      renderSettings();
      await refreshAccess();
    } catch (error) { message(error.message, true); }
    finally { loading = false; controls(); }
  }
  function protocolHint() {
    const claude = el('aiProtocol').value === 'anthropic';
    el('aiBaseUrl').placeholder = claude ? 'https://api.anthropic.com/v1' : 'https://api.openai.com/v1';
    el('aiEndpointHint').textContent = claude ? '填写 Claude 原生接口地址，例如 https://api.anthropic.com/v1；系统使用 Messages API。'
      : '填写服务商的 Base URL；本机 Ollama 可填 http://127.0.0.1:11434/v1。';
  }
  function settingsFeedback() {
    message(allowed ? (settings.http_enabled && !settings.secure ? 'HTTP 已允许，模型密钥到面板的传输为明文。' : '无需连接 SSH 即可配置和测试模型。')
      : '先关闭此窗口，在远程终端上方确认允许本次登录使用 HTTP；也可通过 HTTPS 或本地隧道打开面板。', !allowed, 'aiSettingsFeedback');
  }
  async function openSettings() {
    await refresh();
    el('aiProtocol').value = settings.protocol || 'openai'; el('aiBaseUrl').value = settings.base_url || '';
    el('aiModel').value = settings.model || ''; el('aiKey').value = ''; el('aiClearKey').checked = false;
    el('aiKey').placeholder = settings.has_key ? '已保存密钥；留空保留，输入新密钥可替换' : '输入 API Key；无密钥的本机服务可留空';
    protocolHint();
    window.AppSelects?.syncAll();
    settingsFeedback();
    el('aiSettingsDialog').showModal(); controls();
  }
  const draft = () => ({protocol: el('aiProtocol').value, base_url: el('aiBaseUrl').value.trim(),
    model: el('aiModel').value.trim(), api_key: el('aiKey').value, clear_key: el('aiClearKey').checked});
  function stopAuto() {
    clearTimeout(autoTimer); autoTimer = undefined; autoSession = ''; autoCursor = -1;
    el('aiAuto').checked = false; el('aiScopeLabel').textContent = '对话';
  }
  function scheduleAuto() {
    clearTimeout(autoTimer);
    if (!autoSession || working || !conversationId) return;
    autoTimer = setTimeout(async () => {
      if (!autoSession || activeView !== 'terminal' || document.hidden || context().session_id !== autoSession) {
        if (context().session_id !== autoSession || activeView !== 'terminal') stopAuto(); else scheduleAuto();
        return;
      }
      if (context().cursor === autoCursor) { scheduleAuto(); return; }
      autoCursor = context().cursor;
      try {
        await ask('分析新增的终端输出，是否出现新的异常？', true);
      } catch (error) { stopAuto(); message(error.message, true); }
    }, 30000);
  }
  function appendEntry(asked) {
    el('aiEmpty')?.remove();
    while (el('aiMessages').children.length >= 12) el('aiMessages').firstElementChild.remove();
    const entry = document.createElement('article'); entry.className = 'tai-entry';
    const q = document.createElement('p'); q.className = 'tai-question'; q.textContent = asked;
    const answer = document.createElement('div'); answer.className = 'tai-answer'; answer.textContent = '正在回复…';
    const tools = document.createElement('div'); tools.className = 'tai-steps';
    const approval = document.createElement('div'); approval.className = 'tai-approval'; approval.hidden = true;
    const meta = document.createElement('p'); meta.className = 'tai-entry-meta';
    entry.append(q, tools, approval, answer, meta); el('aiMessages').append(entry); el('aiMessages').scrollTop = el('aiMessages').scrollHeight;
    return {answer, meta, tools, approval, steps: new Map(), approvalId: ''};
  }
  function renderProgress(entry, result, id) {
    if (!entry) return;
    const labels = {running: '执行中', awaiting_approval: '等待确认', done: '已完成', failed: '未完成', blocked: '未执行', cancelled: '已停止'};
    for (const step of result.events || []) {
      let row = entry.steps.get(step.id);
      if (!row) {
        const details = document.createElement('details'); details.className = 'tai-step';
        const summary = document.createElement('summary'); const pre = document.createElement('pre');
        details.append(summary, pre); entry.tools.append(details); row = {details, summary, pre}; entry.steps.set(step.id, row);
      }
      if (row.details.dataset.state !== step.status) row.details.dataset.state = step.status;
      const title = `${step.title} · ${labels[step.status] || step.status}`;
      if (row.summary.textContent !== title) row.summary.textContent = title;
      const content = [step.command, step.result].filter(Boolean).join('\n\n');
      if (row.pre.textContent !== content) row.pre.textContent = content;
    }
    if (!result.approval) { entry.approval.hidden = true; entry.approvalId = ''; return; }
    entry.answer.textContent = '确认后继续排查'; message('有操作需要你确认');
    if (entry.approvalId === result.approval.id) return;
    const approval = result.approval; entry.approvalId = approval.id;
    entry.approval.replaceChildren(); entry.approval.hidden = false;
    const title = document.createElement('strong'); title.textContent = approval.title;
    const target = document.createElement('p'); target.textContent = `服务器：${approval.target}`;
    const command = document.createElement('pre'); command.textContent = approval.command;
    const impact = document.createElement('p'); impact.textContent = `${approval.impact} 请在 2 分钟内确认。`;
    const actions = document.createElement('div'); actions.className = 'tai-approval-actions';
    for (const [text, decision] of [['拒绝', false], ['同意执行', true]]) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'tai-text-button'; button.textContent = text;
      button.addEventListener('click', async () => {
        actions.querySelectorAll('button').forEach(b => { b.disabled = true; });
        try {
          await api('/terminal-ai/approve', {id, approval_id: approval.id, approve: decision});
          if (!working || job !== id) return;
          entry.approval.hidden = true; message(decision ? '已同意，正在继续执行' : '已拒绝本次操作');
        } catch (error) { message(error.message, true); actions.querySelectorAll('button').forEach(b => { b.disabled = false; }); }
      }); actions.append(button);
    }
    entry.approval.append(title, target, command, impact, actions);
    el('aiMessages').scrollTop = el('aiMessages').scrollHeight;
  }
  function renderAnswer(target, text) {
    target.replaceChildren();
    // Render model text as text nodes. Fenced snippets never become executable HTML.
    const regex = /```[^\n]*\n([\s\S]*?)```/g; let offset = 0, match;
    while ((match = regex.exec(text))) {
      target.append(document.createTextNode(text.slice(offset, match.index)));
      const block = document.createElement('div'); block.className = 'tai-command';
      const pre = document.createElement('pre'); const command = match[1].trim(); pre.textContent = command;
      const actions = document.createElement('div'); actions.className = 'tai-command-actions';
      const copy = document.createElement('button'); copy.type = 'button'; copy.className = 'tai-text-button'; copy.textContent = '复制';
      copy.addEventListener('click', async () => {
        try { await navigator.clipboard.writeText(command); message('命令已复制，请核对后执行。'); }
        catch { message('请选中命令后使用系统复制。'); }
      });
      actions.append(copy);
      if (command && !/[\x00-\x1f\x7f]/.test(command) && command.length <= 3000) {
        const fill = document.createElement('button'); fill.type = 'button'; fill.className = 'tai-text-button'; fill.textContent = '填入终端';
        fill.addEventListener('click', () => {
          if (!window.SSHTerminal.insertCommand(command)) message('请先连接 SSH，并确认命令内容。');
        });
        actions.append(fill);
      }
      block.append(pre, actions); target.append(block); offset = regex.lastIndex;
    }
    target.append(document.createTextNode(text.slice(offset)));
  }
  async function waitForJob(id, current, entry) {
    const deadline = Date.now() + 300000;
    while (current === generation && Date.now() < deadline) {
      const result = await api(`/terminal-ai/jobs?id=${encodeURIComponent(id)}`);
      if (current !== generation) return null;
      renderProgress(entry, result, id);
      if (result.status === 'failed') throw new Error(result.detail);
      if (result.status === 'cancelled') throw new Error(result.detail || '任务已停止。');
      if (result.status === 'done') return result;
      await new Promise(resolve => setTimeout(resolve, 700));
    }
    if (current === generation) { cancelJob(id); throw new Error('本轮排查超时，已请求停止。'); }
    return null;
  }
  async function ask(asked, automatic = false) {
    if (working || saving || accessLoading || !asked.trim()) return;
    panel(true);
    if (!settings.configured) { message('先配置模型，问题会保留。'); await openSettings(); return; }
    if (!allowed) { message('请先确认允许本次登录使用 HTTP，或通过 HTTPS / 本地隧道使用 AI 对话。', true); return; }
    const sessionId = context().session_id;
    const includeTerminal = !!sessionId && el('aiShareOutput').checked;
    working = true; controls(); message('正在回复…');
    const current = ++generation; const entry = appendEntry(asked); pendingEntry = entry;
    try {
      submitting = current;
      const submitted = await api('/terminal-ai/chat', {session_id: sessionId, conversation_id: conversationId,
        include_terminal: includeTerminal, consent: true, question: asked, automatic,
        permission: sessionId ? permission : 'chat',
        provider: {base_url: settings.base_url, protocol: settings.protocol, model: settings.model}});
      if (submitting === current) submitting = 0;
      if (current !== generation) { cancelJob(submitted.id); return; }
      conversationId = submitted.conversation_id;
      if (!automatic) el('aiQuestion').value = '';
      job = submitted.id;
      const result = await waitForJob(job, current, entry);
      if (!result || current !== generation) return;
      renderAnswer(entry.answer, result.text);
      const usage = result.usage || {}; const tokens = usage.total_tokens ?? ((usage.input_tokens || 0) + (usage.output_tokens || 0));
      const scope = result.context?.lines ? `最近输出 ${result.context.lines} 行` : '未附带终端输出';
      entry.meta.textContent = `${result.model} · ${scope}${result.events?.length ? ` · ${result.events.length} 项检查` : ''}${tokens ? ` · ${tokens} tokens` : ''}`;
      message('可继续追问');
      el('aiMessages').scrollTop = el('aiMessages').scrollHeight;
    } catch (error) {
      if (current === generation) {
        entry.answer.textContent = error.message; entry.answer.classList.add('tai-error'); message(error.message, true); stopAuto();
        if (error.status === 404 || error.status === 409 && /连接或模型|模型配置/.test(error.message)) conversationId = '';
      }
    } finally {
      if (submitting === current) submitting = 0;
      if (current === generation) { working = false; job = ''; pendingEntry = undefined; controls(); await refreshAccess(); scheduleAuto(); }
    }
  }
  function newChat() {
    conversationId = ''; stopAuto();
    el('aiMessages').replaceChildren();
    const empty = document.createElement('div'); empty.id = 'aiEmpty'; empty.className = 'tai-empty';
    const title = document.createElement('strong'); title.textContent = '想排查什么问题？';
    const hint = document.createElement('p'); hint.textContent = '例如：这段报错是什么原因？';
    empty.append(title, hint); el('aiMessages').append(empty); message('等待提问'); controls();
  }
  el('aiSettingsOpen').addEventListener('click', openSettings);
  el('aiPanelModel').addEventListener('click', openSettings);
  el('aiProtocol').addEventListener('change', protocolHint);
  el('aiSettingsForm').addEventListener('submit', async event => {
    event.preventDefault(); if (!allowed || working || saving) return;
    saving = true; controls();
    try {
      settings = await api('/terminal-ai/settings', draft()); newChat(); renderSettings(); await refreshAccess();
      el('aiKey').value = ''; el('aiSettingsDialog').close(); message('模型设置已保存。');
    } catch (error) { message(error.message, true, 'aiSettingsFeedback'); }
    finally { saving = false; controls(); }
  });
  el('aiTest').addEventListener('click', async () => {
    if (!allowed || working || saving || !el('aiSettingsForm').reportValidity()) return;
    working = true; dialogTest = true; controls(); const current = ++generation;
    message('正在连接模型服务…', false, 'aiSettingsFeedback');
    try {
      const submitted = await api('/terminal-ai/test', draft());
      if (current !== generation) return;
      job = submitted.id; const result = await waitForJob(job, current);
      if (result && current === generation) message(`连接成功：${result.model}。可保存设置。`, false, 'aiSettingsFeedback');
    } catch (error) { if (current === generation) message(error.message, true, 'aiSettingsFeedback'); }
    finally { if (current === generation) { working = false; dialogTest = false; job = ''; controls(); } }
  });
  el('aiChatOpen').addEventListener('click', () => { panel(true); el('aiQuestion').focus(); });
  el('aiPanelClose').addEventListener('click', () => panel(false));
  el('aiBackdrop').addEventListener('click', () => panel(false));
  el('aiNewChat').addEventListener('click', () => { if (!working) { newChat(); el('aiQuestion').focus(); } });
  el('aiAskForm').addEventListener('submit', event => { event.preventDefault(); ask(el('aiQuestion').value.trim()); });
  el('aiQuestion').addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); el('aiAskForm').requestSubmit(); }
  });
  el('aiShareOutput').addEventListener('change', () => { if (!el('aiShareOutput').checked) stopAuto(); controls(); });
  el('aiAuto').addEventListener('change', () => {
    if (!el('aiAuto').checked) stopAuto();
    else {
      if (!context().session_id || !allowed || !settings.configured) { stopAuto(); message('先配置模型并连接 SSH。', true); return; }
      autoSession = context().session_id; autoCursor = context().cursor;
      el('aiScopeLabel').textContent = '跟进 · 每 30 秒';
      message(conversationId ? '开始跟进新输出' : '发送首条问题后开始跟进新输出'); scheduleAuto();
    }
  });
  function cancelJob(id) {
    if (!id) return;
    return fetch('/terminal-ai/cancel', {method: 'POST', credentials: 'same-origin', keepalive: true,
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': token}, body: JSON.stringify({id})})
      .then(response => { if (!response.ok) message('停止请求未成功，请核对任务和服务器状态。', true); })
      .catch(() => message('停止请求未送达，请核对任务和服务器状态。', true));
  }
  function stopWaiting() {
    cancelJob(job);
    if (pendingEntry) { pendingEntry.answer.textContent = '已请求停止任务。'; pendingEntry.approval.hidden = true; pendingEntry = undefined; }
    generation++; if (!submitting) controller?.abort(); working = false; dialogTest = false; job = ''; stopAuto();
    message('已请求停止；已提交的远端操作可能仍在执行。'); controls();
  }
  async function changeAccess(mode, consent = false) {
    const sessionId = context().session_id;
    if (!sessionId || !allowed) return;
    accessLoading = true; controls();
    try {
      const result = await api('/terminal-ai/access', {session_id: sessionId, mode, consent});
      if (sessionId !== context().session_id) return;
      permission = result.mode;
      if (working && !dialogTest) stopWaiting();
      stopAuto(); message(mode === 'auto' ? '已授权当前连接自动操作，有效 30 分钟' : '操作权限已更新');
    } finally { accessLoading = false; controls(); }
  }
  el('aiPermission').addEventListener('change', async () => {
    const mode = el('aiPermission').value;
    if (mode === 'approve' || mode === 'auto') {
      accessDraft = mode; accessSession = context().session_id;
      el('aiAccessTitle').textContent = mode === 'auto' ? '授权自动操作' : '启用修改审批';
      el('aiAccessTarget').textContent = `当前服务器：${context().target}`;
      el('aiAccessDescription').textContent = mode === 'auto' ? 'AI 可以直接重启容器或重启、重载服务，不再逐次询问，可能短暂中断业务。'
        : 'AI 可以自动检查；重启容器或重启、重载服务前会展示具体操作，由你逐次确认。';
      el('aiAccessConsent').checked = false; el('aiAccessConfirm').disabled = true;
      message('', false, 'aiAccessFeedback'); el('aiAccessDialog').showModal(); controls();
    } else {
      try { await changeAccess(mode); } catch (error) { message(error.message, true); }
    }
  });
  el('aiAccessConsent').addEventListener('change', () => { el('aiAccessConfirm').disabled = !el('aiAccessConsent').checked; });
  el('aiAccessConfirm').addEventListener('click', async () => {
    if (!el('aiAccessConsent').checked || accessSession !== context().session_id || accessLoading) return;
    el('aiAccessConfirm').disabled = true;
    el('aiAccessDialog').querySelectorAll('[data-ai-close]').forEach(button => { button.disabled = true; });
    try { await changeAccess(accessDraft, true); el('aiAccessDialog').close(); }
    catch (error) { message(error.message, true, 'aiAccessFeedback'); el('aiAccessConfirm').disabled = false; }
    finally { el('aiAccessDialog').querySelectorAll('[data-ai-close]').forEach(button => { button.disabled = false; }); }
  });
  el('aiAccessDialog').addEventListener('cancel', event => { if (accessLoading) event.preventDefault(); });
  el('aiAccessDialog').addEventListener('close', () => { accessDraft = ''; accessSession = ''; controls(); });
  el('aiStop').addEventListener('click', stopWaiting);
  document.querySelectorAll('[data-ai-close]').forEach(button => button.addEventListener('click', () => button.closest('dialog').close()));
  el('aiSettingsDialog').addEventListener('close', () => { el('aiKey').value = ''; if (dialogTest) stopWaiting(); });
  window.addEventListener('ssh-session-change', () => {
    const current = context().session_id;
    if (current !== savedSession) {
      if (working && !dialogTest) stopWaiting();
      savedSession = current;
      el('aiAccessDialog').close(); permission = 'readonly'; refreshAccess();
      newChat();
    }
    if (autoSession && context().session_id !== autoSession) stopAuto();
    controls();
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) scheduleAuto(); });
  matchMedia('(max-width:1000px)').addEventListener('change', () => panel(!el('aiPanel').hidden));
  window.addEventListener('pagehide', () => { cancelJob(job); stopAuto(); if (!submitting) controller?.abort(); el('aiKey').value = ''; });
  window.TerminalAI = {viewChanged(view) {
    if (view === 'terminal') { refresh(); controls(); }
    else { stopAuto(); panel(false); if (working) stopWaiting(); }
  }};
  icons(); controls();
  if (activeView === 'terminal') refresh();
})();
