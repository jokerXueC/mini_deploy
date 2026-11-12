(() => {
  const el = id => document.getElementById(id);
  let token = '', profiles = [], profileId = '', session = '', cursor = 0;
  let terminal, fit, generation = 0, busy = false, allowed = false, available = false;
  let allowingHttp = false, refreshPromise, suspended = false, loginResolve;
  const sessionKey = 'mini-deploy-ssh-session';
  const connections = el('sshForm').closest('.ssh-connections');
  const login = document.createElement('dialog'); login.id = 'sshLoginDialog'; login.className = 'ssh-login-dialog';
  login.setAttribute('aria-labelledby', 'sshLoginTitle');
  login.innerHTML = '<header class="ssh-login-head"><div><h2 id="sshLoginTitle">登录服务器</h2><p>与远程终端共用连接</p></div><button id="sshLoginClose" class="toolbar-icon" type="button" title="关闭" aria-label="关闭服务器登录"><img src="ui/previews/ssh-ai/icons/x.svg" alt=""></button></header><div class="ssh-login-body"></div>';
  document.body.append(login);
  // Fingerprint verification must also work while the terminal page is hidden.
  document.body.append(el('sshFingerprintDialog'));
  const loginParts = ['sshSecurityNotice', connections, 'sshFeedback', 'sshRelogin'].map(value => {
    const node = typeof value === 'string' ? el(value) : value;
    const anchor = document.createComment('ssh-login-home'); node.before(anchor);
    return {node, anchor};
  });
  let input = '', inputTimer, sending = false, resizeTimer, lastSize = '', outputController;
  const feedback = (message, kind = 'info') => { el('sshFeedback').textContent = message; el('sshFeedback').dataset.kind = kind; };
  const credentials = () => { ['sshPassword', 'sshPrivateKey', 'sshPassphrase', 'sshKeyFile'].forEach(id => { el(id).value = ''; }); };
  const bytes64 = text => btoa(String.fromCharCode(...new TextEncoder().encode(text)));
  const spec = () => ({host: el('sshHost').value.trim(), port: Number(el('sshPort').value), username: el('sshUsername').value.trim()});
  const state = (text, connected = false) => {
    if (el('sshState').textContent !== text) el('sshState').textContent = text;
    el('sshState').classList.toggle('connected', connected);
    el('sshState').dataset.state = connected ? 'connected' : text === '连接失败' ? 'error' : text === '网络重试中' ? 'waiting' : 'idle';
  };
  function collapseConnections(collapsed) {
    el('sshForm').closest('.ssh-connections').classList.toggle('collapsed', collapsed);
    el('sshLayout').classList.toggle('connections-collapsed', collapsed);
    el('sshConnectionToggle').setAttribute('aria-expanded', String(!collapsed));
    el('sshConnectionToggle').title = collapsed ? '展开连接设置' : '收起连接设置';
    try { localStorage.setItem('mini-deploy-ssh-collapsed', String(collapsed)); } catch { /* Storage may be disabled. */ }
    requestAnimationFrame(resize);
  }

  async function api(path, body, {keepalive = false, signal, raw = false} = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), path === '/ssh/connect' ? 60000 : 35000);
    try {
      const response = await fetch(path, {method: body ? 'POST' : 'GET', credentials: 'same-origin',
        headers: body ? {'Content-Type': 'application/json', 'X-CSRF-Token': token} : {},
        body: body ? JSON.stringify(body) : undefined, signal: signal || controller.signal, keepalive});
      const data = raw && response.ok ? null : await response.json().catch(() => ({}));
      if (!response.ok) {
        const error = new Error(data.detail || (response.status === 401 ? '登录已过期，请刷新面板重新登录。' : '终端请求失败。'));
        error.status = response.status;
        throw error;
      }
      return raw ? response : data;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('连接超时，请检查网络后重试。');
      throw error;
    } finally { clearTimeout(timer); }
  }

  function controls() {
    el('sshFields').disabled = !allowed || !available || busy;
    el('sshConnect').disabled = (!!session && !login.open) || busy;
    el('sshLoginClose').disabled = busy;
    el('sshDisconnect').disabled = !session;
    ['sshCopy', 'sshPaste', 'sshClear'].forEach(id => { el(id).disabled = !terminal; });
    el('sshPaste').disabled = !session;
    el('sshMobileKeys').hidden = !session;
    el('sshNew').disabled = busy;
    if (terminal) terminal.options.disableStdin = !session;
    el('sshProfiles').querySelectorAll('button').forEach(button => { button.disabled = busy || !allowed; });
    el('sshSessions').querySelectorAll('button').forEach(button => { button.disabled = busy || !allowed; });
    window.dispatchEvent(new CustomEvent('ssh-session-change'));
  }

  function renderProfiles() {
    const list = el('sshProfiles'); list.replaceChildren();
    if (!profiles.length) {
      const empty = document.createElement('p'); empty.className = 'muted'; empty.textContent = '暂无保存的连接'; list.append(empty);
    }
    for (const profile of profiles) {
      const row = document.createElement('div'); row.className = 'ssh-profile';
      const button = document.createElement('button'); button.type = 'button'; button.className = 'ssh-profile-select';
      button.classList.toggle('active', profile.id === profileId);
      const name = document.createElement('strong'); name.textContent = profile.name;
      const address = document.createElement('span'); address.textContent = `${profile.username}@${profile.host}:${profile.port}`;
      button.append(name, address);
      button.addEventListener('click', () => {
        profileId = profile.id; credentials();
        el('sshHost').value = profile.host; el('sshPort').value = profile.port;
        el('sshUsername').value = profile.username; el('sshName').value = profile.name;
        renderProfiles(); controls();
      });
      const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'toolbar-icon';
      remove.textContent = '×'; remove.title = '移除保存的地址'; remove.setAttribute('aria-label', `移除 ${profile.name} 的地址`);
      remove.addEventListener('click', async () => {
        try {
          profiles = (await api('/ssh/profiles', {action: 'delete', id: profile.id})).profiles;
          if (profileId === profile.id) profileId = '';
          renderProfiles(); controls(); feedback('已移除连接地址。');
        } catch (error) { feedback(error.message); }
      });
      row.append(button, remove); list.append(row);
    }
  }

  function updateAccess(data) {
    token = data.csrf_token || token;
    allowed = !!((data.secure && window.isSecureContext) || data.http_enabled === true);
    el('sshSecurityNotice').hidden = !!(data.secure && window.isSecureContext);
    el('sshHttpConsent').hidden = data.http_enabled === true;
    el('sshHttpTitle').textContent = data.http_enabled ? 'HTTP 已允许 · 仅限本次登录' : '当前通过 HTTP 访问';
    el('sshTransportLabel').textContent = data.http_enabled && !data.secure ? 'SSH 通道加密 · 面板 HTTP 明文' : 'SSH 通道加密';
    if (data.http_enabled) el('sshHttpFeedback').textContent = '退出并重新登录后，需要再次确认。';
    controls();
  }
  el('sshHttpCheck').addEventListener('change', () => { el('sshAllowHttp').disabled = allowingHttp || !el('sshHttpCheck').checked; });
  el('sshAllowHttp').addEventListener('click', async () => {
    if (allowingHttp || !el('sshHttpCheck').checked) return;
    allowingHttp = true; el('sshAllowHttp').disabled = true;
    try {
      await api('/ssh/allow-http', {consent: true});
      await refresh(); window.TerminalAI?.viewChanged('terminal');
    } catch (error) { el('sshHttpFeedback').textContent = error.message; el('sshRelogin').hidden = error.status !== 401; }
    finally { allowingHttp = false; el('sshAllowHttp').disabled = !el('sshHttpCheck').checked; }
  });

  function refresh() {
    if (refreshPromise) return refreshPromise;
    refreshPromise = refreshStatus().finally(() => { refreshPromise = undefined; });
    return refreshPromise;
  }
  async function refreshStatus() {
    try {
      const data = await api('/ssh');
      el('sshRelogin').hidden = true;
      profiles = data.profiles; available = data.available; updateAccess(data);
      renderProfiles();
      if (session && !data.sessions.some(item => item.id === session)) finish('连接已结束，请重新登录服务器。');
      let remembered = '';
      try { remembered = sessionStorage.getItem(sessionKey) || ''; } catch { /* Storage may be disabled. */ }
      const existing = data.sessions.find(item => item.id === (session || remembered)) || (!session && data.sessions.length === 1 ? data.sessions[0] : null);
      if (allowed && existing && (!session || suspended)) attach(existing);
      if (!available) feedback('SSH 组件尚未安装。请重新运行安装脚本，或执行 apt-get install -y python3-paramiko 后重启 mini-deploy-agent。');
      else if (!allowed) feedback('先在上方确认允许本次登录使用 HTTP，再填写连接信息。也可使用 HTTPS 或本地隧道。');
      else if (!session) feedback('填写服务器地址和登录凭据后连接。');
      const other = data.sessions.filter(item => login.open || item.id !== session);
      el('sshOtherSessions').hidden = !other.length;
      el('sshOtherSessions').open = login.open;
      el('sshSessions').replaceChildren();
      for (const item of other) {
        const row = document.createElement('div'); row.className = 'ssh-profile';
        const address = document.createElement('span'); address.textContent = `${item.username}@${item.host}:${item.port}`;
        const use = document.createElement('button'); use.className = 'ghost-button compact-action'; use.type = 'button'; use.textContent = '使用此连接'; use.disabled = !allowed;
        use.addEventListener('click', () => { attach(item); if (login.open) login.close('connected'); else refresh(); });
        const button = document.createElement('button'); button.className = 'ghost-button compact-action'; button.textContent = '断开'; button.type = 'button'; button.disabled = !allowed;
        button.addEventListener('click', async () => { try { await api('/ssh/disconnect', {id: item.id}); if (session === item.id) finish('连接已断开。'); await refresh(); } catch (error) { feedback(error.message); } });
        row.append(address, use, button); el('sshSessions').append(row);
      }
    } catch (error) { allowed = false; feedback(error.message); el('sshRelogin').hidden = error.status !== 401; }
    finally { controls(); }
  }

  function theme() {
    if (!terminal) return;
    const light = document.documentElement.dataset.theme === 'light';
    terminal.options.theme = light ? {
      background: '#fcfcfd', foreground: '#24272e', cursor: '#13734b', cursorAccent: '#fcfcfd', selectionBackground: '#cfe4f5',
      black: '#24272e', red: '#b42335', green: '#13734b', yellow: '#986500', blue: '#245bcb', magenta: '#9441a0', cyan: '#006f85', white: '#626b78',
      brightBlack: '#737b89', brightRed: '#cc203b', brightGreen: '#08794a', brightYellow: '#946000', brightBlue: '#175ad6', brightMagenta: '#a126ac', brightCyan: '#00718b', brightWhite: '#17191f'
    } : {
      background: '#101215', foreground: '#dce0e6', cursor: '#69ddb0', cursorAccent: '#101215', selectionBackground: '#344251',
      black: '#343942', red: '#f17f8c', green: '#69ddb0', yellow: '#ebc475', blue: '#83b1ff', magenta: '#d69deb', cyan: '#75d6e3', white: '#dce0e6',
      brightBlack: '#99a2af', brightRed: '#ff9aa5', brightGreen: '#91ebbd', brightYellow: '#f7d993', brightBlue: '#adcaff', brightMagenta: '#e8bafa', brightCyan: '#a0e7ef', brightWhite: '#ffffff'
    };
  }

  function ensureTerminal() {
    el('sshTerminal').hidden = false; el('sshEmpty').hidden = true;
    if (terminal) { terminal.reset(); return; }
    terminal = new Terminal({fontFamily: '"SFMono-Regular", Consolas, "Liberation Mono", monospace', fontSize: 14, lineHeight: 1.2,
      cursorBlink: true, cursorStyle: 'bar', scrollback: 3000, allowProposedApi: false, screenReaderMode: true, convertEol: false});
    fit = new FitAddon.FitAddon(); terminal.loadAddon(fit);
    terminal.open(el('sshTerminal')); theme();
    terminal.onData(queueInput);
    new ResizeObserver(() => { clearTimeout(resizeTimer); resizeTimer = setTimeout(resize, 120); }).observe(el('sshTerminal'));
    new MutationObserver(theme).observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme']});
    el('sshTerminal').addEventListener('paste', event => {
      const value = event.clipboardData?.getData('text/plain');
      if (value !== undefined) { event.preventDefault(); event.stopImmediatePropagation(); paste(value); }
    }, true);
    resize(); controls();
  }

  function paste(value) {
    if (!session || !value) return;
    if (new TextEncoder().encode(value).length > 65536) { feedback('粘贴内容超过 64 KB，请分段粘贴。'); return; }
    if (/\r|\n/.test(value) && !confirm('粘贴内容含换行，可能立即执行多条命令。确认粘贴？')) return;
    terminal.paste(value); terminal.focus();
  }

  async function resize() {
    if (!terminal || activeView !== 'terminal' || !el('sshTerminal').clientWidth) return;
    fit.fit();
    const cols = Math.max(20, Math.min(300, terminal.cols)), rows = Math.max(5, Math.min(120, terminal.rows));
    const size = `${cols}:${rows}`;
    if (!session || size === lastSize) return;
    lastSize = size;
    try { await api('/ssh/resize', {id: session, cols, rows}); }
    catch (error) { lastSize = ''; feedback(error.message); }
  }

  function queueInput(value) {
    if (!session) return;
    if (new TextEncoder().encode(input + value).length > 65536) { feedback('输入队列已满，请等待远程服务器处理。'); return; }
    input += value;
    if (!sending && !inputTimer) inputTimer = setTimeout(() => { inputTimer = undefined; sendInput(); }, 0);
  }

  async function sendInput() {
    if (sending || !session) return;
    sending = true;
    const id = session, current = generation;
    try {
      while (input && current === generation && id === session) {
        // Slice by Unicode code points so multibyte characters survive batching.
        const points = Array.from(input), chunk = points.slice(0, 3000).join('');
        input = points.slice(3000).join('');
        await api('/ssh/input', {id, data: bytes64(chunk)});
      }
    } catch (error) {
      if (current === generation) {
        input = '';
        if ([401, 403, 404, 410].includes(error.status)) finish(error.message);
        else state('网络重试中');
        feedback(`${error.message} 未自动重发输入，请核对服务器状态。`, 'error');
        el('sshRelogin').hidden = error.status !== 401;
      }
    } finally { sending = false; if (input && session) sendInput(); }
  }

  async function renderOutput(data, id, current) {
    if (session !== id || generation !== current) return false;
    if (data.gap) terminal.writeln('\r\n[输出量较大，部分早期内容已跳过]\r\n');
    if (data.data) {
      const bytes = Uint8Array.from(atob(data.data), c => c.charCodeAt(0));
      await new Promise(resolve => terminal.write(bytes, resolve));
    }
    if (session !== id || generation !== current) return false;
    cursor = data.cursor;
    if (data.closed) { finish(data.reason || '会话已结束。'); return false; }
    state('已连接', true);
    return true;
  }

  async function streamOutput(id, current, onPacket) {
    const controller = new AbortController(); outputController = controller;
    let timer, reader;
    const heartbeat = () => { clearTimeout(timer); timer = setTimeout(() => controller.abort(), 35000); };
    heartbeat();
    try {
      const response = await api(`/ssh/stream?id=${encodeURIComponent(id)}&cursor=${cursor}`, undefined,
        {raw: true, signal: controller.signal});
      reader = response.body.getReader();
      const decoder = new TextDecoder(); let pending = '';
      while (session === id && generation === current) {
        const {value, done} = await reader.read();
        if (done) {
          if (pending) throw new Error('终端输出不完整，正在重新读取。');
          return;
        }
        heartbeat(); pending += decoder.decode(value, {stream: true});
        let end;
        while ((end = pending.indexOf('\n')) >= 0) {
          if (end > 512 * 1024) throw new Error('终端输出超过单批上限。');
          const line = pending.slice(0, end); pending = pending.slice(end + 1);
          if (!line) continue;
          if (!(await renderOutput(JSON.parse(line), id, current))) return;
          onPacket();
        }
        if (pending.length > 512 * 1024) throw new Error('终端输出超过单批上限。');
      }
    } finally {
      clearTimeout(timer); controller.abort();
      if (reader) { try { await reader.cancel(); } catch { /* Request may already be aborted. */ } }
      if (outputController === controller) outputController = undefined;
    }
  }

  async function poll(id, current) {
    let failures = 0, streaming = true;
    while (session === id && generation === current) {
      try {
        if (streaming) await streamOutput(id, current, () => { failures = 0; });
        else {
          const data = await api(`/ssh/output?id=${encodeURIComponent(id)}&cursor=${cursor}`);
          if (!(await renderOutput(data, id, current))) return;
          failures = 0;
        }
      } catch (error) {
        if (session !== id || generation !== current) return;
        if (streaming && error.status === 404) { streaming = false; continue; }
        if ([401, 403, 404, 410].includes(error.status)) {
          finish(error.message); feedback(error.message, 'error'); el('sshRelogin').hidden = error.status !== 401; return;
        }
        state('网络重试中');
        failures = Math.min(failures + 1, 5);
        feedback('终端输出暂时中断，正在恢复连接…');
        await new Promise(resolve => setTimeout(resolve, Math.min(15000, 1000 * 2 ** (failures - 1))));
      }
    }
  }

  function trust(probe) {
    if (probe.state === 'known') return Promise.resolve(true);
    const dialog = el('sshFingerprintDialog');
    el('sshFingerprintTitle').textContent = probe.state === 'changed' ? '服务器指纹发生变化' : '首次连接，核对服务器指纹';
    el('sshFingerprintMessage').textContent = probe.state === 'changed'
      ? '服务器重装也可能改变指纹。若无法确认变化原因，请取消连接。'
      : '确认是你要连接的服务器后，面板才会发送登录凭据。';
    el('sshFingerprintHost').textContent = `${probe.username}@${probe.host}:${probe.port}`;
    el('sshFingerprintValue').textContent = `${probe.algorithm} · ${probe.fingerprint}`;
    el('sshPreviousLabel').hidden = el('sshPreviousValue').hidden = !probe.expected;
    el('sshPreviousValue').textContent = probe.expected;
    el('sshTrustCheck').checked = false; el('sshTrustConfirm').disabled = true;
    dialog.returnValue = 'cancel';
    dialog.showModal();
    return new Promise(resolve => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'trust' && el('sshTrustCheck').checked), {once: true}));
  }

  function finish(reason) {
    session = ''; generation++; input = ''; lastSize = ''; clearTimeout(inputTimer); inputTimer = undefined;
    try { sessionStorage.removeItem(sessionKey); } catch { /* Storage may be disabled. */ }
    outputController?.abort();
    credentials(); state('已断开'); controls(); feedback(reason);
  }

  async function disconnect({strict = false} = {}) {
    const id = session; finish('连接已断开。');
    if (id) try { await api('/ssh/disconnect', {id}); } catch (error) {
      if ([404, 410].includes(error.status)) return;
      feedback(error.message);
      if (strict) throw error;
    }
  }

  function attach(data) {
    if (session === data.id && !suspended) return;
    generation++; outputController?.abort(); input = ''; cursor = 0; lastSize = ''; suspended = false;
    clearTimeout(inputTimer); inputTimer = undefined;
    ensureTerminal(); session = data.id;
    try { sessionStorage.setItem(sessionKey, session); } catch { /* Credentials are never stored. */ }
    el('sshTarget').replaceChildren();
    for (const [kind, value] of [['user', data.username], ['separator', '@'], ['host', data.host], ['port', `:${data.port}`]]) {
      const span = document.createElement('span'); span.className = `ssh-target-${kind}`; span.textContent = value; el('sshTarget').append(span);
    }
    state('已连接', true); feedback('连接已建立，可在 AI 助手和远程终端中继续使用。', 'success'); controls();
    poll(session, generation); requestAnimationFrame(resize);
  }

  async function requestConnection({manage = false} = {}) {
    if (login.open) return new Promise(resolve => login.addEventListener('close', () => resolve(login.returnValue === 'connected'), {once:true}));
    await refresh();
    if (login.open) return new Promise(resolve => login.addEventListener('close', () => resolve(login.returnValue === 'connected'), {once:true}));
    if (session && !manage) return true;
    for (const {node} of loginParts) login.querySelector('.ssh-login-body').append(node);
    credentials(); login.returnValue = 'cancel'; login.showModal(); controls();
    const completed = new Promise(resolve => { loginResolve = resolve; });
    refresh(); el('sshHost').focus();
    return completed;
  }
  el('sshLoginClose').onclick = () => { if (!busy) login.close('cancel'); };
  login.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
  login.addEventListener('close', () => {
    credentials(); for (const {node, anchor} of loginParts) anchor.after(node);
    controls(); loginResolve?.(login.returnValue === 'connected'); loginResolve = undefined;
    requestAnimationFrame(resize);
  });

  el('sshForm').addEventListener('submit', async event => {
    event.preventDefault(); if (busy || (session && !login.open) || !allowed || !available) return;
    busy = true; controls(); state('检查主机中'); feedback('正在连接 SSH 服务并检查主机指纹…');
    try {
      const probe = await api('/ssh/probe', spec());
      if (!(await trust(probe))) { state(session ? '已连接' : '未连接', !!session); feedback('已取消，未发送登录凭据。'); return; }
      state('认证中'); feedback('正在认证并启动终端…');
      const data = await api('/ssh/connect', {ticket: probe.ticket, accept_host_key: true, auth: el('sshAuth').value,
        password: el('sshPassword').value, private_key: el('sshPrivateKey').value, passphrase: el('sshPassphrase').value,
        cols: Math.max(20, Math.min(300, terminal?.cols || 80)), rows: Math.max(5, Math.min(120, terminal?.rows || 24))});
      attach(data);
      if (matchMedia('(max-width: 760px)').matches) collapseConnections(true);
      if (login.open) login.close('connected'); else terminal.focus();
    } catch (error) { state(session ? '已连接' : '连接失败', !!session); feedback(error.message, 'error'); }
    finally { busy = false; credentials(); controls(); }
  });
  el('sshTrustCheck').addEventListener('change', () => { el('sshTrustConfirm').disabled = !el('sshTrustCheck').checked; });
  el('sshAuth').addEventListener('change', () => {
    credentials(); el('sshPasswordField').hidden = el('sshAuth').value !== 'password';
    el('sshKeyFields').hidden = el('sshAuth').value !== 'key';
  });
  el('sshKeyFile').addEventListener('change', async () => {
    const file = el('sshKeyFile').files[0]; if (!file) return;
    if (file.size > 32768) { el('sshKeyFile').value = ''; feedback('私钥文件超过 32 KB，请检查是否选对文件。'); return; }
    try { el('sshPrivateKey').value = await file.text(); } catch { feedback('无法读取私钥文件。'); }
  });
  el('sshSave').addEventListener('click', async () => {
    if (!el('sshHost').reportValidity() || !el('sshPort').reportValidity() || !el('sshUsername').reportValidity()) return;
    try {
      profiles = (await api('/ssh/profiles', {action: 'save', id: profileId, name: el('sshName').value, ...spec()})).profiles;
      if (!profileId) profileId = profiles[profiles.length - 1].id;
      renderProfiles(); controls(); feedback('连接地址已保存，密码和私钥未保存。');
    } catch (error) { feedback(error.message); }
  });
  el('sshNew').addEventListener('click', async () => {
    if (busy) return;
    profileId = ''; credentials(); el('sshHost').value = el('sshName').value = '';
    el('sshPort').value = '22'; el('sshUsername').value = 'root';
    await requestConnection({manage:true});
  });
  el('sshNew').title = '登录其他服务器，保留已有连接';
  el('sshConnectionToggle').addEventListener('click', () => collapseConnections(el('sshConnectionToggle').getAttribute('aria-expanded') === 'true'));
  el('sshDisconnect').addEventListener('click', disconnect);
  el('sshClear').addEventListener('click', () => { terminal?.clear(); terminal?.focus(); });
  el('sshCopy').addEventListener('click', async () => {
    try { const selected = terminal?.getSelection(); if (selected) await navigator.clipboard.writeText(selected); else feedback('请先选择要复制的终端内容。'); }
    catch { feedback('浏览器禁止读取剪贴板，请选择内容后使用系统复制。'); }
  });
  el('sshPaste').addEventListener('click', async () => { try { paste(await navigator.clipboard.readText()); } catch { feedback('浏览器禁止读取剪贴板，请使用系统粘贴。'); } });
  el('sshMobileKeys').addEventListener('click', event => {
    const key = event.target.closest('[data-ssh-key]')?.dataset.sshKey;
    if (key) { queueInput(key.replace(/\\u([0-9a-f]{4})/gi, (_, hex) => String.fromCharCode(parseInt(hex, 16)))); terminal?.focus(); }
  });
  el('terminalViewTab').addEventListener('click', () => setView('terminal'));
  window.addEventListener('pagehide', () => {
    suspended = true; generation++; outputController?.abort(); input = ''; credentials();
  });
  window.addEventListener('pageshow', event => { if (event.persisted) refresh(); });
  window.addEventListener('beforeunload', event => { if (busy) { event.preventDefault(); event.returnValue = ''; } });
  window.SSHTerminal = {
    updateAccess, requestConnection,
    context() { return {session_id: session, selection: terminal?.getSelection() || '', cursor,
                       target: session ? el('sshTarget').textContent : ''}; },
    fileRequest(body, options) {
      if (!session || !allowed) return Promise.reject(new Error('请先连接远程 SSH 服务器'));
      return api('/ssh/files', {...body, id:session}, options);
    },
    insertCommand(command) {
      if (!session || !terminal || /[\x00-\x1f\x7f]/.test(command) || command.length > 3000) return false;
      if (!confirm(`将以下命令填入当前终端，不自动按回车。检查后自行执行：\n\n${command}`)) return false;
      terminal.paste(command); terminal.focus(); return true;
    },
    viewChanged(view) { if (view === 'terminal') { refresh(); requestAnimationFrame(resize); } }
  };
  try { collapseConnections(localStorage.getItem('mini-deploy-ssh-collapsed') === 'true'); } catch { /* Use expanded default. */ }
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && activeView === 'terminal') { refresh(); requestAnimationFrame(resize); }
  });
  if (activeView === 'terminal') window.SSHTerminal.viewChanged('terminal');
  else { try { if (sessionStorage.getItem(sessionKey)) refresh(); } catch { /* Storage may be disabled. */ } }
})();
