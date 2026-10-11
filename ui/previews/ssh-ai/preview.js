(() => {
  const el = id => document.getElementById(id);
  const mobile = matchMedia('(max-width:760px)');
  let connected = true, assistantOpen = !mobile.matches, analyzing = false, healthy = false;
  let promptInput = '', autoTimer, toastTimer, fitTimer;
  let selectedServer = 'production', selectedCommand = 'inspect';
  let newConnection = false, connectionCount = 0;
  const servers = {
    production: {name: '生产服务器', host: 'production-01', address: '192.0.2.10', user: 'root', port: 22},
    staging: {name: '测试服务器', host: 'staging-01', address: '192.0.2.20', user: 'debian', port: 22}
  };
  const commands = {
    inspect: "docker inspect api postgres \\\n  --format '{{.Name}} {{json .NetworkSettings.Networks}}'",
    repair: 'docker network connect app_internal api'
  };
  const initialAnswer = el('answer').innerHTML;
  const terminal = new Terminal({fontFamily: '"SFMono-Regular", Consolas, "Liberation Mono", monospace',
    fontSize: 12, lineHeight: 1.4, cursorBlink: true, cursorStyle: 'bar', scrollback: 1500, screenReaderMode: true});
  const fit = new FitAddon.FitAddon(); terminal.loadAddon(fit); terminal.open(el('terminal'));

  function icons(root = document) {
    root.querySelectorAll('[data-icon]').forEach(icon => {
      icon.style.setProperty('--icon', `url("icons/${icon.dataset.icon}.svg")`);
      icon.setAttribute('aria-hidden', 'true');
    });
  }
  function toast(message) {
    el('toast').textContent = message; el('toast').hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { el('toast').hidden = true; }, 3000);
  }
  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    const light = theme === 'light';
    terminal.options.theme = light ? {
      background: '#fcfcfd', foreground: '#303640', cursor: '#167b59', selectionBackground: '#cce5ee',
      black: '#303640', red: '#bc4656', green: '#167b59', yellow: '#a16c14', blue: '#315bbb', cyan: '#147c94', magenta: '#934999', white: '#626b78',
      brightBlack: '#757a84', brightRed: '#bc4656', brightGreen: '#167b59', brightYellow: '#a16c14', brightBlue: '#315bbb', brightCyan: '#147c94', brightMagenta: '#934999', brightWhite: '#24272e'
    } : {
      background: '#121417', foreground: '#d2d8e1', cursor: '#79dcb2', selectionBackground: '#334758',
      black: '#363a43', red: '#f08e9a', green: '#79dcb2', yellow: '#e4bc72', blue: '#91b7fa', cyan: '#88cedb', magenta: '#d6a3df', white: '#d2d8e1',
      brightBlack: '#979da8', brightRed: '#f08e9a', brightGreen: '#79dcb2', brightYellow: '#e4bc72', brightBlue: '#91b7fa', brightCyan: '#88cedb', brightMagenta: '#d6a3df', brightWhite: '#fff'
    };
    el('themeToggle').querySelector('i').dataset.icon = light ? 'moon' : 'sun'; icons(el('themeToggle'));
  }
  function resize() {
    if (!el('terminal').clientWidth) return;
    fit.fit(); el('terminalFeedback').textContent = `UTF-8 · ${terminal.cols} × ${terminal.rows}`;
  }
  function prompt() {
    const server = servers[selectedServer];
    terminal.write(`\x1b[36m${server.user}\x1b[0m@\x1b[32m${server.host}\x1b[0m:\x1b[34m~/app\x1b[0m$ `);
  }
  function sampleOutput() {
    terminal.reset(); promptInput = ''; healthy = false;
    const server = servers[selectedServer];
    const lines = [
      '\x1b[90mLast login: Mon Oct 12 09:42:08 from 192.0.2.1\x1b[0m', '',
      `\x1b[36m${server.user}\x1b[0m@\x1b[32m${server.host}\x1b[0m:\x1b[34m~/app\x1b[0m$ docker ps`,
      '\x1b[90mNAMES       STATUS                  PORTS\x1b[0m',
      'api         \x1b[33mUp 6 minutes (unhealthy)\x1b[0m  8000/tcp',
      'postgres    \x1b[32mUp 2 days (healthy)\x1b[0m     5432/tcp', '',
      `\x1b[36m${server.user}\x1b[0m@\x1b[32m${server.host}\x1b[0m:\x1b[34m~/app\x1b[0m$ docker logs --tail 20 api`,
      '\x1b[90m09:42:11\x1b[0m \x1b[36mINFO\x1b[0m  Started server process [1]',
      '\x1b[90m09:42:11\x1b[0m \x1b[36mINFO\x1b[0m  Listening on 0.0.0.0:8000',
      '\x1b[90m09:42:12\x1b[0m \x1b[31mERROR\x1b[0m Database connection failed',
      '\x1b[31mpsycopg.OperationalError: could not translate',
      'host name "postgres" to address\x1b[0m',
      '\x1b[90m09:42:15\x1b[0m \x1b[33mWARN\x1b[0m  Retrying database connection (1/3)',
      '\x1b[90m09:42:18\x1b[0m \x1b[31mERROR\x1b[0m Database connection failed',
      '\x1b[90m09:42:21\x1b[0m \x1b[33mWARN\x1b[0m  Retrying database connection (2/3)', '',
      `\x1b[36m${server.user}\x1b[0m@\x1b[32m${server.host}\x1b[0m:\x1b[34m~/app\x1b[0m$ docker inspect api postgres`,
      '\x1b[90mCONTAINER   NETWORK\x1b[0m',
      'api         \x1b[33mapp_public\x1b[0m',
      'postgres    \x1b[36mapp_internal\x1b[0m', ''
    ];
    terminal.write(lines.join('\r\n') + '\r\n', prompt);
    el('incidentTitle').textContent = 'api 数据库连接异常';
    el('incidentDetail').textContent = '最近 5 分钟出现 6 次解析失败';
  }
  function showAssistant(open) {
    assistantOpen = open; el('workspace').classList.toggle('assistant-hidden', !open);
    el('assistantToggle').setAttribute('aria-expanded', String(open));
    el('assistantToggle').setAttribute('aria-label', open ? '收起诊断助手' : '展开诊断助手');
    el('assistantToggle').title = open ? '收起诊断助手' : '展开诊断助手';
    el('assistantToggle').querySelector('i').dataset.icon = open ? 'panel-right-close' : 'panel-right-open';
    icons(el('assistantToggle')); el('assistantBackdrop').hidden = !open || !mobile.matches;
    requestAnimationFrame(resize);
  }
  function showDialog(id) { const dialog = el(id); dialog.returnValue = ''; dialog.showModal(); }
  function updateTarget() {
    const server = servers[selectedServer];
    el('targetUser').textContent = server.user; el('targetHost').textContent = el('consoleName').textContent = server.host;
    el('commandTargetName').textContent = server.name; el('commandTargetAddress').textContent = `${server.user}@${server.address}:${server.port}`;
    document.querySelector('.target-port').textContent = `:${server.port}`;
    el('connectionState').replaceChildren();
    const dot = document.createElement('span'); dot.className = `dot ${connected ? 'green' : 'neutral'}`;
    el('connectionState').append(dot, connected ? '已连接' : '已断开');
    el('connectionState').style.color = connected ? 'var(--green)' : 'var(--muted)';
    el('disconnect').title = connected ? '断开连接' : '重新连接';
    el('disconnect').setAttribute('aria-label', el('disconnect').title);
    document.querySelectorAll('[data-server]').forEach(button => {
      button.classList.toggle('selected', button.dataset.server === selectedServer);
      button.querySelector('.dot').className = `dot ${button.dataset.server === selectedServer && connected ? 'green' : 'neutral'}`;
    });
  }
  function restoreAnswer() {
    el('answer').innerHTML = initialAnswer; icons(el('answer'));
    const selected = [[el('includeOutput'), '终端片段'], [el('includeContainers'), '容器状态'], [el('includeResources'), '服务器资源']]
      .filter(([input]) => input.checked).map(([, label]) => label);
    el('scopeLabel').textContent = `依据${selected.join('与')}`;
    if (healthy) {
      el('findingTitle').textContent = '数据库连接已恢复';
      el('findingDescription').textContent = '两个容器已接入 app_internal，数据库主机名能够解析，最近输出未再出现连接错误。';
      el('evidenceText').textContent = 'INFO Database connection established';
      el('repairStep').hidden = true;
    }
  }
  async function analyze(question) {
    if (analyzing) return;
    if (![el('includeOutput'), el('includeContainers'), el('includeResources')].some(input => input.checked)) {
      toast('请至少选择一项分析内容。'); showDialog('scopeDialog'); return;
    }
    analyzing = true; showAssistant(true);
    el('questionText').textContent = question || '分析最近的终端输出';
    el('askSubmit').disabled = el('analyzeOutput').disabled = el('analyzeIncident').disabled = true;
    el('answer').innerHTML = '<div class="analysis-progress"><i data-icon="loader-circle"></i><span id="progressText">整理选中的内容…</span></div>';
    icons(el('answer')); el('answerTime').textContent = '分析中'; el('composeStatus').textContent = '正在生成诊断';
    el('assistantBody').scrollTop = 0;
    try {
      for (const stage of ['整理终端片段…', '核对容器状态与网络…', '生成诊断与排查建议…']) {
        el('progressText').textContent = stage;
        await new Promise(resolve => setTimeout(resolve, 400));
      }
      restoreAnswer();
      if (/内存|cpu|磁盘|资源/i.test(question)) {
        el('findingTitle').textContent = '当前资源未见异常';
        el('findingDescription').textContent = '示例服务器 CPU 使用率 12.4%，内存占用 1.68 / 4 GB。当前数据库错误更符合容器网络问题。';
      }
      if (!el('includeContainers').checked) {
        el('findingTitle').textContent = '数据库主机名解析失败';
        el('findingDescription').textContent = '日志显示 postgres 无法解析。需要容器网络信息，才能进一步区分网络未连接与主机名配置错误。';
        el('answer').querySelector('.network-comparison').hidden = true;
        el('repairStep').hidden = true;
      }
      el('answer').classList.remove('updated'); void el('answer').offsetWidth; el('answer').classList.add('updated');
      el('answerTime').textContent = '刚刚';
    } finally {
      analyzing = false; el('askSubmit').disabled = el('analyzeOutput').disabled = el('analyzeIncident').disabled = false;
      el('composeStatus').textContent = '建议命令需确认执行';
    }
  }
  function openCommand(kind) {
    selectedCommand = kind; const repair = kind === 'repair';
    el('commandTitle').textContent = repair ? '确认网络调整' : '运行检查';
    el('commandExplanation').textContent = repair ? '将 api 接入 app_internal 网络。这会改变容器的网络连接，不修改服务器文件。' : '读取两个容器的网络配置，不修改容器或服务器文件。';
    el('commandPreview').textContent = commands[kind];
    el('commandConsent').hidden = !repair; el('commandCheck').checked = false;
    el('commandExecute').disabled = repair; showDialog('commandDialog');
  }
  async function copy(value) {
    try { await navigator.clipboard.writeText(value); toast('已复制。'); }
    catch { toast('复制不可用，请选择文本后使用系统复制。'); }
  }
  function runSample(command, approved = false) {
    if (!connected) { toast('请先连接示例服务器。'); return; }
    const normalized = command.replace(/\\\s*\n/g, ' ').trim();
    if (normalized.includes('docker network connect') && !approved) { openCommand('repair'); return; }
    terminal.write(command.replace(/\n/g, '\r\n') + '\r\n');
    if (normalized.includes('docker inspect')) {
      terminal.writeln(`/api {"${healthy ? 'app_internal' : 'app_public'}":{"IPAddress":"172.20.0.3"}}`);
      terminal.writeln('/postgres {"app_internal":{"IPAddress":"172.21.0.2"}}');
      toast('示例检查完成，网络信息已显示在终端。');
    } else if (normalized.includes('docker network connect')) {
      healthy = true;
      terminal.writeln('\x1b[32m[示例] api connected to app_internal\x1b[0m');
      terminal.writeln('\x1b[36mINFO\x1b[0m Database connection established');
      el('incidentTitle').textContent = 'api 数据库连接已恢复'; el('incidentDetail').textContent = '示例检查通过，未发现新的连接错误';
      analyze('处理后还有异常吗？');
    } else if (normalized.includes('docker ps')) {
      terminal.writeln(`api        Up 6 minutes (${healthy ? 'healthy' : 'unhealthy'})    8000/tcp`);
      terminal.writeln('postgres   Up 2 days (healthy)      5432/tcp');
    } else if (normalized) terminal.writeln('\x1b[90m[示例环境] 该命令未在服务器执行。\x1b[0m');
    prompt();
  }

  terminal.onData(value => {
    if (!connected) return;
    if (value === '\x03') { terminal.write('^C\r\n'); promptInput = ''; prompt(); return; }
    if (value.startsWith('\x1b')) return;
    for (const character of value) {
      if (character === '\r' || character === '\n') {
        const command = promptInput; terminal.write('\r\n'); promptInput = '';
        // Echo was already shown while typing; only the sample response follows.
        if (command) { terminal.writeln('\x1b[90m[示例环境] 未在服务器执行。\x1b[0m'); }
        prompt();
      } else if (character === '\x7f') {
        if (promptInput) { promptInput = Array.from(promptInput).slice(0, -1).join(''); terminal.write('\b \b'); }
      } else if (character >= ' ') { promptInput += character; terminal.write(character); }
    }
  });
  el('themeToggle').addEventListener('click', () => applyTheme(document.documentElement.dataset.theme === 'light' ? 'dark' : 'light'));
  el('assistantToggle').addEventListener('click', () => showAssistant(!assistantOpen));
  el('assistantClose').addEventListener('click', () => showAssistant(false));
  el('assistantBackdrop').addEventListener('click', () => showAssistant(false));
  el('settingsOpen').addEventListener('click', () => showDialog('settingsDialog'));
  el('assistantModel').addEventListener('click', () => showDialog('settingsDialog'));
  el('settingsForm').addEventListener('submit', event => {
    event.preventDefault(); el('modelKey').value = '';
    el('assistantModel').firstChild.textContent = `${el('modelName').value.trim().slice(0, 40)} · 预览`;
    el('settingsDialog').close(); toast('模型设置已应用到本次预览，未发送请求。');
  });
  el('contextChange').addEventListener('click', () => showDialog('scopeDialog'));
  el('saveScope').addEventListener('click', () => {
    const choices = [[el('includeOutput'), '终端片段'], [el('includeContainers'), '容器状态'], [el('includeResources'), '服务器资源']];
    const selected = choices.filter(([input]) => input.checked).map(([, label]) => label);
    if (!selected.length) { toast('请至少选择一项分析内容。'); return; }
    el('contextLabel').textContent = selected.join(' + ');
    el('scopeLabel').textContent = `依据${selected.join('与')}`;
    el('scopeDialog').close(); toast('分析范围已更新。');
  });
  el('askForm').addEventListener('submit', event => {
    event.preventDefault(); const question = el('askInput').value.trim();
    if (!question) { el('askInput').focus(); return; }
    el('askInput').value = ''; analyze(question);
  });
  el('analyzeOutput').addEventListener('click', () => {
    const selection = terminal.getSelection();
    el('contextLabel').textContent = selection ? `选中片段 · ${selection.split('\n').length} 行` : '最近输出 + 容器状态';
    analyze(selection ? '解释选中的终端内容，并给出排查建议。' : '分析最近的终端输出，为什么 api 连接不上数据库？');
  });
  el('analyzeIncident').addEventListener('click', () => { showAssistant(true); el('assistantBody').scrollTop = 0; });
  el('answer').addEventListener('click', event => {
    const button = event.target.closest('button'); if (!button) return;
    if (button.id === 'scopeOpen') showDialog('scopeDialog');
    else if (button.id === 'repairOpen') openCommand('repair');
    else if (button.id === 'locateEvidence') {
      let found = -1;
      for (let index = 0; index < terminal.buffer.active.length; index++) {
        if (terminal.buffer.active.getLine(index)?.translateToString().includes('host name "postgres"')) { found = index; break; }
      }
      if (found >= 0) { terminal.selectLines(Math.max(0, found - 1), found); terminal.scrollToLine(Math.max(0, found - 4)); terminal.focus(); }
      else toast('当前显示已清空，请重新载入示例输出。');
    } else if (button.dataset.command) {
      if (button.classList.contains('command-copy')) copy(commands[button.dataset.command]);
      else openCommand(button.dataset.command);
    }
  });
  el('commandCheck').addEventListener('change', () => { el('commandExecute').disabled = !el('commandCheck').checked; });
  el('commandCopy').addEventListener('click', () => copy(commands[selectedCommand]));
  el('commandExecute').addEventListener('click', () => {
    if (selectedCommand === 'repair' && !el('commandCheck').checked) return;
    if (!connected) { toast('请先重新连接示例服务器。'); return; }
    el('commandDialog').close(); terminal.clearSelection(); runSample(commands[selectedCommand], true); terminal.focus();
  });
  el('autoAnalyze').addEventListener('change', () => {
    clearTimeout(autoTimer); const enabled = el('autoAnalyze').checked;
    el('watchText').textContent = enabled ? '观察新输出' : '按需分析';
    el('watchDot').className = `dot ${enabled ? 'green' : 'neutral'}`;
    if (enabled) autoTimer = setTimeout(() => {
      if (!connected || !el('autoAnalyze').checked || analyzing) return;
      terminal.writeln(healthy ? '\r\n\x1b[36mINFO\x1b[0m Database connection established' : '\r\n\x1b[90m09:43:06\x1b[0m \x1b[31mERROR\x1b[0m Database connection failed');
      prompt(); analyze(healthy ? '检查恢复后的状态。' : '检测到新的数据库错误，帮我分析原因。');
    }, 3500);
  });
  el('clearTerminal').addEventListener('click', () => { terminal.clear(); terminal.focus(); toast('已清空显示，示例服务器未受影响。'); });
  el('copyTerminal').addEventListener('click', () => { const selected = terminal.getSelection(); if (selected) copy(selected); else toast('请先选择终端内容。'); });
  el('disconnect').addEventListener('click', () => {
    connected = !connected; updateTarget(); clearTimeout(autoTimer);
    if (connected) { sampleOutput(); toast('示例连接已恢复。'); }
    else { terminal.writeln('\r\n\x1b[90m[示例连接已断开]\x1b[0m'); toast('示例连接已断开。'); }
  });
  document.querySelector('.servers').addEventListener('click', event => {
    const button = event.target.closest('[data-server]'); if (!button) return;
    selectedServer = button.dataset.server; connected = true; updateTarget(); sampleOutput(); restoreAnswer();
  });
  function connectionForm(isNew) {
    newConnection = isNew;
    const server = servers[selectedServer];
    el('connectionDialogTitle').textContent = isNew ? '新建连接' : '连接设置';
    el('serverName').value = isNew ? '' : server.name; el('serverAddress').value = isNew ? '' : server.address;
    el('serverUser').value = isNew ? 'root' : server.user;
    el('serverPort').value = isNew ? 22 : server.port; showDialog('connectionDialog');
  }
  el('newConnection').addEventListener('click', () => connectionForm(true));
  el('connectionOptions').addEventListener('click', () => connectionForm(false));
  el('connectionForm').addEventListener('submit', event => {
    event.preventDefault();
    if (newConnection) {
      selectedServer = `example-${++connectionCount}`;
      servers[selectedServer] = {host: `example-${connectionCount}`};
      const newButton = document.querySelector('[data-server]').cloneNode(true);
      newButton.dataset.server = selectedServer;
      document.querySelector('.servers').insertBefore(newButton, document.querySelector('.server-meta'));
      icons(newButton);
    }
    const server = servers[selectedServer];
    server.name = el('serverName').value; server.address = el('serverAddress').value; server.user = el('serverUser').value;
    server.port = Number(el('serverPort').value);
    const button = document.querySelector(`[data-server="${selectedServer}"]`);
    button.querySelector('strong').textContent = server.name; button.querySelector('small').textContent = `${server.user}@${server.address}`;
    connected = true; updateTarget(); sampleOutput(); restoreAnswer(); el('connectionDialog').close(); toast('已更新本次预览的连接信息。');
  });
  document.querySelectorAll('.dialog-close').forEach(button => button.addEventListener('click', () => { button.closest('dialog').close(); el('modelKey').value = ''; }));
  el('settingsDialog').addEventListener('close', () => { el('modelKey').value = ''; });
  document.querySelectorAll('[data-nav]').forEach(button => button.addEventListener('click', () => toast(`${button.dataset.nav}请在当前服务器面板查看；此页面预览终端与诊断。`)));
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && !document.querySelector('dialog[open]') && mobile.matches) showAssistant(false); });
  mobile.addEventListener('change', () => { el('assistantBackdrop').hidden = !assistantOpen || !mobile.matches; resize(); });
  new ResizeObserver(() => { clearTimeout(fitTimer); fitTimer = setTimeout(resize, 60); }).observe(el('terminal'));
  icons(); applyTheme('light'); updateTarget(); showAssistant(assistantOpen); resize(); sampleOutput();
})();
