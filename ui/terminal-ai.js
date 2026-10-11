(() => {
  const el = id => document.getElementById(id);
  let token = '', settings = {}, secure = false, loading = false, working = false;
  let snapshot, question = '', job = '', generation = 0, autoTimer, autoSession = '', autoCursor = -1, controller;
  let dialogTest = false, saving = false;
  let pendingEntry;
  let savedSession = '';
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
      if (!response.ok) throw new Error(result.detail || (response.status === 401 ? '登录已过期，请重新登录。' : '模型操作失败，请刷新重试。'));
      return result;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('请求已停止或超时，请检查连接。');
      throw error;
    } finally { clearTimeout(timer); if (controller === abort) controller = undefined; }
  }
  function panel(open) {
    el('aiPanel').hidden = !open; el('sshLayout').classList.toggle('ai-open', open);
    el('aiPanelToggle').setAttribute('aria-expanded', String(open));
    el('aiPanelToggle').setAttribute('aria-label', open ? '收起诊断助手' : '展开诊断助手');
    el('aiPanelToggle').title = open ? '收起诊断助手' : '展开诊断助手';
    el('aiPanelToggle').querySelector('i').dataset.aiIcon = open ? 'panel-right-close' : 'panel-right-open';
    icons(el('aiPanelToggle'));
    el('aiBackdrop').hidden = !open || !matchMedia('(max-width:1000px)').matches;
  }
  function controls() {
    const connected = !!context().session_id;
    el('aiAnalyzeOutput').disabled = working;
    el('aiAskSend').disabled = working;
    el('aiAuto').disabled = working && !autoSession;
    el('aiStop').hidden = !working;
    el('aiSave').disabled = el('aiTest').disabled = !secure || working || saving;
    el('aiTargetLabel').textContent = connected ? context().target : '尚未连接 SSH';
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
      secure = settings.secure && window.isSecureContext;
      renderSettings();
    } catch (error) { message(error.message, true); }
    finally { loading = false; controls(); }
  }
  function protocolHint() {
    const claude = el('aiProtocol').value === 'anthropic';
    el('aiBaseUrl').placeholder = claude ? 'https://api.anthropic.com/v1' : 'https://api.openai.com/v1';
    el('aiEndpointHint').textContent = claude ? '填写 Claude 原生接口地址，例如 https://api.anthropic.com/v1；系统使用 Messages API。'
      : '填写服务商的 Base URL；本机 Ollama 可填 http://127.0.0.1:11434/v1。';
  }
  async function openSettings() {
    await refresh();
    el('aiProtocol').value = settings.protocol || 'openai'; el('aiBaseUrl').value = settings.base_url || '';
    el('aiModel').value = settings.model || ''; el('aiKey').value = ''; el('aiClearKey').checked = false;
    el('aiKey').placeholder = settings.has_key ? '已保存密钥；留空保留，输入新密钥可替换' : '输入 API Key；无密钥的本机服务可留空';
    protocolHint();
    window.AppSelects?.syncAll();
    message(secure ? '无需连接 SSH 即可配置和测试模型。' : '通过 HTTPS 或 SSH 本地隧道打开面板后，才能安全保存模型密钥。', !secure, 'aiSettingsFeedback');
    el('aiSettingsDialog').showModal(); controls();
  }
  const draft = () => ({protocol: el('aiProtocol').value, base_url: el('aiBaseUrl').value.trim(),
    model: el('aiModel').value.trim(), api_key: el('aiKey').value, clear_key: el('aiClearKey').checked});
  function stopAuto() {
    clearTimeout(autoTimer); autoTimer = undefined; autoSession = ''; autoCursor = -1;
    el('aiAuto').checked = false; el('aiScopeLabel').textContent = '按需分析';
  }
  function scheduleAuto() {
    clearTimeout(autoTimer);
    if (!autoSession || working) return;
    autoTimer = setTimeout(async () => {
      if (!autoSession || activeView !== 'terminal' || document.hidden || context().session_id !== autoSession) {
        if (context().session_id !== autoSession || activeView !== 'terminal') stopAuto(); else scheduleAuto();
        return;
      }
      if (context().cursor === autoCursor) { scheduleAuto(); return; }
      autoCursor = context().cursor;
      try {
        const current = autoSession;
        const prepared = await api('/terminal-ai/prepare', {session_id: current});
        if (autoSession !== current || context().session_id !== current) return;
        await sendAnalysis(prepared, '分析新增的终端输出，是否出现新的异常？', current);
      } catch (error) { stopAuto(); message(error.message, true); }
    }, 30000);
  }
  function appendEntry(asked) {
    el('aiEmpty')?.remove();
    while (el('aiMessages').children.length >= 12) el('aiMessages').firstElementChild.remove();
    const entry = document.createElement('article'); entry.className = 'tai-entry';
    const q = document.createElement('p'); q.className = 'tai-question'; q.textContent = asked;
    const answer = document.createElement('div'); answer.className = 'tai-answer'; answer.textContent = '正在分析…';
    const meta = document.createElement('p'); meta.className = 'tai-entry-meta';
    entry.append(q, answer, meta); el('aiMessages').append(entry); el('aiMessages').scrollTop = el('aiMessages').scrollHeight;
    return {answer, meta};
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
  async function waitForJob(id, current) {
    const deadline = Date.now() + 90000;
    while (current === generation && Date.now() < deadline) {
      const result = await api(`/terminal-ai/jobs?id=${encodeURIComponent(id)}`);
      if (current !== generation) return null;
      if (result.status === 'failed') throw new Error(result.detail);
      if (result.status === 'done') return result;
      await new Promise(resolve => setTimeout(resolve, 700));
    }
    if (current === generation) throw new Error('模型响应较慢，已停止等待，请稍后重试。');
    return null;
  }
  async function sendAnalysis(prepared, asked, sessionId) {
    if (working) return;
    working = true; panel(true); controls(); message('正在分析选定片段…');
    const current = ++generation; const entry = appendEntry(asked); pendingEntry = entry;
    try {
      const submitted = await api('/terminal-ai/analyze', {session_id: sessionId, snapshot_id: prepared.id,
        consent: true, question: asked});
      if (current !== generation) return;
      job = submitted.id;
      const result = await waitForJob(job, current);
      if (!result || current !== generation) return;
      renderAnswer(entry.answer, result.text);
      const usage = result.usage || {}; const tokens = usage.total_tokens ?? ((usage.input_tokens || 0) + (usage.output_tokens || 0));
      entry.meta.textContent = `${result.model} · ${prepared.lines} 行${tokens ? ` · ${tokens} tokens` : ''}`;
      message('分析完成；建议命令不会自动执行。');
    } catch (error) {
      if (current === generation) { entry.answer.textContent = error.message; entry.answer.classList.add('tai-error'); message(error.message, true); stopAuto(); }
    } finally {
      if (current === generation) { working = false; job = ''; pendingEntry = undefined; controls(); scheduleAuto(); }
    }
  }
  async function preview(asked, continuous = false) {
    if (working) return;
    if (!settings.configured) { await openSettings(); return; }
    if (!secure) { message('请通过 HTTPS 或 SSH 本地隧道使用终端分析。', true); panel(true); return; }
    const current = context();
    if (!current.session_id) { message('请先连接 SSH，再选择需要分析的输出。', true); panel(true); stopAuto(); return; }
    try {
      snapshot = await api('/terminal-ai/prepare', {session_id: current.session_id, selection: current.selection || undefined});
      if (context().session_id !== current.session_id) { message('连接已改变，请重新选择片段。', true); return; }
      snapshot.session_id = current.session_id; question = asked || '请分析这段终端输出，说明原因和建议下一步。';
      el('aiPreviewText').textContent = snapshot.text;
      el('aiPreviewDestination').textContent = `发送到 ${snapshot.destination} · ${snapshot.model}`;
      el('aiPreviewCount').textContent = `${current.selection ? '选中片段' : '最近输出'}：${snapshot.lines} 行，${snapshot.characters} 字`;
      el('aiConsent').checked = false; el('aiPreviewSend').disabled = true;
      el('aiContinuousConsent').checked = continuous; el('aiPreviewDialog').showModal();
    } catch (error) { stopAuto(); message(error.message, true); panel(true); }
  }
  el('aiSettingsOpen').addEventListener('click', openSettings);
  el('aiPanelModel').addEventListener('click', openSettings);
  el('aiProtocol').addEventListener('change', protocolHint);
  el('aiSettingsForm').addEventListener('submit', async event => {
    event.preventDefault(); if (!secure || working || saving) return;
    saving = true; controls();
    try {
      settings = await api('/terminal-ai/settings', draft()); stopAuto(); renderSettings();
      el('aiKey').value = ''; el('aiSettingsDialog').close(); message('模型设置已保存。');
    } catch (error) { message(error.message, true, 'aiSettingsFeedback'); }
    finally { saving = false; controls(); }
  });
  el('aiTest').addEventListener('click', async () => {
    if (!secure || working || saving || !el('aiSettingsForm').reportValidity()) return;
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
  el('aiPanelToggle').addEventListener('click', () => panel(el('aiPanel').hidden));
  el('aiPanelClose').addEventListener('click', () => panel(false));
  el('aiBackdrop').addEventListener('click', () => panel(false));
  el('aiAnalyzeOutput').addEventListener('click', () => preview(el('aiQuestion').value.trim()));
  el('aiAskForm').addEventListener('submit', event => { event.preventDefault(); preview(el('aiQuestion').value.trim()); });
  el('aiConsent').addEventListener('change', () => { el('aiPreviewSend').disabled = !el('aiConsent').checked; });
  el('aiPreviewSend').addEventListener('click', () => {
    if (!el('aiConsent').checked || !snapshot || working) return;
    if (context().session_id !== snapshot.session_id) { el('aiPreviewDialog').close(); message('连接已改变，请重新预览。', true); return; }
    if (el('aiContinuousConsent').checked) {
      autoSession = snapshot.session_id; autoCursor = context().cursor;
      el('aiAuto').checked = true; el('aiScopeLabel').textContent = '新输出 · 每 30 秒';
    }
    el('aiPreviewDialog').close(); el('aiQuestion').value = ''; sendAnalysis(snapshot, question, snapshot.session_id);
  });
  el('aiAuto').addEventListener('change', () => {
    if (!el('aiAuto').checked) stopAuto();
    else { el('aiAuto').checked = false; preview('检查最近输出是否存在异常。', true); }
  });
  function stopWaiting() {
    if (pendingEntry) { pendingEntry.answer.textContent = '已停止等待本次分析。'; pendingEntry = undefined; }
    generation++; controller?.abort(); working = false; dialogTest = false; job = ''; stopAuto();
    message('已停止等待；已发送的模型请求可能仍在处理。'); controls();
  }
  el('aiStop').addEventListener('click', stopWaiting);
  document.querySelectorAll('[data-ai-close]').forEach(button => button.addEventListener('click', () => button.closest('dialog').close()));
  el('aiSettingsDialog').addEventListener('close', () => { el('aiKey').value = ''; if (dialogTest) stopWaiting(); });
  window.addEventListener('ssh-session-change', () => {
    const current = context().session_id;
    if (current !== savedSession) {
      if (savedSession && working && !dialogTest) stopWaiting();
      savedSession = current;
      if (el('aiPreviewDialog').open) el('aiPreviewDialog').close();
    }
    if (autoSession && context().session_id !== autoSession) stopAuto();
    controls();
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) scheduleAuto(); });
  matchMedia('(max-width:1000px)').addEventListener('change', () => panel(!el('aiPanel').hidden));
  window.addEventListener('pagehide', () => { stopAuto(); controller?.abort(); el('aiKey').value = ''; });
  window.TerminalAI = {viewChanged(view) {
    if (view === 'terminal') { refresh(); controls(); }
    else { stopAuto(); panel(false); if (working) stopWaiting(); }
  }};
  icons(); controls();
  if (activeView === 'terminal') refresh();
})();
