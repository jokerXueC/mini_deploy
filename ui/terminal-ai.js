(() => {
  const el = id => document.getElementById(id);
  el('aiSettingsFeedback').insertAdjacentHTML('beforebegin', '<details class="tai-model-options"><summary>响应设置</summary><label>无响应等待时间（秒，0 表示不限）<input id="aiResponseTimeout" type="number" min="0" step="1" value="0" required></label><label>最大输出 tokens（可选）<input id="aiMaxTokens" type="number" min="1" step="1" placeholder="留空使用服务默认；Claude 为 16384"></label><p class="tai-hint">Claude 协议要求输出额度；填写模型支持的数值。超过服务商额度时，服务商可能拒绝请求。</p></details>');
  let token = '', settings = {}, allowed = false, loading = false, working = false;
  let conversationId = '', job = '', generation = 0, autoTimer, autoSession = '', autoCursor = -1, controller;
  let dialogTest = false, saving = false;
  let pendingEntry;
  let savedSession = '';
  let archiveId = '', historyTarget = '', historyOffset = 0, historyBefore = null, historyLoading = false, historyGeneration = 0;
  let permission = 'approve', accessLoading = false, accessDraft = '', accessSession = '', submitting = 0;
  const layout = el('sshLayout'), resizeHandle = el('aiResizeHandle');
  const aiPanel = el('aiPanel'), expandedDialog = el('aiExpandedDialog');
  let expanded = false;
  let preferredWidth = 330, resizeFrame = 0, drag;
  try {
    const savedWidth = Number(localStorage.getItem('mini-deploy-ai-width'));
    if (Number.isFinite(savedWidth) && savedWidth >= 280) preferredWidth = Math.min(1600, savedWidth);
  } catch { /* Use default width when storage is unavailable. */ }
  function widthBounds() {
    const sidebar = layout.classList.contains('connections-collapsed') ? 52 : innerWidth > 1150 ? 220 : 0;
    return {min: 280, max: Math.max(280, Math.floor(layout.clientWidth - sidebar - 360 - 14))};
  }
  function setPanelWidth(value, persist = false) {
    if (expanded || !matchMedia('(min-width:1001px)').matches || !layout.classList.contains('ai-open')) return;
    const bounds = widthBounds(), width = Math.round(Math.max(bounds.min, Math.min(bounds.max, value)));
    layout.style.setProperty('--tai-width', `${width}px`);
    resizeHandle.setAttribute('aria-valuemin', bounds.min);
    resizeHandle.setAttribute('aria-valuemax', bounds.max);
    resizeHandle.setAttribute('aria-valuenow', width);
    resizeHandle.setAttribute('aria-valuetext', `AI 对话宽度 ${width} 像素`);
    if (persist) {
      preferredWidth = width;
      try { localStorage.setItem('mini-deploy-ai-width', String(width)); } catch { /* Width still applies. */ }
    }
  }
  function endResize(event) {
    if (!drag || event && event.pointerId !== drag.id) return;
    setPanelWidth(drag.width + drag.x - drag.lastX, true);
    const id = drag.id; drag = undefined;
    document.body.classList.remove('tai-resizing');
    if (resizeHandle.hasPointerCapture(id)) resizeHandle.releasePointerCapture(id);
  }
  resizeHandle.addEventListener('pointerdown', event => {
    if (event.button !== 0 || !matchMedia('(min-width:1001px)').matches) return;
    event.preventDefault(); resizeHandle.focus(); resizeHandle.setPointerCapture(event.pointerId);
    drag = {id: event.pointerId, x: event.clientX, lastX: event.clientX, width: el('aiPanel').getBoundingClientRect().width};
    document.body.classList.add('tai-resizing');
  });
  resizeHandle.addEventListener('pointermove', event => {
    if (!drag || drag.id !== event.pointerId) return;
    drag.lastX = event.clientX;
    if (!resizeFrame) resizeFrame = requestAnimationFrame(() => {
      resizeFrame = 0;
      if (drag) setPanelWidth(drag.width + drag.x - drag.lastX);
    });
  });
  ['pointerup', 'pointercancel', 'lostpointercapture'].forEach(type => resizeHandle.addEventListener(type, endResize));
  resizeHandle.addEventListener('dblclick', () => setPanelWidth(330, true));
  resizeHandle.addEventListener('keydown', event => {
    const width = el('aiPanel').getBoundingClientRect().width, bounds = widthBounds();
    const changes = {ArrowLeft: width + 24, ArrowRight: width - 24, Home: bounds.min, End: bounds.max};
    if (event.key in changes) { event.preventDefault(); setPanelWidth(changes[event.key], true); }
  });
  new ResizeObserver(() => { if (!drag) setPanelWidth(preferredWidth); }).observe(layout);
  new MutationObserver(() => { if (!drag) setPanelWidth(preferredWidth); }).observe(layout, {attributes: true, attributeFilter: ['class']});
  window.addEventListener('resize', () => { if (drag) endResize(); setPanelWidth(preferredWidth); });
  window.addEventListener('blur', () => endResize());
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
  function syncPanel() {
    const open = !aiPanel.hidden;
    layout.classList.toggle('ai-open', open && !expanded);
    resizeHandle.hidden = !open || expanded;
    if (open && !expanded) requestAnimationFrame(() => setPanelWidth(preferredWidth));
    el('aiChatOpen').setAttribute('aria-expanded', String(open));
    el('aiBackdrop').hidden = !open || expanded || !matchMedia('(max-width:1000px)').matches;
  }
  function expandPanel(open, focus = true) {
    if (expanded === open) return;
    endResize();
    const scroll = el('aiMessages').scrollTop;
    expanded = open;
    if (open) {
      aiPanel.hidden = false; expandedDialog.append(aiPanel); expandedDialog.showModal();
    } else {
      expandedDialog.close(); resizeHandle.after(aiPanel);
    }
    document.body.classList.toggle('tai-expanded', open);
    const button = el('aiPanelExpand'), icon = button.querySelector('[data-ai-icon]');
    button.title = open ? '恢复侧栏' : '放大 AI 对话';
    button.setAttribute('aria-label', button.title); button.setAttribute('aria-expanded', String(open));
    icon.dataset.aiIcon = open ? 'minimize-2' : 'maximize-2'; icons(button);
    syncPanel(); el('aiMessages').scrollTop = scroll;
    if (focus) button.focus({preventScroll: true});
  }
  function panel(open) {
    if (!open) { endResize(); expandPanel(false, false); }
    aiPanel.hidden = !open; syncPanel();
  }
  function controls() {
    const connected = !!context().session_id;
    const historyMismatch = archiveId && historyTarget && historyTarget !== context().target;
    el('aiAskSend').disabled = working || accessLoading || saving || historyLoading || !!historyMismatch;
    el('aiNewChat').disabled = working || historyLoading;
    el('aiHistoryOpen').disabled = working || historyLoading || !allowed;
    el('aiHistoryOlder').disabled = el('aiHistoryLatest').disabled = working || historyLoading;
    el('aiTest').classList.toggle('is-loading', working && dialogTest);
    el('aiAskSend').classList.toggle('is-loading', working && !dialogTest);
    el('aiShareOutput').disabled = !connected || working;
    el('aiPermission').disabled = !connected || !allowed || accessLoading || saving;
    el('aiPermission').value = connected ? permission : 'chat';
    el('aiHealthCheck').disabled = !connected || !allowed || working || saving || accessLoading || historyLoading;
    document.querySelectorAll('.tai-restore').forEach(button => {
      button.disabled = !connected || !allowed || working || saving || accessLoading || historyLoading
        || !['approve', 'auto', 'command'].includes(permission) || button.dataset.target !== context().target;
    });
    el('aiAuto').disabled = !connected || !el('aiShareOutput').checked || working && !autoSession;
    el('aiStop').hidden = !working;
    el('aiSave').disabled = el('aiTest').disabled = !allowed || working || saving;
    el('aiTargetLabel').textContent = connected ? context().target : '普通对话';
    if (historyMismatch) el('aiTargetLabel').textContent = `连接 ${historyTarget} 后可继续，或新建对话`;
    let scope = connected && el('aiShareOutput').checked ? '问题、对话记录与最近输出（常见凭据会脱敏）' : '问题与对话记录';
    if (connected && permission !== 'chat') scope += '及工具检查结果';
    el('aiSendContext').textContent = settings.configured ? `发送${scope}至 ${settings.base_url}` : '先配置模型';
    window.AppSelects?.syncAll();
  }
  async function refreshAccess() {
    const sessionId = context().session_id;
    if (!sessionId || !allowed) { permission = 'approve'; controls(); return; }
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
    // Allow model settings to open from diagnostics while the terminal view is hidden.
    document.body.append(el('aiSettingsDialog'));
    await refresh();
    el('aiProtocol').value = settings.protocol || 'openai'; el('aiBaseUrl').value = settings.base_url || '';
    el('aiModel').value = settings.model || ''; el('aiKey').value = ''; el('aiClearKey').checked = false;
    el('aiResponseTimeout').value = settings.response_timeout ?? 0;
    el('aiMaxTokens').value = settings.max_tokens ?? '';
    el('aiKey').placeholder = settings.has_key ? '已保存密钥；留空保留，输入新密钥可替换' : '输入 API Key；无密钥的本机服务可留空';
    protocolHint();
    window.AppSelects?.syncAll();
    settingsFeedback();
    el('aiSettingsDialog').showModal(); controls();
  }
  const draft = () => ({protocol: el('aiProtocol').value, base_url: el('aiBaseUrl').value.trim(),
    model: el('aiModel').value.trim(), api_key: el('aiKey').value, clear_key: el('aiClearKey').checked,
    response_timeout: Number(el('aiResponseTimeout').value),
    max_tokens: el('aiMaxTokens').value ? Number(el('aiMaxTokens').value) : null});
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
  function appendEntry(asked, bounded = true) {
    el('aiEmpty')?.remove();
    while (bounded && el('aiMessages').children.length >= 12) el('aiMessages').firstElementChild.remove();
    const entry = document.createElement('article'); entry.className = 'tai-entry';
    const q = document.createElement('p'); q.className = 'tai-question'; q.textContent = asked;
    const answer = document.createElement('div'); answer.className = 'tai-answer';
    const loading = document.createElement('div'); loading.className = 'tai-loading'; loading.setAttribute('role', 'status');
    const spinner = document.createElement('i'); spinner.className = 'tai-icon'; spinner.dataset.aiIcon = 'loader-circle';
    const label = document.createElement('span'); label.textContent = '等待模型回复';
    const elapsed = document.createElement('span'); elapsed.className = 'tai-elapsed';
    loading.append(spinner, label, elapsed); icons(loading);
    const tools = document.createElement('div'); tools.className = 'tai-steps';
    const approval = document.createElement('div'); approval.className = 'tai-approval'; approval.hidden = true;
    const meta = document.createElement('p'); meta.className = 'tai-entry-meta';
    entry.append(q, tools, approval, loading, answer, meta); entry.setAttribute('aria-busy', 'true');
    el('aiMessages').append(entry); el('aiMessages').scrollTop = el('aiMessages').scrollHeight;
    return {root: entry, answer, loading, label, elapsed, started: Date.now(), meta, tools, approval, steps: new Map(), approvalId: ''};
  }
  function finishEntry(entry) {
    entry.loading.hidden = true; entry.root.setAttribute('aria-busy', 'false');
  }
  function renderProgress(entry, result, id) {
    if (!entry) return;
    const running = (result.events || []).find(step => step.status === 'running');
    entry.loading.hidden = result.status === 'awaiting_approval' || ['done', 'failed', 'cancelled'].includes(result.status);
    const phases = {preview: '正在预览差异', approval: '等待操作确认', execute: '正在执行', verify: '正在验证结果'};
    entry.label.textContent = result.stage === 'tools' && running ? `${phases[running.phase] || '正在检查'}：${running.title}`
      : result.events?.length ? '正在整理检查结果' : '等待模型回复';
    if (result.status === 'cancelling') entry.label.textContent = '正在停止，后续操作不会继续';
    const elapsed = Math.floor((Date.now() - entry.started) / 1000);
    entry.elapsed.textContent = elapsed ? `${elapsed} 秒` : '';
    const labels = {running: '执行中', awaiting_approval: '等待确认', done: '已完成', failed: '未完成', blocked: '未执行', cancelled: '已停止'};
    for (const step of result.events || []) {
      let row = entry.steps.get(step.id);
      if (!row) {
        const details = document.createElement('details'); details.className = 'tai-step';
        const summary = document.createElement('summary'); const pre = document.createElement('pre');
        const outcome = document.createElement('p'); outcome.className = 'tai-verification'; outcome.hidden = true;
        const restore = document.createElement('button'); restore.type = 'button'; restore.className = 'tai-text-button tai-restore';
        restore.hidden = true; restore.title = '预览原配置与当前配置的差异，恢复前保留当前版本';
        const icon = document.createElement('i'); icon.className = 'tai-icon'; icon.dataset.aiIcon = 'corner-up-left';
        restore.append(icon, document.createTextNode('恢复备份')); icons(restore);
        restore.addEventListener('click', () => runOperation('restore', {job_id: step.job_id, event_id: step.id}, restore.dataset.target));
        details.append(summary, pre, outcome, restore); entry.tools.append(details);
        row = {details, summary, pre, outcome, restore}; entry.steps.set(step.id, row);
      }
      if (row.details.dataset.state !== step.status) row.details.dataset.state = step.status;
      const title = `${step.title} · ${labels[step.status] || step.status}`;
      if (row.summary.textContent !== title) row.summary.textContent = title;
      const content = [step.command, step.preview?.diff, step.result].filter(Boolean).join('\n\n');
      if (row.pre.textContent !== content) row.pre.textContent = content;
      if (step.verification) {
        row.outcome.hidden = false; row.outcome.dataset.state = step.verification.status;
        row.outcome.textContent = `${step.verification.status === 'passed' ? '验证通过' : '验证未通过'} · ${step.verification.command || step.verification.detail}`;
      }
      if (step.recovery && step.job_id) {
        row.restore.hidden = false; row.restore.dataset.target = result.target || historyTarget;
        row.restore.disabled = working || !context().session_id || !allowed
          || !['approve', 'auto', 'command'].includes(permission) || row.restore.dataset.target !== context().target;
      }
    }
    if (!result.approval) {
      entry.approval.hidden = true; entry.approvalId = '';
      if (entry.answer.textContent === '确认后继续排查') entry.answer.textContent = '';
      return;
    }
    entry.answer.textContent = '确认后继续排查'; message('有操作需要你确认');
    if (entry.approvalId === result.approval.id) return;
    const approval = result.approval; entry.approvalId = approval.id;
    entry.approval.replaceChildren(); entry.approval.hidden = false;
    const title = document.createElement('strong'); title.textContent = approval.title;
    const target = document.createElement('p'); target.textContent = `服务器：${approval.target}`;
    const reason = document.createElement('p'); reason.textContent = approval.reason || ''; reason.hidden = !approval.reason;
    const command = document.createElement('pre'); command.textContent = approval.command;
    const preview = document.createElement('div'); preview.className = 'tai-preview'; preview.hidden = !approval.preview;
    if (approval.preview) {
      const path = document.createElement('p'); path.textContent = `文件：${approval.preview.path}`;
      const diff = document.createElement('pre'); diff.className = 'tai-diff';
      for (const line of (approval.preview.diff || '内容相同，无需替换。').split('\n')) {
        const span = document.createElement('span'); span.textContent = line;
        if (line.startsWith('+')) span.className = 'tai-diff-added';
        if (line.startsWith('-')) span.className = 'tai-diff-removed';
        diff.append(span);
      }
      const notice = document.createElement('p'); notice.textContent = approval.preview.notice + (approval.preview.truncated ? ' 差异较长，仅显示前一部分。' : '');
      preview.append(path, diff, notice); command.hidden = true;
      const original = document.createElement('details'); const caption = document.createElement('summary'); caption.textContent = '查看完整操作参数';
      original.append(caption, command); command.hidden = false; preview.append(original);
    }
    const verification = document.createElement('p'); verification.hidden = !approval.validation;
    verification.textContent = approval.validation ? `执行后校验：${approval.validation}` : '';
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
    entry.approval.append(title, target, reason);
    if (!approval.preview) entry.approval.append(command);
    entry.approval.append(preview, verification, impact, actions);
    const messages = el('aiMessages');
    messages.scrollTop += entry.approval.getBoundingClientRect().top - messages.getBoundingClientRect().top - 10;
  }
  function renderAnswer(target, text, options = {}) {
    target.replaceChildren();
    if (!window.marked || !window.DOMPurify) { target.textContent = text; return; }
    // Parse Markdown, then retain only formatting elements; model HTML cannot create active UI.
    const fragment = DOMPurify.sanitize(marked.parse(String(text), {gfm: true, breaks: true, async: false}), {
      RETURN_DOM_FRAGMENT: true,
      ALLOWED_TAGS: ['p', 'br', 'strong', 'em', 'del', 'code', 'pre', 'ul', 'ol', 'li',
        'blockquote', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'a'],
      ALLOWED_ATTR: ['href', 'title', 'start'], ALLOW_DATA_ATTR: false, ALLOW_ARIA_ATTR: false
    });
    fragment.querySelectorAll('a').forEach(link => {
      try {
        if (!/^https?:\/\//i.test(link.getAttribute('href') || '')) throw new Error();
        const url = new URL(link.getAttribute('href'));
        if (!url.hostname || url.username || url.password) throw new Error();
        link.target = '_blank'; link.rel = 'noopener noreferrer'; link.referrerPolicy = 'no-referrer';
      } catch { link.replaceWith(document.createTextNode(link.textContent)); }
    });
    fragment.querySelectorAll('table').forEach(table => {
      const scroll = document.createElement('div'); scroll.className = 'tai-table-scroll';
      scroll.tabIndex = 0; scroll.setAttribute('role', 'region'); scroll.setAttribute('aria-label', 'AI 回复表格');
      table.replaceWith(scroll); scroll.append(table);
    });
    fragment.querySelectorAll('pre').forEach(pre => {
      const block = document.createElement('div'); block.className = 'tai-command';
      const command = pre.textContent.trimEnd();
      // Strip HTML formatting from fenced or raw code blocks before copying to the terminal.
      pre.textContent = command; pre.replaceWith(block);
      const actions = document.createElement('div'); actions.className = 'tai-command-actions';
      const copy = document.createElement('button'); copy.type = 'button'; copy.className = 'tai-text-button'; copy.textContent = '复制';
      copy.addEventListener('click', async () => {
        try { await navigator.clipboard.writeText(command); message('命令已复制，请核对后执行。'); }
        catch { message('请选中命令后使用系统复制。'); }
      });
      actions.append(copy);
      if (options.insertCommand !== false && command && !/[\x00-\x1f\x7f]/.test(command) && command.length <= 3000) {
        const fill = document.createElement('button'); fill.type = 'button'; fill.className = 'tai-text-button'; fill.textContent = '填入终端';
        fill.addEventListener('click', () => {
          if (!window.SSHTerminal.insertCommand(command)) message('请先连接 SSH，并确认命令内容。');
        });
        actions.append(fill);
      }
      block.append(pre, actions);
    });
    target.append(fragment);
  }
  async function waitForJob(id, current, entry) {
    while (current === generation) {
      const result = await api(`/terminal-ai/jobs?id=${encodeURIComponent(id)}`);
      if (current !== generation) return null;
      renderProgress(entry, result, id);
      if (result.partial_text && entry.streamed !== result.partial_text) {
        const messages = el('aiMessages'), follow = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 80;
        renderAnswer(entry.answer, result.partial_text); entry.streamed = result.partial_text;
        if (follow) messages.scrollTop = messages.scrollHeight;
      }
      if (result.status === 'failed') throw new Error([result.partial_text, result.detail].filter(Boolean).join('\n\n'));
      if (result.status === 'cancelled') throw new Error(result.detail || '任务已停止。');
      if (result.status === 'done') return result;
      await new Promise(resolve => setTimeout(resolve, 700));
    }
    return null;
  }
  async function ask(asked, automatic = false) {
    if (working || saving || accessLoading || historyLoading || !asked.trim()) return;
    if (archiveId && historyTarget && historyTarget !== context().target) {
      message('请连接历史对话对应的服务器，或新建对话。', true); return;
    }
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
        include_terminal: includeTerminal, consent: true, question: asked, automatic, history_id: archiveId,
        permission: sessionId ? permission : 'chat',
        provider: {base_url: settings.base_url, protocol: settings.protocol, model: settings.model}});
      if (submitting === current) submitting = 0;
      if (current !== generation) { cancelJob(submitted.id); return; }
      conversationId = submitted.conversation_id;
      archiveId = submitted.history_id; historyTarget = context().target;
      if (!automatic && el('aiQuestion').value.trim() === asked) el('aiQuestion').value = '';
      job = submitted.id;
      const result = await waitForJob(job, current, entry);
      if (!result || current !== generation) return;
      renderAnswer(entry.answer, result.text);
      finishEntry(entry);
      const usage = result.usage || {}; const tokens = usage.total_tokens ?? ((usage.input_tokens || 0) + (usage.output_tokens || 0));
      const scope = result.context?.lines ? `最近输出 ${result.context.lines} 行` : '未附带终端输出';
      entry.meta.textContent = `${result.model} · ${scope}${result.events?.length ? ` · ${result.events.length} 项检查` : ''}${tokens ? ` · ${tokens} tokens` : ''}`;
      message(result.history_error || result.notice || '可继续追问', !!result.history_error);
      el('aiMessages').scrollTop = el('aiMessages').scrollHeight;
    } catch (error) {
      if (current === generation) {
        entry.answer.textContent = error.message; entry.answer.classList.add('tai-error'); message(error.message, true); stopAuto();
        finishEntry(entry);
        if (error.status === 404 || error.status === 409 && /连接或模型|模型配置/.test(error.message)) conversationId = '';
      }
    } finally {
      if (submitting === current) submitting = 0;
      if (current === generation) { working = false; job = ''; pendingEntry = undefined; controls(); await refreshAccess(); scheduleAuto(); }
    }
  }
  async function runOperation(action, data = {}, target = context().target) {
    if (working || saving || accessLoading || historyLoading || !allowed) return;
    const sessionId = context().session_id;
    if (!sessionId || !target || target !== context().target) { message('请先连接这条记录对应的服务器。', true); return; }
    stopAuto(); panel(true); working = true; controls();
    const current = ++generation;
    const entry = appendEntry(action === 'health' ? '服务器只读体检' : '预览并恢复配置备份'); pendingEntry = entry;
    message(action === 'health' ? '正在采集服务器状态…' : '正在读取备份并预览差异…');
    try {
      submitting = current;
      const submitted = await api(`/terminal-ai/${action}`, {session_id: sessionId, ...data});
      if (submitting === current) submitting = 0;
      if (current !== generation) { cancelJob(submitted.id); return; }
      conversationId = ''; archiveId = submitted.history_id; historyTarget = target;
      el('aiHistoryBar').hidden = true;
      job = submitted.id;
      const result = await waitForJob(job, current, entry);
      if (!result || current !== generation) return;
      renderAnswer(entry.answer, result.text); finishEntry(entry);
      entry.meta.textContent = `${target} · ${result.events.length} 项操作 · 已保存记录`;
      message(result.history_error || '操作完成，可继续提问', !!result.history_error);
    } catch (error) {
      if (current === generation) {
        entry.answer.textContent = error.message; entry.answer.classList.add('tai-error');
        finishEntry(entry); message(error.message, true);
      }
    } finally {
      if (submitting === current) submitting = 0;
      if (current === generation) { working = false; job = ''; pendingEntry = undefined; controls(); await refreshAccess(); }
    }
  }
  el('aiHealthCheck').addEventListener('click', () => runOperation('health'));
  function newChat() {
    conversationId = ''; archiveId = ''; historyTarget = ''; historyBefore = null; historyGeneration++; stopAuto();
    el('aiHistoryBar').hidden = true;
    el('aiMessages').replaceChildren();
    const empty = document.createElement('div'); empty.id = 'aiEmpty'; empty.className = 'tai-empty';
    const title = document.createElement('strong'); title.textContent = '想排查什么问题？';
    const hint = document.createElement('p'); hint.textContent = '例如：这段报错是什么原因？';
    empty.append(title, hint); el('aiMessages').append(empty); message('等待提问'); controls();
  }
  const historyDate = value => new Date(value * 1000).toLocaleString('zh-CN', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'});
  async function listHistory(offset = 0) {
    if (!allowed || working || historyLoading) return;
    historyLoading = true; controls();
    if (!el('aiHistoryDialog').open) el('aiHistoryDialog').showModal();
    message('正在读取历史记录…', false, 'aiHistoryFeedback');
    try {
      const result = await api(`/terminal-ai/history?offset=${offset}`);
      if (!offset) el('aiHistoryList').replaceChildren();
      for (const item of result.conversations) {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'tai-history-row';
        const title = document.createElement('strong'); title.textContent = item.title;
        const detail = document.createElement('span'); detail.textContent = `${item.target || '普通对话'} · ${historyDate(item.updated)} · ${item.model}`;
        button.append(title, detail); button.addEventListener('click', () => {
          if (item.target?.startsWith('面板主机：')) { el('aiHistoryDialog').close(); window.PanelAI.restore(item.id); }
          else openHistory(item.id);
        });
        el('aiHistoryList').append(button);
      }
      historyOffset = result.next_offset;
      el('aiHistoryMore').hidden = historyOffset === null;
      message(el('aiHistoryList').children.length ? '' : '暂无对话历史，发送第一条问题后会自动保存。', false, 'aiHistoryFeedback');
    } catch (error) { message(error.message, true, 'aiHistoryFeedback'); }
    finally { historyLoading = false; controls(); }
  }
  async function openHistory(id, before = null) {
    if (working || historyLoading) return;
    historyLoading = true; controls();
    const current = ++historyGeneration;
    try {
      const result = await api(`/terminal-ai/history?id=${encodeURIComponent(id)}${before ? `&before=${before}` : ''}`);
      if (current !== historyGeneration) return;
      stopAuto(); conversationId = ''; archiveId = id; historyTarget = result.conversation.target;
      historyBefore = result.next_before;
      el('aiHistoryDialog').close(); panel(true); el('aiMessages').replaceChildren();
      const status = {failed: '未完成', cancelled: '已停止', running: '本轮尚未完成或已中断'};
      for (const turn of result.turns) {
        const entry = appendEntry(turn.question, false);
        const events = turn.events.map(event => ['running', 'awaiting_approval'].includes(event.status)
          ? {...event, status: 'cancelled'} : event);
        renderProgress(entry, {events, status: 'done', target: historyTarget}, '');
        renderAnswer(entry.answer, turn.answer || status[turn.status] || '本轮没有回复'); finishEntry(entry);
        entry.meta.textContent = `${historyDate(turn.created)}${status[turn.status] ? ` · ${status[turn.status]}` : ''}`;
      }
      el('aiHistoryBar').hidden = false;
      el('aiHistoryCaption').textContent = result.conversation.title;
      el('aiHistoryOlder').hidden = historyBefore === null;
      el('aiHistoryLatest').hidden = !before;
      message(historyTarget && historyTarget !== context().target ? '这是历史记录，连接对应服务器后可继续。' : '历史已加载，可继续追问');
    } catch (error) { message(error.message, true, el('aiHistoryDialog').open ? 'aiHistoryFeedback' : 'aiFeedback'); }
    finally { historyLoading = false; controls(); }
  }
  el('aiHistoryOpen').addEventListener('click', () => listHistory());
  el('aiHistoryMore').addEventListener('click', () => { if (historyOffset !== null) listHistory(historyOffset); });
  el('aiHistoryOlder').addEventListener('click', () => { if (historyBefore !== null) openHistory(archiveId, historyBefore); });
  el('aiHistoryLatest').addEventListener('click', () => openHistory(archiveId));
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
  el('aiPanelExpand').addEventListener('click', () => expandPanel(!expanded));
  expandedDialog.addEventListener('cancel', event => { event.preventDefault(); expandPanel(false); });
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
    if (pendingEntry) {
      pendingEntry.answer.textContent = '已请求停止任务。'; pendingEntry.approval.hidden = true; finishEntry(pendingEntry); pendingEntry = undefined;
    }
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
      stopAuto(); message(mode === 'command' ? '已开启通用命令，每条均需确认，有效 30 分钟'
        : mode === 'auto' ? '已授权当前连接自动操作，有效 30 分钟' : '操作权限已更新');
    } finally { accessLoading = false; controls(); }
  }
  el('aiPermission').addEventListener('change', async () => {
    const mode = el('aiPermission').value;
    if (mode === 'auto' || mode === 'command') {
      accessDraft = mode; accessSession = context().session_id;
      el('aiAccessTitle').textContent = mode === 'command' ? '授权通用命令' : '授权自动操作';
      el('aiAccessTarget').textContent = `当前服务器：${context().target}`;
      el('aiAccessDescription').textContent = mode === 'command'
        ? 'AI 可调用服务器程序、Docker 容器内命令和数据库客户端。每条通用命令都先展示服务器、命令和用途，由你确认后执行；受控的只读检查仍自动执行。'
        : 'AI 可以创建或编辑文件、启停容器及服务，不再逐次询问。已有文件先备份并校验版本；配置变更和服务操作可能中断业务。';
      el('aiAccessPolicy').textContent = mode === 'command'
        ? '通用命令可能修改或删除数据，无法保证“禁止删除”，也不会自动备份或回滚。请核对每条命令；需要严格保留文件时，请继续使用“修改需确认”等受控模式。停止或超时不保证远端程序终止。'
        : '受控模式支持文件读取、搜索、备份后编辑及服务/容器管理；禁止文件删除、清理和任意脚本执行。通用命令需要单独授权。';
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
      el('aiAccessDialog').close(); permission = 'approve'; refreshAccess();
      newChat();
    }
    if (autoSession && context().session_id !== autoSession) stopAuto();
    controls();
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) scheduleAuto(); });
  matchMedia('(max-width:1000px)').addEventListener('change', syncPanel);
  window.addEventListener('pagehide', () => { cancelJob(job); stopAuto(); if (!submitting) controller?.abort(); el('aiKey').value = ''; });
  window.TerminalAI = {renderAnswer, openSettings, prepareAnalysis(text) {
    panel(true);
    const question = el('aiQuestion');
    question.value = question.value.trim() ? `${question.value}\n\n${text}` : text;
    question.focus();
  }, viewChanged(view) {
    if (view === 'terminal') { refresh(); controls(); }
    else { stopAuto(); panel(false); if (working) stopWaiting(); }
  }};
  icons(); controls();
  if (activeView === 'terminal') refresh();
})();
